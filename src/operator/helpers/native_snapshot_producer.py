"""Origin-only capture/file-pull POST fence and exact original UID recovery."""

import json
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from helpers import managed_power as power
from helpers import native_origin_checkpoint as checkpoint

PRODUCER = "troshka.redhat.com/origin-producer"


def admitted_digest(value):
    spec = deepcopy(value.get("spec", {}))
    if value["kind"] == "PersistentVolumeClaim":
        # Kubernetes binding sets this after admission. Custody independently
        # pins the resulting PV UID, handle, and image identity.
        spec.pop("volumeName", None)
    return power.digest(spec)


def enabled(kind):
    if not checkpoint.participating() or kind not in {"VolumeSnapshot", "PersistentVolumeClaim", "Job", "Pod"}:
        return False
    root, _, _ = power._context.get()
    annotations = root["metadata"].get("annotations", {})
    child = power._producer_body.get()
    return (child is None or child.get("kind") == "TroshkaProject") and any(
        annotations.get("troshka.redhat.com/" + name) for name in ("capture-request", "file-pull-request"))


def unique_names(*names):
    if not checkpoint.participating():
        return names
    root, _, _ = power._context.get()
    operation = checkpoint.operation(root)
    return tuple(name[:42] + "-" + power.digest([operation, name])[:12] for name in names)


def _role(body, root):
    if body["kind"] == "VolumeSnapshot":
        return "snapshot"
    if body["kind"] == "PersistentVolumeClaim":
        return "restore" if body.get("spec", {}).get("dataSource", {}).get("kind") == "VolumeSnapshot" else "scratch"
    return "filepull" if root["metadata"].get("annotations", {}).get("troshka.redhat.com/file-pull-request") else "export"


def intent(root, body):
    if body["kind"] == "PersistentVolumeClaim":
        checkpoint.require(not body.get("spec", {}).get("volumeName"), "origin_prebound_helper_unsupported")
    value = dict(operation_digest=checkpoint.operation(root), kind=body["kind"], name=body["metadata"]["name"],
                 role=_role(body, root), spec_digest=power.digest(body.get("spec", {})))
    if body["kind"] == "VolumeSnapshot":
        value["source_pvc_name"] = body["spec"]["source"]["persistentVolumeClaimName"]
        value["snapshot_spec"] = body["spec"]
    value["id"] = str(uuid5(NAMESPACE_URL, power.digest([root["spec"]["originCheckpoint"]["intentId"], value])))
    return value


def stamp(root, body, producer):
    meta = body["metadata"]
    meta["ownerReferences"] = [{"apiVersion": root["apiVersion"], "kind": "TroshkaProject",
        "name": root["metadata"]["name"], "uid": root["metadata"]["uid"], "controller": True, "blockOwnerDeletion": True}]
    meta.setdefault("annotations", {}).update({PRODUCER: producer["id"],
        "troshka.redhat.com/origin-operation": producer["operation_digest"],
        "troshka.redhat.com/origin-role": producer["role"]})


def build_request(api, root, namespace, producer):
    claims = api.rows("PersistentVolumeClaim", namespace)
    checkpoint.require(bool(claims) and len(claims) <= 100, "origin_snapshot_source_unavailable")
    request = dict(version=1, intentId=root["spec"]["originCheckpoint"]["intentId"], namespace=namespace,
        namespaceUid=api.get("Namespace", None, namespace)["metadata"]["uid"], root=checkpoint.identity(root),
        producer=checkpoint.identity(root), target={"kind": producer["kind"], "name": producer["name"]},
        action="create-producer", reason="create-" + producer["role"], operationDigest=checkpoint.operation(root),
        producerIntent=producer, objects=sorted([checkpoint.object_identity(root, p) for p in claims], key=lambda p: p["uid"]))
    request["requestId"] = str(uuid5(NAMESPACE_URL, power.digest(request)))
    return request


def _existing(api, root, body, producer):
    live = api.find(body["kind"], root["metadata"]["namespace"], body["metadata"]["name"])
    if live is None:
        return None
    saved = root.get("status", {}).get("originProducerObjects", {}).get(producer["id"], {})
    checkpoint.require(saved.get("uid") == live["metadata"]["uid"]
        and saved.get("specDigest") == admitted_digest(live)
        and saved.get("requestedSpecDigest") == producer["spec_digest"]
        and live["metadata"].get("annotations", {}).get(PRODUCER) == producer["id"],
        "origin_producer_original_uid_unresolved")
    return live


def _record(api, root, body, producer, result):
    from kubernetes.client import ApiClient

    value = result if isinstance(result, dict) else ApiClient().sanitize_for_serialization(result)
    namespace = root["metadata"]["namespace"]
    live = api.get(body["kind"], namespace, body["metadata"]["name"])
    checkpoint.require(value["metadata"]["uid"] == live["metadata"]["uid"], "origin_producer_post_identity_changed")
    checkpoint.require(_subset(body["spec"], live["spec"]), "origin_producer_admission_changed")
    root = power.root_for(root, namespace, api)
    status = deepcopy(root.get("status", {}))
    records = status.setdefault("originProducerObjects", {})
    records[producer["id"]] = {"outcome": "observed", "uid": live["metadata"]["uid"], "specDigest": admitted_digest(live),
                               "requestedSpecDigest": producer["spec_digest"]}
    checkpoint.require(len(records) <= 1000 and len(json.dumps(records)) < 512 * 1024, "origin_producer_history_limit")
    api.patch(root, [{"op": "add", "path": "/status", "value": status}], status=True)
    return result


def _subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(_subset(a, b) for a, b in zip(expected, actual))
    return expected == actual


def _submit(api, root, namespace, body, producer):
    """Persist ambiguity before POST; an empty later GET cannot reopen it."""
    checkpoint.require(checkpoint.creation_epoch(root, body["kind"], producer["name"]) is None,
                       "origin_producer_submitted_outcome_unresolved")
    checkpoint.begin_create(api, root, body["kind"], namespace, producer["name"])
    root = power.root_for(root, namespace, api)
    status = deepcopy(root.get("status", {}))
    records = status.setdefault("originProducerObjects", {})
    checkpoint.require(producer["id"] not in records, "origin_producer_submitted_outcome_unresolved")
    records[producer["id"]] = dict(outcome="submitted", requestedSpecDigest=producer["spec_digest"],
        creationEpoch=checkpoint.creation_epoch(root, body["kind"], producer["name"]))
    checkpoint.require(len(records) <= 1000 and len(json.dumps(records)) < 512 * 1024, "origin_producer_history_limit")
    api.patch(root, [{"op": "add", "path": "/status", "value": status}], status=True)


def create(original, kwargs):
    from helpers.native_origin_drain import assert_open

    root, namespace, api = power._context.get()
    root = assert_open(api, root, namespace)
    checkpoint.require(kwargs.get("namespace") == namespace)
    body = kwargs["body"]
    producer = intent(root, body)
    stamp(root, body, producer)
    live = _existing(api, root, body, producer)
    if live is not None:
        return live
    checkpoint.require(producer["id"] not in root.get("status", {}).get("originProducerObjects", {}),
                       "origin_original_producer_disappeared")
    request = build_request(api, root, namespace, producer)
    ack = json.loads(root["metadata"].get("annotations", {}).get(checkpoint.ACK, "{}"))
    if not checkpoint.signature_valid(root["spec"]["originCheckpoint"]["publicKey"], request, ack):
        checkpoint.publish(api, root, request)
        raise checkpoint.CheckpointHold("origin_producer_intent_pending")
    # Current root + selected storage identities are rechecked immediately before POST.
    current = power.root_for(root, namespace, api)
    checkpoint.require(build_request(api, current, namespace, producer) == request)
    _submit(api, current, namespace, body, producer)
    assert_open(api, current, namespace)
    try:
        result = original(**kwargs)
    except Exception as error:
        raise checkpoint.CheckpointHold("origin_producer_post_outcome_unresolved") from error
    try:
        return _record(api, root, body, producer, result)
    except Exception as error:
        raise checkpoint.CheckpointHold("origin_producer_original_uid_unresolved") from error
