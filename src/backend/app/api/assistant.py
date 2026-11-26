"""Private project assistant API. No repair endpoints are exposed."""

import datetime as dt
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import SessionLocal, get_db
from app.models.assistant import (
    AssistantAuditEvent,
    AssistantConversation,
    AssistantEvidence,
    AssistantGrant,
    AssistantMessage,
)
from app.models.user import User
from app.schemas.assistant import ConversationCreate, GrantCreate, MessageCreate
from app.services.access_window import _as_utc
from app.services.assistant.policy import (
    active_grant,
    conversation_access,
    owned_project,
)
from app.services.assistant.projection import safe_text
from app.services.assistant.runs import RunLease, cancel
from app.services.assistant.runtime import Turn
from app.services.assistant.settings import enabled

router = APIRouter(prefix="/assistant", tags=["assistant"])
Db = Annotated[Session, Depends(get_db)]


def assistant_user(request: Request, user: User = Depends(get_current_user)):
    if getattr(request.state, "api_key", None) or request.headers.get(
        "authorization", ""
    ).startswith("Bearer trk_"):
        raise HTTPException(403, "Assistant requires an interactive user session")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site assistant request denied")
    if not enabled():
        raise HTTPException(503, "Assistant is disabled")
    return user


Actor = Annotated[User, Depends(assistant_user)]


@router.get("/capabilities")
def capabilities(user: Annotated[User, Depends(get_current_user)]):
    return {"enabled": enabled(), "repairs": False}


@router.post("/conversations", status_code=201)
def create_conversation(body: ConversationCreate, user: Actor, db: Db):
    if body.project_id:
        owned_project(db, user, str(body.project_id))
    conversation = AssistantConversation(
        owner_id=user.id, project_id=str(body.project_id) if body.project_id else None
    )
    db.add(conversation)
    db.commit()
    return {"id": conversation.id, "project_id": conversation.project_id}


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: UUID, user: Actor, db: Db):
    cid = str(conversation_id)
    conversation = conversation_access(db, user, cid)
    grant = active_grant(db, cid)
    messages = (
        db.query(AssistantMessage)
        .filter_by(conversation_id=cid)
        .order_by(AssistantMessage.created_at)
        .limit(200)
        .all()
    )
    evidence = (
        db.query(AssistantEvidence)
        .filter_by(conversation_id=cid)
        .order_by(AssistantEvidence.created_at)
        .limit(100)
        .all()
    )
    return {
        "id": cid,
        "project_id": conversation.project_id,
        "grant": {
            "id": grant.id,
            "guest_checks": grant.guest_checks,
            "expires_at": _as_utc(grant.expires_at).isoformat(),
        }
        if grant
        else None,
        "messages": [
            {
                "role": m.role,
                "text": m.text,
                "status": m.status,
                "request_id": m.request_id,
            }
            for m in messages
        ],
        "evidence": [e.payload for e in evidence],
    }


@router.post("/conversations/{conversation_id}/grant")
def grant_access(conversation_id: UUID, body: GrantCreate, user: Actor, db: Db):
    cid = str(conversation_id)
    conversation = conversation_access(db, user, cid)
    if not conversation.project_id:
        raise HTTPException(400, "This conversation is not bound to a project")
    now = dt.datetime.now(dt.UTC)
    current = active_grant(db, cid, now)
    if current and current.guest_checks == body.guest_checks:
        return {
            "id": current.id,
            "guest_checks": current.guest_checks,
            "expires_at": _as_utc(current.expires_at).isoformat(),
        }
    _revoke(db, cid, now)
    grant = AssistantGrant(
        conversation_id=cid,
        guest_checks=body.guest_checks,
        expires_at=now + dt.timedelta(minutes=30),
    )
    db.add(grant)
    db.add(
        AssistantAuditEvent(
            actor_id=user.id,
            conversation_id=cid,
            check="grant",
            outcome="guest" if body.guest_checks else "project",
        )
    )
    db.commit()
    return {
        "id": grant.id,
        "guest_checks": grant.guest_checks,
        "expires_at": _as_utc(grant.expires_at).isoformat(),
    }


def _revoke(db, cid, now):
    db.query(AssistantGrant).filter_by(conversation_id=cid, revoked_at=None).update(
        {"revoked_at": now}
    )


@router.delete("/conversations/{conversation_id}/grant", status_code=204)
def revoke_access(conversation_id: UUID, user: Actor, db: Db):
    cid = str(conversation_id)
    conversation_access(db, user, cid)
    _revoke(db, cid, dt.datetime.now(dt.UTC))
    db.add(
        AssistantAuditEvent(
            actor_id=user.id, conversation_id=cid, check="revoke", outcome="revoked"
        )
    )
    db.commit()  # Revoke even when Redis is unavailable.
    cancel(cid)


@router.delete("/conversations/{conversation_id}/active-run", status_code=204)
def cancel_run(conversation_id: UUID, user: Actor, db: Db):
    conversation_access(db, user, str(conversation_id))
    cancel(str(conversation_id))


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: UUID, user: Actor, db: Db):
    from app.services.assistant.retention import delete_content

    cid = str(conversation_id)
    conversation_access(db, user, cid)
    delete_content(db, cid)
    db.add(
        AssistantAuditEvent(
            actor_id=user.id, conversation_id=cid, check="delete", outcome="deleted"
        )
    )
    db.commit()
    cancel(cid)


def _existing(db, cid, request_id):
    return (
        db.query(AssistantMessage)
        .filter_by(conversation_id=cid, request_id=request_id, role="user")
        .first()
    )


def _replay(db, cid, request_id, status):
    if status == "running":
        raise HTTPException(
            409,
            "Request is active or interrupted; refresh history before starting a new request",
        )
    answer = (
        db.query(AssistantMessage)
        .filter_by(conversation_id=cid, request_id=request_id, role="assistant")
        .first()
    )
    events = [
        {
            "sequence": 1,
            "request_id": request_id,
            "type": "answer_delta" if answer else "error",
            "payload": {"text": answer.text}
            if answer
            else {
                "message": "Previous request did not complete; use a new request ID to retry explicitly."
            },
        },
        {"sequence": 2, "request_id": request_id, "type": "done", "payload": {}},
    ]
    return StreamingResponse(
        iter([_sse(e) for e in events]),
        media_type="text/event-stream",
        headers=_HEADERS,
    )


_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


def _sse(event):
    return f"id: {event['sequence']}\ndata: {json.dumps(event)}\n\n"


@router.post("/conversations/{conversation_id}/messages")
def send_message(conversation_id: UUID, body: MessageCreate, user: Actor, db: Db):
    cid, request_id = str(conversation_id), str(body.request_id)
    conversation_access(db, user, cid)
    existing = _existing(db, cid, request_id)
    if existing:
        return _replay(db, cid, request_id, existing.status)
    lease = RunLease(user.id, cid)
    lease.acquire()
    try:
        grant = active_grant(db, cid)
        grant_id = grant.id if grant else None
        db.add(
            AssistantMessage(
                conversation_id=cid,
                request_id=request_id,
                role="user",
                text=safe_text(body.text),
                grant_id=grant_id,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        lease.release()
        raise HTTPException(409, "Unable to start request; refresh history") from None
    turn = Turn(SessionLocal, user.id, cid, request_id, body.text, grant_id, lease)

    async def events():
        async for event in turn.run():
            yield _sse(event)

    return StreamingResponse(events(), media_type="text/event-stream", headers=_HEADERS)
