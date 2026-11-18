"""Bounded exact native monitor/recert dispatch, invocation closure and settlement.

All helpers changing evidence use the origin identity lock. No queue or provider
I/O happens in these transactions. Historical unsupported holds stay unsupported.
"""

import os
import threading
from typing import TYPE_CHECKING
from uuid import uuid4

from app.models.native_origin_cleanup import NativeOriginWriter, NativeOriginWriterHold
from app.models.native_origin_descendants import (
    NativeOriginDescendantClaim as Claim,
)
from app.models.native_origin_descendants import (
    NativeOriginDescendantHandoff as Handoff,
)
from app.models.native_origin_descendants import (
    NativeOriginDispatchClosure as DispatchClosure,
)
from app.models.native_origin_descendants import (
    NativeOriginHoldResolution as Resolution,
)
from app.models.native_origin_descendants import (
    NativeOriginRemoteHold as RemoteHold,
)
from app.models.native_origin_descendants import (
    NativeOriginWriterClosure as Closure,
)
from app.services import native_deployment_origin as origin
from app.services import native_origin_writers as writers
from app.services.kubevirt_storage_enrollment import digest

_REASON = "native_ops_pod_registered_descendants_v1"
_LIMIT = 512


def _put(db, model, key, payload):
    db.add(model(**key, payload=payload, digest=digest(payload)))
    db.flush()


def _handoffs(db, intent_id, parent_id=None, *, all_parents=False):
    query = db.query(Handoff).filter_by(intent_id=intent_id)
    if not all_parents:
        query = query.filter_by(parent_id=parent_id)
    rows = query.order_by(Handoff.id).all()
    for row in rows:
        _checked_handoff(row)
    return rows


def _checked_handoff(row):
    payload = writers._checked(row)
    origin.require(
        payload.get("handoff_id") == row.id
        and payload.get("intent_id") == row.intent_id
        and payload.get("parent_id") == row.parent_id,
        "Native descendant handoff identity changed",
    )
    return payload


def _pins(rows):
    return [{"id": row.id, "digest": row.digest} for row in rows]


def _open(db, project_id):
    intent = origin.active(db, project_id)
    if intent is None:
        return None
    intent, pointer = origin._locked(db, str(intent.id), allow_cancelled=True)
    origin.require(
        not pointer.cancelled and db.get(DispatchClosure, str(intent.id)) is None,
        "Native origin descendant dispatch cancelled",
    )
    return intent


def mark_support(db, project_id, *, remote_pending=False, commit=True):
    intent = _open(db, project_id)
    if intent is None:
        return None
    writer = writers.register_current(db, str(intent.id))
    hold = db.get(NativeOriginWriterHold, writer.id)
    if hold is None:
        _put(
            db,
            NativeOriginWriterHold,
            {"writer_id": writer.id},
            dict(writer_id=writer.id, writer_digest=writer.digest, reason=_REASON),
        )
    else:
        origin.require(
            writers._checked(hold).get("reason") == _REASON,
            "Native origin historical asynchronous support hold unresolved",
        )
    if remote_pending:
        _retain_remote_hold(db, writer)
    if commit:
        db.commit()
    return writer.id


def _retain_remote_hold(db, writer):
    payload = dict(
        writer_id=writer.id,
        writer_digest=writer.digest,
        reason="native_ops_pod_remote_mutations_untracked_v1",
    )
    existing = db.get(RemoteHold, writer.id)
    if existing is None:
        _put(db, RemoteHold, {"writer_id": writer.id}, payload)
    else:
        origin.require(
            writers._checked(existing) == payload, "Native support remote hold changed"
        )


def require_remote_settled(db, writer):
    hold = db.get(RemoteHold, writer.id)
    if hold is not None:
        writers._checked(hold)
        origin.require(
            False,
            "Native origin asynchronous support remote operations remain untracked",
        )
    from app.services.native_origin_ops_state import cleanup_obligations

    debt, boundaries = cleanup_obligations(db, writer.intent_id, writer_id=writer.id)
    origin.require(
        not debt and not boundaries,
        "Native origin support remote operations require namespace and storage settlement",
    )


def mark_remote_support(project_id):
    """4c2 must independently settle remote mutations; local exit is insufficient."""
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        mark_support(db, project_id, remote_pending=True)


def _parent(db, intent):
    if writers._execution.get() is None:
        return None
    writer = writers.register_current(db, str(intent.id))
    origin.require(
        db.get(Closure, writer.id) is None,
        "Native origin descendant set already closed",
    )
    hold = db.get(NativeOriginWriterHold, writer.id)
    if hold is None:
        _put(
            db,
            NativeOriginWriterHold,
            {"writer_id": writer.id},
            dict(writer_id=writer.id, writer_digest=writer.digest, reason=_REASON),
        )
    else:
        origin.require(
            writers._checked(hold).get("reason") == _REASON,
            "Native origin historical asynchronous support hold unresolved",
        )
    return writer


def prepare(db, project_id, role, arguments):
    """Commit before enqueue/start; unknown dispatch outcomes remain liabilities."""
    intent = _open(db, project_id)
    if intent is None:
        return None
    origin.require(role in {"monitor", "recert"}, "Unsupported native descendant role")
    origin.require(
        arguments.get("host_id", str(intent.host_id)) == str(intent.host_id),
        "Native descendant host changed",
    )
    rows = _handoffs(db, str(intent.id), all_parents=True)
    origin.require(len(rows) < _LIMIT, "Native origin descendant history limit")
    parent = _parent(db, intent)
    origin.require(
        role == "monitor" or parent is not None, "Native recert requires exact parent"
    )
    handoff_id = str(uuid4())
    payload = dict(
        version=1,
        handoff_id=handoff_id,
        intent_id=str(intent.id),
        project_id=project_id,
        provider_id=str(intent.provider_id),
        host_id=str(intent.host_id),
        parent_id=parent.id if parent else None,
        parent_digest=parent.digest if parent else None,
        role=role,
        arguments_digest=digest(arguments),
        dispatcher=dict(
            process=os.getpid(),
            incarnation=writers._PROCESS,
            thread=threading.get_ident(),
        ),
    )
    db.add(
        Handoff(
            id=handoff_id,
            intent_id=str(intent.id),
            parent_id=payload["parent_id"],
            digest=digest(payload),
            payload=payload,
        )
    )
    db.commit()
    return handoff_id


def close_writer(db, writer):
    """An invocation can never add another handoff once its exact exit is retained."""
    payload = dict(
        writer_id=writer.id,
        writer_digest=writer.digest,
        handoffs=_pins(_handoffs(db, writer.intent_id, writer.id)),
    )
    existing = db.get(Closure, writer.id)
    if existing is None:
        _put(db, Closure, {"writer_id": writer.id}, payload)
    else:
        origin.require(
            writers._checked(existing) == payload, "Native descendant closure changed"
        )


def close_origin(db, intent_id):
    """Called in the cancellation transaction; scanner dispatch is now closed."""
    payload = dict(
        intent_id=intent_id, handoffs=_pins(_handoffs(db, intent_id, all_parents=True))
    )
    existing = db.get(DispatchClosure, intent_id)
    if existing is None:
        _put(db, DispatchClosure, {"intent_id": intent_id}, payload)
    else:
        origin.require(
            writers._checked(existing) == payload,
            "Native origin dispatch closure changed",
        )


def _claim(db, handoff_id, project_id, role, arguments):
    row = db.get(Handoff, handoff_id)
    origin.require(row is not None, "Native descendant handoff unavailable")
    payload = _checked_handoff(row)
    intent, pointer = origin._locked(db, row.intent_id, allow_cancelled=True)
    origin.require(
        payload["project_id"] == project_id == str(intent.project_id)
        and payload["host_id"] == str(intent.host_id)
        and payload["provider_id"] == str(intent.provider_id)
        and payload["role"] == role
        and payload["arguments_digest"] == digest(arguments),
        "Native descendant identity changed",
    )
    origin.require(
        db.get(Claim, row.id) is None, "Native descendant handoff already claimed"
    )
    scope = writers._execution.get()
    origin.require(scope is not None, "Native descendant exact execution unavailable")
    if TYPE_CHECKING:
        assert scope is not None
    _execution_identity(scope, role, handoff_id)
    result = dict(
        handoff_id=row.id,
        handoff_digest=row.digest,
        execution=scope["identity"],
        outcome="never_entered" if pointer.cancelled else "claimed",
    )
    if not pointer.cancelled:
        writer = writers.register_current(db, row.intent_id)
        origin.require(
            writer.payload["tracking"] in {"rq-execution-v2", "native-joined-child-v1"},
            "Native descendant exact execution unavailable",
        )
        result.update(writer_id=writer.id, writer_digest=writer.digest)
    _put(db, Claim, {"handoff_id": row.id}, result)
    db.commit()
    return not pointer.cancelled


def _execution_identity(scope, role, handoff_id):
    from rq import get_current_job

    identity = scope["identity"]
    origin.require(
        identity["thread_id"] == threading.get_ident(),
        "Native descendant execution thread changed",
    )
    if role == "recert":
        origin.require(
            scope.get("tracking") == "native-joined-child-v1"
            and identity.get("execution_id", "").startswith(f"joined:{handoff_id}:"),
            "Native recert exact child identity unavailable",
        )
    else:
        job = get_current_job()
        origin.require(
            scope.get("tracking") is None
            and job is not None
            and identity.get("job_id") == job.id
            and identity.get("rq_execution_id"),
            "Native monitor exact RQ invocation unavailable",
        )


def invoke(handoff_id, project_id, role, arguments, function):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        if handoff_id is None:
            origin.require(
                origin.active(db, project_id) is None,
                "Native descendant handoff required",
            )
            permitted = True
        else:
            permitted = _claim(db, handoff_id, project_id, role, arguments)
    if permitted:
        return function()
    return None


def _settled(db, handoff, seen):
    claim = db.get(Claim, handoff.id)
    origin.require(
        claim is not None, "Native origin descendant dispatch remains unresolved"
    )
    value = writers._checked(claim)
    origin.require(
        value["handoff_id"] == handoff.id and value["handoff_digest"] == handoff.digest,
        "Native origin descendant claim changed",
    )
    if value["outcome"] == "claimed":
        child = db.get(NativeOriginWriter, value["writer_id"])
        origin.require(
            child is not None
            and child.digest == value["writer_digest"]
            and child.intent_id == handoff.intent_id
            and child.id not in seen,
            "Native origin descendant execution changed",
        )
        writers._terminal(db, child, seen=seen)
    else:
        origin.require(
            value["outcome"] == "never_entered", "Native descendant outcome unavailable"
        )
    return dict(
        handoff_id=handoff.id, handoff_digest=handoff.digest, claim_digest=claim.digest
    )


def resolve_hold(db, writer, hold, *, seen):
    value = writers._checked(hold)
    origin.require(
        value.get("reason") == _REASON and value.get("writer_digest") == writer.digest,
        "Native origin asynchronous support writer termination unavailable",
    )
    closure = db.get(Closure, writer.id)
    origin.require(closure is not None, "Native origin descendant set is not closed")
    handoffs = _handoffs(db, writer.intent_id, writer.id)
    origin.require(
        writers._checked(closure)
        == dict(
            writer_id=writer.id, writer_digest=writer.digest, handoffs=_pins(handoffs)
        ),
        "Native origin descendant closure changed",
    )
    facts = [_settled(db, row, seen) for row in handoffs]
    payload = dict(
        writer_id=writer.id,
        writer_digest=writer.digest,
        hold_digest=hold.digest,
        closure_digest=closure.digest,
        descendants=facts,
    )
    existing = db.get(Resolution, writer.id)
    if existing is None:
        _put(db, Resolution, {"writer_id": writer.id}, payload)
    else:
        origin.require(
            writers._checked(existing) == payload, "Native support settlement changed"
        )


def require_dispatch_settled(db, intent_id):
    close_origin(db, intent_id)
    for handoff in _handoffs(db, intent_id, all_parents=True):
        _settled(db, handoff, set())


def _run_joined(handoff_id, project_id, arguments, function):
    identity = dict(
        execution_id=f"joined:{handoff_id}:{uuid4()}",
        process_id=os.getpid(),
        process_incarnation=writers._PROCESS,
        thread_id=threading.get_ident(),
    )
    scope = dict(
        identity=identity,
        tracking="native-joined-child-v1",
        writers=set(),
        threads=set(threading.enumerate()),
        children=set(),
    )
    token = writers._execution.set(scope)
    outcome = None
    try:
        invoke(handoff_id, project_id, "recert", arguments, function)
        outcome = "returned"
    except Exception:
        outcome = "failed"
        raise
    finally:
        writers._execution.reset(token)
        writers._complete_execution(scope, outcome)


def start_joined(project_id, arguments, function):
    """Only participating recert work receives an explicit tracked child scope."""
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        handoff_id = prepare(db, project_id, "recert", arguments)
    if handoff_id is None:
        return None
    scope = writers._execution.get()
    origin.require(scope is not None, "Native recert parent execution unavailable")
    if TYPE_CHECKING:
        assert scope is not None
    child = threading.Thread(
        target=_run_joined,
        args=(handoff_id, project_id, arguments, function),
        daemon=True,
    )
    scope.setdefault("children", set()).add(child)
    child.start()
    return child


def join(child, timeout=55):
    """Timeout leaves the child's exact exit absent; it never fabricates completion."""
    if child is not None:
        child.join(timeout=timeout)
