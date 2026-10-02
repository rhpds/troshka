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


def _running_in_cluster() -> bool:
    """True when the backend is running inside a Kubernetes pod.

    Used so oauth-off (EKS basic-auth quickstart, local helm) still gets image
    digest updates, while laptop ``./dev-services.sh`` keeps source-hash ``dev``
    mode when oauth is disabled.
    """
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return True
    try:
        return Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace").is_file()
    except Exception:
        return False


def _compose_update_enabled() -> bool:
    """Local Compose quickstart: host helper + shared updater dir."""
    return os.environ.get("TROSHKA_COMPOSE_UPDATE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _compose_dir() -> Path:
    return Path(
        os.environ.get("TROSHKA_COMPOSE_DIR", "/var/lib/troshka/compose-updater")
    )


def _get_own_namespace() -> str:
    ns = os.environ.get("POD_NAMESPACE")
    if ns:
        return ns
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as f:
            return f.read().strip()
    except Exception:
        return "troshka"


def _read_own_deployment_meta() -> tuple[dict, dict]:
    """``(labels, annotations)`` of our own Deployment (in-cluster)."""
    from kubernetes import client
    from kubernetes import config as k8s_config

    k8s_config.load_incluster_config()
    apps = client.AppsV1Api()
    dep = apps.read_namespaced_deployment("troshka-backend", _get_own_namespace())
    md = dep.metadata  # type: ignore[union-attr]
    return (md.labels or {}), (md.annotations or {})  # type: ignore[union-attr]


def _is_argo_managed(labels: dict, annotations: dict | None = None) -> bool:
    """True if ArgoCD manages this Deployment.

    ArgoCD marks managed resources with EITHER the instance LABEL
    (``argocd.argoproj.io/instance``, label-based tracking) OR the tracking-id
    ANNOTATION (``argocd.argoproj.io/tracking-id``, annotation-based tracking —
    the default on newer ArgoCD, e.g. infra01). Detect both so an
    annotation-tracked prod correctly disables the in-app updater — ArgoCD Image
    Updater handles rollouts there, so the in-app "Apply update" button must not
    show (it only applies on dedicated CI / local dev).
    """
    if "argocd.argoproj.io/instance" in (labels or {}):
        return True
    return "argocd.argoproj.io/tracking-id" in (annotations or {})


def _compute_mode() -> str:
    configured = _configured_mode()
    if configured != "auto":
        return configured
    # Local Compose quickstart (host helper for pull+up) before oauth-off→dev.
    if _compose_update_enabled() and not _running_in_cluster():
        return "compose"
    # Laptop / ./dev-services.sh: oauth-off → source-hash "dev" mode.
    # In-cluster (EKS basic-auth, etc.): skip this so digest updates still run.
    if not _oauth_enabled() and not _running_in_cluster():
        return "dev"
    # Let a failed metadata read propagate so resolve_mode() does not cache a
    # failure-derived "image" mode on an ArgoCD-managed cluster.
    labels, annotations = _read_own_deployment_meta()
    return "disabled" if _is_argo_managed(labels, annotations) else "image"


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
_ROLLOUT_DEPLOYMENTS = list(COMPONENTS.values()) + ["troshka-worker"]

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
    for suffix in _ROLLOUT_DEPLOYMENTS:
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


# ---------------------------------------------------------------------------
# Stuck rollout self-heal (Multus FailedCreatePodSandBox during Apply update)
# ---------------------------------------------------------------------------

ANN_ROLLOUT_ATTEMPTS = "troshka.io/app-rollout-reschedule-attempts"
ANN_ROLLOUT_STUCK_NODES = "troshka.io/app-rollout-stuck-nodes"

_ROLLOUT_STUCK_THRESHOLD_S = 90
_ROLLOUT_MAX_ATTEMPTS = 3
_ROLLOUT_NODE_EXCLUDE_AFTER = 2
_ROLLOUT_POLL_INTERVAL_S = 30


def _pod_is_scheduled(pod) -> bool:
    for cond in getattr(getattr(pod, "status", None), "conditions", None) or []:
        if (
            getattr(cond, "type", None) == "PodScheduled"
            and getattr(cond, "status", None) == "True"
        ):
            return True
    return False


def _pod_has_running_container(pod) -> bool:
    for cs in getattr(getattr(pod, "status", None), "container_statuses", None) or []:
        state = getattr(cs, "state", None)
        if state is not None and getattr(state, "running", None) is not None:
            return True
    return False


def _is_rollout_pod_stuck(
    pod, *, now=None, threshold_s=_ROLLOUT_STUCK_THRESHOLD_S
) -> bool:
    """True when scheduled past threshold with no running container (Multus hang)."""
    if now is None:
        now = time.time()
    if getattr(getattr(pod, "metadata", None), "deletion_timestamp", None) is not None:
        return False
    created = getattr(getattr(pod, "metadata", None), "creation_timestamp", None)
    if created is None:
        return False
    if (now - created.timestamp()) < threshold_s:
        return False
    if not _pod_is_scheduled(pod):
        return False
    phase = str(getattr(getattr(pod, "status", None), "phase", "") or "").lower()
    if phase in ("running", "succeeded", "failed"):
        return False
    if _pod_has_running_container(pod):
        return False
    return True


def _parse_stuck_node_counts(annotations: dict | None) -> dict[str, int]:
    raw = (annotations or {}).get(ANN_ROLLOUT_STUCK_NODES, "")
    if not raw:
        return {}
    counts: dict[str, int] = {}
    for part in raw.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        host, _, n = part.partition(":")
        try:
            counts[host] = int(n)
        except ValueError:
            continue
    return counts


def _format_stuck_node_counts(counts: dict[str, int]) -> str:
    return ",".join(f"{h}:{c}" for h, c in sorted(counts.items()))


def _plan_rollout_heal(annotations: dict | None, node: str) -> dict:
    """Pure: decide reschedule vs exhausted and next annotations/exclude."""
    annotations = dict(annotations or {})
    attempts = int(annotations.get(ANN_ROLLOUT_ATTEMPTS, "0") or "0")
    if attempts >= _ROLLOUT_MAX_ATTEMPTS:
        return {
            "action": "exhausted",
            "message": (
                f"Update pods stuck on Multus ({node}) after {attempts} "
                "reschedule attempts"
            ),
            "attempts": attempts,
            "exclude": [],
            "annotations": annotations,
        }

    counts = _parse_stuck_node_counts(annotations)
    counts[node] = counts.get(node, 0) + 1
    attempts += 1
    next_ann = dict(annotations)
    next_ann[ANN_ROLLOUT_STUCK_NODES] = _format_stuck_node_counts(counts)
    next_ann[ANN_ROLLOUT_ATTEMPTS] = str(attempts)
    exclude = [h for h, c in counts.items() if c >= _ROLLOUT_NODE_EXCLUDE_AFTER]
    return {
        "action": "reschedule",
        "attempts": attempts,
        "exclude": exclude,
        "annotations": next_ann,
        "detail": f"rescheduled off {node} (attempt {attempts})",
    }


def _node_hostname_not_in_affinity(hostnames: list[str]) -> dict:
    return {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "kubernetes.io/hostname",
                                "operator": "NotIn",
                                "values": list(hostnames),
                            }
                        ]
                    }
                ]
            }
        }
    }


def _deployment_name_for_rs(apps_api, namespace: str, rs_name: str) -> str | None:
    try:
        rs = apps_api.read_namespaced_replica_set(name=rs_name, namespace=namespace)
    except Exception:  # noqa: BLE001
        return None
    for ref in getattr(getattr(rs, "metadata", None), "owner_references", None) or []:
        if (
            getattr(ref, "controller", False)
            and getattr(ref, "kind", None) == "Deployment"
        ):
            return str(ref.name)
    return None


def _controller_owner(pod) -> tuple[str, str] | None:
    for ref in getattr(getattr(pod, "metadata", None), "owner_references", None) or []:
        if not getattr(ref, "controller", False):
            continue
        kind = str(getattr(ref, "kind", "") or "")
        name = str(getattr(ref, "name", "") or "")
        if kind and name:
            return kind, name
    return None


def _patch_rollout_deployment_heal(apps_api, namespace, deploy_name, plan) -> None:
    try:
        dep = apps_api.read_namespaced_deployment(name=deploy_name, namespace=namespace)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "app_updater: read Deployment %s/%s for heal: %s", namespace, deploy_name, e
        )
        return

    meta_ann = dict(getattr(getattr(dep, "metadata", None), "annotations", None) or {})
    meta_ann.update(plan["annotations"])
    patch: dict = {"metadata": {"annotations": meta_ann}}
    if plan["exclude"]:
        affinity = _node_hostname_not_in_affinity(plan["exclude"])
        bump = plan["annotations"].get(ANN_ROLLOUT_ATTEMPTS, "0")
        patch["spec"] = {
            "template": {
                "metadata": {"annotations": {"troshka.io/app-rollout-heal-bump": bump}},
                "spec": {"affinity": affinity},
            }
        }
    try:
        apps_api.patch_namespaced_deployment(
            name=deploy_name, namespace=namespace, body=patch
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "app_updater: patch Deployment %s/%s heal failed: %s",
            namespace,
            deploy_name,
            e,
        )


def _heal_one_stuck_rollout_pod(core_api, apps_api, namespace, pod) -> str | None:
    """Force-delete a stuck creating pod; return exhausted error or None."""
    node = getattr(getattr(pod, "spec", None), "node_name", None) or "unknown"
    owner = _controller_owner(pod)
    deploy_name = None
    annotations: dict = {}
    if owner:
        kind, name = owner
        if kind == "Deployment":
            deploy_name = name
        elif kind == "ReplicaSet":
            deploy_name = _deployment_name_for_rs(apps_api, namespace, name)
    if deploy_name:
        try:
            dep = apps_api.read_namespaced_deployment(
                name=deploy_name, namespace=namespace
            )
            annotations = dict(
                getattr(getattr(dep, "metadata", None), "annotations", None) or {}
            )
        except Exception:  # noqa: BLE001
            annotations = {}

    plan = _plan_rollout_heal(annotations, node)
    if plan["action"] == "exhausted":
        return plan["message"]

    if deploy_name:
        _patch_rollout_deployment_heal(apps_api, namespace, deploy_name, plan)
    try:
        core_api.delete_namespaced_pod(
            name=pod.metadata.name, namespace=namespace, grace_period_seconds=0
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "app_updater: force-delete stuck pod %s/%s: %s",
            namespace,
            getattr(pod.metadata, "name", "?"),
            e,
        )
        return None
    logger.info(
        "app_updater: healed stuck rollout pod %s off %s (%s)",
        pod.metadata.name,
        node,
        plan["detail"],
    )
    return None


def _list_rollout_deploy_pods(core_api, apps_api, namespace: str, deploy_name: str):
    """Return pods for a rollout Deployment, or None if listing fails."""
    try:
        dep = apps_api.read_namespaced_deployment(name=deploy_name, namespace=namespace)
        selector = _selector_from_match_labels(
            dep.spec.selector.match_labels  # type: ignore[union-attr]
        )
        if not selector:
            return None
        pods = core_api.list_namespaced_pod(
            namespace=namespace, label_selector=selector
        )
        return pods.items or []  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        logger.debug(
            "app_updater: list pods for %s heal failed", deploy_name, exc_info=True
        )
        return None


def _heal_stuck_pods_for_deploy(
    core_api, apps_api, namespace: str, deploy_name: str, errors: list[str]
) -> None:
    """Heal Multus-stuck pods for one Deployment; append exhausted errors."""
    pods = _list_rollout_deploy_pods(core_api, apps_api, namespace, deploy_name)
    if pods is None:
        return
    for pod in pods:
        if not _is_rollout_pod_stuck(pod):
            continue
        try:
            err = _heal_one_stuck_rollout_pod(core_api, apps_api, namespace, pod)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "app_updater: heal %s failed: %s",
                getattr(getattr(pod, "metadata", None), "name", "?"),
                e,
            )
            continue
        if err:
            errors.append(err)


def _heal_stuck_rollout_pods() -> str | None:
    """Detect Multus-stuck app pods and reschedule; return error if exhausted."""
    from kubernetes import client
    from kubernetes import config as k8s_config

    try:
        k8s_config.load_incluster_config()
        core = client.CoreV1Api()
        apps = client.AppsV1Api()
    except Exception:  # noqa: BLE001
        logger.debug("app_updater: heal skipped (no in-cluster config)", exc_info=True)
        return None

    ns = _get_own_namespace()
    errors: list[str] = []
    for deploy_name in _ROLLOUT_DEPLOYMENTS:
        _heal_stuck_pods_for_deploy(core, apps, ns, deploy_name, errors)
    return errors[0] if errors else None


def _build_image_snapshot() -> dict:
    running = _read_own_digests()
    rolling = _read_rolling_out()
    rollout_error = None
    if rolling:
        try:
            rollout_error = _heal_stuck_rollout_pods()
        except Exception:  # noqa: BLE001
            logger.exception("app_updater: stuck rollout heal failed")
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
    return {
        "up_to_date": up_to_date,
        "rolling_out": rolling,
        "rollout_error": rollout_error,
        "components": comps,
    }


def _read_compose_kv_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip()
    return out


def _compose_tag() -> str:
    kv = _read_compose_kv_file(_compose_dir() / "running-digests")
    return kv.get("tag") or os.environ.get("TROSHKA_IMAGE_TAG") or _rolling_tag()


def _read_compose_status_line() -> tuple[bool, str | None]:
    """Return (rolling_out, rollout_error) from the host helper status file."""
    path = _compose_dir() / "update-status"
    try:
        line = path.read_text(encoding="utf-8").strip().splitlines()[0]
    except (OSError, IndexError):
        return False, None
    if line == "rolling_out":
        return True, None
    if line.startswith("error:"):
        return False, line[len("error:") :].strip() or "compose update failed"
    return False, None


def _build_compose_snapshot() -> dict:
    kv = _read_compose_kv_file(_compose_dir() / "running-digests")
    tag = _compose_tag()
    rolling, rollout_error = _read_compose_status_line()
    # Pending request also means a rollout is in flight (helper may not have
    # flipped status yet).
    if (_compose_dir() / "update-request").is_file():
        rolling = True
    comps: dict = {}
    up_to_date = True
    for name, suffix in COMPONENTS.items():
        available = _fetch_registry_digest(f"{_repo()}/{suffix}", tag)
        current = kv.get(name) or None
        if current == "":
            current = None
        comps[name] = {
            "current": current,
            "available": available,
            "deploy_tag": tag,
            "compare_tag": tag,
        }
        if current and available and current != available:
            up_to_date = False
    return {
        "up_to_date": up_to_date,
        "rolling_out": rolling,
        "rollout_error": rollout_error,
        "components": comps,
        "stale_key": f"compose:{tag}:{_compose_components_key(comps)}",
    }


def _compose_components_key(comps: dict) -> str:
    """Stable dismiss key fragment from component digest pairs."""
    parts = []
    for name in sorted(comps):
        c = comps[name] or {}
        parts.append(f"{name}:{c.get('current') or ''}:{c.get('available') or ''}")
    return "|".join(parts)


def _poll() -> None:
    global _snapshot
    try:
        mode = resolve_mode()
        if mode == "compose":
            _snapshot = _build_compose_snapshot()
        else:
            _snapshot = _build_image_snapshot()
    except Exception:
        logger.exception("app update poll failed")


def _next_poll_sleep() -> int:
    """Poll faster while a rollout is in progress so Multus hangs heal quickly."""
    if (_snapshot or {}).get("rolling_out"):
        return _ROLLOUT_POLL_INTERVAL_S
    return _poll_interval()


def _poller_loop() -> None:
    time.sleep(10)
    mode = resolve_mode()
    if mode not in ("image", "compose"):
        logger.info("app_updater: mode=%s, polling disabled", mode)
        return
    _poll()
    while True:
        time.sleep(_next_poll_sleep())
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
    if mode in ("image", "compose") and not _snapshot:
        try:
            _poll()
        except Exception:
            logger.debug("app_updater: lazy status poll failed", exc_info=True)
    snap = _snapshot or {
        "up_to_date": True,
        "rolling_out": False,
        "rollout_error": None,
        "components": {},
    }
    return {"mode": mode, **snap}


def _rolling_image_ref(suffix: str) -> str:
    return f"{_registry()}/{_repo()}/{suffix}:{_rolling_tag()}"


def image_ref(suffix: str) -> str:
    """Full image reference for a troshka component at the configured deploy tag.

    Uses ``app_update`` registry/repo/tag (e.g. quay.io/redhat-gpte/<suffix>:
    production) so runtime-built pod specs match what the operator deploys,
    instead of hardcoding a tag that may not exist in the registry."""
    return f"{_registry()}/{_repo()}/{suffix}:{_tag()}"


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


_WORKER_DEPLOYMENT = "troshka-worker"


def _apply_worker_image() -> None:
    """Roll out the worker deployment too.

    The worker runs the BACKEND image (worker-deployment.yaml uses
    troshka.backendImage) but is a separate deployment, so updating only
    backend/frontend leaves the worker on the old code — backend/worker version
    skew that breaks deploy jobs (e.g. a signature that changed between them).
    Patch it with the backend image ref, or restart it if the tag is unchanged.
    Best-effort: deployments without a separate worker are skipped.
    """
    backend = COMPONENTS["backend"]
    try:
        deploy_tag = _read_deployment_tag(_WORKER_DEPLOYMENT)
        if _comparison_tag(deploy_tag) != deploy_tag:
            _patch_deployment_image(_WORKER_DEPLOYMENT, _rolling_image_ref(backend))
        else:
            _patch_restart(_WORKER_DEPLOYMENT)
    except Exception:
        logger.warning("app_updater: worker rollout skipped", exc_info=True)


def _apply_image() -> dict:
    for suffix in COMPONENTS.values():
        deploy_tag = _read_deployment_tag(suffix)
        if _comparison_tag(deploy_tag) != deploy_tag:
            _patch_deployment_image(suffix, _rolling_image_ref(suffix))
        else:
            _patch_restart(suffix)
    _apply_worker_image()
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
    current_hash = _compute_source_hash()
    try:
        if _RESTART_LOCK.exists():
            age = now - _RESTART_LOCK.stat().st_mtime
            # The cooldown guards against accidental DOUBLE restarts of the same
            # source. A genuine new code change (hash differs from the last
            # restart) must still restart — otherwise the fresh process never
            # loads it and the UI spins until timeout ("still out of date"). So
            # only skip when both within cooldown AND the source is unchanged.
            lock_lines = _RESTART_LOCK.read_text().splitlines()
            lock_hash = lock_lines[3] if len(lock_lines) > 3 else ""
            if age < _RESTART_COOLDOWN_SEC and lock_hash == current_hash:
                logger.warning(
                    "apply_dev: restart skipped — duplicate within cooldown "
                    "(age %.0fs, source unchanged)",
                    age,
                )
                audit(f"apply_dev skipped duplicate lock_age={age:.0f}s")
                return {"status": "restarting"}
    except OSError:
        pass

    try:
        _RESTART_LOCK.parent.mkdir(parents=True, exist_ok=True)
        _RESTART_LOCK.write_text(
            f"{now}\n{initiated_by or ''}\n{client_ip or ''}\n{current_hash}\n"
        )
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


def _apply_compose() -> dict:
    """Ask the host compose-updater helper to pull and recreate containers."""
    d = _compose_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValueError(f"compose updater dir not writable: {d}") from e
    req = d / "update-request"
    status = d / "update-status"
    try:
        status.write_text("rolling_out\n", encoding="utf-8")
        req.write_text(f"{time.time()}\n", encoding="utf-8")
    except OSError as e:
        raise ValueError(f"failed to request compose update: {e}") from e
    global _snapshot
    _snapshot = {**(_snapshot or {}), "rolling_out": True, "rollout_error": None}
    return {"status": "rolling_out"}


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
    if mode == "compose":
        return _apply_compose()
    if mode == "dev":
        return _apply_dev(
            initiated_by=initiated_by,
            client_ip=client_ip,
            restart_workers=restart_workers,
        )
    raise ValueError(f"apply_update not supported in mode={mode}")
