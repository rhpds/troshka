import datetime as dt
import uuid

import pytest

from app.models.assistant import AssistantConversation, AssistantGrant
from app.models.project import Project
from app.models.user import User


@pytest.fixture
def case(isolated_quota_database):
    now = dt.datetime.now(dt.UTC)
    with isolated_quota_database() as db:
        user = User(email=f"{uuid.uuid4()}@example.test", role="user")
        db.add(user)
        db.flush()
        project = Project(name="Diagnostic fixture", owner_id=user.id, state="failed")
        db.add(project)
        db.flush()
        conversation = AssistantConversation(owner_id=user.id, project_id=project.id)
        db.add(conversation)
        db.flush()
        grant = AssistantGrant(
            conversation_id=conversation.id, expires_at=now + dt.timedelta(minutes=30)
        )
        db.add(grant)
        db.commit()
        yield db, user, project, conversation, grant, now
