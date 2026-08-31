"""Unit tests for canvas auto-layout."""

from pathlib import Path

import yaml

from app.services.auto_layout import auto_layout
from app.services.template_loader import (
    generate_topology_from_template,
    resolve_inline_template,
)

_WORKSHOP_TEMPLATE = (
    Path(__file__).resolve().parents[3]
    / "example_templates"
    / "net-automation-workshop.yaml"
)


def _load_workshop_template() -> dict:
    return yaml.safe_load(_WORKSHOP_TEMPLATE.read_text())


def _pos(nodes, name):
    node = next(n for n in nodes if n.get("data", {}).get("name") == name)
    return node["position"]


def test_network_top_bottom_from_vm_nic_handle():
    """Network→VM edges with VM bottom NIC should classify network as top row."""
    nodes = [
        {
            "id": "vm1",
            "type": "vmNode",
            "position": {"x": 0, "y": 0},
            "data": {"name": "rtr1"},
        },
        {
            "id": "net1",
            "type": "networkNode",
            "position": {"x": 0, "y": 0},
            "data": {
                "name": "link-r1-r2",
                "subtype": "network",
                "cidr": "10.1.12.0/30",
            },
        },
        {
            "id": "net2",
            "type": "networkNode",
            "position": {"x": 0, "y": 0},
            "data": {"name": "lab", "subtype": "network"},
        },
    ]
    edges = [
        {
            "id": "e1",
            "source": "net1",
            "target": "vm1",
            "sourceHandle": "top",
            "targetHandle": "nic-abc-bottom",
        },
        {
            "id": "e2",
            "source": "net2",
            "target": "vm1",
            "sourceHandle": "top",
            "targetHandle": "nic-def-bottom",
        },
    ]
    laid_out, _ = auto_layout(nodes, edges)
    link = _pos(laid_out, "link-r1-r2")
    lab = _pos(laid_out, "lab")
    vm = _pos(laid_out, "rtr1")
    assert link["y"] < vm["y"]
    assert lab["y"] > vm["y"]


def test_workshop_layout_places_link_nets_above_vms():
    """Net-automation workshop: link-* above routers, lab below."""
    tmpl = _load_workshop_template()
    topo = generate_topology_from_template(resolve_inline_template(tmpl))
    nodes = topo["nodes"]

    rtr1 = _pos(nodes, "rtr1")
    rtr2 = _pos(nodes, "rtr2")
    link12 = _pos(nodes, "link-r1-r2")
    mgmt = _pos(nodes, "mgmt")
    lab = _pos(nodes, "lab")

    assert rtr1["x"] < rtr2["x"]
    assert link12["y"] < rtr1["y"]
    assert mgmt["y"] < rtr1["y"]
    assert lab["y"] > rtr1["y"]
    assert link12["x"] > rtr1["x"]
    assert link12["x"] < rtr2["x"]


def test_workshop_networks_keep_standard_pill_width():
    tmpl = _load_workshop_template()
    topo = generate_topology_from_template(resolve_inline_template(tmpl))
    lab = next(n for n in topo["nodes"] if n.get("data", {}).get("name") == "lab")
    mgmt = next(n for n in topo["nodes"] if n.get("data", {}).get("name") == "mgmt")
    assert "layoutWidth" not in lab["data"]
    assert "layoutWidth" not in mgmt["data"]


def test_workshop_mgmt_clears_showroom_disk():
    tmpl = _load_workshop_template()
    topo = generate_topology_from_template(resolve_inline_template(tmpl))
    nodes = topo["nodes"]
    mgmt = _pos(nodes, "mgmt")
    disk = _pos(nodes, "showroom-vol0")
    assert mgmt["y"] >= disk["y"] + 90 + 20


def test_workshop_layout_edge_handles_match_placement():
    """Edges above VMs attach to VM top handles; lab uses split bottom routing."""
    tmpl = _load_workshop_template()
    topo = generate_topology_from_template(resolve_inline_template(tmpl))
    nodes, edges = topo["nodes"], topo["edges"]
    name_by_id = {n["id"]: n.get("data", {}).get("name", "?") for n in nodes}
    pos = {n["id"]: n["position"] for n in nodes}

    lab_edges = [
        e
        for e in edges
        if name_by_id.get(e["source"]) == "lab" or name_by_id.get(e["target"]) == "lab"
    ]
    assert lab_edges, "expected lab edges in workshop topology"

    outer_names = {"control", "rtr4"}
    for e in lab_edges:
        src_name = name_by_id.get(e["source"])
        tgt_name = name_by_id.get(e["target"])
        if src_name in outer_names:
            assert tgt_name == "lab"
            assert e.get("sourceHandle", "").endswith("-bottom")
            assert e.get("targetHandle") == "bottom"
        elif tgt_name in outer_names:
            assert src_name == "lab"
            assert e.get("sourceHandle") == "top"
            assert e.get("targetHandle", "").endswith("-bottom")
        elif src_name == "lab":
            assert e.get("sourceHandle") == "top"
            assert e.get("targetHandle", "").endswith("-bottom")
        elif tgt_name == "lab":
            assert e.get("sourceHandle", "").endswith("-bottom")
            assert e.get("targetHandle") == "bottom"

    for e in edges:
        src_name = name_by_id.get(e["source"])
        if src_name not in ("mgmt",) and not str(src_name).startswith("link"):
            continue
        net_y = pos[e["source"]]["y"]
        vm_y = pos[e["target"]]["y"]
        tgt_handle = e.get("targetHandle", "")
        if net_y < vm_y:
            assert e.get("sourceHandle") == "bottom"
            assert tgt_handle.endswith("-top")
        else:
            assert e.get("sourceHandle") == "top"
            assert tgt_handle.endswith("-bottom")


def test_workshop_lab_position_and_outer_attachment():
    """Lab sits deep below VMs; outer routers attach via VM bottom→lab bottom."""
    tmpl = _load_workshop_template()
    topo = generate_topology_from_template(resolve_inline_template(tmpl))
    nodes, edges = topo["nodes"], topo["edges"]
    lab = _pos(nodes, "lab")
    rtr1 = _pos(nodes, "rtr1")

    assert lab["y"] >= 750
    assert lab["y"] > rtr1["y"] + 200

    name_by_id = {n["id"]: n.get("data", {}).get("name") for n in nodes}
    control_edge = next(
        e
        for e in edges
        if name_by_id[e["source"]] == "control" and name_by_id[e["target"]] == "lab"
    )
    assert control_edge["sourceHandle"].endswith("-bottom")
    assert control_edge["targetHandle"] == "bottom"


def test_auto_layout_is_idempotent_for_workshop():
    tmpl = _load_workshop_template()
    topo = generate_topology_from_template(resolve_inline_template(tmpl))
    nodes2, _ = auto_layout(topo["nodes"], topo["edges"])
    link12 = _pos(nodes2, "link-r1-r2")
    rtr1 = _pos(nodes2, "rtr1")
    assert link12["y"] < rtr1["y"]


def test_workload_order_control_vscode_routers():
    tmpl = _load_workshop_template()
    topo = generate_topology_from_template(resolve_inline_template(tmpl))
    nodes = topo["nodes"]
    vms = [n for n in nodes if n["type"] == "vmNode"]
    names = [n["data"]["name"] for n in sorted(vms, key=lambda n: n["position"]["x"])]
    assert names[:2] == ["control", "vscode"]
    assert names[2:] == ["rtr1", "rtr2", "rtr3", "rtr4"]
