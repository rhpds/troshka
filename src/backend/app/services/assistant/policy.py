"""No admin bypass, no client-selected authority, and no cached consent."""

import datetime as dt

from fastapi import HTTPException

from app.models.assistant import AssistantConversation, AssistantGrant
from app.models.project import Project
from app.services.access_window import _as_utc, user_within_access_window
from app.services.assistant.contracts import AuthorizedContext


def conversation_access(db, actor, conversation_id, now=None):
    now = now or dt.datetime.now(dt.UTC)
    if (
        not actor
        or not actor.access_enabled
        or not user_within_access_window(actor, now)
    ):
        raise HTTPException(403, "Account access is unavailable")
    conversation = db.get(AssistantConversation, conversation_id)
    if not conversation or conversation.owner_id != actor.id:
        raise HTTPException(404, "Conversation not found")
    if _as_utc(conversation.expires_at) <= now:
        raise HTTPException(410, "Conversation expired")
    if conversation.project_id:
        owned_project(db, actor, conversation.project_id)
    return conversation


def owned_project(db, actor, project_id):
    project = db.get(Project, project_id)
    if not project or project.owner_id != actor.id:
        raise HTTPException(404, "Project not found")
    return project


def active_grant(db, conversation_id, now=None):
    now = now or dt.datetime.now(dt.UTC)
    return (
        db.query(AssistantGrant)
        .filter(
            AssistantGrant.conversation_id == conversation_id,
            AssistantGrant.revoked_at.is_(None),
            AssistantGrant.expires_at > now,
        )
        .order_by(AssistantGrant.expires_at.desc())
        .first()
    )


def authorize(db, actor, conversation_id, scope="project", now=None):
    now = now or dt.datetime.now(dt.UTC)
    conversation = conversation_access(db, actor, conversation_id, now)
    grant = active_grant(db, conversation_id, now)
    if not conversation.project_id or not grant:
        raise HTTPException(403, "Diagnostic consent is required")
    if scope == "guest" and not grant.guest_checks:
        raise HTTPException(403, "Guest diagnostic consent is required")
    return AuthorizedContext(
        actor.id,
        conversation.id,
        conversation.project_id,
        grant.id,
        _as_utc(grant.expires_at),
    )
