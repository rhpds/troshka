"""Contracts for runtime response hydration and operator redeploy handoffs."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes.client.exceptions import ApiException

from app.api import projects as api


@pytest.mark.parametrize(
    "phase,deleted",
    [("Running", False), ("Failed", True), ("Succeeded", True), (None, False)],
)
def test_clear_stale_vmi_only_when_not_running(phase, deleted):
    client = Mock()
    client.get_namespaced_custom_object.return_value = {"status": {"phase": phase}}
    assert api._kv_clear_stuck_vmi(client, "ns", "vm") is deleted
    assert client.delete_namespaced_custom_object.called is deleted


@pytest.mark.parametrize("operation", ["get", "delete"])
def test_stale_vmi_cleanup_is_best_effort(operation):
    client = Mock()
    client.get_namespaced_custom_object.return_value = {"status": {"phase": "Failed"}}
    getattr(client, f"{operation}_namespaced_custom_object").side_effect = RuntimeError(
        "unavailable"
    )
    assert api._kv_clear_stuck_vmi(client, "ns", "vm") is False


@pytest.mark.parametrize("port_map", [None, {"22": 2022}])
def test_eip_hydration_overlays_runtime_fields_without_mutating_source(port_map):
    original = {"id": "eip", "ip": "old", "_transit_port_map": {"stale": 1}}
    eip = SimpleNamespace(
        canvas_eip_id="eip",
        public_ip="203.0.113.1",
        private_ip="10.0.0.1",
        port_map=port_map,
    )
    db = Mock()
    db.query.return_value.filter_by.return_value.all.return_value = [eip]
    result = {
        "topology": {"externalIps": [original, {"id": "other"}]},
        "deployed_topology": None,
    }
    api._hydrate_response_external_ips(db, "project", result)
    hydrated = result["topology"]["externalIps"][0]
    assert hydrated["ip"] == eip.public_ip
    assert hydrated["_private_ip"] == eip.private_ip
    assert hydrated.get("_transit_port_map") == port_map
    assert original["ip"] == "old"
    assert result["topology"]["externalIps"][1] == {"id": "other"}


def test_route_hydration_preserves_existing_endpoints():
    deployed = [{"host": "console.example.test"}]
    nodes = [
        {"id": "gw", "data": {"subtype": "gateway"}},
        {
            "id": "other",
            "data": {"subtype": "gateway", "externalEndpoints": ["existing"]},
        },
        {"id": "vm", "data": {}},
    ]
    result = {
        "topology": {"nodes": nodes},
        "deployed_topology": {
            "nodes": [
                {
                    "id": "gw",
                    "data": {"subtype": "gateway", "externalEndpoints": deployed},
                }
            ]
        },
    }
    api._hydrate_response_external_endpoints(result)
    assert nodes[0]["data"]["externalEndpoints"] == deployed
    assert nodes[1]["data"]["externalEndpoints"] == ["existing"]
    assert "externalEndpoints" not in nodes[2]["data"]


@pytest.mark.parametrize(
    "started,expected", [(123.9, 123), ("123", None), (None, None)]
)
def test_cluster_install_metadata_is_scoped_and_numeric(started, expected):
    topology = {
        "clusters": [
            {"id": "other"},
            {
                "name": "target",
                "ocpInstallStatus": "ready",
                "ocpInstallElapsed": 90,
                "ocpInstallStartedAt": started,
            },
        ]
    }
    assert api._cluster_install_meta(topology, "target") == ("ready", 90, expected)
    assert api._cluster_install_meta(topology, "missing") == (None, None, None)


@pytest.mark.parametrize(
    "state,expected",
    [("running", True), ("stopped", False), ("shut_off", False), ("shut off", False)],
)
def test_redeploy_uses_known_cached_power_state(monkeypatch, state, expected):
    monkeypatch.setattr(
        "app.services.ws_pubsub.get_cached_vm_states",
        lambda _: {"states": {"vm": state}},
    )
    client = Mock()
    assert api._kubevirt_vm_was_running("p", "vm", client, "ns", "cr") is expected
    client.get_namespaced_custom_object.assert_not_called()


@pytest.mark.parametrize(
    "result,expected",
    [
        ({"status": {"state": "Running"}}, True),
        ({"status": {"state": "Stopped"}}, False),
        (RuntimeError("offline"), True),
    ],
)
def test_redeploy_falls_back_to_operator_power_state(monkeypatch, result, expected):
    monkeypatch.setattr("app.services.ws_pubsub.get_cached_vm_states", lambda _: None)
    client = Mock()
    if isinstance(result, Exception):
        client.get_namespaced_custom_object.side_effect = result
    else:
        client.get_namespaced_custom_object.return_value = result
    assert api._kubevirt_vm_was_running("p", "vm", client, "ns", "cr") is expected


@pytest.mark.parametrize("operation,ignored", [("delete", 404), ("create", 409)])
def test_cr_mutations_are_idempotent_only_for_expected_status(operation, ignored):
    client = Mock()
    method = getattr(client, f"{operation}_namespaced_custom_object")
    fn = api._delete_troshkavm_cr if operation == "delete" else api._create_troshkavm_cr
    method.side_effect = ApiException(status=ignored)
    fn(client, "ns", "cr")
    method.side_effect = ApiException(status=403)
    with pytest.raises(ApiException):
        fn(client, "ns", "cr")


@pytest.fixture
def fake_clock(monkeypatch):
    now = [0]
    monkeypatch.setattr("time.time", lambda: now[0])
    monkeypatch.setattr("time.sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    return now


def test_wait_cr_deletion_requires_not_found(fake_clock):
    client = Mock()
    client.get_namespaced_custom_object.side_effect = [{}, ApiException(status=404)]
    api._wait_troshkavm_deleted(client, "ns", "cr", timeout=5)
    assert fake_clock[0] == 2
    client.get_namespaced_custom_object.side_effect = ApiException(status=403)
    with pytest.raises(ApiException):
        api._wait_troshkavm_deleted(client, "ns", "cr")
    client.get_namespaced_custom_object.side_effect = None
    with pytest.raises(TimeoutError, match="not deleted"):
        api._wait_troshkavm_deleted(client, "ns", "cr", timeout=3)


def test_wait_pvcs_tracks_each_remaining_disk(fake_clock):
    client = Mock()
    client.read_namespaced_persistent_volume_claim.side_effect = [
        ApiException(status=404),
        {},
        ApiException(status=404),
        ApiException(status=404),
    ]
    api._wait_kubevirt_pvcs_deleted(client, "ns", ["root", "data"], timeout=10)
    assert fake_clock[0] == 3
    client.read_namespaced_persistent_volume_claim.side_effect = ApiException(
        status=403
    )
    with pytest.raises(ApiException):
        api._wait_kubevirt_pvcs_deleted(client, "ns", ["root"])
    client.read_namespaced_persistent_volume_claim.side_effect = None
    with pytest.raises(TimeoutError, match="root"):
        api._wait_kubevirt_pvcs_deleted(client, "ns", ["root"], timeout=4)
    api._wait_kubevirt_pvcs_deleted(client, "ns", [])


@pytest.mark.parametrize("state", ["Running", "Stopped"])
def test_redeploy_ready_retries_transient_reads_and_pending_state(
    fake_clock, monkeypatch, state
):
    progress = {}
    monkeypatch.setattr(api, "_redeploy_progress", progress)
    client = Mock()
    client.get_namespaced_custom_object.side_effect = [
        RuntimeError("not yet created"),
        {"status": {"state": "Pending"}},
        {"status": {"state": state}},
    ]
    assert api._wait_troshkavm_redeploy_ready(client, "ns", "cr", "domain") == state
    assert fake_clock[0] == 8
    assert progress["domain"] == {"step": "creating", "detail": "Pending"}


def test_redeploy_ready_surfaces_operator_error_and_timeout(fake_clock, monkeypatch):
    monkeypatch.setattr(api, "_redeploy_progress", {})
    client = Mock()
    client.get_namespaced_custom_object.return_value = {
        "status": {"state": "Error", "message": "disk failed"}
    }
    with pytest.raises(RuntimeError, match="disk failed"):
        api._wait_troshkavm_redeploy_ready(client, "ns", "cr", "domain")
    client.get_namespaced_custom_object.return_value = {}
    with pytest.raises(TimeoutError, match="not ready"):
        api._wait_troshkavm_redeploy_ready(client, "ns", "cr", "domain", timeout=4)
