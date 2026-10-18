import os
import tempfile
import unittest
from unittest.mock import patch

from vantage6.node.docker.task_manager import DockerTaskManager

from tests_node.runtime.helpers import (
    EXPECTED_ENV,
    INPUT,
    TOKEN,
    databases,
    init_task_attributes,
    prepare_run,
)


class TestDockerRunIO(unittest.TestCase):
    """The files and environment variables that docker algorithm runs receive"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.task = DockerTaskManager.__new__(DockerTaskManager)
        init_task_attributes(self.task, self.tmp.name, databases(self.tmp.name))
        self.task.proxy = None
        self.task._vpn_manager = None
        self.task.data_volume_name = "data-volume"

    def tearDown(self):
        self.tmp.cleanup()

    def test_environment(self):
        self.assertEqual(prepare_run(self.task), EXPECTED_ENV)

    def test_io_files(self):
        prepare_run(self.task)
        folder = os.path.join(self.tmp.name, "task-000000012")
        with open(os.path.join(folder, "input"), "rb") as fp:
            self.assertEqual(fp.read(), INPUT)
        with open(os.path.join(folder, "token"), "rb") as fp:
            self.assertEqual(fp.read(), TOKEN.encode())
        with open(os.path.join(folder, "output"), "rb") as fp:
            self.assertEqual(fp.read(), b"")

    def test_squid_proxy_environment(self):
        self.task.proxy = type("Squid", (), {"address": "http://squid:3128"})()
        env = prepare_run(self.task)
        self.assertEqual(env["HTTP_PROXY"], "http://squid:3128")
        self.assertEqual(env["https_proxy"], "http://squid:3128")
        self.assertEqual(env["NO_PROXY"], "localhost, proxy.example")

    @patch("vantage6.node.docker.task_manager.running_in_docker", return_value=True)
    def test_volumes(self, _):
        prepare_run(self.task)
        volumes = self.task._prepare_volumes("tmp-volume")
        self.assertEqual(
            volumes,
            {
                "tmp-volume": {"bind": "/mnt/tmp", "mode": "rw"},
                "data-volume": {"bind": "/mnt/data", "mode": "rw"},
            },
        )
