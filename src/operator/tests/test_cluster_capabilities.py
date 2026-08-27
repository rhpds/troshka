"""Tests for cluster capability discovery."""

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from helpers import cluster_capabilities as cc


def _api_error(status: int, body: str):
    exc = ApiException(status=status, reason="fail")
    exc.body = body
    return exc


class TestCollectKubevirtClusterCapabilities:
    @patch.object(cc, "_probe_video_models", return_value=["vga"])
    @patch.object(cc, "_probe_disk_buses", return_value=["virtio", "scsi", "sata", "usb"])
    @patch.object(cc, "is_video_config_enabled", return_value=True)
    @patch.object(cc, "_get_kubevirt_feature_gates", return_value=frozenset({"VideoConfig"}))
    def test_collect_merges_probe_results(
        self,
        _gates,
        _video_gate,
        _disk_probe,
        _video_probe,
    ):
        custom_api = MagicMock()
        doc = cc.collect_kubevirt_cluster_capabilities(custom_api, "troshka-operator")
        assert doc["kubevirt"]["diskBuses"] == ["virtio", "scsi", "sata", "usb"]
        assert doc["kubevirt"]["videoModels"] == ["vga"]
        assert "machineTypes" not in doc["kubevirt"]
        assert doc["kubevirt"]["videoConfigEnabled"] is True


class TestAdmissionAllows:
    def test_returns_true_on_dry_run_success(self):
        custom_api = MagicMock()
        assert cc._admission_allows(custom_api, "ns", {"kind": "VirtualMachine"}) is True

    def test_returns_false_on_admission_rejection(self):
        custom_api = MagicMock()
        custom_api.create_namespaced_custom_object.side_effect = _api_error(
            422,
            '{"details":{"causes":[{"message":"IDE bus is not supported"}]}}',
        )
        assert cc._admission_allows(custom_api, "ns", {"kind": "VirtualMachine"}) is False


class TestUpsertCapabilitiesConfigmap:
    def test_creates_when_missing(self):
        core_api = MagicMock()
        core_api.replace_namespaced_config_map.side_effect = _api_error(404, "missing")
        cc.upsert_capabilities_configmap(
            core_api,
            "troshka-operator",
            {"version": 1, "kubevirt": {"diskBuses": ["virtio"]}},
        )
        core_api.create_namespaced_config_map.assert_called_once()

    def test_replaces_when_present(self):
        core_api = MagicMock()
        cc.upsert_capabilities_configmap(
            core_api,
            "troshka-operator",
            {"version": 1, "kubevirt": {"diskBuses": ["virtio"]}},
        )
        core_api.replace_namespaced_config_map.assert_called_once()
        core_api.create_namespaced_config_map.assert_not_called()
