"""Test doubles for the Kubernetes runtime"""

from datetime import datetime, timedelta, timezone

from vantage6.node.k8s import naming


def _matches(obj: dict, selector: str) -> bool:
    labels = obj["metadata"].get("labels") or {}
    for requirement in selector.split(","):
        key, value = requirement.split("=")
        if labels.get(key) != value:
            return False
    return True


class FakeKubernetesApi:
    """In-memory stand-in for KubernetesApi"""

    def __init__(self, namespace: str = "ns"):
        self.namespace = namespace
        self.jobs: dict[str, dict] = {}
        self.pods: dict[str, dict] = {}
        self.logs: dict[str, str] = {}
        self.deleted: list[str] = []
        self.create_error: Exception | None = None

    def check_access(self, task_pvc: str) -> None:
        pass

    def create_job(self, manifest: dict) -> dict:
        if self.create_error:
            raise self.create_error
        job = {
            **manifest,
            "metadata": {
                **manifest["metadata"],
                "creationTimestamp": datetime.now(timezone.utc).isoformat(),
            },
            "status": {},
        }
        self.jobs[manifest["metadata"]["name"]] = job
        return job

    def delete_job(self, name: str) -> None:
        self.deleted.append(name)
        job = self.jobs.pop(name, None)
        if job:
            for pod_name in [
                n
                for n, p in self.pods.items()
                if p["metadata"]["labels"] == job["metadata"]["labels"]
            ]:
                del self.pods[pod_name]

    def list_jobs(self, label_selector: str) -> list[dict]:
        return [j for j in self.jobs.values() if _matches(j, label_selector)]

    def list_pods(self, label_selector: str) -> list[dict]:
        return [p for p in self.pods.values() if _matches(p, label_selector)]

    def read_pod_log(self, pod_name: str, container: str) -> str:
        return self.logs.get(pod_name, "")

    def stream_pod_log(self, pod_name, container, since_seconds=None):
        return iter([])

    # helpers to simulate Kubernetes

    def start_pod(self, job_name: str, state: dict | None = None) -> dict:
        """Create the pod of a job, with the given state of its container"""
        job = self.jobs[job_name]
        pod = {
            "metadata": {
                "name": f"{job_name}-abcde",
                "labels": dict(job["metadata"]["labels"]),
                "creationTimestamp": datetime.now(timezone.utc).isoformat(),
            },
            "status": {
                "containerStatuses": [
                    {
                        "name": naming.ALGORITHM_CONTAINER,
                        "state": state or {"running": {}},
                    }
                ]
            },
        }
        self.pods[pod["metadata"]["name"]] = pod
        return pod

    def finish_job(self, job_name: str, exit_code: int = 0, reason: str = "") -> None:
        """Let the container of a job terminate and set the job condition"""
        pod = self.pods.get(f"{job_name}-abcde") or self.start_pod(job_name)
        pod["status"]["containerStatuses"][0]["state"] = {
            "terminated": {"exitCode": exit_code, "reason": reason}
        }
        condition = "Complete" if exit_code == 0 else "Failed"
        self.jobs[job_name]["status"] = {
            "conditions": [{"type": condition, "status": "True"}]
        }

    def age_job(self, job_name: str, seconds: int) -> None:
        created = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        self.jobs[job_name]["metadata"]["creationTimestamp"] = created.isoformat()
