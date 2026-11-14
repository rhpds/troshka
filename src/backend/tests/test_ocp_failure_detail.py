from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.parametrize(
    "log,reason",
    [
        (
            'level=fatal msg=invalid install-config configuration: pullSecret: Invalid value: "": unexpected end of JSON input',
            "OCP pull secret is missing or invalid",
        ),
        (
            "level=fatal msg=unauthorized: authentication required token=private",
            "Registry authentication failed",
        ),
        ("Bootstrap failed to complete", "Cluster bootstrap failed"),
        ("context deadline exceeded", "Installation timed out"),
        (
            "level=fatal msg=unknown password=private",
            "Installer reported an error; see install logs",
        ),
    ],
)
def test_install_failure_detail_is_actionable_and_does_not_copy_secrets(log, reason):
    from app.services.ocp.ops_pod_install import install_failure_detail

    assert install_failure_detail(log) == f"OpenShift Install Failed: {reason}"


def test_ops_monitor_publishes_installer_reason():
    from app.services.deploy_service import _publish_ops_pod_progress

    progress = {
        "overall": "failed",
        "failed": ["ocp"],
        "clusters": {"ocp": "failed"},
        "failure_detail": "OpenShift Install Failed: OCP pull secret is missing or invalid",
    }
    with patch(
        "app.services.deploy_service._project_infra_in_progress", return_value=False
    ), patch("app.services.deploy_service.notify_project"), patch(
        "app.services.deploy_service._ocp_push_status"
    ) as push:
        _publish_ops_pod_progress("project", progress)
    push.assert_called_once_with("project", "error", progress["failure_detail"])


@pytest.mark.parametrize("phase", ["timeout", "oc", "nodes"])
def test_api_timeout_preserves_confirmed_install_failure(phase):
    from app.services.deploy_service import _ocp_push_status

    project = MagicMock(
        ocp_status_detail="OpenShift Install Failed: OCP pull secret is missing or invalid"
    )
    session = MagicMock()
    session.get.return_value = project
    with patch("app.core.database.SessionLocal", return_value=session), patch(
        "app.services.deploy_service.notify_project"
    ) as notify:
        _ocp_push_status(
            "project", phase, "OpenShift API server not reachable (35m 32s)"
        )
    assert (
        project.ocp_status_detail
        == "OpenShift Install Failed: OCP pull secret is missing or invalid"
    )
    assert notify.call_args.args[1]["detail"] == project.ocp_status_detail
