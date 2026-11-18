"""Real producer and deletion handlers under root-independent namespace fencing."""

import asyncio
import json
from unittest.mock import Mock

import pytest
from helpers import managed_power as power
from helpers import native_namespace_cleanup as cleanup
from helpers import native_origin_drain as drain

from tests.test_native_origin_drain import OPERATOR, case
from tests.test_native_origin_drain import operator_identity as operator_identity_fixture

operator_identity = operator_identity_fixture


def mark(api):
    marker = dict(version="native-project-namespace-v1", cleanupId="cleanup", sourceKind="admin_custody",
        sourceId="receipt", sourceDigest="a" * 64, projectId="project", clusterUid="kube-system-uid",
        namespace="ns", namespaceUid="ns-uid", rootUid="project-uid",
        operator={k: v for k, v in OPERATOR.items() if k != "incarnation"},
        qualification=dict(version="single-operator-recreate-v1", clusterUid="kube-system-uid",
                           deploymentName="operator", deploymentUid="operator-uid", imageId="qualified-image"))
    api.values["Namespace", "ns"]["metadata"]["annotations"] = {cleanup.ANNOTATION: json.dumps(marker)}
    return marker


def ack(api):
    raw = api.values["Namespace", "ns"]["metadata"]["annotations"].get(cleanup.ACK)
    return json.loads(raw) if raw else None


def test_namespace_ack_survives_missing_root_and_replay():
    _, api = case()
    marker = mark(api)
    api.values.pop(("TroshkaProject", "project"))
    asyncio.run(cleanup.acknowledge(api, "ns"))
    assert ack(api)["markerDigest"] == power.digest(marker)
    count = len(api.calls)
    asyncio.run(cleanup.acknowledge(api, "ns"))
    assert len(api.calls) == count


def test_namespace_ack_uses_cluster_scoped_conditional_patch(monkeypatch):
    from types import SimpleNamespace

    _, api = case()
    marker = mark(api)
    namespace = api.values["Namespace", "ns"]
    namespace["metadata"].pop("namespace", None)
    namespace["metadata"]["resourceVersion"] = "42"
    transport = Mock()
    response = Mock(status=200)
    response.read.return_value = b'{}'
    transport.request.return_value = response
    client = power.PowerClient(SimpleNamespace(
        default_headers={}, configuration=SimpleNamespace(host="https://cluster.invalid"),
        update_params_for_auth=Mock(), rest_client=SimpleNamespace(pool_manager=transport),
    ))
    monkeypatch.setattr(client, "get", api.get)
    monkeypatch.setattr(client, "rows", api.rows)
    asyncio.run(cleanup.acknowledge(client, "ns"))
    transport.request.assert_called_once()
    args, kwargs = transport.request.call_args
    assert args == ("PATCH", "https://cluster.invalid/api/v1/namespaces/ns")
    body = json.loads(kwargs["body"])
    assert body[:2] == [
        {"op": "test", "path": "/metadata/uid", "value": "ns-uid"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "42"},
    ]
    assert body[2]["path"] == "/metadata/annotations"
    assert json.loads(body[2]["value"][cleanup.ACK])["markerDigest"] == power.digest(marker)
    assert kwargs["headers"]["Content-Type"] == "application/json-patch+json"
    assert kwargs["retries"] is False


@pytest.mark.parametrize("replacement", ["pod", "container"])
def test_qualified_replacement_acknowledges_only_successor_marker(monkeypatch, replacement):
    _, api = case()
    original = mark(api)
    asyncio.run(cleanup.acknowledge(api, "ns"))
    field = "uid" if replacement == "pod" else "containerId"
    identity = {**OPERATOR, field: "replacement", "incarnation": "new-process"}
    pod = api.values["Pod", "operator"]
    if replacement == "pod":
        pod["metadata"]["uid"] = "replacement"
    else:
        pod["status"]["containerStatuses"][0]["containerID"] = "replacement"
    monkeypatch.setattr(drain, "operator_identity", lambda api: identity)
    with pytest.raises(power.ManagedPowerError, match="operator_changed"):
        asyncio.run(cleanup.acknowledge(api, "ns"))
    successor = {**original, "operator": {k: v for k, v in identity.items() if k != "incarnation"}}
    api.values["Namespace", "ns"]["metadata"]["annotations"][cleanup.ANNOTATION] = json.dumps(successor)
    asyncio.run(cleanup.acknowledge(api, "ns"))
    assert ack(api)["markerDigest"] == power.digest(successor)
    assert ack(api)["operator"] == identity


def test_actual_namespace_registration_never_requires_a_finalizer():
    import subprocess
    import sys

    # The ordinary operator harness intentionally mocks Kopf. Load its actual
    # registration in a fresh interpreter without invoking a Kubernetes client.
    subprocess.run([sys.executable, "-c", """
import kopf
from handlers.namespace_cleanup import namespace_cleanup_event
registry = kopf.get_default_registry()
assert any(h.fn is namespace_cleanup_event for h in registry._watching._handlers)
assert not any(h.fn is namespace_cleanup_event
    for handlers in (registry._changing._handlers, registry._spawning._handlers)
    for h in handlers)
"""], check=True, timeout=10)


@pytest.mark.parametrize("origin", [False, True])
def test_ack_waits_for_handler_and_actual_child_return(monkeypatch, origin):
    root, api = case()
    if not origin:
        root["spec"].pop("originCheckpoint")
        api.values["TroshkaProject", "project"] = root
    monkeypatch.setattr(power, "PowerClient", lambda: api)

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        @power.producer(project=True)
        async def handler(body, namespace):
            started.set()
            await drain.tracked_await(release.wait())

        task = asyncio.create_task(handler(root, "ns"))
        await started.wait()
        mark(api)
        waiter = asyncio.create_task(cleanup.acknowledge(api, "ns"))
        await asyncio.sleep(0)
        assert ack(api) is None
        release.set()
        await task
        await waiter
        assert ack(api)

    asyncio.run(scenario())


def test_unjoined_local_child_prevents_namespace_ack():
    _, api = case()
    mark(api)

    async def scenario():
        future = asyncio.get_running_loop().create_future()
        drain._unfinished_children["project-uid"] = [future]
        try:
            with pytest.raises(power.ManagedPowerError, match="child_still_running"):
                await cleanup.acknowledge(api, "ns")
            assert ack(api) is None
            future.set_result(None)
            await cleanup.acknowledge(api, "ns")
        finally:
            drain._unfinished_children.clear()

    asyncio.run(scenario())


def test_timed_out_legacy_child_survives_later_producer_and_blocks_ack(monkeypatch):
    from helpers.native_origin_checkpoint import CheckpointHold

    root, api = case()
    root["spec"].pop("originCheckpoint")
    api.values["TroshkaProject", "project"] = root
    monkeypatch.setattr(power, "PowerClient", lambda: api)

    async def expired_join(future, timeout):
        assert timeout == 55
        raise TimeoutError("simulated join deadline")

    monkeypatch.setattr(drain.asyncio, "wait_for", expired_join)

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        later_calls = []

        @power.producer(project=True)
        async def original(body, namespace):
            started.set()
            await drain.tracked_await(release.wait())

        @power.producer(project=True)
        async def later(body, namespace):
            later_calls.append("ran")

        task = asyncio.create_task(original(root, "ns"))
        await started.wait()
        task.cancel()
        with pytest.raises(TimeoutError):
            await task
        children = list(drain._unfinished_children["project-uid"])
        try:
            with pytest.raises(CheckpointHold, match="prior_local_child"):
                await later(root, "ns")
            assert not later_calls and not children[0].done()
            mark(api)
            with pytest.raises(power.ManagedPowerError, match="child_still_running"):
                await cleanup.acknowledge(api, "ns")
            assert ack(api) is None
            release.set()
            await asyncio.gather(*children)
            await cleanup.acknowledge(api, "ns")
            assert ack(api)
        finally:
            release.set()
            await asyncio.gather(*children)
            drain._unfinished_children.clear()

    asyncio.run(scenario())


@pytest.mark.parametrize("origin", [False, True])
@pytest.mark.parametrize("client_kind", ["guard", "fence"])
def test_cached_producer_client_is_fenced(monkeypatch, origin, client_kind):
    from helpers.native_origin_checkpoint import guard_client

    root, api = case()
    if not origin:
        root["spec"].pop("originCheckpoint")
    token = power._context.set((root, "ns", api))
    raw = Mock()
    try:
        wrapped = guard_client(raw) if client_kind == "guard" else drain.FenceClient(raw)
        mark(api)
        with pytest.raises(power.ManagedPowerError, match="production_fenced"):
            wrapped.create_namespaced_pod(namespace="ns", body={})
        assert not raw.method_calls
    finally:
        power._context.reset(token)


def deletion_handler(name):
    import kopf
    from handlers import ceph, network, project, vm  # noqa: F401

    return next(c.args[0] for c in kopf.on.delete.return_value.call_args_list
                if c.args and getattr(c.args[0], "__name__", None) == name)


@pytest.mark.parametrize("name", ["project_delete", "vm_delete", "network_delete", "ceph_delete"])
def test_actual_delete_handler_needs_no_root_and_preserves_namespace_children(monkeypatch, name):
    from handlers import network

    _, api = case()
    mark(api)
    api.values.pop(("TroshkaProject", "project"))
    monkeypatch.setattr(power, "PowerClient", lambda: api)
    removed = []
    monkeypatch.setattr(network, "_modify_scc_users", lambda client, scc, sa, action: removed.append((scc, sa, action)))
    before = set(api.values)
    asyncio.run(deletion_handler(name)(spec={}, status={}, meta={"name": "child", "uid": "child"},
                                      body={"metadata": {"name": "child", "uid": "child"}}, namespace="ns", name="child"))
    assert set(api.values) == before
    assert not api.calls
    assert bool(removed) == (name != "vm_delete")


@pytest.mark.parametrize("name", ["project_delete", "network_delete", "ceph_delete"])
def test_scc_failure_keeps_actual_finalizer_pending(monkeypatch, name):
    from handlers import network

    _, api = case()
    mark(api)
    monkeypatch.setattr(power, "PowerClient", lambda: api)
    monkeypatch.setattr(network, "_modify_scc_users", Mock(side_effect=RuntimeError("SCC permission unavailable")))
    with pytest.raises(RuntimeError, match="SCC permission"):
        asyncio.run(deletion_handler(name)(spec={}, meta={}, namespace="ns", name="child", body={"metadata": {}}))


def test_wrong_namespace_uid_holds_before_external_duty():
    _, api = case()
    mark(api)
    api.values["Namespace", "ns"]["metadata"]["uid"] = "replacement"
    with pytest.raises(power.ManagedPowerError, match="uid_changed"):
        cleanup.finish_external(api, "ns", "TroshkaVM")
