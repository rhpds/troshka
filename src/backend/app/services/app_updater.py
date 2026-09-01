"""Detect and apply Troshka app (backend/frontend) updates.

Mirrors operator_updater but targets the app's OWN images. In image mode a
daemon thread compares the running pod digest against the registry digest for
the tag each Deployment is actually pinned to (auto-detected per component, e.g.
latest for dedicated CI or production for production deploys). Dev mode compares
a content hash of the backend source against the hash captured at process start.
Disabled where ArgoCD manages the deployment.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from app.core.config import config
from app.core.lifecycle import LOG_PATH, RUNTIME_DIR

logger = logging.getLogger(__name__)

_resolved_mode: str | None = None


def _au(key, default):
    """Read an app_update.<key> setting, tolerant of the whole block being
    absent. Deployed ConfigMaps often omit the app_update block entirely, and
    accessing a missing top-level Dynaconf key raises AttributeError — so go
    through config.get(), which returns the default instead of raising."""
    return config.get("app_update", {}).get(key, default)


def _configured_mode() -> str:
    return str(_au("mode", "auto") or "auto")


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
    return str(_au("registry", "quay.io") or "quay.io")


def _repo() -> str:
    return str(_au("repo", "redhat-gpte") or "redhat-gpte")


def _tag() -> str:
    return str(_au("tag", "production") or "production")


def _rolling_tag() -> str:
    """Registry tag to compare against when Deployments are pinned to commit SHAs."""
    return str(_au("rolling_tag", "latest") or "latest")


_COMMIT_SHA_TAG = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)


def _is_commit_sha_tag(tag: str | None) -> bool:
    return bool(tag and _COMMIT_SHA_TAG.fullmatch(tag))


def _comparison_tag(deploy_tag: str) -> str:
    """Tag used to look up the newest image on the registry.

    Dedicated-CI rollouts often pin Deployments to a commit SHA; those immutable
    tags never move, so compare against the rolling tag (``latest``) instead.
    """
    if _is_commit_sha_tag(deploy_tag):
        return _rolling_tag()
    return deploy_tag


def _poll_interval() -> int:
    return int(_au("poll_interval", 300) or 300)


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


def _read_deployment_image(suffix: str) -> tuple[str, str]:
    """Return (container_name, image_ref) for the deployment's first container."""
    from kubernetes import client
    from kubernetes import config as k8s_config

    k8s_config.load_incluster_config()
    apps = client.AppsV1Api()
    dep = apps.read_namespaced_deployment(name=suffix, namespace=_get_own_namespace())
    container = dep.spec.template.spec.containers[0]  # type: ignore[union-attr]
    return container.name, container.image


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


def _selector_from_match_labels(match: dict | None) -> str:
    """Build a label_selector string from a Deployment's selector.matchLabels."""
    return ",".join(f"{k}={v}" for k, v in (match or {}).items())


def _read_own_digests() -> dict:
    from kubernetes import client
    from kubernetes import config as k8s_config

    from app.services.operator_updater import _extract_digest_from_pod

    k8s_config.load_incluster_config()
    core = client.CoreV1Api()
    apps = client.AppsV1Api()
    ns = _get_own_namespace()
    out: dict = {}
    for name, suffix in COMPONENTS.items():
        out[name] = None
        try:
            # Derive the pod selector from the Deployment itself so this works
            # regardless of label convention (app= vs app.kubernetes.io/name=).
            dep = apps.read_namespaced_deployment(name=suffix, namespace=ns)
            selector = _selector_from_match_labels(
                dep.spec.selector.match_labels  # type: ignore[union-attr]
            )
            if not selector:
                continue
            pods = core.list_namespaced_pod(namespace=ns, label_selector=selector)
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
        deploy_tag = _read_deployment_tag(suffix)
        compare_tag = _comparison_tag(deploy_tag)
        available = _fetch_registry_digest(f"{_repo()}/{suffix}", compare_tag)
        current = running.get(name)
        comps[name] = {
            "current": current,
            "available": available,
            "deploy_tag": deploy_tag,
            "compare_tag": compare_tag,
        }
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


def _app_src_dir() -> Path:
    return Path(__file__).resolve().parents[1]  # src/backend/app


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]  # repo root


def _compute_source_hash() -> str:
    """Content hash of the backend application source (src/backend/app/**/*.py).

    Uses file *content*, not mtime, so git checkout/pull/rebase — which rewrite
    mtimes without changing content — never spuriously report an update.
    """
    h = hashlib.sha256()
    for path in sorted(_app_src_dir().rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            h.update(path.relative_to(_app_src_dir()).as_posix().encode())
            h.update(b"\0")
            h.update(path.read_bytes())
            h.update(b"\0")
        except OSError:
            continue
    return h.hexdigest()


# Snapshot of the source at process start; dev mode compares the live hash to it.
_SOURCE_HASH_AT_START = _compute_source_hash()


def _dev_up_to_date() -> bool:
    return _compute_source_hash() == _SOURCE_HASH_AT_START


def _dev_stale_key() -> str:
    """Dismiss key for dev-mode update banner — changes when source files change."""
    return f"dev:{_compute_source_hash()}"


def get_status() -> dict:
    mode = resolve_mode()
    if mode == "disabled":
        return {"mode": "disabled"}
    if mode == "dev":
        return {
            "mode": "dev",
            "up_to_date": _dev_up_to_date(),
            "stale_key": _dev_stale_key(),
            "rolling_out": False,
            "components": {},
        }
    if mode == "image" and not _snapshot:
        try:
            _poll()
        except Exception:
            logger.debug("app_updater: lazy status poll failed", exc_info=True)
    snap = _snapshot or {"up_to_date": True, "rolling_out": False, "components": {}}
    return {"mode": "image", **snap}


def _rolling_image_ref(suffix: str) -> str:
    return f"{_registry()}/{_repo()}/{suffix}:{_rolling_tag()}"


def _patch_deployment_image(suffix: str, image: str) -> None:
    from kubernetes import client
    from kubernetes import config as k8s_config

    k8s_config.load_incluster_config()
    apps = client.AppsV1Api()
    container_name, _ = _read_deployment_image(suffix)
    ts = datetime.datetime.now(datetime.UTC).isoformat()
    apps.patch_namespaced_deployment(
        name=suffix,
        namespace=_get_own_namespace(),
        body={
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {"kubectl.kubernetes.io/restartedAt": ts}
                    },
                    "spec": {"containers": [{"name": container_name, "image": image}]},
                }
            }
        },
    )


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
        deploy_tag = _read_deployment_tag(suffix)
        if _comparison_tag(deploy_tag) != deploy_tag:
            _patch_deployment_image(suffix, _rolling_image_ref(suffix))
        else:
            _patch_restart(suffix)
    return {"status": "rolling_out"}


_RESTART_LOCK = RUNTIME_DIR / "backend-restart.lock"
_RESTART_COOLDOWN_SEC = 120


def _apply_dev(
    initiated_by: str | None = None,
    client_ip: str | None = None,
    restart_workers: bool = True,
) -> dict:
    from app.core.crash_report import log_event
    from app.core.lifecycle import audit

    audit(
        f"apply_dev spawn restart initiated_by={initiated_by or 'unknown'} "
        f"client_ip={client_ip or 'unknown'}"
    )
    log_event(
        f"apply_dev restart initiated_by={initiated_by or 'unknown'} "
        f"client_ip={client_ip or 'unknown'}"
    )

    now = time.time()
    try:
        if _RESTART_LOCK.exists():
            age = now - _RESTART_LOCK.stat().st_mtime
            if age < _RESTART_COOLDOWN_SEC:
                logger.warning(
                    "apply_dev: restart skipped — lock age %.0fs (cooldown %ds)",
                    age,
                    _RESTART_COOLDOWN_SEC,
                )
                audit(f"apply_dev skipped lock_age={age:.0f}s")
                return {"status": "restarting"}
    except OSError:
        pass

    try:
        _RESTART_LOCK.parent.mkdir(parents=True, exist_ok=True)
        _RESTART_LOCK.write_text(f"{now}\n{initiated_by or ''}\n{client_ip or ''}\n")
    except OSError:
        logger.warning("apply_dev: could not write restart lock file")

    repo = _repo_root()
    dev_services = repo / "dev-services.sh"
    env = os.environ.copy()
    env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
    # The backend runs with these "already detached" markers set by its own
    # supervisor. If inherited, the freshly-spawned supervise-backend.py /
    # supervise-worker.py skip re-detaching and run in the foreground, so
    # dev-services.sh never returns and the restart hangs. Drop them.
    env.pop("TROSHKA_SUPERVISOR_DETACHED", None)
    env.pop("TROSHKA_WORKER_SUPERVISOR_DETACHED", None)
    target = "backend-worker" if restart_workers else "backend"
    _spawn_dev_services_restart(dev_services, repo, env, LOG_PATH, target=target)
    return {"status": "restarting"}


def _spawn_dev_services_restart(
    dev_services: Path,
    repo: Path,
    env: dict[str, str],
    log_path: Path,
    target: str = "backend",
) -> None:
    """Restart backend without fork() in this threaded process (macOS aborts on fork)."""
    argv = [str(dev_services), "restart", target]
    log_str = str(log_path)
    if hasattr(os, "posix_spawn"):
        log_fd = os.open(log_str, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            file_actions = [
                (
                    os.POSIX_SPAWN_OPEN,
                    log_fd,
                    log_str,
                    os.O_WRONLY | os.O_APPEND,
                    0o644,
                ),
                (os.POSIX_SPAWN_DUP2, log_fd, 1),
                (os.POSIX_SPAWN_DUP2, log_fd, 2),
                (os.POSIX_SPAWN_CLOSE, log_fd),
            ]
            os.posix_spawn(
                str(dev_services),
                argv,
                env,
                file_actions=file_actions,
                setsid=True,
            )
        finally:
            os.close(log_fd)
        return
    log_fd = log_path.open("a", encoding="utf-8")
    try:
        subprocess.Popen(
            argv,
            cwd=str(repo),
            start_new_session=True,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            close_fds=True,
            env=env,
        )
    finally:
        log_fd.close()


def apply_update(
    initiated_by: str | None = None,
    client_ip: str | None = None,
    restart_workers: bool = True,
) -> dict:
    from app.core.lifecycle import audit

    mode = resolve_mode()
    audit(f"apply_update mode={mode} initiated_by={initiated_by or 'unknown'}")
    if mode == "image":
        return _apply_image()
    if mode == "dev":
        return _apply_dev(
            initiated_by=initiated_by,
            client_ip=client_ip,
            restart_workers=restart_workers,
        )
    raise ValueError(f"apply_update not supported in mode={mode}")
