"""Tests for app.services.vm_power — pause/unpause/hibernate helpers.

All troshkad and K8s I/O is mocked; these exercise the host-type dispatch
and the shape of the calls made to each backend.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.vm_power import (
    HibernateUnsupported,
    hibernate_vm_on_host,
    pause_vm_on_host,
    supports_hibernate,
    unpause_vm_on_host,
)

_PROJECT_ID = "proj-abcdef12"
_VM_ID = "vm-12345678"


def _troshkad_host(**kwargs):
    defaults = {"host_type": "shared"}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _kubevirt_host():
    provider = MagicMock()
    provider.get_credentials.return_value = {"token": "tok"}
    return SimpleNamespace(host_type="kubevirt-cluster", provider=provider)


# ---------------------------------------------------------------------------
# supports_hibernate
# ---------------------------------------------------------------------------
def test_supports_hibernate_false_for_kubevirt():
    host = SimpleNamespace(host_type="kubevirt-cluster")
    assert supports_hibernate(host) is False


def test_supports_hibernate_true_for_shared():
    host = SimpleNamespace(host_type="shared")
    assert supports_hibernate(host) is True


# ---------------------------------------------------------------------------
# hibernate_vm_on_host — KubeVirt unsupported
# ---------------------------------------------------------------------------
def test_hibernate_kubevirt_raises():
    with pytest.raises(HibernateUnsupported):
        hibernate_vm_on_host(SimpleNamespace(host_type="kubevirt-cluster"), "p", "v")


def test_hibernate_unsupported_code():
    exc = HibernateUnsupported()
    assert exc.code == "hibernate_unsupported"


# ---------------------------------------------------------------------------
# troshkad/libvirt dispatch
# ---------------------------------------------------------------------------
@patch("app.services.vm_power.wait_for_job")
@patch("app.services.vm_power.start_job")
def test_pause_vm_on_host_troshkad(mock_start_job, mock_wait_for_job):
    host = _troshkad_host()
    mock_start_job.return_value = "job-1"

    pause_vm_on_host(host, _PROJECT_ID, _VM_ID)

    path = mock_start_job.call_args[0][1]
    params = mock_start_job.call_args[0][2]
    assert path == "/vms/pause"
    assert params["domain_name"]
    mock_wait_for_job.assert_called_once_with(
        host, "job-1", timeout=60, poll_interval=2
    )


@patch("app.services.vm_power.wait_for_job")
@patch("app.services.vm_power.start_job")
def test_unpause_vm_on_host_troshkad(mock_start_job, mock_wait_for_job):
    host = _troshkad_host()
    mock_start_job.return_value = "job-2"

    unpause_vm_on_host(host, _PROJECT_ID, _VM_ID)

    assert mock_start_job.call_args[0][1] == "/vms/resume"
    mock_wait_for_job.assert_called_once_with(
        host, "job-2", timeout=60, poll_interval=2
    )


@patch("app.services.vm_power.wait_for_job")
@patch("app.services.vm_power.start_job")
def test_hibernate_vm_on_host_troshkad(mock_start_job, mock_wait_for_job):
    host = _troshkad_host()
    mock_start_job.return_value = "job-3"

    hibernate_vm_on_host(host, _PROJECT_ID, _VM_ID)

    assert mock_start_job.call_args[0][1] == "/vms/hibernate"
    mock_wait_for_job.assert_called_once_with(
        host, "job-3", timeout=300, poll_interval=2
    )


# ---------------------------------------------------------------------------
# KubeVirt dispatch — VMI pause/unpause subresource
# ---------------------------------------------------------------------------
@patch("app.services.providers.kubevirt._project_ns", return_value="troshka-proj")
@patch("app.services.providers.kubevirt._get_k8s_clients")
def test_pause_vm_on_host_kubevirt(mock_get_clients, _mock_project_ns):
    custom_api = MagicMock()
    custom_api.api_client.configuration.host = "https://cluster.example.com"
    mock_get_clients.return_value = (custom_api, MagicMock(), MagicMock())
    host = _kubevirt_host()

    pause_vm_on_host(host, _PROJECT_ID, _VM_ID)

    args, kwargs = custom_api.api_client.request.call_args
    assert args[0] == "PUT"
    assert args[1].endswith(f"/virtualmachineinstances/troshka-vm-{_VM_ID[:8]}/pause")
    assert kwargs["headers"]["Accept"] == "*/*"
    assert kwargs["headers"]["Authorization"] == "Bearer tok"


@patch("app.services.providers.kubevirt._project_ns", return_value="troshka-proj")
@patch("app.services.providers.kubevirt._get_k8s_clients")
def test_unpause_vm_on_host_kubevirt(mock_get_clients, _mock_project_ns):
    custom_api = MagicMock()
    custom_api.api_client.configuration.host = "https://cluster.example.com"
    mock_get_clients.return_value = (custom_api, MagicMock(), MagicMock())
    host = _kubevirt_host()

    unpause_vm_on_host(host, _PROJECT_ID, _VM_ID)

    args, _kwargs = custom_api.api_client.request.call_args
    assert args[0] == "PUT"
    assert args[1].endswith(f"/virtualmachineinstances/troshka-vm-{_VM_ID[:8]}/unpause")
