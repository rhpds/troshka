"""StarGate integration client.

Creates runs, submits evidence, and triggers evaluations in StarGate
to get structured failure classification for troshka deployments.
"""

import logging
import uuid
from datetime import datetime, timezone

from app.core.config import config
from app.services.event_publisher import _get_session, _ssl_verify

logger = logging.getLogger("troshka.stargate")

_run_ids: dict[str, str] = {}


def _stargate_url() -> str:
    return config.get("integration.stargate_url", "")


def _api_key() -> str:
    return config.get("integration.api_key", "")


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    api_key = _api_key()
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _is_enabled() -> bool:
    return config.get("integration.enabled", False) and bool(_stargate_url())


def create_run(project_id: str, project_name: str, host_name: str = "", provider_name: str = "") -> str | None:
    """Create a StarGate run for a troshka deployment. Returns run_id."""
    if not _is_enabled():
        return None
    try:
        run_id = f"troshka-{project_id[:8]}-{uuid.uuid4().hex[:8]}"
        resp = _get_session().post(
            f"{_stargate_url()}/runs",
            json={
                "run_id": run_id,
                "demo_id": f"troshka-{project_name}",
                "namespace": f"troshka-{project_id[:8]}",
                "requested_by": "troshka",
                "rubric_version": "troshka-deploy-v1",
                "lab_code": project_name,
                "cluster_name": provider_name or host_name,
            },
            headers=_headers(),
            timeout=10,
            verify=_ssl_verify(),
        )
        if resp.status_code in (200, 201):
            _run_ids[project_id] = run_id
            logger.debug("StarGate run created: %s", run_id)
            return run_id
        logger.warning("StarGate run creation failed: %s %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.warning("StarGate run creation failed (non-critical): %s", e)
    return None


def submit_evidence(project_id: str, stage: str, observed: dict, result: str = "pass") -> None:
    """Submit evidence for a deploy stage."""
    if not _is_enabled():
        return
    run_id = _run_ids.get(project_id)
    if not run_id:
        return
    try:
        _get_session().post(
            f"{_stargate_url()}/runs/{run_id}/stages/{stage}/evidence",
            json={
                "type": "troshka_deploy",
                "source": "troshka",
                "observed": observed,
                "result": result,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            headers=_headers(),
            timeout=10,
            verify=_ssl_verify(),
        )
    except Exception as e:
        logger.warning("StarGate evidence submission failed for %s/%s: %s", run_id, stage, e)


def evaluate_stage(project_id: str, stage: str) -> dict | None:
    """Trigger rubric evaluation for a stage. Returns evaluation result."""
    if not _is_enabled():
        return None
    run_id = _run_ids.get(project_id)
    if not run_id:
        return None
    try:
        resp = _get_session().post(
            f"{_stargate_url()}/runs/{run_id}/stages/{stage}/evaluate",
            json={},
            headers=_headers(),
            timeout=10,
            verify=_ssl_verify(),
        )
        if resp.status_code == 200:
            result = resp.json()
            logger.debug("StarGate evaluation %s/%s: %s", run_id, stage, result.get("outcome"))
            return result
    except Exception as e:
        logger.warning("StarGate evaluation failed for %s/%s: %s", run_id, stage, e)
    return None


def complete_run(project_id: str) -> None:
    """Clean up run tracking for a completed project."""
    _run_ids.pop(project_id, None)
