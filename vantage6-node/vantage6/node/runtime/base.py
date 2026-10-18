"""
Interface that a container runtime (e.g. Docker, Kubernetes) has to implement to
run algorithms on behalf of the node.

The node itself only talks to an instance of :class:`TaskRuntime`. The flow of
starting runs, waiting for them to finish and killing them is the same for every
runtime and is therefore implemented here. The runtime-specific parts are delegated
to the task managers that the runtime creates for each run, and to a few abstract
methods.
"""

import logging
import time
from abc import ABC, abstractmethod
from socket import SocketIO

from vantage6.algorithm.tools.wrappers import get_column_names
from vantage6.common import logger_name
from vantage6.common.client.node_client import NodeClient
from vantage6.common.task_status import TaskStatus, has_task_failed
from vantage6.node.docker.exceptions import (
    AlgorithmContainerNotFound,
    PermanentAlgorithmStartFail,
    UnknownAlgorithmStartFail,
)
from vantage6.node.runtime.types import KilledRun, Result, ToBeKilled


class TaskRuntime(ABC):
    """
    Base class for the runtimes that start algorithms on behalf of the node.

    Subclasses should set the following attributes:

    - ``client``: the :class:`NodeClient` to communicate with the server
    - ``active_tasks``: list of task managers of runs that are running
    - ``failed_tasks``: list of task managers of runs that failed to start
    - ``databases``: dictionary with the databases available to algorithms
    - ``algorithm_env``: additional environment variables for algorithms

    The task managers that are created by :meth:`_create_task` should provide the
    attributes ``run_id``, ``task_id``, ``parent_id`` and ``status`` and the
    methods ``run``, ``is_finished``, ``report_status``, ``get_results`` and
    ``cleanup``.
    """

    log = logging.getLogger(logger_name(__name__))

    # seconds to wait between checking whether runs have finished
    poll_interval = 1

    client: NodeClient
    active_tasks: list
    failed_tasks: list
    databases: dict
    algorithm_env: dict

    @abstractmethod
    def prepare_job_storage(self, job_id: int) -> str:
        """
        Prepare the temporary storage that is shared by all runs of a job.

        Parameters
        ----------
        job_id: int
            ID of the job

        Returns
        -------
        str
            Identifier of the storage (e.g. a volume name) to pass to :meth:`run`
        """

    @abstractmethod
    def is_running(self, run_id: int) -> bool:
        """
        Check if an algorithm is already running for a run.

        Parameters
        ----------
        run_id: int
            ID of the run

        Returns
        -------
        bool
            Whether or not the algorithm is running already
        """

    @abstractmethod
    def cleanup(self) -> None:
        """Release the resources of this runtime when the node shuts down"""

    @abstractmethod
    def _create_task(
        self, run_id: int, task_info: dict, image: str, socketIO: SocketIO
    ):
        """
        Create the runtime-specific task manager for a single run.

        Parameters
        ----------
        run_id: int
            Server run identifier
        task_info: dict
            Dictionary with task information
        image: str
            Docker image name
        socketIO: SocketIO
            Socket connection to the server, used to share algorithm logs
        """

    def reconcile(self) -> None:
        """
        Pick up runs that were started by a previous instance of the node.

        Called before the node connects to the server and synchronizes its task
        queue. Runtimes whose algorithms do not outlive the node do not need to do
        anything here.
        """

    def attach_socket(self, socketIO: SocketIO) -> None:
        """
        Pass the socket connection to runs picked up by :meth:`reconcile`

        Parameters
        ----------
        socketIO: SocketIO
            Socket connection to the server
        """

    def num_active_tasks(self) -> int:
        """Number of algorithm runs that are currently running"""
        return len(self.active_tasks)

    def _on_run_finished(self, task) -> None:
        """
        Hook that is called when a run has finished and its results are read.

        Parameters
        ----------
        task
            Task manager of the finished run
        """

    def run(
        self,
        run_id: int,
        task_info: dict,
        image: str,
        docker_input: bytes | str,
        tmp_vol_name: str,
        token: str,
        databases_to_use: list[dict],
        socketIO: SocketIO,
    ) -> tuple[TaskStatus, list[dict] | None]:
        """
        Checks if the algorithm is running. If not, creates a task manager to
        run the algorithm

        Parameters
        ----------
        run_id: int
            Server run identifier
        task_info: dict
            Dictionary with task information
        image: str
            Docker image name
        docker_input: bytes | str
            Input that can be read by the algorithm
        tmp_vol_name: str
            Identifier of the temporary storage assigned to the algorithm, as
            returned by :meth:`prepare_job_storage`
        token: str
            Bearer token that the container can use
        databases_to_use: list[dict]
            Database selection objects to use
        socketIO: SocketIO
            Socket connection to the server

        Returns
        -------
        TaskStatus, list[dict] | None
            Returns a tuple with the status of the task and a description of
            each port on the VPN client that forwards traffic to the algorithm
            container (``None`` if VPN is not set up).
        """
        # Verify that an allowed image is used
        if not self.is_docker_image_allowed(image, task_info):
            msg = f"Docker image {image} is not allowed on this Node!"
            self.log.critical(msg)
            return TaskStatus.NOT_ALLOWED, None

        # Check that this task is not already running
        if self.is_running(run_id):
            self.log.info("Task is already being executed, discarding task")
            self.log.debug("run_id=%s is discarded", run_id)
            return TaskStatus.ACTIVE, None

        task = self._create_task(
            run_id=run_id, task_info=task_info, image=image, socketIO=socketIO
        )

        # attempt to kick of the task. If it fails do to unknown reasons we try
        # again. If it fails permanently we add it to the failed tasks to be
        # handled by the speaking worker of the node
        vpn_ports = None
        attempts = 1
        while not (task.status == TaskStatus.ACTIVE) and attempts < 3:
            try:
                vpn_ports = task.run(
                    docker_input=docker_input,
                    tmp_vol_name=tmp_vol_name,
                    token=token,
                    algorithm_env=self.algorithm_env,
                    databases_to_use=databases_to_use,
                )

            except UnknownAlgorithmStartFail:
                self.log.exception(
                    f"Failed to start run {run_id} for an unknown reason. Retrying..."
                )
                time.sleep(1)  # add some time before retrying the next attempt

            except PermanentAlgorithmStartFail:
                break

            attempts += 1

        # keep track of the active container
        if has_task_failed(task.status):
            self.failed_tasks.append(task)
            return task.status, None
        else:
            self.active_tasks.append(task)
            return task.status, vpn_ports

    def _wait_for_finished_task(self):
        """
        Block until a run has finished or failed to start.

        Returns
        -------
        task manager | None
            The task manager of the finished run, or None if a run failed to start
        """
        finished_tasks = []
        while (not finished_tasks) and (not self.failed_tasks):
            for task in self.active_tasks:
                try:
                    if task.is_finished():
                        finished_tasks.append(task)
                        self.active_tasks.remove(task)
                        break
                except AlgorithmContainerNotFound:
                    self.log.exception(
                        "Failed to find container for algorithm with run_id %s",
                        task.run_id,
                    )
                    self.failed_tasks.append(task)
                    self.active_tasks.remove(task)
                    break

            # sleep before checking again
            time.sleep(self.poll_interval)

        return finished_tasks.pop() if finished_tasks else None

    def get_result(self) -> Result:
        """
        Returns the oldest (FIFO) finished algorithm run.

        This is a blocking method until a finished run shows up. Once the
        run is obtained and the results are read, the algorithm container is
        removed.

        Returns
        -------
        Result
            result of the algorithm run
        """

        # get finished results and get the first one, if no result is available
        # this is blocking
        finished_task = self._wait_for_finished_task()

        if finished_task:
            # at least one task is finished
            self.log.debug("Run id=%s is finished", finished_task.run_id)

            # Check exit status and report
            logs = finished_task.report_status()

            # Cleanup containers
            finished_task.cleanup()

            # Retrieve results from file
            results = finished_task.get_results()

            self._on_run_finished(finished_task)
        else:
            # at least one task failed to start
            finished_task = self.failed_tasks.pop()
            logs = getattr(finished_task, "failure_logs", None) or (
                "Container failed. Check node logs for details"
            )
            results = b""

        return Result(
            run_id=finished_task.run_id,
            task_id=finished_task.task_id,
            logs=logs,
            data=results,
            status=finished_task.status,
            parent_id=finished_task.parent_id,
        )

    def cleanup_tasks(self) -> list[KilledRun]:
        """
        Stop all active tasks

        Returns
        -------
        list[KilledRun]:
            List of information on tasks that have been killed
        """
        run_ids_killed = []
        if self.active_tasks:
            self.log.debug("Killing %s active task(s)", len(self.active_tasks))
        while self.active_tasks:
            task = self.active_tasks.pop()
            task.cleanup()
            run_ids_killed.append(
                KilledRun(
                    run_id=task.run_id, task_id=task.task_id, parent_id=task.parent_id
                )
            )
        return run_ids_killed

    def kill_selected_tasks(
        self, org_id: int, kill_list: list[ToBeKilled] = None
    ) -> list[KilledRun]:
        """
        Kill tasks specified by a kill list, if they are currently running on
        this node

        Parameters
        ----------
        org_id: int
            The organization id of this node
        kill_list: list[ToBeKilled]
            A list of info about tasks that should be killed.

        Returns
        -------
        list[KilledRun]
            List with information on killed tasks
        """
        killed_list = []
        for container_to_kill in kill_list:
            if container_to_kill["organization_id"] != org_id:
                continue  # this run is on another node
            # find the task
            task = next(
                (
                    t
                    for t in self.active_tasks
                    if t.run_id == container_to_kill["run_id"]
                ),
                None,
            )
            if task:
                self.log.info(f"Killing containers for run_id={task.run_id}")
                self.active_tasks.remove(task)
                task.cleanup()
                killed_list.append(
                    KilledRun(
                        run_id=task.run_id,
                        task_id=task.task_id,
                        parent_id=task.parent_id,
                    )
                )
            else:
                self.log.warning(
                    "Received instruction to kill run_id="
                    f"{container_to_kill['run_id']}, but it was not "
                    "found running on this node."
                )
        return killed_list

    def kill_tasks(
        self, org_id: int, kill_list: list[ToBeKilled] = None
    ) -> list[KilledRun]:
        """
        Kill tasks currently running on this node.

        Parameters
        ----------
        org_id: int
            The organization id of this node
        kill_list: list[ToBeKilled] (optional)
            A list of info on tasks that should be killed. If the list
            is not specified, all running algorithm containers will be killed.

        Returns
        -------
        list[KilledRun]
            List of dictionaries with information on killed tasks
        """
        if kill_list:
            killed_runs = self.kill_selected_tasks(org_id=org_id, kill_list=kill_list)
        else:
            # received instruction to kill all tasks on this node
            self.log.warning(
                "Received instruction from server to kill all algorithms "
                "running on this node. Executing that now..."
            )
            killed_runs = self.cleanup_tasks()
            if len(killed_runs):
                self.log.warning(
                    "Killed the following run ids as instructed via socket:"
                    f" {', '.join([str(r.run_id) for r in killed_runs])}"
                )
            else:
                self.log.warning("Instructed to kill tasks but none were running")
        return killed_runs

    def get_column_names(self, label: str, type_: str) -> list[str]:
        """
        Get column names from a node database

        Parameters
        ----------
        label: str
            Label of the database
        type_: str
            Type of the database

        Returns
        -------
        list[str]
            List of column names
        """
        db = self.databases.get(label)
        if not db:
            self.log.error("Database with label %s not found", label)
            return []
        if not db["is_file"]:
            self.log.error(
                "Database with label %s is not a file. Cannot"
                " determine columns without query",
                label,
            )
            return []
        if db["type"] == "excel":
            self.log.error(
                "Cannot determine columns for excel database without a worksheet"
            )
            return []
        if type_ not in ("csv", "sparql"):
            self.log.error(
                "Cannot determine columns for database of type %s."
                "Only csv and sparql are supported",
                type_,
            )
            return []
        return get_column_names(db["uri"], type_)
