"""Project Ceph in a box — topology stamp and workload credential helpers."""

from __future__ import annotations

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
        "osdCount",
        "replicateSize",
    ):
        if key in project_ceph and project_ceph[key]:
            out[key] = project_ceph[key]
    if project_ceph.get("secretName"):
        out.setdefault("troshka_project_ceph_secret", project_ceph["secretName"])
    return out


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
