from typing import NamedTuple


class Result(NamedTuple):
    """
    Data class to store the result of an algorithm run.

    Attributes
    ----------
    run_id: int
        ID of the current algorithm run
    task_id: int
        ID of the task the run belongs to
    logs: str
        Logs attached to current algorithm run
    data: str
        Output data of the algorithm
    status: str
        Status of the algorithm run
    parent_id: int | None
        ID of the parent task, if any
    """

    run_id: int
    task_id: int
    logs: str
    data: str
    status: str
    parent_id: int | None


class ToBeKilled(NamedTuple):
    """Data class to store which tasks should be killed"""

    task_id: int
    run_id: int
    organization_id: int


class KilledRun(NamedTuple):
    """Data class to store which algorithms have been killed"""

    run_id: int
    task_id: int
    parent_id: int
