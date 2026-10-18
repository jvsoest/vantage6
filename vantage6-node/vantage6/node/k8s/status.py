"""
Translation of the state of a Kubernetes Job and its pod into the status of an
algorithm run.

The functions in this module work on the Job and Pod objects as dictionaries, as
returned by the Kubernetes API (camelCase keys).
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from vantage6.common.task_status import TaskStatus
from vantage6.node.k8s.config import JobConfig
from vantage6.node.k8s.naming import ALGORITHM_CONTAINER

# Reasons that a container is waiting because its image cannot be pulled
IMAGE_PULL_ERRORS = {"ErrImagePull", "ImagePullBackOff"}
# Reasons that a container will never start
PERMANENT_START_ERRORS = {
    "InvalidImageName",
    "ErrImageNeverPull",
    "CreateContainerConfigError",
}
# Reasons that a container could not be created, which may be transient
CREATE_ERRORS = {"CreateContainerError", "RunContainerError"}


@dataclass
class RunState:
    """State of an algorithm run as derived from its Job and pod"""

    finished: bool
    status: TaskStatus | None = None
    # explanation for the user, e.g. why a run failed
    message: str | None = None


def evaluate(
    job: dict, pods: list[dict], config: JobConfig, now: datetime | None = None
) -> RunState:
    """
    Determine whether an algorithm run has finished and with what status.

    Parameters
    ----------
    job: dict
        The Job of the run
    pods: list[dict]
        The pods of the Job. There is normally one pod, as Jobs are not retried.
    config: JobConfig
        Settings of the Kubernetes jobs, containing timeouts
    now: datetime | None
        Current time, defaults to the actual current time

    Returns
    -------
    RunState
        State of the run
    """
    now = now or datetime.now(timezone.utc)
    pod = _latest(pods)
    container = _container_status(pod)

    for condition in job.get("status", {}).get("conditions") or []:
        if condition.get("status") != "True":
            continue
        if condition.get("type") == "Complete":
            return RunState(finished=True, status=TaskStatus.COMPLETED)
        if condition.get("type") == "Failed":
            return RunState(
                finished=True,
                status=TaskStatus.CRASHED,
                message=_failure_message(condition, container),
            )

    if not pod:
        return RunState(finished=False)

    waiting = (container.get("state") or {}).get("waiting") or {}
    reason = waiting.get("reason")
    age = _age(job, now)

    if reason in PERMANENT_START_ERRORS:
        status = (
            TaskStatus.START_FAILED
            if reason == "CreateContainerConfigError"
            else TaskStatus.NO_DOCKER_IMAGE
        )
        return RunState(finished=True, status=status, message=_waiting_message(waiting))
    if reason in IMAGE_PULL_ERRORS and age > config.image_pull_timeout_seconds:
        return RunState(
            finished=True,
            status=TaskStatus.NO_DOCKER_IMAGE,
            message=_waiting_message(waiting),
        )
    if reason in CREATE_ERRORS and age > config.image_pull_timeout_seconds:
        return RunState(
            finished=True,
            status=TaskStatus.START_FAILED,
            message=_waiting_message(waiting),
        )

    unschedulable = _unschedulable_condition(pod)
    if (
        unschedulable
        and config.pending_timeout_seconds is not None
        and age > config.pending_timeout_seconds
    ):
        return RunState(
            finished=True,
            status=TaskStatus.START_FAILED,
            message=(
                "Algorithm could not be scheduled on the Kubernetes cluster: "
                f"{unschedulable.get('message', '')}"
            ),
        )

    return RunState(finished=False)


def exit_code(pods: list[dict]) -> int | None:
    """
    Exit code of the algorithm container, if it has terminated.

    Parameters
    ----------
    pods: list[dict]
        The pods of the Job

    Returns
    -------
    int | None
        Exit code, or None if the container has not terminated
    """
    terminated = (_container_status(_latest(pods)).get("state") or {}).get("terminated")
    return terminated.get("exitCode") if terminated else None


def is_running(pod: dict | None) -> bool:
    """Whether the algorithm container of a pod is running or has run"""
    if not pod:
        return False
    state = _container_status(pod).get("state") or {}
    return bool(state.get("running") or state.get("terminated"))


def _latest(pods: list[dict]) -> dict | None:
    """Most recently created pod"""
    if not pods:
        return None
    return max(pods, key=lambda p: p.get("metadata", {}).get("creationTimestamp", ""))


def _container_status(pod: dict | None) -> dict:
    """Status of the algorithm container in a pod"""
    if not pod:
        return {}
    for status in pod.get("status", {}).get("containerStatuses") or []:
        if status.get("name") == ALGORITHM_CONTAINER:
            return status
    return {}


def _unschedulable_condition(pod: dict) -> dict | None:
    """The PodScheduled condition of a pod if it could not be scheduled"""
    for condition in pod.get("status", {}).get("conditions") or []:
        if (
            condition.get("type") == "PodScheduled"
            and condition.get("status") == "False"
            and condition.get("reason") == "Unschedulable"
        ):
            return condition
    return None


def _age(job: dict, now: datetime) -> float:
    """Seconds since the Job was created"""
    created = job.get("metadata", {}).get("creationTimestamp")
    if not created:
        return 0
    if isinstance(created, str):
        created = datetime.fromisoformat(created.replace("Z", "+00:00"))
    return (now - created).total_seconds()


def _failure_message(condition: dict, container: dict) -> str:
    """Explanation of why a Job failed"""
    terminated = (container.get("state") or {}).get("terminated") or {}
    if condition.get("reason") == "DeadlineExceeded":
        return (
            "Algorithm was stopped because it exceeded the maximum run time of "
            "this node."
        )
    parts = []
    if terminated.get("exitCode") is not None:
        parts.append(f"Algorithm exited with code {terminated['exitCode']}")
    if terminated.get("reason") == "OOMKilled":
        parts.append("the algorithm ran out of memory (OOMKilled)")
    elif condition.get("message"):
        parts.append(condition["message"])
    return ": ".join(parts) if parts else "Algorithm failed"


def _waiting_message(waiting: dict) -> str:
    """Explanation of why a container could not be started"""
    message = f"Algorithm could not be started: {waiting.get('reason')}"
    if waiting.get("message"):
        message += f" ({waiting['message']})"
    return message
