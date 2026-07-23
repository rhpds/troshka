"""Check and update troshka-operator on KubeVirt provider clusters.

Polls the registry and running operator digests periodically so the admin
UI can show which providers are outdated. Does NOT auto-restart — the admin
triggers updates via the "Update Operator" button.
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

REGISTRY = "quay.io"
IMAGE = "redhat-gpte/troshka-operator"
TAG = "production"
POLL_INTERVAL = 300

_registry_digest: str | None = None


def get_registry_digest() -> str | None:
    """Return the cached registry digest (refreshed by poller)."""
    return _registry_digest


def _fetch_registry_digest(tag: str | None = None) -> str | None:
    """Get the current digest for the given tag from quay.io."""
    url = f"https://{REGISTRY}/v2/{IMAGE}/manifests/{tag or TAG}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            digest = resp.headers.get("Docker-Content-Digest")
            if digest:
                return digest
            body = json.loads(resp.read())
            return body.get("config", {}).get("digest")
    except Exception as e:
        logger.warning("Failed to fetch operator image digest from registry: %s", e)
        return None


def _get_operator_info(provider) -> tuple[str | None, bool, str]:
    """Get the running operator digest, rollout status, and image tag.

    Returns (digest, rolling_out, tag). The tag is read from the deployment
    spec so the registry check uses the correct tag (latest vs production).
    """
    from kubernetes import client

    creds = provider.get_credentials()
    config = client.Configuration()
    config.host = creds["api_url"]
    config.api_key = {"authorization": f"Bearer {creds['token']}"}
    config.verify_ssl = creds.get("verify_ssl", False)
    api_client = client.ApiClient(config)
    core_api = client.CoreV1Api(api_client)
    apps_api = client.AppsV1Api(api_client)

    operator_ns = creds.get("namespace", "troshka-operator")
    digest = None
    rolling_out = False
    tag = TAG

    try:
        dep = apps_api.read_namespaced_deployment(
            name="troshka-operator", namespace=operator_ns
        )
        desired = dep.spec.replicas or 1  # type: ignore[union-attr]
        updated = dep.status.updated_replicas or 0  # type: ignore[union-attr]
        ready = dep.status.ready_replicas or 0  # type: ignore[union-attr]
        if updated < desired or ready < desired:
            rolling_out = True
        image = dep.spec.template.spec.containers[0].image or ""  # type: ignore[union-attr]
        if ":" in image:
            tag = image.rsplit(":", 1)[1]
    except Exception:
        pass

    try:
        pods = core_api.list_namespaced_pod(
            namespace=operator_ns,
            label_selector="app=troshka-operator",
        )
        for pod in pods.items or []:  # type: ignore[union-attr]
            phase = pod.status.phase  # type: ignore[union-attr]
            if phase != "Running":
                continue
            for cs in pod.status.container_statuses or []:  # type: ignore[union-attr]
                if not (cs.ready and cs.started):  # type: ignore[union-attr]
                    continue
                image_id = cs.image_id or ""
                if "@sha256:" in image_id:
                    digest = "sha256:" + image_id.split("@sha256:")[-1]
    except Exception as e:
        logger.warning("Failed to get operator digest on %s: %s", provider.name, e)

    return digest, rolling_out, tag


def _poll_operator_digests():
    """Refresh the running operator digest for all kubevirt-cluster hosts."""
    global _registry_digest

    digest = _fetch_registry_digest()
    if digest:
        _registry_digest = digest

    from app.core.database import SessionLocal
    from app.models.host import Host

    db = SessionLocal()
    try:
        hosts = db.query(Host).filter(Host.host_type == "kubevirt-cluster").all()
        for host in hosts:
            if not host.provider:
                continue
            running, rolling_out, _ = _get_operator_info(host.provider)
            if rolling_out:
                continue
            if running and running != host.operator_digest:
                host.operator_digest = running
        db.commit()
    except Exception:
        logger.exception("Operator digest poll failed")
        db.rollback()
    finally:
        db.close()


def update_operator(provider) -> dict:
    """Force rollout restart of the operator deployment. Returns status dict."""
    from kubernetes import client

    creds = provider.get_credentials()
    config = client.Configuration()
    config.host = creds["api_url"]
    config.api_key = {"authorization": f"Bearer {creds['token']}"}
    config.verify_ssl = creds.get("verify_ssl", False)
    api_client = client.ApiClient(config)
    apps_api = client.AppsV1Api(api_client)

    operator_ns = creds.get("namespace", "troshka-operator")
    try:
        apps_api.patch_namespaced_deployment(
            name="troshka-operator",
            namespace=operator_ns,
            body={
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "troshka.redhat.com/restartedAt": datetime.datetime.now(
                                    datetime.UTC
                                ).isoformat()
                            }
                        }
                    }
                }
            },
        )
        logger.info("Restarted operator on %s", provider.name)
        return {
            "status": "updated",
            "registry_digest": (_registry_digest or "")[:20],
        }
    except Exception as e:
        logger.error("Failed to restart operator on %s: %s", provider.name, e)
        return {"status": "error", "message": str(e)}


def _poller_loop():
    time.sleep(10)
    logger.info("Operator updater: initial poll")
    _poll_operator_digests()

    while True:
        time.sleep(POLL_INTERVAL)
        try:
            _poll_operator_digests()
        except Exception:
            logger.exception("Operator updater poll failed")


def start_operator_updater():
    """Start the background operator updater thread. Call once at app startup."""
    thread = threading.Thread(target=_poller_loop, daemon=True, name="operator-updater")
    thread.start()
    return thread
