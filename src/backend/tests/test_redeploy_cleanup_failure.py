from unittest.mock import Mock, patch

import pytest

from app.services import deploy_service as service


@pytest.mark.parametrize("status", ["failed", "running", None])
def test_cleanup_requires_explicit_completion(status):
    with patch.object(service, "wait_for_job", return_value={"status": status}):
        with pytest.raises(service.TroshkadError):
            service._wait_cleanup_job(Mock(), "job", 30)


def test_cleanup_accepts_completed_job():
    with patch.object(service, "wait_for_job", return_value={"status": "completed"}):
        service._wait_cleanup_job(Mock(), "job", 30)


def test_agent_reported_vm_delete_failure_aborts_cleanup():
    with (
        patch.object(service, "_extract_vms", return_value=[{"node_id": "vm"}]),
        patch.object(service, "start_job", return_value="job"),
        patch.object(
            service,
            "wait_for_job",
            return_value={
                "status": "failed",
                "result": {"error": "libvirt refused deletion"},
            },
        ),
    ):
        with pytest.raises(service.TroshkadError, match="libvirt refused deletion"):
            service._destroy_topology_vms(Mock(), "project", {})


def test_vm_delete_failure_is_not_reported_as_success():
    with (
        patch.object(service, "_extract_vms", return_value=[{"node_id": "vm"}]),
        patch.object(
            service, "start_job", side_effect=service.TroshkadError("delete denied")
        ),
    ):
        with pytest.raises(service.TroshkadError, match="delete denied"):
            service._destroy_topology_vms(Mock(), "project", {})


def test_disk_directory_delete_failure_aborts_cleanup():
    with (
        patch.object(service, "_get_host_pool", return_value=None),
        patch.object(service, "_extract_containers", return_value=[]),
        patch.object(service, "_destroy_topology_vms", return_value=set()),
        patch.object(service, "_reap_orphan_project_domains"),
        patch.object(
            service,
            "start_job",
            side_effect=["pool", service.TroshkadError("remove denied")],
        ),
        patch.object(service, "wait_for_job"),
    ):
        with pytest.raises(service.TroshkadError, match="remove denied"):
            service._destroy_troshkad_resources(Mock(), "project", {}, {}, Mock())
