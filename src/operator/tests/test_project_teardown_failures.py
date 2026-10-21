"""Regression tests for best-effort deletion and finalizer wait boundaries."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from handlers import project


DELETERS = [
    (project._delete_namespaced_deployments, "AppsV1Api", "deployment", True),
    (project._force_delete_namespaced_pods, "CoreV1Api", "pod", True),
    (project._delete_namespaced_services, "CoreV1Api", "service", True),
    (project._delete_namespaced_pvcs, "CoreV1Api", "persistent_volume_claim", False),
]


def named_resource(name):
    return SimpleNamespace(metadata=SimpleNamespace(name=name))


@pytest.mark.parametrize("delete_resources,api_name,kind,force", DELETERS)
def test_teardown_continues_past_missing_and_forbidden_resources(delete_resources, api_name, kind, force, caplog):
    api = MagicMock()
    listing = getattr(api, f"list_namespaced_{kind}")
    deletion = getattr(api, f"delete_namespaced_{kind}")
    listing.return_value = SimpleNamespace(items=[
        SimpleNamespace(metadata=None), named_resource(""),
        named_resource("gone"), named_resource("forbidden"), named_resource("remaining"),
    ])
    deletion.side_effect = [ApiException(status=404), ApiException(status=403), None]
    with patch.object(project.client, api_name, return_value=api):
        delete_resources("lab")
    assert [call.kwargs["name"] for call in deletion.call_args_list] == ["gone", "forbidden", "remaining"]
    expected = {"name": "remaining", "namespace": "lab"}
    if force:
        expected["grace_period_seconds"] = 0
    assert deletion.call_args.kwargs == expected
    assert "forbidden" in caplog.text


@pytest.mark.parametrize("delete_resources,api_name,kind,force", DELETERS)
def test_teardown_list_failure_is_logged_without_deleting_unknown_resources(delete_resources, api_name, kind, force, caplog):
    api = MagicMock()
    getattr(api, f"list_namespaced_{kind}").side_effect = RuntimeError("connection lost")
    with patch.object(project.client, api_name, return_value=api):
        delete_resources("lab")
    getattr(api, f"delete_namespaced_{kind}").assert_not_called()
    assert "connection lost" in caplog.text


@pytest.mark.parametrize("delete_resources,api_name,kind,force", DELETERS)
def test_teardown_tolerates_empty_api_list(delete_resources, api_name, kind, force):
    api = MagicMock()
    getattr(api, f"list_namespaced_{kind}").return_value = SimpleNamespace(items=None)
    with patch.object(project.client, api_name, return_value=api):
        delete_resources("lab")
    getattr(api, f"delete_namespaced_{kind}").assert_not_called()


WAITERS = [
    (project._wait_namespaced_pods_gone, "pod"),
    (project._wait_namespaced_services_gone, "service"),
]


@pytest.mark.parametrize("wait_resources,kind", WAITERS)
def test_finalizer_wait_retries_api_outage_and_terminating_resources(wait_resources, kind):
    api = MagicMock()
    listing = getattr(api, f"list_namespaced_{kind}")
    listing.side_effect = [ApiException(status=503), SimpleNamespace(items=[named_resource("terminating")]), SimpleNamespace(items=[])]
    with (
        patch.object(project.client, "CoreV1Api", return_value=api),
        patch.object(project.time, "sleep") as sleep,
    ):
        wait_resources("lab", attempts=3)
    assert listing.call_count == 3
    assert sleep.call_count == 2


@pytest.mark.parametrize("wait_resources,kind", WAITERS)
def test_finalizer_wait_accepts_deleted_namespace(wait_resources, kind):
    api = MagicMock()
    listing = getattr(api, f"list_namespaced_{kind}")
    listing.side_effect = ApiException(status=404)
    with (
        patch.object(project.client, "CoreV1Api", return_value=api),
        patch.object(project.time, "sleep") as sleep,
    ):
        wait_resources("lab", attempts=3)
    listing.assert_called_once_with(namespace="lab")
    sleep.assert_not_called()
