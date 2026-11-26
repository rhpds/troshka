import uuid
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.models.elastic_ip import ElasticIp
from app.models.native_project_cleanup import NativeProjectNamespaceCleanup
from app.models.vm import VM
from app.services.assistant.contracts import NetworkArgs, NoArgs
from app.services.assistant.network_checks import network, probe_endpoint
from app.services.assistant.policy import authorize
from app.services.assistant.project_checks import cleanup, install


def test_missing_pull_secret_is_distinct_from_later_api_timeout(case):
    db, user, project, conversation, _, now = case
    project.deploy_error = "pullSecret: unexpected end of JSON input PRIVATE_CANARY"
    project.ocp_status = "failed"
    project.ocp_status_detail = "API server not reachable"
    project.state = "active"
    db.commit()
    evidence = install(db, authorize(db, user, conversation.id, now=now), NoArgs())
    assert evidence.facts["failure_categories"] == [
        "pull_secret_missing",
        "api_unreachable",
    ]
    assert evidence.facts["installation_state"] == "failed"
    assert evidence.facts["infrastructure_state"] == "active"
    assert "PRIVATE_CANARY" not in evidence.model_dump_json()


def test_disconnected_nic_preserves_causal_category(case):
    db, user, project, conversation, _, now = case
    project.deploy_error = "NIC missing networkRef; deployment timed out"
    db.commit()
    evidence = install(db, authorize(db, user, conversation.id, now=now), NoArgs())
    assert evidence.facts["failure_categories"] == ["nic_missing_network", "timeout"]


def test_cleanup_does_not_invent_storage_settlement(case):
    db, user, project, conversation, _, now = case
    project.state = "cleanup_pending"
    db.add(
        NativeProjectNamespaceCleanup(
            id=str(uuid.uuid4()),
            project_id=project.id,
            digest="fixture",
            payload={
                "phase": "namespace_absent",
                "last_error": "collection unavailable PRIVATE_CANARY",
            },
        )
    )
    db.commit()
    evidence = cleanup(db, authorize(db, user, conversation.id, now=now), NoArgs())
    assert evidence.facts["storage_settlement"] == "unknown"
    assert evidence.facts["cleanup"][0]["blockers"] == [
        "storage_observation_incomplete"
    ]
    assert "PRIVATE_CANARY" not in evidence.model_dump_json()


@pytest.fixture
def network_case(case):
    db, user, project, conversation, _, now = case
    vm = VM(
        name="bastion", project_id=project.id, state="stopped", ip_address="10.0.0.37"
    )
    endpoint = ElasticIp(
        provider_id=str(uuid.uuid4()),
        project_id=project.id,
        canvas_eip_id="eip1",
        allocation_id="fixture",
        public_ip="8.8.8.8",
    )
    db.add_all([vm, endpoint])
    db.flush()
    project.topology = {
        "nodes": [
            {
                "id": "gateway",
                "data": {
                    "portForwards": [
                        {
                            "extIpId": "eip1",
                            "extPort": "2222",
                            "intIp": "10.0.0.50",
                            "intPort": "22",
                            "proto": "tcp",
                        }
                    ]
                },
            }
        ]
    }
    db.commit()
    return db, project, vm, endpoint, authorize(db, user, conversation.id, now=now)


def test_stopped_vm_ip_mismatch_reported_without_probe(network_case):
    db, _, vm, _, ctx = network_case
    evidence = network(db, ctx, NetworkArgs(vm_id=vm.id))
    assert evidence.facts["recorded_state"] == "stopped"
    assert evidence.facts["recorded_ip"] == "10.0.0.37/32"
    assert evidence.facts["forwards"][0]["target_ip"] == "10.0.0.50/32"
    assert evidence.facts["probe_performed"] is False


def test_missing_or_wrong_forward_never_opens_socket(network_case):
    db, project, vm, endpoint, _ = network_case
    with patch("socket.create_connection") as connect:
        with pytest.raises(HTTPException):
            probe_endpoint(
                db,
                project,
                vm,
                NetworkArgs(vm_id=vm.id, endpoint_id=endpoint.id, external_port=22),
            )
        connect.assert_not_called()


def test_probe_only_uses_bound_public_endpoint(network_case):
    db, project, vm, endpoint, _ = network_case
    vm.ip_address = "10.0.0.50"
    db.commit()
    args = NetworkArgs(vm_id=vm.id, endpoint_id=endpoint.id, external_port=2222)
    with patch("socket.create_connection") as connect:
        result = probe_endpoint(db, project, vm, args)
        connect.assert_called_once_with(("8.8.8.8", 2222), timeout=3)
    assert result["result"] == "tcp_connected"
    assert result["source"] == "Troshka backend"


def test_ops_probe_does_not_claim_guest_reachability(network_case):
    db, project, vm, endpoint, _ = network_case
    vm.ip_address = "10.0.0.50"
    db.commit()
    with patch("socket.create_connection") as connect:
        result = probe_endpoint(
            db,
            project,
            vm,
            NetworkArgs(
                vm_id=vm.id, endpoint_id=endpoint.id, external_port=2222, source="ops"
            ),
        )
        connect.assert_not_called()
    assert result["status"] == "unsupported"


def test_foreign_vm_and_endpoint_are_not_observed(network_case):
    db, project, vm, endpoint, ctx = network_case
    with pytest.raises(HTTPException):
        network(db, ctx, NetworkArgs(vm_id=uuid.uuid4()))
    endpoint.project_id = str(uuid.uuid4())
    db.commit()
    with patch("socket.create_connection") as connect:
        with pytest.raises(HTTPException):
            probe_endpoint(
                db,
                project,
                vm,
                NetworkArgs(vm_id=vm.id, endpoint_id=endpoint.id, external_port=2222),
            )
        connect.assert_not_called()
