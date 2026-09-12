"""Orchestrate a workload run: create the record, resolve everything backend-side,
launch the runner pod, and track progress. Credential-safe (git creds + vault key
never leave the backend; see Plan 1)."""

from __future__ import annotations

import datetime

from app.core.database import SessionLocal
from app.core.redis import enqueue_job
from app.models.project import Project
from app.models.workload_run import WorkloadRun
from app.services.workloads.cluster_access import resolve_cluster_access
from app.services.workloads.inventory import (
    build_inventory_yaml,
    validate_ansible_groups,
)
from app.services.workloads.pod_launch import (
    RunPaths,
    build_artifact_files,
    build_run_command,
    launch_runner_pod,
)
from app.services.workloads.resolver import resolve_catalog_item
from app.services.workloads.run_key import mint_run_key
from app.workers.jobs import job_run_workload


def _now():
    return datetime.datetime.now(datetime.UTC)


def start_workload_run(
    db,
    *,
    project_id,
    kind,
    catalog_item=None,
    role_fqcn=None,
    target_map=None,
    owner_id=None,
) -> WorkloadRun:
    run = WorkloadRun(
        project_id=project_id,
        kind=kind,
        catalog_item=catalog_item,
        role_fqcn=role_fqcn,
        target_map=target_map,
        owner_id=owner_id,
        status="pending",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    enqueue_job(job_run_workload, run.id, project_id=project_id)
    return run


def _host_for_project(db, project):
    """Resolve the host for a project. Mirrors deploy_service._deploy_resolve_host."""
    from app.models.host import Host

    if not project.host_id:
        raise RuntimeError(f"Project {project.id} has no host assigned")
    host = db.query(Host).filter_by(id=project.host_id).first()
    if not host:
        raise RuntimeError(f"Host {project.host_id} not found for project {project.id}")
    return host


def _prepare_agnosticd(item) -> None:
    """Ensure agnosticd-v2 checkout + required collections are cached."""
    from app.core.database import get_db
    from app.services.workloads import repo_cache, secret_store

    # Get agnosticd-v2 git URL + credentials
    db = next(get_db())
    try:
        git_conf = secret_store.get_json_secret(db, "agnosticd_v2_git")
        if not git_conf or not git_conf.get("url"):
            raise RuntimeError("agnosticd_v2_git secret is not configured")

        # Authenticated URL for private repos
        git_url = git_conf["url"]
        token = git_conf.get("token")
        if token:
            from urllib.parse import urlsplit, urlunsplit

            parts = urlsplit(git_url)
            if parts.scheme in ("http", "https") and parts.hostname:
                netloc = f"x-access-token:{token}@{parts.hostname}"
                if parts.port:
                    netloc += f":{parts.port}"
                git_url = urlunsplit(
                    (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
                )

        # Ensure agnosticd-v2 checkout at item.scm_ref
        ref = item.scm_ref or repo_cache.default_ref("agnosticd-v2")
        repo_cache.ensure_repo("agnosticd-v2", git_url, ref)

        # Ensure workload collections from requirements_content
        if item.requirements_content:
            _ensure_collections(item.requirements_content)
    finally:
        db.close()


def _ensure_collections(requirements_content: dict) -> None:
    """Ensure workload collections named in requirements_content are cached.

    For now, this is a placeholder — collection caching is implemented in the
    runner pod via ansible-galaxy install. If we need pre-caching, add it here.
    """
    # Collections are installed in the runner pod via ANSIBLE_COLLECTIONS_PATH
    # and ansible-galaxy. No pre-caching needed yet.
    pass


def _has_ocp(project) -> bool:
    topo = project.deployed_topology or project.topology or {}
    return any(
        (n.get("data") or {}).get("ocpKubeconfig") for n in (topo.get("nodes") or [])
    )


def run_workload_job(run_id: str) -> None:
    db = SessionLocal()
    try:
        run = db.get(WorkloadRun, run_id)
        if not run:
            raise RuntimeError(f"WorkloadRun {run_id} not found")
        project = db.get(Project, run.project_id)
        if not project:
            raise RuntimeError(f"Project {run.project_id} not found")

        host = _host_for_project(db, project)
        item = _resolve_item(db, run)
        _prepare_agnosticd(item)
        key = mint_run_key(db, project)
        topo = project.deployed_topology or project.topology or {}
        validate_ansible_groups(topo, require_bastion=(not _has_ocp(project)))

        from app.core.config import config

        inv = build_inventory_yaml(config.app.external_url, key, project.id)
        cluster_access = resolve_cluster_access(project) if _has_ocp(project) else {}

        paths = RunPaths()
        extra_vars = dict(item.extra_vars)
        extra_vars["clusters"] = cluster_access

        # Thread requirements_content to the pod (HARD REQUIREMENT A)
        if item.requirements_content:
            extra_vars["requirements_content"] = item.requirements_content

        files = build_artifact_files(
            extra_vars=extra_vars,
            inventory_yaml=inv,
            cluster_access=cluster_access,
            cloud_creds=None,
            paths=paths,
        )

        command = build_run_command(item, paths)

        # Wire KubeVirt project-network attachment (HARD REQUIREMENT B)
        networks = _resolve_pod_networks(host, project, topo)

        launch_runner_pod(
            host,
            project,
            ee_image=item.ee_image or "quay.io/redhat-gpte/troshka-runner:latest",
            command=command,
            files=files,
            networks=networks,
        )
        run.status = "running"
        run.started_at = _now()
        db.commit()
        _start_workload_monitor(host, run.id)
    except Exception as exc:  # noqa: BLE001 — record and surface
        _fail_run(db, run_id, str(exc))
        raise
    finally:
        db.close()


def _resolve_item(db, run):
    if run.kind == "catalog_item":
        return resolve_catalog_item(db, run.catalog_item)
    return _synthesize_ad_hoc(db, run)


def _synthesize_ad_hoc(db, run):
    """Build a ResolvedItem-like object for an ad-hoc single role.

    Infers the collection from the role FQCN (namespace.collection.role) and
    builds a minimal extra_vars + requirements_content.
    """
    from types import SimpleNamespace

    if not run.role_fqcn:
        raise RuntimeError("Ad-hoc run requires role_fqcn")

    # Parse FQCN: namespace.collection.role → collection namespace.collection
    parts = run.role_fqcn.split(".")
    if len(parts) < 3:
        raise RuntimeError(
            f"Invalid role FQCN: {run.role_fqcn} (expected namespace.collection.role)"
        )

    namespace = parts[0]
    collection_name = parts[1]
    collection_fqcn = f"{namespace}.{collection_name}"

    # Build minimal extra_vars with workloads: [role_fqcn]
    extra_vars = {
        "config": "openshift-workloads",
        "workloads": [run.role_fqcn],
    }

    # Build requirements_content inferring the collection from the role FQCN
    requirements_content = {"collections": [{"name": collection_fqcn}]}

    return SimpleNamespace(
        extra_vars=extra_vars,
        ee_image="quay.io/redhat-gpte/troshka-runner:latest",
        scm_ref=None,
        requirements_content=requirements_content,
    )


def _resolve_pod_networks(host, project, topo):
    """Resolve pod network attachment for runner pod.

    - KubeVirt hosts: project NADs (for project-network attachment)
    - troshkad hosts: project networks (dict entries)
    """
    if getattr(host, "host_type", None) == "kubevirt-cluster":
        # KubeVirt: return NAD names for project networks
        from app.services.ocp.ops_pod_scaffold import ops_pod_network_nads

        cluster_nads, _bmc_nad = ops_pod_network_nads(topo)
        return cluster_nads
    # troshkad: return network entries (mirrors deploy_service ops pod pattern)
    return []


def _fail_run(db, run_id, message: str) -> None:
    row = db.get(WorkloadRun, run_id)
    if row is not None:
        row.status = "error"
        row.error = message[:2000]
        row.ended_at = _now()
        db.commit()


def _start_workload_monitor(host, run_id: str) -> None:
    """Monitor stub — real monitor implemented in Task 7."""
    # Implemented in Task 7 (enqueued monitor job + Redis lock).
    pass
