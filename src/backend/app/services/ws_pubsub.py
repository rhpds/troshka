"""
In-memory WebSocket pub/sub for real-time project state updates.

Manages subscriber sets keyed by project_id. Sync callers (background threads,
deploy service) use notify_project() which bridges into the async event loop
via asyncio.run_coroutine_threadsafe().
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


def get_active_project_ids() -> set[str]:
    with _lock:
        return {k for k in _subscribers.keys() if ":" not in k}


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


_last_states: dict[str, dict] = {}
_POLL_INTERVAL = 5


def _poll_loop():
    logger.info("WS state poller started (interval=%ds)", _POLL_INTERVAL)
    import time

    while True:
        time.sleep(_POLL_INTERVAL)
        try:
            _poll_active_projects()
        except Exception:
            logger.exception("WS state poller error")


def _poll_active_projects():
    project_ids = get_active_project_ids()
    if not project_ids:
        return

    from app.api.projects import _domain_name, _redeploy_progress
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project
    from app.services.deploy_service import _deploy_progress
    from app.services.troshkad_client import get_all_vm_states

    db = SessionLocal()
    try:
        # Load all subscribed projects
        projects = {}
        for project_id in project_ids:
            project = db.query(Project).filter_by(id=project_id).first()
            if project:
                projects[project_id] = project

        # Check which hosts have active deploys
        deploying_host_ids = set()
        for pid in project_ids:
            if pid in _deploy_progress:
                p = projects.get(pid)
                if p and p.host_id:
                    deploying_host_ids.add(p.host_id)

        # Batch-fetch VM states: one call per host instead of per-VM
        host_batch_states = {}
        hosts_polled = {}
        for project in projects.values():
            if not project.host_id or project.state not in ("active", "stopped"):
                continue
            if (
                project.host_id in deploying_host_ids
                or project.host_id in host_batch_states
            ):
                continue
            host = db.query(Host).filter_by(id=project.host_id).first()
            if not host or not host.ip_address or host.agent_status != "connected":
                continue
            hosts_polled[project.host_id] = host
            try:
                batch = get_all_vm_states(host)
                if batch is not None:
                    host_batch_states[project.host_id] = batch
            except Exception:
                pass

        for project_id, project in projects.items():
            # Start OCP health monitor on demand (only when someone is watching)
            if project.ocp_status == "monitoring" and project.state == "active":
                from app.services.deploy_service import maybe_start_ocp_health_monitor

                maybe_start_ocp_health_monitor(project_id)

            # Always push project state changes
            last = _last_states.get(project_id, {})
            current_project_state = project.state
            current_deploy_error = project.deploy_error
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
            dp = _deploy_progress.get(project_id)
            if dp and dp != last.get("deploy_progress"):
                notify_project(project_id, {"type": "deploy-progress", "progress": dp})

            # Map batch VM states to this project's nodes
            vm_states = {}
            vm_progress = {}
            vm_boot_devs = {}
            batch = host_batch_states.get(project.host_id) if project.host_id else None
            if batch is not None and current_project_state in ("active", "stopped"):
                for node in (project.topology or {}).get("nodes", []):
                    if node.get("type") != "vmNode":
                        continue
                    dom_name = _domain_name(project.id, node["id"])
                    if dom_name in _redeploy_progress:
                        vm_states[node["id"]] = "redeploying"
                        vm_progress[node["id"]] = _redeploy_progress[dom_name]
                    else:
                        state = batch.get(dom_name, "not_found")
                        if state == "not_found":
                            continue
                        if state in (
                            "shut_off",
                            "shutting_down",
                            "crashed",
                            "suspended",
                            "paused",
                        ):
                            state = "stopped"
                        vm_states[node["id"]] = state

            # Log VM state changes
            prev_vm_states = last.get("vm_states", {})
            for vm_id, new_state in vm_states.items():
                old_state = prev_vm_states.get(vm_id)
                if old_state and old_state != new_state:
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

            _last_states[project_id] = {
                "project_state": current_project_state,
                "deploy_error": current_deploy_error,
                "deploy_progress": dp,
                "vm_states": vm_states,
                "vm_progress": vm_progress,
                "vm_boot_devs": vm_boot_devs,
            }
    finally:
        db.close()


def start_state_poller():
    thread = threading.Thread(target=_poll_loop, daemon=True, name="ws-state-poller")
    thread.start()
    return thread
