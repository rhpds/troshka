"""Libvirt "bring your own host" provider driver.

Adopts an existing SSH-reachable Linux box (with libvirt + nested
virtualization already set up) as a Troshka host instead of provisioning
new compute via a cloud API. Intended for local/dev testing against a host
you already built yourself (e.g. a disposable VM) — Troshka never creates
or destroys the underlying machine for this provider type.
"""

import logging

from app.services.providers.base import ProviderDriver

logger = logging.getLogger(__name__)

# Not a real hardware/API limit — libvirt hosts have no cloud EIP resource
# to allocate. This just caps how many gateway port-forward rules can
# reference this host, since they all share the same host.ip_address (see
# allocate_eip below). Bump via PATCH /hosts/{id} if you need more.
DEFAULT_MAX_EIPS = 50


class LibvirtDriver(ProviderDriver):
    def provision_host(
        self, provider, host_id, instance_type, storage_size_gb, **kwargs
    ):
        ip_address = kwargs.get("ip_address")
        if not ip_address:
            raise ValueError("ip_address is required for libvirt provider hosts")

        creds = provider.get_credentials()
        private_key = creds.get("ssh_private_key", "")
        if not private_key:
            raise ValueError(
                "Provider credentials are missing 'ssh_private_key'"
            )

        return {
            "host_id": host_id,
            "instance_id": f"libvirt-{host_id[:8]}",
            "instance_type": instance_type or "existing-host",
            "public_ip": ip_address,
            "private_ip": ip_address,
            # Corrected automatically by the health poller once troshkad
            # connects and reports real capacity (see health_poller.py).
            "total_vcpus": 0,
            "total_ram_mb": 0,
            "private_key": private_key,
            "key_pair_name": f"troshka-libvirt-{host_id[:8]}",
            "storage_size_gb": storage_size_gb,
            "max_eips": DEFAULT_MAX_EIPS,
        }

    def terminate_host(self, provider, instance_id):
        # No-op: this machine is owned/managed by the admin directly —
        # Troshka didn't create it via a cloud API, so it shouldn't try
        # to destroy it. Removing the Host row is enough on our side.
        pass

    def get_host_status(self, provider, instance_id):
        # There's no cloud instance to poll — terminate_host() above is
        # already a no-op, so as far as _wait_terminated_bg() is concerned
        # this host is "terminated" the moment deletion is requested.
        # Without this override, the base class's NotImplementedError gets
        # swallowed by _wait_terminated_bg's broad except, silently leaving
        # the Host row stuck in "shutting_down" forever.
        return {
            "instance_id": instance_id,
            "state": "terminated",
            "public_ip": None,
            "private_ip": None,
        }

    def allocate_eip(self, provider, host, eip_id, project_id=None):
        # No cloud API to call — a libvirt host has exactly one real
        # address, so "allocating an EIP" just means handing back that
        # same address with a synthetic allocation id for bookkeeping.
        if not host.ip_address:
            raise ValueError("Host has no ip_address to use as an elastic IP")

        allocation_id = f"libvirt-eip-{eip_id[:8]}"
        logger.info(
            "Allocated libvirt EIP %s (%s) for host %s using host IP",
            host.ip_address,
            allocation_id,
            host.id[:8],
        )
        return {"public_ip": host.ip_address, "allocation_id": allocation_id}

    def associate_eip(self, provider, host, allocation_id):
        # Returning the host's own IP as private_ip is what makes
        # troshkad's host-level nftables DNAT (priv_ip branch) fire,
        # mapping host.ip_address:extPort -> transit_ip:extPort. See
        # _setup_host_port_forward_dnat in src/troshkad/troshkad.py.
        return {"private_ip": host.ip_address}

    def release_eip(self, provider, allocation_id, namespace=None):
        # No cloud resource was ever created — nothing to release.
        logger.info("Released libvirt EIP %s (no-op, no cloud resource)", allocation_id)
