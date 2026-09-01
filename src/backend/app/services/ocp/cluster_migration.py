"""Lazy migration of legacy single-cluster OCP topology to ``clusters[]``.

Legacy persisted topology predates the multi-cluster model: it has RHCOS VM
nodes tagged with ``AnsibleGroup`` (``controllers``/``workers``) but no
``clusters`` key and no ``clusterNode`` boundary node. This module synthesizes
the missing multi-cluster structure on read so the rest of the stack can treat
old and new topology uniformly. It is a no-op for non-OCP topology and
idempotent once migrated.
"""

from app.services.template_loader import _CP_SIZE_DEFAULTS, _WORKER_SIZE_DEFAULTS

_CLUSTER_ID = "ocp"
_CLUSTER_NODE_ID = "cluster-ocp"
_BASE_DOMAIN = "ocp.local"


def _is_ocp_node(node: dict) -> bool:
    return node.get("data", {}).get("os") == "rhcos"


def _group_of(node: dict) -> str:
    return node.get("data", {}).get("tags", {}).get("AnsibleGroup", "")


def _infer_type(cp: int, wk: int) -> str:
    if cp <= 1 and wk == 0:
        return "sno"
    if wk == 0:
        return "compact"
    return "standard"


def _build_cluster_node(ctype: str, control_plane: int, workers: int) -> dict:
    """Build the ``clusterNode`` boundary node matching the generator's shape."""
    return {
        "id": _CLUSTER_NODE_ID,
        "type": "clusterNode",
        "position": {"x": 100, "y": 100},
        "data": {
            "name": _CLUSTER_ID,
            "type": ctype,
            "controlPlane": control_plane,
            "workers": workers,
            "baseDomain": _BASE_DOMAIN,
            "apiVip": None,
            "ingressVip": None,
        },
    }


def migrate_topology_clusters(topology: dict) -> dict:
    """Synthesize a one-element ``clusters[]`` + ``clusterNode`` for legacy topology.

    Idempotent: returns ``topology`` unchanged if ``clusters`` is already present
    or no RHCOS nodes exist. Otherwise mutates ``topology`` in place, stamping
    ``clusterId``/``parentNode`` on each RHCOS node, appending a real
    ``clusterNode`` to ``nodes``, and adding the ``clusters`` entry.
    """
    if not isinstance(topology, dict):
        return topology
    if topology.get("clusters"):
        return topology
    ocp_nodes = [n for n in topology.get("nodes", []) if _is_ocp_node(n)]
    if not ocp_nodes:
        return topology

    cp = sum(1 for n in ocp_nodes if "controllers" in _group_of(n))
    wk = sum(1 for n in ocp_nodes if "workers" in _group_of(n))
    ctype = _infer_type(cp, wk)
    control_plane = 1 if ctype == "sno" else 3

    for n in ocp_nodes:
        n.setdefault("data", {})["clusterId"] = _CLUSTER_ID
        n["parentNode"] = _CLUSTER_NODE_ID

    topology.setdefault("nodes", []).append(
        _build_cluster_node(ctype, control_plane, wk)
    )
    topology["clusters"] = [
        {
            "id": _CLUSTER_ID,
            "name": _CLUSTER_ID,
            "nodeId": _CLUSTER_NODE_ID,
            "type": ctype,
            "controlPlane": control_plane,
            "workers": wk,
            "controlPlaneCpu": _CP_SIZE_DEFAULTS["cpu"],
            "controlPlaneMemory": _CP_SIZE_DEFAULTS["memory"],
            "controlPlaneDisk": _CP_SIZE_DEFAULTS["disk"],
            "workerCpu": _WORKER_SIZE_DEFAULTS["cpu"],
            "workerMemory": _WORKER_SIZE_DEFAULTS["memory"],
            "workerDisk": _WORKER_SIZE_DEFAULTS["disk"],
            "baseDomain": _BASE_DOMAIN,
            "apiVip": None,
            "ingressVip": None,
            "ocpVersion": "4.20",
            "pullThroughRegistry": None,
        }
    ]
    return topology
