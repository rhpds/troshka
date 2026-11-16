from app.services.deploy_topology import validate_vm_network_connections


def topology(edges=None):
    return {
        "nodes": [
            {
                "id": "vm",
                "type": "vmNode",
                "data": {
                    "name": "bastion",
                    "nics": [
                        {"id": "nic-one", "name": "eth0"},
                        {"id": "nic-two", "name": "eth1"},
                    ],
                },
            },
            {"id": "net", "type": "networkNode", "data": {"name": "mgmt"}},
        ],
        "edges": edges or [],
    }


def test_reports_each_disconnected_nic_even_if_other_nic_is_connected():
    errors = validate_vm_network_connections(
        topology(
            [
                {"source": "net", "target": "vm", "targetHandle": "nic-nic-one-top"},
            ]
        )
    )
    assert len(errors) == 1
    assert all(value in errors[0] for value in ("bastion", "eth1", "nic-two"))


def test_accepts_both_edge_directions_and_handle_positions():
    assert (
        validate_vm_network_connections(
            topology(
                [
                    {
                        "source": "net",
                        "target": "vm",
                        "targetHandle": "nic-nic-one-top",
                    },
                    {
                        "source": "vm",
                        "target": "net",
                        "sourceHandle": "nic-nic-two-bottom",
                    },
                ]
            )
        )
        == []
    )


def test_stale_network_edge_does_not_count_as_connection():
    assert (
        len(
            validate_vm_network_connections(
                topology(
                    [
                        {
                            "source": "deleted",
                            "target": "vm",
                            "targetHandle": "nic-nic-one-top",
                        },
                    ]
                )
            )
        )
        == 2
    )


def test_deploy_api_uses_network_validation():
    from app.api.projects import _collect_deploy_topology_errors

    assert any("eth1" in error for error in _collect_deploy_topology_errors(topology()))


def test_worker_rejects_invalid_nic_before_native_dispatch(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.services import deploy_service, native_managed_deploy, quota_operations

    project = SimpleNamespace(id="project", state="deploying", topology=topology())
    db = Mock()
    db.query.return_value.filter_by.return_value.first.return_value = project
    dispatch = Mock()
    notify = Mock()
    monkeypatch.setattr(native_managed_deploy, "dispatch", dispatch)
    monkeypatch.setattr(deploy_service, "_set_deploy_error_and_notify", notify)
    token = quota_operations.current_operation.set("operation")
    try:
        assert deploy_service._deploying_project(db, "project") is None
    finally:
        quota_operations.current_operation.reset(token)
    dispatch.assert_not_called()
    assert "eth1" in notify.call_args.args[-1]


def test_placement_rejects_invalid_nic_before_reserving_resources(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.services import placement

    managed = Mock(return_value=None)
    monkeypatch.setattr(placement, "_managed_native_placement", managed)
    result = placement.place_project(Mock(), SimpleNamespace(topology=topology()))
    assert "eth1" in result["error"]
    managed.assert_not_called()
