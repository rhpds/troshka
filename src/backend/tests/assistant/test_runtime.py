import asyncio
import uuid
from unittest.mock import Mock, patch

from app.models.assistant import AssistantEvidence, AssistantGrant, AssistantMessage
from app.services.assistant.runtime import Turn


def make_turn(case, sessions):
    db, user, _, conversation, grant, _ = case
    request_id = str(uuid.uuid4())
    db.add(
        AssistantMessage(
            conversation_id=conversation.id,
            request_id=request_id,
            role="user",
            text="Why did installation fail?",
            grant_id=grant.id,
        )
    )
    db.commit()
    return Turn(
        sessions,
        user.id,
        conversation.id,
        request_id,
        "Why did installation fail?",
        grant.id,
        Mock(),
    )


class FakeModel:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call1",
                        "name": "get_install_failure",
                        "input": {},
                    }
                ]
            }
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Installation evidence is insufficient; check the earliest installer error.",
                }
            ]
        }


def test_tool_loop_persists_safe_evidence(case, isolated_quota_database):
    turn = make_turn(case, isolated_quota_database)

    async def run():
        return [e async for e in turn.run(FakeModel())]

    with (
        patch("app.services.assistant.runtime.enabled", return_value=True),
        patch("app.services.assistant.dispatcher.enabled", return_value=True),
    ):
        events = asyncio.run(run())
    assert events[-1]["type"] == "done"
    assert any(e["type"] == "check_finished" for e in events)
    with isolated_quota_database() as db:
        assert db.query(AssistantEvidence).count() == 1


def test_revoke_during_inference_suppresses_answer(case, isolated_quota_database):
    turn = make_turn(case, isolated_quota_database)

    class RevokeModel:
        async def complete(self, messages, tools):
            with isolated_quota_database() as db:
                db.get(AssistantGrant, turn.grant_id).revoked_at = case[-1]
                db.commit()
            return {"content": [{"type": "text", "text": "late private answer"}]}

    async def run():
        return [e async for e in turn.run(RevokeModel())]

    with patch("app.services.assistant.runtime.enabled", return_value=True):
        events = asyncio.run(run())
    assert events[-1]["type"] == "error"
    assert not any(e["type"] == "answer_delta" for e in events)


def test_general_mode_does_not_reuse_project_history(case, isolated_quota_database):
    db, _, _, conversation, grant, _ = case
    db.add(
        AssistantMessage(
            conversation_id=conversation.id,
            request_id=str(uuid.uuid4()),
            role="assistant",
            text="PRIVATE_OLD_CONTEXT",
            grant_id=grant.id,
            status="done",
        )
    )
    db.commit()
    turn = make_turn(case, isolated_quota_database)
    turn.grant_id = None
    with patch("app.services.assistant.runtime.enabled", return_value=True):
        messages, tools = turn.context()
    assert "PRIVATE_OLD_CONTEXT" not in str(messages)
    assert tools == []
