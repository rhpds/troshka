"""Unit tests for auto_layout helpers extracted for Sonar S3776."""

from app.services.auto_layout import (
    _CL_CELL_H,
    _CL_CELL_W,
    _CL_COLS,
    _CL_HEADER_H,
    _CL_HEADER_MIN_W,
    _CL_PAD,
    _bmc_position_clear_of_clusters,
    _cluster_box_style,
    _cluster_boxes_for_ids,
    _cluster_member_ids,
    _cluster_reflow_entries,
    _cluster_style_box_rects,
    _free_network_ids,
    _grid_cluster_members,
    _invert_cluster_network_anchors,
    _partition_cluster_members,
    _partition_networks_by_placement,
    _place_unattached_storage,
    _position_anchored_network,
    _reflow_one_cluster,
    _reposition_one_bmc_network,
)


def test_partition_cluster_members_roles_and_unknown():
    members = [
        {"id": "w1", "data": {"name": "worker-1", "clusterRole": "worker"}},
        {"id": "cp0", "data": {"name": "cp-0", "clusterRole": "control-plane"}},
        {"id": "misc", "data": {"name": "other"}},
        {"id": "cp1", "data": {"name": "cp-1", "clusterRole": "control-plane"}},
    ]
    cps, workers = _partition_cluster_members(members)
    assert [m["id"] for m in cps] == ["cp0", "cp1", "misc"]
    assert [m["id"] for m in workers] == ["w1"]


def test_grid_cluster_members_relative_positions():
    cps = [
        {"id": "cp0", "data": {"name": "cp-0"}},
        {"id": "cp1", "data": {"name": "cp-1"}},
    ]
    workers = [{"id": "w0", "data": {"name": "worker-0"}}]
    _grid_cluster_members(cps, workers)
    assert cps[0]["position"] == {
        "x": _CL_PAD,
        "y": _CL_HEADER_H + _CL_PAD,
    }
    assert cps[1]["position"]["x"] == _CL_PAD + _CL_CELL_W
    # workers sit on the row after control-plane rows (1 row for 2 CPs)
    assert workers[0]["position"]["y"] == _CL_HEADER_H + _CL_PAD + _CL_CELL_H


def test_cluster_box_style_min_width_and_rows():
    cps = [{"id": f"cp{i}"} for i in range(5)]  # 2 CP rows
    workers = [{"id": f"w{i}"} for i in range(2)]
    style = _cluster_box_style(7, cps, workers)
    assert style["width"] == max(
        2 * _CL_PAD + min(_CL_COLS, 7) * _CL_CELL_W, _CL_HEADER_MIN_W
    )
    assert style["height"] == _CL_HEADER_H + _CL_PAD + 3 * _CL_CELL_H


def test_reflow_one_cluster_sizes_boundary():
    boundary = {"id": "c1", "type": "clusterNode", "position": {"x": 0, "y": 0}}
    members = [
        {
            "id": "cp0",
            "type": "vmNode",
            "parentId": "c1",
            "position": {"x": 100, "y": 200},
            "data": {"name": "cp-0", "clusterRole": "control-plane"},
        }
    ]
    _reflow_one_cluster(boundary, members)
    assert members[0]["position"]["x"] == _CL_PAD
    assert boundary["style"]["width"] >= _CL_HEADER_MIN_W
    assert boundary["style"]["height"] > 0


def test_cluster_reflow_entries_skips_empty():
    nodes = [
        {"id": "c1", "type": "clusterNode", "position": {"x": 0, "y": 0}},
        {
            "id": "vm1",
            "type": "vmNode",
            "parentId": "c1",
            "position": {"x": 50, "y": 10},
            "data": {"name": "cp-0"},
        },
        {"id": "c2", "type": "clusterNode", "position": {"x": 0, "y": 0}},
    ]
    entries = _cluster_reflow_entries(nodes)
    assert len(entries) == 1
    boundary, members, orig_x = entries[0]
    assert boundary["id"] == "c1"
    assert [m["id"] for m in members] == ["vm1"]
    assert orig_x == 50


def test_invert_and_position_anchored_network():
    anchors = {
        "net1": [{"cluster_id": "c1", "side": "top"}],
        "net2": [
            {"cluster_id": "c1", "side": "bottom"},
            {"cluster_id": "c2", "side": "bottom"},
        ],
    }
    inverted = _invert_cluster_network_anchors(anchors)
    assert inverted["net1"]["top"] == ["c1"]
    assert inverted["net2"]["bottom"] == ["c1", "c2"]

    nodes = [
        {
            "id": "c1",
            "type": "clusterNode",
            "position": {"x": 100, "y": 300},
            "style": {"width": 400, "height": 200},
        }
    ]
    boxes = _cluster_boxes_for_ids(nodes, ["c1", "missing"])
    assert boxes == [(100.0, 300.0, 500.0, 500.0)]

    net = {"id": "net1", "position": {"x": 0, "y": 0}}
    _position_anchored_network(net, boxes, "top", net_w=240, net_h=70, gap=80)
    assert net["position"]["y"] == 300 - 70 - 80
    assert net["position"]["x"] == 100 + (400 - 240) / 2


def test_bmc_clearance_and_reposition():
    box_rects = [(0.0, 100.0, 400.0, 500.0)]
    # Right-of-cluster candidate is clear.
    nx, ny = _bmc_position_clear_of_clusters(240, 70, 80, box_rects)
    assert nx == 400 + 80
    assert ny == 100 + (400 - 70) / 2

    # Overlapping BMC moves to clearance.
    n = {
        "id": "bmc",
        "type": "networkNode",
        "position": {"x": 50, "y": 200},
        "data": {"networkType": "bmc"},
    }
    _reposition_one_bmc_network(n, 240, 70, 80, box_rects, skip=set())
    assert n["position"]["x"] == nx
    assert n["position"]["y"] == ny

    # Skipped ids are left alone.
    n2 = {
        "id": "bmc2",
        "type": "networkNode",
        "position": {"x": 50, "y": 200},
        "data": {"networkType": "bmc"},
    }
    _reposition_one_bmc_network(n2, 240, 70, 80, box_rects, skip={"bmc2"})
    assert n2["position"] == {"x": 50, "y": 200}


def test_bmc_falls_below_when_right_still_overlaps():
    # Negative gap keeps the "to the right" candidate inside the box, forcing
    # the below-clusters fallback.
    box_rects = [(0.0, 0.0, 400.0, 100.0)]
    nx, ny = _bmc_position_clear_of_clusters(240, 70, -50, box_rects)
    assert nx == 0.0
    assert ny == 100.0 + (-50)


def test_cluster_style_box_rects_and_member_ids():
    nodes = [
        {
            "id": "c1",
            "type": "clusterNode",
            "position": {"x": 10, "y": 20},
            "style": {"width": 100, "height": 50},
        },
        {
            "id": "vm1",
            "type": "vmNode",
            "parentId": "c1",
            "data": {"name": "cp-0"},
        },
        {
            "id": "vm2",
            "type": "vmNode",
            "data": {"name": "extra", "clusterId": "ocp-1"},
        },
        {"id": "vm3", "type": "vmNode", "data": {"name": "free"}},
    ]
    assert _cluster_style_box_rects(nodes) == [(10, 20, 110, 70)]
    assert _cluster_member_ids(nodes) == {"vm1", "vm2"}


def test_partition_networks_by_placement():
    networks = [
        {"id": "bb"},
        {"id": "lnk"},
        {"id": "wide"},
        {"id": "bot_bb"},
        {"id": "ignored"},
    ]
    placements = {
        "bb": {"side": "top", "tier": "backbone"},
        "lnk": {"side": "top", "tier": "link"},
        "wide": {"side": "bottom", "tier": "wide"},
        "bot_bb": {"side": "bottom", "tier": "backbone"},
        "ignored": {"side": "top", "tier": "other"},
    }
    top_bb, top_links, bottom = _partition_networks_by_placement(networks, placements)
    assert [n["id"] for n in top_bb] == ["bb"]
    assert [n["id"] for n in top_links] == ["lnk"]
    assert [n["id"] for n in bottom] == ["wide", "bot_bb"]


def test_place_unattached_storage_and_free_network_ids():
    updated: dict = {}
    storage = [
        {"id": "s1"},
        {"id": "s2"},
        {"id": "attached"},
    ]
    _place_unattached_storage(storage, {"attached": "vm1"}, updated, 900.0, 170, 40)
    assert updated["s1"] == {"x": 40, "y": 900.0}
    assert updated["s2"] == {"x": 40 + 170 + 40, "y": 900.0}
    assert "attached" not in updated

    nodes = [
        {"id": "n1", "type": "networkNode"},
        {"id": "n2", "type": "networkNode"},
        {"id": "vm1", "type": "vmNode"},
    ]
    edges = [{"source": "n1", "target": "vm1"}]
    assert _free_network_ids(nodes, edges) == {"n2"}
