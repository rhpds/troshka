from types import SimpleNamespace

from app.services.assistant.projection import (
    failure_categories,
    project_summary,
    safe_text,
)


def test_allowlisted_topology_excludes_nested_secrets():
    p = SimpleNamespace(
        id="project",
        state="active",
        ocp_status="failed",
        auto_stop_minutes=None,
        auto_delete_minutes=None,
        topology={
            "nodes": [
                {
                    "id": "vm",
                    "type": "vmNode",
                    "data": {
                        "ciCloudUserPassword": "CANARY",
                        "public_token": "CANARY",
                        "cloudInit": "CANARY",
                        "label": "CANARY",
                    },
                }
            ]
        },
    )
    assert "CANARY" not in project_summary(p).model_dump_json()


def test_logs_return_categories_never_text():
    assert failure_categories("pullSecret: unexpected end of JSON input CANARY") == [
        "pull_secret_missing"
    ]
    assert failure_categories("unrecognized failure CANARY") == []


def test_free_text_redacts_recognizable_credentials():
    assert "CANARY" not in safe_text(
        "password=CANARY Bearer CANARY https://user:CANARY@host"
    )
