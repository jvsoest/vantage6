import unittest
from datetime import datetime, timedelta, timezone

from vantage6.common.task_status import TaskStatus
from vantage6.node.k8s.config import JobConfig
from vantage6.node.k8s.status import evaluate, exit_code

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _job(conditions=None, age=10):
    created = (NOW - timedelta(seconds=age)).isoformat().replace("+00:00", "Z")
    return {
        "metadata": {"creationTimestamp": created},
        "status": {"conditions": conditions or []},
    }


def _pod(state=None, conditions=None):
    return {
        "metadata": {"name": "p", "creationTimestamp": "2026-01-01T11:59:59Z"},
        "status": {
            "conditions": conditions or [],
            "containerStatuses": [{"name": "algorithm", "state": state or {}}],
        },
    }


def _waiting(reason, message=""):
    return {"waiting": {"reason": reason, "message": message}}


class TestEvaluate(unittest.TestCase):
    config = JobConfig(image_pull_timeout_seconds=300, pending_timeout_seconds=600)

    def evaluate(self, job, pods=()):
        return evaluate(job, list(pods), self.config, now=NOW)

    def test_running(self):
        state = self.evaluate(_job(), [_pod({"running": {}})])
        self.assertFalse(state.finished)

    def test_no_pod_yet(self):
        self.assertFalse(self.evaluate(_job()).finished)

    def test_complete(self):
        state = self.evaluate(_job([{"type": "Complete", "status": "True"}]))
        self.assertTrue(state.finished)
        self.assertEqual(state.status, TaskStatus.COMPLETED)

    def test_failed_with_exit_code(self):
        pod = _pod({"terminated": {"exitCode": 3}})
        state = self.evaluate(
            _job(
                [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]
            ),
            [pod],
        )
        self.assertTrue(state.finished)
        self.assertEqual(state.status, TaskStatus.CRASHED)
        self.assertIn("code 3", state.message)
        self.assertEqual(exit_code([pod]), 3)

    def test_out_of_memory(self):
        state = self.evaluate(
            _job([{"type": "Failed", "status": "True"}]),
            [_pod({"terminated": {"exitCode": 137, "reason": "OOMKilled"}})],
        )
        self.assertEqual(state.status, TaskStatus.CRASHED)
        self.assertIn("OOMKilled", state.message)

    def test_deadline_exceeded(self):
        state = self.evaluate(
            _job([{"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}])
        )
        self.assertEqual(state.status, TaskStatus.CRASHED)
        self.assertIn("maximum run time", state.message)

    def test_condition_not_true_is_ignored(self):
        state = self.evaluate(_job([{"type": "Failed", "status": "False"}]))
        self.assertFalse(state.finished)

    def test_image_pull_error_within_timeout(self):
        state = self.evaluate(_job(age=60), [_pod(_waiting("ImagePullBackOff"))])
        self.assertFalse(state.finished)

    def test_image_pull_error_after_timeout(self):
        state = self.evaluate(
            _job(age=301), [_pod(_waiting("ErrImagePull", "not found"))]
        )
        self.assertTrue(state.finished)
        self.assertEqual(state.status, TaskStatus.NO_DOCKER_IMAGE)
        self.assertIn("not found", state.message)

    def test_invalid_image_name(self):
        state = self.evaluate(_job(), [_pod(_waiting("InvalidImageName"))])
        self.assertEqual(state.status, TaskStatus.NO_DOCKER_IMAGE)

    def test_container_config_error(self):
        state = self.evaluate(_job(), [_pod(_waiting("CreateContainerConfigError"))])
        self.assertEqual(state.status, TaskStatus.START_FAILED)

    def test_create_container_error_after_timeout(self):
        pod = _pod(_waiting("CreateContainerError"))
        self.assertFalse(self.evaluate(_job(age=10), [pod]).finished)
        self.assertEqual(
            self.evaluate(_job(age=301), [pod]).status, TaskStatus.START_FAILED
        )

    def test_unschedulable(self):
        pod = _pod(
            conditions=[
                {
                    "type": "PodScheduled",
                    "status": "False",
                    "reason": "Unschedulable",
                    "message": "0/3 nodes are available",
                }
            ]
        )
        self.assertFalse(self.evaluate(_job(age=60), [pod]).finished)
        state = self.evaluate(_job(age=601), [pod])
        self.assertEqual(state.status, TaskStatus.START_FAILED)
        self.assertIn("0/3 nodes", state.message)

    def test_unschedulable_without_timeout_keeps_waiting(self):
        pod = _pod(
            conditions=[
                {"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}
            ]
        )
        state = evaluate(_job(age=10**6), [pod], JobConfig(), now=NOW)
        self.assertFalse(state.finished)
