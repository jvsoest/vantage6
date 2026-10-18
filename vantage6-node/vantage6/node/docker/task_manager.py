# TODO the task folder is also created by this class. This folder needs
# to be cleaned at some point.
import logging
from socket import SocketIO
import threading
import docker.errors
import json

from pathlib import Path
from docker import DockerClient

from vantage6.common.globals import APPNAME
from vantage6.common.docker.addons import (
    remove_container_if_exists,
    remove_container,
    running_in_docker,
)
from vantage6.common.docker.network_manager import NetworkManager
from vantage6.common.task_status import TaskStatus
from vantage6.node.util import get_parent_id
from vantage6.node.globals import ALPINE_IMAGE
from vantage6.node.docker.vpn_manager import VPNManager
from vantage6.node.docker.squid import Squid
from vantage6.node.docker.docker_base import DockerBaseManager
from vantage6.node.docker.exceptions import (
    UnknownAlgorithmStartFail,
    PermanentAlgorithmStartFail,
    AlgorithmContainerNotFound,
)
from vantage6.node.runtime.run_io import RunIOMixin


class DockerTaskManager(RunIOMixin, DockerBaseManager):
    """
    Manager for running a vantage6 algorithm container within docker.

    Ensures that the environment is properly set up (docker volumes,
    directories, environment variables, etc). Then runs the algorithm as a
    docker container. Finally, it monitors the container state and can return
    it's results when the algorithm finished.
    """

    def __init__(
        self,
        image: str,
        docker_client: DockerClient,
        vpn_manager: VPNManager | None,
        node_name: str,
        node_id: int,
        run_id: int,
        task_info: dict,
        tasks_dir: Path,
        isolated_network_mgr: NetworkManager,
        databases: dict,
        docker_volume_name: str,
        socketIO: SocketIO,
        collaboration_id: int,
        alpine_image: str | None = None,
        proxy: Squid | None = None,
        device_requests: list | None = None,
        requires_pull: bool = False,
        share_algorithm_logs: bool = False,
        write_run_context_file: bool = False,
    ):
        """
        Initialization creates DockerTaskManager instance

        Parameters
        ----------
        image: str
            Name of docker image to be run
        docker_client: DockerClient
            Docker client instance to use
        vpn_manager: VPNManager | None
            VPN manager required to set up traffic forwarding via VPN, or None
        node_name: str
            Name of the node, to track running algorithms
        node_id: int
            Numeric identifier of the node in the server
        run_id: int
            Algorithm run identifier
        task_info: dict
            Dictionary with info about the task
        tasks_dir: Path
            Directory in which this task's data are stored
        isolated_network_mgr: NetworkManager
            Manager of isolated network to which algorithm needs to connect
        databases: dict
            List of databases
        docker_volume_name: str
            Name of the docker volume
        alpine_image: str | None
            Name of alternative Alpine image to be used
        device_requests: list | None
            List of DeviceRequest objects to be passed to the algorithm
            container
        requires_pull: bool
            If true, and the Docker image cannot be pulled, don't start the algorithm
            event if a local image is available
        share_algorithm_logs: bool
            If true, share algorithm logs with the server
        write_run_context_file: bool
            If true, write a run context file and expose RUN_CONTEXT_FILE
        """
        self.task_id = task_info["id"]
        self.log = logging.getLogger(f"task ({self.task_id})")

        super().__init__(isolated_network_mgr, docker_client=docker_client)
        self.image = image
        self._vpn_manager = vpn_manager
        self.run_id = run_id
        self.task_id = task_info["id"]
        self.parent_id = get_parent_id(task_info)
        self._tasks_dir = tasks_dir
        self.databases = databases
        self.data_volume_name = docker_volume_name
        self.node_name = node_name
        self.node_id = node_id
        self.alpine_image = ALPINE_IMAGE if alpine_image is None else alpine_image
        self.proxy = proxy
        self.requires_pull = requires_pull
        self.share_algorithm_logs = share_algorithm_logs
        self.write_run_context_file = write_run_context_file
        self.container = None
        self.helper_container = None
        self.status_code = None
        self.docker_input = None

        self.labels = {
            f"{APPNAME}-type": "algorithm",
            "node": node_name,
            "run_id": str(run_id),
        }
        self.helper_labels = self.labels.copy()
        self.helper_labels[f"{APPNAME}-type"] = "algorithm-helper"

        # FIXME: these values should be retrieved from DockerNodeContext
        #   in some way.
        # these paths are for algorithm container filesystem namespace
        self.tmp_folder = "/mnt/tmp"
        self.data_folder = "/mnt/data"

        # keep track of the task status
        self.status: TaskStatus = TaskStatus.INITIALIZING

        # set device requests
        self.device_requests = []
        if device_requests:
            self.device_requests = device_requests

        self.socket = socketIO
        self.collaboration_id = collaboration_id

    def is_finished(self) -> bool:
        """
        Checks if algorithm container is finished

        Returns
        -------
        bool:
            True if algorithm container is finished
        """
        try:
            self.container.reload()
        except (docker.errors.NotFound, AttributeError):
            self.log.error("Container not found")
            self.log.error("- task id: %s", self.task_id)
            self.log.error("- result id: %s", self.task_id)
            self.status = TaskStatus.UNKNOWN_ERROR
            raise AlgorithmContainerNotFound

        return self.container.status == "exited"

    def report_status(self) -> str:
        """
        Checks if algorithm has exited successfully. If not, it prints an
        error message

        Returns
        -------
        logs: str
            Log messages of the algorithm container
        """
        logs = self.container.logs().decode("utf8")

        # report if the container has a different status than 0
        self.status_code = self.container.attrs["State"]["ExitCode"]
        if self.status_code:
            self.log.error(f"Received non-zero exitcode: {self.status_code}")
            self.log.error(f"  Container id: {self.container.id}")
            self.status = TaskStatus.CRASHED
        else:
            self.status = TaskStatus.COMPLETED
        return logs

    def get_results(self) -> bytes:
        """
        Read results output file of the algorithm container

        Returns
        -------
        bytes:
            Results of the algorithm container
        """
        with open(self.output_file, "rb") as fp:
            results = fp.read()
        return results

    def pull(self, local_exists: bool) -> None:
        """
        Pull the latest docker image.

        Parameters
        ----------
        local_exists: bool
            Whether the image already exists locally

        Raises
        ------
        PermanentAlgorithmStartFail
            If the image could not be pulled and does not exist locally
        """
        try:
            self.log.info("Retrieving latest image: '%s'", self.image)
            self.docker.images.pull(self.image)
        except Exception as exc:
            if isinstance(exc, docker.errors.APIError):
                self.log.warning("Failed to pull image! Image does not exist")
            else:
                self.log.warning("Failed to pull image!")
            if not local_exists:
                self.log.exception(exc)
                self.status = TaskStatus.NO_DOCKER_IMAGE
                raise PermanentAlgorithmStartFail from exc
            elif self.requires_pull:
                self.log.warning(
                    "Node policy prevents local image to be used to start algorithm"
                )
                self.status = TaskStatus.NO_DOCKER_IMAGE
                raise PermanentAlgorithmStartFail from exc
            else:
                self.log.info("Using local image")

    def run(
        self,
        docker_input: bytes | str,
        tmp_vol_name: str,
        token: str,
        algorithm_env: dict,
        databases_to_use: list[dict],
    ) -> list[dict] | None:
        """
        Runs the docker-image in detached mode.

        It will will attach all mounts (input, output and datafile) to the
        docker image. And will supply some environment variables.

        Parameters
        ----------
        docker_input: bytes | str
            Input that can be read by docker container
        tmp_vol_name: str
            Name of temporary docker volume assigned to the algorithm
        token: str
            Bearer token that the container can use
        algorithm_env: dict
            Dictionary with additional environment variables to set
        databases_to_use: list[dict]
            List of database selection objects to use in the task

        Returns
        -------
        list[dict] | None
            Description of each port on the VPN client that forwards traffic to
            the algo container. None if VPN is not set up.
        """
        # generate task folders
        self._make_task_folders()

        # prepare volumes
        # NOTE: `docker_input` is used consistently in this class for now.
        # `run_input` may be a clearer name in a future focused refactor.
        # The run-context helper already uses `run_input` terminology.
        self._write_io_files(docker_input, token)
        self.volumes = self._prepare_volumes(tmp_vol_name)
        self.log.debug("volumes: %s", self.volumes)

        self.environment_variables = self._prepare_environment(
            algorithm_env=algorithm_env, databases_to_use=databases_to_use
        )

        # run the algorithm as docker container
        vpn_ports = self._run_algorithm()
        return vpn_ports

    def cleanup(self) -> None:
        """Cleanup the containers generated for this task"""
        if self.helper_container:
            remove_container(self.helper_container, kill=True)
        if self.container:
            remove_container(self.container, kill=True)

    def _run_algorithm(self) -> list[dict] | None:
        """
        Run the algorithm container

        Start up a helper container to complete VPN setup, pull the latest
        image and then run the algorithm

        Returns
        -------
        list[dict] or None
            Description of each port on the VPN client that forwards traffic to
            the algo container. None if VPN is inactive
        """
        vpn_ports = None
        container_name = f"{APPNAME}-{self.node_name}-run-{self.run_id}"
        helper_container_name = container_name + "-helper"

        # Try to pull the latest image
        local_exists = len(self.docker.images.list(name=self.image)) > 0
        self.pull(local_exists=local_exists)

        # remove algorithm containers if they were already running
        self.log.debug("Check if algorithm container is already running")
        remove_container_if_exists(docker_client=self.docker, name=container_name)
        remove_container_if_exists(
            docker_client=self.docker, name=helper_container_name
        )

        container_network = self.isolated_network_mgr.network_name
        if self._vpn_manager and self._vpn_manager.has_vpn:
            # if VPN is active, network exceptions must be configured
            # First, start a container that runs indefinitely. The algorithm
            # container will run in the same network and network exceptions
            # will therefore also affect the algorithm.
            self.log.debug("Start helper container to setup VPN network")
            self.helper_container = self.docker.containers.run(
                command="sleep infinity",
                image=self.alpine_image,
                labels=self.helper_labels,
                network=self.isolated_network_mgr.network_name,
                name=helper_container_name,
                detach=True,
            )
            # setup forwarding of traffic via VPN client to and from the
            # algorithm container:
            self.log.debug("Setup port forwarder")
            vpn_ports = self._vpn_manager.forward_vpn_traffic(
                helper_container=self.helper_container, algo_image_name=self.image
            )
            container_network = "container:" + self.helper_container.id

        # try reading docker input
        # FIXME BvB 2023-02-03: why do we read docker input here? It is never
        # really used below. Should it?
        deserialized_input = None
        if self.docker_input:
            self.log.debug("Deserialize input")
            try:
                deserialized_input = json.loads(self.docker_input)
            except Exception:
                pass

        # attempt to run the image
        try:
            if deserialized_input:
                self.log.info(
                    f"Run docker image {self.image} with input "
                    f"{self._printable_input(deserialized_input)}"
                )
            else:
                self.log.info(f"Run docker image {self.image}")
            self.container = self.docker.containers.run(
                self.image,
                detach=True,
                environment=self.environment_variables,
                network=container_network,
                volumes=self.volumes,
                name=container_name,
                labels=self.labels,
                device_requests=self.device_requests,
            )
            self._stream_logs(
                share_algorithm_logs=self.share_algorithm_logs,
                socketIO=self.socket,
                run_id=self.run_id,
                task_id=self.task_id,
                collaboration_id=self.collaboration_id,
            )

        except Exception as e:
            self.status = TaskStatus.START_FAILED
            raise UnknownAlgorithmStartFail(e)

        self.status = TaskStatus.ACTIVE
        return vpn_ports

    def _prepare_volumes(self, tmp_vol_name: str) -> dict:
        """
        Generate docker volumes required to run the algorithm

        Parameters
        ----------
        tmp_vol_name: str
            Name of temporary docker volume assigned to the algorithm

        Returns
        -------
        dict:
            Volumes to support running the algorithm
        """
        volumes = {
            tmp_vol_name: {"bind": self.tmp_folder, "mode": "rw"},
        }

        for db_label, db in self.databases.items():
            if db.get("mount_mode") == "ro":
                # both ro dirs and ro files are mounted here
                self.log.debug(f"Adding read-only database mount '{db_label}'")
                volumes[db["host_uri"]] = {"bind": db["mount_target"], "mode": "ro"}
            elif db["is_dir"]:
                self.log.debug(f"Adding folder as database '{db_label}'")
                volumes[db["uri"]] = {"bind": f"/mnt/{db_label}", "mode": "rw"}

        if running_in_docker():
            volumes[self.data_volume_name] = {"bind": self.data_folder, "mode": "rw"}
        else:
            volumes[self._tasks_dir] = {"bind": self.data_folder, "mode": "rw"}

        return volumes

    def _extra_network_env(self, proxy_host: str) -> dict:
        """
        Squid proxy environment variables

        Parameters
        ----------
        proxy_host: str
            Host of the node proxy server

        Returns
        -------
        dict
            Proxy environment variables, empty if no squid proxy is used
        """
        environment_variables = {}
        if self.proxy:
            # applications/libraries in the algorithm container need to adhere
            # to the proxy settings. Because we are not sure which application
            # is used for the request we both set HTTP_PROXY and http_proxy and
            # HTTPS_PROXY and https_proxy for the secure connection.
            environment_variables["HTTP_PROXY"] = self.proxy.address
            environment_variables["http_proxy"] = self.proxy.address
            environment_variables["HTTPS_PROXY"] = self.proxy.address
            environment_variables["https_proxy"] = self.proxy.address

            no_proxy = []
            if self._vpn_manager and self._vpn_manager.subnet:
                # Computing all ips in the vpn network is not feasible as the
                # no_proxy environment variable will be too long for the
                # container to start. So we only add the net + mask. For some
                # applications and libraries this is format is ignored.
                no_proxy.append(self._vpn_manager.subnet)
            no_proxy.append("localhost")
            no_proxy.append(proxy_host)

            # Add the NO_PROXY and no_proxy environment variable.
            environment_variables["NO_PROXY"] = ", ".join(no_proxy)
            environment_variables["no_proxy"] = ", ".join(no_proxy)

        return environment_variables

    def _stream_logs(
        self, share_algorithm_logs, socketIO, run_id, task_id, collaboration_id
    ):
        """
        Stream logs from the running container to node logs and optionally to
        the server
        """
        if not self.container:
            self.log.error("No container to stream logs from.")
            return

        def log_stream():
            for log in self.container.logs(stream=True):
                decoded_log = log.decode("utf-8")
                # print algorithm logs to node logs
                self.log.info("[Task %s]: %s", run_id, decoded_log.rstrip())
                # send logs to server
                if share_algorithm_logs:
                    socketIO.emit(
                        "algorithm_log",
                        data={
                            "collaboration_id": collaboration_id,
                            "run_id": run_id,
                            "task_id": task_id,
                            "log": decoded_log,
                        },
                        namespace="/tasks",
                    )

        log_thread = threading.Thread(target=log_stream)
        log_thread.start()
