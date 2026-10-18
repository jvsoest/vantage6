"""
Databases of a Kubernetes node.

Three kinds of databases are supported:

- Service databases (e.g. a SQL server): the URI is passed to the algorithm as-is.
  Network access must be allowed with a NetworkPolicy.
- File or folder databases with ``mount_mode: ro``: the file or folder lives on an
  existing persistent volume claim (``volume.claim_name``, optionally in
  ``volume.sub_path``) and is mounted read-only in the algorithm pods.
- File databases with ``mount_mode: copy`` (the default): the node copies the
  file to the task volume when it starts, and mounts the copy read-only in the
  algorithm pods.

The node should have access to file databases as well (the deployment mounts
claims of read-only databases at ``/mnt/databases/<label>``), so that it can share
the column names of the database with the server.
"""

import logging
import os
import shutil
from pathlib import Path

from vantage6.common import get_database_config, logger_name
from vantage6.common.globals import DEFAULT_DB_MOUNT_MODE
from vantage6.node.k8s.config import KubernetesConfigError
from vantage6.node.k8s.job_spec import TASK_VOLUME

log = logging.getLogger(logger_name(__name__))

SUPPORTED_DATABASE_MOUNT_MODES = {"copy", "ro"}

# Folder on the task volume to which databases are copied
COPIED_DATABASES_FOLDER = "databases"


def setup_databases(
    databases: list[dict], tasks_dir: Path, databases_mount_root: Path
) -> dict:
    """
    Process the databases from the node configuration.

    Parameters
    ----------
    databases: list[dict]
        Databases as specified in the node configuration
    tasks_dir: Path
        Root of the task volume in the node pod
    databases_mount_root: Path
        Folder in the node pod at which the claims of read-only databases are
        mounted, in subfolders named after the database label

    Returns
    -------
    dict
        Databases by label, in the same format as used by the docker runtime, with
        the additional key 'volume' for read-only databases

    Raises
    ------
    KubernetesConfigError
        If a database is configured in a way that is not supported
    """
    result = {}
    for label in [db["label"] for db in databases or []]:
        db_config = get_database_config(databases, label)
        mount_mode = str(db_config.get("mount_mode", DEFAULT_DB_MOUNT_MODE)).lower()
        if mount_mode not in SUPPORTED_DATABASE_MOUNT_MODES:
            raise KubernetesConfigError(
                f"Invalid mount_mode '{mount_mode}' for database '{label}'. "
                "Supported values are 'copy' and 'ro'."
            )
        if mount_mode == "ro":
            result[label] = _read_only_database(label, db_config, databases_mount_root)
        else:
            result[label] = _copied_or_service_database(
                label, db_config, tasks_dir, databases_mount_root
            )
    log.debug("Databases: %s", result)
    return result


def _base(db_config: dict, uri: str, mount_mode: str) -> dict:
    """Database properties that are the same for all kinds of databases"""
    return {
        "uri": uri,
        "is_file": False,
        "is_dir": False,
        "type": db_config["type"],
        "env": db_config.get("env", {}),
        "mount_mode": mount_mode,
        "host_uri": db_config["uri"],
        "mount_target": None,
    }


def _read_only_database(label: str, db_config: dict, mount_root: Path) -> dict:
    """
    A file or folder on an existing persistent volume claim that is mounted
    read-only in algorithm pods
    """
    volume = db_config.get("volume") or {}
    if not volume.get("claim_name"):
        raise KubernetesConfigError(
            f"Database '{label}' uses mount_mode 'ro', which requires "
            "'volume.claim_name' on Kubernetes nodes."
        )
    uri = str(db_config["uri"])
    if os.path.isabs(uri):
        raise KubernetesConfigError(
            f"The uri of database '{label}' should be relative to the root of "
            f"volume '{volume['claim_name']}'."
        )

    # location in the node pod, where the deployment should have mounted the claim
    node_path = mount_root / label / uri
    if node_path.exists():
        is_file = node_path.is_file()
    else:
        # we can't check, so guess based on the name
        is_file = bool(Path(uri).suffix)
        log.warning(
            "Database '%s' is not mounted in the node pod at %s. Assuming it is a "
            "%s. The node cannot share the column names of this database.",
            label,
            node_path,
            "file" if is_file else "folder",
        )

    db = _base(db_config, str(node_path), "ro")
    db["is_file"] = is_file
    db["is_dir"] = not is_file
    db["mount_target"] = (
        f"/mnt/{label}{Path(uri).suffix}" if is_file else f"/mnt/{label}"
    )
    db["volume"] = {
        "claim_name": volume["claim_name"],
        "sub_path": os.path.normpath(os.path.join(volume.get("sub_path") or "", uri)),
    }
    log.info(
        "Using read-only mount for database '%s' from volume '%s'",
        label,
        volume["claim_name"],
    )
    return db


def _copied_or_service_database(
    label: str, db_config: dict, tasks_dir: Path, mount_root: Path
) -> dict:
    """
    A file that is copied to the task volume, or a service that the algorithm
    connects to
    """
    uri = str(db_config["uri"])
    if db_config.get("volume"):
        source = mount_root / label / uri
    else:
        source = Path(uri)

    if source.is_dir():
        raise KubernetesConfigError(
            f"Database '{label}' is a folder. Folders can only be used with "
            "mount_mode 'ro' on Kubernetes nodes."
        )
    if not source.is_file():
        # not a file in the node pod, so it should be a service (e.g. a database
        # server) that algorithms can connect to
        return _base(db_config, uri, "copy")

    target_dir = tasks_dir / COPIED_DATABASES_FOLDER
    os.makedirs(target_dir, exist_ok=True)
    target = target_dir / source.name
    log.info("Copying %s to %s", source, target_dir)
    shutil.copy(source, target)
    os.chmod(target, 0o644)

    db = _base(db_config, str(target), "copy")
    db["is_file"] = True
    return db


def algorithm_volumes(databases: dict, data_folder: str) -> tuple[list, list]:
    """
    Volumes and volume mounts that give algorithm pods access to the databases.

    Parameters
    ----------
    databases: dict
        Databases as returned by :func:`setup_databases`
    data_folder: str
        Location of the root of the task volume in the algorithm container

    Returns
    -------
    tuple[list, list]
        Volumes and volume mounts to add to the algorithm pod
    """
    volumes = []
    volume_mounts = []
    claims = {}
    for label, db in databases.items():
        if db["mount_mode"] == "ro":
            claim = db["volume"]["claim_name"]
            if claim not in claims:
                claims[claim] = f"database-{len(claims)}"
                volumes.append(
                    {
                        "name": claims[claim],
                        "persistentVolumeClaim": {"claimName": claim, "readOnly": True},
                    }
                )
            volume_mounts.append(
                {
                    "name": claims[claim],
                    "mountPath": db["mount_target"],
                    "subPath": db["volume"]["sub_path"],
                    "readOnly": True,
                }
            )
        elif db["is_file"]:
            # copied to the task volume; the algorithm only gets to see the copy
            basename = os.path.basename(db["uri"])
            volume_mounts.append(
                {
                    "name": TASK_VOLUME,
                    "mountPath": f"{data_folder}/{basename}",
                    "subPath": f"{COPIED_DATABASES_FOLDER}/{basename}",
                    "readOnly": True,
                }
            )
    return volumes, volume_mounts
