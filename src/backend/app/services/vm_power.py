"""Shared VM power-state helpers: pause, unpause, and hibernate.

KubeVirt (``host_type == "kubevirt-cluster"``) handles pause/unpause via the
VirtualMachineInstance pause/unpause subresource (K8s API) — the same empty-PUT
pattern used by ``ocpvirt.unpause_host``. Hibernate has no KubeVirt equivalent
(no managed-save upstream yet), so it's rejected there via
``HibernateUnsupported``.

All other host types (troshkad/libvirt hosts) delegate to troshkad's job API
(``/vms/pause``, ``/vms/resume``, ``/vms/hibernate``).
"""

from app.services.deploy_topology import _vm_domain_name
from app.services.troshkad_client import start_job, wait_for_job

_KUBEVIRT_HOST_TYPE = "kubevirt-cluster"


class HibernateUnsupported(Exception):  # noqa: N818
    """Raised when hibernate is requested on a host type that can't support it."""

    code = "hibernate_unsupported"

    def __init__(self, message: str = "Hibernate is not supported on this host type"):
        super().__init__(message)


def supports_hibernate(host) -> bool:
    """KubeVirt has no managed-save equivalent yet — only libvirt hosts hibernate."""
    return host.host_type != _KUBEVIRT_HOST_TYPE


def _kv_name(vm_id: str) -> str:
    return f"troshka-vm-{vm_id[:8]}"


def _kubevirt_vmi_subresource(host, project_id: str, vm_id: str, action: str) -> None:
    """PUT an empty body to the KubeVirt VMI pause/unpause subresource.

    Mirrors ``ocpvirt.unpause_host``: the subresource requires an empty PUT
    with ``Accept: */*`` — ``Accept: application/json`` (the default injected
    by ``call_api``) yields 406, and a JSON body also fails.
    """
    from app.services.providers.kubevirt import _get_k8s_clients, _project_ns

    provider = host.provider
    creds = provider.get_credentials()
    namespace = _project_ns(provider, project_id)
    kv_name = _kv_name(vm_id)
    custom_api, _core_api, _api_client = _get_k8s_clients(provider)
    path = (
        f"/apis/subresources.kubevirt.io/v1/namespaces/{namespace}"
        f"/virtualmachineinstances/{kv_name}/{action}"
    )
    base = custom_api.api_client.configuration.host.rstrip("/")
    custom_api.api_client.request(
        "PUT",
        f"{base}{path}",
        headers={
            "Authorization": f"Bearer {creds['token']}",
            "Accept": "*/*",
        },
    )


def pause_vm_on_host(host, project_id: str, vm_id: str) -> None:
    """Suspend a running VM in place (RAM retained, CPU/I-O suspended)."""
    if host.host_type == _KUBEVIRT_HOST_TYPE:
        _kubevirt_vmi_subresource(host, project_id, vm_id, "pause")
        return
    dom = _vm_domain_name(project_id, vm_id)
    job_id = start_job(host, "/vms/pause", {"domain_name": dom})
    wait_for_job(host, job_id, timeout=60, poll_interval=2)


def unpause_vm_on_host(host, project_id: str, vm_id: str) -> None:
    """Resume a VM previously paused by :func:`pause_vm_on_host`."""
    if host.host_type == _KUBEVIRT_HOST_TYPE:
        _kubevirt_vmi_subresource(host, project_id, vm_id, "unpause")
        return
    dom = _vm_domain_name(project_id, vm_id)
    job_id = start_job(host, "/vms/resume", {"domain_name": dom})
    wait_for_job(host, job_id, timeout=60, poll_interval=2)


def hibernate_vm_on_host(host, project_id: str, vm_id: str) -> None:
    """Hibernate (``virsh managedsave``) a running VM.

    Raises:
        HibernateUnsupported: On KubeVirt hosts (no managed-save equivalent).
    """
    if not supports_hibernate(host):
        raise HibernateUnsupported()
    dom = _vm_domain_name(project_id, vm_id)
    job_id = start_job(host, "/vms/hibernate", {"domain_name": dom})
    wait_for_job(host, job_id, timeout=300, poll_interval=2)
