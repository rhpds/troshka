"""Tests for main.py — startup configure() and module-level constants."""

import logging
import textwrap
from unittest.mock import MagicMock, patch


class TestCrdConstants:
    def test_crd_group(self):
        from helpers.k8s import CRD_GROUP

        assert CRD_GROUP == "troshka.redhat.com"

    def test_crd_version(self):
        from helpers.k8s import CRD_VERSION

        assert CRD_VERSION == "v1alpha1"


def _get_configure():
    """Build the configure function from main.py source without loading
    handler modules (which would pollute the kopf mock for other tests)."""
    # Read the raw source of main.py and exec only the configure function
    import os

    op_path = os.path.join(os.path.dirname(__file__), "..", "main.py")
    with open(op_path) as f:
        source = f.read()

    import sys

    # Build a minimal namespace with the constants, logger, and kopf mock
    ns = {
        "logging": logging,
        "kopf": sys.modules["kopf"],
        "CRD_GROUP": "troshka.redhat.com",
        "CRD_VERSION": "v1alpha1",
        "logger": logging.getLogger("troshka-operator"),
    }

    # Extract the configure function body (skip the decorator line)
    lines = source.splitlines()
    func_lines = []
    in_func = False
    for line in lines:
        if line.startswith("def configure("):
            in_func = True
            func_lines.append(line)
            continue
        if in_func:
            if line and not line[0].isspace() and not line.startswith("#"):
                break
            func_lines.append(line)

    exec("\n".join(func_lines), ns)
    return ns["configure"]


_configure = _get_configure()


class TestConfigureStartup:
    """Tests for the @kopf.on.startup() configure() function."""

    def _make_settings(self):
        settings = MagicMock()
        settings.posting.level = None
        settings.persistence.finalizer = None
        settings.execution.max_workers = None
        settings.batching.batch_window = None
        return settings

    @patch("kubernetes.client.BatchV1Api")
    @patch("kubernetes.client.CustomObjectsApi")
    def test_sets_operator_settings(self, mock_custom_cls, mock_batch_cls):
        mock_custom_cls.return_value.list_cluster_custom_object.return_value = {
            "items": []
        }

        settings = self._make_settings()
        _configure(settings=settings)

        assert settings.posting.level == logging.WARNING
        assert settings.persistence.finalizer == "troshka.redhat.com/finalizer"
        assert settings.execution.max_workers == 100
        assert settings.batching.batch_window == 0.5

    @patch("kubernetes.client.BatchV1Api")
    @patch("kubernetes.client.CustomObjectsApi")
    def test_no_projects_in_error_state(self, mock_custom_cls, mock_batch_cls):
        custom_api = mock_custom_cls.return_value
        batch_api = mock_batch_cls.return_value

        custom_api.list_cluster_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"namespace": "ns1", "name": "proj1"},
                    "status": {"phase": "Running"},
                }
            ]
        }

        settings = self._make_settings()
        _configure(settings=settings)

        batch_api.delete_namespaced_job.assert_not_called()
        custom_api.patch_namespaced_custom_object_status.assert_not_called()

    @patch("kubernetes.client.BatchV1Api")
    @patch("kubernetes.client.CustomObjectsApi")
    def test_error_state_with_recert_config(self, mock_custom_cls, mock_batch_cls):
        custom_api = mock_custom_cls.return_value
        batch_api = mock_batch_cls.return_value

        custom_api.list_cluster_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"namespace": "ns1", "name": "proj1"},
                    "status": {
                        "phase": "Error",
                        "recertConfig": {"rhcosPvc": "my-vm-disk-abc"},
                    },
                }
            ]
        }

        settings = self._make_settings()
        _configure(settings=settings)

        batch_api.delete_namespaced_job.assert_called_once_with(
            name="recert-my-vm",
            namespace="ns1",
            propagation_policy="Background",
        )
        custom_api.patch_namespaced_custom_object_status.assert_called_once()
        patch_body = custom_api.patch_namespaced_custom_object_status.call_args[1][
            "body"
        ]
        assert patch_body["status"]["phase"] == "Deploying"
        assert patch_body["status"]["recertAttempts"] == 0

    @patch("kubernetes.client.BatchV1Api")
    @patch("kubernetes.client.CustomObjectsApi")
    def test_error_state_without_recert_config_skips(
        self, mock_custom_cls, mock_batch_cls
    ):
        custom_api = mock_custom_cls.return_value
        batch_api = mock_batch_cls.return_value

        custom_api.list_cluster_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"namespace": "ns1", "name": "proj1"},
                    "status": {"phase": "Error"},
                }
            ]
        }

        settings = self._make_settings()
        _configure(settings=settings)

        batch_api.delete_namespaced_job.assert_not_called()
        custom_api.patch_namespaced_custom_object_status.assert_not_called()

    @patch("kubernetes.client.BatchV1Api")
    @patch("kubernetes.client.CustomObjectsApi")
    def test_list_projects_exception_handled(self, mock_custom_cls, mock_batch_cls):
        custom_api = mock_custom_cls.return_value
        custom_api.list_cluster_custom_object.side_effect = Exception("API down")

        settings = self._make_settings()
        # Should not raise
        _configure(settings=settings)

    @patch("kubernetes.client.BatchV1Api")
    @patch("kubernetes.client.CustomObjectsApi")
    def test_delete_job_failure_continues(self, mock_custom_cls, mock_batch_cls):
        custom_api = mock_custom_cls.return_value
        batch_api = mock_batch_cls.return_value

        custom_api.list_cluster_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"namespace": "ns1", "name": "proj1"},
                    "status": {
                        "phase": "Error",
                        "recertConfig": {"rhcosPvc": "vm-disk-1"},
                    },
                }
            ]
        }
        batch_api.delete_namespaced_job.side_effect = Exception("Job not found")

        settings = self._make_settings()
        _configure(settings=settings)

        # Patch status should still be called even though job delete failed
        custom_api.patch_namespaced_custom_object_status.assert_called_once()

    @patch("kubernetes.client.BatchV1Api")
    @patch("kubernetes.client.CustomObjectsApi")
    def test_recert_pvc_without_disk_separator(self, mock_custom_cls, mock_batch_cls):
        """When rhcosPvc has no '-disk-' separator, job_name uses 'vm' fallback."""
        custom_api = mock_custom_cls.return_value
        batch_api = mock_batch_cls.return_value

        custom_api.list_cluster_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"namespace": "ns1", "name": "proj1"},
                    "status": {
                        "phase": "Error",
                        "recertConfig": {"rhcosPvc": "some-pvc-name"},
                    },
                }
            ]
        }

        settings = self._make_settings()
        _configure(settings=settings)

        batch_api.delete_namespaced_job.assert_called_once_with(
            name="recert-vm",
            namespace="ns1",
            propagation_policy="Background",
        )
