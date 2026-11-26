import uuid

import pytest
from fastapi import HTTPException

from app.services.assistant.policy import authorize


@pytest.mark.parametrize("state", ["failed", "deleting", "cleanup_pending", "draft"])
def test_failure_states_remain_diagnosable(case, state):
    db, user, project, conversation, _, now = case
    project.state = state
    db.commit()
    assert authorize(db, user, conversation.id, "project", now).project_id == project.id


@pytest.mark.parametrize(
    "change", ["foreign", "admin", "expired", "revoked", "account", "guest", "deleted"]
)
def test_grant_is_not_an_elevation(case, change):
    db, user, project, conversation, grant, now = case
    scope = "project"
    if change in ("foreign", "admin"):
        project.owner_id = str(uuid.uuid4())
        user.role = "admin" if change == "admin" else "user"
    if change == "expired":
        grant.expires_at = now
    if change == "revoked":
        grant.revoked_at = now
    if change == "account":
        user.access_ends_at = now
    if change == "guest":
        scope = "guest"
    if change == "deleted":
        db.delete(project)
    db.commit()
    with pytest.raises(HTTPException):
        authorize(db, user, conversation.id, scope, now)
