"""Tests for kubevirt cluster capability reads."""

import json
from unittest.mock import MagicMock, patch

from kubernetes.client.exceptions import ApiException

from app.services.providers import kubevirt_capabilities as kc


class TestFetchClusterCapabilities:
    def test_reads_configmap(self):
        provider = MagicMock()
        provider.id = "prov-1"
        provider.get_credentials.return_value = {"namespace": "troshka-operator"}

        cm = MagicMock()
        cm.data = {
            "capabilities.json": json.dumps(
                {
                    "version": 1,
                    "kubevirt": {
                        "diskBuses": ["virtio", "scsi"],
                        "videoConfigEnabled": False,
                        "videoModels": [],
                        "machineTypes": ["q35"],
                    },
                }
            )
        }

        with patch(
            "app.services.providers.kubevirt._get_k8s_clients",
            return_value=(
                MagicMock(),
                MagicMock(read_namespaced_config_map=lambda *_a, **_k: cm),
                None,
            ),
        ):
            kc._cache.clear()
            doc = kc.fetch_cluster_capabilities(provider)

        assert doc is not None
        assert doc["kubevirt"]["diskBuses"] == ["virtio", "scsi"]

    def test_returns_none_when_configmap_missing(self):
        provider = MagicMock()
        provider.id = "prov-2"
        provider.get_credentials.return_value = {"namespace": "troshka-operator"}
        core_api = MagicMock()
        core_api.read_namespaced_config_map.side_effect = ApiException(
            status=404, reason="missing"
        )

        with patch(
            "app.services.providers.kubevirt._get_k8s_clients",
            return_value=(MagicMock(), core_api, None),
        ):
            kc._cache.clear()
            assert kc.fetch_cluster_capabilities(provider) is None


class TestValidateVmDiskBusesAgainstCapabilities:
    def test_rejects_unsupported_bus(self):
        topo = {
            "nodes": [
                {
                    "id": "disk-1",
                    "type": "storageNode",
                    "data": {"name": "disk0", "format": "qcow2"},
                },
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "name": "rtr3",
                        "diskControllers": [
                            {"id": "dp-1", "bus": "ide", "name": "disk0"},
                        ],
                    },
                },
            ],
            "edges": [
                {
                    "source": "disk-1",
                    "target": "vm-1",
                    "targetHandle": "dp-1-left",
                }
            ],
        }
        err = kc.validate_vm_disk_buses_against_capabilities(
            topo,
            ["vm-1"],
            {"diskBuses": ["virtio", "scsi"]},
        )
        assert err is not None
        assert "IDE" in err
