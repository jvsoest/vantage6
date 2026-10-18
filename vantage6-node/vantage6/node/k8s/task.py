"""
Management of a single algorithm run on Kubernetes.
"""

import logging
import os
import threading
from pathlib import Path
from socket import SocketIO
from typing import Callable

from kubernetes.client.exceptions import ApiException

from vantage6.common.task_status import TaskStatus
from vantage6.node.docker.exceptions import (
    AlgorithmContainerNotFound,
    PermanentAlgorithmStartFail,
    UnknownAlgorithmStartFail,
)
from vantage6.node.k8s import naming, status
from vantage6.node.k8s.client import KubernetesApi
from vantage6.node.k8s.config import KubernetesConfig
from vantage6.node.k8s.databases import algorithm_volumes
from vantage6.node.k8s.job_spec import AlgorithmJob, build_job
from vantage6.node.runtime.run_io import RunIOMixin
from vantage6.node.util import get_parent_id

# Function that returns the most recently observed Job and pods of a run
StateProvider = Callable[[int], tuple[dict | None, list[dict]]]

# Seconds between attempts to find the running pod whose logs should be streamed
LOG_STREAM_POLL_INTERVAL = 2


class KubernetesTaskManager(RunIOMixin):
    """
    Runs an algorithm as a Kubernetes Job and tracks its progress.

    The input, output and token files are written to the task volume, which is
    mounted both in the node pod and (partially) in the algorithm pod.

    Parameters
    ----------
    api: KubernetesApi
        Access to the Kubernetes API
    config: KubernetesConfig
        Settings of the Kubernetes runtime
    state_provider: StateProvider
        Provides the latest observed Job and pods of the run
    image: str
        Docker image of the algorithm, as requested by the user
    run_image: str
        Docker image reference to run, e.g. pinned to a digest
    node: str
        Node identifier, used to label the jobs of this node
    node_name: str
        Name of the node
    node_id: int
        ID of the node at the server
    run_id: int
        ID of the run
    task_info: dict
        Information on the task
    tasks_dir: Path
        Root of the task volume in the node pod
    databases: dict
        Databases available to the algorithm
    socketIO: SocketIO | None
        Socket connection to the server, used to share algorithm logs
    collaboration_id: int
        ID of the collaboration
    requires_pull: bool
        Whether the image should always be pulled
    gpu: bool
        Whether the algorithm should get access to a GPU
    share_algorithm_logs: bool
        Whether to share algorithm logs with the server
    write_run_context_file: bool
        Whether to write a run context file
    """

    # The run folder (with the token) is only shared between the node and the
    # algorithm pod of the run. Both may run with different users in the same
    # group (e.g. on OpenShift or when fsGroup is set).
    task_folder_mode = 0o770
    task_file_mode = 0o660

    def __init__(
        self,
        api: KubernetesApi,
        config: KubernetesConfig,
        state_provider: StateProvider,
        image: str,
        run_image: str,
        node: str,
        node_name: str,
        node_id: int,
        run_id: int,
        task_info: dict,
        tasks_dir: Path,
        databases: dict,
        socketIO: SocketIO | None,
        collaboration_id: int,
        requires_pull: bool = True,
        gpu: bool = False,
        share_algorithm_logs: bool = False,
        write_run_context_file: bool = False,
    ) -> None:
        self.task_id = task_info["id"]
        self.log = logging.getLogger(f"task ({self.task_id})")
        self.api = api
        self.config = config
        self.state_provider = state_provider
        self.image = image
        self.run_image = run_image
        self.node = node
        self.node_name = node_name
        self.node_id = node_id
        self.run_id = run_id
        self.job_id = task_info.get("job_id")
        self.parent_id = get_parent_id(task_info)
        self._tasks_dir = tasks_dir
        self.databases = databases
        self.socket = socketIO
        self.collaboration_id = collaboration_id
        self.requires_pull = requires_pull
        self.gpu = gpu
        self.share_algorithm_logs = share_algorithm_logs
        self.write_run_context_file = write_run_context_file

        self.job_name = naming.job_name(node, run_id)
        self.labels = naming.run_labels(
            node, run_id, self.task_id, self.job_id, self.parent_id
        )

        # paths in the algorithm container, the same as for docker nodes
        self.tmp_folder = "/mnt/tmp"
        self.data_folder = "/mnt/data"

        self.status: TaskStatus = TaskStatus.INITIALIZING
        self.docker_input = None
        self.status_message: str | None = None
        self.failure_logs: str | None = None
        self.task_folder_name = f"task-{self.run_id:09d}"
        self.output_file = os.path.join(
            self._tasks_dir, self.task_folder_name, "output"
        )

        self._log_thread: threading.Thread | None = None
        self._stop_logs = threading.Event()

    @classmethod
    def from_job(cls, job: dict, **kwargs) -> "KubernetesTaskManager":
        """
        Create a task manager for a job that was started by a previous instance
        of the node.

        Parameters
        ----------
        job: dict
            The job of the run
        **kwargs
            Other arguments of the constructor, except those that are derived
            from the job

        Returns
        -------
        KubernetesTaskManager
            Task manager of the active run
        """
        labels = job["metadata"]["labels"]
        annotations = job["metadata"].get("annotations") or {}
        parent_id = labels.get(naming.LABEL_PARENT_ID)
        task_info = {
            "id": int(labels[naming.LABEL_TASK_ID]),
            "job_id": int(labels[naming.LABEL_JOB_ID]),
            "parent": {"id": int(parent_id)} if parent_id else None,
        }
        image = annotations.get(naming.ANNOTATION_IMAGE, "")
        task = cls(
            run_id=int(labels[naming.LABEL_RUN_ID]),
            task_info=task_info,
            image=image,
            run_image=image,
            **kwargs,
        )
        task.status = TaskStatus.ACTIVE
        return task

    def run(
        self,
        docker_input: bytes | str,
        tmp_vol_name: str,
        token: str,
        algorithm_env: dict,
        databases_to_use: list[dict],
    ) -> None:
        """
        Write the files of the run to the task volume and create its Job.

        Parameters
        ----------
        docker_input: bytes | str
            Input that can be read by the algorithm
        tmp_vol_name: str
            Location of the temporary folder of the job, relative to the root of
            the task volume
        token: str
            Bearer token that the algorithm can use
        algorithm_env: dict
            Dictionary with additional environment variables to set
        databases_to_use: list[dict]
            List of database selection objects to use in the task

        Returns
        -------
        None
            VPN is not supported, so there are never VPN ports to report
        """
        self._make_task_folders()
        self._write_io_files(docker_input, token)
        environment = self._prepare_environment(
            algorithm_env=algorithm_env, databases_to_use=databases_to_use
        )

        volumes, volume_mounts = algorithm_volumes(self.databases, self.data_folder)
        manifest = build_job(
            AlgorithmJob(
                name=self.job_name,
                image=self.run_image,
                labels=self.labels,
                annotations={
                    naming.ANNOTATION_NODE_NAME: self.node_name,
                    naming.ANNOTATION_IMAGE: self.image,
                    naming.ANNOTATION_COLLABORATION_ID: str(self.collaboration_id),
                },
                environment=environment,
                run_folder=self.task_folder_name,
                run_folder_mount_path=f"{self.data_folder}/{self.task_folder_name}",
                tmp_folder=tmp_vol_name,
                tmp_folder_mount_path=self.tmp_folder,
                requires_pull=self.requires_pull,
                gpu=self.gpu,
                volumes=volumes,
                volume_mounts=volume_mounts,
            ),
            self.config,
        )

        self.log.info(
            "Creating Kubernetes job %s for image %s", self.job_name, self.image
        )
        try:
            self.api.create_job(manifest)
        except ApiException as exc:
            if exc.status == 409:
                # The job already exists, e.g. because the node was restarted
                self.log.info("Job %s already exists", self.job_name)
            elif exc.status and 400 <= exc.status < 500:
                self.status = TaskStatus.START_FAILED
                self.failure_logs = (
                    f"Kubernetes refused to create the algorithm job: {exc.reason}. "
                    "Check the node logs for details."
                )
                self.log.error("Kubernetes refused job %s: %s", self.job_name, exc)
                raise PermanentAlgorithmStartFail(exc) from exc
            else:
                self.status = TaskStatus.START_FAILED
                raise UnknownAlgorithmStartFail(exc) from exc
        except Exception as exc:
            self.status = TaskStatus.START_FAILED
            raise UnknownAlgorithmStartFail(exc) from exc

        self.status = TaskStatus.ACTIVE
        self.start_log_stream()
        return None

    def is_finished(self) -> bool:
        """
        Check whether the algorithm has finished, based on the most recently
        observed state of its Job.

        Returns
        -------
        bool
            True if the run has finished (successfully or not)

        Raises
        ------
        AlgorithmContainerNotFound
            If the Job of the run no longer exists
        """
        job, pods = self.state_provider(self.run_id)
        if job is None and self._job_exists():
            # the job was created after the state was last observed
            return False
        if job is None:
            self.log.error("Job %s not found", self.job_name)
            self.status = TaskStatus.UNKNOWN_ERROR
            self.failure_logs = (
                "The Kubernetes job of the algorithm disappeared. Check the node "
                "logs for details."
            )
            raise AlgorithmContainerNotFound
        state = status.evaluate(job, pods, self.config.job)
        if state.finished:
            self._final_status = state.status
            self.status_message = state.message
            self._final_pods = pods
        return state.finished

    def _job_exists(self) -> bool:
        """Check with the Kubernetes API whether the job of the run exists"""
        try:
            return bool(self.api.list_jobs(naming.run_selector(self.node, self.run_id)))
        except Exception:
            self.log.exception("Could not check whether job %s exists", self.job_name)
            # assume it exists, we'll check again later
            return True

    def report_status(self) -> str:
        """
        Set the final status of the run and collect its logs.

        Returns
        -------
        str
            Logs of the algorithm, followed by an explanation of the node if the
            algorithm did not complete successfully
        """
        self.status = self._final_status
        logs = self._read_logs()
        if self.status != TaskStatus.COMPLETED:
            self.log.error(
                "Run %s did not complete successfully: %s",
                self.run_id,
                self.status_message or self.status,
            )
            if self.status_message:
                logs = f"{logs}\n[vantage6 node] {self.status_message}".lstrip()
        return logs

    def get_results(self) -> bytes:
        """
        Read the output file of the algorithm

        Returns
        -------
        bytes
            Results of the algorithm
        """
        try:
            with open(self.output_file, "rb") as fp:
                return fp.read()
        except FileNotFoundError:
            self.log.warning("Output file of run %s not found", self.run_id)
            return b""

    def cleanup(self) -> None:
        """Delete the Job of the run, which also deletes its pod"""
        self._stop_logs.set()
        try:
            self.api.delete_job(self.job_name)
        except Exception:
            self.log.exception("Could not delete job %s", self.job_name)

    def stop_log_stream(self) -> None:
        """Stop streaming the logs of the algorithm, without stopping it"""
        self._stop_logs.set()

    def start_log_stream(self, since_seconds: int | None = None) -> None:
        """
        Stream the logs of the algorithm to the node logs and, if allowed, to the
        server, once the algorithm container has started.

        Parameters
        ----------
        since_seconds: int | None
            Only stream logs of the last number of seconds. Used to prevent
            sending logs twice when the node picks up a run after a restart.
        """
        if self._log_thread and self._log_thread.is_alive():
            return
        self._stop_logs.clear()
        self._log_thread = threading.Thread(
            target=self._stream_logs, args=(since_seconds,), daemon=True
        )
        self._log_thread.start()

    def _find_started_pod(self) -> dict | None:
        """Wait until the algorithm container has started and return its pod"""
        while not self._stop_logs.is_set():
            try:
                pods = self.api.list_pods(naming.run_selector(self.node, self.run_id))
            except Exception:
                self.log.debug("Could not list pods of run %s", self.run_id)
                pods = []
            started = [pod for pod in pods if status.is_running(pod)]
            if started:
                return started[-1]
            self._stop_logs.wait(LOG_STREAM_POLL_INTERVAL)
        return None

    def _stream_logs(self, since_seconds: int | None = None) -> None:
        """Follow the logs of the algorithm container"""
        pod = self._find_started_pod()
        if not pod:
            return
        try:
            for line in self.api.stream_pod_log(
                pod["metadata"]["name"], naming.ALGORITHM_CONTAINER, since_seconds
            ):
                self.log.info("[Task %s]: %s", self.run_id, line.rstrip())
                if self.share_algorithm_logs and self.socket:
                    self.socket.emit(
                        "algorithm_log",
                        data={
                            "collaboration_id": self.collaboration_id,
                            "run_id": self.run_id,
                            "task_id": self.task_id,
                            "log": line,
                        },
                        namespace="/tasks",
                    )
                if self._stop_logs.is_set():
                    break
        except Exception:
            self.log.debug("Log stream of run %s ended", self.run_id, exc_info=True)

    def _read_logs(self) -> str:
        """Read the complete logs of the algorithm container"""
        pods = getattr(self, "_final_pods", None) or []
        if not pods:
            return ""
        pod = max(
            pods, key=lambda p: p.get("metadata", {}).get("creationTimestamp", "")
        )
        try:
            return self.api.read_pod_log(
                pod["metadata"]["name"], naming.ALGORITHM_CONTAINER
            )
        except ApiException as exc:
            # e.g. the container never started
            self.log.debug("Could not read logs of run %s: %s", self.run_id, exc)
            return ""
        except Exception:
            self.log.exception("Could not read logs of run %s", self.run_id)
            return ""
