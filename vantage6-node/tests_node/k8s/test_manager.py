import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kubernetes.client.exceptions import ApiException

from vantage6.common.task_status import TaskStatus
from vantage6.node.k8s import naming
from vantage6.node.k8s.config import KubernetesConfigError
from vantage6.node.k8s.manager import KubernetesManager
from vantage6.node.k8s.task import KubernetesTaskManager

from tests_node.k8s.fakes import FakeKubernetesApi
from tests_node.runtime.helpers import (
    DATABASES_TO_USE,
    EXPECTED_ENV,
    INPUT,
    TOKEN,
    databases,
    decode_env,
    init_task_attributes,
    prepare_run,
)

TASK_INFO = {"id": 3, "job_id": 7, "parent": None, "init_org": {"id": 1}}


class ManagerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tasks_dir = Path(self.tmp.name)
        self.api = FakeKubernetesApi()
        self.client = MagicMock()
        self.client.base_path = "https://server.example/api"
        self.client.whoami.id_ = 4
        self.client.collaboration_id = 5
        self.config = {
            "databases": [],
            "share_algorithm_logs": False,
            "kubernetes": {
                "namespace": "ns",
                "task_pvc": "tasks",
                "task_volume_mount_path": str(self.tasks_dir),
            },
        }
        # don't contact registries
        patcher = patch(
            "vantage6.node.k8s.manager.get_registry_digest",
            return_value="sha256:" + "b" * 64,
        )
        self.get_digest = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ, {"PROXY_SERVER_HOST": "proxy.example"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def manager(self) -> KubernetesManager:
        ctx = SimpleNamespace(
            config=self.config, name="node", databases=self.config["databases"]
        )
        manager = KubernetesManager(ctx, self.client, api=self.api)
        manager.poll_interval = 0
        return manager

    def start(self, manager, run_id=12, image="registry.example/algo:1"):
        tmp = manager.prepare_job_storage(TASK_INFO["job_id"])
        return manager.run(
            run_id=run_id,
            task_info=TASK_INFO,
            image=image,
            docker_input=INPUT,
            tmp_vol_name=tmp,
            token=TOKEN,
            databases_to_use=[],
            socketIO=None,
        )


class TestKubernetesManager(ManagerTestCase):
    def test_unsupported_config(self):
        self.config["vpn_subnet"] = "10.0.0.0/8"
        with self.assertRaises(KubernetesConfigError):
            self.manager()

    def test_run_creates_job(self):
        manager = self.manager()
        status, vpn_ports = self.start(manager)

        self.assertEqual(status, TaskStatus.ACTIVE)
        self.assertIsNone(vpn_ports)
        self.assertEqual(len(self.api.jobs), 1)
        job = next(iter(self.api.jobs.values()))
        labels = job["metadata"]["labels"]
        self.assertEqual(labels[naming.LABEL_RUN_ID], "12")
        self.assertEqual(labels[naming.LABEL_TASK_ID], "3")
        self.assertEqual(labels[naming.LABEL_NODE], manager.node)
        self.assertNotIn(naming.LABEL_PARENT_ID, labels)
        # the image is pinned to the digest that was checked
        container = job["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(
            container["image"], "registry.example/algo:1@sha256:" + "b" * 64
        )
        self.assertEqual(
            job["metadata"]["annotations"][naming.ANNOTATION_IMAGE],
            "registry.example/algo:1",
        )
        mounts = {m["mountPath"]: m["subPath"] for m in container["volumeMounts"]}
        self.assertEqual(mounts["/mnt/tmp"], "tmp/job-7")
        self.assertEqual(mounts["/mnt/data/task-000000012"], "task-000000012")

        # the files are on the task volume, only accessible by owner and group
        folder = self.tasks_dir / "task-000000012"
        self.assertEqual((folder / "token").read_bytes(), TOKEN.encode())
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o770)
        self.assertEqual(stat.S_IMODE(os.stat(folder / "token").st_mode), 0o660)
        self.assertTrue((self.tasks_dir / "tmp" / "job-7").is_dir())

    def test_image_without_digest_runs_by_tag(self):
        self.get_digest.return_value = None
        manager = self.manager()
        self.start(manager)
        job = next(iter(self.api.jobs.values()))
        self.assertEqual(
            job["spec"]["template"]["spec"]["containers"][0]["image"],
            "registry.example/algo:1",
        )

    def test_registry_credentials(self):
        self.config["docker_registries"] = [
            {"registry": "registry.example", "username": "u", "password": "p"}
        ]
        manager = self.manager()
        manager._resolve_digest("registry.example/algo:1")
        self.get_digest.assert_called_with("registry.example/algo:1", "u", "p")
        manager._resolve_digest("other.example/algo:1")
        self.get_digest.assert_called_with("other.example/algo:1", None, None)

    def test_not_allowed(self):
        self.config["policies"] = {"allowed_algorithms": ["registry.example/other"]}
        manager = self.manager()
        status, _ = self.start(manager)
        self.assertEqual(status, TaskStatus.NOT_ALLOWED)
        self.assertEqual(self.api.jobs, {})

    def test_already_running(self):
        manager = self.manager()
        self.start(manager)
        self.assertTrue(manager.is_running(12))
        status, _ = self.start(manager)
        self.assertEqual(status, TaskStatus.ACTIVE)
        self.assertEqual(len(manager.active_tasks), 1)
        self.assertFalse(manager.is_running(13))

    def test_job_rejected_by_kubernetes(self):
        self.api.create_error = ApiException(status=422, reason="Invalid")
        manager = self.manager()
        status, _ = self.start(manager)
        self.assertEqual(status, TaskStatus.START_FAILED)

        result = manager.get_result()
        self.assertEqual(result.status, TaskStatus.START_FAILED)
        self.assertIn("refused", result.logs)

    def test_job_already_exists(self):
        self.api.create_error = ApiException(status=409, reason="AlreadyExists")
        manager = self.manager()
        status, _ = self.start(manager)
        self.assertEqual(status, TaskStatus.ACTIVE)

    def test_completed_run(self):
        manager = self.manager()
        self.start(manager)
        job_name = naming.job_name(manager.node, 12)
        (self.tasks_dir / "task-000000012" / "output").write_bytes(b"result")
        self.api.finish_job(job_name)
        self.api.logs[f"{job_name}-abcde"] = "algorithm logs"

        result = manager.get_result()

        self.assertEqual(result.run_id, 12)
        self.assertEqual(result.task_id, 3)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.data, b"result")
        self.assertEqual(result.logs, "algorithm logs")
        self.assertIsNone(result.parent_id)
        # the job is removed after reporting
        self.assertIn(job_name, self.api.deleted)
        self.assertEqual(manager.active_tasks, [])

    def test_crashed_run(self):
        manager = self.manager()
        self.start(manager)
        job_name = naming.job_name(manager.node, 12)
        self.api.finish_job(job_name, exit_code=1)
        self.api.logs[f"{job_name}-abcde"] = "Traceback"

        result = manager.get_result()

        self.assertEqual(result.status, TaskStatus.CRASHED)
        self.assertTrue(result.logs.startswith("Traceback"))
        self.assertIn("exited with code 1", result.logs)

    def test_image_cannot_be_pulled(self):
        manager = self.manager()
        self.start(manager)
        job_name = naming.job_name(manager.node, 12)
        self.api.start_pod(job_name, {"waiting": {"reason": "ImagePullBackOff"}})
        self.api.age_job(job_name, 1000)

        result = manager.get_result()

        self.assertEqual(result.status, TaskStatus.NO_DOCKER_IMAGE)
        self.assertIn("ImagePullBackOff", result.logs)
        self.assertNotIn(job_name, self.api.jobs)

    def test_job_disappeared(self):
        manager = self.manager()
        self.start(manager)
        self.api.jobs.clear()

        result = manager.get_result()

        self.assertEqual(result.status, TaskStatus.UNKNOWN_ERROR)

    def test_job_created_after_state_was_observed(self):
        manager = self.manager()
        self.start(manager)
        manager._jobs = {}
        task = manager.active_tasks[0]
        # not in the observed state, but it does exist
        self.assertFalse(task.is_finished())

    def test_kill_selected(self):
        manager = self.manager()
        self.start(manager, run_id=12)
        self.start(manager, run_id=13)

        killed = manager.kill_tasks(
            org_id=1,
            kill_list=[
                {"run_id": 12, "task_id": 3, "organization_id": 1},
                {"run_id": 14, "task_id": 3, "organization_id": 1},
            ],
        )

        self.assertEqual([k.run_id for k in killed], [12])
        self.assertIn(naming.job_name(manager.node, 12), self.api.deleted)
        self.assertEqual([t.run_id for t in manager.active_tasks], [13])

    def test_kill_all(self):
        manager = self.manager()
        self.start(manager, run_id=12)
        self.start(manager, run_id=13)
        killed = manager.kill_tasks(org_id=1)
        self.assertEqual(sorted(k.run_id for k in killed), [12, 13])
        self.assertEqual(self.api.jobs, {})

    def test_cleanup_keeps_jobs(self):
        manager = self.manager()
        self.start(manager)
        manager.cleanup()
        self.assertEqual(len(self.api.jobs), 1)

    def test_cleanup_deletes_jobs_if_configured(self):
        self.config["kubernetes"]["job"] = {"keep_on_shutdown": False}
        manager = self.manager()
        self.start(manager)
        manager.cleanup()
        self.assertEqual(self.api.jobs, {})


class TestReconcile(ManagerTestCase):
    def test_reconcile(self):
        # jobs started by a previous instance of the node
        previous = self.manager()
        self.start(previous, run_id=12)
        self.start(previous, run_id=13)
        self.start(previous, run_id=14)
        # a job of another node in the same namespace
        self.client.whoami.id_ = 99
        other = self.manager()
        self.start(other, run_id=12)
        self.client.whoami.id_ = 4

        statuses = {12: "active", 13: "killed by user", 14: "active"}
        self.client.request.side_effect = lambda path: {
            "status": statuses[int(path.split("/")[1])]
        }
        manager = self.manager()
        manager.reconcile()

        self.assertEqual(sorted(t.run_id for t in manager.active_tasks), [12, 14])
        self.assertIn(naming.job_name(manager.node, 13), self.api.deleted)
        self.assertIn(naming.job_name(other.node, 12), self.api.jobs)
        task = manager.active_tasks[0]
        self.assertEqual(task.status, TaskStatus.ACTIVE)
        self.assertEqual(task.task_id, 3)
        self.assertEqual(task.image, "registry.example/algo:1")
        self.assertTrue(manager.is_running(12))

        # results of picked up runs are reported as usual
        self.api.finish_job(naming.job_name(manager.node, 12))
        (self.tasks_dir / "task-000000012" / "output").write_bytes(b"result")
        result = manager.get_result()
        self.assertEqual((result.run_id, result.data), (12, b"result"))

        # picked up runs get the socket to share logs
        socket = MagicMock()
        manager.attach_socket(socket)
        self.assertIs(manager.active_tasks[0].socket, socket)


class TestEnvironmentContract(unittest.TestCase):
    """
    Algorithms should receive the same environment variables and files on
    Kubernetes nodes as on docker nodes, so that a collaboration can mix both.
    """

    def test_same_environment_as_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = KubernetesTaskManager.__new__(KubernetesTaskManager)
            init_task_attributes(task, tmp, databases(tmp))
            self.assertEqual(prepare_run(task), EXPECTED_ENV)

    def test_run_job_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeKubernetesApi()
            from vantage6.node.k8s.config import KubernetesConfig

            task = KubernetesTaskManager(
                api=api,
                config=KubernetesConfig(namespace="ns", task_pvc="tasks"),
                state_provider=lambda run_id: (None, []),
                image="algo",
                run_image="algo",
                node="abc",
                node_name="node",
                node_id=4,
                run_id=12,
                task_info={"id": 3, "job_id": 7, "parent": {"id": 2}},
                tasks_dir=Path(tmp),
                databases=databases(tmp),
                socketIO=None,
                collaboration_id=5,
            )
            with patch.dict(os.environ, {"PROXY_SERVER_HOST": "proxy.example"}):
                task.run(INPUT, "tmp/job-7", TOKEN, {"EXTRA": "x"}, DATABASES_TO_USE)
            job = api.jobs["v6-abc-run-12"]
            container = job["spec"]["template"]["spec"]["containers"][0]
            env = decode_env({e["name"]: e["value"] for e in container["env"]})
            self.assertEqual(env, EXPECTED_ENV)
            self.assertEqual(job["metadata"]["labels"][naming.LABEL_PARENT_ID], "2")
            # the copied csv database is mounted where the environment points
            self.assertIn(
                {
                    "name": "task-volume",
                    "mountPath": "/mnt/data/data.csv",
                    "subPath": "databases/data.csv",
                    "readOnly": True,
                },
                container["volumeMounts"],
            )
