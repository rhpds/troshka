"""
Redis-backed WebSocket pub/sub + background state poller.

All notifications flow through Redis pub/sub so multiple backend replicas
can deliver WebSocket updates to their respective clients. Background
services (VM state polling, OCP monitors, timers) run continuously against
the DB regardless of connected viewers.

notify_project() publishes to a Redis channel. Each backend pod subscribes
to Redis channels and delivers messages to its local WebSocket clients.
"""

import asyncio
import json
import logging
import threading

from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)

_subscribers: dict[str, set[WebSocket]] = {}
_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_redis_listener_started = False


def set_event_loop(loop: asyncio.AbstractEventLoop):
    global _loop
    _loop = loop


def subscribe(project_id: str, ws: WebSocket):
    with _lock:
        if project_id not in _subscribers:
            _subscribers[project_id] = set()
        _subscribers[project_id].add(ws)
    logger.debug(
        "WS subscribe: project=%s (total=%d)",
        project_id[:8],
        len(_subscribers[project_id]),
    )


def unsubscribe(project_id: str, ws: WebSocket):
    with _lock:
        subs = _subscribers.get(project_id)
        if subs:
            subs.discard(ws)
            if not subs:
                del _subscribers[project_id]


async def _send_to_subscribers(project_id: str, message: dict):
    with _lock:
        subs = set(_subscribers.get(project_id, set()))

    data = json.dumps(message)
    dead = []
    for ws in subs:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)

    if dead:
        with _lock:
            s = _subscribers.get(project_id)
            if s:
                for ws in dead:
                    s.discard(ws)
                if not s:
                    del _subscribers[project_id]


def notify_project(project_id: str, message: dict):
    """Publish a notification via Redis pub/sub.

    All backend pods receive the message and deliver to their local WS clients.
    Also delivers locally if this pod has subscribers (for single-replica mode
    or if Redis is unavailable).
    """
    # Always deliver locally (this pod's subscribers)
    _deliver_locally(project_id, message)

    # Also publish to Redis so other pods receive it
    try:
        from app.core.redis import is_redis_available, publish

        if is_redis_available():
            publish(f"project:{project_id}", message)
    except Exception:
        pass


def _deliver_locally(project_id: str, message: dict):
    """Deliver a message to local WebSocket subscribers on this pod."""
    if not _loop:
        return
    with _lock:
        if project_id not in _subscribers:
            return
    asyncio.run_coroutine_threadsafe(_send_to_subscribers(project_id, message), _loop)


def notify_pattern(pattern_id: str, message: dict):
    notify_project(f"pattern:{pattern_id}", message)


def subscribe_pattern(pattern_id: str, ws: WebSocket):
    subscribe(f"pattern:{pattern_id}", ws)


def unsubscribe_pattern(pattern_id: str, ws: WebSocket):
    unsubscribe(f"pattern:{pattern_id}", ws)


def start_redis_listener():
    """Start a background thread that subscribes to Redis pub/sub and delivers
    messages to local WebSocket clients. Called once from the lifespan handler."""
    global _redis_listener_started
    if _redis_listener_started:
        return
    _redis_listener_started = True
    thread = threading.Thread(
        target=_redis_listen_loop, daemon=True, name="redis-ws-bridge"
    )
    thread.start()
    logger.info("Redis WebSocket pub/sub bridge started")


def _dispatch_pubsub_message(msg: dict) -> None:
    """Parse a single Redis pub/sub message and deliver to local WS clients."""
    if msg["type"] != "pmessage":
        return
    channel = msg["channel"]
    if isinstance(channel, bytes):
        channel = channel.decode()
    prefix = "project:"
    if not channel.startswith(prefix):
        return
    project_id = channel[len(prefix) :]
    try:
        data = json.loads(msg["data"])
    except (json.JSONDecodeError, TypeError):
        return
    _deliver_locally(project_id, data)


def _redis_listen_loop():
    """Subscribe to Redis pub/sub channels and dispatch to local WS clients.

    Reconnects automatically on timeout or connection loss.
    """
    import time as _time

    import redis as _redis

    while True:
        try:
            from app.core.redis import _get_redis_url

            conn = _redis.from_url(
                _get_redis_url(), decode_responses=True, socket_timeout=None
            )
            ps = conn.pubsub()
            ps.psubscribe("project:*")
            logger.info("Redis pub/sub listener connected")

            for msg in ps.listen():
                _dispatch_pubsub_message(msg)
        except Exception:
            logger.warning("Redis pub/sub listener disconnected, reconnecting in 5s")
            _time.sleep(5)


_last_states: dict[str, dict] = {}
_POLL_INTERVAL = 2


def get_cached_vm_states(project_id: str) -> dict | None:
    """Return cached VM states from the last poll cycle, or None if not cached."""
    cached = _last_states.get(project_id)
    if not cached:
        return None
    return {
        "states": cached.get("vm_states", {}),
        "container_states": cached.get("container_states", {}),
        "progress": cached.get("vm_progress", {}),
    }


_OCP_MONITOR_SCAN_INTERVAL = 30
_last_ocp_scan = 0.0


def _poll_loop():
    logger.info("WS state poller started (interval=%ds)", _POLL_INTERVAL)
    import time

    while True:
        time.sleep(_POLL_INTERVAL)
        try:
            _poll_active_projects()
        except Exception:
            logger.exception("WS state poller error")
        try:
            _maybe_scan_ocp_monitors()
        except Exception:
            logger.exception("OCP monitor scan error")


def _maybe_scan_ocp_monitors():
    """Periodically start OCP health monitors for projects still in 'monitoring'
    state, even if no one has the project open in the UI."""
    import time

    global _last_ocp_scan
    now = time.time()
    if now - _last_ocp_scan < _OCP_MONITOR_SCAN_INTERVAL:
        return
    _last_ocp_scan = now

    from app.core.database import SessionLocal
    from app.models.project import Project
    from app.services.deploy_service import maybe_start_ocp_health_monitor

    db = SessionLocal()
    try:
        projects = (
            db.query(Project.id)
            .filter(Project.ocp_status == "monitoring", Project.state == "active")
            .all()
        )
        if projects:
            logger.info(
                "OCP monitor scan: %d project(s) need monitoring", len(projects)
            )
        for (project_id,) in projects:
            maybe_start_ocp_health_monitor(project_id)
    finally:
        db.close()


def _fetch_kubevirt_vm_states(project, host, db):
    """Fetch VM states from KubeVirt VMIs for a kubevirt-cluster host.

    Returns a dict of {node_id: phase_string} or None on failure.
    """
    from app.models.provider import Provider
    from app.services.providers.kubevirt import _get_k8s_clients, _project_ns

    provider = db.query(Provider).filter_by(id=host.provider_id).first()
    if not provider:
        return None
    try:
        custom_api, _, _ = _get_k8s_clients(provider)
        namespace = _project_ns(provider, project.id)
        vmis_raw = custom_api.list_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachineinstances",
        )
        vmis: dict = vmis_raw if isinstance(vmis_raw, dict) else {}
        vmi_phases = {}
        for vmi in vmis.get("items", []):
            vmi_phases[vmi["metadata"]["name"]] = vmi.get("status", {}).get(
                "phase", "Unknown"
            )
        vm_states = {}
        topo = project.topology or {}
        for node in topo.get("nodes", []):
            if node.get("type") != "vmNode":
                continue
            node_id = node.get("data", {}).get("id", node.get("id", ""))
            kv_name = f"troshka-vm-{node_id[:8]}"
            if kv_name in vmi_phases:
                vm_states[node_id] = vmi_phases[kv_name]
            else:
                vm_states[node_id] = "Stopped"
        return vm_states if vm_states else None
    except Exception:
        return None


_STOPPED_STATES = frozenset(
    ("shut_off", "shutting_down", "crashed", "suspended", "paused", "Stopped")
)


def _normalize_vm_state(state: str) -> str:
    """Normalize a raw VM state to a canonical display state."""
    if state in _STOPPED_STATES:
        return "stopped"
    if state == "Running":
        return "running"
    return state


def _map_vm_states_for_project(project, host_batch, kv_batch):
    """Map batch VM states to per-project node states.

    Returns (vm_states, vm_progress, vm_boot_devs).
    """
    from app.api.projects import _domain_name, _redeploy_progress

    vm_states = {}
    vm_progress = {}
    vm_boot_devs = {}

    batch = kv_batch or host_batch
    is_kubevirt = kv_batch is not None
    if batch is None:
        return vm_states, vm_progress, vm_boot_devs

    for node in (project.topology or {}).get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        node_id = node.get("data", {}).get("id", node.get("id", ""))
        if is_kubevirt:
            state = batch.get(node_id, "not_found")
        else:
            dom_name = _domain_name(project.id, node_id)
            if dom_name in _redeploy_progress:
                vm_states[node_id] = "redeploying"
                vm_progress[node_id] = _redeploy_progress[dom_name]
                continue
            state = batch.get(dom_name, "not_found")
        if state == "not_found":
            continue
        vm_states[node_id] = _normalize_vm_state(state)

    return vm_states, vm_progress, vm_boot_devs


def _container_domain_name(project_id: str, node_id: str) -> str:
    return f"troshka-{project_id[:8]}-{node_id[:8]}"


_CONTAINER_STOPPED_STATES = frozenset(("stopped", "dead"))


def _map_container_states_for_project(project, host_batch):
    """Map batch container states to per-project containerNode states.

    Returns dict of {node_id: {"state": str, "ips": list[str]}}.
    """
    container_states: dict = {}
    if host_batch is None:
        return container_states

    for node in (project.topology or {}).get("nodes", []):
        if node.get("type") != "containerNode":
            continue
        node_id = node.get("data", {}).get("id", node.get("id", ""))
        name = _container_domain_name(project.id, node_id)
        info = host_batch.get(name)
        if info is None:
            continue
        state = info.get("state", "unknown")
        container_states[node_id] = {
            "state": "stopped" if state in _CONTAINER_STOPPED_STATES else state,
            "ips": info.get("ips", []),
        }

    return container_states


def _log_vm_state_changes(project, vm_states, prev_vm_states):
    """Log individual VM state transitions for debugging."""
    for vm_id, new_state in vm_states.items():
        old_state = prev_vm_states.get(vm_id)
        if not old_state or old_state == new_state:
            continue
        vm_label = ""
        for node in (project.topology or {}).get("nodes", []):
            if node["id"] == vm_id:
                vm_label = node.get("data", {}).get("label", vm_id[:8])
                break
        logger.info(
            "VM state change: %s/%s %s → %s",
            project.name[:30],
            vm_label,
            old_state,
            new_state,
        )


def _check_and_notify_project_changes(
    project_id,
    project,
    dp,
    vm_states,
    vm_progress,
    vm_boot_devs,
    container_states=None,
):
    """Compare current state vs last cached state and send WS notifications.

    Updates _last_states for the project.
    """
    if container_states is None:
        container_states = {}
    last = _last_states.get(project_id, {})
    current_project_state = project.state
    current_deploy_error = project.deploy_error

    # Push project state changes
    if (
        last.get("project_state") != current_project_state
        or last.get("deploy_error") != current_deploy_error
    ):
        notify_project(
            project_id,
            {
                "type": "project-state",
                "state": current_project_state,
                "deploy_error": current_deploy_error,
            },
        )

    # Push deploy progress if active
    if dp and dp != last.get("deploy_progress"):
        notify_project(project_id, {"type": "deploy-progress", "progress": dp})

    # Log and notify VM state changes
    prev_vm_states = last.get("vm_states", {})
    _log_vm_state_changes(project, vm_states, prev_vm_states)

    if vm_states and (
        vm_states != prev_vm_states
        or vm_progress != last.get("vm_progress")
        or vm_boot_devs != last.get("vm_boot_devs")
    ):
        notify_project(
            project_id,
            {
                "type": "vm-state",
                "states": vm_states,
                "progress": vm_progress,
                "boot_devs": vm_boot_devs,
            },
        )

    # Log and notify container state changes
    prev_container_states = last.get("container_states", {})
    if container_states and container_states != prev_container_states:
        notify_project(
            project_id,
            {"type": "container-state", "states": container_states},
        )

    # Update cache
    _last_states[project_id] = {
        "project_state": current_project_state,
        "deploy_error": current_deploy_error,
        "deploy_progress": dp,
        "vm_states": vm_states if vm_states else last.get("vm_states", {}),
        "vm_progress": vm_progress,
        "vm_boot_devs": vm_boot_devs,
        "container_states": (
            container_states if container_states else last.get("container_states", {})
        ),
    }


def _fetch_troshkad_host_states(host, host_batch_states, container_batch_states=None):
    """Fetch VM and container states from a troshkad host if not already cached."""
    if container_batch_states is None:
        container_batch_states = {}
    if host.agent_status != "connected":
        return
    from app.services.troshkad_client import get_all_container_states, get_all_vm_states

    if host.id not in host_batch_states:
        try:
            batch = get_all_vm_states(host)
            if batch is not None:
                host_batch_states[host.id] = batch
        except Exception:
            pass

    if host.id not in container_batch_states:
        try:
            batch = get_all_container_states(host)
            if batch is not None:
                container_batch_states[host.id] = batch
        except Exception:
            pass


def _collect_hosts_to_query(projects):
    """Build set of all hosts to query from project assignments."""
    hosts_to_query = set()
    for project in projects.values():
        if project.host_assignments:
            hosts_to_query.update(set(project.host_assignments.values()))
        elif project.host_id:
            hosts_to_query.add(project.host_id)
    return hosts_to_query


def _fetch_kubevirt_states_for_host(host_id, projects, host, db, project_batch_states):
    """Fetch KubeVirt VM states for all projects on this cluster."""
    for project in projects.values():
        if project.host_id == host_id or (
            project.host_assignments and host_id in project.host_assignments.values()
        ):
            kv_states = _fetch_kubevirt_vm_states(project, host, db)
            if kv_states:
                project_batch_states[project.id] = kv_states


def _batch_fetch_vm_states(projects, deploying_host_ids, db):
    """Batch-fetch VM and container states: one call per host (troshkad) or per
    project (kubevirt).

    Returns (host_batch_states, project_batch_states, container_batch_states).
    """
    from app.models.host import Host

    host_batch_states: dict = {}
    project_batch_states: dict = {}
    container_batch_states: dict = {}

    hosts_to_query = _collect_hosts_to_query(projects)
    hosts_to_query -= deploying_host_ids

    for host_id in hosts_to_query:
        host = db.query(Host).filter_by(id=host_id).first()
        if not host or not host.ip_address:
            continue
        if host.host_type == "kubevirt-cluster":
            _fetch_kubevirt_states_for_host(
                host_id, projects, host, db, project_batch_states
            )
        else:
            _fetch_troshkad_host_states(host, host_batch_states, container_batch_states)

    return host_batch_states, project_batch_states, container_batch_states


def _collect_host_batch_for_project(project, host_batch_states):
    """Collect host states for this project (single-host or multi-host)."""
    if project.host_assignments:
        host_batch = {}
        for host_id in set(project.host_assignments.values()):
            if host_id in host_batch_states:
                host_batch.update(host_batch_states[host_id])
        return host_batch
    return host_batch_states.get(project.host_id) if project.host_id else None


def _get_vm_states_for_project(project, host_batch, kv_batch):
    """Get VM states, progress, and boot devices for a project."""
    if project.state in ("active", "stopped"):
        return _map_vm_states_for_project(project, host_batch, kv_batch)
    return {}, {}, {}


def _get_container_states_for_project(project, container_batch):
    """Get container states for a project."""
    if project.state in ("active", "stopped"):
        return _map_container_states_for_project(project, container_batch)
    return {}


def _evict_stale_cache_entries(projects):
    """Evict cache entries for projects no longer active/stopped."""
    stale = [k for k in _last_states if k not in projects]
    for k in stale:
        del _last_states[k]


def _notify_all_projects(
    projects, host_batch_states, project_batch_states, container_batch_states=None
):
    """Send WS notifications for all projects based on fetched VM/container states."""
    if container_batch_states is None:
        container_batch_states = {}
    from app.services.deploy_service import _get_deploy_progress_data

    for project_id, project in projects.items():
        dp = _get_deploy_progress_data(project_id)
        kv_batch = project_batch_states.get(project_id)
        host_batch = _collect_host_batch_for_project(project, host_batch_states)
        container_batch = _collect_host_batch_for_project(
            project, container_batch_states
        )
        vm_states, vm_progress, vm_boot_devs = _get_vm_states_for_project(
            project, host_batch, kv_batch
        )
        container_states = _get_container_states_for_project(project, container_batch)
        _check_and_notify_project_changes(
            project_id,
            project,
            dp,
            vm_states,
            vm_progress,
            vm_boot_devs,
            container_states,
        )

    _evict_stale_cache_entries(projects)


def _poll_active_projects():
    from sqlalchemy import or_

    from app.core.database import SessionLocal
    from app.models.project import Project
    from app.services.deploy_service import _get_deploy_progress_data

    db = SessionLocal()
    try:
        all_projects = (
            db.query(Project)
            .filter(
                or_(
                    Project.host_id.isnot(None),
                    Project.host_assignments.isnot(None),
                ),
                Project.state.in_(("active", "stopped")),
            )
            .all()
        )
        if not all_projects:
            return

        projects = {p.id: p for p in all_projects}

        deploying_host_ids = set()
        for pid, p in projects.items():
            if _get_deploy_progress_data(pid):
                if p.host_id:
                    deploying_host_ids.add(p.host_id)
                if p.host_assignments:
                    deploying_host_ids.update(set(p.host_assignments.values()))

        host_batch_states, project_batch_states, container_batch_states = (
            _batch_fetch_vm_states(projects, deploying_host_ids, db)
        )
        _notify_all_projects(
            projects, host_batch_states, project_batch_states, container_batch_states
        )
    finally:
        db.close()


def start_state_poller():
    thread = threading.Thread(target=_poll_loop, daemon=True, name="ws-state-poller")
    thread.start()
    return thread
