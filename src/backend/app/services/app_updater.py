"""Detect and apply Troshka app (backend/frontend) updates.

Mirrors operator_updater but targets the app's OWN images. In image mode a
daemon thread compares the running pod digest against the registry digest for
the tag each Deployment is actually pinned to (auto-detected per component, e.g.
latest for dedicated CI or production for production deploys). Dev mode compares
source mtimes against process start. Disabled where ArgoCD manages the
deployment.
"""

from __future__ import annotations

import datetime
import logging
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from app.core.config import config

logger = logging.getLogger(__name__)

_resolved_mode: str | None = None


def _configured_mode() -> str:
    return str(getattr(config.app_update, "mode", "auto") or "auto")


def _oauth_enabled() -> bool:
    return bool(config.auth.oauth_enabled)


def _get_own_namespace() -> str:
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
    # Let a failed label read propagate so resolve_mode() does not cache a
    # failure-derived "image" mode on an ArgoCD-managed cluster.
    labels = _read_own_deployment_labels()
    return "disabled" if _is_argo_managed(labels) else "image"


def resolve_mode() -> str:
    global _resolved_mode
    if _resolved_mode is None:
        try:
            _resolved_mode = _compute_mode()
        except Exception:
            # Transient in-cluster API failure: return disabled WITHOUT caching
            # so the next call recomputes and caches the correct mode.
            logger.warning("app_updater: mode resolution failed, disabling for now")
            return "disabled"
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


def _extract_tag_from_ref(ref: str) -> str | None:
    # Parse the tag from an image reference, avoiding registry-port colons and
    # digest pins. Only the final path segment can carry a ":tag" (a leading
    # "registry:port/" segment contains a "/", so it is never parsed as a tag).
    last_segment = ref.rsplit("/", 1)[-1]
    # Drop any digest pin (name@sha256:abc) so its ":" is not read as a tag.
    last_segment = last_segment.split("@", 1)[0]
    if ":" not in last_segment:
        return None
    tag = last_segment.rsplit(":", 1)[1]
    return tag or None


def _read_deployment_tag(suffix: str) -> str:
    try:
        from kubernetes import client
        from kubernetes import config as k8s_config

        k8s_config.load_incluster_config()
        apps = client.AppsV1Api()
        dep = apps.read_namespaced_deployment(
            name=suffix, namespace=_get_own_namespace()
        )
        image = dep.spec.template.spec.containers[0].image  # type: ignore[union-attr]
        return _extract_tag_from_ref(image) or _tag()
    except Exception:
        logger.debug("Failed to read %s deployment image tag", suffix, exc_info=True)
        return _tag()


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
        tag = _read_deployment_tag(suffix)
        available = _fetch_registry_digest(f"{_repo()}/{suffix}", tag)
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


_PROCESS_START = time.time()


def _backend_src_dir() -> Path:
    return Path(__file__).resolve().parents[2]  # src/backend


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]  # repo root


def _dev_up_to_date() -> bool:
    newest = 0.0
    for path in _backend_src_dir().rglob("*.py"):
        try:
            mtime = path.stat().st_mtime
            if mtime > newest:
                newest = mtime
        except OSError:
            continue
    return newest <= _PROCESS_START


def get_status() -> dict:
    mode = resolve_mode()
    if mode == "disabled":
        return {"mode": "disabled"}
    if mode == "dev":
        return {
            "mode": "dev",
            "up_to_date": _dev_up_to_date(),
            "rolling_out": False,
            "components": {},
        }
    snap = _snapshot or {"up_to_date": True, "rolling_out": False, "components": {}}
    return {"mode": "image", **snap}


def _patch_restart(name: str) -> None:
    from kubernetes import client
    from kubernetes import config as k8s_config

    k8s_config.load_incluster_config()
    apps = client.AppsV1Api()
    ts = datetime.datetime.now(datetime.UTC).isoformat()
    apps.patch_namespaced_deployment(
        name=name,
        namespace=_get_own_namespace(),
        body={
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {"kubectl.kubernetes.io/restartedAt": ts}
                    }
                }
            }
        },
    )


def _apply_image() -> dict:
    for suffix in COMPONENTS.values():
        _patch_restart(suffix)
    return {"status": "rolling_out"}


def _apply_dev() -> dict:
    subprocess.Popen(
        ["./dev-services.sh", "restart", "backend"],
        cwd=str(_repo_root()),
        start_new_session=True,
    )
    return {"status": "restarting"}


def apply_update() -> dict:
    mode = resolve_mode()
    if mode == "image":
        return _apply_image()
    if mode == "dev":
        return _apply_dev()
    raise ValueError(f"apply_update not supported in mode={mode}")
