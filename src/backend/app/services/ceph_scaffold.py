"""Scaffold a cephClusterNode from template YAML (KubeVirt projects)."""

from __future__ import annotations

import uuid

_DEFAULT_OSD_COUNT = 3
_MIN_OSD_COUNT = 1
_MAX_OSD_COUNT = 6
_MIN_OSD_SIZE_GI = 50


def _default_ceph_lab_ip(cidr: str) -> str:
    if not cidr or "/" not in cidr:
        return ""
    octets = cidr.split("/")[0].split(".")
    octets[3] = "3"
    return ".".join(octets)


def build_ceph_from_config(
    ceph_cfg: dict,
    *,
    net_ids: dict[str, str],
    nets_def: dict,
    cluster_name_to_id: dict[str, str],
    y: int = 250,
    x: int = 50,
) -> tuple[dict, list[dict]]:
    """Return (cephClusterNode, edges) from template ``cephCluster`` section."""
    network_name = str(ceph_cfg.get("network") or "cluster").strip()
    net_id = net_ids.get(network_name, network_name)
    cidr = str((nets_def.get(network_name) or {}).get("cidr") or "")

    osd_count = int(ceph_cfg.get("osdCount") or _DEFAULT_OSD_COUNT)
    osd_count = max(_MIN_OSD_COUNT, min(_MAX_OSD_COUNT, osd_count))
    capacity_gi = int(ceph_cfg.get("capacityGi") or osd_count * _MIN_OSD_SIZE_GI)
    capacity_gi = max(capacity_gi, osd_count * _MIN_OSD_SIZE_GI)

    lab_ip = str(ceph_cfg.get("labIp") or "").strip() or _default_ceph_lab_ip(cidr)
    node_id = f"ceph-{uuid.uuid4().hex[:8]}"
    name = str(ceph_cfg.get("name") or "Ceph Storage")

    node = {
        "id": node_id,
        "type": "cephClusterNode",
        "position": {"x": x, "y": y},
        "data": {
            "id": node_id,
            "label": name,
            "name": name,
            "networkRef": net_id,
            "labIp": lab_ip,
            "capacityGi": capacity_gi,
            "osdCount": osd_count,
            "linkedClusters": list(ceph_cfg.get("clusters") or []),
            "storageClassName": ceph_cfg.get("storageClassName") or "troshka-ceph-rbd",
        },
    }

    edges: list[dict] = []

    for idx, cluster_name in enumerate(ceph_cfg.get("clusters") or []):
        cluster_id = cluster_name_to_id.get(cluster_name)
        if not cluster_id:
            continue
        # Alternate sides: first cluster on ceph right, second on left, etc.
        if idx % 2 == 0:
            source_handle, target_handle = "right", "ceph-left"
        else:
            source_handle, target_handle = "left", "ceph-right"
        edges.append(
            {
                "id": f"edge-{node_id}-{cluster_id}",
                "source": node_id,
                "target": cluster_id,
                "sourceHandle": source_handle,
                "targetHandle": target_handle,
            }
        )

    return node, edges


def export_ceph_section(
    topology: dict, net_id_to_name: dict[str, str] | None = None
) -> dict | None:
    """Export cephCluster template section from a deployed topology."""
    net_id_to_name = net_id_to_name or {}
    for node in topology.get("nodes", []):
        if node.get("type") != "cephClusterNode":
            continue
        data = node.get("data", {})
        network_ref = str(data.get("networkRef") or "")
        return {
            "name": data.get("name", "Ceph Storage"),
            "network": net_id_to_name.get(network_ref, network_ref),
            "labIp": data.get("labIp", ""),
            "capacityGi": data.get("capacityGi", 300),
            "osdCount": data.get("osdCount", 3),
            "clusters": list(data.get("linkedClusters") or []),
            "storageClassName": data.get("storageClassName", "troshka-ceph-rbd"),
        }
    return None
