"""Heal project pods stuck Terminating or in Multus sandbox setup.

Covers gateway/dnsmasq/BMC/vnc/exec Deployments and bare showroom pods.
Skips virt-launcher (handled separately) and ops pods (backend heal).
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

ANN_STUCK_NODES = "troshka.io/pod-stuck-nodes"
ANN_ATTEMPTS = "troshka.io/pod-reschedule-attempts"

_STUCK_MAX_ATTEMPTS = 3
_STUCK_NODE_EXCLUDE_AFTER = 2
# Multus CmdAdd timeouts hang well past a normal image pull.
_STUCK_CREATING_THRESHOLD_S = 180
# NotReady nodes leave pods Terminating until force-deleted.
_STUCK_TERMINATING_THRESHOLD_S = 120

_LOG_HEAL = "Stuck-pod heal: %s"
_KUBECTL_ANN_PREFIX = "kubectl.kubernetes.io/"


def _should_skip_pod(pod) -> bool:
    """Skip pods healed elsewhere (virt-launcher, ops)."""
    name = str(getattr(getattr(pod, "metadata", None), "name", "") or "")
    labels = dict(getattr(getattr(pod, "metadata", None), "labels", None) or {})
    if name.startswith("virt-launcher-") or "kubevirt.io/domain" in labels:
        return True
    if labels.get("app") == "troshka-ops-pod" or name.endswith("-ops"):
        return True
    return False


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


def _is_pod_stuck_terminating(
    pod, *, now=None, threshold_s=_STUCK_TERMINATING_THRESHOLD_S
) -> bool:
    """True when deletionTimestamp is set and the pod has not gone away."""
    if now is None:
        now = time.time()
    deleted = getattr(getattr(pod, "metadata", None), "deletion_timestamp", None)
    if deleted is None:
        return False
    return (now - deleted.timestamp()) >= threshold_s


def _is_pod_stuck_creating(
    pod, *, now=None, threshold_s=_STUCK_CREATING_THRESHOLD_S
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
    return ",".join(f"{h}:{c}" for h, c in sorted(counts.items()))


def _node_hostname_not_in_affinity(hostnames: list[str]) -> dict[str, Any]:
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


def _plan_pod_heal(annotations: dict | None, node: str) -> dict[str, Any]:
    """Pure: decide reschedule vs exhausted and next annotations/exclude."""
    annotations = dict(annotations or {})
    attempts = int(annotations.get(ANN_ATTEMPTS, "0") or "0")
    if attempts >= _STUCK_MAX_ATTEMPTS:
        return {
            "action": "exhausted",
            "message": (
                f"pod sandbox stuck on {node} after {attempts} reschedule attempts"
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
        "detail": f"rescheduled off {node} (attempt {attempts})",
    }


def _controller_owner(pod) -> tuple[str, str] | None:
    for ref in getattr(getattr(pod, "metadata", None), "owner_references", None) or []:
        if not getattr(ref, "controller", False):
            continue
        kind = str(getattr(ref, "kind", "") or "")
        name = str(getattr(ref, "name", "") or "")
        if kind and name:
            return kind, name
    return None


def _is_deployment_managed(pod) -> bool:
    owner = _controller_owner(pod)
    return owner is not None and owner[0] in ("ReplicaSet", "Deployment")


def _force_delete_pod(core_api, namespace: str, name: str) -> None:
    core_api.delete_namespaced_pod(
        name=name, namespace=namespace, grace_period_seconds=0
    )


def _deployment_name_for_pod(apps_api, namespace: str, pod) -> str | None:
    owner = _controller_owner(pod)
    if owner is None:
        return None
    kind, name = owner
    if kind == "Deployment":
        return name
    if kind != "ReplicaSet":
        return None
    try:
        rs = apps_api.read_namespaced_replica_set(name=name, namespace=namespace)
    except Exception:  # noqa: BLE001
        return None
    for ref in getattr(getattr(rs, "metadata", None), "owner_references", None) or []:
        if (
            getattr(ref, "controller", False)
            and getattr(ref, "kind", None) == "Deployment"
        ):
            return str(ref.name)
    return None


def _patch_deployment_heal(
    apps_api, namespace: str, deploy_name: str, plan: dict[str, Any]
) -> None:
    """Stamp attempt annotations + optional NotIn affinity on the Deployment."""
    try:
        dep = apps_api.read_namespaced_deployment(name=deploy_name, namespace=namespace)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Stuck-pod heal: read Deployment %s/%s: %s", namespace, deploy_name, e
        )
        return

    meta_ann = dict(getattr(getattr(dep, "metadata", None), "annotations", None) or {})
    meta_ann.update(plan["annotations"])
    patch: dict[str, Any] = {"metadata": {"annotations": meta_ann}}
    if plan["exclude"]:
        affinity = _node_hostname_not_in_affinity(plan["exclude"])
        patch["spec"] = {"template": {"spec": {"affinity": affinity}}}
        # Bump a harmless annotation so the pod template changes and RS rolls.
        tmpl_ann = {
            "troshka.io/pod-heal-bump": plan["annotations"].get(ANN_ATTEMPTS, "0")
        }
        patch["spec"]["template"]["metadata"] = {"annotations": tmpl_ann}
    try:
        apps_api.patch_namespaced_deployment(
            name=deploy_name, namespace=namespace, body=patch
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Stuck-pod heal: patch Deployment %s/%s failed: %s",
            namespace,
            deploy_name,
            e,
        )


def _is_k8s_model(obj: Any) -> bool:
    """True for kubernetes client models (not MagicMock / plain dicts)."""
    return isinstance(getattr(type(obj), "openapi_types", None), dict)


def _plain(obj: Any) -> Any:
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
                return None
    return None


def _as_str(val: Any, default: str = "") -> str:
    return val if isinstance(val, str) and val else default


def _pod_labels_and_annotations(meta) -> tuple[dict, dict]:
    labels = getattr(meta, "labels", None) or {}
    prior_ann = getattr(meta, "annotations", None) or {}
    if not isinstance(labels, dict):
        labels = {}
    if not isinstance(prior_ann, dict):
        prior_ann = {}
    return labels, prior_ann


def _plain_containers(spec) -> list:
    containers = getattr(spec, "containers", None) or []
    plain_containers = _plain(containers)
    if plain_containers is None:
        # Test doubles often stash plain dicts on the mock.
        return containers if isinstance(containers, list) else []
    return plain_containers or []


def _strip_kubectl_annotations(annotations: dict) -> dict:
    return {
        k: v for k, v in annotations.items() if not k.startswith(_KUBECTL_ANN_PREFIX)
    }


def _owner_references_for_body(meta) -> list[dict[str, Any]]:
    owners: list[dict[str, Any]] = []
    for ref in getattr(meta, "owner_references", None) or []:
        kind = getattr(ref, "kind", None)
        name = getattr(ref, "name", None)
        if not isinstance(kind, str) or not isinstance(name, str):
            continue
        api_ver = getattr(ref, "api_version", None)
        if not isinstance(api_ver, str):
            api_ver = "troshka.redhat.com/v1alpha1"
        entry: dict[str, Any] = {
            "apiVersion": api_ver,
            "kind": kind,
            "name": name,
            "controller": bool(getattr(ref, "controller", False)),
        }
        uid = getattr(ref, "uid", None)
        if isinstance(uid, str):
            entry["uid"] = uid
        owners.append(entry)
    return owners


def _copy_optional_spec_fields(spec, body_spec: dict) -> None:
    for attr, key in (
        ("init_containers", "initContainers"),
        ("volumes", "volumes"),
        ("dns_policy", "dnsPolicy"),
        ("dns_config", "dnsConfig"),
        ("security_context", "securityContext"),
        ("automount_service_account_token", "automountServiceAccountToken"),
    ):
        raw = getattr(spec, attr, None)
        if raw is None:
            continue
        val = _plain(raw)
        if val is None and isinstance(raw, (list, dict, str, bool, int)):
            val = raw
        if val is not None and val != []:
            body_spec[key] = val


def _merge_affinity(spec, affinity: dict | None, body_spec: dict) -> None:
    if not affinity:
        return
    existing = _plain(getattr(spec, "affinity", None))
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(affinity)
    body_spec["affinity"] = merged


def _bare_pod_recreate_body(pod, *, annotations: dict, affinity: dict | None) -> dict:
    meta = pod.metadata
    spec = pod.spec
    labels, prior_ann = _pod_labels_and_annotations(meta)

    body: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": meta.name,
            "namespace": getattr(meta, "namespace", None),
            "labels": dict(labels),
            "annotations": _strip_kubectl_annotations(
                {**dict(prior_ann), **annotations}
            ),
        },
        "spec": {
            "restartPolicy": _as_str(getattr(spec, "restart_policy", None), "Always")
            or "Always",
            "serviceAccountName": _as_str(
                getattr(spec, "service_account_name", None), "default"
            )
            or "default",
            "containers": _plain_containers(spec),
        },
    }
    owners = _owner_references_for_body(meta)
    if owners:
        body["metadata"]["ownerReferences"] = owners

    _copy_optional_spec_fields(spec, body["spec"])
    _merge_affinity(spec, affinity, body["spec"])
    return body


def _heal_terminating(core_api, namespace: str, pod) -> str:
    name = pod.metadata.name
    _force_delete_pod(core_api, namespace, name)
    detail = f"force-deleted stuck Terminating pod {name}"
    logger.info(_LOG_HEAL, detail)
    return detail


def _heal_creating(core_api, apps_api, namespace: str, pod) -> tuple[str, str | None]:
    """Returns (detail, error_or_None)."""
    node = getattr(getattr(pod, "spec", None), "node_name", None) or "unknown"

    if _is_deployment_managed(pod):
        return _heal_creating_deployment(core_api, apps_api, namespace, pod, node)

    return _heal_creating_bare(core_api, namespace, pod, node)


def _heal_creating_deployment(
    core_api, apps_api, namespace, pod, node
) -> tuple[str, str | None]:
    deploy_name = _deployment_name_for_pod(apps_api, namespace, pod)
    annotations: dict[str, str] = {}
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

    plan = _plan_pod_heal(annotations, node)
    if plan["action"] == "exhausted":
        return "", plan["message"]

    if deploy_name:
        _patch_deployment_heal(apps_api, namespace, deploy_name, plan)
    _force_delete_pod(core_api, namespace, pod.metadata.name)
    detail = f"force-deleted stuck creating {pod.metadata.name}; {plan['detail']}"
    logger.info(_LOG_HEAL, detail)
    return detail, None


def _heal_creating_bare(core_api, namespace, pod, node) -> tuple[str, str | None]:
    annotations = dict(
        getattr(getattr(pod, "metadata", None), "annotations", None) or {}
    )
    plan = _plan_pod_heal(annotations, node)
    if plan["action"] == "exhausted":
        return "", plan["message"]

    affinity = (
        _node_hostname_not_in_affinity(plan["exclude"]) if plan["exclude"] else None
    )
    body = _bare_pod_recreate_body(
        pod, annotations=plan["annotations"], affinity=affinity
    )
    _force_delete_pod(core_api, namespace, pod.metadata.name)
    _recreate_bare_pod(core_api, namespace, body, pod.metadata.name)
    detail = f"recreated stuck creating {pod.metadata.name}; {plan['detail']}"
    logger.info(_LOG_HEAL, detail)
    return detail, None


def _recreate_bare_pod(core_api, namespace: str, body: dict, name: str) -> None:
    """Create after force-delete; ignore AlreadyExists while predecessor drains."""
    try:
        core_api.create_namespaced_pod(namespace=namespace, body=body)
    except Exception as e:  # noqa: BLE001
        status = getattr(e, "status", None)
        if status == 409:
            logger.debug(
                "Stuck-pod heal: recreate %s/%s still exists (will retry next tick)",
                namespace,
                name,
            )
            return
        raise


def heal_stuck_project_pods(
    core_api,
    apps_api,
    namespace: str,
    *,
    now=None,
    creating_threshold_s=_STUCK_CREATING_THRESHOLD_S,
    terminating_threshold_s=_STUCK_TERMINATING_THRESHOLD_S,
) -> tuple[list[str], list[str]]:
    """Force-delete / recreate stuck pods in a project namespace.

    Returns ``(details, errors)``.
    """
    if now is None:
        now = time.time()
    details: list[str] = []
    errors: list[str] = []

    try:
        pod_list = core_api.list_namespaced_pod(namespace=namespace)
    except Exception as e:  # noqa: BLE001
        logger.debug("Stuck-pod heal: list %s failed: %s", namespace, e)
        return details, errors

    for pod in pod_list.items or []:
        if _should_skip_pod(pod):
            continue
        try:
            outcome = _heal_one_pod(
                core_api,
                apps_api,
                namespace,
                pod,
                now=now,
                creating_threshold_s=creating_threshold_s,
                terminating_threshold_s=terminating_threshold_s,
            )
        except Exception as e:  # noqa: BLE001
            msg = f"{getattr(pod.metadata, 'name', '?')}: {e}"
            logger.warning("Stuck-pod heal failed: %s", msg)
            errors.append(msg)
            continue
        if outcome is None:
            continue
        kind, payload = outcome
        if kind == "error":
            errors.append(payload)
        else:
            details.append(payload)
    return details, errors


def _heal_one_pod(
    core_api,
    apps_api,
    namespace,
    pod,
    *,
    now,
    creating_threshold_s,
    terminating_threshold_s,
):
    if _is_pod_stuck_terminating(pod, now=now, threshold_s=terminating_threshold_s):
        return ("detail", _heal_terminating(core_api, namespace, pod))

    if _is_pod_stuck_creating(pod, now=now, threshold_s=creating_threshold_s):
        detail, err = _heal_creating(core_api, apps_api, namespace, pod)
        if err:
            return ("error", err)
        return ("detail", detail)

    return None
