"""
Kubernetes Job manifests for algorithm runs.

The manifest is built as a plain dictionary (as it would be written in YAML), so
that values from the node configuration, such as resources and tolerations, can be
passed on to Kubernetes as-is.
"""

import copy
from dataclasses import dataclass, field

from vantage6.node.k8s.config import KubernetesConfig
from vantage6.node.k8s.naming import ALGORITHM_CONTAINER

TASK_VOLUME = "task-volume"
TMP_VOLUME = "tmp"

# Security settings that are always applied to algorithm pods. They are compatible
# with the 'restricted' Pod Security Standard and the OpenShift restricted-v2 SCC.
POD_SECURITY_CONTEXT = {
    "runAsNonRoot": True,
    "seccompProfile": {"type": "RuntimeDefault"},
}
CONTAINER_SECURITY_CONTEXT = {
    "allowPrivilegeEscalation": False,
    "privileged": False,
    "capabilities": {"drop": ["ALL"]},
}


@dataclass
class AlgorithmJob:
    """Run-specific input for the job manifest of an algorithm run"""

    name: str
    image: str
    labels: dict
    annotations: dict
    # (encoded) environment variables of the algorithm
    environment: dict
    # path of the run folder relative to the root of the task volume
    run_folder: str
    # path of the run folder in the algorithm container
    run_folder_mount_path: str
    # path of the temporary folder relative to the root of the task volume
    tmp_folder: str
    # path of the temporary folder in the algorithm container
    tmp_folder_mount_path: str
    requires_pull: bool = True
    gpu: bool = False
    # additional volumes and volume mounts, e.g. for databases
    volumes: list[dict] = field(default_factory=list)
    volume_mounts: list[dict] = field(default_factory=list)


def build_job(job: AlgorithmJob, config: KubernetesConfig) -> dict:
    """
    Build the Kubernetes Job manifest of an algorithm run.

    Parameters
    ----------
    job: AlgorithmJob
        Run-specific input
    config: KubernetesConfig
        Settings of the Kubernetes runtime

    Returns
    -------
    dict
        Job manifest
    """
    pod_config = config.algorithm_pod
    job_config = config.job

    volumes = [
        {"name": TASK_VOLUME, "persistentVolumeClaim": {"claimName": config.task_pvc}}
    ]
    volume_mounts = [
        {
            "name": TASK_VOLUME,
            "mountPath": job.run_folder_mount_path,
            "subPath": job.run_folder,
        },
        {
            "name": TASK_VOLUME,
            "mountPath": job.tmp_folder_mount_path,
            "subPath": job.tmp_folder,
        },
    ]
    volumes.extend(job.volumes)
    volume_mounts.extend(job.volume_mounts)
    if pod_config.read_only_root_filesystem:
        # many libraries need a writable /tmp
        volumes.append({"name": TMP_VOLUME, "emptyDir": {}})
        volume_mounts.append({"name": TMP_VOLUME, "mountPath": "/tmp"})

    resources = copy.deepcopy(pod_config.resources)
    if job.gpu:
        resources.setdefault("limits", {})[pod_config.gpu_resource_name] = 1

    container = {
        "name": ALGORITHM_CONTAINER,
        "image": job.image,
        "imagePullPolicy": "Always" if job.requires_pull else "IfNotPresent",
        "env": [
            {"name": key, "value": str(value)} for key, value in job.environment.items()
        ],
        "volumeMounts": volume_mounts,
        "securityContext": {
            **CONTAINER_SECURITY_CONTEXT,
            "readOnlyRootFilesystem": pod_config.read_only_root_filesystem,
        },
    }
    if resources:
        container["resources"] = resources

    pod_spec = {
        "restartPolicy": "Never",
        # algorithms have no business with the Kubernetes API
        "automountServiceAccountToken": False,
        # don't leak information on other services in the namespace
        "enableServiceLinks": False,
        "securityContext": {**POD_SECURITY_CONTEXT, **pod_config.security_context},
        "containers": [container],
        "volumes": volumes,
    }
    if config.algorithm_service_account:
        pod_spec["serviceAccountName"] = config.algorithm_service_account
    if config.image_pull_secrets:
        pod_spec["imagePullSecrets"] = [
            {"name": name} for name in config.image_pull_secrets
        ]
    if pod_config.node_selector:
        pod_spec["nodeSelector"] = pod_config.node_selector
    if pod_config.tolerations:
        pod_spec["tolerations"] = pod_config.tolerations
    if pod_config.priority_class_name:
        pod_spec["priorityClassName"] = pod_config.priority_class_name
    affinity = _affinity(config)
    if affinity:
        pod_spec["affinity"] = affinity

    job_spec = {
        # an algorithm run is never retried by Kubernetes, the node reports the
        # failure instead
        "backoffLimit": 0,
        "completions": 1,
        "parallelism": 1,
        "ttlSecondsAfterFinished": job_config.ttl_seconds_after_finished,
        "template": {
            "metadata": {
                "labels": {**pod_config.labels, **job.labels},
                "annotations": {**pod_config.annotations, **job.annotations},
            },
            "spec": pod_spec,
        },
    }
    if job_config.active_deadline_seconds:
        job_spec["activeDeadlineSeconds"] = job_config.active_deadline_seconds

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job.name,
            "namespace": config.namespace,
            "labels": job.labels,
            "annotations": job.annotations,
        },
        "spec": job_spec,
    }


def _affinity(config: KubernetesConfig) -> dict:
    """
    Affinity rules of algorithm pods, including the rule to schedule them next to
    the node pod if the task volume can only be mounted on a single Kubernetes node.

    Parameters
    ----------
    config: KubernetesConfig
        Settings of the Kubernetes runtime

    Returns
    -------
    dict
        Affinity rules
    """
    affinity = copy.deepcopy(config.algorithm_pod.affinity)
    if config.algorithm_pod.co_locate_with_node:
        pod_affinity = affinity.setdefault("podAffinity", {})
        required = pod_affinity.setdefault(
            "requiredDuringSchedulingIgnoredDuringExecution", []
        )
        required.append(
            {
                "labelSelector": {"matchLabels": config.node_pod_labels},
                "topologyKey": "kubernetes.io/hostname",
            }
        )
    return affinity
