"""
Thin wrapper around the Kubernetes API client.

All Kubernetes objects are exchanged as dictionaries in the format of the
Kubernetes API (camelCase keys), which keeps the rest of the runtime independent
of the model classes of the Kubernetes client.
"""

import logging
from typing import Iterator

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException

from vantage6.common import logger_name

log = logging.getLogger(logger_name(__name__))

# Seconds to wait for a response of the Kubernetes API
REQUEST_TIMEOUT = 30


class KubernetesApi:
    """
    Access to the Kubernetes API, limited to the namespace of the node.

    Parameters
    ----------
    namespace: str
        Namespace in which the jobs of algorithm runs are managed
    """

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        try:
            # running in a pod: use the service account of the pod
            k8s_config.load_incluster_config()
        except ConfigException:
            # running elsewhere, e.g. during development: use the kubeconfig
            log.info("Not running in a Kubernetes pod, using kubeconfig")
            k8s_config.load_kube_config()
        self.api_client = k8s_client.ApiClient()
        self.batch = k8s_client.BatchV1Api(self.api_client)
        self.core = k8s_client.CoreV1Api(self.api_client)

    def _to_dict(self, obj) -> dict:
        """Convert a Kubernetes model object to an API dictionary"""
        return self.api_client.sanitize_for_serialization(obj)

    def check_access(self, task_pvc: str) -> None:
        """
        Check that the node is allowed to manage jobs and that the task volume
        exists. Raises an ApiException if not.

        Parameters
        ----------
        task_pvc: str
            Name of the persistent volume claim of the task volume
        """
        self.batch.list_namespaced_job(
            self.namespace, limit=1, _request_timeout=REQUEST_TIMEOUT
        )
        self.core.list_namespaced_pod(
            self.namespace, limit=1, _request_timeout=REQUEST_TIMEOUT
        )
        try:
            self.core.read_namespaced_persistent_volume_claim(
                task_pvc, self.namespace, _request_timeout=REQUEST_TIMEOUT
            )
        except ApiException as exc:
            if exc.status == 404:
                raise
            # reading PVCs is not required to run algorithms
            log.debug("Not allowed to verify the task volume claim: %s", exc.reason)

    def create_job(self, manifest: dict) -> dict:
        """Create a job from a manifest"""
        job = self.batch.create_namespaced_job(
            self.namespace, manifest, _request_timeout=REQUEST_TIMEOUT
        )
        return self._to_dict(job)

    def delete_job(self, name: str) -> None:
        """Delete a job and its pods. Does nothing if the job does not exist."""
        try:
            self.batch.delete_namespaced_job(
                name,
                self.namespace,
                propagation_policy="Background",
                grace_period_seconds=10,
                _request_timeout=REQUEST_TIMEOUT,
            )
        except ApiException as exc:
            if exc.status != 404:
                raise

    def list_jobs(self, label_selector: str) -> list[dict]:
        """List the jobs that match a label selector"""
        jobs = self.batch.list_namespaced_job(
            self.namespace,
            label_selector=label_selector,
            _request_timeout=REQUEST_TIMEOUT,
        )
        return [self._to_dict(job) for job in jobs.items]

    def list_pods(self, label_selector: str) -> list[dict]:
        """List the pods that match a label selector"""
        pods = self.core.list_namespaced_pod(
            self.namespace,
            label_selector=label_selector,
            _request_timeout=REQUEST_TIMEOUT,
        )
        return [self._to_dict(pod) for pod in pods.items]

    def read_pod_log(self, pod_name: str, container: str) -> str:
        """Read the complete log of a container"""
        return self.core.read_namespaced_pod_log(
            pod_name,
            self.namespace,
            container=container,
            _request_timeout=REQUEST_TIMEOUT,
        )

    def stream_pod_log(
        self, pod_name: str, container: str, since_seconds: int | None = None
    ) -> Iterator[str]:
        """
        Follow the log of a container, yielding it line by line until the
        container stops.
        """
        kwargs = {"since_seconds": since_seconds} if since_seconds else {}
        response = self.core.read_namespaced_pod_log(
            pod_name,
            self.namespace,
            container=container,
            follow=True,
            _preload_content=False,
            **kwargs,
        )
        try:
            # the (urllib3) response is a file-like object that yields lines
            for line in response:
                yield line.decode("utf-8", errors="replace")
        finally:
            response.release_conn()
