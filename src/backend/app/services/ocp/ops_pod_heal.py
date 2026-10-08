"""Heal KubeVirt ops pods stuck in ContainerCreating / sandbox setup.

Mirrors the operator's virt-launcher stuck-heal: detect a scheduled pod that
never becomes Ready, delete it, and recreate with ``NotIn`` affinity after
repeated hits on the same node. Caps attempts so Multus sandbox
``DeadlineExceeded`` cannot hang the install monitor forever.

Also heals a silent failure mode: recreate must not copy OVN/CNI status
annotations (``k8s.ovn.org/pod-networks``, network-status). Carrying a prior
node's pod IP onto a new node leaves eth0 Ready but with no cluster egress.
Running pods whose primary IP falls outside the node's OVN subnet are
rescheduled the same way.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

ANN_STUCK_NODES = "troshka.io/ops-stuck-nodes"
ANN_ATTEMPTS = "troshka.io/ops-reschedule-attempts"
_STUCK_OPS_MAX_ATTEMPTS = 3
_STUCK_NODE_EXCLUDE_AFTER = 2
# Longer than a normal image pull; Multus sandbox failures hang well past this.
_STUCK_OPS_THRESHOLD_S = 180
# Wrong-OVN-IP pods are Ready immediately; short grace avoids racing CNI setup.
_OVN_MISMATCH_GRACE_S = 30
_CNI_NETWORKS_ANN = "k8s.v1.cni.cncf.io/networks"
_CNI_STATUS_ANN_KEYS = frozenset(
    {
        "k8s.v1.cni.cncf.io/network-status",
        "k8s.v1.cni.cncf.io/networks-status",
    }
)
_OVN_POD_ANN_PREFIX = "k8s.ovn.org/"
_CIDR_RE = re.compile(r"\d+\.\d+\.\d+\.\d+/\d+")


def _is_ops_pod_stuck_creating(
    pod, *, now=None, threshold_s: float = _STUCK_OPS_THRESHOLD_S
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


def _pod_age_s(pod, *, now: float) -> float | None:
    created = getattr(getattr(pod, "metadata", None), "creation_timestamp", None)
    if created is None:
        return None
    return now - created.timestamp()


def _pod_primary_ip(pod) -> str | None:
    """Return the pod's primary (OVN) IP from status, if assigned."""
    status = getattr(pod, "status", None)
    if status is None:
        return None
    ip = getattr(status, "pod_ip", None) or getattr(status, "podIP", None)
    return str(ip) if ip else None


def _parse_node_ovn_subnets(node) -> list[ipaddress.IPv4Network]:
    """Parse ``k8s.ovn.org/node-subnets`` into IPv4 networks (empty if unknown)."""
    meta = getattr(node, "metadata", None)
    anns = getattr(meta, "annotations", None) or {}
    if isinstance(node, dict):
        anns = (node.get("metadata") or {}).get("annotations") or {}
    raw = anns.get("k8s.ovn.org/node-subnets") or ""
    if not raw:
        return []
    networks: list[ipaddress.IPv4Network] = []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    candidates: list[str] = []
    if isinstance(parsed, dict):
        default = parsed.get("default")
        if isinstance(default, str):
            candidates.append(default)
        elif isinstance(default, list):
            candidates.extend(str(x) for x in default)
    if not candidates:
        candidates = _CIDR_RE.findall(str(raw))
    for cidr in candidates:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if isinstance(net, ipaddress.IPv4Network):
            networks.append(net)
    return networks


def _ops_pod_ovn_ip_mismatched(pod, node_subnets: list[ipaddress.IPv4Network]) -> bool:
    """True when the Running pod's primary IP is outside the node's OVN subnet(s)."""
    if not node_subnets:
        return False
    phase = str(getattr(getattr(pod, "status", None), "phase", "") or "").lower()
    if phase != "running":
        return False
    raw_ip = _pod_primary_ip(pod)
    if not raw_ip:
        return False
    try:
        ip = ipaddress.ip_address(raw_ip)
    except ValueError:
        return False
    return not any(ip in net for net in node_subnets)


def _read_node_ovn_subnets(core_api, node_name: str) -> list[ipaddress.IPv4Network]:
    """Best-effort node OVN subnet lookup (empty on failure)."""
    if not node_name or node_name == "unknown":
        return []
    try:
        node = core_api.read_node(node_name)
    except Exception as e:  # noqa: BLE001
        logger.debug("Ops pod heal: read node %s failed: %s", node_name, e)
        return []
    return _parse_node_ovn_subnets(node)


def _needs_ops_pod_heal(
    core_api,
    pod,
    *,
    now: float,
    threshold_s: float,
) -> tuple[bool, str]:
    """Return ``(needs_heal, reason)`` for stuck-creating or OVN IP mismatch."""
    if _is_ops_pod_stuck_creating(pod, now=now, threshold_s=threshold_s):
        return True, "stuck-creating"
    age = _pod_age_s(pod, now=now)
    if age is None or age < _OVN_MISMATCH_GRACE_S:
        return False, ""
    if not _pod_is_scheduled(pod):
        return False, ""
    node = getattr(getattr(pod, "spec", None), "node_name", None) or ""
    subnets = _read_node_ovn_subnets(core_api, node)
    if _ops_pod_ovn_ip_mismatched(pod, subnets):
        return True, "ovn-ip-mismatch"
    return False, ""


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


def _ops_pod_body_from_attrs(pod) -> dict[str, Any]:
    meta = pod.metadata
    spec = pod.spec
    body: dict[str, Any] = {
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
    return body


def _is_ops_pod_cni_status_annotation(key: str) -> bool:
    """True for OVN/CNI *status* annotations that must not survive recreate."""
    if key in _CNI_STATUS_ANN_KEYS:
        return True
    if key.startswith(_OVN_POD_ANN_PREFIX):
        return True
    return False


def _strip_ops_pod_cni_status_annotations(annotations: dict[str, Any]) -> None:
    """Drop OVN/CNI status annotations; keep desired Multus ``networks`` list."""
    for key in list(annotations):
        if key == _CNI_NETWORKS_ANN:
            continue
        if _is_ops_pod_cni_status_annotation(key):
            annotations.pop(key, None)


def _strip_ops_pod_runtime_fields(body: dict[str, Any]) -> None:
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
    anns = meta.get("annotations")
    if isinstance(anns, dict):
        _strip_ops_pod_cni_status_annotations(anns)
    body.pop("status", None)
    spec = body.setdefault("spec", {})
    for k in ("nodeName", "node_name"):
        spec.pop(k, None)


def _build_ops_pod_recreate_body(
    pod, *, annotations: dict[str, str], affinity: dict | None
) -> dict[str, Any]:
    """Build a create-able Pod dict from a live read (strip runtime fields)."""
    body = _pod_body_from_serialized_model(pod)
    if body is None:
        body = _ops_pod_body_from_attrs(pod)

    _strip_ops_pod_runtime_fields(body)
    meta = body.setdefault("metadata", {})
    prior = dict(meta.get("annotations") or {})
    prior.update(annotations)
    # Plan annotations are a full copy of the live pod's annotations — strip
    # again after merge so OVN pod-networks cannot pin a stale host IP.
    _strip_ops_pod_cni_status_annotations(prior)
    meta["annotations"] = prior

    spec = body.setdefault("spec", {})
    if affinity:
        merged = dict(spec.get("affinity") or {})
        merged.update(affinity)
        spec["affinity"] = merged
    return body


def _pod_body_from_serialized_model(pod) -> dict[str, Any] | None:
    if not _is_k8s_model(pod):
        return None
    try:
        from kubernetes.client import ApiClient

        raw = ApiClient().sanitize_for_serialization(pod)
        if isinstance(raw, dict) and isinstance(raw.get("metadata"), dict):
            return raw
    except Exception:  # noqa: BLE001 - fall back for partial objects
        return None
    return None


def _recreate_ops_pod(core_api, namespace: str, pod_body: dict, name: str) -> None:
    """Retry create until a terminating predecessor is fully gone."""
    from app.services.providers.kubevirt import _recreate_ops_pod as _kv_recreate

    _kv_recreate(core_api, namespace, pod_body, name)


def _heal_exhausted_message(node: str, attempts: int, reason: str) -> str:
    if reason == "ovn-ip-mismatch":
        return (
            f"ops pod OVN IP mismatched node subnet on {node} "
            f"after {attempts} reschedule attempts"
        )
    return (
        f"ops pod sandbox stuck in ContainerCreating on {node} "
        f"after {attempts} reschedule attempts"
    )


def heal_stuck_ops_pod(
    core_api,
    namespace: str,
    pod_name: str,
    *,
    now=None,
    threshold_s=_STUCK_OPS_THRESHOLD_S,
) -> dict[str, Any]:
    """Delete+recreate a stuck or OVN-misplaced ops pod, or report exhausted.

    Returns a result dict with ``action`` in ``ok`` / ``reschedule`` / ``exhausted``
    / ``error``. ``ok`` means healthy (or transient read failure).
    """
    if now is None:
        now = time.time()
    try:
        pod = core_api.read_namespaced_pod(name=pod_name, namespace=namespace)
    except Exception as e:  # noqa: BLE001
        logger.debug("Ops pod heal: read %s/%s failed: %s", namespace, pod_name, e)
        return {"action": "ok"}

    needs_heal, reason = _needs_ops_pod_heal(
        core_api, pod, now=now, threshold_s=threshold_s
    )
    if not needs_heal:
        return {"action": "ok"}

    node = getattr(getattr(pod, "spec", None), "node_name", None) or "unknown"
    annotations = dict(
        getattr(getattr(pod, "metadata", None), "annotations", None) or {}
    )
    plan = plan_ops_pod_heal(annotations, node)
    if plan["action"] == "exhausted":
        return {
            "action": "exhausted",
            "message": _heal_exhausted_message(node, plan["attempts"], reason),
            "node": node,
            "reason": reason,
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
            "Ops pod heal: failed to reschedule %s/%s off %s (%s): %s",
            namespace,
            pod_name,
            node,
            reason,
            e,
        )
        return {"action": "error", "message": str(e), "reason": reason}

    detail = f"{plan['detail']} [{reason}]"
    logger.info("Stuck ops-pod heal: %s", detail)
    return {
        "action": "reschedule",
        "detail": detail,
        "node": node,
        "attempts": plan["attempts"],
        "reason": reason,
    }
