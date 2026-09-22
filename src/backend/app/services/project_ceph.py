"""Project Ceph in a box — topology stamp and workload credential helpers."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

TROSHKA_CEPH_CR_NAME = "project-ceph"
_DEFAULT_OSD_COUNT = 3
_MIN_OSD_COUNT = 1
_MAX_OSD_COUNT = 6
_MIN_OSD_SIZE_GI = 50

ODF_EXTERNAL_SECRET_NAME = (
    "rook-ceph-external-cluster-details"  # pragma: allowlist secret
)
TROSHKA_CEPH_SECRET_NAME = "troshka-ceph-external"  # pragma: allowlist secret


def build_project_ceph_stamp(
    *,
    namespace: str,
    status: dict,
    spec: dict | None = None,
) -> dict:
    """Build topology.projectCeph from a TroshkaCeph CR status/spec."""
    spec = spec or {}
    lab_ip = str(spec.get("labIp") or "").strip()
    mon_endpoint = str(status.get("monEndpoint") or "").strip()
    if not mon_endpoint and lab_ip:
        mon_endpoint = f"{lab_ip}:6789"
    return {
        "phase": status.get("phase", ""),
        "secretName": status.get("secretName") or TROSHKA_CEPH_SECRET_NAME,
        "secretNamespace": namespace,
        "monHost": lab_ip,
        "monEndpoint": mon_endpoint,
        "storageClassName": status.get("storageClassName")
        or spec.get("storageClassName")
        or "troshka-ceph-rbd",
        "poolName": status.get("poolName") or "troshka-ceph-pool",
        "osdCount": status.get("osdCount") or spec.get("osdCount") or 3,
        "replicateSize": status.get("replicateSize") or spec.get("replicateSize") or 3,
        "linkedClusters": list(spec.get("linkedClusterIds") or []),
        "troshka_ceph_secret_name": ODF_EXTERNAL_SECRET_NAME,
        "troshka_ceph_mon_host": lab_ip,
        "cclm_ceph_secret_name": ODF_EXTERNAL_SECRET_NAME,
    }


def merge_project_ceph_extra_vars(topology: dict, extra_vars: dict | None) -> dict:
    """Merge stamped projectCeph fields into workload extra_vars."""
    out = dict(extra_vars or {})
    project_ceph = topology.get("projectCeph") or {}
    if not project_ceph:
        return out
    for key in (
        "troshka_ceph_secret_name",
        "troshka_ceph_mon_host",
        "cclm_ceph_secret_name",
        "storageClassName",
        "monEndpoint",
        "secretName",
        "secretNamespace",
        "poolName",
        "osdCount",
        "replicateSize",
    ):
        if key in project_ceph and project_ceph[key]:
            out[key] = project_ceph[key]
    if project_ceph.get("secretName"):
        out.setdefault("troshka_project_ceph_secret", project_ceph["secretName"])
    return out


def _default_ceph_lab_ip(cidr: str) -> str:
    if not cidr or "/" not in cidr:
        return ""
    octets = cidr.split("/")[0].split(".")
    octets[3] = "4"
    return ".".join(octets)


def _network_nad_for_ref(nodes: list, network_ref: str) -> tuple[str, str]:
    for node in nodes:
        if node.get("type") != "networkNode":
            continue
        data = node.get("data") or {}
        if data.get("subtype") == "gateway":
            continue
        node_id = data.get("id", node.get("id", ""))
        if node_id != network_ref and node.get("id") != network_ref:
            continue
        return f"net-{str(node_id)[:8]}-nad", str(data.get("cidr") or "")
    return "", ""


def _linked_cluster_ids(nodes: list, edges: list, ceph_node_id: str) -> list[str]:
    linked: list[str] = []
    for edge in edges:
        src, tgt = edge.get("source", ""), edge.get("target", "")
        other = tgt if src == ceph_node_id else src if tgt == ceph_node_id else ""
        if not other:
            continue
        for node in nodes:
            if node.get("id") != other or node.get("type") != "clusterNode":
                continue
            data = node.get("data") or {}
            cluster_id = data.get("clusterId") or data.get("name") or ""
            if cluster_id:
                linked.append(str(cluster_id))
    return linked


def extract_ceph_cluster_spec(topology: dict) -> dict | None:
    """Return TroshkaCeph spec fields from a cephClusterNode, or None."""
    nodes = topology.get("nodes") or []
    edges = topology.get("edges") or []
    ceph_node = next((n for n in nodes if n.get("type") == "cephClusterNode"), None)
    if not ceph_node:
        return None

    data = ceph_node.get("data") or {}
    ceph_id = data.get("id", ceph_node.get("id", ""))
    network_ref = data.get("networkRef", "")
    network_nad, cidr = _network_nad_for_ref(nodes, str(network_ref))

    lab_ip = str(data.get("labIp") or "").strip() or _default_ceph_lab_ip(cidr)

    osd_count = int(data.get("osdCount") or _DEFAULT_OSD_COUNT)
    osd_count = max(_MIN_OSD_COUNT, min(_MAX_OSD_COUNT, osd_count))
    capacity_gi = int(data.get("capacityGi") or osd_count * _MIN_OSD_SIZE_GI)
    capacity_gi = max(capacity_gi, osd_count * _MIN_OSD_SIZE_GI)

    prefix = 24
    if cidr and "/" in cidr:
        prefix = int(cidr.split("/")[1])

    linked = list(data.get("linkedClusters") or [])
    if not linked:
        linked = _linked_cluster_ids(nodes, edges, ceph_node.get("id", ceph_id))

    # Ensure the ceph node carries stable, collision-free static OSD IPs. This
    # stamps data["osdIps"] in place (preserving any already allocated) so the
    # operator gets explicit addresses instead of self-assigning a fixed offset
    # that collides with node/VM NICs. See deploy_topology._auto_assign_ceph_osd_ips.
    from app.services.deploy_topology import _auto_assign_ceph_osd_ips

    _auto_assign_ceph_osd_ips(topology)
    osd_ips = list(data.get("osdIps") or [])

    return {
        "cephId": ceph_id,
        "networkNad": network_nad,
        "labIp": lab_ip,
        "labPrefixLength": prefix,
        "capacityGi": capacity_gi,
        "osdCount": osd_count,
        "osdIps": osd_ips,
        "replicateSize": min(osd_count, 3),
        "linkedClusterIds": linked,
        "storageClassName": data.get("storageClassName") or "troshka-ceph-rbd",
        "osdStorageClass": data.get("osdStorageClass") or "",
    }


def build_troshka_ceph_cr(
    *,
    namespace: str,
    project_id: str,
    spec: dict,
    owner_refs: list[dict],
) -> dict:
    return {
        "apiVersion": "troshka.redhat.com/v1alpha1",
        "kind": "TroshkaCeph",
        "metadata": {
            "name": TROSHKA_CEPH_CR_NAME,
            "namespace": namespace,
            "ownerReferences": owner_refs,
            "labels": {"troshka-project": f"project-{project_id[:8]}"},
        },
        "spec": spec,
    }


def topology_has_ceph(topology: dict | None) -> bool:
    """True when topology includes a cephClusterNode."""
    for node in (topology or {}).get("nodes") or []:
        if node.get("type") == "cephClusterNode":
            return True
    return False


def fetch_troshka_ceph_cr(custom_api, namespace: str) -> dict | None:
    """Read TroshkaCeph project-ceph CR from the project namespace."""
    try:
        return custom_api.get_namespaced_custom_object(
            group="troshka.redhat.com",
            version="v1alpha1",
            namespace=namespace,
            plural="troshkancephs",
            name="project-ceph",
        )
    except Exception:
        return None
