"""Tests for SNO OCP VM flag helpers."""

from app.services.ocp_topology_flags import (
    apply_sno_ocp_vm_flags,
    has_bastion_vm,
    rhcos_vms,
)

SNO_TOPOLOGY = {
    "nodes": [
        {
            "id": "bastion",
            "type": "vmNode",
            "data": {"name": "bastion", "os": "rhel"},
        },
        {
            "id": "cp0",
            "type": "vmNode",
            "data": {"name": "cp-0", "os": "rhcos"},
        },
    ]
}


def test_rhcos_vms_finds_control_plane():
    assert len(rhcos_vms(SNO_TOPOLOGY)) == 1
    assert rhcos_vms(SNO_TOPOLOGY)[0]["id"] == "cp0"


def test_has_bastion_vm():
    assert has_bastion_vm(SNO_TOPOLOGY) is True


def test_apply_sno_ocp_vm_flags_sets_defaults():
    topo = {
        "nodes": [
            {"id": "b", "type": "vmNode", "data": {"label": "bastion", "os": "rhel"}},
            {"id": "cp", "type": "vmNode", "data": {"name": "cp-0", "os": "rhcos"}},
        ]
    }
    apply_sno_ocp_vm_flags(topo, recert=True)
    data = topo["nodes"][1]["data"]
    assert data["recertEnabled"] is True
    assert data["ocpMonitor"] is True
    assert data["configureBastionBrowser"] is True


def test_apply_sno_ocp_vm_flags_respects_explicit_false():
    topo = {
        "nodes": [
            {
                "id": "cp",
                "type": "vmNode",
                "data": {"name": "cp-0", "os": "rhcos", "ocpMonitor": False},
            },
        ]
    }
    apply_sno_ocp_vm_flags(topo, recert=True)
    assert topo["nodes"][0]["data"]["ocpMonitor"] is False


def test_apply_sno_ocp_vm_flags_fills_null_only():
    topo = {
        "nodes": [
            {"id": "b", "type": "vmNode", "data": {"name": "bastion", "os": "rhel"}},
            {
                "id": "cp",
                "type": "vmNode",
                "data": {
                    "name": "cp-0",
                    "os": "rhcos",
                    "recertEnabled": None,
                    "ocpMonitor": None,
                    "configureBastionBrowser": None,
                },
            },
        ]
    }
    apply_sno_ocp_vm_flags(topo, recert=False)
    data = topo["nodes"][1]["data"]
    assert "recertEnabled" not in data or data.get("recertEnabled") is None
    assert data["ocpMonitor"] is True
    assert data["configureBastionBrowser"] is True


def test_apply_sno_ocp_vm_flags_skips_multi_node():
    topo = {
        "nodes": [
            {"id": "cp0", "type": "vmNode", "data": {"os": "rhcos"}},
            {"id": "cp1", "type": "vmNode", "data": {"os": "rhcos"}},
        ]
    }
    apply_sno_ocp_vm_flags(topo, recert=True)
    for node in topo["nodes"]:
        assert node["data"].get("ocpMonitor") is None
