import unittest

from vantage6.node.k8s.config import KubernetesConfig
from vantage6.node.k8s.job_spec import AlgorithmJob, build_job


def _config(**kubernetes):
    return KubernetesConfig.from_node_config(
        {"kubernetes": {"namespace": "ns", "task_pvc": "tasks", **kubernetes}}
    )


def _job(**kwargs):
    defaults = dict(
        name="v6-abc-run-1",
        image="registry.example/algo:1@sha256:" + "a" * 64,
        labels={"vantage6.ai/run-id": "1"},
        annotations={"vantage6.ai/image": "registry.example/algo:1"},
        environment={"INPUT_FILE": "ENCODED", "PORT": 8080},
        run_folder="task-000000001",
        run_folder_mount_path="/mnt/data/task-000000001",
        tmp_folder="tmp/job-7",
        tmp_folder_mount_path="/mnt/tmp",
    )
    defaults.update(kwargs)
    return AlgorithmJob(**defaults)


class TestBuildJob(unittest.TestCase):
    def test_job(self):
        manifest = build_job(_job(), _config())
        self.assertEqual(manifest["kind"], "Job")
        self.assertEqual(manifest["metadata"]["name"], "v6-abc-run-1")
        self.assertEqual(manifest["metadata"]["namespace"], "ns")
        spec = manifest["spec"]
        self.assertEqual(spec["backoffLimit"], 0)
        self.assertEqual(spec["ttlSecondsAfterFinished"], 86400)
        self.assertNotIn("activeDeadlineSeconds", spec)
        self.assertEqual(
            spec["template"]["metadata"]["labels"], {"vantage6.ai/run-id": "1"}
        )

    def test_pod_is_locked_down(self):
        pod = build_job(_job(), _config())["spec"]["template"]["spec"]
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertFalse(pod["enableServiceLinks"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(
            pod["securityContext"]["seccompProfile"], {"type": "RuntimeDefault"}
        )
        for key in ("runAsUser", "runAsGroup", "fsGroup"):
            # OpenShift assigns these, so they are only set when configured
            self.assertNotIn(key, pod["securityContext"])
        for key in ("hostNetwork", "hostPID", "hostIPC", "nodeName"):
            self.assertNotIn(key, pod)
        container = pod["containers"][0]
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertFalse(container["securityContext"]["privileged"])
        self.assertEqual(
            container["securityContext"]["capabilities"], {"drop": ["ALL"]}
        )

    def test_container(self):
        container = build_job(_job(), _config())["spec"]["template"]["spec"][
            "containers"
        ][0]
        self.assertEqual(container["name"], "algorithm")
        self.assertTrue(container["image"].endswith("@sha256:" + "a" * 64))
        self.assertEqual(container["imagePullPolicy"], "Always")
        self.assertEqual(
            container["env"],
            [
                {"name": "INPUT_FILE", "value": "ENCODED"},
                {"name": "PORT", "value": "8080"},
            ],
        )
        self.assertNotIn("resources", container)

    def test_only_own_task_files_are_mounted(self):
        pod = build_job(_job(), _config())["spec"]["template"]["spec"]
        self.assertEqual(
            pod["volumes"],
            [{"name": "task-volume", "persistentVolumeClaim": {"claimName": "tasks"}}],
        )
        self.assertEqual(
            pod["containers"][0]["volumeMounts"],
            [
                {
                    "name": "task-volume",
                    "mountPath": "/mnt/data/task-000000001",
                    "subPath": "task-000000001",
                },
                {
                    "name": "task-volume",
                    "mountPath": "/mnt/tmp",
                    "subPath": "tmp/job-7",
                },
            ],
        )

    def test_configured_pod_settings(self):
        config = _config(
            algorithm_service_account="algorithms",
            image_pull_secrets=["registry"],
            job={"active_deadline_seconds": 3600},
            algorithm_pod={
                "resources": {"limits": {"memory": "1Gi"}},
                "node_selector": {"pool": "compute"},
                "tolerations": [{"key": "compute", "effect": "NoSchedule"}],
                "priority_class_name": "low",
                "security_context": {"runAsUser": 1000, "fsGroup": 1000},
                "read_only_root_filesystem": True,
                "labels": {"team": "x"},
            },
        )
        manifest = build_job(_job(gpu=True, requires_pull=False), config)
        self.assertEqual(manifest["spec"]["activeDeadlineSeconds"], 3600)
        template = manifest["spec"]["template"]
        self.assertEqual(template["metadata"]["labels"]["team"], "x")
        pod = template["spec"]
        self.assertEqual(pod["serviceAccountName"], "algorithms")
        self.assertEqual(pod["imagePullSecrets"], [{"name": "registry"}])
        self.assertEqual(pod["nodeSelector"], {"pool": "compute"})
        self.assertEqual(pod["priorityClassName"], "low")
        self.assertEqual(pod["securityContext"]["runAsUser"], 1000)
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        container = pod["containers"][0]
        self.assertEqual(container["imagePullPolicy"], "IfNotPresent")
        self.assertEqual(
            container["resources"], {"limits": {"memory": "1Gi", "nvidia.com/gpu": 1}}
        )
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertIn({"name": "tmp", "mountPath": "/tmp"}, container["volumeMounts"])
        # the configured resources are not modified
        self.assertEqual(config.algorithm_pod.resources, {"limits": {"memory": "1Gi"}})

    def test_co_locate_with_node(self):
        config = _config(
            node_pod_labels={"app.kubernetes.io/instance": "node"},
            algorithm_pod={
                "co_locate_with_node": True,
                "affinity": {"nodeAffinity": {"x": 1}},
            },
        )
        affinity = build_job(_job(), config)["spec"]["template"]["spec"]["affinity"]
        self.assertEqual(affinity["nodeAffinity"], {"x": 1})
        self.assertEqual(
            affinity["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"],
            [
                {
                    "labelSelector": {
                        "matchLabels": {"app.kubernetes.io/instance": "node"}
                    },
                    "topologyKey": "kubernetes.io/hostname",
                }
            ],
        )
        self.assertNotIn("podAffinity", config.algorithm_pod.affinity)

    def test_extra_volumes(self):
        volume = {"name": "database-0", "persistentVolumeClaim": {"claimName": "d"}}
        mount = {"name": "database-0", "mountPath": "/mnt/db", "readOnly": True}
        pod = build_job(_job(volumes=[volume], volume_mounts=[mount]), _config())[
            "spec"
        ]["template"]["spec"]
        self.assertIn(volume, pod["volumes"])
        self.assertIn(mount, pod["containers"][0]["volumeMounts"])
