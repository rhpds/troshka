"""Namespace-scoped cancellation and external obligations, independent of a root."""

import asyncio
import json

from helpers import managed_power as power

ANNOTATION = "troshka.redhat.com/namespace-cleanup"
ACK = "troshka.redhat.com/namespace-cleanup-ack"


def namespace_cleanup(api, namespace, expected_namespace_uid=None):
    value = api.get("Namespace", None, namespace)
    meta = value["metadata"]
    raw = meta.get("annotations", {}).get(ANNOTATION)
    if raw is None:
        return None
    try:
        marker = json.loads(raw)
    except (ValueError, TypeError) as error:
        raise power.ManagedPowerError("namespace_cleanup_marker_invalid") from error
    power.require(isinstance(marker, dict) and marker.get("version") == "native-project-namespace-v1"
                  and marker.get("cleanupId") and marker.get("sourceKind") in {"origin", "admin_custody"}
                  and marker.get("sourceId") and marker.get("sourceDigest") and marker.get("projectId"),
                  "namespace_cleanup_marker_invalid")
    power.require(marker.get("namespace") == namespace and marker.get("namespaceUid") == meta["uid"]
                  and expected_namespace_uid in (None, meta["uid"]), "namespace_cleanup_uid_changed")
    cluster = api.get("Namespace", None, "kube-system")
    power.require(cluster["metadata"]["uid"] == marker.get("clusterUid"), "namespace_cleanup_cluster_changed")
    return marker


def assert_project_open(api, namespace):
    power.require(namespace_cleanup(api, namespace) is None, "namespace_cleanup_production_fenced")


async def acknowledge(api, namespace):
    """The existing root lock joins earlier handlers, including root disappearance."""
    from helpers.native_origin_drain import operator_identity
    from helpers.native_origin_operator import qualify

    marker = namespace_cleanup(api, namespace)
    if marker is None:
        return
    key = marker.get("rootUid") or marker["namespaceUid"]
    async with power._locks.setdefault(key, asyncio.Lock()):
        from helpers.native_origin_drain import _unfinished_children

        power.require(not any(not child.done() for child in _unfinished_children.get(key, [])),
                      "namespace_cleanup_local_child_still_running")
        power.require(namespace_cleanup(api, namespace) == marker, "namespace_cleanup_marker_changed")
        identity = operator_identity(api)
        qualify(api, marker.get("qualification"), identity)
        power.require({k: v for k, v in identity.items() if k != "incarnation"} == marker.get("operator"),
                      "namespace_cleanup_operator_changed")
        value = api.get("Namespace", None, namespace)
        annotations = dict(value["metadata"].get("annotations", {}))
        ack = dict(markerDigest=power.digest(marker), namespaceUid=marker["namespaceUid"], operator=identity)
        encoded = json.dumps(ack, sort_keys=True, separators=(",", ":"))
        if annotations.get(ACK) == encoded:
            return
        annotations[ACK] = encoded
        api.patch(value, [{"op": "add", "path": "/metadata/annotations", "value": annotations}])


def finish_external(api, namespace, kind):
    """Only exact SCC memberships; failures keep Kopf's own finalizer pending."""
    from handlers.network import _modify_scc_users
    from kubernetes import client

    marker = namespace_cleanup(api, namespace)
    if marker is None:
        return False
    obligations = {
        "TroshkaProject": [("troshka-network", "troshka-network-pods"), ("troshka-network", "troshka-gateway"),
                           ("troshka-bmc", "troshka-gateway"), ("troshka-recert", "troshka-privileged-jobs")],
        "TroshkaNetwork": [("troshka-network", "troshka-network-pods"), ("troshka-network", "troshka-gateway")],
        "TroshkaVM": [],
    }
    if kind == "TroshkaCeph":
        from handlers.ceph import APPLIANCE_SCC_NAME, APPLIANCE_SCC_SAS

        selected = [(sa, APPLIANCE_SCC_NAME) for sa in APPLIANCE_SCC_SAS]
    else:
        power.require(kind in obligations, "namespace_cleanup_handler_unsupported")
        selected = obligations[kind]
    custom = client.CustomObjectsApi()
    for sa, scc in selected:
        _modify_scc_users(custom, scc, f"system:serviceaccount:{namespace}:{sa}", "remove")
    power.require(namespace_cleanup(api, namespace) == marker, "namespace_cleanup_external_identity_changed")
    return True
