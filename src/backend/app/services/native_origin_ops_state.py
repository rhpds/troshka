"""Identity-locked evidence transactions for bounded native ops support work.

No network calls belong in this module. Every returned operation is detached
from its committed transaction before callers dispatch remote work.
"""

from uuid import uuid4

from app.models.native_origin_ops import NativeOriginOpsFact as Fact
from app.models.native_origin_ops import NativeOriginOpsOperation as Operation
from app.schemas.native_deployment_origin import NamespaceReceipt, OriginIntent
from app.services import native_deployment_origin as origin
from app.services import native_origin_writers as writers
from app.services.kubevirt_storage_enrollment import digest

CurrentSupport = tuple[OriginIntent, NamespaceReceipt]

LIMIT = 512


def operations(db, intent_id):
    rows = (
        db.query(Operation).filter_by(intent_id=intent_id).order_by(Operation.id).all()
    )
    origin.require(len(rows) <= LIMIT, "Native support operation history limit")
    for row in rows:
        writers._checked(row)
    return rows


def fact(db, operation, stage):
    row = db.get(Fact, operation.id + ":" + stage)
    if row is None:
        return None
    payload = writers._checked(row)
    origin.require(
        payload["operation_digest"] == operation.digest and payload["stage"] == stage,
        "Native support observation identity changed",
    )
    return payload["value"]


def retain(db, operation, stage, value):
    origin._locked(db, operation.intent_id, allow_cancelled=True)
    origin.require(
        db.get(Operation, operation.id).digest == operation.digest,
        "Native support operation changed",
    )
    payload = dict(operation_digest=operation.digest, stage=stage, value=value)
    existing = db.get(Fact, operation.id + ":" + stage)
    if existing is not None:
        origin.require(
            writers._checked(existing) == payload, "Native support observation changed"
        )
    else:
        origin.require(
            db.query(Fact).filter_by(operation_id=operation.id).count() < 128,
            "Native support observation history limit",
        )
        db.add(
            Fact(
                id=operation.id + ":" + stage,
                operation_id=operation.id,
                digest=digest(payload),
                payload=payload,
            )
        )
    db.flush()


def current(db, project_id, *, creating=False):
    intent = origin.active(db, project_id)
    if intent is None:
        return None
    intent, pointer = origin._locked(db, str(intent.id), allow_cancelled=True)
    origin.require(
        not creating or not pointer.cancelled, "Native support mutation cancelled"
    )
    namespace, _ = origin.receipts(db, str(intent.id))
    origin.require(
        namespace is not None, "Native support namespace receipt unavailable"
    )
    return intent, namespace


def begin(db, intent, kind, request, *, body=None):
    from app.services.native_origin_descendants import mark_support

    _, pointer = origin._locked(db, str(intent.id), allow_cancelled=True)
    origin.require(not pointer.cancelled, "Native support mutation cancelled")
    origin.require(
        len(operations(db, str(intent.id))) < LIMIT,
        "Native support operation history limit",
    )
    writer_id = mark_support(db, str(intent.project_id), commit=False)
    writer = writers.register_current(db, str(intent.id))
    origin.require(writer.id == writer_id, "Native support writer changed")
    if kind == "exec":
        pod = db.get(Operation, request["pod_operation"])
        origin.require(
            pod is not None
            and pod.intent_id == str(intent.id)
            and pod.digest == request["pod_digest"]
            and fact(db, pod, "exec_closed") is None,
            "Native support exec admission closed",
        )
    identity = str(uuid4())
    payload = dict(
        version=1,
        id=identity,
        intent_id=str(intent.id),
        writer_id=writer.id,
        writer_digest=writer.digest,
        project_id=str(intent.project_id),
        provider_id=str(intent.provider_id),
        host_id=str(intent.host_id),
        namespace=intent.namespace,
        kind=kind,
        request=request,
    )
    row = Operation(
        id=identity,
        intent_id=str(intent.id),
        writer_id=writer.id,
        digest=digest(payload),
        payload=payload,
        encrypted_body=_encrypt(body),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    db.expunge(row)
    return row


def _encrypt(body):
    import json

    from app.core.encryption import encrypt

    if body is None:
        return None
    encoded = json.dumps(body, sort_keys=True)
    origin.require(
        len(encoded.encode()) <= 1024 * 1024, "Native support body size limit"
    )
    return encrypt(encoded)


def body(operation):
    import json

    from app.core.encryption import decrypt

    origin.require(
        operation.encrypted_body is not None,
        "Native support protected body unavailable",
    )
    value = json.loads(decrypt(operation.encrypted_body))
    origin.require(
        digest(value) == operation.payload["request"]["body_digest"],
        "Native support protected body changed",
    )
    return value


def dispatch(db, operation):
    """Fence immediately before I/O; a committed dispatch remains debt on error."""
    _, pointer = origin._locked(db, operation.intent_id, allow_cancelled=True)
    origin.require(not pointer.cancelled, "Native support mutation cancelled")
    writer = writers.register_current(db, operation.intent_id)
    origin.require(
        writer.id == operation.writer_id
        and writer.digest == operation.payload["writer_digest"],
        "Native support dispatch execution changed",
    )
    origin.require(
        fact(db, operation, "dispatch") is None, "Native support dispatch replay denied"
    )
    if operation.payload["kind"] == "exec":
        pod = db.get(Operation, operation.payload["request"]["pod_operation"])
        origin.require(
            fact(db, pod, "exec_closed") is None, "Native support exec dispatch closed"
        )
    retain(
        db,
        operation,
        "dispatch",
        {"request_digest": digest(operation.payload["request"])},
    )
    db.commit()


def latest(db, intent_id, kind, name):
    rows = [
        r
        for r in operations(db, intent_id)
        if r.payload["kind"] == "create"
        and r.payload["request"]["kind"] == kind
        and r.payload["request"]["name"] == name
    ]
    predecessors = {r.payload["request"].get("predecessor") for r in rows}
    heads = [r for r in rows if r.id not in predecessors]
    origin.require(len(heads) <= 1, "Native support successor chain ambiguous")
    return heads[0] if heads else None


def close_exec(db, pod):
    """Fence local name-addressed dispatch before active-project replacement."""
    origin._locked(db, pod.intent_id, allow_cancelled=True)
    retain(db, pod, "exec_closed", {"pod_operation": pod.id})
    pending = [
        r
        for r in operations(db, pod.intent_id)
        if r.payload["kind"] == "exec"
        and r.payload["request"]["pod_operation"] == pod.id
        and fact(db, r, "dispatch") is not None
        and fact(db, r, "local_exit") is None
    ]
    db.commit()  # Keep the fence even when an already dispatched call is pending.
    origin.require(not pending, "Native support local exec dispatch still in flight")


def successor(db, replacement):
    rows = [
        r
        for r in operations(db, replacement.intent_id)
        if r.payload["kind"] == "create"
        and r.payload["request"].get("replacement") == replacement.id
    ]
    origin.require(len(rows) <= 1, "Native support successor ambiguous")
    return rows[0] if rows else None


def fence_snapshot(db, intent_id):
    """Cancelled dispatch ledger only; does not resolve any historical remote hold."""
    _, pointer = origin._locked(db, intent_id, allow_cancelled=True)
    origin.require(
        pointer.cancelled, "Native support dispatch fence requires cancellation"
    )
    return [
        dict(
            operation_id=r.id,
            operation_digest=r.digest,
            writer_id=r.writer_id,
            kind=r.payload["kind"],
            request=r.payload["request"],
            dispatched=fact(db, r, "dispatch"),
            local_exit=fact(db, r, "local_exit"),
            returned=fact(db, r, "returned"),
            absent=fact(db, r, "absent"),
        )
        for r in operations(db, intent_id)
    ]


def cleanup_obligations(db, intent_id, *, writer_id=None):
    """Known support effects stay bound to their original namespace lifetime.

    A returned exec is not remote quiescence. Only the whole-project namespace
    and independent private/shared storage proof may settle its boundary.
    Unknown dispatch or admission never acquires this bounded classification.
    """
    namespace, root = origin.receipts(db, intent_id)
    debt, boundaries = [], []
    for operation in operations(db, intent_id):
        if writer_id is not None and operation.writer_id != writer_id:
            continue
        if fact(db, operation, "dispatch") is None:
            continue  # No remote call; replacement plans are represented by their actual operations.
        target = _completed_target(db, operation, root)
        if target is None or namespace is None:
            debt.append("support_outcome_unresolved:" + operation.id)
        elif target != "Secret":
            from app.schemas.native_project_cleanup import SupportBoundary

            boundaries.append(
                SupportBoundary(
                    operation_id=operation.id,
                    operation_digest=operation.digest,
                    namespace_uid=namespace.namespace_uid,
                    target_kind=target[0],
                    target_uid=target[1],
                )
            )
    return debt, boundaries


def _verified_object(db, operation):
    returned = fact(db, operation, "returned")
    if (
        returned
        and returned.get("uid")
        and fact(db, operation, "admitted") == returned
        and fact(db, operation, "verified") == returned
    ):
        return returned
    return None


def _completed_target(db, operation, root):
    request = operation.payload["request"]
    kind = operation.payload["kind"]
    if fact(db, operation, "local_exit") is None:
        return None
    if kind == "create":
        verified = _verified_object(db, operation)
        if verified and request["kind"] in {"Pod", "Secret"}:
            return "Secret" if request["kind"] == "Secret" else ("Pod", verified["uid"])
    elif kind == "exec":
        pod = db.get(Operation, request["pod_operation"])
        verified = _verified_object(db, pod) if pod is not None else None
        if (
            verified
            and pod.intent_id == operation.intent_id
            and pod.digest == request["pod_digest"]
            and verified["uid"] == request["pod_uid"]
            and fact(db, operation, "returned")
            == {
                "pod_uid": request["pod_uid"],
                "command_digest": request["command_digest"],
            }
        ):
            return "Pod", verified["uid"]
    elif kind == "filepull" and root is not None:
        expected = {"root_uid": root.root_uid, "request_id": request["request_id"]}
        if (
            request["root_uid"] == root.root_uid
            and fact(db, operation, "submitted") == expected
            and fact(db, operation, "returned") == expected
        ):
            return "TroshkaProject", root.root_uid
    return None
