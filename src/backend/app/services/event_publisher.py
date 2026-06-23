"""Outbound event publisher for platform integration.

Pushes troshka lifecycle events to DeepField and StarGate.
Fails silently — never blocks deploys or state transitions.
"""

import logging
import uuid
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import config

logger = logging.getLogger("troshka.integration")

_session = None

DEEPFIELD_SIGNAL_MAP = {
    "deploy.failed": ("vm_failed", "high"),
    "deploy.step_failed": ("vm_failed", "high"),
    "deploy.completed": ("vm_running", "info"),
    "vm.state_changed.stopped": ("vm_failed", "high"),
    "vm.state_changed.crashed": ("vm_failed", "high"),
    "host.health_warning.critical": ("node_pressure", "critical"),
    "host.health_warning.warning": ("node_pressure", "medium"),
}


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        _session.mount("https://", adapter)
        _session.mount("http://", adapter)
    return _session


def _is_enabled() -> bool:
    return config.get("integration.enabled", False)


def _deepfield_url() -> str:
    return config.get("integration.deepfield_url", "")


def _stargate_url() -> str:
    return config.get("integration.stargate_url", "")


def _api_key() -> str:
    return config.get("integration.api_key", "")


def _ssl_verify() -> bool:
    return config.get("integration.ssl_verify", True)


def publish_event(project_id: str, event_type: str, payload: dict) -> None:
    """Push a lifecycle event to all configured integration targets."""
    if not _is_enabled():
        return

    payload = {
        **payload,
        "project_id": project_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    _push_to_deepfield(event_type, payload)


def _push_to_deepfield(event_type: str, payload: dict) -> None:
    """Push event to DeepField as an integration event."""
    url = _deepfield_url()
    if not url:
        return

    signal_key = event_type
    if event_type == "vm.state_changed":
        new_state = payload.get("new_state", "")
        signal_key = f"vm.state_changed.{new_state}"
    elif event_type == "host.health_warning":
        severity = payload.get("severity", "warning")
        signal_key = f"host.health_warning.{severity}"

    mapping = DEEPFIELD_SIGNAL_MAP.get(signal_key)
    if not mapping:
        return

    signal_type, severity = mapping

    event = {
        "source": "troshka",
        "event_type": f"troshka.{event_type}",
        "event_id": str(uuid.uuid4()),
        "timestamp": payload.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "payload": {
            "signal_type": signal_type,
            "severity": severity,
            "namespace": f"troshka-{payload.get('project_id', 'unknown')[:8]}",
            "resource_kind": "VirtualMachine",
            "resource_name": payload.get("vm_name", payload.get("project_id", "")),
            "evidence": {
                k: v for k, v in payload.items()
                if k not in ("project_id", "timestamp")
            },
        },
    }

    _post(f"{url}/integration/events", event)


def _post(url: str, payload: dict) -> None:
    """POST JSON to a URL. Never raises."""
    try:
        headers = {"Content-Type": "application/json"}
        api_key = _api_key()
        if api_key:
            headers["X-API-Key"] = api_key

        resp = _get_session().post(
            url, json=payload, headers=headers,
            timeout=10, verify=_ssl_verify(),
        )
        logger.debug("Event pushed to %s: %s → %s", url, payload.get("event_type"), resp.status_code)
    except Exception as e:
        logger.warning("Event push to %s failed (non-critical): %s", url, e)
