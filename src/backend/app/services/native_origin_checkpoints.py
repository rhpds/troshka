"""Signed, committed pre-removal receipts over Kubernetes coordination transport."""

import base64
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from app.core.encryption import decrypt, encrypt
from app.models.native_origin_checkpoint import (
    NativeOriginCheckpointKey,
    NativeOriginProducerReceipt,
)
from app.schemas.quota import AccountingUnavailable
from app.services.kubevirt_storage_enrollment import digest, digest_cr_spec

REQUEST = "troshka.redhat.com/origin-checkpoint-request"
ACK = "troshka.redhat.com/origin-checkpoint-ack"


def require(condition, message):
    if not condition:
        raise AccountingUnavailable(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def generate_key():
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    private = base64.b64encode(
        key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    ).decode()
    return public, encrypt(private)


def initialize(db, spec):
    """Called before the original spec digest/intent; never backfills old roots."""
    require("originCheckpoint" not in spec, "Origin checkpoint key already present")
    identity = str(uuid4())
    public, private = generate_key()
    db.add(
        NativeOriginCheckpointKey(
            intent_id=identity, public_key=public, encrypted_private_key=private
        )
    )
    spec["originCheckpoint"] = {"version": 1, "intentId": identity, "publicKey": public}
    return identity


def sign(secret, request, receipt_id, receipt_digest, predecessor_id):
    value = dict(
        version=1,
        requestDigest=digest(request),
        intentId=request.get("intentId"),
        receiptId=receipt_id,
        receiptDigest=receipt_digest,
        predecessorId=predecessor_id,
    )
    try:
        key = Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(decrypt(secret), validate=True)
        )
    except (ValueError, TypeError) as error:
        raise AccountingUnavailable(
            "Origin checkpoint signing key unavailable"
        ) from error
    return {**value, "signature": base64.b64encode(key.sign(canonical(value))).decode()}


def verify_signature(public, request, ack):
    try:
        value = {k: v for k, v in ack.items() if k != "signature"}
        key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public, validate=True)
        )
        key.verify(base64.b64decode(ack["signature"], validate=True), canonical(value))
        return (
            value["version"] == 1
            and value["requestDigest"] == digest(request)
            and value["intentId"] == request.get("intentId")
        )
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False


def protocol():
    import importlib

    from app.services.native_managed_power import helpers

    helpers()
    return importlib.import_module("helpers.native_origin_checkpoint")


def _owned(api, value, root, namespace):
    for _ in range(8):
        if value["metadata"]["uid"] == root["metadata"]["uid"]:
            return
        owners = value["metadata"].get("ownerReferences", [])
        require(len(owners) == 1, "Origin producer ownership ambiguous")
        owner = owners[0]
        value = api.get(owner["kind"], namespace, owner["name"])
        require(
            value["metadata"]["uid"] == owner["uid"], "Origin producer owner changed"
        )
    raise AccountingUnavailable("Origin producer ownership chain incomplete")


def validate_live(api, intent, root_receipt, request, public_key):
    """Reconstruct the request from independently read live objects, never reports."""
    from app.services.native_managed_power import helpers

    module = protocol()
    if hasattr(api, "begin_inventory"):
        api.begin_inventory()
    root = api.get("TroshkaProject", intent.namespace, intent.root_name)
    require(request.get("intentId") == str(intent.id), "Origin request intent changed")
    require(
        root["metadata"]["uid"] == root_receipt.root_uid
        and root["metadata"]["generation"] == root_receipt.generation
        and digest_cr_spec(root["spec"]) == intent.cr_spec_digest,
        "Origin request root changed",
    )
    require(
        root["spec"].get("originCheckpoint")
        == dict(version=1, intentId=str(intent.id), publicKey=public_key),
        "Origin checkpoint key changed",
    )
    require(not root["metadata"].get("deletionTimestamp"), "Origin root deleting")
    require(
        helpers().intent(root) is None and helpers().cleanup_intent(root) is None,
        "Origin producer stopped",
    )
    require(
        api.get("Namespace", None, intent.namespace)["metadata"]["uid"]
        == root_receipt.namespace_uid,
        "Origin namespace changed",
    )
    producer = request["producer"]
    require(
        producer["kind"] in {"TroshkaProject", "TroshkaVM"},
        "Origin producer kind unsupported",
    )
    producer = api.get(producer["kind"], intent.namespace, producer["name"])
    _owned(api, producer, root, intent.namespace)
    rebuilt = _rebuild_request(api, root, intent.namespace, request, producer, module)
    require(rebuilt == request, "Origin producer request changed")
    raw = root["metadata"].get("annotations", {}).get(REQUEST, "{}")
    require(
        len(raw) <= 128 * 1024 and json.loads(raw) == request,
        "Origin current producer request changed",
    )
    return root


def _rebuild_request(api, root, namespace, request, producer, module):
    if request.get("action") == "create-producer":
        import importlib

        from app.schemas.native_snapshot_custody import OriginProducerIntent

        value = OriginProducerIntent.model_validate(request["producerIntent"])
        require(
            value.uid is None and value.admitted_spec_digest is None,
            "Origin producer was not prepared before POST",
        )
        require(
            api.find(value.kind, namespace, value.name) is None,
            "Origin producer object already exists",
        )
        snapshot = importlib.import_module("helpers.native_snapshot_producer")
        return snapshot.build_request(api, root, namespace, request["producerIntent"])
    target = request["target"]
    require(
        target["kind"]
        in {
            "PersistentVolumeClaim",
            "DataVolume",
            "VirtualMachine",
            "TroshkaVM",
            "Pod",
            "Job",
            "Deployment",
            "VolumeSnapshot",
        },
        "Origin removal kind unsupported",
    )
    target = api.get(target["kind"], namespace, target["name"])
    return module.build_request(
        api, root, namespace, target, request["reason"], producer
    )


def _context(db, intent_id):
    from app.models.native_deployment_origin import NativeDeploymentOriginIntent
    from app.schemas.native_deployment_origin import OriginIntent
    from app.services import native_deployment_origin as origin
    from app.services import native_origin_custody as custody

    row = db.get(NativeDeploymentOriginIntent, str(intent_id))
    require(row is not None, "Origin intent missing")
    intent = origin._validated(row, OriginIntent)
    custody._active_identity(db, intent, publishing=True, mode="removal")
    _, root_receipt = origin.receipts(db, str(intent_id))
    key = db.get(NativeOriginCheckpointKey, str(intent_id))
    require(
        root_receipt is not None and key is not None,
        "Origin signing key or root unavailable",
    )
    if TYPE_CHECKING:
        assert root_receipt is not None and key is not None
    return intent, root_receipt, key


def _existing(db, request):
    row = db.get(NativeOriginProducerReceipt, request["requestId"])
    if row is not None:
        require(
            row.intent_id == request["intentId"]
            and row.request_digest == digest(request)
            and row.payload == request,
            "Origin request ID payload conflict",
        )
    return row


def service_request(sessions, api, intent_id, request):
    """Retain under canonical custody locks, commit, then sign in a new session."""
    require(
        isinstance(request, dict) and len(canonical(request)) <= 128 * 1024,
        "Origin request size invalid",
    )
    with sessions() as db:
        intent, root_receipt, key = _context(db, intent_id)
        public = key.public_key
        existing = _existing(db, request)
        replay = existing is not None
    validate_live(api, intent, root_receipt, request, public)
    if not replay:
        _retain_request(sessions, api, intent, root_receipt, public, request)
    # Live and canonical cancellation fences apply even to a committed replay.
    validate_live(api, intent, root_receipt, request, public)
    with sessions() as db:
        from app.services import native_deployment_origin as origin

        # Acquire the original canonical identity locks without the deploy-only
        # gate; _context immediately enforces removal lifecycle, cancellation,
        # and outstanding-I/O publication gates under those same locks.
        origin._locked(db, str(intent_id), allow_cancelled=True)
        _, _, key = _context(db, intent_id)
        row = _existing(db, request)
        require(row is not None, "Origin committed producer receipt missing")
        if TYPE_CHECKING:
            assert row is not None
        _receipt_authority(db, intent, row)
        return sign(
            key.encrypted_private_key,
            request,
            row.custody_receipt_id,
            row.custody_digest,
            row.predecessor_id,
        )


def _retain_request(sessions, api, intent, root_receipt, public, request):
    from app.services import native_origin_custody as custody

    claims = [
        o["uid"] for o in request["objects"] if o["kind"] == "PersistentVolumeClaim"
    ]
    with sessions() as db:
        producer = (
            request.get("producerIntent")
            if request.get("action") == "create-producer"
            else None
        )
        prepared = custody.prepare(
            db,
            str(intent.id),
            mode="producer" if producer else "removal",
            pvc_uids=claims,
            producer=producer,
        )
        db.commit()
    verified = custody.verify_configured(prepared)
    _snapshot_removal(api, request, verified)
    validate_live(api, intent, root_receipt, request, public)
    with sessions() as db:
        row = custody.apply(db, verified)
        receipt = custody._receipt(row)
        unresolved = custody.unresolved_claims(receipt)
        if not unresolved:
            db.add(
                NativeOriginProducerReceipt(
                    request_id=request["requestId"],
                    intent_id=str(intent.id),
                    request_digest=digest(request),
                    payload=request,
                    custody_receipt_id=row.id,
                    custody_digest=row.digest,
                    predecessor_id=(
                        str(prepared.predecessor_id)
                        if prepared.predecessor_id
                        else None
                    ),
                )
            )
        db.commit()
    require(not unresolved, "Origin pending allocation debt unresolved; removal held")


def _snapshot_removal(api, request, verified):
    """A signed removal never lends a create-intent authority to remove CSI debt."""
    if (
        request.get("action") != "remove"
        or request["target"]["kind"] != "VolumeSnapshot"
    ):
        return
    proofs = [
        p for p in verified.snapshots if p.snapshot_uid == request["target"]["uid"]
    ]
    require(
        len(proofs) == 1,
        "Origin snapshot removal requires resolved original backend custody",
    )
    snapshot = next(p for p in verified.producers if p.id == proofs[0].producer_id)
    helpers = [
        p
        for p in verified.producers
        if p.operation_digest == snapshot.operation_digest
        and p.kind != "VolumeSnapshot"
    ]
    require(
        all(api.find(p.kind, request["namespace"], p.name) is None for p in helpers),
        "Origin snapshot helpers must be removed first",
    )


def _receipt_authority(db, intent, row):
    from app.models.native_origin_custody import NativeOriginCustodyReceipt
    from app.models.project import Project
    from app.services import native_origin_custody as custody

    source = db.get(NativeOriginCustodyReceipt, row.custody_receipt_id)
    require(
        source is not None
        and source.digest == row.custody_digest
        and source.digest == digest(source.payload),
        "Origin committed custody receipt changed",
    )
    retained = custody.retained(db, db.get(Project, str(intent.project_id)))
    require(
        retained is not None and not custody.unresolved_claims(retained[1]),
        "Origin unresolved allocation debt",
    )
    if TYPE_CHECKING:
        assert retained is not None and not custody.unresolved_claims(retained[1])
    old = custody._receipt(source)
    current = {b.pvc_uid: b for b in retained[1].bindings}
    require(
        all(current.get(b.pvc_uid) == b for b in old.bindings),
        "Origin retained storage changed",
    )


# These observers have their own bounded execution and lease. They never call
# enqueue_job(project_id=...), whose callbacks own the producer's RQ mapping.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="origin-observer")
_schedule_lock = threading.Lock()
_pending = set()
_last_attempt = {}
logger = logging.getLogger(__name__)


def observer_threads():
    """Exact workers of our independent observer pool, including idle workers.

    submit() retains these Thread objects before returning. Names and threads
    launched by observation itself grant no exemption from producer tracking.
    """
    return set(_executor._threads)


def schedule(project_id):
    with _schedule_lock:
        if project_id in _pending or len(_pending) >= 2:
            return None
        _pending.add(project_id)
        _last_attempt[project_id] = time.monotonic()
    try:
        return _executor.submit(_observer, project_id)
    except Exception:
        with _schedule_lock:
            _pending.discard(project_id)
        raise


def schedule_many(project_ids):
    """Fair bounded scheduling survives idle UI and quota/capture toggle changes."""
    from app.core.database import SessionLocal
    from app.models.native_deployment_origin import NativeDeploymentOriginActive

    with SessionLocal() as db:
        active = {
            row.project_id
            for row in db.query(NativeDeploymentOriginActive).filter_by(cancelled=False)
        }
    with _schedule_lock:
        ordered = sorted(
            set(project_ids) & active, key=lambda p: (_last_attempt.get(p, 0), p)
        )
    for project_id in ordered[:2]:
        schedule(project_id)


def _observer(project_id):
    from app.core.redis import RedisLock, is_redis_available

    lease = (
        RedisLock("origin-observer:" + project_id, timeout=3600)
        if is_redis_available()
        else None
    )
    acquired = False
    try:
        acquired = lease is None or lease.acquire(blocking=False)
        if acquired:
            observe_project(project_id)
    except Exception:
        logger.info("Origin checkpoint remains held for project %s", project_id)
    finally:
        try:
            if lease is not None and acquired:
                lease.release()
        finally:
            with _schedule_lock:
                _pending.discard(project_id)


def observe_project(project_id):
    from app.core.database import SessionLocal
    from app.models.provider import Provider
    from app.services import native_deployment_origin as origin
    from app.services.native_managed_power import with_client
    from app.services.quota_cleanup import native_intent

    with SessionLocal() as db:
        cleanup = native_intent(db, project_id)
        if cleanup is not None and cleanup.phase != "finalized":
            return
        intent = origin.active(db, project_id)
        if intent is None:
            return
        _context(db, str(intent.id))
        provider = db.get(Provider, str(intent.provider_id))
        db.expunge(provider)
    return with_client(provider, _observe_live, SessionLocal, intent)


def _observe_live(api, sessions, intent):
    _retire_deleted_snapshots(sessions, api, intent)
    root = api.get("TroshkaProject", intent.namespace, intent.root_name)
    raw = root["metadata"].get("annotations", {}).get(REQUEST)
    if raw is not None:
        require(len(raw) <= 128 * 1024, "Origin request size invalid")
        request = json.loads(raw)
        # A consumed request can stay in metadata. Do not mistake absence for
        # a new no-allocation receipt; move to independent custody/retirement.
        target = request["target"]
        live = api.find(target["kind"], intent.namespace, target["name"])
        creating = request.get("action") == "create-producer" and live is None
        if creating or (
            live is not None and protocol().object_identity(root, live) == target
        ):
            ack = service_request(sessions, api, str(intent.id), request)
            root = api.get("TroshkaProject", intent.namespace, intent.root_name)
            require(
                root["metadata"].get("annotations", {}).get(REQUEST) == raw,
                "Origin request changed before publication",
            )
            protocol().publish_ack(api, root, request, ack)
            return
    _retire_one(sessions, api, intent)
    _reconcile_snapshots(sessions, intent)
    _capture(sessions, api, intent)


def _retire_deleted_snapshots(sessions, api, intent):
    """An absent old allocation must not starve behind its next producer.

    Absence only schedules independent verification; it grants no retirement
    authority. Avoid reconciling live producers before custody can discover
    their newly bound restore claims in the pending request.
    """
    from app.services import native_origin_custody as custody
    from app.services.native_snapshot_custody import retired_ids

    with sessions() as db:
        current = custody._current(db, intent.id)
        if current is None:
            return
        receipt = current[1]
        proofs = [
            p for p in receipt.snapshots if p.producer_id not in retired_ids(receipt)
        ]
    if any(
        api.find("VolumeSnapshot", intent.namespace, proof.snapshot_name) is None
        for proof in proofs
    ):
        _retire_one(sessions, api, intent)
        _reconcile_snapshots(sessions, intent)


def _reconcile_snapshots(sessions, intent):
    from app.services import native_origin_custody as custody
    from app.services import native_snapshot_reconciliation as snapshots

    with sessions() as db:
        current = custody._current(db, intent.id)
        if current is None or not custody.unresolved_snapshots(current[1]):
            return
        request = snapshots.prepare(db, str(intent.id))
        db.commit()
    value = custody.verify_configured(request)
    with sessions() as db:
        snapshots.apply(db, value)
        db.commit()


def _capture(sessions, api, intent):
    from app.services import native_origin_custody as custody

    claims = [
        p["metadata"]["uid"]
        for p in api.rows("PersistentVolumeClaim", intent.namespace)
    ]
    # Complete is independently validated; an unsettled root still retains its
    # exact existing claims without making namespace cleanup eligible.
    modes: list[Literal["complete", "removal", "enroll"]] = (
        ["complete", "removal"] if claims else ["enroll"]
    )
    for mode in modes:
        try:
            with sessions() as db:
                request = custody.prepare(
                    db,
                    str(intent.id),
                    mode=mode,
                    pvc_uids=claims if mode == "removal" else (),
                )
                db.commit()
            verified = custody.verify_configured(request)
            with sessions() as db:
                custody.apply(db, verified)
                db.commit()
            return
        except (ValueError, AccountingUnavailable):
            if mode == modes[-1]:
                raise


def _retire_one(sessions, api, intent):
    from app.models.project import Project
    from app.services import native_origin_custody as custody

    with sessions() as db:
        value = custody.retained(db, db.get(Project, str(intent.project_id)))
        if value is None or custody.unresolved_claims(value[1]):
            return
        bindings = custody.active_bindings(db, value[1])
    live_uids = {
        p["metadata"]["uid"]
        for p in api.rows("PersistentVolumeClaim", intent.namespace)
    }
    for binding in bindings:
        if binding.pvc_uid in live_uids:
            continue
        with sessions() as db:
            prepared = custody.prepare_retirement(db, str(intent.id), binding.pvc_uid)
            db.commit()
        verified = custody.verify_retirement_configured(prepared)
        with sessions() as db:
            custody.apply_retirement(db, verified)
            db.commit()
        return


def remove_child(custom_api, namespace, name, project_id=None):
    """Backend child DELETE/GC must retain storage before the API sees DELETE."""
    from app.core.database import SessionLocal
    from app.services.native_managed_power import helpers

    api = helpers().PowerClient(custom_api.api_client)
    child = _find_child(custom_api, api, namespace, name)
    if child is None:
        return False
    root = helpers().root_for(child, namespace, api)
    if "originCheckpoint" not in root.get("spec", {}):
        return False
    if project_id is not None:
        require(
            root["spec"]["projectId"] == project_id,
            "Origin removed child project changed",
        )
    request = protocol().build_request(
        api, root, namespace, child, "delete-TroshkaVM", child
    )
    protocol().publish(api, root, request)
    intent_id = root["spec"]["originCheckpoint"]["intentId"]
    request, ack = _wait_for_receipt(SessionLocal, api, intent_id, request)
    root = api.get("TroshkaProject", namespace, root["metadata"]["name"])
    protocol().publish_ack(api, root, request, ack)
    with SessionLocal() as db:
        intent, receipt, key = _context(db, intent_id)
        public = key.public_key
    validate_live(api, intent, receipt, request, public)
    live = api.get("TroshkaVM", namespace, name)
    root = api.get("TroshkaProject", namespace, intent.root_name)
    require(
        protocol().object_identity(root, live) == request["target"],
        "Origin removed child changed",
    )
    api.request(
        "DELETE",
        "TroshkaVM",
        namespace,
        name,
        dict(
            apiVersion="v1",
            kind="DeleteOptions",
            preconditions={k: live["metadata"][k] for k in ("uid", "resourceVersion")},
        ),
    )
    return True


def _find_child(custom_api, api, namespace, name):
    from kubernetes.client.exceptions import ApiException

    try:
        return custom_api.get_namespaced_custom_object(
            group="troshka.redhat.com",
            version="v1alpha1",
            namespace=namespace,
            plural="troshkavms",
            name=name,
        )
    except ApiException as error:
        if error.status != 404:
            raise
        roots = api.rows("TroshkaProject", namespace)
        require(
            len(roots) == 1 and "originCheckpoint" not in roots[0].get("spec", {}),
            "Origin absent child identity unresolved",
        )
        return None


def mark_replacement_child(custom_api, namespace, body):
    from app.services.native_managed_power import helpers

    api = helpers().PowerClient(custom_api.api_client)
    root = helpers().root_for(body, namespace, api)
    require(
        "originCheckpoint" in root.get("spec", {}),
        "Origin replacement root key missing",
    )
    protocol().mark_child(root, body)


def preserve_child_participation(existing, spec):
    field = protocol().PARTICIPATION
    if field in existing.get("spec", {}):
        spec[field] = existing["spec"][field]


def _wait_for_receipt(sessions, api, intent_id, request):
    """Backend worker only: reprepare after optimistic publication conflicts."""
    deadline = time.monotonic() + 1800
    while True:
        try:
            return request, service_request(sessions, api, intent_id, request)
        except (ValueError, AccountingUnavailable):
            # Cancellation/owner/provider drift is not a waitable storage hold.
            with sessions() as db:
                _context(db, intent_id)
            require(
                time.monotonic() < deadline,
                "Origin checkpoint wait timed out; original storage retained",
            )
            threading.Event().wait(5)
            request = _refresh_wait_request(api, request)


def _refresh_wait_request(api, request):
    root = api.get("TroshkaProject", request["namespace"], request["root"]["name"])
    target = api.get(
        request["target"]["kind"], request["namespace"], request["target"]["name"]
    )
    producer = api.get(
        request["producer"]["kind"], request["namespace"], request["producer"]["name"]
    )
    require(
        protocol().identity(root) == request["root"]
        and protocol().identity(producer) == request["producer"],
        "Origin waiting producer changed",
    )
    wanted = {
        k: v
        for k, v in request["target"].items()
        if k not in {"resourceVersion", "creationEpoch"}
    }
    require(
        protocol().identity(target) == wanted, "Origin waiting deletion target changed"
    )
    updated = protocol().build_request(
        api, root, request["namespace"], target, request["reason"], producer
    )
    protocol().publish(api, root, updated)
    return updated
