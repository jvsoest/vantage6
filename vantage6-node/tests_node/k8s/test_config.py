import os
import unittest
from pathlib import Path
from unittest.mock import patch

from vantage6.node.k8s.config import (
    KubernetesConfig,
    KubernetesConfigError,
    validate_node_config,
)


class TestKubernetesConfig(unittest.TestCase):
    def test_defaults(self):
        config = KubernetesConfig.from_node_config(
            {"kubernetes": {"namespace": "ns", "task_pvc": "tasks"}}
        )
        self.assertEqual(config.namespace, "ns")
        self.assertEqual(config.task_pvc, "tasks")
        self.assertEqual(config.task_volume_mount_path, Path("/mnt/data"))
        self.assertTrue(config.job.keep_on_shutdown)
        self.assertEqual(config.job.image_pull_timeout_seconds, 300)
        self.assertFalse(config.algorithm_pod.co_locate_with_node)

    def test_nested_settings(self):
        config = KubernetesConfig.from_node_config(
            {
                "kubernetes": {
                    "namespace": "ns",
                    "task_pvc": "tasks",
                    "task_volume_mount_path": "/data",
                    "job": {"active_deadline_seconds": 60},
                    "algorithm_pod": {"resources": {"limits": {"cpu": "1"}}},
                }
            }
        )
        self.assertEqual(config.task_volume_mount_path, Path("/data"))
        self.assertEqual(config.job.active_deadline_seconds, 60)
        self.assertEqual(config.algorithm_pod.resources, {"limits": {"cpu": "1"}})

    @patch.dict(os.environ, {"POD_NAMESPACE": "from-env"})
    def test_namespace_from_environment(self):
        config = KubernetesConfig.from_node_config({"kubernetes": {"task_pvc": "t"}})
        self.assertEqual(config.namespace, "from-env")

    def test_task_pvc_required(self):
        with self.assertRaises(KubernetesConfigError):
            KubernetesConfig.from_node_config({"kubernetes": {"namespace": "ns"}})

    def test_unknown_setting(self):
        with self.assertRaises(KubernetesConfigError):
            KubernetesConfig.from_node_config(
                {"kubernetes": {"namespace": "ns", "task_pvc": "t", "typo": 1}}
            )
        with self.assertRaises(KubernetesConfigError):
            KubernetesConfig.from_node_config(
                {"kubernetes": {"namespace": "ns", "task_pvc": "t", "job": {"x": 1}}}
            )


class TestValidateNodeConfig(unittest.TestCase):
    def test_valid(self):
        validate_node_config(
            {
                "databases": [
                    {"label": "a", "uri": "x.csv", "type": "csv"},
                    {
                        "label": "b",
                        "uri": "x.csv",
                        "type": "csv",
                        "mount_mode": "ro",
                        "volume": {"claim_name": "data"},
                    },
                ]
            }
        )

    def test_docker_only_features(self):
        for option in ("vpn_subnet", "ssh-tunnels", "whitelist", "docker_services"):
            with self.subTest(option=option):
                with self.assertRaises(KubernetesConfigError):
                    validate_node_config({option: "something"})

    def test_read_only_database_requires_volume(self):
        with self.assertRaises(KubernetesConfigError):
            validate_node_config(
                {
                    "databases": [
                        {
                            "label": "a",
                            "uri": "x.csv",
                            "type": "csv",
                            "mount_mode": "ro",
                        }
                    ]
                }
            )
