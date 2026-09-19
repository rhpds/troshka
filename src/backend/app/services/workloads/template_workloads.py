"""Enqueue ordered template ``workloads:`` after OCP install completes."""

from __future__ import annotations

import logging

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


def maybe_enqueue_template_workloads(project_id: str) -> str | None:
    """Start the next unfinished template workload role (idempotent).

    Returns the new run id, or None if nothing was started.
    """
    from app.core.database import SessionLocal
    from app.models.project import Project
    from app.models.workload_run import WorkloadRun
    from app.services.workloads.run_service import start_workload_run

    db = SessionLocal()
    try:
        project = db.query(Project).filter_by(id=project_id).first()
        if not project or project.state != "active":
            return None
        if project.ocp_control_plane_usable_at is None:
            return None

        topo = project.deployed_topology or project.topology or {}
        roles = normalize_workload_roles(topo.get("workloads"))
        if not roles:
            return None

        req = topo.get("requirements_content")
        if not isinstance(req, dict):
            req = None

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
        logger.info(
            "Project %s: enqueued template workload %s (run %s)",
            project_id[:8],
            next_role,
            run.id[:8],
        )
        return run.id
    except Exception:
        logger.exception("Failed to enqueue template workloads for %s", project_id[:8])
        return None
    finally:
        db.close()
