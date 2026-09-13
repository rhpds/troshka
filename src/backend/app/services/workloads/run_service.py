"""Orchestrate a workload run: create the record, resolve everything backend-side,
launch the runner pod, and track progress. Credential-safe (git creds + vault key
never leave the backend; see Plan 1)."""

from __future__ import annotations

import datetime
import logging
import re

from app.core.database import SessionLocal
from app.core.redis import enqueue_job
from app.models.project import Project
from app.models.workload_run import WorkloadRun
from app.services.deploy_service import _stored_cluster_creds
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

logger = logging.getLogger(__name__)


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
    requirements_content=None,
    owner_id=None,
) -> WorkloadRun:
    run = WorkloadRun(
        project_id=project_id,
        kind=kind,
        catalog_item=catalog_item,
        role_fqcn=role_fqcn,
        target_map=target_map,
        requirements_content=requirements_content,
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
        key = mint_run_key(db, project)
        topo = project.deployed_topology or project.topology or {}
        validate_ansible_groups(topo, require_bastion=(not _has_ocp(project)))

        from app.core.config import config
        from app.services.workloads import repo_cache

        inv = build_inventory_yaml(config.app.external_url, key, project.id)

        paths = RunPaths()
        extra_vars = dict(item.extra_vars)

        # Thread requirements_content to the pod (HARD REQUIREMENT A)
        if item.requirements_content:
            extra_vars["requirements_content"] = item.requirements_content

        # Read stored kubeconfig from the first cluster's control-plane node (D12: no API calls)
        kubeconfig = next(
            (kc for (_pw, kc) in _stored_cluster_creds(topo).values() if kc), None
        )

        if _has_ocp(project) and not kubeconfig:
            raise RuntimeError(
                f"Project {project.id} targets OCP but no admin kubeconfig is "
                "resolvable from stored topology; cannot run workloads"
            )

        files = build_artifact_files(
            extra_vars=extra_vars,
            inventory_yaml=inv,
            cloud_creds=None,
            kubeconfig=kubeconfig,
            paths=paths,
        )

        # Resolve scm_ref for agnosticd-v2 clone
        scm_ref = item.scm_ref or repo_cache.default_ref("agnosticd-v2")

        # Runner-pod networking so the pod can resolve/reach the cluster API
        # (api.<cluster>.local via the project dnsmasq). Works on BOTH providers:
        # KubeVirt returns cluster NADs + a self-assign-IP prelude + dnsmasq (.2);
        # troshkad returns podman network entries carrying the gateway dnsmasq (.1)
        # and an empty prelude (podman does IPAM).
        networks, dns_nameserver, net_prelude = _resolve_pod_networks(
            host, project, topo
        )

        command = build_run_command(
            item,
            paths,
            agnosticd_v2_url=config.workloads.agnosticd_v2_url,
            scm_ref=scm_ref,
            kubeconfig=kubeconfig,
            net_prelude=net_prelude,
        )

        launch_runner_pod(
            host,
            project,
            ee_image=item.ee_image
            or getattr(config.workloads, "default_ee_image", None)
            or "quay.io/agnosticd/ee-multicloud:chained-latest",
            command=command,
            files=files,
            networks=networks,
            dns_nameserver=dns_nameserver,
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

    Synthesizes the minimal openshift-workloads extra_vars (``config`` +
    ``workloads: [role_fqcn]``) and threads the caller-supplied
    ``requirements_content`` through VERBATIM. This matches how AgnosticD/
    AgnosticV developers declare collections — git-sourced entries such as
    ``{name: https://github.com/rhpds/core_workloads.git, type: git, version: main}``.
    A bare Galaxy name is NOT inferred: agnosticd workload collections are
    git-hosted and not published to Galaxy, so inference would produce an
    unresolvable requirement. If the caller omits ``requirements_content``, none
    is set (the run relies on whatever the EE bundles).
    """
    from types import SimpleNamespace

    from app.core.config import config

    if not run.role_fqcn:
        raise RuntimeError("Ad-hoc run requires role_fqcn")

    # Validate FQCN shape: namespace.collection.role
    if len(run.role_fqcn.split(".")) < 3:
        raise RuntimeError(
            f"Invalid role FQCN: {run.role_fqcn} (expected namespace.collection.role)"
        )

    # Build minimal extra_vars with workloads: [role_fqcn]
    extra_vars = {
        "config": "openshift-workloads",
        "workloads": [run.role_fqcn],
    }

    return SimpleNamespace(
        extra_vars=extra_vars,
        ee_image=getattr(config.workloads, "default_ee_image", None)
        or "quay.io/agnosticd/ee-multicloud:chained-latest",
        scm_ref=None,
        requirements_content=run.requirements_content,
    )


def _resolve_pod_networks(host, project, topo):
    """Resolve runner-pod networking so it can resolve/reach the cluster API
    (``api.<cluster>.local`` via the project dnsmasq). Mirrors the ops pod on
    BOTH providers. Returns ``(networks, dns_nameserver, net_prelude)``:

    - KubeVirt: cluster NAD name(s) + the dnsmasq (``<cidr>.2``) nameserver +
      a ``net_prelude`` of ``ip addr add`` lines (OVN-L2 NADs have no IPAM, so the
      pod must self-assign its lab-net IP before any lookup).
    - troshkad: podman network entries carrying the gateway dnsmasq (``<cidr>.1``);
      podman does IPAM, so ``net_prelude`` is empty.
    """
    if getattr(host, "host_type", None) == "kubevirt-cluster":
        from app.services.deploy_service import (
            _kubevirt_ops_pod_dns,
            _kubevirt_ops_pod_net_ips,
        )
        from app.services.ocp.ops_pod_install import _self_assign_net_ips
        from app.services.ocp.ops_pod_scaffold import ops_pod_network_nads

        cluster_nads, _bmc_nad = ops_pod_network_nads(topo)
        net_ip_assignments, _serving = _kubevirt_ops_pod_net_ips(topo)
        dns = _kubevirt_ops_pod_dns(net_ip_assignments)
        return cluster_nads, dns, _self_assign_net_ips(net_ip_assignments)
    # troshkad: podman networks (IPAM) + gateway dnsmasq; no self-assign prelude.
    # Use a DISTINCT transit IP (.5) — NOT ops_pod_infra_network (.4), which would
    # collide with the (often still-running) ops pod and break the runner's egress.
    from app.services.deploy_topology import _gateway_connected_dns_nameserver
    from app.services.ocp.ops_pod_scaffold import runner_pod_infra_network

    dns = _gateway_connected_dns_nameserver(topo)
    networks = runner_pod_infra_network(project.vni_map or {}, dns_nameserver=dns)
    return networks, dns, ""


def _fail_run(db, run_id, message: str) -> None:
    row = db.get(WorkloadRun, run_id)
    if row is not None:
        row.status = "error"
        row.error = message[:2000]
        row.ended_at = _now()
        db.commit()


# ── Workload monitor (Plan 2, Task 7) ──────────────────────────────────────

_WORKLOAD_MONITOR_TTL = 120


def _workload_monitor_lock_key(run_id: str) -> str:
    return f"workload-monitor:{run_id}"


def _acquire_workload_monitor_lock(run_id: str) -> bool:
    """True if this caller may run the monitor. Redis SET NX; if Redis is
    unavailable (in-memory, not shared), allow (single-process fallback)."""
    from app.core.redis import get_redis, is_redis_available

    if not is_redis_available():
        return True
    try:
        return bool(
            get_redis().set(
                _workload_monitor_lock_key(run_id),
                "1",
                nx=True,
                ex=_WORKLOAD_MONITOR_TTL,
            )
        )
    except Exception:
        return True


def _refresh_workload_monitor_lock(run_id: str) -> None:
    from app.core.redis import get_redis, is_redis_available

    if not is_redis_available():
        return
    try:
        get_redis().set(
            _workload_monitor_lock_key(run_id), "1", ex=_WORKLOAD_MONITOR_TTL
        )
    except Exception:
        pass


def _release_workload_monitor_lock(run_id: str) -> None:
    from app.core.redis import get_redis, is_redis_available

    if not is_redis_available():
        return
    try:
        get_redis().delete(_workload_monitor_lock_key(run_id))
    except Exception:
        pass


def _detached_host_copy(host_id: str):
    """Return a session-detached Host with all columns eager-loaded.

    Background monitor jobs must never touch the run job's Session — sharing
    the ORM object raises SQLAlchemy "This session is provisioning a new
    connection; concurrent operations are not permitted". Loading every mapped
    column and expunging yields a plain object safe to use off-thread.
    """
    from sqlalchemy import inspect as sa_inspect

    from app.models.host import Host

    s = SessionLocal()
    try:
        host = s.query(Host).filter_by(id=host_id).first()
        if host is not None:
            for col in sa_inspect(host).mapper.column_attrs:
                getattr(host, col.key)  # force-load before detaching
            s.expunge(host)
        return host
    finally:
        s.close()


def parse_workload_progress(log_text: str) -> dict:
    """PURE: extract Ansible task name from log text for progress display.

    Searches for "TASK [role_name : task_description]" patterns and returns
    the most recent task name found. Returns empty dict if no task found.
    """
    # Match "TASK [role : description]" or "TASK [description]"
    # Take the last match (most recent task in the log)
    matches = re.findall(r"TASK \[(.*?)\]", log_text)
    if matches:
        return {"step": matches[-1]}
    return {}


def _enqueue_monitor(host, run_id: str) -> None:
    """Start the workload run monitor as its own RQ job.

    Enqueues a background job that tails the runner pod logs, publishes
    progress, and sets terminal status. One monitor per run across workers
    via Redis lock (acquired here, refreshed by loop, released on exit).
    """
    from app.workers.jobs import job_workload_monitor

    if not _acquire_workload_monitor_lock(run_id):
        logger.info(
            "Workload monitor %s: already running elsewhere, not starting",
            run_id[:8],
        )
        return
    enqueue_job(
        job_workload_monitor,
        run_id,
        host.id,
        job_timeout=14400,
    )


def _start_workload_monitor(host, run_id: str) -> None:
    """Start monitoring a workload run (enqueued RQ job)."""
    _enqueue_monitor(host, run_id)


def _publish_workload_progress(run_id: str, progress: dict) -> None:
    """Publish workload progress to WebSocket channel and persist to DB."""
    from app.core.redis import set_progress
    from app.services.ws_pubsub import notify_project

    set_progress(f"workload:{run_id}", progress)

    # Get project_id for WebSocket notification
    db = SessionLocal()
    try:
        run = db.get(WorkloadRun, run_id)
        if run and run.project_id:
            notify_project(
                run.project_id, {"type": "workload-progress", "progress": progress}
            )
    finally:
        db.close()


def _get_project_id_for_run(run_id: str) -> str | None:
    """Get project_id from a WorkloadRun (for KubeVirt namespace resolution)."""
    db = SessionLocal()
    try:
        run = db.get(WorkloadRun, run_id)
        return run.project_id if run else None
    finally:
        db.close()


def _read_runner_pod_logs(host, run_id: str) -> str:
    """Read logs from the runner pod via troshkad containers/exec (cat logfile)."""
    if host.host_type == "kubevirt-cluster":
        return _read_runner_logs_kubevirt(host, run_id)
    return _read_runner_logs_troshkad(host, _troshkad_container_for_run(run_id))


def _troshkad_container_for_run(run_id: str) -> str:
    """Full podman container name of the troshkad runner for this run.

    troshkad prefixes a pod's containers (troshka-<pid8>-<pod>-<container>), so the
    monitor must NOT look up the bare "runner" — it won't match /containers/states.
    """
    from app.services.workloads.pod_launch import troshkad_runner_container_name

    project_id = _get_project_id_for_run(run_id) or ""
    return troshkad_runner_container_name(project_id)


def _read_runner_logs_troshkad(host, container_name: str) -> str:
    """[LIVE-ENV] Read runner logs via troshkad `containers/logs` (podman logs).

    Uses `podman logs` (not `exec cat`) because the monitor reads the log at
    FINALIZATION too — when the runner container has EXITED and exec can no longer
    reach it (exec requires a running container). The runner tees its output to
    both stdout and /workdir/run.log, so `podman logs` returns the same content and
    works whether the container is running or stopped. Empty string on failure.
    """
    from app.services.troshkad_client import TroshkadError, start_job, wait_for_job

    try:
        job_id = start_job(
            host,
            "/containers/logs",
            {"container_name": container_name, "tail": 2000},
        )
        job = wait_for_job(host, job_id, timeout=30)
        if job.get("status") == "completed":
            return (job.get("result") or {}).get("logs", "")
    except TroshkadError:
        pass
    return ""


def _read_runner_logs_kubevirt(host, run_id: str) -> str:
    """[LIVE-ENV] Read runner logs via k8s exec cat.

    Mirrors _exec_ops_pod_cat_kubevirt: exec `cat /workdir/run.log` via
    k8s stream API. Returns empty string on any failure or missing file.
    """
    ctx = _runner_pod_kubevirt_ctx(host, run_id)
    if not ctx:
        return ""
    core_v1, namespace, pod_name = ctx
    log_path = "/workdir/run.log"

    from kubernetes.stream import stream as k8s_stream

    try:
        result = k8s_stream(
            core_v1.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            container="ops",
            command=["cat", log_path],
            stderr=True,
            stdout=True,
            stdin=False,
            tty=False,
            _preload_content=True,
            _request_timeout=35,
        )
        return result if isinstance(result, str) else ""
    except Exception:  # noqa: BLE001
        return ""


def _runner_pod_kubevirt_ctx(host, run_id: str):
    """Resolve (core_v1, namespace, pod_name) for a KubeVirt runner pod.

    Mirrors _kubevirt_ops_pod_ctx. Returns None if provider missing.
    """
    from app.core.database import SessionLocal
    from app.models.provider import Provider
    from app.services.providers.kubevirt import _get_k8s_clients, _project_ns

    project_id = _get_project_id_for_run(run_id)
    if not project_id:
        return None

    db = SessionLocal()
    try:
        provider = db.query(Provider).filter_by(id=host.provider_id).first()
        if not provider:
            return None
        _, core_v1, _ = _get_k8s_clients(provider)
        namespace = _project_ns(provider, project_id)
    finally:
        db.close()
    return core_v1, namespace, "workload-runner"


def _is_runner_pod_running(host, run_id: str) -> bool:
    """Check if the runner pod/container is still running.

    Mirrors _ops_pod_running: conservative on uncertainty (transient errors
    assume running). Only terminal states (container stopped, pod Succeeded/Failed)
    count as dead.
    """
    if host.host_type == "kubevirt-cluster":
        return _runner_pod_running_kubevirt(host, run_id)
    return _runner_pod_running_troshkad(host, _troshkad_container_for_run(run_id))


# Explicit terminal container states — anything else (running, created, configured,
# starting, or transient/unknown) is treated as still-alive so the monitor doesn't
# finalize during the create→running startup window.
_TROSHKAD_DEAD_STATES = {"exited", "stopped", "died"}


def _runner_pod_running_troshkad(host, container_name: str) -> bool:
    """[LIVE-ENV] Whether the runner container is still alive.

    Mirrors _ops_pod_running: conservative — only an EXPLICIT terminal state
    (exited/stopped/died) counts as dead. A just-started container reports
    "created"/"configured" briefly before "running"; treating those as dead
    finalized runs prematurely. None states (transient API error) → alive.
    """
    from app.services.troshkad_client import get_all_container_states

    states = get_all_container_states(host)
    if states is None:
        return True
    info = states.get(container_name)
    if info is None:
        return True  # not yet registered / transient — do not declare dead
    return str(info.get("state", "")).lower() not in _TROSHKAD_DEAD_STATES


def _runner_pod_running_kubevirt(host, run_id: str) -> bool:
    """[LIVE-ENV] Whether the runner Pod is in Running phase.

    Mirrors _ops_pod_running_kubevirt: conservative (transient API error → True).
    Only terminal phases (Succeeded/Failed) or 404 count as dead.
    """
    from kubernetes.client.exceptions import ApiException

    ctx = _runner_pod_kubevirt_ctx(host, run_id)
    if not ctx:
        return True
    core_v1, namespace, pod_name = ctx
    try:
        pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
    except ApiException as e:
        return e.status != 404
    except Exception:  # noqa: BLE001
        return True
    phase = str(getattr(getattr(pod, "status", None), "phase", "") or "").lower()
    return phase not in ("succeeded", "failed")


def monitor_workload_run(run_id: str, host_id: str) -> None:
    """[LIVE-ENV loop] Poll runner pod logs and stream workload progress.

    Loops until the pod completes (exits successfully), fails, or times out.
    Each iteration reads logs, extracts progress, publishes updates, and checks
    for terminal conditions. On success sets status=succeeded; on failure sets
    status=error. Always releases the monitor lock before returning.
    """
    import time as _t

    host = _detached_host_copy(host_id)
    if host is None:
        logger.warning(
            "Workload monitor %s: host %s not found; monitor not started",
            run_id[:8],
            str(host_id)[:8],
        )
        _release_workload_monitor_lock(run_id)
        return

    timeout = 7200  # 2 hours
    poll_interval = 15
    deadline = _t.time() + timeout
    last_progress = {}

    while _t.time() < deadline:
        _refresh_workload_monitor_lock(run_id)

        # Read logs and extract progress
        logs = _read_runner_pod_logs(host, run_id)
        progress = parse_workload_progress(logs)

        # Publish if progress changed
        if progress != last_progress:
            _publish_workload_progress(run_id, progress)
            last_progress = progress

        # Check if pod is still running
        pod_running = _is_runner_pod_running(host, run_id)

        if not pod_running:
            # Pod stopped — check exit status to determine success/failure
            terminal_status = _check_runner_pod_exit_status(host, run_id, logs)
            _finalize_workload_run(run_id, terminal_status, logs)
            _release_workload_monitor_lock(run_id)
            return

        _t.sleep(poll_interval)

    # Timeout
    logger.warning("Workload monitor %s: timed out", run_id[:8])
    _finalize_workload_run(run_id, "timeout", "Monitor timeout after 2 hours")
    _release_workload_monitor_lock(run_id)


def _check_runner_pod_exit_status(host, run_id: str, logs: str) -> str:
    """Check pod exit status to determine success or failure.

    Prefers real exit code over log inference. Falls back to log inspection
    only if API check fails.
    """
    if host.host_type == "kubevirt-cluster":
        return _check_exit_status_kubevirt(host, run_id, logs)
    return _check_exit_status_troshkad(host, logs, _troshkad_container_for_run(run_id))


def _check_exit_status_troshkad(host, logs: str, container_name: str) -> str:
    """[LIVE-ENV] Get runner container exit code via troshkad states.

    Mirrors ops-pod exit-code check. Returns "succeeded" if exit_code == 0,
    "error" otherwise. Falls back to log inference on any failure.
    """
    from app.services.troshkad_client import get_all_container_states

    states = get_all_container_states(host)
    if states:
        info = states.get(container_name)
        if info:
            exit_code = info.get("exit_code")
            if exit_code is not None:
                return "succeeded" if exit_code == 0 else "error"
    return _infer_status_from_logs(logs)


def _check_exit_status_kubevirt(host, run_id: str, logs: str) -> str:
    """[LIVE-ENV] Get runner Pod exit code via k8s container status.

    Reads pod.status.container_statuses[0].state.terminated.exit_code. Returns
    "succeeded" if 0, "error" otherwise. Falls back to log inference on failure.
    """
    ctx = _runner_pod_kubevirt_ctx(host, run_id)
    if not ctx:
        return _infer_status_from_logs(logs)
    core_v1, namespace, pod_name = ctx
    try:
        pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        container_statuses = getattr(
            getattr(pod, "status", None), "container_statuses", None
        )
        if container_statuses:
            for cs in container_statuses:
                terminated = getattr(getattr(cs, "state", None), "terminated", None)
                if terminated:
                    exit_code = getattr(terminated, "exit_code", None)
                    if exit_code is not None:
                        return "succeeded" if exit_code == 0 else "error"
    except Exception:  # noqa: BLE001
        pass
    return _infer_status_from_logs(logs)


def _infer_status_from_logs(logs: str) -> str:
    """Infer status from log content when API checks unavailable."""
    # Look for common Ansible success/failure markers
    # Require PLAY RECAP presence AND no failed=[1-9]+ to infer success
    if "PLAY RECAP" in logs and not re.search(r"failed=[1-9]\d*", logs):
        return "succeeded"
    if "fatal:" in logs or "ERROR" in logs:
        return "error"
    return "error"


def _finalize_workload_run(run_id: str, status: str, error_or_logs: str) -> None:
    """Set terminal status on WorkloadRun."""
    db = SessionLocal()
    try:
        run = db.get(WorkloadRun, run_id)
        if run is not None:
            run.status = status
            run.ended_at = _now()
            if status == "error" or status == "timeout":
                run.error = error_or_logs[:2000]
            db.commit()
            logger.info("Workload run %s finalized: %s", run_id[:8], status)
    finally:
        db.close()


def _enqueue_monitor_by_ids(run_id: str, host_id: str) -> None:
    """Enqueue monitor given run_id and host_id (for resume)."""
    from app.workers.jobs import job_workload_monitor

    if not _acquire_workload_monitor_lock(run_id):
        logger.info(
            "Workload monitor %s: already running elsewhere, not resuming",
            run_id[:8],
        )
        return
    enqueue_job(
        job_workload_monitor,
        run_id,
        host_id,
        job_timeout=14400,
    )


def resume_workload_monitors() -> None:
    """[worker startup] Re-attach monitors for stranded workload runs.

    Covers workload runs stuck at status='running' (a prior worker died
    mid-run, leaving no finalization). The per-run lock makes this safe
    across all worker processes. The monitor is idempotent: it re-reads
    logs and finalizes if the run already completed.
    """
    from sqlalchemy import text

    db = SessionLocal()
    try:
        # Use a raw query to avoid UUID conversion issues in test DB
        result = db.execute(
            text(
                "SELECT wr.id, p.host_id "
                "FROM workload_runs wr "
                "JOIN projects p ON wr.project_id = p.id "
                "WHERE wr.status = 'running' AND p.host_id IS NOT NULL"
            )
        )
        for row in result:
            try:
                run_id: str = row[0]
                host_id: str = row[1]
                logger.info("Resuming workload monitor for %s", run_id[:8])
                _enqueue_monitor_by_ids(run_id, host_id)
            except Exception as exc:
                logger.exception("Failed to resume monitor: %s", exc)
                continue
    except Exception:
        logger.exception("resume_workload_monitors failed")
    finally:
        db.close()


def prune_workload_runs(
    db, *, retention_days: int, now: datetime.datetime | None = None
) -> int:
    """Delete terminal WorkloadRun records older than the retention window.

    Deletes runs with status in ("succeeded", "error", "timeout") and ended_at
    older than (now - retention_days). Never deletes running or pending runs.
    Returns count of deleted runs.

    Args:
        db: SQLAlchemy session.
        retention_days: Number of days to retain terminal runs.
        now: Reference time (defaults to UTC now if not provided).

    Returns:
        Number of WorkloadRun records deleted.
    """
    if now is None:
        now = _now()

    cutoff = now - datetime.timedelta(days=retention_days)

    runs_to_delete = (
        db.query(WorkloadRun)
        .filter(
            WorkloadRun.status.in_(("succeeded", "error", "timeout")),
            WorkloadRun.ended_at < cutoff,
        )
        .all()
    )

    count = len(runs_to_delete)
    for run in runs_to_delete:
        db.delete(run)
    if count > 0:
        db.commit()
        logger.info(
            "Pruned %d WorkloadRun records older than %d days", count, retention_days
        )

    return count
