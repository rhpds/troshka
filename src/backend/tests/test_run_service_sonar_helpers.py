"""Unit tests for workloads/run_service pure helpers (Sonar new-code coverage)."""

from unittest.mock import MagicMock, patch

from app.services.workloads import run_service as rs


def test_has_ocp_reads_node_kubeconfig():
    project = MagicMock(
        deployed_topology={"nodes": [{"data": {"ocpKubeconfig": "KC"}}, {"data": {}}]},
        topology=None,
    )
    assert rs._has_ocp(project) is True
    assert (
        rs._has_ocp(MagicMock(deployed_topology={"nodes": []}, topology=None)) is False
    )


def test_run_limit_joins_vm_names():
    assert rs._run_limit({"vm_names": ["a", "b"]}) == "a,b"
    assert rs._run_limit({"vm_names": []}) is None
    assert rs._run_limit(None) is None


def test_inventory_connection_mode_always_troshka():
    assert rs._inventory_connection_mode(None) == "troshka"
    assert rs._inventory_connection_mode({"anything": 1}) == "troshka"


def test_resolve_kubeconfig_by_cluster_id():
    topo = {"ignored": True}
    with patch.object(
        rs,
        "_stored_cluster_creds",
        return_value={"c1": ("pw", "KC1"), "c2": ("pw", "KC2")},
    ):
        assert rs._resolve_kubeconfig(topo, {"cluster_id": "c1"}) == "KC1"
        assert rs._resolve_kubeconfig(topo, {"cluster_id": "missing"}) is None
        assert rs._resolve_kubeconfig(topo, None) == "KC1"


def test_validate_run_inventory_with_vm_names():
    with patch("app.services.workloads.inventory.validate_vm_names") as mock_validate:
        rs._validate_run_inventory({"nodes": []}, {"vm_names": ["vm1"]})
        mock_validate.assert_called_once()


@patch("app.services.workloads.run_service._now", return_value=1)
def test_fail_run_sets_error_status(_now):
    db = MagicMock()
    run = MagicMock()
    db.get.return_value = run
    rs._fail_run(db, "run-1", "boom")
    assert run.status == "error"
    assert run.error == "boom"
    db.commit.assert_called()
