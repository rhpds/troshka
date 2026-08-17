"""Detect and apply Troshka app (backend/frontend) updates.

Mirrors operator_updater but targets the app's OWN images. In image mode a
daemon thread compares the running pod digest against the production tag digest
on quay.io. Dev mode compares source mtimes against process start. Disabled
where ArgoCD manages the deployment.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.request

from app.core.config import config

logger = logging.getLogger(__name__)

_resolved_mode: str | None = None


def _configured_mode() -> str:
    return str(getattr(config.app_update, "mode", "auto") or "auto")


def _oauth_enabled() -> bool:
    return bool(config.auth.oauth_enabled)


def _get_own_namespace() -> str:
    import os

    ns = os.environ.get("POD_NAMESPACE")
    if ns:
        return ns
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as f:
            return f.read().strip()
    except Exception:
        return "troshka"


def _read_own_deployment_labels() -> dict:
    from kubernetes import client
    from kubernetes import config as k8s_config

    k8s_config.load_incluster_config()
    apps = client.AppsV1Api()
    dep = apps.read_namespaced_deployment("troshka-backend", _get_own_namespace())
    return dep.metadata.labels or {}  # type: ignore[union-attr]


def _is_argo_managed(labels: dict) -> bool:
    return "argocd.argoproj.io/instance" in (labels or {})


def _compute_mode() -> str:
    configured = _configured_mode()
    if configured != "auto":
        return configured
    if not _oauth_enabled():
        return "dev"
    try:
        labels = _read_own_deployment_labels()
    except Exception:
        labels = {}
    return "disabled" if _is_argo_managed(labels) else "image"


def resolve_mode() -> str:
    global _resolved_mode
    if _resolved_mode is None:
        _resolved_mode = _compute_mode()
    return _resolved_mode


COMPONENTS = {
    "backend": "troshka-backend",
    "frontend": "troshka-frontend",
}

_snapshot: dict = {}


def _registry() -> str:
    return str(getattr(config.app_update, "registry", "quay.io") or "quay.io")


def _repo() -> str:
    return str(getattr(config.app_update, "repo", "redhat-gpte") or "redhat-gpte")


def _tag() -> str:
    return str(getattr(config.app_update, "tag", "production") or "production")


def _poll_interval() -> int:
    return int(getattr(config.app_update, "poll_interval", 300) or 300)


def _fetch_registry_digest(image: str, tag: str) -> str | None:
    url = f"https://{_registry()}/v2/{image}/manifests/{tag}"
    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.oci.image.manifest.v1+json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            # Only trust the manifest digest header. Falling back to the image
            # config-blob digest here would compare against the pod's manifest
            # digest and produce a false "update available".
            return resp.headers.get("Docker-Content-Digest")
    except Exception as e:
        logger.warning("Failed to fetch %s:%s digest from registry: %s", image, tag, e)
        return None


def _read_own_digests() -> dict:
    from kubernetes import client
    from kubernetes import config as k8s_config

    from app.services.operator_updater import _extract_digest_from_pod

    k8s_config.load_incluster_config()
    core = client.CoreV1Api()
    ns = _get_own_namespace()
    out: dict = {}
    for name in COMPONENTS:
        out[name] = None
        try:
            pods = core.list_namespaced_pod(
                namespace=ns, label_selector=f"app=troshka-{name}"
            )
            for pod in pods.items or []:  # type: ignore[union-attr]
                digest = _extract_digest_from_pod(pod)
                if digest:
                    out[name] = digest
                    break
        except Exception:
            logger.debug("Failed to read %s pod digest", name, exc_info=True)
    return out


def _read_rolling_out() -> bool:
    from kubernetes import client
    from kubernetes import config as k8s_config

    k8s_config.load_incluster_config()
    apps = client.AppsV1Api()
    ns = _get_own_namespace()
    for suffix in COMPONENTS.values():
        try:
            dep = apps.read_namespaced_deployment(name=suffix, namespace=ns)
            desired = dep.spec.replicas or 1  # type: ignore[union-attr]
            updated = dep.status.updated_replicas or 0  # type: ignore[union-attr]
            ready = dep.status.ready_replicas or 0  # type: ignore[union-attr]
            if updated < desired or ready < desired:
                return True
        except Exception:
            logger.debug("Failed to read %s rollout status", suffix, exc_info=True)
    return False


def _build_image_snapshot() -> dict:
    running = _read_own_digests()
    rolling = _read_rolling_out()
    comps: dict = {}
    up_to_date = True
    for name, suffix in COMPONENTS.items():
        available = _fetch_registry_digest(f"{_repo()}/{suffix}", _tag())
        current = running.get(name)
        comps[name] = {"current": current, "available": available}
        if current and available and current != available:
            up_to_date = False
    return {"up_to_date": up_to_date, "rolling_out": rolling, "components": comps}


def _poll() -> None:
    global _snapshot
    try:
        _snapshot = _build_image_snapshot()
    except Exception:
        logger.exception("app update poll failed")


def _poller_loop() -> None:
    time.sleep(10)
    mode = resolve_mode()
    if mode != "image":
        logger.info("app_updater: mode=%s, polling disabled", mode)
        return
    _poll()
    while True:
        time.sleep(_poll_interval())
        try:
            _poll()
        except Exception:
            logger.exception("app update poll failed")


def start_app_updater() -> threading.Thread:
    """Start the background updater. All k8s work happens inside the thread."""
    thread = threading.Thread(target=_poller_loop, daemon=True, name="app-updater")
    thread.start()
    return thread
