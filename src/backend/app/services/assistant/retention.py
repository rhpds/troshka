"""Bounded retention with a fleet-wide daily admission key."""

import datetime as dt
import threading

from app.models.assistant import (
    AssistantAuditEvent,
    AssistantConversation,
    AssistantEvidence,
    AssistantGrant,
    AssistantMessage,
)


def delete_content(db, conversation_id):
    for model in (AssistantEvidence, AssistantMessage, AssistantGrant):
        db.query(model).filter_by(conversation_id=conversation_id).delete()
    db.query(AssistantConversation).filter_by(id=conversation_id).delete()


def purge_expired(db, now, batch_size=100):
    conversations = (
        db.query(AssistantConversation)
        .filter(AssistantConversation.expires_at <= now)
        .limit(batch_size)
        .all()
    )
    for conversation in conversations:
        delete_content(db, conversation.id)
    audit_ids = [
        row.id
        for row in db.query(AssistantAuditEvent)
        .filter(AssistantAuditEvent.created_at <= now - dt.timedelta(days=30))
        .limit(batch_size)
        .all()
    ]
    if audit_ids:
        db.query(AssistantAuditEvent).filter(
            AssistantAuditEvent.id.in_(audit_ids)
        ).delete(synchronize_session=False)
    db.commit()
    return len(conversations) + len(audit_ids)


def _sweep():
    from app.core.database import SessionLocal
    from app.services.assistant.runs import client

    connection = client()
    key = "assistant:retention:daily"
    if not connection.set(key, "running", nx=True, ex=86400):
        return
    try:
        for _ in range(10):
            with SessionLocal() as db:
                if purge_expired(db, dt.datetime.now(dt.UTC)) < 100:
                    break
    except Exception:
        connection.delete(key)
        raise


def start_retention():
    stop = threading.Event()

    def run():
        import logging

        while not stop.is_set():
            try:
                _sweep()
            except Exception:
                logging.getLogger(__name__).warning("Assistant retention unavailable")
            stop.wait(3600)

    threading.Thread(target=run, name="assistant-retention", daemon=True).start()
    return stop
