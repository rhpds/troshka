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


# --- Pod (bastionless) projects: no bastion / RHCOS ocpMonitor VM node --------


def test_pod_project_has_no_bastion_vm():
    """A pod-install project defines its clusters in ``topology['clusters']`` and
    has no bastion VM node."""
    pod_topo = {
        "clusters": [{"id": "cl-0", "name": "c0"}],
        "nodes": [{"id": "net", "type": "networkNode", "data": {"name": "ocp-net"}}],
    }
    assert has_bastion_vm(pod_topo) is False


def test_apply_sno_ocp_vm_flags_pod_project_is_safe_noop():
    """Pod (bastionless) projects have no RHCOS VM node, so the SNO flag helper
    must be a safe no-op: it does not error and adds no monitor flags to any node
    (the ops-pod install monitor covers OCP status instead of a per-VM monitor)."""
    topo = {
        "clusters": [{"id": "cl-0"}],
        "nodes": [{"id": "net", "type": "networkNode", "data": {}}],
    }
    apply_sno_ocp_vm_flags(topo, recert=True)
    for node in topo["nodes"]:
        data = node.get("data", {})
        assert "ocpMonitor" not in data
        assert "configureBastionBrowser" not in data
        assert "recertEnabled" not in data


def test_apply_sno_ocp_vm_flags_no_bastion_skips_browser_flag():
    """A lone RHCOS VM without a bastion still gets ``ocpMonitor`` but NOT
    ``configureBastionBrowser`` (that flag assumes a bastion VM exists)."""
    topo = {
        "nodes": [
            {"id": "cp", "type": "vmNode", "data": {"name": "cp-0", "os": "rhcos"}},
        ]
    }
    apply_sno_ocp_vm_flags(topo, recert=False)
    data = topo["nodes"][0]["data"]
    assert data["ocpMonitor"] is True
    assert "configureBastionBrowser" not in data


def test_apply_cluster_ocp_flags_projects_onto_members():
    """Cluster-level flags project onto member VMs: recert -> control-plane
    members, monitor/bastion -> the monitor VM (first control plane)."""
    from app.services.ocp_topology_flags import apply_cluster_ocp_flags

    topo = {
        "clusters": [
            {
                "id": "ocp",
                "recert": True,
                "monitorHealth": True,
                "configureBastionBrowser": True,
            }
        ],
        "nodes": [
            {
                "id": "cp0",
                "type": "vmNode",
                "data": {"clusterId": "ocp", "clusterRole": "control-plane"},
            },
            {
                "id": "cp1",
                "type": "vmNode",
                "data": {"clusterId": "ocp", "clusterRole": "control-plane"},
            },
            {
                "id": "w0",
                "type": "vmNode",
                "data": {"clusterId": "ocp", "clusterRole": "worker"},
            },
        ],
    }
    changed = apply_cluster_ocp_flags(topo)
    assert changed is True
    cp0, cp1, w0 = (n["data"] for n in topo["nodes"])
    # recert on all control-plane members
    assert cp0["recertEnabled"] is True
    assert cp1["recertEnabled"] is True
    assert "recertEnabled" not in w0
    # monitor + bastion only on the first control-plane (monitor VM)
    assert cp0["ocpMonitor"] is True
    assert cp0["configureBastionBrowser"] is True
    assert "ocpMonitor" not in cp1
    assert "ocpMonitor" not in w0
    # idempotent
    assert apply_cluster_ocp_flags(topo) is False


def test_apply_cluster_ocp_flags_additive_and_scoped():
    """Only sets flags True (never clears); leaves non-member VMs and other
    clusters untouched."""
    from app.services.ocp_topology_flags import apply_cluster_ocp_flags

    topo = {
        "clusters": [{"id": "a", "monitorHealth": True}, {"id": "b"}],
        "nodes": [
            {
                "id": "a0",
                "type": "vmNode",
                "data": {"clusterId": "a", "clusterRole": "control-plane"},
            },
            {
                "id": "b0",
                "type": "vmNode",
                "data": {"clusterId": "b", "clusterRole": "control-plane"},
            },
            {"id": "loose", "type": "vmNode", "data": {"os": "rhcos"}},
        ],
    }
    assert apply_cluster_ocp_flags(topo) is True
    a0, b0, loose = (n["data"] for n in topo["nodes"])
    assert a0["ocpMonitor"] is True
    assert "ocpMonitor" not in b0  # cluster b has no flags
    assert "ocpMonitor" not in loose  # not a member


def test_apply_cluster_ocp_flags_no_clusters_noop():
    from app.services.ocp_topology_flags import apply_cluster_ocp_flags

    assert apply_cluster_ocp_flags({"nodes": []}) is False
