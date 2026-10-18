import os
from pathlib import Path

from vantage6.common.globals import PACKAGE_FOLDER, APPNAME

from vantage6.common.context import AppContext
from vantage6.common.configuration_manager import ConfigurationManager
from vantage6.cli.context.node import NodeContext
from vantage6.cli.globals import RUNTIME_KUBERNETES
from vantage6.node._version import __version__


class DockerNodeContext(NodeContext):
    """Node context for the dockerized version of the node."""

    running_in_docker = True

    def __init__(self, *args, **kwargs):
        """Display node version number."""
        super().__init__(*args, **kwargs)
        self.log.info(f"Node package version '{__version__}'")

    def set_folders(self, instance_type, instance_name, system_folders):
        """In case of the dockerized version we do not want to use user
        specified directories within the container.
        """
        dirs = self.instance_folders(instance_type, instance_name, system_folders)

        self.log_dir = dirs.get("log")
        self.data_dir = dirs.get("data")
        self.config_dir = dirs.get("config")
        self.vpn_dir = dirs.get("vpn")

    @staticmethod
    def instance_folders(instance_type, instance_name, system_folders):
        """Log, data and config folders are always mounted. The node manager
        should take care of this.
        """
        mnt = Path("/mnt")

        return {
            "log": mnt / "log",
            "data": mnt / "data",
            "config": mnt / "config",
            "vpn": mnt / "vpn",
        }


class KubernetesNodeContext(NodeContext):
    """
    Node context for the node running as a pod in a Kubernetes cluster.

    The node is started with the configuration file at an absolute path (e.g. a
    mounted secret). The folders in the pod are fixed and should be provided by the
    deployment (e.g. the Helm chart):

    - ``/mnt/data``: the (shared) task volume, which is also mounted in the
      algorithm pods
    - ``/mnt/log``: log files
    - ``/mnt/config``: configuration files
    """

    running_in_docker = False
    runtime = RUNTIME_KUBERNETES

    def __init__(self, *args, **kwargs):
        """Display node version number."""
        super().__init__(*args, **kwargs)
        self.log.info(f"Node package version '{__version__}'")
        self.log.info("Running algorithms as Kubernetes jobs")

    def set_folders(self, instance_type, instance_name, system_folders):
        """Use the fixed folders in the node pod"""
        dirs = self.instance_folders(instance_type, instance_name, system_folders)

        self.log_dir = dirs.get("log")
        self.data_dir = dirs.get("data")
        self.config_dir = dirs.get("config")

    @staticmethod
    def instance_folders(instance_type, instance_name, system_folders):
        """Folders that are mounted in the node pod"""
        mnt = Path(os.environ.get("V6_NODE_MOUNT_ROOT", "/mnt"))

        return {
            "log": mnt / "log",
            "data": mnt / "data",
            "config": mnt / "config",
        }


class TestingConfigurationManager(ConfigurationManager):
    VALIDATORS = {}


class TestContext(AppContext):
    INST_CONFIG_MANAGER = TestingConfigurationManager
    LOGGING_ENABLED = False

    @classmethod
    def from_external_config_file(cls, path):
        return super().from_external_config_file(
            cls.test_config_location(), "unittest", True
        )

    @staticmethod
    def test_config_location():
        return PACKAGE_FOLDER / APPNAME / "_data" / "unittest_config.yaml"

    @staticmethod
    def test_data_location():
        return PACKAGE_FOLDER / APPNAME / "_data"
