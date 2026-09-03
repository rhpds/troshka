"""Auto-layout engine for canvas topologies.

Layout strategy (top to bottom):
  Row 0: showroom + gateway
  Row 1+: point-to-point link networks above VMs (stacked when overlapping)
  Row backbone: mgmt / cluster networks above VMs
  VM row: workloads with disks to the left
  Bottom link row: link networks that would cross or crowd the top
  Bottom wide row: lab, BMC, and other broadcast segments
  Last: unattached storage
"""

from __future__ import annotations

import re
from collections import defaultdict

_WORKLOAD_TYPES = ("vmNode", "containerNode")
_SHOWROOM_DISK_Y_OFFSET = 70

# Fixed row anchors tuned to match workshop-style canvases
_LINK_ROW_Y = 70
_LINK_ROW_STEP = 68
_BACKBONE_ROW_Y = 185
_VM_ROW_Y = 340
_TOP_ROW_CLEARANCE = 24
_LAB_ROW_GAP = 210  # below VM row bottom → lab y ≈ 780

# Cluster member grid — mirrors the frontend clusterMaterialize constants so a
# cluster laid out here matches a canvas-edited one.
_CL_CELL_W = 210
_CL_CELL_H = 240
_CL_PAD = 30
_CL_HEADER_H = 48
_CL_COLS = 4


def _is_showroom_node(node: dict) -> bool:
    if node.get("type") != "containerNode":
        return False
    data = node.get("data", {})
    return bool(data.get("isShowroom") or data.get("name") == "showroom")


def _node_name(node: dict) -> str:
    data = node.get("data", {})
    return str(data.get("name") or data.get("label") or "")


def _workload_sort_key(node: dict) -> tuple:
    """Order workloads left-to-right: control, vscode, rtr1..rtrN, then alpha."""
    name = _node_name(node).lower()
    if name == "control":
        return (0, 0, name)
    if name == "vscode":
        return (0, 1, name)
    match = re.match(r"rtr(\d+)", name)
    if match:
        return (1, int(match.group(1)), name)
    return (2, 0, name)


def _classify_nodes(nodes: list[dict]) -> dict[str, list[dict]]:
    """Classify nodes into networks, routers, gateways, workloads, and storage."""
    networks = [
        n
        for n in nodes
        if n.get("type") == "networkNode"
        and n.get("data", {}).get("subtype") == "network"
    ]
    routers = [
        n
        for n in nodes
        if n.get("type") == "networkNode"
        and n.get("data", {}).get("subtype") == "router"
    ]
    gateways = [
        n
        for n in nodes
        if n.get("type") == "networkNode"
        and n.get("data", {}).get("subtype") == "gateway"
    ]
    vm_nodes = sorted(
        [n for n in nodes if n.get("type") == "vmNode"], key=_workload_sort_key
    )
    container_nodes = [n for n in nodes if n.get("type") == "containerNode"]
    showroom_nodes = [n for n in container_nodes if _is_showroom_node(n)]
    workload_containers = sorted(
        [n for n in container_nodes if not _is_showroom_node(n)],
        key=_workload_sort_key,
    )
    storage_nodes = [n for n in nodes if n.get("type") == "storageNode"]

    return {
        "networks": networks,
        "routers": routers,
        "gateways": gateways,
        "showroom_nodes": showroom_nodes,
        "workload_nodes": vm_nodes + workload_containers,
        "storage_nodes": storage_nodes,
    }


def _build_storage_maps(
    nodes: list[dict], edges: list[dict]
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Build workload-to-storage and storage-to-workload adjacency maps."""
    vm_to_storage: dict[str, list[str]] = {}
    storage_to_vm: dict[str, str] = {}
    for e in edges:
        src = _find(nodes, e.get("source", ""))
        tgt = _find(nodes, e.get("target", ""))
        if not src or not tgt:
            continue
        src_type = src.get("type", "")
        tgt_type = tgt.get("type", "")
        if src_type in _WORKLOAD_TYPES and tgt_type == "storageNode":
            vm_to_storage.setdefault(src["id"], []).append(tgt["id"])
            storage_to_vm[tgt["id"]] = src["id"]
        if tgt_type in _WORKLOAD_TYPES and src_type == "storageNode":
            vm_to_storage.setdefault(tgt["id"], []).append(src["id"])
            storage_to_vm[src["id"]] = tgt["id"]
    return vm_to_storage, storage_to_vm


def _build_network_to_vms(nodes: list[dict], edges: list[dict]) -> dict[str, list[str]]:
    """Build network-to-workload adjacency map."""
    network_to_vms: dict[str, list[str]] = {}
    for e in edges:
        src = _find(nodes, e.get("source", ""))
        tgt = _find(nodes, e.get("target", ""))
        if not src or not tgt:
            continue
        src_type = src.get("type", "")
        tgt_type = tgt.get("type", "")
        if src_type in _WORKLOAD_TYPES and tgt_type == "networkNode":
            network_to_vms.setdefault(tgt["id"], []).append(src["id"])
        if tgt_type in _WORKLOAD_TYPES and src_type == "networkNode":
            network_to_vms.setdefault(src["id"], []).append(tgt["id"])
    return network_to_vms


def _build_connection_maps(
    nodes: list[dict], edges: list[dict]
) -> tuple[dict[str, list[str]], dict[str, str], dict[str, list[str]]]:
    """Build adjacency maps: vm_to_storage, storage_to_vm, network_to_vms."""
    vm_to_storage, storage_to_vm = _build_storage_maps(nodes, edges)
    network_to_vms = _build_network_to_vms(nodes, edges)
    return vm_to_storage, storage_to_vm, network_to_vms


def _network_side_from_vm_handle(vm_handle: str) -> str | None:
    """Map VM attachment handle to network row (top=above VMs, bottom=below)."""
    handle = (vm_handle or "").lower()
    if handle.endswith("-top") or handle == "top":
        return "top"
    if handle.endswith("-bottom") or handle == "bottom":
        return "bottom"
    return None


def _collect_network_side_votes(
    nodes: list[dict], edges: list[dict]
) -> dict[str, dict[str, int]]:
    """Tally above/below votes from VM NIC handles for each network."""
    votes: dict[str, dict[str, int]] = defaultdict(lambda: {"top": 0, "bottom": 0})
    for e in edges:
        src = _find(nodes, e.get("source", ""))
        tgt = _find(nodes, e.get("target", ""))
        if not src or not tgt:
            continue
        src_type = src.get("type", "")
        tgt_type = tgt.get("type", "")
        if src_type in _WORKLOAD_TYPES and tgt_type == "networkNode":
            side = _network_side_from_vm_handle(e.get("sourceHandle", ""))
            if side:
                votes[tgt["id"]][side] += 1
        if tgt_type in _WORKLOAD_TYPES and src_type == "networkNode":
            side = _network_side_from_vm_handle(e.get("targetHandle", ""))
            if side:
                votes[src["id"]][side] += 1
    return votes


def _is_link_network(net: dict) -> bool:
    """True for /30 point-to-point style segments between routers."""
    name = _node_name(net).lower()
    cidr = str(net.get("data", {}).get("cidr") or "")
    if name.startswith("link-"):
        return True
    return "/30" in cidr


def _is_backbone_network(net: dict) -> bool:
    """Broadcast / shared infrastructure networks that span multiple workloads."""
    name = _node_name(net).lower()
    if name in ("mgmt", "management", "cluster"):
        return True
    cidr = str(net.get("data", {}).get("cidr") or "")
    if re.search(r"/(2[0-4]|1[6-9]|[89]|[0-9])\b", cidr):
        return True
    return False


def _is_lab_network(net: dict) -> bool:
    name = _node_name(net).lower()
    return name in ("lab", "datacenter")


def _is_wide_bottom_network(net: dict, conn_vm_count: int) -> bool:
    """Lab-style segments that should sit below the VM row."""
    if net.get("data", {}).get("networkType") == "bmc":
        return True
    if _is_lab_network(net):
        return True
    return conn_vm_count >= 4


def _preferred_network_side(
    net: dict, votes: dict[str, dict[str, int]], conn_vm_count: int
) -> str:
    """Choose above (top) or below (bottom) VMs using name rules then NIC votes."""
    if _is_wide_bottom_network(net, conn_vm_count):
        return "bottom"
    if _is_backbone_network(net):
        return "top"
    if _is_link_network(net):
        return "top"

    net_votes = votes.get(net["id"], {})
    if net_votes.get("top", 0) > net_votes.get("bottom", 0):
        return "top"
    if net_votes.get("bottom", 0) > net_votes.get("top", 0):
        return "bottom"
    return "top"


def _classify_network_placements(
    networks: list[dict],
    votes: dict[str, dict[str, int]],
    network_to_vms: dict[str, list[str]],
    cluster_member_ids: set[str] | None = None,
) -> dict[str, dict[str, str]]:
    """Classify each network's vertical side and tier (link/backbone/wide)."""
    cluster_member_ids = cluster_member_ids or set()
    placements: dict[str, dict[str, str]] = {}
    for net in networks:
        conn_count = len(network_to_vms.get(net["id"], []))
        side = _preferred_network_side(net, votes, conn_count)
        # A network wired to OCP cluster members belongs ABOVE the cluster box
        # (top backbone), never in the wide-bottom row where it would land inside
        # the boundary. The BMC network is exempt — it correctly sits below.
        is_bmc = net.get("data", {}).get("networkType") == "bmc"
        connects_cluster = not is_bmc and any(
            vm in cluster_member_ids for vm in network_to_vms.get(net["id"], [])
        )
        if _is_link_network(net):
            tier = "link"
        elif connects_cluster:
            side = "top"
            tier = "backbone"
        elif _is_wide_bottom_network(net, conn_count):
            tier = "wide"
        else:
            tier = "backbone"
        placements[net["id"]] = {"side": side, "tier": tier}
    return placements


def _build_router_connections(
    nodes: list[dict], edges: list[dict]
) -> dict[str, list[str]]:
    """Build router/gateway to network adjacency map."""
    router_to_nets: dict[str, list[str]] = {}
    for e in edges:
        src = _find(nodes, e.get("source", ""))
        tgt = _find(nodes, e.get("target", ""))
        if not src or not tgt:
            continue
        src_sub = src.get("data", {}).get("subtype", "")
        tgt_sub = tgt.get("data", {}).get("subtype", "")
        if src.get("type") == "networkNode" and tgt.get("type") == "networkNode":
            if src_sub in ("router", "gateway"):
                router_to_nets.setdefault(src["id"], []).append(tgt["id"])
            if tgt_sub in ("router", "gateway"):
                router_to_nets.setdefault(tgt["id"], []).append(src["id"])
    return router_to_nets


def _calc_vm_row_width(
    workload_nodes: list[dict],
    vm_to_storage: dict[str, list[str]],
    vm_w: int,
    disk_w: int,
    disk_gap: int,
    gap_x: int,
) -> int:
    """Calculate the total width of the VM+disk row."""
    vm_row_width = 0
    for vm in workload_nodes:
        disks = vm_to_storage.get(vm["id"], [])
        if disks:
            vm_row_width += disk_w + disk_gap
        vm_row_width += vm_w + gap_x
    return max(vm_row_width, 400)


def _vm_center_x(vm_id: str, positions: dict[str, dict], vm_w: int) -> float | None:
    pos = positions.get(vm_id)
    if not pos:
        return None
    return float(pos["x"]) + vm_w / 2


def _net_x_centered_on_vms(
    net_id: str,
    network_to_vms: dict[str, list[str]],
    positions: dict[str, dict],
    net_w: int,
    vm_w: int,
) -> float:
    """Center a network pill on connected VM(s); span min..max for multi-VM links."""
    centers = [
        c
        for vm_id in network_to_vms.get(net_id, [])
        if (c := _vm_center_x(vm_id, positions, vm_w)) is not None
    ]
    if not centers:
        return 40.0
    if len(centers) >= 2:
        center = (min(centers) + max(centers)) / 2
    else:
        center = centers[0]
    return center - net_w / 2


def _net_x_interval(
    net_id: str,
    network_to_vms: dict[str, list[str]],
    positions: dict[str, dict],
    net_w: int,
    vm_w: int,
) -> tuple[float, float]:
    x = _net_x_centered_on_vms(net_id, network_to_vms, positions, net_w, vm_w)
    return (x, x + net_w)


def _intervals_overlap(
    left: tuple[float, float], right: tuple[float, float], margin: float = 16.0
) -> bool:
    return left[0] < right[1] - margin and right[0] < left[1] - margin


def _link_net_span_center(
    net_id: str,
    network_to_vms: dict[str, list[str]],
    positions: dict[str, dict],
    vm_w: int,
) -> float:
    centers = [
        c
        for vm_id in network_to_vms.get(net_id, [])
        if (c := _vm_center_x(vm_id, positions, vm_w)) is not None
    ]
    if len(centers) >= 2:
        return (min(centers) + max(centers)) / 2
    if centers:
        return centers[0]
    return 0.0


def _shared_vm_ids(
    net_a: str, net_b: str, network_to_vms: dict[str, list[str]]
) -> bool:
    return bool(set(network_to_vms.get(net_a, [])) & set(network_to_vms.get(net_b, [])))


def _assign_top_link_rows(
    link_nets: list[dict],
    network_to_vms: dict[str, list[str]],
    positions: dict[str, dict],
    net_w: int,
    vm_w: int,
) -> dict[str, float]:
    """Stack link networks above VMs; separate rows when sharing a VM or x-span."""
    if not link_nets:
        return {}

    sorted_nets = sorted(
        link_nets,
        key=lambda n: _link_net_span_center(n["id"], network_to_vms, positions, vm_w),
    )
    row_intervals: list[list[tuple[float, float]]] = []
    row_net_ids: list[list[str]] = []
    row_y: dict[str, float] = {}

    for net in sorted_nets:
        interval = _net_x_interval(net["id"], network_to_vms, positions, net_w, vm_w)
        placed = False
        for idx, intervals in enumerate(row_intervals):
            if any(_intervals_overlap(interval, existing) for existing in intervals):
                continue
            if any(
                _shared_vm_ids(net["id"], other_id, network_to_vms)
                for other_id in row_net_ids[idx]
            ):
                continue
            intervals.append(interval)
            row_net_ids[idx].append(net["id"])
            row_y[net["id"]] = _LINK_ROW_Y + idx * _LINK_ROW_STEP
            placed = True
            break
        if not placed:
            row_intervals.append([interval])
            row_net_ids.append([net["id"]])
            idx = len(row_intervals) - 1
            row_y[net["id"]] = _LINK_ROW_Y + idx * _LINK_ROW_STEP

    return row_y


def _compute_backbone_row_y(
    updated: dict[str, dict],
    showroom_nodes: list[dict],
    vm_to_storage: dict[str, list[str]],
    disk_h: int,
) -> float:
    """Keep backbone networks below showroom disks and other top-row obstacles."""
    min_y = float(_BACKBONE_ROW_Y)
    for showroom in showroom_nodes:
        for disk_id in vm_to_storage.get(showroom["id"], []):
            pos = updated.get(disk_id, {})
            bottom = float(pos.get("y", 0)) + disk_h
            min_y = max(min_y, bottom + _TOP_ROW_CLEARANCE)
    return min_y


def _layout_gateways(
    gateways: list[dict],
    vm_row_width: int,
    current_y: float,
    net_w: int,
    gap_x: int,
    net_h: int,
    gap_y: int,
) -> tuple[dict[str, dict], float]:
    """Layout gateway nodes centered above VM row. Returns (updates, new_y)."""
    updated: dict[str, dict] = {}
    if not gateways:
        return updated, current_y
    gw_total = len(gateways) * (net_w + gap_x) - gap_x
    gw_start_x = 40 + max(0, (vm_row_width - gw_total) / 2 - vm_row_width * 0.15)
    for i, n in enumerate(gateways):
        updated[n["id"]] = {"x": gw_start_x + i * (net_w + gap_x), "y": current_y}
    return updated, current_y + net_h + gap_y


def _layout_showroom_beside_gateway(
    showroom_nodes: list[dict],
    gateways: list[dict],
    gateway_positions: dict[str, dict],
    vm_to_storage: dict[str, list[str]],
    vm_w: int,
    disk_w: int,
    gap_x: int,
    disk_gap: int,
) -> dict[str, dict]:
    """Place showroom (and disk) on the gateway row, directly left of the gateway."""
    updated: dict[str, dict] = {}
    if not showroom_nodes or not gateways:
        return updated

    gw = gateways[0]
    gw_pos = gateway_positions.get(gw["id"]) or gw.get("position", {})
    gw_x = float(gw_pos.get("x", 150))
    gw_y = float(gw_pos.get("y", 40))

    showroom = showroom_nodes[0]
    showroom_x = gw_x - gap_x - vm_w
    updated[showroom["id"]] = {"x": showroom_x, "y": gw_y}

    disks = vm_to_storage.get(showroom["id"], [])
    for di, disk_id in enumerate(disks):
        updated[disk_id] = {
            "x": showroom_x - disk_w - disk_gap,
            "y": gw_y + _SHOWROOM_DISK_Y_OFFSET + di * 100,
        }
    return updated


def _layout_workloads(
    workload_nodes: list[dict],
    vm_to_storage: dict[str, list[str]],
    vm_row_y: float,
    vm_w: int,
    vm_h: int,
    disk_w: int,
    disk_h: int,
    gap_x: int,
    disk_gap: int,
) -> tuple[dict[str, dict], float, float]:
    """Layout VMs/containers with their disks. Returns (updates, cursor_x, row_bottom)."""
    updated: dict[str, dict] = {}
    cursor_x = 40.0
    max_vm_bottom = vm_row_y

    for vm in workload_nodes:
        disks = vm_to_storage.get(vm["id"], [])
        has_disk = len(disks) > 0

        if has_disk:
            disk_spacing = disk_h + 20
            disk_y = vm_row_y + max(20, (vm_h - disk_h) // 2)
            for di, disk_id in enumerate(disks):
                updated[disk_id] = {
                    "x": cursor_x,
                    "y": disk_y + di * disk_spacing,
                }
            disks_bottom = disk_y + len(disks) * disk_spacing
            max_vm_bottom = max(max_vm_bottom, disks_bottom)
            cursor_x += disk_w + disk_gap

        updated[vm["id"]] = {"x": cursor_x, "y": vm_row_y}
        max_vm_bottom = max(max_vm_bottom, vm_row_y + vm_h)
        cursor_x += vm_w + gap_x

    return updated, cursor_x, max_vm_bottom


def _layout_network_group(
    nets: list[dict],
    network_to_vms: dict[str, list[str]],
    positions: dict[str, dict],
    net_w: int,
    vm_w: int,
    y_by_id: dict[str, float] | None = None,
    default_y: float | None = None,
) -> dict[str, dict]:
    """Place networks centered on their connected VMs at a fixed or per-net Y."""
    updated: dict[str, dict] = {}
    for net in nets:
        y = (y_by_id or {}).get(
            net["id"], default_y if default_y is not None else _LINK_ROW_Y
        )
        updated[net["id"]] = {
            "x": _net_x_centered_on_vms(
                net["id"], network_to_vms, positions, net_w, vm_w
            ),
            "y": y,
        }
    return updated


def _layout_routers_near_nets(
    routers: list[dict],
    router_to_nets: dict[str, list[str]],
    net_positions: dict[str, dict],
) -> dict[str, dict]:
    """Place router nodes near their connected networks."""
    updated: dict[str, dict] = {}
    for router in routers:
        connected = router_to_nets.get(router["id"], [])
        net_positions_list = [
            net_positions[nid] for nid in connected if nid in net_positions
        ]
        if net_positions_list:
            avg_x = sum(p["x"] for p in net_positions_list) / len(net_positions_list)
            avg_y = sum(p["y"] for p in net_positions_list) / len(net_positions_list)
            updated[router["id"]] = {"x": avg_x, "y": avg_y}
        else:
            updated[router["id"]] = {"x": 40.0, "y": _BACKBONE_ROW_Y}
    return updated


def _layout_bottom_network_rows(
    bottom_wide_nets: list[dict],
    network_to_vms: dict[str, list[str]],
    positions: dict[str, dict],
    vm_row_bottom: float,
    vm_row_y: float,
    vm_h: int,
    net_w: int,
    vm_w: int,
    net_h: int,
    gap_y: int,
) -> tuple[dict[str, dict], float]:
    """Layout broadcast networks in a single row under VMs."""
    updated: dict[str, dict] = {}
    current_y = vm_row_bottom + gap_y
    lab_anchor_y = vm_row_y + vm_h + _LAB_ROW_GAP

    for net in bottom_wide_nets:
        y = lab_anchor_y if _is_lab_network(net) else current_y
        updated[net["id"]] = {
            "x": _net_x_centered_on_vms(
                net["id"], network_to_vms, positions, net_w, vm_w
            ),
            "y": y,
        }
        current_y = max(current_y, y + net_h + gap_y)

    return updated, current_y


def _apply_positions(nodes: list[dict], updated: dict[str, dict]) -> list[dict]:
    """Apply computed positions to nodes, clearing any prior layoutWidth."""
    new_nodes = []
    for n in nodes:
        new_n = dict(n)
        pos = updated.get(n["id"])
        if pos:
            new_n["position"] = pos
        if n.get("type") == "networkNode" and n.get("data", {}).get("layoutWidth"):
            data = dict(new_n.get("data", n.get("data", {})))
            data.pop("layoutWidth", None)
            new_n["data"] = data
        new_nodes.append(new_n)
    return new_nodes


def _set_nic_handle_side(handle: str, side: str) -> str:
    """Preserve NIC id in a handle, switching between -top and -bottom."""
    if not handle.startswith("nic-"):
        return handle
    if handle.endswith("-top"):
        base = handle[:-4]
    elif handle.endswith("-bottom"):
        base = handle[:-7]
    else:
        return handle
    return f"{base}-{side}"


def _parse_network_workload_edge(
    e: dict, src: dict, tgt: dict
) -> tuple[dict, dict, bool] | None:
    """Return (network, workload, network_is_source) for network↔workload edges."""
    src_type = src.get("type", "")
    tgt_type = tgt.get("type", "")
    src_sub = src.get("data", {}).get("subtype", "")
    tgt_sub = tgt.get("data", {}).get("subtype", "")
    if (
        src_type == "networkNode"
        and src_sub == "network"
        and tgt_type in _WORKLOAD_TYPES
    ):
        return src, tgt, True
    if (
        tgt_type == "networkNode"
        and tgt_sub == "network"
        and src_type in _WORKLOAD_TYPES
    ):
        return tgt, src, False
    return None


def _network_is_above_workload(
    net_id: str,
    workload_id: str,
    positions: dict[str, dict],
    net_h: int,
    workload_h: int,
) -> bool:
    net_pos = positions.get(net_id) or {}
    wl_pos = positions.get(workload_id) or {}
    net_cy = float(net_pos.get("y", 0)) + net_h / 2
    wl_cy = float(wl_pos.get("y", 0)) + workload_h / 2
    return net_cy < wl_cy


def _fix_network_edge_handles(
    edges: list[dict],
    nodes: list[dict],
    positions: dict[str, dict],
    net_h: int,
    workload_h: int,
) -> list[dict]:
    """Point network↔workload edges at the correct top/bottom handles."""
    new_edges = []
    for e in edges:
        src = _find(nodes, e.get("source", ""))
        tgt = _find(nodes, e.get("target", ""))
        if not src or not tgt:
            new_edges.append(e)
            continue

        parsed = _parse_network_workload_edge(e, src, tgt)
        if not parsed:
            new_edges.append(e)
            continue

        net_node, wl_node, net_is_source = parsed
        above = _network_is_above_workload(
            net_node["id"], wl_node["id"], positions, net_h, workload_h
        )

        if net_is_source:
            vm_handle = e.get("targetHandle", "")
            if above:
                new_edges.append(
                    {
                        **e,
                        "sourceHandle": "bottom",
                        "targetHandle": _set_nic_handle_side(vm_handle, "top"),
                    }
                )
            else:
                new_edges.append(
                    {
                        **e,
                        "sourceHandle": "top",
                        "targetHandle": _set_nic_handle_side(vm_handle, "bottom"),
                    }
                )
        else:
            vm_handle = e.get("sourceHandle", "")
            if above:
                new_edges.append(
                    {
                        **e,
                        "sourceHandle": _set_nic_handle_side(vm_handle, "top"),
                        "targetHandle": "bottom",
                    }
                )
            else:
                new_edges.append(
                    {
                        **e,
                        "sourceHandle": _set_nic_handle_side(vm_handle, "bottom"),
                        "targetHandle": "top",
                    }
                )
    return new_edges


def _lab_outer_vm_ids(
    lab_vm_ids: list[str],
    positions: dict[str, dict],
    vm_w: int,
) -> set[str]:
    """Leftmost and rightmost lab-connected VMs use bottom→bottom attachment."""
    if len(lab_vm_ids) <= 2:
        return set(lab_vm_ids)
    sorted_vms = sorted(
        lab_vm_ids,
        key=lambda vid: _vm_center_x(vid, positions, vm_w) or 0.0,
    )
    return {sorted_vms[0], sorted_vms[-1]}


def _vm_nic_handle_from_edge(e: dict, vm_id: str) -> str:
    """Preserve NIC id from an existing edge, defaulting to bottom attachment."""
    if e.get("source") == vm_id:
        handle = e.get("sourceHandle", "")
    else:
        handle = e.get("targetHandle", "")
    if handle.startswith("nic-"):
        return _set_nic_handle_side(handle, "bottom")
    return "nic-0-bottom"


def _fix_lab_edges(
    edges: list[dict],
    nodes: list[dict],
    positions: dict[str, dict],
    network_to_vms: dict[str, list[str]],
    vm_w: int,
) -> list[dict]:
    """Route lab edges: outer VMs bottom→lab bottom, inner VMs lab top→VM bottom."""
    lab_ids = {
        n["id"] for n in nodes if n.get("type") == "networkNode" and _is_lab_network(n)
    }
    if not lab_ids:
        return edges

    outer_by_lab = {
        lab_id: _lab_outer_vm_ids(network_to_vms.get(lab_id, []), positions, vm_w)
        for lab_id in lab_ids
    }

    result: list[dict] = []
    for e in edges:
        src_id = e.get("source", "")
        tgt_id = e.get("target", "")
        lab_id: str | None = None
        vm_id: str | None = None
        if src_id in lab_ids:
            wl = _find(nodes, tgt_id)
            if wl and wl.get("type") in _WORKLOAD_TYPES:
                lab_id, vm_id = src_id, tgt_id
        elif tgt_id in lab_ids:
            wl = _find(nodes, src_id)
            if wl and wl.get("type") in _WORKLOAD_TYPES:
                lab_id, vm_id = tgt_id, src_id
        if not lab_id or not vm_id:
            result.append(e)
            continue

        nic = _vm_nic_handle_from_edge(e, vm_id)
        if vm_id in outer_by_lab.get(lab_id, set()):
            result.append(
                {
                    **e,
                    "source": vm_id,
                    "target": lab_id,
                    "sourceHandle": nic,
                    "targetHandle": "bottom",
                }
            )
        else:
            result.append(
                {
                    **e,
                    "source": lab_id,
                    "target": vm_id,
                    "sourceHandle": "top",
                    "targetHandle": nic,
                }
            )
    return result


def _apply_edge_path_options(
    edges: list[dict],
    nodes: list[dict],
    positions: dict[str, dict],
    net_w: int,
    vm_w: int,
    net_h: int,
    vm_h: int,
) -> list[dict]:
    """Bias smoothstep bends so corridors avoid the disk row between VMs and networks."""
    new_edges: list[dict] = []
    for e in edges:
        src = _find(nodes, e.get("source", ""))
        tgt = _find(nodes, e.get("target", ""))
        if not src or not tgt:
            new_edges.append(e)
            continue

        parsed = _parse_network_workload_edge(e, src, tgt)
        if not parsed:
            new_edges.append(e)
            continue

        net_node, wl_node, _net_is_source = parsed
        vm_cx = _vm_center_x(wl_node["id"], positions, vm_w) or 0.0
        net_pos = positions.get(net_node["id"], {})
        net_cx = float(net_pos.get("x", 0)) + net_w / 2
        x_delta = abs(vm_cx - net_cx)

        if _is_link_network(net_node):
            offset = 72
        elif _is_lab_network(net_node):
            # Outer VM→lab edges need a wider bend to clear disk pills.
            vm_is_source = wl_node["id"] == e.get("source")
            if vm_is_source:
                offset = min(180, 72 + x_delta * 0.08)
            else:
                offset = min(140, 36 + x_delta * 0.05)
        else:
            offset = min(96, 28 + x_delta * 0.04)

        new_edges.append({**e, "pathOptions": {"offset": offset, "borderRadius": 6}})
    return new_edges


def _cluster_member_role(node: dict) -> str | None:
    d = node.get("data", {})
    role = d.get("clusterRole")
    if role in ("control-plane", "worker"):
        return role
    group = (d.get("tags") or {}).get("AnsibleGroup", "")
    if "controllers" in group:
        return "control-plane"
    if "workers" in group:
        return "worker"
    return None


def _cluster_member_index(node: dict) -> int:
    name = str(node.get("data", {}).get("name", ""))
    digits = ""
    for ch in reversed(name):
        if ch.isdigit():
            digits = ch + digits
        elif digits:
            break
    return int(digits) if digits else 0


def reflow_cluster_members(nodes: list[dict]) -> None:
    """Re-lay OCP cluster members inside their boundary and size the box.

    The core layout above is cluster-agnostic: it gives member VMs ABSOLUTE
    positions as if they were free workloads, but React Flow renders a child's
    position RELATIVE to its parent boundary — so a laid-out member floats
    outside the box. For each cluster boundary, anchor the boundary at the
    top-left of where its members landed, then grid the members inside it
    (control-plane rows first, then workers) with RELATIVE positions and size
    the boundary to contain them. Mirrors the frontend reflowMembers /
    clusterBoxSize and the template loader's original reflow.
    """
    boundaries = [n for n in nodes if n.get("type") == "clusterNode"]
    for boundary in boundaries:
        members = [
            n
            for n in nodes
            if n.get("type") == "vmNode" and n.get("parentId") == boundary["id"]
        ]
        if not members:
            continue
        # Anchor the boundary at the member row (keeps it near the network the
        # members connect to). The box TOP sits at the member row so its header
        # band occupies the top of the box and members render below it — this
        # preserves the network-row → VM-row gap instead of the box eating into
        # the network row above. X is inset by the left padding so member column
        # 0 lands where auto_layout placed it.
        min_x = min(m["position"]["x"] for m in members)
        min_y = min(m["position"]["y"] for m in members)
        boundary["position"] = {"x": min_x - _CL_PAD, "y": min_y}

        cps = sorted(
            [m for m in members if _cluster_member_role(m) == "control-plane"],
            key=_cluster_member_index,
        )
        workers = sorted(
            [m for m in members if _cluster_member_role(m) == "worker"],
            key=_cluster_member_index,
        )
        # Any member without a recognizable role still needs a slot.
        placed = set(id(m) for m in cps) | set(id(m) for m in workers)
        cps += [m for m in members if id(m) not in placed]

        cp_rows = (len(cps) + _CL_COLS - 1) // _CL_COLS if cps else 0
        for i, m in enumerate(cps):
            m["position"] = {
                "x": _CL_PAD + (i % _CL_COLS) * _CL_CELL_W,
                "y": _CL_HEADER_H + (i // _CL_COLS) * _CL_CELL_H,
            }
        for j, m in enumerate(workers):
            m["position"] = {
                "x": _CL_PAD + (j % _CL_COLS) * _CL_CELL_W,
                "y": _CL_HEADER_H + (cp_rows + j // _CL_COLS) * _CL_CELL_H,
            }
        cols = max(1, min(_CL_COLS, len(members)))
        worker_rows = (len(workers) + _CL_COLS - 1) // _CL_COLS if workers else 0
        rows = max(1, cp_rows + worker_rows)
        boundary["style"] = {
            "width": 2 * _CL_PAD + cols * _CL_CELL_W,
            "height": _CL_HEADER_H + _CL_PAD + rows * _CL_CELL_H,
        }


def auto_layout(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """Apply auto-layout to nodes/edges, return updated copies."""
    if not nodes:
        return nodes, edges

    classified = _classify_nodes(nodes)
    vm_to_storage, storage_to_vm, network_to_vms = _build_connection_maps(nodes, edges)
    side_votes = _collect_network_side_votes(nodes, edges)
    boundary_ids = {n["id"] for n in nodes if n.get("type") == "clusterNode"}
    cluster_member_ids = {
        n["id"]
        for n in nodes
        if n.get("type") == "vmNode"
        and (n.get("parentId") in boundary_ids or n.get("data", {}).get("clusterId"))
    }
    placements = _classify_network_placements(
        classified["networks"], side_votes, network_to_vms, cluster_member_ids
    )

    # Sizing constants (match frontend)
    net_w = 240
    net_h = 70
    vm_w = 200
    vm_h = 230
    disk_w = 170
    disk_h = 90
    gap_x = 40
    gap_y = 80
    disk_gap = 30

    router_to_nets = _build_router_connections(nodes, edges)
    vm_row_width = _calc_vm_row_width(
        classified["workload_nodes"], vm_to_storage, vm_w, disk_w, disk_gap, gap_x
    )

    updated: dict[str, dict] = {}
    current_y: float = 40

    positions, _current_y = _layout_gateways(
        classified["gateways"], vm_row_width, current_y, net_w, gap_x, net_h, gap_y
    )
    updated.update(positions)

    positions = _layout_showroom_beside_gateway(
        classified["showroom_nodes"],
        classified["gateways"],
        updated,
        vm_to_storage,
        vm_w,
        disk_w,
        gap_x,
        disk_gap,
    )
    updated.update(positions)

    positions, _cursor_x, vm_row_bottom = _layout_workloads(
        classified["workload_nodes"],
        vm_to_storage,
        _VM_ROW_Y,
        vm_w,
        vm_h,
        disk_w,
        disk_h,
        gap_x,
        disk_gap,
    )
    updated.update(positions)

    top_backbone = [
        n
        for n in classified["networks"]
        if placements[n["id"]]["side"] == "top"
        and placements[n["id"]]["tier"] == "backbone"
    ]
    top_links = [
        n
        for n in classified["networks"]
        if placements[n["id"]]["side"] == "top"
        and placements[n["id"]]["tier"] == "link"
    ]
    bottom_wide = [
        n
        for n in classified["networks"]
        if placements[n["id"]]["tier"] == "wide"
        or (
            placements[n["id"]]["side"] == "bottom"
            and placements[n["id"]]["tier"] == "backbone"
        )
    ]

    link_row_y = _assign_top_link_rows(top_links, network_to_vms, updated, net_w, vm_w)
    backbone_row_y = _compute_backbone_row_y(
        updated,
        classified["showroom_nodes"],
        vm_to_storage,
        disk_h,
    )
    updated.update(
        _layout_network_group(
            [n for n in top_links if n["id"] in link_row_y],
            network_to_vms,
            updated,
            net_w,
            vm_w,
            y_by_id=link_row_y,
        )
    )
    updated.update(
        _layout_network_group(
            top_backbone,
            network_to_vms,
            updated,
            net_w,
            vm_w,
            default_y=backbone_row_y,
        )
    )
    updated.update(
        _layout_routers_near_nets(classified["routers"], router_to_nets, updated)
    )

    positions, current_y = _layout_bottom_network_rows(
        bottom_wide,
        network_to_vms,
        updated,
        vm_row_bottom,
        _VM_ROW_Y,
        vm_h,
        net_w,
        vm_w,
        net_h,
        gap_y,
    )
    updated.update(positions)

    unattached = [
        n for n in classified["storage_nodes"] if n["id"] not in storage_to_vm
    ]
    if unattached:
        for i, n in enumerate(unattached):
            updated[n["id"]] = {"x": 40 + i * (disk_w + gap_x), "y": current_y}

    new_nodes = _apply_positions(nodes, updated)
    new_edges = _fix_network_edge_handles(edges, nodes, updated, net_h, vm_h)
    new_edges = _fix_lab_edges(new_edges, nodes, updated, network_to_vms, vm_w)
    new_edges = _apply_edge_path_options(
        new_edges, nodes, updated, net_w, vm_w, net_h, vm_h
    )
    # Cluster-aware pass: pull OCP cluster members back inside their boundary
    # (they were laid out above as free workloads) and size the box.
    reflow_cluster_members(new_nodes)
    return new_nodes, new_edges


def _find(nodes: list[dict], node_id: str) -> dict | None:
    for n in nodes:
        if n["id"] == node_id:
            return n
    return None
