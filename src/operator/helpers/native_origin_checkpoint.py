"""Exact signed pre-removal acknowledgements for generic origin producers."""

import base64
import json
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import NAMESPACE_URL, uuid4, uuid5

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from helpers import managed_power as power

REQUEST = "troshka.redhat.com/origin-checkpoint-request"
ACK = "troshka.redhat.com/origin-checkpoint-ack"
PARTICIPATION = "originCheckpointIntent"
_held = ContextVar("origin_checkpoint_hold", default=None)


class CheckpointHold(power.ManagedPowerError):
    """Must propagate to the handler; no cleanup continuation after a hold."""


def propagate_hold(error):
    if isinstance(error, CheckpointHold):
        raise error


def require(condition, reason="origin_checkpoint_changed"):
    if not condition:
        raise CheckpointHold(reason)


def identity(value, *, resource_version=False):
    meta = value["metadata"]
    result = {"kind": value["kind"], "name": meta["name"], "uid": meta["uid"],
              "generation": meta.get("generation"), "specDigest": power.digest(value.get("spec", {}))}
    if resource_version:
        result["resourceVersion"] = meta["resourceVersion"]
    return result


def creation_epoch(root, kind, name):
    key = power.digest([kind, name])
    return root.get("status", {}).get("originCreateEpochs", {}).get(key)


def object_identity(root, value):
    return {**identity(value, resource_version=True),
            "creationEpoch": creation_epoch(root, value["kind"], value["metadata"]["name"])}


def operation(root):
    annotations = root["metadata"].get("annotations", {})
    return power.digest({k: annotations.get("troshka.redhat.com/" + k)
                         for k in ("capture-request", "file-pull-request")})


def signature_valid(public, request, ack):
    try:
        value = {k: v for k, v in ack.items() if k != "signature"}
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public, validate=True))
        key.verify(base64.b64decode(ack["signature"], validate=True),
                   json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
        return value["version"] == 1 and value["requestDigest"] == power.digest(request) and value["intentId"] == request["intentId"]
    except (ValueError, TypeError, KeyError, InvalidSignature):
        return False


def build_request(api, root, namespace, target, reason, producer=None):
    config = root["spec"]["originCheckpoint"]
    claims = api.rows("PersistentVolumeClaim", namespace)
    if target["kind"] in {"DataVolume", "PersistentVolumeClaim"}:
        claims = [p for p in claims if p["metadata"]["name"] == target["metadata"]["name"]]
    require(bool(claims), "origin_allocation_identity_unresolved")
    require(len(claims) <= 100, "origin_checkpoint_claim_limit")
    objects = {v["metadata"]["uid"]: object_identity(root, v) for v in [target, *claims]}
    # A PVC owned by a DV also pins the parent which can garbage-collect it.
    for claim in claims:
        for owner in claim["metadata"].get("ownerReferences", []):
            if owner.get("kind") == "DataVolume":
                dv = api.get("DataVolume", namespace, owner["name"])
                require(dv["metadata"]["uid"] == owner["uid"])
                objects[owner["uid"]] = object_identity(root, dv)
    value = {"version": 1, "intentId": config["intentId"], "namespace": namespace,
             "namespaceUid": api.get("Namespace", None, namespace)["metadata"]["uid"],
             "root": identity(root), "producer": identity(producer or root), "target": object_identity(root, target),
             "action": "remove", "reason": reason, "operationDigest": operation(root),
             "plainPVCs": sorted(p["metadata"]["uid"] for p in claims
                                 if _plain_pvc(root, p, producer or root)),
             "objects": sorted(objects.values(), key=lambda v: v["uid"])}
    value["requestId"] = str(uuid5(NAMESPACE_URL, power.digest(value)))
    return value


def publish(api, root, request):
    annotations = dict(root["metadata"].get("annotations", {}))
    annotations[REQUEST] = json.dumps(request, sort_keys=True, separators=(",", ":"))
    api.patch(root, [{"op": "add", "path": "/metadata/annotations", "value": annotations}])


def remove(kind, namespace, name, reason):
    context = power._context.get()
    if context is None or "originCheckpoint" not in context[0].get("spec", {}):
        return False
    root, _, api = context
    require(namespace == context[1], "origin_removal_namespace_changed")
    root = power.root_for(root, namespace, api)
    from helpers.native_origin_drain import assert_open

    root = assert_open(api, root, namespace)
    target = api.find(kind, namespace, name)
    if target is None:
        return _absent(api, root, kind, namespace, name)
    producer = power._producer_body.get() or root
    producer = api.get(producer["kind"], namespace, producer["metadata"]["name"])
    request = build_request(api, root, namespace, target, reason, producer)
    annotations = root["metadata"].get("annotations", {})
    raw = annotations.get(ACK, "{}")
    try:
        ack = json.loads(raw)
    except (TypeError, ValueError):
        ack = {}
    if not signature_valid(root["spec"]["originCheckpoint"]["publicKey"], request, ack):
        publish(api, root, request)
        raise CheckpointHold("origin_checkpoint_pending")
    current = power.root_for(root, namespace, api)
    require(identity(current) == request["root"] and power.intent(current) is None and power.cleanup_intent(current) is None)
    require(operation(current) == request["operationDigest"])
    require(identity(api.get(producer["kind"], namespace, producer["metadata"]["name"])) == request["producer"])
    for obj in request["objects"]:
        require(object_identity(current, api.get(obj["kind"], namespace, obj["name"])) == obj)
    live = api.get(kind, namespace, name)
    require(object_identity(current, live) == request["target"])
    assert_open(api, current, namespace)
    api.request("DELETE", kind, namespace, name, {"apiVersion": "v1", "kind": "DeleteOptions",
                "preconditions": {k: live["metadata"][k] for k in ("uid", "resourceVersion")}})
    # A 409 or a still-terminating original must never authorize replacement.
    require(api.find(kind, namespace, name) is None, "origin_removal_not_observed")
    if kind == "DataVolume":
        require(api.find("PersistentVolumeClaim", namespace, name) is None, "origin_claim_removal_not_observed")
    return True


def _absent(api, root, kind, namespace, name):
    if kind not in {"DataVolume", "PersistentVolumeClaim", "TroshkaVM", "VirtualMachine"}:
        return True
    if kind == "DataVolume" and _plain_pvc_without_dv(api, root, namespace, name):
        return True
    entries = root.get("status", {}).get("originCheckpointReceipts", {})
    require(isinstance(entries, dict) and len(entries) <= 256)
    producer = power._producer_body.get() or root
    producer = api.get(producer["kind"], namespace, producer["metadata"]["name"])
    valid = any(_absence_receipt(root, producer, kind, name, row) for row in entries.values())
    if not valid and kind == "DataVolume" and creation_epoch(root, kind, name) is None:
        valid = any(_absent_plain_pvc(root, producer, name, row) for row in entries.values())
    require(valid, "origin_absent_original_unresolved")
    if kind == "DataVolume":
        require(api.find("PersistentVolumeClaim", namespace, name) is None)
    return True


def _plain_pvc_without_dv(api, root, namespace, name):
    """A recorded direct child PVC is not an absent/ambiguous CDI allocation."""
    pvc = api.find("PersistentVolumeClaim", namespace, name)
    producer = power._producer_body.get()
    if pvc is None or not producer or producer.get("kind") != "TroshkaVM":
        return False
    child = api.get("TroshkaVM", namespace, producer["metadata"]["name"])
    require(child["metadata"]["uid"] == producer["metadata"]["uid"])
    return _plain_pvc(root, pvc, child)


def _plain_pvc(root, pvc, producer):
    meta, spec = pvc["metadata"], pvc.get("spec", {})
    name = meta["name"]
    if creation_epoch(root, "DataVolume", name) is not None or creation_epoch(root, "PersistentVolumeClaim", name) is None:
        return False
    if spec.get("dataSource") or spec.get("dataSourceRef"):
        return False
    if any(k.startswith("cdi.kubevirt.io/") for k in meta.get("annotations", {})):
        return False
    refs = meta.get("ownerReferences", [])
    return (producer.get("kind") == "TroshkaVM"
            and len(refs) == 1 and refs[0].get("kind") == "TroshkaVM"
            and refs[0].get("uid") == producer["metadata"]["uid"]
            and refs[0].get("name") == producer["metadata"]["name"])


def _absent_plain_pvc(root, producer, name, row):
    if not _absence_receipt(root, producer, "PersistentVolumeClaim", name, row):
        return False
    request = row["request"]
    return any(o["kind"] == "PersistentVolumeClaim" and o["name"] == name
               and o["uid"] in request.get("plainPVCs", []) for o in request["objects"])


def _absence_receipt(root, producer, kind, name, row):
    try:
        request, ack = row["request"], row["ack"]
        require(request.get("action") == "remove", "origin_create_receipt_is_not_removal_authority")
        require(signature_valid(root["spec"]["originCheckpoint"]["publicKey"], request, ack))
        require(request["root"] == identity(root) and request["operationDigest"] == operation(root))
        require(request["producer"] == identity(producer))
        return any(o["kind"] == kind and o["name"] == name
                   and o.get("creationEpoch") == creation_epoch(root, kind, name) for o in request["objects"])
    except (KeyError, TypeError, ValueError):
        return False


def publish_ack(api, root, request, ack):
    status = dict(root.get("status", {}))
    entries = dict(status.get("originCheckpointReceipts", {}))
    entries[request["requestId"]] = {"request": request, "ack": ack}
    require(len(entries) <= 256 and len(json.dumps(entries)) <= 512 * 1024,
            "origin_checkpoint_history_limit")
    status["originCheckpointReceipts"] = entries
    api.patch(root, [{"op": "add", "path": "/status", "value": status}], status=True)
    root = api.get("TroshkaProject", root["metadata"]["namespace"], root["metadata"]["name"])
    annotations = dict(root["metadata"].get("annotations", {}))
    require(json.loads(annotations.get(REQUEST, "{}")) == request)
    annotations[ACK] = json.dumps(ack, sort_keys=True, separators=(",", ":"))
    api.patch(root, [{"op": "add", "path": "/metadata/annotations", "value": annotations}])


def begin_create(api, root, kind, namespace, name):
    """Invalidate old absence proofs before possibly ambiguous same-name CREATE."""
    from helpers.native_origin_drain import assert_open

    root = assert_open(api, root, namespace)
    if api.find(kind, namespace, name) is not None:
        return
    status = dict(root.get("status", {}))
    epochs = dict(status.get("originCreateEpochs", {}))
    kinds = [kind, "PersistentVolumeClaim"] if kind == "DataVolume" else [kind]
    for selected in kinds:
        epochs[power.digest([selected, name])] = str(uuid4())
    require(len(epochs) <= 1000, "origin_creation_epoch_limit")
    status["originCreateEpochs"] = epochs
    api.patch(root, [{"op": "add", "path": "/status", "value": status}], status=True)


def participating():
    context = power._context.get()
    return context is not None and "originCheckpoint" in context[0].get("spec", {})


_CORE_KINDS = {
    "persistent_volume_claim": "PersistentVolumeClaim", "pod": "Pod", "job": "Job",
    "deployment": "Deployment", "service": "Service", "secret": "Secret",  # pragma: allowlist secret -- Kubernetes kind
    "config_map": "ConfigMap", "volume_attachment": "VolumeAttachment",
}


def _method_kind(method, kwargs):
    if method.endswith("custom_object"):
        plural = kwargs.get("plural")
        mapping = {p: k for k, (_, p) in (power.KINDS | power.CLEANUP_KINDS).items()}
        return mapping.get(plural)
    for suffix, kind in _CORE_KINDS.items():
        if method.endswith("_" + suffix):
            return kind
    return None


def _finalizers(body):
    if isinstance(body, dict):
        return "finalizers" in body.get("metadata", {})
    if isinstance(body, list):
        return any("finalizers" in row.get("path", "") for row in body)
    return False


class GuardedClient:
    """Intercept destructive calls made by generic handlers and their helpers."""

    def __init__(self, client):
        self.client = client

    def __getattr__(self, method):
        original = getattr(self.client, method)
        if not method.startswith(("delete_", "patch_", "create_", "replace_")):
            return original

        def guarded(*args, **kwargs):
            context = power._context.get()
            if context is not None:
                from helpers.native_namespace_cleanup import assert_project_open

                assert_project_open(context[2], context[1])
            if not participating():
                return original(*args, **kwargs)
            try:
                require(_held.get() is None, "origin_previous_checkpoint_held")
                from helpers.native_origin_drain import assert_open

                root, namespace, api = power._context.get()
                assert_open(api, root, namespace)
                if kwargs.get("namespace") == "troshka-cache":
                    return original(*args, **kwargs)
                return _guard_mutation(original, method, args, kwargs)
            except CheckpointHold as error:
                _held.set(error)
                raise
            except Exception as error:
                if method.startswith("delete_"):
                    held = CheckpointHold("origin_conditional_removal_unconfirmed")
                    _held.set(held)
                    raise held from error
                raise

        return guarded


def _guard_mutation(original, method, args, kwargs):
    require(not args, "origin_named_delete_arguments_required")
    if _finalizers(kwargs.get("body")):
        raise CheckpointHold("origin_finalizer_stripping_unsupported")
    if method.startswith("create_"):
        from helpers import native_snapshot_producer as snapshot

        if snapshot.enabled(_method_kind(method, kwargs)):
            return snapshot.create(original, kwargs)
        try:
            _guard_create(method, kwargs)
        except Exception as error:
            raise CheckpointHold("origin_creation_fence_unavailable") from error
    if not method.startswith("delete_"):
        from helpers.native_origin_drain import assert_open

        root, namespace, api = power._context.get()
        assert_open(api, root, namespace)
        return original(*args, **kwargs)
    kind = _method_kind(method, kwargs)
    require(kind is not None, "origin_parent_removal_unsupported")
    if kind in {"Secret", "Service", "ConfigMap"}:
        return original(*args, **kwargs)
    if remove(kind, kwargs.get("namespace"), kwargs.get("name"), "delete-" + kind):
        return {"kind": "Status", "status": "Success"}
    return original(*args, **kwargs)


def _guard_create(method, kwargs):
    kind = _method_kind(method, kwargs)
    if kind not in {"DataVolume", "PersistentVolumeClaim", "VirtualMachine", "TroshkaVM", "Pod", "Job", "Deployment"}:
        return
    body = kwargs.get("body")
    require(isinstance(body, dict), "origin_creation_manifest_required")
    root, namespace, api = power._context.get()
    require(kwargs.get("namespace") == namespace)
    root = power.root_for(root, namespace, api)
    mark_child(root, body)
    begin_create(api, root, kind, namespace, body["metadata"]["name"])


def guard_client(client):
    return GuardedClient(client)


def mark_child(root, body):
    """Negative participation signal only; never supplies deletion authority."""
    config = root.get("spec", {}).get("originCheckpoint")
    if config is not None and body.get("kind") == "TroshkaVM":
        body["spec"][PARTICIPATION] = config["intentId"]


def _legacy_parent_absent(api, spec, namespace):
    require(PARTICIPATION not in spec, "origin_parent_absent")
    ns = api.get("Namespace", None, namespace)
    annotations = ns["metadata"].get("annotations", {})
    require(not any(k.startswith("troshka.redhat.com/origin-") for k in annotations), "origin_namespace_parent_absent")


def _finalizer_root(api, meta, namespace, spec):
    refs = meta.get("ownerReferences", [])
    if not refs:
        return _legacy_parent_absent(api, spec, namespace)
    require(len(refs) == 1 and refs[0].get("kind") == "TroshkaProject", "origin_finalizer_parent_ambiguous")
    root = api.find("TroshkaProject", namespace, refs[0]["name"])
    if root is None:
        return _legacy_parent_absent(api, spec, namespace)
    require(root["metadata"]["uid"] == refs[0]["uid"], "origin_finalizer_parent_changed")
    if "originCheckpoint" not in root.get("spec", {}):
        require(PARTICIPATION not in spec, "origin_finalizer_key_missing")
        return None
    return power.root_for(root, namespace, api)


@contextmanager
def finalizer(meta, namespace, spec):
    """A child finalizer needs the same live origin boundary as producers."""
    api = power.PowerClient()
    root = _finalizer_root(api, meta, namespace, spec)
    child = None
    if root is not None:
        child = api.get("TroshkaVM", namespace, meta["name"])
        require(child["metadata"]["uid"] == meta["uid"])
    context = (root, namespace, api) if root is not None else None
    tokens = (power._context.set(context), power._producer_body.set(child), _held.set(None))
    try:
        yield
    finally:
        held = _held.get()
        _held.reset(tokens[2])
        power._producer_body.reset(tokens[1])
        power._context.reset(tokens[0])
        if held is not None:
            raise held
