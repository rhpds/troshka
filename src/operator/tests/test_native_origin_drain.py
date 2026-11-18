"""Cancellation acknowledgement is serialized after real producer execution."""

import asyncio
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from helpers import managed_power as power
from helpers import native_origin_checkpoint as checkpoint

from tests.test_managed_power import FakeAPI, resource

MARKER = "troshka.redhat.com/origin-drain"
OPERATOR = {"namespace": "operators", "name": "operator", "uid": "operator-uid", "containerId": "container", "incarnation": "process"}


@pytest.fixture(autouse=True)
def operator_identity(monkeypatch):
    from helpers import native_origin_drain as drain

    monkeypatch.setattr(drain, "operator_identity", lambda api: deepcopy(OPERATOR))
    monkeypatch.setattr(power, "_locks", {})


def case():
    root = resource("TroshkaProject", "project", spec={"originCheckpoint": {"intentId": "intent", "publicKey": "key"}})
    root["metadata"]["annotations"] = {"troshka.redhat.com/capture-request": "operation"}
    api = FakeAPI([root, resource("Namespace", "ns"), resource("PersistentVolumeClaim", "disk", root)])
    api.find = lambda kind, ns, name: deepcopy(api.values.get((kind, name)))
    deployment = resource("Deployment", "operator", spec={"replicas": 1, "strategy": {"type": "Recreate"},
        "selector": {"matchLabels": {"app": "operator"}}}, status={"observedGeneration": 1, "replicas": 1,
        "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1})
    rs = resource("ReplicaSet", "operator-rs", deployment)
    pod = resource("Pod", "operator", rs, status={"phase": "Running", "containerStatuses": [{"name": "operator",
        "ready": True, "state": {"running": {"startedAt": "start"}}, "containerID": "container", "imageID": "qualified-image"}]})
    pod["metadata"]["labels"] = {"app": "operator"}
    for value in (deployment, rs, pod):
        value["metadata"]["namespace"] = "operators"
        api.values[value["kind"], value["metadata"]["name"]] = value
    cluster = resource("Namespace", "kube-system")
    api.values["Namespace", "kube-system"] = cluster
    return root, api


def mark(api, **changes):
    value = {"version": 1, "intentId": "intent", "drainId": "drain", "epoch": "epoch", "rootUid": "project-uid", "namespaceUid": "ns-uid"}
    value.update(operator={k: v for k, v in OPERATOR.items() if k != "incarnation"},
                 executionsDigest=power.digest(api.values["TroshkaProject", "project"]["status"].get("originExecutions", {})))
    value["qualification"] = {"version": "single-operator-recreate-v1", "clusterUid": "kube-system-uid",
        "deploymentName": "operator", "deploymentUid": "operator-uid", "imageId": "qualified-image"}
    value.update(changes)
    api.values["TroshkaProject", "project"]["metadata"]["annotations"][MARKER] = json.dumps(value)


@pytest.mark.parametrize("boundary", ["begin", "snapshot", "patch", "replace", "shared"])
def test_cancelled_mutation_boundaries_reject_cached_creation_ack(monkeypatch, boundary):
    from helpers import native_snapshot_producer as snapshot
    from helpers.patterns import build_volume_snapshot

    root, api = case()
    mark(api)
    token = power._context.set((root, "ns", api))
    held = checkpoint._held.set(None)
    raw = Mock()
    monkeypatch.setattr(checkpoint, "signature_valid", lambda *a: True)
    try:
        with pytest.raises(power.ManagedPowerError, match="drain|stopped"):
            if boundary == "begin":
                checkpoint.begin_create(api, root, "PersistentVolumeClaim", "ns", "new")
            elif boundary == "snapshot":
                snapshot.create(raw, {"namespace": "ns", "body": build_volume_snapshot("snap", "ns", "disk")})
            elif boundary == "shared":
                checkpoint.guard_client(raw).create_namespaced_secret(namespace="troshka-cache", body={})
            else:
                getattr(checkpoint.guard_client(raw), boundary + "_namespaced_persistent_volume_claim")(
                    namespace="ns", name="disk", body={"spec": {}})
        assert not raw.method_calls
        assert not api.calls
    finally:
        power._context.reset(token)
        checkpoint._held.reset(held)


@pytest.mark.parametrize("annotation", [power.ANNOTATION, power.CLEANUP_ANNOTATION])
def test_old_signed_snapshot_ack_cannot_cross_stop_or_cleanup(monkeypatch, annotation):
    from helpers import native_snapshot_producer as snapshot
    from helpers.patterns import build_volume_snapshot

    root, api = case()
    api.values["TroshkaProject", "project"]["metadata"]["annotations"][annotation] = "{}"
    token = power._context.set((root, "ns", api))
    raw = Mock()
    monkeypatch.setattr(checkpoint, "signature_valid", lambda *a: True)
    try:
        with pytest.raises(power.ManagedPowerError):
            snapshot.create(raw, {"namespace": "ns", "body": build_volume_snapshot("snap", "ns", "disk")})
        assert not raw.called and not api.calls
    finally:
        power._context.reset(token)


def test_drain_ack_waits_for_prior_handler_and_pins_final_creation_history(monkeypatch):
    root, api = case()
    monkeypatch.setattr(power, "PowerClient", lambda: api)

    async def scenario():
        entered, exit_handler = asyncio.Event(), asyncio.Event()
        calls = []

        @power.producer(project=True)
        async def produce(body, namespace):
            calls.append("enter")
            entered.set()
            await exit_handler.wait()
            # An already submitted call returns its original identity before exit.
            api.values["TroshkaProject", "project"]["status"]["originProducerObjects"] = {"late": {"uid": "late-uid"}}
            calls.append("exit")

        first = asyncio.create_task(produce(root, "ns"))
        await entered.wait()
        mark(api)
        second = asyncio.create_task(produce(root, "ns"))
        await asyncio.sleep(0)
        assert "originDrain" not in api.values["TroshkaProject", "project"]["status"]
        exit_handler.set()
        await first
        with pytest.raises(power.ManagedPowerError, match="execution_set_changed"):
            await second
        mark(api)  # Backend must re-observe completion after its initial marker.
        await produce(root, "ns")
        assert calls == ["enter", "exit"]
        state = api.values["TroshkaProject", "project"]["status"]["originDrain"]
        assert state["drainId"] == "drain" and state["epoch"] == "epoch"
        assert state["producerDigest"] == power.digest({"objects": {"late": {"uid": "late-uid"}}, "epochs": {}})
        assert "managedPower" not in api.values["TroshkaProject", "project"]["status"]
        assert "troshka.redhat.com/capture-request" in api.values["TroshkaProject", "project"]["metadata"]["annotations"]

    asyncio.run(scenario())


@pytest.mark.parametrize("changed", [{"rootUid": "other"}, {"namespaceUid": "other"}, {"intentId": "other"}])
def test_drain_ack_rejects_substituted_identity(monkeypatch, changed):
    root, api = case()
    mark(api, **changed)
    monkeypatch.setattr(power, "PowerClient", lambda: api)

    @power.producer(project=True)
    async def produce(body, namespace):
        raise AssertionError("cancelled producer entered")

    with pytest.raises(power.ManagedPowerError):
        asyncio.run(produce(root, "ns"))
    assert not api.calls


@pytest.mark.parametrize("terminal", [False, True])
def test_restart_retains_complete_history_but_holds_interrupted_incarnation(monkeypatch, terminal):
    root, api = case()
    api.values["TroshkaProject", "project"]["status"]["originExecutions"] = {
        "old-process": {"operator": {**OPERATOR, "incarnation": "old-process"}, "executionId": "old-call",
                            "state": "complete" if terminal else "active"}}
    mark(api)
    monkeypatch.setattr(power, "PowerClient", lambda: api)

    @power.producer(project=True)
    async def produce(body, namespace):
        raise AssertionError("cancelled producer entered")

    if terminal:
        asyncio.run(produce(root, "ns"))
        assert api.values["TroshkaProject", "project"]["status"]["originDrain"]
    else:
        with pytest.raises(power.ManagedPowerError, match="prior_execution_unresolved"):
            asyncio.run(produce(root, "ns"))
        assert not api.calls


def test_failed_handler_records_terminal_only_after_all_thread_io_returns(monkeypatch):
    from threading import Event

    root, api = case()
    monkeypatch.setattr(power, "PowerClient", lambda: api)
    entered, release = Event(), Event()

    def thread_io():
        entered.set()
        assert release.wait(timeout=5)
        return "late-result"

    async def scenario():
        @power.producer(project=True)
        async def produce(body, namespace):
            await power.checked_await(asyncio.to_thread(thread_io))

        original = asyncio.create_task(produce(root, "ns"))
        assert await asyncio.to_thread(entered.wait, 5)
        original.cancel()
        await asyncio.sleep(0)
        record = api.values["TroshkaProject", "project"]["status"]["originExecutions"]["process"]
        assert record["state"] == "active" and not original.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await original
        record = api.values["TroshkaProject", "project"]["status"]["originExecutions"]["process"]
        assert record["state"] == "complete" and record["outcome"] == "failed"

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_normal_failure_and_new_execution_do_not_accumulate_timer_history(monkeypatch):
    root, api = case()
    monkeypatch.setattr(power, "PowerClient", lambda: api)

    @power.producer(project=True)
    async def produce(body, namespace):
        raise RuntimeError("ordinary handler failure")

    ids = []
    for _ in range(2):
        with pytest.raises(RuntimeError, match="ordinary"):
            asyncio.run(produce(root, "ns"))
        history = api.values["TroshkaProject", "project"]["status"]["originExecutions"]
        assert len(history) == 1 and history["process"]["state"] == "complete"
        ids.append(history["process"]["executionId"])
    assert ids[0] != ids[1]


def test_network_timer_registers_and_joins_actual_exec_thread(monkeypatch):
    import importlib
    from threading import Event

    from handlers import network

    monkeypatch.setattr(network.kopf.timer, "side_effect", lambda *a, **kw: lambda fn: fn)
    importlib.reload(network)

    root, api = case()
    child = resource("TroshkaNetwork", "network", root)
    api.values["TroshkaNetwork", "network"] = child
    monkeypatch.setattr(power, "PowerClient", lambda: api)
    entered, release = Event(), Event()

    def heal(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(network, "ensure_nested_mac_port_security", heal)

    async def scenario():
        call = asyncio.create_task(network.network_nested_mac_ovn_timer(
            spec={"allowNestedMacs": True}, name="network", namespace="ns", body=child))
        assert await asyncio.to_thread(entered.wait, 5)
        call.cancel()
        await asyncio.sleep(0)
        state = api.values["TroshkaProject", "project"]["status"]
        assert state["originExecutions"]["process"]["state"] == "active"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert api.values["TroshkaProject", "project"]["status"]["originExecutions"]["process"]["state"] == "complete"

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_ancillary_exec_is_fenced_before_remote_mutation():
    from helpers.native_origin_drain import FenceClient

    root, api = case()
    mark(api)
    token = power._context.set((root, "ns", api))
    raw = Mock()
    try:
        with pytest.raises(power.ManagedPowerError):
            FenceClient(raw).connect_get_namespaced_pod_exec(name="ovn", namespace="ovn")
        assert not raw.method_calls
    finally:
        power._context.reset(token)


@pytest.mark.parametrize("mode", ["legacy", "origin", "cancelled"])
def test_real_kubernetes_stream_preserves_bound_transport_and_fence(mode, monkeypatch):
    from types import SimpleNamespace

    from helpers.native_origin_drain import FenceClient
    from kubernetes import client
    from kubernetes.stream import stream

    root, api = case()
    if mode == "cancelled":
        mark(api)
    raw = client.CoreV1Api()
    # Keep the real generated method and stream adapter; the HTTP boundary is
    # synthetic and calls the transport installed by that adapter.
    monkeypatch.setattr(raw.api_client, "call_api", lambda *args, **kwargs: raw.api_client.request())
    original_request = raw.api_client.request
    transport = Mock(return_value=SimpleNamespace(status=200, data="exec-output", getheaders=dict))
    token = power._context.set(None if mode == "legacy" else (root, "ns", api))
    try:
        method = FenceClient(raw).connect_get_namespaced_pod_exec
        if mode == "cancelled":
            with pytest.raises(power.ManagedPowerError):
                stream.func(transport, {}, method, "ovn", "ns", command=["true"], _preload_content=False)
            transport.assert_not_called()
        else:
            result = stream.func(transport, {}, method, "ovn", "ns", command=["true"], _preload_content=False)
            assert result.data == "exec-output" and transport.call_count == 1
        assert raw.api_client.request == original_request
    finally:
        power._context.reset(token)


def test_asserted_termination_digest_cannot_clear_active_prior_execution(monkeypatch):
    root, api = case()
    execution = {"operator": {**OPERATOR, "incarnation": "old"}, "executionId": "interrupted", "state": "active"}
    api.values["TroshkaProject", "project"]["status"]["originExecutions"] = {"old": execution}
    mark(api, terminatedExecutions={"old": power.digest(execution)})
    monkeypatch.setattr(power, "PowerClient", lambda: api)

    @power.producer(project=True)
    async def produce(body, namespace):
        raise AssertionError("cancelled producer entered")

    with pytest.raises(power.ManagedPowerError, match="prior_execution_unresolved"):
        asyncio.run(produce(root, "ns"))
    assert not api.calls


def test_completed_execution_slot_reused_at_history_capacity(monkeypatch):
    from helpers import native_origin_drain as drain

    root, api = case()
    history = {str(i): {"state": "complete"} for i in range(999)}
    history["process"] = {"state": "complete"}
    api.values["TroshkaProject", "project"]["status"]["originExecutions"] = history
    for _ in range(1001):
        execution = drain.register(api, root, "ns")
        drain.complete(api, root, "ns", execution)
    assert len(api.values["TroshkaProject", "project"]["status"]["originExecutions"]) == 1000
    monkeypatch.setattr(drain, "operator_identity", lambda api: {**OPERATOR, "incarnation": "new-process"})
    with pytest.raises(power.ManagedPowerError, match="history_limit"):
        drain.register(api, root, "ns")
    assert len(api.values["TroshkaProject", "project"]["status"]["originExecutions"]) == 1000


def test_execution_registration_cannot_overwrite_active_same_incarnation():
    from helpers import native_origin_drain as drain

    root, api = case()
    execution = drain.register(api, root, "ns")
    with pytest.raises(power.ManagedPowerError, match="prior_execution_unresolved"):
        drain.register(api, root, "ns")
    saved = api.values["TroshkaProject", "project"]["status"]["originExecutions"]["process"]
    assert saved["executionId"] == execution[0] and saved["state"] == "active"


@pytest.mark.parametrize("change", ["overlap", "rollout", "replacement", "image", "owner"])
def test_drain_rejects_unqualified_operator_runtime(monkeypatch, change):
    root, api = case()
    mark(api)
    if change == "overlap":
        other = deepcopy(api.values["Pod", "operator"])
        other["metadata"].update(name="old-operator", uid="old-operator")
        api.values["Pod", "old-operator"] = other
    elif change == "rollout":
        api.values["Deployment", "operator"]["spec"]["strategy"]["type"] = "RollingUpdate"
    elif change == "replacement":
        api.values["Pod", "operator"]["metadata"]["uid"] = "replacement"
    elif change == "image":
        api.values["Pod", "operator"]["status"]["containerStatuses"][0]["imageID"] = "different-image"
    else:
        api.values["ReplicaSet", "operator-rs"]["metadata"]["ownerReferences"][0]["uid"] = "different-deployment"
    monkeypatch.setattr(power, "PowerClient", lambda: api)

    @power.producer(project=True)
    async def produce(body, namespace):
        raise AssertionError("cancelled producer entered")

    with pytest.raises(power.ManagedPowerError):
        asyncio.run(produce(root, "ns"))
    assert not api.calls


def test_inflight_snapshot_post_returns_original_uid_before_terminal_execution(monkeypatch):
    from threading import Event

    from helpers import native_snapshot_producer as snapshot
    from helpers.patterns import build_volume_snapshot

    root, api = case()
    monkeypatch.setattr(power, "PowerClient", lambda: api)
    monkeypatch.setattr(checkpoint, "signature_valid", lambda *a: True)
    entered, release = Event(), Event()
    calls = []

    def post(**kwargs):
        calls.append("POST")
        entered.set()
        assert release.wait(timeout=5)
        value = deepcopy(kwargs["body"])
        value["metadata"].update(uid="original-snapshot", resourceVersion="1")
        api.values["VolumeSnapshot", "snap"] = value
        return deepcopy(value)

    async def scenario():
        @power.producer(project=True)
        async def produce(body, namespace):
            await power.checked_await(asyncio.to_thread(snapshot.create, post, {
                "namespace": "ns", "body": build_volume_snapshot("snap", "ns", "disk")}))

        original = asyncio.create_task(produce(root, "ns"))
        assert await asyncio.to_thread(entered.wait, 5)
        mark(api)
        waiting = asyncio.create_task(produce(root, "ns"))
        await asyncio.sleep(0)
        assert "originDrain" not in api.values["TroshkaProject", "project"]["status"]
        release.set()
        with pytest.raises(power.ManagedPowerError, match="draining"):
            await original
        with pytest.raises(power.ManagedPowerError, match="execution_set_changed"):
            await waiting
        mark(api)
        await produce(root, "ns")
        status = api.values["TroshkaProject", "project"]["status"]
        assert {value.get("uid") for value in status["originProducerObjects"].values()} == {"original-snapshot"}
        assert status["originDrain"] and status["originExecutions"]["process"]["state"] == "complete"
        assert calls == ["POST"]
        assert ("PersistentVolumeClaim", "disk") in api.values

    try:
        asyncio.run(scenario())
    finally:
        release.set()
