"""
Configuration of the Kubernetes runtime.

The runtime is configured in the ``kubernetes`` section of the node configuration
file. Values that describe Kubernetes objects (resources, tolerations, affinity,
etc.) are passed on to Kubernetes as-is and should therefore be written as in a
Kubernetes manifest (i.e. camelCase keys).
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from vantage6.common import logger_name
from vantage6.common.globals import DEFAULT_DB_MOUNT_MODE

log = logging.getLogger(logger_name(__name__))

SERVICE_ACCOUNT_NAMESPACE_FILE = Path(
    "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
)

# Node configuration options that require Docker and cannot be used with the
# Kubernetes runtime
UNSUPPORTED_OPTIONS = {
    "vpn_subnet": "VPN (node-to-node communication)",
    "ssh-tunnels": "SSH tunnels",
    "whitelist": "the squid proxy whitelist (use NetworkPolicies instead)",
    "docker_services": "linking docker services",
}

# Node configuration options that only apply to Docker nodes and are ignored
IGNORED_OPTIONS = (
    "node_extra_env",
    "node_extra_mounts",
    "node_extra_hosts",
    "alpine",
    "images",
)


class KubernetesConfigError(Exception):
    """Raised when the node configuration is not valid for Kubernetes"""


@dataclass
class JobConfig:
    """Settings of the Kubernetes jobs that run the algorithms"""

    # Seconds after which Kubernetes removes finished jobs. The node removes jobs
    # itself after it has reported their results, so this is only a safety net.
    ttl_seconds_after_finished: int = 86400
    # Maximum number of seconds that an algorithm may run. None for no limit.
    active_deadline_seconds: int | None = None
    # Seconds after which a run fails if its image cannot be pulled
    image_pull_timeout_seconds: int = 300
    # Seconds after which a run fails if it cannot be scheduled. None to wait
    # until it can be scheduled.
    pending_timeout_seconds: int | None = None
    # Whether to leave running jobs alone when the node shuts down, so that the
    # node can pick them up again when it is restarted (e.g. rescheduled)
    keep_on_shutdown: bool = True


@dataclass
class AlgorithmPodConfig:
    """Settings of the pods in which the algorithms run"""

    resources: dict = field(default_factory=dict)
    node_selector: dict = field(default_factory=dict)
    tolerations: list = field(default_factory=list)
    affinity: dict = field(default_factory=dict)
    priority_class_name: str | None = None
    # Pod security context, e.g. runAsUser, runAsGroup and fsGroup. Leave the user
    # and group out on OpenShift, which assigns them per namespace.
    security_context: dict = field(default_factory=dict)
    read_only_root_filesystem: bool = False
    gpu_resource_name: str = "nvidia.com/gpu"
    # Schedule algorithm pods on the same Kubernetes node as the node pod. Required
    # if the task volume can only be mounted on one Kubernetes node at a time
    # (ReadWriteOnce).
    co_locate_with_node: bool = False
    labels: dict = field(default_factory=dict)
    annotations: dict = field(default_factory=dict)


@dataclass
class KubernetesConfig:
    """Settings of the Kubernetes runtime"""

    # Namespace in which the node runs and in which the jobs are created
    namespace: str
    # Persistent volume claim that holds the task files. It must be mounted in
    # the node pod at `task_volume_mount_path`.
    task_pvc: str
    task_volume_mount_path: Path = Path("/mnt/data")
    # Service account for the algorithm pods. Should not have any permissions.
    algorithm_service_account: str | None = None
    image_pull_secrets: list[str] = field(default_factory=list)
    # Labels of the node pod, used to select it in pod affinity rules
    node_pod_labels: dict = field(default_factory=dict)
    job: JobConfig = field(default_factory=JobConfig)
    algorithm_pod: AlgorithmPodConfig = field(default_factory=AlgorithmPodConfig)

    @classmethod
    def from_node_config(cls, config: dict) -> "KubernetesConfig":
        """
        Create the Kubernetes settings from the node configuration

        Parameters
        ----------
        config: dict
            Node configuration

        Returns
        -------
        KubernetesConfig
            Settings of the Kubernetes runtime

        Raises
        ------
        KubernetesConfigError
            If the settings are invalid
        """
        k8s = dict(config.get("kubernetes") or {})
        try:
            job = JobConfig(**(k8s.pop("job", None) or {}))
            algorithm_pod = AlgorithmPodConfig(**(k8s.pop("algorithm_pod", None) or {}))
        except TypeError as exc:
            raise KubernetesConfigError(f"Invalid kubernetes configuration: {exc}")

        namespace = k8s.pop("namespace", None) or _current_namespace()
        if not namespace:
            raise KubernetesConfigError(
                "Could not determine the namespace of the node. Set "
                "'kubernetes.namespace' in the node configuration or the "
                "POD_NAMESPACE environment variable."
            )
        task_pvc = k8s.pop("task_pvc", None)
        if not task_pvc:
            raise KubernetesConfigError(
                "Set 'kubernetes.task_pvc' to the persistent volume claim that "
                "holds the task files."
            )
        if "task_volume_mount_path" in k8s:
            k8s["task_volume_mount_path"] = Path(k8s["task_volume_mount_path"])
        try:
            return cls(
                namespace=namespace,
                task_pvc=task_pvc,
                job=job,
                algorithm_pod=algorithm_pod,
                **k8s,
            )
        except TypeError as exc:
            raise KubernetesConfigError(f"Invalid kubernetes configuration: {exc}")


def _current_namespace() -> str | None:
    """Namespace of the pod in which the node runs"""
    namespace = os.environ.get("POD_NAMESPACE")
    if namespace:
        return namespace
    try:
        return SERVICE_ACCOUNT_NAMESPACE_FILE.read_text().strip()
    except OSError:
        return None


def validate_node_config(config: dict) -> None:
    """
    Check that the node configuration can be used with the Kubernetes runtime.

    Parameters
    ----------
    config: dict
        Node configuration

    Raises
    ------
    KubernetesConfigError
        If the configuration contains options that are not supported
    """
    errors = []
    for option, description in UNSUPPORTED_OPTIONS.items():
        if config.get(option):
            errors.append(
                f"'{option}' is set, but {description} is not supported on "
                "Kubernetes nodes."
            )

    for db in config.get("databases") or []:
        mount_mode = str(db.get("mount_mode", DEFAULT_DB_MOUNT_MODE)).lower()
        if mount_mode == "ro" and not (db.get("volume") or {}).get("claim_name"):
            errors.append(
                f"Database '{db.get('label')}' uses mount_mode 'ro', which requires "
                "'volume.claim_name' on Kubernetes nodes."
            )
    if errors:
        raise KubernetesConfigError(" ".join(errors))

    for option in IGNORED_OPTIONS:
        if config.get(option):
            log.warning(
                "Ignoring '%s': this option only applies to docker nodes.", option
            )
