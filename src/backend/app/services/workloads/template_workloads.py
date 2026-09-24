"""Enqueue ordered template ``workloads:`` after OCP install completes."""

from __future__ import annotations

import logging

from app.core.database import SessionLocal
from app.services.workloads.run_service import start_workload_run

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = frozenset({"pending", "running", "queued"})


def _entry_role(entry) -> str | None:
    """Extract FQCN from a string or ``{name|role|role_fqcn}`` mapping."""
    if isinstance(entry, str) and entry.strip():
        return entry.strip()
    if isinstance(entry, dict):
        name = entry.get("name") or entry.get("role") or entry.get("role_fqcn")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def normalize_workload_roles(workloads) -> list[str]:
    """Accept string FQCNs or ``{name|role: fqcn}`` mappings; return FQCN list."""
    roles: list[str] = []
    for entry in workloads or []:
        role = _entry_role(entry)
        if role:
            roles.append(role)
    return roles


def run_once_roles(workloads) -> set[str]:
    """FQCNs marked ``runOnce: true`` in mapping-form workload entries."""
    out: set[str] = set()
    for entry in workloads or []:
        if not isinstance(entry, dict) or not entry.get("runOnce"):
            continue
        role = _entry_role(entry)
        if role:
            out.add(role)
    return out


def workloads_done_set(topology: dict | None) -> set[str]:
    """Roles already auto-completed (or stamped on pattern capture)."""
    raw = (topology or {}).get("workloadsDone") or []
    if not isinstance(raw, list):
        return set()
    return {str(x).strip() for x in raw if isinstance(x, str) and x.strip()}


def mark_run_once_roles_done(
    topology: dict, *, roles: set[str] | None = None
) -> set[str]:
    """Merge runOnce (or explicit) roles into ``topology['workloadsDone']``.

    Returns the set of roles added/considered for stamping.
    """
    to_mark = roles if roles is not None else run_once_roles(topology.get("workloads"))
    if not to_mark:
        return set()
    done = workloads_done_set(topology)
    done |= set(to_mark)
    topology["workloadsDone"] = sorted(done)
    return set(to_mark)


def next_auto_workload_role(
    workloads,
    *,
    succeeded: set[str],
    done: set[str],
) -> str | None:
    """Next role for auto-enqueue: not succeeded, and not runOnce-already-done."""
    once = run_once_roles(workloads)
    for role in normalize_workload_roles(workloads):
        if role in succeeded:
            continue
        if role in once and role in done:
            continue
        return role
    return None


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


def _all_ocp_clusters_ready(topology: dict | None) -> bool:
    """True when every OCP cluster reports ``ocpInstallStatus=ready``.

    No clusters, or no per-cluster statuses stamped yet → True (legacy /
    VM-adjacent topologies; project-level milestone / ``ocp_status`` still
    gate). Once any cluster has a status, **all** must be ``ready`` so
    multi-cluster workloads do not start when only the first CP is usable.
    """
    clusters = (topology or {}).get("clusters") or []
    if not clusters:
        return True
    if not any(c.get("ocpInstallStatus") for c in clusters):
        return True
    return all(c.get("ocpInstallStatus") == "ready" for c in clusters)


def _project_workload_ready(project) -> bool:
    """True when template workloads may auto-start (or be triggered via API)."""
    topo = (
        getattr(project, "deployed_topology", None)
        or getattr(project, "topology", None)
        or {}
    )
    if not _all_ocp_clusters_ready(topo):
        return False
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
        # Prefer editable workloads list for runOnce flags when deployed lost it.
        topo = {**topo, "workloads": editable.get("workloads")}
        if editable.get("workloadsDone") and not topo.get("workloadsDone"):
            topo = {**topo, "workloadsDone": editable.get("workloadsDone")}
    if not isinstance(req, dict):
        req = None
    return roles, req, topo


def stamp_run_once_role_done(project_id: str, role_fqcn: str) -> None:
    """After a successful auto run of a runOnce role, persist it on topology."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.project import Project

    if not role_fqcn:
        return
    db = SessionLocal()
    try:
        project = db.query(Project).filter_by(id=project_id).first()
        if not project:
            return
        once: set[str] = set()
        for attr in ("deployed_topology", "topology"):
            topo = getattr(project, attr, None)
            if isinstance(topo, dict):
                once |= run_once_roles(topo.get("workloads"))
        if role_fqcn not in once:
            return
        for attr in ("deployed_topology", "topology"):
            topo = getattr(project, attr, None)
            if not isinstance(topo, dict):
                continue
            mark_run_once_roles_done(topo, roles={role_fqcn})
            flag_modified(project, attr)
        db.commit()
    except Exception:
        logger.exception(
            "Failed to stamp runOnce done for %s / %s",
            project_id[:8],
            role_fqcn,
        )
        db.rollback()
    finally:
        db.close()


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
        next_role = next_auto_workload_role(
            topo.get("workloads"),
            succeeded=succeeded,
            done=workloads_done_set(topo),
        )
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
