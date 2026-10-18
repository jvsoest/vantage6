"""
Kubernetes runtime of the node: runs algorithms as Kubernetes Jobs.
"""

import logging
import os
from pathlib import Path
from socket import SocketIO

from vantage6.common import logger_name
from vantage6.common.client.node_client import NodeClient
from vantage6.common.docker.addons import get_registry_digest, parse_image_name
from vantage6.common.globals import NodePolicy
from vantage6.common.task_status import has_task_finished
from vantage6.node.globals import DEFAULT_REQUIRE_ALGO_IMAGE_PULL
from vantage6.node.k8s import naming
from vantage6.node.k8s.client import KubernetesApi
from vantage6.node.k8s.config import KubernetesConfig, validate_node_config
from vantage6.node.k8s.databases import setup_databases
from vantage6.node.k8s.task import KubernetesTaskManager
from vantage6.node.runtime.base import TaskRuntime
from vantage6.node.runtime.policy import ImagePolicyMixin

# Folder in the node pod at which the claims of read-only databases are mounted
DATABASES_MOUNT_ROOT = Path(os.environ.get("V6_DATABASES_MOUNT_ROOT", "/mnt/databases"))

# Folder on the task volume in which the temporary folders of jobs are created
TMP_FOLDER = "tmp"


class KubernetesManager(ImagePolicyMixin, TaskRuntime):
    """
    Runs algorithms as Kubernetes Jobs in the namespace of the node.

    Parameters
    ----------
    ctx: KubernetesNodeContext
        Context of the node
    client: NodeClient
        Client to communicate with the server
    api: KubernetesApi | None
        Access to the Kubernetes API. Created from the in-cluster configuration if
        not provided.
    """

    log = logging.getLogger(logger_name(__name__))

    # seconds between checks whether runs have finished
    poll_interval = 2

    def __init__(self, ctx, client: NodeClient, api: KubernetesApi | None = None):
        self.log.debug("Initializing KubernetesManager")
        config = ctx.config
        validate_node_config(config)
        self.k8s_config = KubernetesConfig.from_node_config(config)
        self.ctx = ctx
        self.client = client
        self.node_name = ctx.name
        self.tasks_dir = Path(self.k8s_config.task_volume_mount_path)

        self.api = api or KubernetesApi(self.k8s_config.namespace)
        self.api.check_access(self.k8s_config.task_pvc)

        # jobs of this node are labeled with an identifier that is unique per
        # server and node, as multiple nodes may share a namespace
        self.node = naming.node_identity(client.base_path, client.whoami.id_)
        self.log.info(
            "Running algorithms as jobs in namespace '%s' (node label %s=%s)",
            self.k8s_config.namespace,
            naming.LABEL_NODE,
            self.node,
        )

        self.algorithm_env = config.get("algorithm_env", {})
        self.write_run_context_file = config.get("run_context_file", False)
        self.gpu = bool(config.get("algorithm_device_requests", {}).get("gpu", False))
        self._policies = self._setup_policies(config)
        self.registries = config.get("docker_registries", []) or []
        self.share_algorithm_logs = config.get("share_algorithm_logs", True)
        if self.share_algorithm_logs:
            self.log.warning(
                "Algorithm logs and errors will be shared with the server."
            )

        self.databases = setup_databases(
            ctx.databases, self.tasks_dir, DATABASES_MOUNT_ROOT
        )

        self.active_tasks: list[KubernetesTaskManager] = []
        self.failed_tasks: list[KubernetesTaskManager] = []

        # most recently observed jobs and pods of this node, by run id
        self._jobs: dict[int, dict] = {}
        self._pods: dict[int, list[dict]] = {}

    def prepare_job_storage(self, job_id: int) -> str:
        """
        Create the temporary folder that is shared by all runs of a job on the
        task volume.

        Parameters
        ----------
        job_id: int
            ID of the job

        Returns
        -------
        str
            Location of the folder relative to the root of the task volume
        """
        folder = f"{TMP_FOLDER}/job-{job_id}"
        path = self.tasks_dir / folder
        os.makedirs(path, exist_ok=True)
        os.chmod(path, 0o770)
        return folder

    def is_running(self, run_id: int) -> bool:
        """
        Check if a Job exists for a run. A finished Job also counts, as its results
        may not have been reported yet.

        Parameters
        ----------
        run_id: int
            ID of the run

        Returns
        -------
        bool
            Whether the algorithm is (or was) running
        """
        if any(task.run_id == run_id for task in self.active_tasks):
            return True
        return bool(self.api.list_jobs(naming.run_selector(self.node, run_id)))

    def cleanup(self) -> None:
        """
        Called when the node shuts down. Running jobs are kept by default, so that
        they can be picked up when the node restarts.
        """
        if self.k8s_config.job.keep_on_shutdown:
            for task in self.active_tasks:
                task.stop_log_stream()
            if self.active_tasks:
                self.log.info(
                    "Leaving %s algorithm job(s) running, they will be picked up "
                    "when the node restarts.",
                    len(self.active_tasks),
                )
        else:
            # note: the killed runs are not registered as killed, so that they
            # are started again when the node restarts
            self.cleanup_tasks()

    def _resolve_digest(self, image: str) -> str | None:
        """
        Get the digest of an image from its registry.

        Parameters
        ----------
        image: str
            Name of the image

        Returns
        -------
        str | None
            Digest of the image, or None if it could not be obtained
        """
        username, password = self._registry_credentials(image)
        return get_registry_digest(image, username, password)

    def _registry_credentials(self, image: str) -> tuple[str | None, str | None]:
        """Credentials for the registry of an image from the node configuration"""
        try:
            registry, _, _ = parse_image_name(image)
        except Exception:
            return None, None
        for entry in self.registries:
            if entry.get("registry") in (registry, f"https://{registry}"):
                return entry.get("username"), entry.get("password")
        return None, None

    def _pinned_image(self, image: str) -> str:
        """
        Reference to the image pinned to its current digest, so that the image
        that was checked against the node policies is the image that runs.
        """
        if "@sha256:" in image:
            return image
        digest = self._resolve_digest(image)
        if not digest:
            self.log.warning(
                "Could not determine the digest of image %s, running it by tag", image
            )
            return image
        return f"{image}@{digest}"

    def _task_kwargs(self, socketIO: SocketIO | None) -> dict:
        """Arguments for task managers that are the same for all runs"""
        return {
            "api": self.api,
            "config": self.k8s_config,
            "state_provider": self._observed_state,
            "node": self.node,
            "node_name": self.node_name,
            "node_id": self.client.whoami.id_,
            "tasks_dir": self.tasks_dir,
            "databases": self.databases,
            "socketIO": socketIO,
            "collaboration_id": self.client.collaboration_id,
            "requires_pull": self._policies.get(
                NodePolicy.REQUIRE_ALGORITHM_PULL, DEFAULT_REQUIRE_ALGO_IMAGE_PULL
            ),
            "gpu": self.gpu,
            "share_algorithm_logs": self.share_algorithm_logs,
            "write_run_context_file": self.write_run_context_file,
        }

    def _create_task(
        self, run_id: int, task_info: dict, image: str, socketIO: SocketIO
    ) -> KubernetesTaskManager:
        """
        Create the task manager that runs the algorithm as Kubernetes Job

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
        KubernetesTaskManager
            Task manager for the run
        """
        return KubernetesTaskManager(
            image=image,
            run_image=self._pinned_image(image),
            run_id=run_id,
            task_info=task_info,
            **self._task_kwargs(socketIO),
        )

    def _refresh_state(self) -> None:
        """Observe the jobs and pods of all runs of this node"""
        selector = naming.node_selector(self.node)
        jobs = self.api.list_jobs(selector)
        pods = self.api.list_pods(selector)
        self._jobs = {
            int(job["metadata"]["labels"][naming.LABEL_RUN_ID]): job for job in jobs
        }
        self._pods = {}
        for pod in pods:
            run_id = int(pod["metadata"]["labels"][naming.LABEL_RUN_ID])
            self._pods.setdefault(run_id, []).append(pod)

    def _observed_state(self, run_id: int) -> tuple[dict | None, list[dict]]:
        """Most recently observed Job and pods of a run"""
        return self._jobs.get(run_id), self._pods.get(run_id, [])

    def reconcile(self) -> None:
        """
        Pick up the jobs that were started by a previous instance of this node.

        Jobs of runs that are still open at the server are tracked again, so that
        their results are reported when they finish. Jobs of runs that the server
        considers finished (e.g. killed while the node was offline) are deleted.
        """
        self._refresh_state()
        for run_id, job in self._jobs.items():
            name = job["metadata"]["name"]
            try:
                run = self.client.request(f"run/{run_id}")
                run_status = run.get("status")
            except Exception:
                self.log.exception("Could not get run %s from the server", run_id)
                continue
            if not run_status or has_task_finished(run_status):
                self.log.info(
                    "Deleting job %s: its run has status '%s' at the server",
                    name,
                    run_status,
                )
                self.api.delete_job(name)
                continue
            self.log.info("Picking up job %s of run %s", name, run_id)
            task = KubernetesTaskManager.from_job(job, **self._task_kwargs(None))
            self.active_tasks.append(task)

    def attach_socket(self, socketIO: SocketIO) -> None:
        """
        Resume streaming the logs of runs that were picked up after a restart.

        Parameters
        ----------
        socketIO: SocketIO
            Socket connection to the server
        """
        for task in self.active_tasks:
            if task.socket is None:
                task.socket = socketIO
                # only new log lines; earlier lines have been streamed before
                task.start_log_stream(since_seconds=1)
