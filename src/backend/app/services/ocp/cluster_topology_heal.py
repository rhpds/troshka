"""Heal cluster topology drift (legacy migration ghosts, wrong member parentage)."""

from __future__ import annotations

import copy
import re

LEGACY_GHOST_CLUSTER_ID = "ocp"
LEGACY_GHOST_NODE_ID = "cluster-ocp"

_DEPLOY_ONLY_CLUSTER_FIELDS = frozenset(
    {
        "_generatedInstallConfig",
        "_generatedAgentConfig",
        "_generatedPullThroughItms",
        "controlPlaneDisks",
        "workerDisks",
    }
)

_MEMBER_ID_RE = re.compile(r"^(.+)-(cp|worker)-\d+$")


def _is_legacy_migration_ghost(cluster: dict) -> bool:
    return (
        cluster.get("id") == LEGACY_GHOST_CLUSTER_ID
        and cluster.get("nodeId") == LEGACY_GHOST_NODE_ID
        and cluster.get("baseDomain") in (None, "", "ocp.local")
    )


def _strip_deploy_only(cluster: dict) -> dict:
    out = {k: v for k, v in cluster.items() if k not in _DEPLOY_ONLY_CLUSTER_FIELDS}
    return out


def _infer_cluster_id_from_member_id(vm_id: str) -> str | None:
    m = _MEMBER_ID_RE.match(vm_id or "")
    return m.group(1) if m else None


def _clusters_are_legacy_ghost_only(clusters: list) -> bool:
    return bool(clusters) and all(_is_legacy_migration_ghost(c) for c in clusters)


def freeze_deployed_cluster_ocp_versions(current: dict, deployed: dict) -> None:
    """Revert ocpVersion on clusters already in deployed_topology.

    OCP version is fixed at install; canvas edits must not survive reconfigure.
    """
    dep_versions = {
        c["id"]: c.get("ocpVersion") or ""
        for c in (deployed.get("clusters") or [])
        if c.get("id")
    }
    if not dep_versions:
        return
    for cluster in current.get("clusters") or []:
        cid = cluster.get("id")
        if cid not in dep_versions:
            continue
        frozen = dep_versions[cid]
        if frozen:
            cluster["ocpVersion"] = frozen


def _reconcile_canvas_clusters(
    canvas_clusters: list,
    deployed_clusters: list,
    nodes: list,
) -> list:
    stripped_deployed = [_strip_deploy_only(c) for c in deployed_clusters]
    has_ghost = any(_is_legacy_migration_ghost(c) for c in canvas_clusters)
    base = [c for c in canvas_clusters if not _is_legacy_migration_ghost(c)]

    if (has_ghost or not base) and stripped_deployed:
        seen = {c.get("id") for c in base}
        for dc in stripped_deployed:
            if dc.get("id") not in seen:
                base.append(dc)
                seen.add(dc.get("id"))

    by_node = {c.get("nodeId"): c for c in base if c.get("nodeId")}
    for node in nodes:
        if node.get("type") != "clusterNode":
            continue
        if node.get("id") == LEGACY_GHOST_NODE_ID:
            continue
        data = node.get("data") or {}
        cid = data.get("clusterId")
        if not cid or node.get("id") in by_node or cid in {c.get("id") for c in base}:
            continue
        dep = next(
            (
                c
                for c in stripped_deployed
                if c.get("id") == cid or c.get("nodeId") == node.get("id")
            ),
            None,
        )
        entry = {
            "id": cid,
            "name": data.get("name") or (dep or {}).get("name") or cid,
            "nodeId": node.get("id"),
            "type": data.get("type") or (dep or {}).get("type") or "sno",
            "controlPlane": data.get("controlPlane")
            if data.get("controlPlane") is not None
            else (dep or {}).get("controlPlane", 1),
            "workers": data.get("workers")
            if data.get("workers") is not None
            else (dep or {}).get("workers", 0),
            "baseDomain": data.get("baseDomain")
            or (dep or {}).get("baseDomain")
            or "local",
            "apiVip": data.get("apiVip") or (dep or {}).get("apiVip"),
            "ingressVip": data.get("ingressVip") or (dep or {}).get("ingressVip"),
            "ocpVersion": (dep or {}).get("ocpVersion") or "",
            "networkIds": (dep or {}).get("networkIds") or data.get("networkIds") or [],
        }
        base.append(entry)

    for node in nodes:
        if node.get("type") != "clusterNode":
            continue
        data = node.get("data") or {}
        cid = data.get("clusterId")
        if not cid:
            continue
        cluster = next((c for c in base if c.get("id") == cid), None)
        if not cluster:
            continue
        if data.get("controlPlane") is not None:
            cluster["controlPlane"] = data["controlPlane"]
        if data.get("workers") is not None:
            cluster["workers"] = data["workers"]

    return [c for c in base if not _is_legacy_migration_ghost(c)]


def _resolve_member_cluster_id(
    node: dict, clusters: list, deployed_ids: set[str]
) -> str | None:
    data = node.get("data") or {}
    by_id = {c.get("id"): c for c in clusters if c.get("id")}
    cid = data.get("clusterId")
    inferred = _infer_cluster_id_from_member_id(node.get("id", ""))

    if inferred and inferred in by_id:
        return inferred

    if cid == LEGACY_GHOST_CLUSTER_ID and any(
        not _is_legacy_migration_ghost(c) for c in clusters
    ):
        name = str(data.get("name") or "")
        if re.match(r"^cp-\d+$", name):
            sno = [
                c
                for c in clusters
                if c.get("type") == "sno" and not _is_legacy_migration_ghost(c)
            ]
            deployed_sno = [c for c in sno if c.get("id") in deployed_ids]
            if len(deployed_sno) == 1:
                return deployed_sno[0].get("id")
            if len(sno) == 1:
                return sno[0].get("id")
        if inferred:
            return inferred

    if cid in by_id:
        return cid
    return inferred if inferred in by_id else None


def _heal_membership(nodes: list, clusters: list, deployed_clusters: list) -> None:
    by_id = {c.get("id"): c for c in clusters if c.get("id")}
    node_by_id = {n.get("id"): n for n in nodes}
    deployed_ids = {str(c.get("id")) for c in deployed_clusters if c.get("id")}

    for node in nodes:
        ntype = node.get("type")
        data = node.get("data") or {}

        if ntype == "vmNode" and (data.get("os") == "rhcos" or data.get("clusterId")):
            cid = _resolve_member_cluster_id(node, clusters, deployed_ids)
            cluster = by_id.get(cid) if cid else None
            if not cluster:
                continue
            node["parentId"] = cluster.get("nodeId")
            data["clusterId"] = cluster.get("id")
            continue

        if (
            ntype == "storageNode"
            and node.get("parentId") == LEGACY_GHOST_NODE_ID
            and any(not _is_legacy_migration_ghost(c) for c in clusters)
        ):
            owner_id = re.sub(r"-disk-\d+$", "", node.get("id", ""))
            owner = node_by_id.get(owner_id)
            if not owner:
                continue
            cid = _resolve_member_cluster_id(owner, clusters, deployed_ids)
            cluster = by_id.get(cid) if cid else None
            if cluster:
                node["parentId"] = cluster.get("nodeId")


def _order_parents_before_children(nodes: list) -> list:
    id_to_index = {n.get("id"): i for i, n in enumerate(nodes)}
    out = list(nodes)
    for node in nodes:
        parent_id = node.get("parentId")
        if not parent_id:
            continue
        child_id = node.get("id")
        pi = id_to_index.get(parent_id)
        ci = id_to_index.get(child_id)
        if pi is None or ci is None or ci > pi:
            continue
        child = out.pop(ci)
        # Rebuild index map after pop
        id_to_index = {n.get("id"): i for i, n in enumerate(out)}
        pi = id_to_index.get(parent_id)
        if pi is None:
            out.append(child)
        else:
            out.insert(pi + 1, child)
        id_to_index = {n.get("id"): i for i, n in enumerate(out)}
    return out


def heal_cluster_topology(
    topology: dict,
    *,
    deployed_clusters: list | None = None,
) -> dict:
    """Return a copy with clusters[], membership, and node order repaired."""
    topo = copy.deepcopy(topology or {})
    nodes = topo.get("nodes") or []
    deployed = deployed_clusters or []

    clusters = _reconcile_canvas_clusters(
        topo.get("clusters") or [],
        deployed,
        nodes,
    )
    if clusters:
        topo["clusters"] = clusters

    if clusters and any(not _is_legacy_migration_ghost(c) for c in clusters):
        nodes = [n for n in nodes if n.get("id") != LEGACY_GHOST_NODE_ID]

    _heal_membership(nodes, clusters, deployed)
    topo["nodes"] = _order_parents_before_children(nodes)
    return topo


def seed_topology_clusters_from_deployed(
    topology: dict, deployed_clusters: list
) -> dict:
    """Seed/heal canvas clusters from deployed_topology (no legacy ghost)."""
    return heal_cluster_topology(topology, deployed_clusters=deployed_clusters)
