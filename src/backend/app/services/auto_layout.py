"""Auto-layout engine for canvas topologies.

Positions nodes in a readable grid layout:
  Row 0: gateways
  Row 1: top networks (cluster, sriov, ptp, etc.)
  Row 2: VMs with disks to the left
  Row 3: bottom networks (BMC)
  Row 4: unattached storage
"""

_WORKLOAD_TYPES = ("vmNode", "containerNode")
_SHOWROOM_DISK_Y_OFFSET = 70


def _is_showroom_node(node: dict) -> bool:
    if node.get("type") != "containerNode":
        return False
    data = node.get("data", {})
    return bool(data.get("isShowroom") or data.get("name") == "showroom")


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
    vm_nodes = [n for n in nodes if n.get("type") == "vmNode"]
    container_nodes = [n for n in nodes if n.get("type") == "containerNode"]
    showroom_nodes = [n for n in container_nodes if _is_showroom_node(n)]
    workload_containers = [n for n in container_nodes if not _is_showroom_node(n)]
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


def _classify_network_by_handle(
    handle: str, net_id: str, top_ids: set, bottom_ids: set
):
    """Place a network ID into top or bottom set based on a workload edge handle."""
    if "top" in handle:
        top_ids.add(net_id)
    elif "bottom" in handle:
        bottom_ids.add(net_id)
    else:
        top_ids.add(net_id)


def _classify_networks_from_edges(
    nodes: list[dict], edges: list[dict]
) -> tuple[set[str], set[str]]:
    """Classify networks as top/bottom based on workload edge handle positions."""
    top_net_ids: set[str] = set()
    bottom_net_ids: set[str] = set()
    for e in edges:
        src = _find(nodes, e.get("source", ""))
        tgt = _find(nodes, e.get("target", ""))
        if not src or not tgt:
            continue
        src_type = src.get("type", "")
        tgt_type = tgt.get("type", "")
        if src_type in _WORKLOAD_TYPES and tgt_type == "networkNode":
            handle = (e.get("sourceHandle") or "").lower()
            _classify_network_by_handle(handle, tgt["id"], top_net_ids, bottom_net_ids)
        if tgt_type in _WORKLOAD_TYPES and src_type == "networkNode":
            handle = (e.get("targetHandle") or "").lower()
            _classify_network_by_handle(handle, src["id"], top_net_ids, bottom_net_ids)
    return top_net_ids, bottom_net_ids


def _classify_network_positions(
    nodes: list[dict], edges: list[dict], networks: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Determine which networks go on top vs bottom based on edge handles."""
    top_net_ids, bottom_net_ids = _classify_networks_from_edges(nodes, edges)

    for n in networks:
        if n.get("data", {}).get("networkType") == "bmc":
            top_net_ids.discard(n["id"])
            bottom_net_ids.add(n["id"])
    for n in networks:
        if n["id"] not in top_net_ids and n["id"] not in bottom_net_ids:
            top_net_ids.add(n["id"])

    top_nets = [n for n in networks if n["id"] in top_net_ids]
    bottom_nets = [n for n in networks if n["id"] in bottom_net_ids]
    return top_nets, bottom_nets


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


def _layout_infra_row(
    top_nets: list[dict],
    routers: list[dict],
    router_to_nets: dict[str, list[str]],
    vm_row_width: int,
    current_y: float,
    net_w: int,
    gap_x: int,
    net_h: int,
    gap_y: int,
) -> tuple[dict[str, dict], float]:
    """Layout top networks + routers. Returns (updates, new_y)."""
    updated: dict[str, dict] = {}
    if not top_nets and not routers:
        return updated, current_y

    placed_infra: set[str] = set()
    infra_items: list[dict] = []
    for net in top_nets:
        infra_items.append(net)
        placed_infra.add(net["id"])
        for r in routers:
            if r["id"] in placed_infra:
                continue
            if net["id"] in router_to_nets.get(r["id"], []):
                infra_items.append(r)
                placed_infra.add(r["id"])
    for r in routers:
        if r["id"] not in placed_infra:
            infra_items.append(r)

    infra_total = len(infra_items) * (net_w + gap_x) - gap_x
    infra_start_x = 40 + max(0, (vm_row_width - infra_total) / 2 + vm_row_width * 0.15)
    for i, n in enumerate(infra_items):
        updated[n["id"]] = {
            "x": infra_start_x + i * (net_w + gap_x),
            "y": current_y,
        }
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
    current_y: float,
    vm_w: int,
    vm_h: int,
    disk_w: int,
    disk_h: int,
    gap_x: int,
    disk_gap: int,
    gap_y: int,
) -> tuple[dict[str, dict], float, float]:
    """Layout VMs/containers with their disks. Returns (updates, cursor_x, new_y)."""
    updated: dict[str, dict] = {}
    vm_row_y = current_y
    cursor_x = 40.0
    max_vm_bottom = vm_row_y

    for vm in workload_nodes:
        disks = vm_to_storage.get(vm["id"], [])
        has_disk = len(disks) > 0

        if has_disk:
            disk_spacing = disk_h + 20
            for di, disk_id in enumerate(disks):
                updated[disk_id] = {
                    "x": cursor_x,
                    "y": vm_row_y + 20 + di * disk_spacing,
                }
            disks_bottom = vm_row_y + 20 + len(disks) * disk_spacing
            if disks_bottom > max_vm_bottom:
                max_vm_bottom = disks_bottom
            cursor_x += disk_w + disk_gap

        updated[vm["id"]] = {"x": cursor_x, "y": vm_row_y}
        vm_bottom = vm_row_y + vm_h
        if vm_bottom > max_vm_bottom:
            max_vm_bottom = vm_bottom

        cursor_x += vm_w + gap_x

    return updated, cursor_x, max_vm_bottom + gap_y


def _layout_bottom_nets(
    bottom_nets: list[dict],
    network_to_vms: dict[str, list[str]],
    already_placed: dict[str, dict],
    cursor_x: float,
    current_y: float,
    net_w: int,
    gap_x: int,
    net_h: int,
    gap_y: int,
) -> tuple[dict[str, dict], float]:
    """Layout bottom networks under connected VMs. Returns (updates, new_y)."""
    updated: dict[str, dict] = {}
    if not bottom_nets:
        return updated, current_y

    unplaced_bottom: list[dict] = []
    for n in bottom_nets:
        conn_vms = network_to_vms.get(n["id"], [])
        conn_vm_pos = [already_placed[vid] for vid in conn_vms if vid in already_placed]
        if conn_vm_pos:
            avg_x = sum(p["x"] for p in conn_vm_pos) / len(conn_vm_pos)
            updated[n["id"]] = {"x": avg_x, "y": current_y}
        else:
            unplaced_bottom.append(n)
    if unplaced_bottom:
        vm_area_width = cursor_x - 40
        net_total_width = len(unplaced_bottom) * (net_w + gap_x) - gap_x
        net_start_x = 40 + (vm_area_width - net_total_width) / 2
        for i, n in enumerate(unplaced_bottom):
            updated[n["id"]] = {
                "x": max(40, net_start_x + i * (net_w + gap_x)),
                "y": current_y,
            }
    return updated, current_y + net_h + gap_y


def _apply_positions(nodes: list[dict], updated: dict[str, dict]) -> list[dict]:
    """Apply computed positions to nodes, returning new list."""
    new_nodes = []
    for n in nodes:
        pos = updated.get(n["id"])
        if pos:
            new_nodes.append({**n, "position": pos})
        else:
            new_nodes.append(n)
    return new_nodes


def _fix_bottom_edges(
    edges: list[dict], nodes: list[dict], bottom_net_ids: set[str]
) -> list[dict]:
    """Fix edge handles so bottom-network edges connect from the bottom."""
    new_edges = []
    for e in edges:
        src = _find(nodes, e.get("source", ""))
        tgt = _find(nodes, e.get("target", ""))
        if not src or not tgt:
            new_edges.append(e)
            continue
        src_type = src.get("type", "")
        tgt_type = tgt.get("type", "")
        if (
            src_type == "networkNode"
            and tgt_type in _WORKLOAD_TYPES
            and src["id"] in bottom_net_ids
        ):
            handle = (e.get("targetHandle") or "").replace("-top", "-bottom")
            new_edges.append({**e, "sourceHandle": "top", "targetHandle": handle})
        elif (
            tgt_type == "networkNode"
            and src_type in _WORKLOAD_TYPES
            and tgt["id"] in bottom_net_ids
        ):
            handle = (e.get("sourceHandle") or "").replace("-top", "-bottom")
            new_edges.append({**e, "sourceHandle": handle, "targetHandle": "top"})
        else:
            new_edges.append(e)
    return new_edges


def auto_layout(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """Apply auto-layout to nodes/edges, return updated copies."""
    if not nodes:
        return nodes, edges

    classified = _classify_nodes(nodes)
    vm_to_storage, storage_to_vm, network_to_vms = _build_connection_maps(nodes, edges)
    top_nets, bottom_nets = _classify_network_positions(
        nodes, edges, classified["networks"]
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

    # Row 0: Gateways
    positions, current_y = _layout_gateways(
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

    # Row 1: Top networks + routers
    positions, current_y = _layout_infra_row(
        top_nets,
        classified["routers"],
        router_to_nets,
        vm_row_width,
        current_y,
        net_w,
        gap_x,
        net_h,
        gap_y,
    )
    updated.update(positions)

    # Row 2: VMs and containers with disks
    positions, cursor_x, current_y = _layout_workloads(
        classified["workload_nodes"],
        vm_to_storage,
        current_y,
        vm_w,
        vm_h,
        disk_w,
        disk_h,
        gap_x,
        disk_gap,
        gap_y,
    )
    updated.update(positions)

    # Row 3: Bottom networks
    positions, current_y = _layout_bottom_nets(
        bottom_nets,
        network_to_vms,
        updated,
        cursor_x,
        current_y,
        net_w,
        gap_x,
        net_h,
        gap_y,
    )
    updated.update(positions)

    # Row 4: Unattached storage
    unattached = [
        n for n in classified["storage_nodes"] if n["id"] not in storage_to_vm
    ]
    if unattached:
        for i, n in enumerate(unattached):
            updated[n["id"]] = {"x": 40 + i * (disk_w + gap_x), "y": current_y}

    new_nodes = _apply_positions(nodes, updated)
    new_edges = _fix_bottom_edges(edges, nodes, {n["id"] for n in bottom_nets})
    return new_nodes, new_edges


def _find(nodes: list[dict], node_id: str) -> dict | None:
    for n in nodes:
        if n["id"] == node_id:
            return n
    return None
