import datetime as dt
import uuid
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api import assistant
from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.assistant import (
    AssistantAuditEvent,
    AssistantConversation,
    AssistantMessage,
)
from app.services.assistant.retention import purge_expired


@pytest.fixture
def api(case, isolated_quota_database):
    _, user, _, _, _, _ = case
    app = FastAPI()
    app.include_router(assistant.router)

    def db():
        with isolated_quota_database() as session:
            yield session

    def actor(request: Request):
        request.state.api_key = object() if request.headers.get("x-test-key") else None
        return user

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = actor
    with patch.object(assistant, "enabled", return_value=True), patch.object(
        assistant, "cancel"
    ):
        with TestClient(app) as client:
            yield client


def test_panel_open_does_not_grant_access(case, api):
    _, _, project, _, _, _ = case
    created = api.post("/assistant/conversations", json={"project_id": project.id})
    assert created.status_code == 201
    history = api.get(f"/assistant/conversations/{created.json()['id']}").json()
    assert history["grant"] is None
    assert history["messages"] == []


def test_grant_revoke_and_no_silent_extension(case, api):
    _, _, _, conversation, _, _ = case
    url = f"/assistant/conversations/{conversation.id}"
    first = api.post(url + "/grant", json={}).json()
    second = api.post(url + "/grant", json={}).json()
    assert first == second
    assert not first["guest_checks"]
    assert api.delete(url + "/grant").status_code == 204
    assert api.get(url).json()["grant"] is None


def test_api_keys_cross_site_and_foreign_ids_denied(case, api):
    _, _, _, conversation, _, _ = case
    url = f"/assistant/conversations/{conversation.id}"
    assert api.get(url, headers={"x-test-key": "yes"}).status_code == 403
    assert (
        api.post(
            url + "/grant", json={}, headers={"sec-fetch-site": "cross-site"}
        ).status_code
        == 403
    )
    assert api.get(f"/assistant/conversations/{uuid.uuid4()}").status_code == 404


def test_delete_removes_content_but_retains_audit(case, api):
    db, user, _, conversation, _, _ = case
    cid = conversation.id
    db.add(
        AssistantMessage(
            conversation_id=cid,
            request_id=str(uuid.uuid4()),
            role="user",
            text="fixture",
        )
    )
    db.commit()
    assert api.delete(f"/assistant/conversations/{cid}").status_code == 204
    db.expire_all()
    assert db.query(AssistantMessage).filter_by(conversation_id=cid).count() == 0
    assert db.query(AssistantAuditEvent).filter_by(conversation_id=cid).count() == 1


def test_retention_expired_history_inaccessible(case, api):
    db, _, _, conversation, _, now = case
    conversation.expires_at = now - dt.timedelta(seconds=1)
    db.commit()
    assert api.get(f"/assistant/conversations/{conversation.id}").status_code == 410
    cid = conversation.id
    assert purge_expired(db, now) == 1
    assert db.get(AssistantConversation, cid) is None


def test_disabled_feature_leaves_capabilities_readable(case, api):
    with patch.object(assistant, "enabled", return_value=False):
        assert api.get("/assistant/capabilities").json()["enabled"] is False
        assert api.post("/assistant/conversations", json={}).status_code == 503
