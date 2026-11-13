"""Regression tests for best-effort deletion and finalizer wait boundaries."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from handlers import project
from helpers import managed_power


DELETERS = [
    (project._delete_namespaced_deployments, "AppsV1Api", "deployment", True),
    (project._force_delete_namespaced_pods, "CoreV1Api", "pod", True),
    (project._delete_namespaced_services, "CoreV1Api", "service", True),
    (project._delete_namespaced_pvcs, "CoreV1Api", "persistent_volume_claim", False),
]


def test_managed_project_finalizer_never_enters_legacy_cleanup(monkeypatch):
    root = {"apiVersion": "troshka.redhat.com/v1alpha1", "kind": "TroshkaProject", "metadata": {"name": "project", "namespace": "ns", "uid": "project-uid", "generation": 1, "resourceVersion": "1"}}
    request = {"version": 1, "mode": "stopped", "requestId": "stop-1", "allocationGeneration": "generation-1"}
    refs = [
        {"kind": "Namespace", "name": "ns", "uid": "ns-uid", "resource_version": "1", "namespace": None},
        {"kind": "TroshkaProject", "name": "project", "uid": "project-uid", "resource_version": "1", "namespace": "ns"},
    ]
    payload = {"version": "native-cleanup-v1", "namespace_mode": "managed", "intent_id": "cleanup-1", "generation": "generation-1", "root": {"namespace": "ns", "namespace_uid": "ns-uid", "project_cr_uid": "project-uid", "deployment_generation": 1}, "stop_proof": {"request_id": "stop-1", "generation": "generation-1"}, "objects": refs, "inventory_digest": managed_power.digest(refs)}
    root["metadata"]["annotations"] = {managed_power.ANNOTATION: json.dumps(request), managed_power.CLEANUP_ANNOTATION: json.dumps(payload)}

    class Reader:
        def get(self, kind, namespace, name):
            return root if kind == "TroshkaProject" else {"metadata": {"uid": "ns-uid"}}

        def rows(self, kind, namespace):
            return []

    monkeypatch.setattr(managed_power, "PowerClient", Reader)

    def forbidden(*args, **kwargs):
        raise AssertionError("managed cleanup reached legacy namespace/capture cleanup")

    for name in ("_abort_pattern_capture_temps", "_delete_custom_resources", "_delete_namespaced_deployments", "_force_delete_namespaced_pods", "_delete_namespaced_services", "_delete_namespaced_pvcs"):
        monkeypatch.setattr(project, name, forbidden)
    import kopf

    handler = next(args[0][0] for args in reversed(kopf.on.delete.return_value.call_args_list) if asyncio.iscoroutinefunction(args[0][0]) and args[0][0].__name__ == "project_delete")
    asyncio.run(handler("ns", "project", body=root))


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
