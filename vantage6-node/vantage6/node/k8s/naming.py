"""
Names and labels of the Kubernetes objects that the node creates.

Kubernetes object names and label values are limited to 63 characters of a
restricted character set, so the node is identified by a short hash rather than
by its name.
"""

import hashlib

# Labels on the jobs and pods of algorithm runs
LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
LABEL_COMPONENT = "app.kubernetes.io/component"
LABEL_NODE = "vantage6.ai/node"
LABEL_RUN_ID = "vantage6.ai/run-id"
LABEL_TASK_ID = "vantage6.ai/task-id"
LABEL_JOB_ID = "vantage6.ai/job-id"
LABEL_PARENT_ID = "vantage6.ai/parent-id"

MANAGED_BY = "vantage6-node"
COMPONENT_ALGORITHM = "algorithm"

# Annotations on the jobs of algorithm runs
ANNOTATION_NODE_NAME = "vantage6.ai/node-name"
ANNOTATION_IMAGE = "vantage6.ai/image"
ANNOTATION_COLLABORATION_ID = "vantage6.ai/collaboration-id"

# Name of the algorithm container in the pod
ALGORITHM_CONTAINER = "algorithm"


def node_identity(server_url: str, node_id: int) -> str:
    """
    Short identifier of a node, unique per server and node.

    Parameters
    ----------
    server_url: str
        URL of the server the node is connected to
    node_id: int
        ID of the node at the server

    Returns
    -------
    str
        Identifier of 10 hexadecimal characters
    """
    return hashlib.sha256(f"{server_url}|{node_id}".encode()).hexdigest()[:10]


def job_name(node: str, run_id: int) -> str:
    """
    Name of the job of an algorithm run.

    Parameters
    ----------
    node: str
        Node identifier, see :func:`node_identity`
    run_id: int
        ID of the run

    Returns
    -------
    str
        Name of the job
    """
    return f"v6-{node}-run-{run_id}"


def node_selector(node: str) -> str:
    """
    Label selector for the jobs and pods of all algorithm runs of a node.

    Parameters
    ----------
    node: str
        Node identifier, see :func:`node_identity`

    Returns
    -------
    str
        Label selector
    """
    return (
        f"{LABEL_MANAGED_BY}={MANAGED_BY},"
        f"{LABEL_COMPONENT}={COMPONENT_ALGORITHM},"
        f"{LABEL_NODE}={node}"
    )


def run_selector(node: str, run_id: int) -> str:
    """
    Label selector for the job and pods of a single algorithm run.

    Parameters
    ----------
    node: str
        Node identifier, see :func:`node_identity`
    run_id: int
        ID of the run

    Returns
    -------
    str
        Label selector
    """
    return f"{node_selector(node)},{LABEL_RUN_ID}={run_id}"


def run_labels(
    node: str, run_id: int, task_id: int, job_id: int, parent_id: int | None
) -> dict:
    """
    Labels of the job and pods of an algorithm run.

    Parameters
    ----------
    node: str
        Node identifier, see :func:`node_identity`
    run_id: int
        ID of the run
    task_id: int
        ID of the task
    job_id: int
        ID of the job (i.e. the group of tasks that the task belongs to)
    parent_id: int | None
        ID of the parent task, if any

    Returns
    -------
    dict
        Labels
    """
    labels = {
        LABEL_MANAGED_BY: MANAGED_BY,
        LABEL_COMPONENT: COMPONENT_ALGORITHM,
        LABEL_NODE: node,
        LABEL_RUN_ID: str(run_id),
        LABEL_TASK_ID: str(task_id),
        LABEL_JOB_ID: str(job_id),
    }
    if parent_id is not None:
        labels[LABEL_PARENT_ID] = str(parent_id)
    return labels
