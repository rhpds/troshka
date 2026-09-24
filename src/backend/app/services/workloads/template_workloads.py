"""Enqueue ordered template ``workloads:`` after OCP install completes."""

from __future__ import annotations

import logging

from app.core.database import SessionLocal
from app.services.workloads.run_service import start_workload_run

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = frozenset({"pending", "running", "queued"})


def normalize_workload_roles(workloads) -> list[str]:
    """Accept string FQCNs or ``{name|role: fqcn}`` mappings; return FQCN list."""
    roles: list[str] = []
    for entry in workloads or []:
        if isinstance(entry, str) and entry.strip():
            roles.append(entry.strip())
            continue
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("role") or entry.get("role_fqcn")
            if isinstance(name, str) and name.strip():
                roles.append(name.strip())
    return roles


def named_cluster_kubeconfigs(topology: dict) -> dict[str, str]:
    """Map cluster *name* → kubeconfig YAML from harvested control-plane members."""
    from app.services.deploy_service import _stored_cluster_creds

    id_to_name = {
        str(c.get("id")): str(c.get("name") or c.get("id") or "")
        for c in (topology.get("clusters") or [])
        if c.get("id")
    }
    out: dict[str, str] = {}
    for cid, (_pw, kc) in _stored_cluster_creds(topology).items():
        if not kc:
            continue
        name = id_to_name.get(cid) or cid
        if name:
            out[name] = kc
    return out


def _primary_cluster_id(topology: dict) -> str | None:
    """Prefer a cluster named ``source``, else the first cluster id."""
    clusters = topology.get("clusters") or []
    for c in clusters:
        if c.get("name") == "source" and c.get("id"):
            return str(c["id"])
    for c in clusters:
        if c.get("id"):
            return str(c["id"])
    return None


def _project_workload_ready(project) -> bool:
    """True when template workloads may auto-start (or be triggered via API)."""
    if getattr(project, "ocp_control_plane_usable_at", None) is not None:
        return True
    return getattr(project, "ocp_status", None) == "ready"


def resolve_template_workload_chain(project) -> tuple[list[str], dict | None, dict]:
    """Return ``(roles, requirements_content, topology_for_targeting)``.

    Prefers ``deployed_topology``; if it lost ``workloads`` (canvas wipe), falls
    back to editable ``topology``. Targeting clusters still come from deployed
    when present.
    """
    deployed = getattr(project, "deployed_topology", None) or {}
    editable = getattr(project, "topology", None) or {}
    topo = deployed or editable
    roles = normalize_workload_roles(topo.get("workloads"))
    req = topo.get("requirements_content")
    if not roles and editable and topo is not editable:
        roles = normalize_workload_roles(editable.get("workloads"))
        if not isinstance(req, dict):
            alt = editable.get("requirements_content")
            if isinstance(alt, dict):
                req = alt
    if not isinstance(req, dict):
        req = None
    return roles, req, topo


def maybe_enqueue_template_workloads(project_id: str) -> str | None:
    """Start the next unfinished template workload role (idempotent).

    Returns the new run id, or None if nothing was started.
    """
    from app.models.project import Project
    from app.models.workload_run import WorkloadRun

    db = SessionLocal()
    try:
        project = db.query(Project).filter_by(id=project_id).first()
        if not project or project.state != "active":
            return None
        if not _project_workload_ready(project):
            return None

        roles, req, topo = resolve_template_workload_chain(project)
        if not roles:
            return None

        # Don't pile on if a run is already in flight.
        inflight = (
            db.query(WorkloadRun)
            .filter(
                WorkloadRun.project_id == project_id,
                WorkloadRun.status.in_(tuple(_ACTIVE_STATUSES)),
            )
            .first()
        )
        if inflight:
            return None

        succeeded = {
            r.role_fqcn
            for r in db.query(WorkloadRun)
            .filter_by(project_id=project_id, status="succeeded")
            .all()
            if r.role_fqcn
        }
        next_role = next((role for role in roles if role not in succeeded), None)
        if not next_role:
            return None

        cluster_id = _primary_cluster_id(topo)
        target_map = {"mode": "cluster"}
        if cluster_id:
            target_map["cluster_id"] = cluster_id

        run = start_workload_run(
            db,
            project_id=project_id,
            kind="ad_hoc",
            role_fqcn=next_role,
            target_map=target_map,
            requirements_content=req,
            owner_id=project.owner_id,
        )
        run_id = getattr(run, "id", None) or ""
        logger.info(
            "Project %s: enqueued template workload %s (run %s)",
            project_id[:8],
            next_role,
            run_id[:8] or "?",
        )
        return run_id or None
    except Exception:
        logger.exception("Failed to enqueue template workloads for %s", project_id[:8])
        return None
    finally:
        db.close()
