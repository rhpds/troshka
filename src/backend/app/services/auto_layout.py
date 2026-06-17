"""Auto-layout engine for canvas topologies.

Positions nodes in a readable grid layout:
  Row 0: gateways
  Row 1: top networks (cluster, sriov, ptp, etc.)
  Row 2: VMs with disks to the left
  Row 3: bottom networks (BMC)
  Row 4: unattached storage
"""


def auto_layout(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """Apply auto-layout to nodes/edges, return updated copies."""
    if not nodes:
        return nodes, edges

    updated: dict[str, dict] = {}

    # Classify nodes
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
    storage_nodes = [n for n in nodes if n.get("type") == "storageNode"]

    # Build connection maps
    vm_to_storage: dict[str, list[str]] = {}
    storage_to_vm: dict[str, str] = {}

    for e in edges:
        src = _find(nodes, e.get("source", ""))
        tgt = _find(nodes, e.get("target", ""))
        if not src or not tgt:
            continue
        if src.get("type") == "vmNode" and tgt.get("type") == "storageNode":
            vm_to_storage.setdefault(src["id"], []).append(tgt["id"])
            storage_to_vm[tgt["id"]] = src["id"]
        if tgt.get("type") == "vmNode" and src.get("type") == "storageNode":
            vm_to_storage.setdefault(tgt["id"], []).append(src["id"])
            storage_to_vm[src["id"]] = tgt["id"]

    # Build network connection maps
    network_to_vms: dict[str, list[str]] = {}
    for e in edges:
        src = _find(nodes, e.get("source", ""))
        tgt = _find(nodes, e.get("target", ""))
        if not src or not tgt:
            continue
        if src.get("type") == "vmNode" and tgt.get("type") == "networkNode":
            network_to_vms.setdefault(tgt["id"], []).append(src["id"])
        if tgt.get("type") == "vmNode" and src.get("type") == "networkNode":
            network_to_vms.setdefault(src["id"], []).append(tgt["id"])

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

    # Determine top vs bottom networks from edge handles
    top_net_ids: set[str] = set()
    bottom_net_ids: set[str] = set()
    for e in edges:
        src = _find(nodes, e.get("source", ""))
        tgt = _find(nodes, e.get("target", ""))
        if not src or not tgt:
            continue
        s_h = (e.get("sourceHandle") or "").lower()
        t_h = (e.get("targetHandle") or "").lower()
        if src.get("type") == "vmNode" and tgt.get("type") == "networkNode":
            if "top" in s_h:
                top_net_ids.add(tgt["id"])
            elif "bottom" in s_h:
                bottom_net_ids.add(tgt["id"])
            else:
                top_net_ids.add(tgt["id"])
        if tgt.get("type") == "vmNode" and src.get("type") == "networkNode":
            if "top" in t_h:
                top_net_ids.add(src["id"])
            elif "bottom" in t_h:
                bottom_net_ids.add(src["id"])
            else:
                top_net_ids.add(src["id"])

    # BMC networks always bottom
    for n in networks:
        if n.get("data", {}).get("networkType") == "bmc":
            top_net_ids.discard(n["id"])
            bottom_net_ids.add(n["id"])
    # Unconnected networks go to top
    for n in networks:
        if n["id"] not in top_net_ids and n["id"] not in bottom_net_ids:
            top_net_ids.add(n["id"])

    top_nets = [n for n in networks if n["id"] in top_net_ids]
    bottom_nets = [n for n in networks if n["id"] in bottom_net_ids]

    # --- Layout rows ---
    current_y = 40

    # Row 0: Gateways
    if gateways:
        gw_spacing = max(net_w + gap_x, disk_w + disk_gap + vm_w + gap_x)
        for i, n in enumerate(gateways):
            updated[n["id"]] = {"x": 40 + i * gw_spacing, "y": current_y}
        current_y += net_h + gap_y

    # Row 1: Top networks + routers
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

    placed_infra: set[str] = set()
    if top_nets or routers:
        net_x = 40
        for net in top_nets:
            updated[net["id"]] = {"x": net_x, "y": current_y}
            placed_infra.add(net["id"])
            net_x += net_w + gap_x
            for r in routers:
                if r["id"] in placed_infra:
                    continue
                conn_nets = router_to_nets.get(r["id"], [])
                if net["id"] in conn_nets:
                    updated[r["id"]] = {"x": net_x, "y": current_y}
                    placed_infra.add(r["id"])
                    net_x += net_w + gap_x
        for r in routers:
            if r["id"] not in placed_infra:
                updated[r["id"]] = {"x": net_x, "y": current_y}
                net_x += net_w + gap_x
        current_y += net_h + gap_y

    # Row 2: VMs with disks
    vm_row_y = current_y
    cursor_x = 40
    max_vm_bottom = vm_row_y

    for vm in vm_nodes:
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

    current_y = max_vm_bottom + gap_y

    # Row 3: Bottom networks — position under connected VMs when possible
    if bottom_nets:
        unplaced_bottom = []
        for n in bottom_nets:
            conn_vms = network_to_vms.get(n["id"], [])
            conn_vm_pos = [updated[vid] for vid in conn_vms if vid in updated]
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
        current_y += net_h + gap_y

    # Row 4: Unattached storage
    unattached = [n for n in storage_nodes if n["id"] not in storage_to_vm]
    if unattached:
        for i, n in enumerate(unattached):
            updated[n["id"]] = {"x": 40 + i * (disk_w + gap_x), "y": current_y}

    # Apply positions
    new_nodes = []
    for n in nodes:
        pos = updated.get(n["id"])
        if pos:
            new_nodes.append({**n, "position": pos})
        else:
            new_nodes.append(n)

    # Fix edge handles for bottom networks
    bottom_net_id_set = {n["id"] for n in bottom_nets}
    new_edges = []
    for e in edges:
        src = _find(nodes, e.get("source", ""))
        tgt = _find(nodes, e.get("target", ""))
        if not src or not tgt:
            new_edges.append(e)
            continue
        if (
            src.get("type") == "networkNode"
            and tgt.get("type") == "vmNode"
            and src["id"] in bottom_net_id_set
        ):
            handle = (e.get("targetHandle") or "").replace("-top", "-bottom")
            new_edges.append({**e, "sourceHandle": "top", "targetHandle": handle})
        elif (
            tgt.get("type") == "networkNode"
            and src.get("type") == "vmNode"
            and tgt["id"] in bottom_net_id_set
        ):
            handle = (e.get("sourceHandle") or "").replace("-top", "-bottom")
            new_edges.append({**e, "sourceHandle": handle, "targetHandle": "top"})
        else:
            new_edges.append(e)

    return new_nodes, new_edges


def _find(nodes: list[dict], node_id: str) -> dict | None:
    for n in nodes:
        if n["id"] == node_id:
            return n
    return None
