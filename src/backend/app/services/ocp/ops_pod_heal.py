"""Heal KubeVirt ops pods stuck in ContainerCreating / sandbox setup.

Mirrors the operator's virt-launcher stuck-heal: detect a scheduled pod that
never becomes Ready, delete it, and recreate with ``NotIn`` affinity after
repeated hits on the same node. Caps attempts so Multus sandbox
``DeadlineExceeded`` cannot hang the install monitor forever.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

ANN_STUCK_NODES = "troshka.io/ops-stuck-nodes"
ANN_ATTEMPTS = "troshka.io/ops-reschedule-attempts"
_STUCK_OPS_MAX_ATTEMPTS = 3
_STUCK_NODE_EXCLUDE_AFTER = 2
# Longer than a normal image pull; Multus sandbox failures hang well past this.
_STUCK_OPS_THRESHOLD_S = 180


def _is_ops_pod_stuck_creating(
    pod, *, now=None, threshold_s=_STUCK_OPS_THRESHOLD_S
) -> bool:
    """True when a scheduled ops pod has been creating / Pending too long."""
    if now is None:
        now = time.time()
    created = getattr(getattr(pod, "metadata", None), "creation_timestamp", None)
    if created is None:
        return False
    age = now - created.timestamp()
    if age < threshold_s:
        return False
    if not _pod_is_scheduled(pod):
        return False
    phase = str(getattr(getattr(pod, "status", None), "phase", "") or "").lower()
    if phase in ("running", "succeeded", "failed"):
        return False
    if _pod_has_running_container(pod):
        return False
    # Pending + scheduled + no running container past threshold: stuck creating
    # (ContainerCreating) or Multus sandbox never came up (empty statuses).
    return True


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


def _parse_stuck_node_counts(annotations: dict | None) -> dict[str, int]:
    """Parse ``host:count,host:count`` from the stuck-nodes annotation."""
    raw = (annotations or {}).get(ANN_STUCK_NODES, "")
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
    """Serialize stuck-node counts as ``host:count,host:count``."""
    return ",".join(f"{h}:{c}" for h, c in sorted(counts.items()))


def _node_hostname_not_in_affinity(hostnames: list[str]) -> dict[str, Any]:
    """Build a required NotIn affinity for kubernetes.io/hostname."""
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


def plan_ops_pod_heal(annotations: dict | None, node: str) -> dict[str, Any]:
    """Pure: decide reschedule vs exhausted and compute next annotations/exclude."""
    annotations = dict(annotations or {})
    attempts = int(annotations.get(ANN_ATTEMPTS, "0") or "0")
    if attempts >= _STUCK_OPS_MAX_ATTEMPTS:
        return {
            "action": "exhausted",
            "message": (
                f"ops pod sandbox stuck in ContainerCreating on {node} "
                f"after {attempts} reschedule attempts"
            ),
            "attempts": attempts,
            "exclude": [],
            "annotations": annotations,
        }

    counts = _parse_stuck_node_counts(annotations)
    counts[node] = counts.get(node, 0) + 1
    attempts += 1
    next_ann = dict(annotations)
    next_ann[ANN_STUCK_NODES] = _format_stuck_node_counts(counts)
    next_ann[ANN_ATTEMPTS] = str(attempts)
    exclude = [h for h, c in counts.items() if c >= _STUCK_NODE_EXCLUDE_AFTER]
    return {
        "action": "reschedule",
        "attempts": attempts,
        "exclude": exclude,
        "annotations": next_ann,
        "detail": f"rescheduled ops pod off {node} (attempt {attempts})",
    }


def _is_k8s_model(obj: Any) -> bool:
    """True for kubernetes client models (not MagicMock / plain dicts)."""
    # Use type(obj) — hasattr(MagicMock(), "openapi_types") is always True.
    return isinstance(getattr(type(obj), "openapi_types", None), dict)


def _plain(obj: Any) -> Any:
    """Best-effort plain dict/list for recreate bodies (tests + live objects)."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [_plain(x) for x in obj]
    if _is_k8s_model(obj):
        to_dict = getattr(obj, "to_dict", None)
        if callable(to_dict):
            try:
                return _plain(to_dict())
            except Exception:  # noqa: BLE001
                pass
    return None


def _build_ops_pod_recreate_body(
    pod, *, annotations: dict[str, str], affinity: dict | None
) -> dict[str, Any]:
    """Build a create-able Pod dict from a live read (strip runtime fields)."""
    body: dict[str, Any] | None = None
    if _is_k8s_model(pod):
        try:
            from kubernetes.client import ApiClient

            raw = ApiClient().sanitize_for_serialization(pod)
            if isinstance(raw, dict) and isinstance(raw.get("metadata"), dict):
                body = raw
        except Exception:  # noqa: BLE001 - fall back for partial objects
            body = None

    if body is None:
        meta = pod.metadata
        spec = pod.spec
        body = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": meta.name,
                "labels": dict(getattr(meta, "labels", None) or {}),
            },
            "spec": {
                "restartPolicy": getattr(spec, "restart_policy", None) or "Always",
                "serviceAccountName": getattr(spec, "service_account_name", None)
                or "default",
                "containers": _plain(getattr(spec, "containers", None) or []) or [],
                "volumes": _plain(getattr(spec, "volumes", None) or []) or [],
            },
        }
        ns = getattr(meta, "namespace", None)
        if ns:
            body["metadata"]["namespace"] = ns
        for attr, key in (
            ("dns_policy", "dnsPolicy"),
            ("dns_config", "dnsConfig"),
            ("host_aliases", "hostAliases"),
            ("security_context", "securityContext"),
        ):
            val = _plain(getattr(spec, attr, None))
            if val:
                body["spec"][key] = val
        existing_aff = _plain(getattr(spec, "affinity", None))
        if existing_aff:
            body["spec"]["affinity"] = existing_aff

    meta = body.setdefault("metadata", {})
    for k in (
        "resourceVersion",
        "resource_version",
        "uid",
        "creationTimestamp",
        "creation_timestamp",
        "managedFields",
        "managed_fields",
        "generation",
        "selfLink",
        "self_link",
    ):
        meta.pop(k, None)
    body.pop("status", None)
    spec = body.setdefault("spec", {})
    for k in ("nodeName", "node_name"):
        spec.pop(k, None)

    # Merge Multus / prior annotations with heal state.
    prior = dict(meta.get("annotations") or {})
    prior.update(annotations)
    meta["annotations"] = prior

    if affinity:
        merged = dict(spec.get("affinity") or {})
        merged.update(affinity)
        spec["affinity"] = merged
    return body


def _recreate_ops_pod(core_api, namespace: str, pod_body: dict, name: str) -> None:
    """Retry create until a terminating predecessor is fully gone."""
    from app.services.providers.kubevirt import _recreate_ops_pod as _kv_recreate

    _kv_recreate(core_api, namespace, pod_body, name)


def heal_stuck_ops_pod(
    core_api,
    namespace: str,
    pod_name: str,
    *,
    now=None,
    threshold_s=_STUCK_OPS_THRESHOLD_S,
) -> dict[str, Any]:
    """Delete+recreate a stuck ops pod, or report exhausted attempts.

    Returns a result dict with ``action`` in ``ok`` / ``reschedule`` / ``exhausted``
    / ``error``. ``ok`` means not stuck (or transient read failure).
    """
    if now is None:
        now = time.time()
    try:
        pod = core_api.read_namespaced_pod(name=pod_name, namespace=namespace)
    except Exception as e:  # noqa: BLE001
        logger.debug("Ops pod heal: read %s/%s failed: %s", namespace, pod_name, e)
        return {"action": "ok"}

    if not _is_ops_pod_stuck_creating(pod, now=now, threshold_s=threshold_s):
        return {"action": "ok"}

    node = getattr(getattr(pod, "spec", None), "node_name", None) or "unknown"
    annotations = dict(
        getattr(getattr(pod, "metadata", None), "annotations", None) or {}
    )
    plan = plan_ops_pod_heal(annotations, node)
    if plan["action"] == "exhausted":
        return {
            "action": "exhausted",
            "message": plan["message"],
            "node": node,
        }

    affinity = (
        _node_hostname_not_in_affinity(plan["exclude"]) if plan["exclude"] else None
    )
    body = _build_ops_pod_recreate_body(
        pod, annotations=plan["annotations"], affinity=affinity
    )
    try:
        core_api.delete_namespaced_pod(
            name=pod_name, namespace=namespace, grace_period_seconds=0
        )
        _recreate_ops_pod(core_api, namespace, body, pod_name)
    except Exception as e:
        logger.warning(
            "Ops pod heal: failed to reschedule %s/%s off %s: %s",
            namespace,
            pod_name,
            node,
            e,
        )
        return {"action": "error", "message": str(e)}

    detail = plan["detail"]
    logger.info("Stuck ops-pod heal: %s", detail)
    return {
        "action": "reschedule",
        "detail": detail,
        "node": node,
        "attempts": plan["attempts"],
    }
