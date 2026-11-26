import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.services.assistant.contracts import ToolRequest
from app.services.assistant.dispatcher import dispatch
from app.services.assistant.guest_checks import parse_output
from app.services.assistant.knowledge import search_help
from app.services.assistant.network_checks import public_probe_address


def test_guest_output_is_projected():
    assert parse_output(
        "addresses", '[{"addr_info":[{"local":"10.0.0.50", "secret":"CANARY"}]}]'
    ) == {"addresses": ["10.0.0.50/32"]}
    assert parse_output("service_status", "CANARY") == {"sshd": "unknown"}


@pytest.mark.parametrize(
    "ip", ["169.254.169.254", "127.0.0.1", "10.0.0.1", "::1", "224.0.0.1"]
)
def test_probe_rejects_management_and_metadata(ip):
    with pytest.raises(ValueError):
        public_probe_address(ip)


def test_help_does_not_need_private_transcripts():
    assert search_help("ssh port")[0]["id"] == "ssh-forwarding"
    assert search_help("unrelated") == []


def test_dispatch_rejects_arbitrary_commands(case, isolated_quota_database):
    _, user, _, conversation, _, _ = case
    with patch("app.services.assistant.dispatcher.enabled", return_value=True):
        with pytest.raises(HTTPException):
            asyncio.run(
                dispatch(
                    isolated_quota_database,
                    user.id,
                    conversation.id,
                    ToolRequest(name="get_host_health", arguments={"command": "id"}),
                )
            )


def test_dispatch_rechecks_revocation(case, isolated_quota_database):
    from app.models.assistant import AssistantGrant
    from app.services.assistant import dispatcher
    from app.services.assistant.contracts import Evidence, NoArgs

    db, user, project, conversation, grant, now = case

    def adapter(session, ctx, args):
        session.get(AssistantGrant, grant.id).revoked_at = now
        session.commit()
        return Evidence(
            check="get_project_summary", project_id=project.id, facts={"late": True}
        )

    with (
        patch.object(dispatcher, "enabled", return_value=True),
        patch.dict(
            dispatcher.TOOLS,
            {"get_project_summary": (NoArgs, "project", adapter, "fixture")},
        ),
    ):
        with pytest.raises(HTTPException):
            asyncio.run(
                dispatch(
                    isolated_quota_database,
                    user.id,
                    conversation.id,
                    ToolRequest(name="get_project_summary"),
                )
            )
