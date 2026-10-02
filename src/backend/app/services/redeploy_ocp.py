"""OCP rebuild-vs-recert helpers for project redeploy.

Redeploy of a project with OpenShift clusters must choose explicitly:

- ``rebuild`` — wipe install markers (and rely on full destroy to wipe disks)
  so the next deploy takes the fresh agent-install path.
- ``recert`` — keep disks + kubeconfig markers and re-run best-effort recert
  in place (prefer Save as Pattern for a reliable clone).
"""

from __future__ import annotations

import copy
from typing import Any

OCP_MODE_REBUILD = "rebuild"
OCP_MODE_RECERT = "recert"
VALID_OCP_MODES = frozenset({OCP_MODE_REBUILD, OCP_MODE_RECERT})

_VM_OCP_CRED_KEYS = (
    "ocpKubeconfig",
    "ocpKubeadminPassword",
    "recertEnabled",
    "guestfishCommands",
)
_CLUSTER_INSTALL_KEYS = (
    "ocpInstallStatus",
    "ocpInstallElapsed",
    "ocpInstallStartedAt",
)
_STORAGE_PATTERN_KEYS = ("source", "patternId", "patternDiskId")


def topology_has_ocp_clusters(topology: dict | None) -> bool:
    if not topology:
        return False
    if topology.get("clusters"):
        return True
    return any(n.get("type") == "clusterNode" for n in (topology.get("nodes") or []))


def all_ocp_clusters_ready(topology: dict | None) -> bool:
    """True only when every topology cluster reports ``ocpInstallStatus=ready``."""
    clusters = (topology or {}).get("clusters") or []
    if not clusters:
        return False
    return all(c.get("ocpInstallStatus") == "ready" for c in clusters)


def clear_topology_ocp_for_rebuild(topology: dict) -> None:
    """Strip markers that would send the next deploy down the recert path.

    Also clears pattern disk sources so rebuild cannot boot a leftover
    pattern-backed image ahead of a fresh agent ISO. Install status is set to
    ``monitoring`` (not deleted) so the canvas cannot keep showing Ready from a
    prior successful install while create-image/boot runs.
    """
    for cluster in topology.get("clusters") or []:
        for key in _CLUSTER_INSTALL_KEYS:
            cluster.pop(key, None)
        cluster["ocpInstallStatus"] = "monitoring"
    for node in topology.get("nodes") or []:
        data = node.get("data")
        if not isinstance(data, dict):
            continue
        ntype = node.get("type")
        if ntype == "vmNode":
            for key in _VM_OCP_CRED_KEYS:
                data.pop(key, None)
        elif ntype == "storageNode":
            for key in _STORAGE_PATTERN_KEYS:
                data.pop(key, None)


def apply_ocp_rebuild_to_project(project: Any) -> None:
    """Clear OCP ready/cred markers on both topologies and project columns."""
    for attr in ("topology", "deployed_topology"):
        topo = getattr(project, attr, None)
        if not topo:
            continue
        cleared = copy.deepcopy(topo)
        clear_topology_ocp_for_rebuild(cleared)
        setattr(project, attr, cleared)
    project.ocp_status = None
    project.ocp_status_detail = None
    project.ocp_install_elapsed = None
    project.ocp_monitor_started_at = None
    project.ocp_control_plane_usable_at = None
    project.ocp_control_plane_usable_elapsed = None


def run_inplace_ocp_recert(project_id: str) -> None:
    """Best-effort in-place OCP recert: recreate ops-pod, keep disks/VMs.

    Prefer Save as Pattern for a reliable clone — this path reuses the existing
    cluster disks and kubeconfig markers without wiping storage.
    """
    import logging

    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project
    from app.services.deploy_service import (
        _clear_deploy_cancelled,
        _deploy_ops_pod,
        _should_use_ops_pod,
    )

    logger = logging.getLogger(__name__)
    _clear_deploy_cancelled(project_id)
    s = SessionLocal()
    try:
        project = s.get(Project, project_id)
        if not project:
            return
        if not project.host_id:
            project.state = "error"
            project.deploy_error = "Cannot re-cert: project has no host"
            s.commit()
            return
        host = s.query(Host).filter_by(id=project.host_id).first()
        if not host or not host.ip_address:
            project.state = "error"
            project.deploy_error = "Cannot re-cert: host not reachable"
            s.commit()
            return
        topology = project.deployed_topology or project.topology or {}
        if not _should_use_ops_pod(topology):
            project.state = "error"
            project.deploy_error = (
                "Cannot re-cert in place: project is not ops-pod (bastionless) OCP"
            )
            s.commit()
            return
        project.ocp_status = "monitoring"
        project.deploy_error = None
        s.commit()
        try:
            _deploy_ops_pod(
                s,
                host,
                project_id,
                project,
                topology,
                project.vni_map or {},
                fresh_install_log=True,
            )
            project.state = "active"
            s.commit()
        except Exception as e:
            logger.exception("In-place OCP recert failed for %s", project_id[:8])
            project.state = "error"
            project.deploy_error = f"Re-cert failed: {e}"
            project.ocp_status = "error"
            s.commit()
    finally:
        s.close()
