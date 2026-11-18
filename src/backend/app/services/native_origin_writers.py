"""Exact backend invocation registrations, completions, and unresolved support holds."""

import logging
import os
import threading
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from app.models.native_origin_cleanup import (
    NativeOriginRootSubmission,
    NativeOriginWriter,
    NativeOriginWriterHold,
    NativeOriginWriterTerminal,
)
from app.services import native_deployment_origin as origin
from app.services.kubevirt_storage_enrollment import digest

_PROCESS = str(uuid4())
_execution: ContextVar[dict[str, Any] | None] = ContextVar(
    "native_origin_writer_execution", default=None
)
_logger = logging.getLogger(__name__)


def _checked(row):
    origin.require(
        row.digest == digest(row.payload), "Native origin writer evidence changed"
    )
    return row.payload


def registrations(db, intent_id):
    rows = (
        db.query(NativeOriginWriter)
        .filter_by(intent_id=intent_id)
        .order_by(NativeOriginWriter.id)
        .all()
    )
    for row in rows:
        _checked(row)
    return rows


def register_current(db, intent_id):
    """Called under the origin identity lock, before any provider mutation."""
    from rq import get_current_job

    job = get_current_job()
    scope = _execution.get()
    if scope is not None and scope.get("tracking") == "native-joined-child-v1":
        origin.require(
            scope["identity"]["thread_id"] == threading.get_ident(),
            "Native origin child execution thread changed",
        )
        payload = dict(
            intent_id=intent_id, tracking=scope["tracking"], **scope["identity"]
        )
        execution_id = scope["identity"]["execution_id"]
    elif scope is not None:
        origin.require(
            job is not None
            and scope["identity"]["job_id"] == job.id
            and scope["identity"]["thread_id"] == threading.get_ident(),
            "Native origin execution context changed",
        )
        if TYPE_CHECKING:
            assert (
                job is not None
                and scope["identity"]["job_id"] == job.id
                and scope["identity"]["thread_id"] == threading.get_ident()
            )
        payload = dict(
            intent_id=intent_id, tracking="rq-execution-v2", **scope["identity"]
        )
        execution_id = f'{job.id}:{payload["rq_execution_id"]}'
    else:
        execution_id = f"untracked:{_PROCESS}:{threading.get_ident()}:{job.id if job else 'thread'}"
        payload = dict(
            intent_id=intent_id, execution_id=execution_id, tracking="untracked"
        )
    payload["execution_id"] = execution_id
    row = (
        db.query(NativeOriginWriter)
        .filter_by(intent_id=intent_id, execution_id=execution_id)
        .one_or_none()
    )
    if row is None:
        row = NativeOriginWriter(
            id=str(uuid4()),
            intent_id=intent_id,
            execution_id=execution_id,
            digest=digest(payload),
            payload=payload,
        )
        db.add(row)
        db.flush()
    _checked(row)
    origin.require(
        db.get(NativeOriginWriterTerminal, row.id) is None,
        "Native origin terminal writer cannot resume",
    )
    origin.require(
        row.payload == payload, "Native origin writer execution identity changed"
    )
    if scope is not None:
        scope["writers"].add((intent_id, row.id, row.digest))
    return row


def submitted_root(db, intent_id, writer, io_token):
    row = db.get(NativeOriginRootSubmission, intent_id)
    if row is None:
        payload = dict(
            intent_id=intent_id,
            writer_id=writer.id,
            writer_digest=writer.digest,
            io_token=io_token,
        )
        db.add(
            NativeOriginRootSubmission(
                intent_id=intent_id, digest=digest(payload), payload=payload
            )
        )
        db.flush()
    else:
        _checked(row)


def root_submitted(db, intent_id):
    row = db.get(NativeOriginRootSubmission, intent_id)
    if row is not None:
        _checked(row)
        return True
    # Roots recorded before this history existed are never classified as unsubmitted.
    return origin.receipts(db, intent_id)[1] is not None


def _snapshot(db, intent_id):
    _, pointer = origin._locked(db, intent_id, allow_cancelled=True)
    origin.require(
        pointer.cancelled, "Native origin writer termination requires cancellation"
    )
    rows = registrations(db, intent_id)
    origin.require(bool(rows), "Native origin original writer history unavailable")
    return dict(
        writers=[(r.id, r.digest) for r in rows],
        phase=pointer.io_phase,
        token=pointer.io_token,
    )


def observe_terminal(db, intent_id):
    """Only immutable completion from the exact invocation proves writer exit."""
    _snapshot(db, intent_id)
    rows = registrations(db, intent_id)
    proofs = {row.id: _terminal(db, row) for row in rows}
    from app.services.native_origin_descendants import require_dispatch_settled

    require_dispatch_settled(db, intent_id)
    db.commit()
    return proofs


def _terminal(db, writer, *, seen=None):
    seen = set() if seen is None else set(seen)
    origin.require(
        writer.id not in seen and len(seen) < 3,
        "Native descendant cycle or depth unavailable",
    )
    seen.add(writer.id)
    origin.require(
        writer.payload.get("tracking") in {"rq-execution-v2", "native-joined-child-v1"},
        "Native origin untracked or thread writer termination unavailable",
    )
    terminal = db.get(NativeOriginWriterTerminal, writer.id)
    origin.require(
        terminal is not None, "Native origin exact execution completion unavailable"
    )
    payload = _checked(terminal)
    origin.require(
        payload.get("version") == 2
        and payload.get("writer_digest") == writer.digest
        and payload.get("execution") == writer.payload,
        "Native origin exact execution termination evidence changed",
    )
    _require_no_hold(db, writer, seen=seen)
    return payload


def _require_no_hold(db, writer, *, seen=None):
    from app.services.native_origin_descendants import require_remote_settled

    require_remote_settled(db, writer)
    hold = db.get(NativeOriginWriterHold, writer.id)
    if hold is not None:
        from app.services.native_origin_descendants import resolve_hold

        resolve_hold(db, writer, hold, seen=seen or {writer.id})


def mark_async_support(db, project_id):
    """Retain unsupported support dispatch before any Pod mutation or enqueue."""
    intent = origin.active(db, project_id)
    if intent is None:
        return
    _, pointer = origin._locked(db, str(intent.id), allow_cancelled=True)
    origin.require(not pointer.cancelled, "Native origin support dispatch cancelled")
    writer = register_current(db, str(intent.id))
    hold = db.get(NativeOriginWriterHold, writer.id)
    if hold is None:
        payload = dict(
            writer_id=writer.id,
            writer_digest=writer.digest,
            reason="native_ops_pod_support_lifecycle_unregistered",
        )
        db.add(
            NativeOriginWriterHold(
                writer_id=writer.id, digest=digest(payload), payload=payload
            )
        )
    else:
        _checked(hold)
    db.commit()


def run_execution(worker, job, invoke):
    """Bind the installed worker's in-memory execution before dispatch, without Redis I/O."""
    execution = worker.execution
    if (
        execution is None
        or execution.job_id != job.id
        or execution.worker_name != worker.name
    ):
        return (
            invoke()
        )  # Unsupported worker registrations deliberately remain untracked.
    identity = dict(
        job_id=job.id,
        rq_execution_id=execution.id,
        worker_name=worker.name,
        execution_created_at=execution.created_at.isoformat(),
        process_id=os.getpid(),
        process_incarnation=_PROCESS,
        thread_id=threading.get_ident(),
    )
    scope = dict(identity=identity, writers=set(), threads=set(threading.enumerate()))
    token = _execution.set(scope)
    outcome = None
    try:
        result = invoke()
        outcome = "failed" if result is False else "returned"
        return result
    except Exception:
        outcome = "failed"
        raise
    finally:
        _execution.reset(token)
        _complete_execution(scope, outcome)


def _complete_execution(scope, outcome):
    from app.services.native_origin_checkpoints import observer_threads

    # Detached participating work has no join contract here. Keep its execution
    # unresolved instead of mistaking RQ return/cancel/status for writer exit.
    if (
        outcome is None
        or set(threading.enumerate())
        - scope["threads"]
        - scope.get("children", set())
        - observer_threads()
    ):
        return
    from app.core.database import SessionLocal

    for intent_id, writer_id, writer_digest in sorted(scope["writers"]):
        try:
            with SessionLocal() as db:
                origin._locked(db, intent_id, allow_cancelled=True)
                writer = db.get(NativeOriginWriter, writer_id)
                origin.require(
                    writer is not None and writer.digest == writer_digest,
                    "Native origin registered writer changed before completion",
                )
                if TYPE_CHECKING:
                    assert writer is not None and writer.digest == writer_digest
                _checked(writer)
                # Invocation exit is a separate fact from descendant settlement.
                # The immutable hold is still enforced by _terminal on every use.
                from app.services.native_origin_descendants import close_writer

                close_writer(db, writer)
                payload = dict(
                    version=2,
                    writer_id=writer.id,
                    writer_digest=writer.digest,
                    execution=writer.payload,
                    outcome=outcome,
                )
                db.add(
                    NativeOriginWriterTerminal(
                        writer_id=writer.id, digest=digest(payload), payload=payload
                    )
                )
                db.commit()
        except Exception:
            # A lost completion write is a hold; never mask the producer outcome.
            _logger.exception(
                "Native origin execution completion could not be retained"
            )
