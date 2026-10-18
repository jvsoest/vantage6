import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vantage6.node.k8s.config import KubernetesConfigError
from vantage6.node.k8s.databases import algorithm_volumes, setup_databases
from vantage6.node.runtime.base import TaskRuntime


class TestDatabases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks_dir = root / "tasks"
        self.mount_root = root / "databases"
        self.tasks_dir.mkdir()
        self.mount_root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def setup(self, *databases):
        return setup_databases(list(databases), self.tasks_dir, self.mount_root)

    def test_service_database(self):
        dbs = self.setup(
            {
                "label": "omop",
                "uri": "postgresql://db.example/omop",
                "type": "sql",
                "env": {"user": "me"},
            }
        )
        self.assertEqual(dbs["omop"]["uri"], "postgresql://db.example/omop")
        self.assertFalse(dbs["omop"]["is_file"])
        self.assertEqual(dbs["omop"]["env"], {"user": "me"})
        self.assertEqual(algorithm_volumes(dbs, "/mnt/data"), ([], []))

    def test_copied_file(self):
        source = Path(self.tmp.name) / "data.csv"
        source.write_text("a,b\n1,2\n")
        dbs = self.setup({"label": "default", "uri": str(source), "type": "csv"})

        copy = self.tasks_dir / "databases" / "data.csv"
        self.assertEqual(copy.read_text(), "a,b\n1,2\n")
        self.assertEqual(dbs["default"]["uri"], str(copy))
        self.assertTrue(dbs["default"]["is_file"])
        self.assertEqual(dbs["default"]["mount_mode"], "copy")
        self.assertEqual(
            algorithm_volumes(dbs, "/mnt/data"),
            (
                [],
                [
                    {
                        "name": "task-volume",
                        "mountPath": "/mnt/data/data.csv",
                        "subPath": "databases/data.csv",
                        "readOnly": True,
                    }
                ],
            ),
        )

    def test_copied_file_from_volume(self):
        (self.mount_root / "default").mkdir()
        (self.mount_root / "default" / "data.csv").write_text("a\n")
        dbs = self.setup(
            {
                "label": "default",
                "uri": "data.csv",
                "type": "csv",
                "volume": {"claim_name": "data"},
            }
        )
        self.assertTrue((self.tasks_dir / "databases" / "data.csv").is_file())
        self.assertTrue(dbs["default"]["is_file"])

    def test_copied_folder_not_supported(self):
        with self.assertRaises(KubernetesConfigError):
            self.setup({"label": "x", "uri": self.tmp.name, "type": "folder"})

    def test_read_only_file(self):
        (self.mount_root / "default" / "study").mkdir(parents=True)
        (self.mount_root / "default" / "study" / "data.csv").write_text("a\n")
        dbs = self.setup(
            {
                "label": "default",
                "uri": "study/data.csv",
                "type": "csv",
                "mount_mode": "ro",
                "volume": {"claim_name": "data", "sub_path": "project"},
            }
        )
        db = dbs["default"]
        self.assertTrue(db["is_file"])
        self.assertEqual(db["mount_target"], "/mnt/default.csv")
        # the node reads the file from its own mount (e.g. for column names)
        self.assertEqual(db["uri"], str(self.mount_root / "default" / "study/data.csv"))
        volumes, mounts = algorithm_volumes(dbs, "/mnt/data")
        self.assertEqual(
            volumes,
            [
                {
                    "name": "database-0",
                    "persistentVolumeClaim": {"claimName": "data", "readOnly": True},
                }
            ],
        )
        self.assertEqual(
            mounts,
            [
                {
                    "name": "database-0",
                    "mountPath": "/mnt/default.csv",
                    "subPath": "project/study/data.csv",
                    "readOnly": True,
                }
            ],
        )
        # nothing is copied
        self.assertFalse((self.tasks_dir / "databases").exists())

    def test_read_only_folder_not_mounted_in_node(self):
        dbs = self.setup(
            {
                "label": "images",
                "uri": "scans",
                "type": "folder",
                "mount_mode": "ro",
                "volume": {"claim_name": "data"},
            }
        )
        self.assertTrue(dbs["images"]["is_dir"])
        self.assertEqual(dbs["images"]["mount_target"], "/mnt/images")

    def test_volumes_are_shared_per_claim(self):
        dbs = self.setup(
            *[
                {
                    "label": label,
                    "uri": f"{label}.csv",
                    "type": "csv",
                    "mount_mode": "ro",
                    "volume": {"claim_name": "data"},
                }
                for label in ("a", "b")
            ]
        )
        volumes, mounts = algorithm_volumes(dbs, "/mnt/data")
        self.assertEqual(len(volumes), 1)
        self.assertEqual([m["name"] for m in mounts], ["database-0", "database-0"])

    def test_read_only_requires_relative_uri(self):
        with self.assertRaises(KubernetesConfigError):
            self.setup(
                {
                    "label": "a",
                    "uri": "/abs/a.csv",
                    "type": "csv",
                    "mount_mode": "ro",
                    "volume": {"claim_name": "data"},
                }
            )

    def test_invalid_mount_mode(self):
        with self.assertRaises(KubernetesConfigError):
            self.setup({"label": "a", "uri": "a", "type": "csv", "mount_mode": "rw"})

    def test_column_names(self):
        source = Path(self.tmp.name) / "data.csv"
        source.write_text("age,weight\n1,2\n")
        dbs = self.setup({"label": "default", "uri": str(source), "type": "csv"})
        runtime = SimpleNamespace(databases=dbs, log=logging.getLogger("test"))
        self.assertEqual(
            TaskRuntime.get_column_names(runtime, "default", "csv"), ["age", "weight"]
        )
