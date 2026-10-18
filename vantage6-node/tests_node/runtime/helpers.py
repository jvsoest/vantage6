"""Helpers to test the contract between the node and algorithm runs."""

import base64
import logging
import os

from vantage6.common.globals import ENV_VAR_EQUALS_REPLACEMENT, STRING_ENCODING
from vantage6.common.task_status import TaskStatus

RUN_ID = 12
TASK_ID = 3
NODE_ID = 4
COLLABORATION_ID = 5
PROXY_HOST = "proxy.example"
TOKEN = "algorithm-token"
INPUT = b'{"method": "central", "args": [], "kwargs": {"a": 1}}'
DATABASES_TO_USE = [
    {"label": "default", "parameters": '{"query": "SELECT 1"}'},
]


def init_task_attributes(task, tasks_dir, databases: dict) -> None:
    """
    Set the attributes that RunIOMixin needs on a task manager that was created
    without calling its constructor.
    """
    task.log = logging.getLogger("test")
    task.run_id = RUN_ID
    task.task_id = TASK_ID
    task.node_id = NODE_ID
    task.collaboration_id = COLLABORATION_ID
    task.databases = databases
    task.write_run_context_file = False
    task.status = TaskStatus.INITIALIZING
    task._tasks_dir = tasks_dir
    task.tmp_folder = "/mnt/tmp"
    task.data_folder = "/mnt/data"


def decode_env(environment: dict) -> dict:
    """Decode environment variables the way the algorithm tools do"""
    decoded = {}
    for key, value in environment.items():
        try:
            decoded[key] = base64.b32decode(
                value.replace(ENV_VAR_EQUALS_REPLACEMENT, "=").encode(STRING_ENCODING)
            ).decode(STRING_ENCODING)
        except Exception:
            decoded[key] = value
    return decoded


def prepare_run(task) -> dict:
    """Run the RunIOMixin steps of starting a run and return the decoded env"""
    old = os.environ.get("PROXY_SERVER_HOST")
    os.environ["PROXY_SERVER_HOST"] = PROXY_HOST
    try:
        task._make_task_folders()
        task._write_io_files(INPUT, TOKEN)
        env = task._prepare_environment(
            algorithm_env={"EXTRA": "x"}, databases_to_use=DATABASES_TO_USE
        )
    finally:
        if old is None:
            del os.environ["PROXY_SERVER_HOST"]
        else:
            os.environ["PROXY_SERVER_HOST"] = old
    return decode_env(env)


# Environment that algorithms receive for the run above, for a node with one
# service database ('default') and one copied csv file ('csv').
EXPECTED_ENV = {
    "INPUT_FILE": "/mnt/data/task-000000012/input",
    "OUTPUT_FILE": "/mnt/data/task-000000012/output",
    "TOKEN_FILE": "/mnt/data/task-000000012/token",
    "TEMPORARY_FOLDER": "/mnt/tmp",
    "HOST": f"http://{PROXY_HOST}",
    "PORT": os.environ.get("PROXY_SERVER_PORT", "8080"),
    "API_PATH": "",
    "DEFAULT_QUERY": "SELECT 1",
    "USER_REQUESTED_DATABASE_LABELS": "default",
    "DEFAULT_DATABASE_URI": "postgresql://db.example/omop",
    "DEFAULT_DATABASE_TYPE": "sql",
    "DEFAULT_DB_PARAM_USER": "me",
    "CSV_DATABASE_URI": "/mnt/data/data.csv",
    "CSV_DATABASE_TYPE": "csv",
    "DB_LABELS": "default,csv",
    "EXTRA": "x",
}


def databases(tasks_dir) -> dict:
    """Databases as the runtimes store them after processing the node config"""
    return {
        "default": {
            "uri": "postgresql://db.example/omop",
            "is_file": False,
            "is_dir": False,
            "type": "sql",
            "env": {"user": "me"},
            "mount_mode": "copy",
            "host_uri": "postgresql://db.example/omop",
            "mount_target": None,
        },
        "csv": {
            "uri": os.path.join(tasks_dir, "data.csv"),
            "is_file": True,
            "is_dir": False,
            "type": "csv",
            "env": {},
            "mount_mode": "copy",
            "host_uri": "/somewhere/data.csv",
            "mount_target": "/mnt/csv.csv",
        },
    }
