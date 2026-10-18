"""
Docker manager

The docker manager is responsible for communicating with the docker-daemon and
is a wrapper around the docker module. It has methods
for creating docker networks, docker volumes, start containers and retrieve
results from finished containers.
"""

import logging
import os
import shutil
from pathlib import Path
from socket import SocketIO

import docker
from vantage6.cli.context.node import NodeContext
from vantage6.common import get_database_config, logger_name
from vantage6.common.client.node_client import NodeClient
from vantage6.common.docker.addons import (
    get_container,
    get_digest,
    running_in_docker,
)
from vantage6.common.docker.network_manager import NetworkManager
from vantage6.common.globals import (
    APPNAME,
    DEFAULT_DB_MOUNT_MODE,
    NodePolicy,
)
from vantage6.node.context import DockerNodeContext
from vantage6.node.docker.docker_base import DockerBaseManager
from vantage6.node.docker.squid import Squid
from vantage6.node.docker.task_manager import DockerTaskManager
from vantage6.node.docker.utils import login_to_registries
from vantage6.node.docker.vpn_manager import VPNManager
from vantage6.node.globals import DEFAULT_REQUIRE_ALGO_IMAGE_PULL
from vantage6.node.runtime.base import TaskRuntime
from vantage6.node.runtime.policy import ImagePolicyMixin

# Kept importable from this module for backwards compatibility
from vantage6.node.runtime.types import KilledRun, Result, ToBeKilled  # noqa: F401

log = logging.getLogger(logger_name(__name__))

SUPPORTED_DATABASE_MOUNT_MODES = {"copy", "ro"}


class DockerManager(ImagePolicyMixin, TaskRuntime, DockerBaseManager):
    """
    Wrapper for the docker-py module.

    This class manages tasks related to Docker, such as logging in to
    docker registries, managing input/output files, logs etc. Results
    can be retrieved through `get_result()` which returns the first available
    algorithm result.
    """

    log = logging.getLogger(logger_name(__name__))

    def __init__(
        self,
        ctx: DockerNodeContext | NodeContext,
        isolated_network_mgr: NetworkManager,
        vpn_manager: VPNManager | None,
        tasks_dir: Path,
        client: NodeClient,
        proxy: Squid | None = None,
        docker_client: docker.DockerClient | None = None,
    ) -> None:
        """Initialization of DockerManager creates docker connection and
        sets some default values.

        Parameters
        ----------
        ctx: DockerNodeContext | NodeContext
            Context object from which some settings are obtained
        isolated_network_mgr: NetworkManager
            Manager for the isolated network
        vpn_manager: VPNManager | None
            VPN Manager object, or None if VPN is not configured
        tasks_dir: Path
            Directory in which this task's data are stored
        client: NodeClient
            Client object to communicate with the server
        proxy: Squid | None
            Squid proxy object
        docker_client: docker.DockerClient | None
            Authenticated docker client to reuse
        """
        self.log.debug("Initializing DockerManager")
        super().__init__(isolated_network_mgr, docker_client=docker_client)

        self.data_volume_name = ctx.docker_volume_name
        self.ctx = ctx
        config = ctx.config
        self.algorithm_env = config.get("algorithm_env", {})
        self.write_run_context_file = config.get("run_context_file", False)
        self.vpn_manager = vpn_manager
        self.client = client
        self.__tasks_dir = tasks_dir
        self.alpine_image = config.get("alpine")
        self.proxy = proxy

        # keep track of the running containers
        self.active_tasks: list[DockerTaskManager] = []

        # keep track of the containers that have failed to start
        self.failed_tasks: list[DockerTaskManager] = []

        # before a task is executed it gets exposed to these policies
        self._policies = self._setup_policies(config)

        # node name is used to identify algorithm containers belonging
        # to this node. This is required as multiple nodes may run at
        # a single machine sharing the docker daemon while using a
        # different server. Using a different server means that there
        # could be duplicate result_id's running at the node at the same
        # time.
        self.node_name = ctx.name

        # name of the container that is running the node
        self.node_container_name = ctx.docker_container_name

        # login to the registries
        docker_registries = ctx.config.get("docker_registries", [])
        login_to_registries(docker_registries, docker_client=self.docker)

        # set database uri and whether or not it is a file
        self._set_database(ctx.databases)

        # keep track of linked docker services
        self.linked_services: list[str] = []

        # set algorithm device requests
        self.algorithm_device_requests = []
        if "algorithm_device_requests" in config:
            self._set_algorithm_device_requests(config["algorithm_device_requests"])

        # whether to share or not algorithm logs with the server
        # TODO: config loading could be centralized in a class, then validate,
        # set defaults, warn about dangers, etc
        self.share_algorithm_logs = config.get("share_algorithm_logs", True)
        if self.share_algorithm_logs:
            self.log.warning(
                "Algorithm logs and errors will be shared with the server."
            )

    def _set_database(self, databases: dict | list) -> None:
        """
        Set database location and whether or not it is a file

        Parameters
        ----------
        databases: dict | list
            databases as specified in the config file
        """
        db_labels = [db["label"] for db in databases]

        # If we're running in a docker container, database_uri would point
        # to a path on the *host* (since it's been read from the config
        # file). That's no good here. Therefore, we expect the CLI to set
        # the environment variables for us. This has the added bonus that we
        # can override the URI from the command line as well.
        self.databases = {}
        for label in db_labels:
            label_upper = label.upper()
            db_config = get_database_config(databases, label)
            # we default to 'copy' mode to preserve current/previous behavior
            mount_mode = str(db_config.get("mount_mode", DEFAULT_DB_MOUNT_MODE)).lower()

            # we check again container-side (aside from host-running cli)
            if mount_mode not in SUPPORTED_DATABASE_MOUNT_MODES:
                raise ValueError(
                    f"Invalid mount_mode '{mount_mode}' for database '{label}'. "
                    "Supported values are 'copy' and 'ro'."
                )

            if running_in_docker():
                # `v6 node start` sets:
                #    env[f"{label_capitals}_DATABASE_URI"] = f"{label}{suffix}"
                #    mounts.append((f"/mnt/{label}{suffix}", str(uri)))
                # So uri will get assigned to something like `default.csv` now
                # It's not a "path" per se yet (not 'universal')
                uri = os.environ[f"{label_upper}_DATABASE_URI"]
            else:
                uri = db_config["uri"]

            if running_in_docker():
                db_is_file = (
                    Path(f"/mnt/{uri}").exists() and Path(f"/mnt/{uri}").is_file()
                )
                db_is_dir = (
                    Path(f"/mnt/{uri}").exists() and Path(f"/mnt/{uri}").is_dir()
                )

                if db_is_file:
                    # because `v6 node start` did
                    # `mounts.append((f"/mnt/{label}{suffix}", str(uri)))` now
                    # uri points to the dataset that has been bind-mounted from
                    # the host. This is still in the node filesystem namespace
                    uri = f"/mnt/{uri}"
            else:
                db_is_file = Path(uri).exists() and Path(uri).is_file()
                db_is_dir = Path(uri).exists() and Path(uri).is_dir()

            if db_is_file:
                if mount_mode == "ro":
                    self.log.info(
                        "Using read-only bind mount for database '%s' from '%s'",
                        label,
                        # keep in mind: db_config["uri"] is uri in host filesystem namespace
                        db_config["uri"],
                    )
                    # we don't just overwrite uri with `db_config["uri"]` like
                    # we do below for dirs because, in the node, we might still
                    # check the dataset (e.g. get_column_names()). We'll end up
                    # passing a host_uri so task manager knows what to tell
                    # host-side-running docker engine what to mount
                else:
                    # We'll copy the file to the folder `data` in our task_dir.
                    self.log.info(f"Copying {uri} to {self.__tasks_dir}")
                    # task_dir at the moment is just the data volume (dockerized)
                    # See: https://github.com/vantage6/vantage6/issues/2533
                    # But this named volume will be shared with the algorithm
                    shutil.copy(uri, self.__tasks_dir)
                    # after the below line, uri will point to path in (still)
                    # node filesystem namespace, the copy we've just made
                    uri = self.__tasks_dir / os.path.basename(uri)

            if db_is_dir:
                self.log.info(
                    "Database folder from '%s' mounted at /mnt/%s",
                    db_config["uri"],
                    uri,
                )
                # Ignore all previous comments about file locations: folders
                # need to be mounted from the host.
                uri = db_config["uri"]

            db_suffix = Path(str(db_config["uri"])).suffix
            mount_target = None
            if db_is_file:
                # used for ro mode. With copy we don't mount the file itself
                mount_target = f"/mnt/{label}{db_suffix}"
            elif db_is_dir:
                # will be used for ro dir mount
                mount_target = f"/mnt/{label}"

            if mount_mode == "ro":
                if not (db_is_file or db_is_dir):
                    raise ValueError(
                        f"Database '{label}' uses mount_mode 'ro' but does not resolve "
                        "to a file/folder database."
                    )
                if not db_config.get("uri") or not mount_target:
                    raise ValueError(
                        f"Database '{label}' uses mount_mode 'ro' but missing host "
                        "uri or mount target."
                    )

            self.databases[label] = {
                "uri": uri,
                "is_file": db_is_file,
                "is_dir": db_is_dir,
                "type": db_config["type"],
                "env": db_config.get("env", {}),
                "mount_mode": mount_mode,
                # host_uri and mount_target are only used in 'ro' mount mode
                "host_uri": db_config["uri"],
                "mount_target": mount_target,
            }
        self.log.debug("Databases: %s", self.databases)

    def _set_algorithm_device_requests(self, device_requests_config: dict) -> None:
        """
        Configure device access for the algorithm container.

        Parameters
        ----------
        device_requests_config: dict
           A dictionary containing configuration options for device access.
           Supported keys:
           - 'gpu': A boolean value indicating whether GPU access is required.
        """
        device_requests = []
        if device_requests_config.get("gpu", False):
            device = docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
            device_requests.append(device)
        self.algorithm_device_requests = device_requests

    def create_volume(self, volume_name: str) -> None:
        """
        Create a temporary volume for a single run.

        A single run can consist of multiple algorithm containers. It is
        important to note that all algorithm containers having the same job_id
        have access to this container.

        Parameters
        ----------
        volume_name: str
            Name of the volume to be created
        """
        try:
            self.docker.volumes.get(volume_name)
            self.log.debug("Volume %s already exists.", volume_name)

        except docker.errors.NotFound:
            self.log.debug("Creating volume %s", volume_name)
            self.docker.volumes.create(volume_name)

    def is_running(self, run_id: int) -> bool:
        """
        Check if a container is already running for <run_id>.

        Parameters
        ----------
        run_id: int
            run_id of the algorithm container to be found

        Returns
        -------
        bool
            Whether or not algorithm container is running already
        """
        running_containers = self.docker.containers.list(
            filters={
                "label": [
                    f"{APPNAME}-type=algorithm",
                    f"node={self.node_name}",
                    f"run_id={run_id}",
                ]
            }
        )
        return bool(running_containers)

    def cleanup(self) -> None:
        """
        Stop all active tasks and delete the isolated network

        Note: the temporary docker volumes are kept as they may still be used
        by a parent container
        """
        # note: the function `cleanup_tasks` returns a list of tasks that were
        # killed, but we don't register them as killed so they will be run
        # again when the node is restarted
        self.cleanup_tasks()

        for service in self.linked_services:
            self.isolated_network_mgr.disconnect(service)

        # remove the node container from the network, it runs this code.. so
        # it does not make sense to delete it just yet
        self.isolated_network_mgr.disconnect(self.node_container_name)

        # remove the connected containers and the network
        self.isolated_network_mgr.delete(kill_containers=True)

    def prepare_job_storage(self, job_id: int) -> str:
        """
        Create the temporary docker volume that is shared by all runs of a job.

        Parameters
        ----------
        job_id: int
            ID of the job

        Returns
        -------
        str
            Name of the docker volume
        """
        volume_name = self.ctx.docker_temporary_volume_name(job_id)
        self.create_volume(volume_name)
        return volume_name

    def _resolve_digest(self, image: str) -> str | None:
        """
        Get the digest of a docker image, using the docker daemon in which we
        may have logged in to registries.

        Parameters
        ----------
        image: str
            Name of the image

        Returns
        -------
        str | None
            Digest of the image, or None if it could not be obtained
        """
        return get_digest(image, client=self.docker)

    def _create_task(
        self, run_id: int, task_info: dict, image: str, socketIO: SocketIO
    ) -> DockerTaskManager:
        """
        Create the task manager that runs the algorithm as docker container

        Parameters
        ----------
        run_id: int
            Server run identifier
        task_info: dict
            Dictionary with task information
        image: str
            Docker image name
        socketIO: SocketIO
            Socket connection to the server

        Returns
        -------
        DockerTaskManager
            Task manager for the run
        """
        # we pass self.docker instance, in which we may have logged in to registries
        return DockerTaskManager(
            image=image,
            docker_client=self.docker,
            run_id=run_id,
            task_info=task_info,
            vpn_manager=self.vpn_manager,
            node_name=self.node_name,
            node_id=self.client.whoami.id_,
            tasks_dir=self.__tasks_dir,
            isolated_network_mgr=self.isolated_network_mgr,
            databases=self.databases,
            docker_volume_name=self.data_volume_name,
            alpine_image=self.alpine_image,
            proxy=self.proxy,
            device_requests=self.algorithm_device_requests,
            requires_pull=self._policies.get(
                NodePolicy.REQUIRE_ALGORITHM_PULL, DEFAULT_REQUIRE_ALGO_IMAGE_PULL
            ),
            socketIO=socketIO,
            collaboration_id=self.client.collaboration_id,
            share_algorithm_logs=self.share_algorithm_logs,
            write_run_context_file=self.write_run_context_file,
        )

    def _on_run_finished(self, task: DockerTaskManager) -> None:
        """
        Remove the VPN ports of a finished run from the server

        Parameters
        ----------
        task: DockerTaskManager
            Task manager of the finished run
        """
        self.client.request("port", params={"run_id": task.run_id}, method="DELETE")

    def link_container_to_network(self, container_name: str, config_alias: str) -> None:
        """
        Link a docker container to the isolated docker network

        Parameters
        ----------
        container_name: str
            Name of the docker container to be linked to the network
        config_alias: str
            Alias of the docker container defined in the config file
        """
        container = get_container(docker_client=self.docker, name=container_name)
        if not container:
            self.log.error(
                f"Could not link docker container {container_name} "
                "that was specified in the configuration file to "
                "the isolated docker network."
            )
            self.log.error("Container not found!")
            return
        self.isolated_network_mgr.connect(
            container_name=container_name, aliases=[config_alias]
        )
        self.linked_services.append(container_name)
