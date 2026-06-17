"""Azure provider driver.

Provisions Azure VMs with nested virtualization,
manages VNet/NSG networking, Azure DNS, public IPs, and Azure Files NFS.
Self-contained with helper functions (same pattern as GCP driver).
"""

import base64
import logging
import time

from app.services.providers.base import ProviderDriver

logger = logging.getLogger(__name__)

AZURE_DEFAULT_INSTANCE_TYPE = "Standard_E32s_v5"
AZURE_CURATED_INSTANCE_TYPES = [
    "Standard_E4s_v5",
    "Standard_E8s_v5",
    "Standard_E16s_v5",
    "Standard_E32s_v5",
    "Standard_E48s_v5",
    "Standard_E64s_v5",
    "Standard_E96s_v5",
]
AZURE_RAM_PER_VCPU_GB = {
    "Standard_E4s_v5": (4, 32),
    "Standard_E8s_v5": (8, 64),
    "Standard_E16s_v5": (16, 128),
    "Standard_E32s_v5": (32, 256),
    "Standard_E48s_v5": (48, 384),
    "Standard_E64s_v5": (64, 512),
    "Standard_E96s_v5": (96, 672),
}

CLOUD_INIT_TEMPLATE = """#cloud-config
packages:
  - qemu-kvm
  - libvirt-daemon-system
  - libvirt-clients
  - virtinst
  - dnsmasq
  - nftables
  - python3
  - xorriso
  - ncat
  - sshpass
  - nfs-common

runcmd:
  - |
    DATA_DEV=/dev/disk/azure/scsi1/lun0
    # Wait for Azure data disk to appear (can take a few seconds)
    for i in $(seq 1 30); do [ -b "$DATA_DEV" ] && break; sleep 2; done
    if [ -b "$DATA_DEV" ]; then
      blkid "$DATA_DEV" || mkfs.ext4 -F "$DATA_DEV"
      mkdir -p /var/lib/troshka
      mount "$DATA_DEV" /var/lib/troshka
      grep -q /var/lib/troshka /etc/fstab || echo "$DATA_DEV /var/lib/troshka ext4 defaults,nofail 0 2" >> /etc/fstab
    else
      mkdir -p /var/lib/troshka
    fi
  - mkdir -p /var/lib/troshka/vms /var/lib/troshka/images /var/lib/troshka/seeds /var/lib/troshka/tmp /var/lib/troshka/local /var/lib/troshka/cache /etc/troshka-agent
{nfs_setup}  - systemctl enable --now libvirtd
  - systemctl enable --now nftables
  - systemctl disable --now dnsmasq 2>/dev/null || true
  - sysctl -w vm.overcommit_memory=1
  - echo "vm.overcommit_memory=1" >> /etc/sysctl.d/99-troshka.conf
  - |
    echo 1 > /sys/kernel/mm/ksm/run
    echo 200 > /sys/kernel/mm/ksm/sleep_millisecs
  - 'echo "host_id: {host_id}" > /etc/troshka-agent/host-id'
"""


def _get_credential(creds_dict):
    """Build ClientSecretCredential from stored credentials."""
    from azure.identity import ClientSecretCredential

    return ClientSecretCredential(
        tenant_id=creds_dict["tenant_id"],
        client_id=creds_dict["client_id"],
        client_secret=creds_dict["client_secret"],
    )


def _get_subscription_id(creds_dict):
    """Extract subscription_id from credentials dict."""
    return creds_dict["subscription_id"]


def _get_compute_client(creds_dict):
    """Return ComputeManagementClient."""
    from azure.mgmt.compute import ComputeManagementClient

    credential = _get_credential(creds_dict)
    subscription_id = _get_subscription_id(creds_dict)
    return ComputeManagementClient(credential, subscription_id)


def _get_network_client(creds_dict):
    """Return NetworkManagementClient."""
    from azure.mgmt.network import NetworkManagementClient

    credential = _get_credential(creds_dict)
    subscription_id = _get_subscription_id(creds_dict)
    return NetworkManagementClient(credential, subscription_id)


def _get_dns_client(creds_dict):
    """Return DnsManagementClient."""
    from azure.mgmt.dns import DnsManagementClient

    credential = _get_credential(creds_dict)
    subscription_id = _get_subscription_id(creds_dict)
    return DnsManagementClient(credential, subscription_id)


def _parse_instance_type(instance_type):
    """Parse instance type into (vcpus, ram_mb)."""
    if not instance_type:
        instance_type = AZURE_DEFAULT_INSTANCE_TYPE

    specs = AZURE_RAM_PER_VCPU_GB.get(instance_type)
    if specs:
        vcpus, ram_gb = specs
        return vcpus, ram_gb * 1024

    # Fallback: try to parse vCPU count from the type name (e.g. Standard_E32s_v5)
    import re

    match = re.search(r"(\d+)", instance_type)
    vcpus = int(match.group(1)) if match else 32
    return vcpus, vcpus * 8 * 1024


def _generate_ssh_keypair():
    """Generate an RSA 4096 SSH keypair for provisioning."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    public_key = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    return private_pem, public_key


def _build_cloud_init(host_id, nfs_server=None, nfs_path=None):
    """Build cloud-init user data YAML."""
    nfs_setup = ""
    if nfs_server and nfs_path:
        nfs_setup = (
            f"  - mkdir -p /var/lib/troshka/shared\n"
            f"  - 'echo \"{nfs_server}:{nfs_path} /var/lib/troshka/shared nfs "
            f"nfsvers=4.1,nconnect=16,hard,_netdev 0 0\" >> /etc/fstab'\n"
            f"  - mount /var/lib/troshka/shared\n"
        )
    return CLOUD_INIT_TEMPLATE.format(host_id=host_id, nfs_setup=nfs_setup)


def _parse_image_urn(image_urn):
    """Parse an Azure image reference.

    Accepts either:
    - URN format: 'Publisher:Offer:Sku:Version' (marketplace images)
    - Resource ID: '/subscriptions/.../images/name' (managed images from Image Builder)
    """
    if image_urn.startswith("/subscriptions/"):
        return {"id": image_urn}
    parts = image_urn.split(":")
    if len(parts) != 4:
        raise ValueError(
            f"Invalid Azure image reference '{image_urn}' — "
            f"expected URN (Publisher:Offer:Sku:Version) or resource ID (/subscriptions/...)"
        )
    return {
        "publisher": parts[0],
        "offer": parts[1],
        "sku": parts[2],
        "version": parts[3],
    }


def _resource_not_found(exc):
    """Check if an Azure exception is a 'not found' error."""
    err_str = str(exc)
    return (
        "ResourceNotFound" in err_str
        or "NotFound" in err_str
        or "was not found" in err_str
        or "could not be found" in err_str
    )


def _delete_resource(label, delete_fn):
    """Call delete_fn, handle 'not found' gracefully, wait for completion."""
    try:
        poller = delete_fn()
        if hasattr(poller, "result"):
            poller.result()
        logger.info("Deleted %s", label)
    except Exception as e:
        if _resource_not_found(e):
            logger.info("%s already gone", label)
        else:
            raise


class AzureDriver(ProviderDriver):
    def provision_host(
        self, provider, host_id, instance_type, storage_size_gb, **kwargs
    ):
        creds_dict = provider.get_credentials()
        rg = provider.azure_resource_group
        location = provider.azure_location or provider.default_region

        instance_type = instance_type or AZURE_DEFAULT_INSTANCE_TYPE
        vcpus, ram_mb = _parse_instance_type(instance_type)
        instance_name = f"troshka-{host_id[:12]}"
        nic_name = f"troshka-nic-{host_id[:12]}"
        ip_name = f"troshka-ip-{host_id[:12]}"
        os_disk_name = f"troshka-{host_id[:12]}-os"
        data_disk_name = f"troshka-data-{host_id[:12]}"

        private_key, public_key = _generate_ssh_keypair()

        # Build cloud-init
        nfs_server = kwargs.get("nfs_server")
        nfs_path = kwargs.get("nfs_path")
        user_data = _build_cloud_init(host_id, nfs_server, nfs_path)
        custom_data_b64 = base64.b64encode(user_data.encode()).decode()

        # Resolve boot image URN
        image_urn = kwargs.get("image_id") or provider.default_image
        if not image_urn:
            raise ValueError(
                "No boot image specified — set image_id or default_image "
                "(format: Publisher:Offer:Sku:Version)"
            )
        image_ref = _parse_image_urn(image_urn)

        network_client = _get_network_client(creds_dict)
        compute_client = _get_compute_client(creds_dict)

        # --- Create public IP ---
        poller = network_client.public_ip_addresses.begin_create_or_update(
            rg,
            ip_name,
            {
                "location": location,
                "sku": {"name": "Standard"},
                "public_ip_allocation_method": "Static",
                "tags": {"managed-by": "troshka", "troshka-host-id": host_id[:12]},
            },
        )
        public_ip_resource = poller.result()
        logger.info("Created public IP %s (%s)", ip_name, public_ip_resource.ip_address)

        # --- Create NIC ---
        nic_params = {
            "location": location,
            "ip_configurations": [
                {
                    "name": "primary",
                    "subnet": {"id": provider.azure_subnet_id},
                    "public_ip_address": {"id": public_ip_resource.id},
                    "primary": True,
                }
            ],
            "tags": {"managed-by": "troshka", "troshka-host-id": host_id[:12]},
        }
        if provider.azure_nsg_id:
            nic_params["network_security_group"] = {"id": provider.azure_nsg_id}

        poller = network_client.network_interfaces.begin_create_or_update(
            rg, nic_name, nic_params
        )
        nic = poller.result()
        logger.info("Created NIC %s", nic_name)

        # --- Accept marketplace terms if needed (BYOS images) ---
        # Skip marketplace terms entirely for managed images (Image Builder)
        if "id" not in image_ref:
            try:
                from azure.mgmt.marketplaceordering import MarketplaceOrderingAgreements

                credential = _get_credential(creds_dict)
                subscription_id = _get_subscription_id(creds_dict)
                mp_client = MarketplaceOrderingAgreements(credential, subscription_id)
                agreement = mp_client.marketplace_agreements.get(
                    offer_type="virtualmachine",
                    publisher_id=image_ref["publisher"],
                    offer_id=image_ref["offer"],
                    plan_id=image_ref["sku"],
                )
                agreement.accepted = True
                mp_client.marketplace_agreements.create(
                    offer_type="virtualmachine",
                    publisher_id=image_ref["publisher"],
                    offer_id=image_ref["offer"],
                    plan_id=image_ref["sku"],
                    parameters=agreement,
                )
                logger.info("Accepted marketplace terms for %s", image_urn)
                plan_info = {
                    "name": image_ref["sku"],
                    "publisher": image_ref["publisher"],
                    "product": image_ref["offer"],
                }
            except Exception as e:
                if "not found" in str(e).lower() or "no agreement" in str(e).lower():
                    # Not a marketplace image, no terms needed
                    plan_info = None
                else:
                    logger.debug(
                        "Marketplace terms check for %s: %s (proceeding without plan)",
                        image_urn,
                        e,
                    )
                    plan_info = None
        else:
            plan_info = None

        # --- Create VM ---
        from azure.mgmt.compute.models import (
            DataDisk,
            HardwareProfile,
            ImageReference,
            LinuxConfiguration,
            ManagedDiskParameters,
            NetworkInterfaceReference,
            NetworkProfile,
            OSDisk,
            OSProfile,
            SshConfiguration,
            SshPublicKey,
            StorageProfile,
            VirtualMachine,
        )

        if "id" in image_ref:
            image_reference = ImageReference(id=image_ref["id"])
        else:
            image_reference = ImageReference(
                publisher=image_ref["publisher"],
                offer=image_ref["offer"],
                sku=image_ref["sku"],
                version=image_ref["version"],
            )

        vm_obj = VirtualMachine(
            location=location,
            tags={"managed-by": "troshka", "troshka-host-id": host_id[:12]},
            hardware_profile=HardwareProfile(vm_size=instance_type),
            storage_profile=StorageProfile(
                image_reference=image_reference,
                os_disk=OSDisk(
                    name=os_disk_name,
                    create_option="FromImage",
                    disk_size_gb=50,
                    managed_disk=ManagedDiskParameters(
                        storage_account_type="Premium_LRS"
                    ),
                    delete_option="Detach",
                ),
                data_disks=[
                    DataDisk(
                        lun=0,
                        name=data_disk_name,
                        create_option="Empty",
                        disk_size_gb=storage_size_gb,
                        managed_disk=ManagedDiskParameters(
                            storage_account_type="Premium_LRS"
                        ),
                        delete_option="Detach",
                    )
                ],
            ),
            os_profile=OSProfile(
                computer_name=instance_name[:15],
                admin_username="troshka",
                custom_data=custom_data_b64,
                linux_configuration=LinuxConfiguration(
                    disable_password_authentication=True,
                    ssh=SshConfiguration(
                        public_keys=[
                            SshPublicKey(
                                path="/home/troshka/.ssh/authorized_keys",
                                key_data=public_key,
                            )
                        ]
                    ),
                ),
            ),
            network_profile=NetworkProfile(
                network_interfaces=[NetworkInterfaceReference(id=nic.id, primary=True)]
            ),
        )

        if plan_info:
            from azure.mgmt.compute.models import Plan

            vm_obj.plan = Plan(
                name=plan_info["name"],
                publisher=plan_info["publisher"],
                product=plan_info["product"],
            )

        poller = compute_client.virtual_machines.begin_create_or_update(
            rg, instance_name, vm_obj
        )
        poller.result()
        logger.info("Created VM %s (%s) in %s", instance_name, instance_type, location)

        # --- Poll until running and get IPs ---
        public_ip = None
        private_ip = None
        for attempt in range(60):
            time.sleep(5)
            vm = compute_client.virtual_machines.get(
                rg, instance_name, expand="instanceView"
            )
            power_state = None
            if vm.instance_view and vm.instance_view.statuses:
                for status in vm.instance_view.statuses:
                    if status.code and status.code.startswith("PowerState/"):
                        power_state = status.code.split("/", 1)[1]
                        break

            if power_state == "running":
                # Get IPs from the NIC
                nic_info = network_client.network_interfaces.get(rg, nic_name)
                for ip_config in nic_info.ip_configurations:
                    if ip_config.private_ip_address:
                        private_ip = ip_config.private_ip_address
                    if ip_config.public_ip_address:
                        pip = network_client.public_ip_addresses.get(rg, ip_name)
                        public_ip = pip.ip_address
                break
        else:
            raise RuntimeError(
                f"VM {instance_name} did not reach running state within 5 minutes"
            )

        return {
            "host_id": host_id,
            "instance_id": instance_name,
            "instance_type": instance_type,
            "public_ip": public_ip,
            "private_ip": private_ip,
            "total_vcpus": vcpus,
            "total_ram_mb": ram_mb,
            "key_pair_name": None,
            "private_key": private_key,
            "storage_size_gb": storage_size_gb,
            "max_eips": 32,
            "_ssh_host": public_ip,
            "_ssh_port": 22,
            "_ssh_user": "troshka",
        }

    def terminate_host(self, provider, instance_id):
        creds_dict = provider.get_credentials()
        rg = provider.azure_resource_group

        compute_client = _get_compute_client(creds_dict)
        network_client = _get_network_client(creds_dict)

        # Derive resource names from instance_id (troshka-{host_id[:12]})
        suffix = instance_id.replace("troshka-", "", 1)
        nic_name = f"troshka-nic-{suffix}"
        ip_name = f"troshka-ip-{suffix}"
        os_disk_name = f"troshka-{suffix}-os"
        data_disk_name = f"troshka-data-{suffix}"

        # 1. Delete VM (must be first — releases NIC and disk references)
        _delete_resource(
            f"VM {instance_id}",
            lambda: compute_client.virtual_machines.begin_delete(rg, instance_id),
        )

        # 2. Delete OS disk
        _delete_resource(
            f"OS disk {os_disk_name}",
            lambda: compute_client.disks.begin_delete(rg, os_disk_name),
        )

        # 3. Delete data disk
        _delete_resource(
            f"data disk {data_disk_name}",
            lambda: compute_client.disks.begin_delete(rg, data_disk_name),
        )

        # 4. Delete NIC
        _delete_resource(
            f"NIC {nic_name}",
            lambda: network_client.network_interfaces.begin_delete(rg, nic_name),
        )

        # 5. Delete public IP
        _delete_resource(
            f"public IP {ip_name}",
            lambda: network_client.public_ip_addresses.begin_delete(rg, ip_name),
        )

        logger.info("Terminated Azure host %s", instance_id)

    def get_host_status(self, provider, instance_id):
        creds_dict = provider.get_credentials()
        rg = provider.azure_resource_group

        compute_client = _get_compute_client(creds_dict)
        network_client = _get_network_client(creds_dict)

        try:
            vm = compute_client.virtual_machines.get(
                rg, instance_id, expand="instanceView"
            )
        except Exception as e:
            if _resource_not_found(e):
                return None
            raise

        # Parse power state from instance view statuses
        state_map = {
            "running": "running",
            "stopped": "stopped",
            "deallocated": "stopped",
            "deallocating": "stopping",
            "starting": "pending",
            "stopping": "stopping",
        }
        power_state = "unknown"
        if vm.instance_view and vm.instance_view.statuses:
            for status in vm.instance_view.statuses:
                if status.code and status.code.startswith("PowerState/"):
                    raw = status.code.split("/", 1)[1]
                    power_state = state_map.get(raw, "unknown")
                    break

        # Get IPs from NIC
        public_ip = None
        private_ip = None
        if vm.network_profile and vm.network_profile.network_interfaces:
            nic_ref = vm.network_profile.network_interfaces[0]
            # Extract NIC name from the resource ID
            nic_name = nic_ref.id.rsplit("/", 1)[-1]
            try:
                nic_info = network_client.network_interfaces.get(rg, nic_name)
                for ip_config in nic_info.ip_configurations:
                    if ip_config.private_ip_address:
                        private_ip = ip_config.private_ip_address
                    if ip_config.public_ip_address:
                        pip_name = ip_config.public_ip_address.id.rsplit("/", 1)[-1]
                        pip = network_client.public_ip_addresses.get(rg, pip_name)
                        public_ip = pip.ip_address
            except Exception:
                logger.debug("Could not retrieve NIC info for %s", instance_id)

        return {
            "instance_id": instance_id,
            "state": power_state,
            "public_ip": public_ip,
            "private_ip": private_ip,
        }

    def get_host_powerstate(self, provider, instance_id):
        status = self.get_host_status(provider, instance_id)
        return status["state"] if status else "unknown"

    def start_host(self, provider, instance_id):
        creds_dict = provider.get_credentials()
        rg = provider.azure_resource_group

        compute_client = _get_compute_client(creds_dict)
        poller = compute_client.virtual_machines.begin_start(rg, instance_id)
        poller.result()
        logger.info("Started Azure VM %s", instance_id)

    def stop_host(self, provider, instance_id):
        creds_dict = provider.get_credentials()
        rg = provider.azure_resource_group

        compute_client = _get_compute_client(creds_dict)
        # Use deallocate (not power_off) to release compute billing
        poller = compute_client.virtual_machines.begin_deallocate(rg, instance_id)
        poller.result()
        logger.info("Deallocated Azure VM %s", instance_id)

    def resize_host(self, provider, instance_id, new_instance_type):
        creds_dict = provider.get_credentials()
        rg = provider.azure_resource_group

        compute_client = _get_compute_client(creds_dict)

        # Try hot-resize first (update VM size in-place)
        try:
            poller = compute_client.virtual_machines.begin_update(
                rg,
                instance_id,
                {"hardware_profile": {"vm_size": new_instance_type}},
            )
            poller.result()
            logger.info("Hot-resized %s to %s", instance_id, new_instance_type)
        except Exception as e:
            logger.info(
                "Hot-resize failed for %s (%s), falling back to deallocate+resize",
                instance_id,
                e,
            )
            # Deallocate
            poller = compute_client.virtual_machines.begin_deallocate(rg, instance_id)
            poller.result()

            # Update VM size
            poller = compute_client.virtual_machines.begin_update(
                rg,
                instance_id,
                {"hardware_profile": {"vm_size": new_instance_type}},
            )
            poller.result()

            # Start
            poller = compute_client.virtual_machines.begin_start(rg, instance_id)
            poller.result()
            logger.info(
                "Resized %s to %s (via deallocate)", instance_id, new_instance_type
            )

        vcpus, ram_mb = _parse_instance_type(new_instance_type)
        return {
            "instance_type": new_instance_type,
            "total_vcpus": vcpus,
            "total_ram_mb": ram_mb,
        }

    def extend_host_storage(self, provider, host, db, increment_gb=None):
        creds_dict = provider.get_credentials()
        rg = provider.azure_resource_group

        increment = increment_gb or host.auto_extend_increment_gb
        old_size = host.storage_size_gb
        new_size = old_size + increment
        if host.auto_extend_max_gb:
            new_size = min(new_size, host.auto_extend_max_gb)
        if new_size <= old_size:
            raise ValueError(f"Cannot extend: already at max ({old_size} GB)")

        suffix = host.instance_id.replace("troshka-", "", 1)
        data_disk_name = f"troshka-data-{suffix}"

        compute_client = _get_compute_client(creds_dict)
        # Azure Premium SSD supports online resize (no detach required)
        poller = compute_client.disks.begin_update(
            rg,
            data_disk_name,
            {"disk_size_gb": new_size},
        )
        poller.result()

        # Tell troshkad to resize the filesystem if agent is connected
        if host.agent_connected:
            try:
                from app.services.troshkad_client import start_job, wait_for_job

                job = start_job(host, "POST", "/host/resize-storage")
                wait_for_job(host, job["job_id"], timeout=120)
            except Exception:
                logger.warning(
                    "Could not trigger filesystem resize on %s", host.instance_id
                )

        host.storage_size_gb = new_size
        db.commit()
        logger.info(
            "Extended disk %s from %d to %d GB", data_disk_name, old_size, new_size
        )
        return {"old_size_gb": old_size, "new_size_gb": new_size}

    def setup_console(self, provider, base_domain):
        creds_dict = provider.get_credentials()
        rg = provider.azure_resource_group

        dns_client = _get_dns_client(creds_dict)

        # Create or update a DNS zone
        zone = dns_client.zones.create_or_update(
            rg,
            base_domain,
            {"location": "global", "zone_type": "Public"},
        )
        logger.info("Created Azure DNS zone %s", base_domain)

        nameservers = list(zone.name_servers) if zone.name_servers else []

        return {
            "console_base_domain": base_domain,
            "console_zone_id": base_domain,
            "console_nameservers": nameservers,
        }

    def create_console_record(self, provider, host, hostname, ip_address):
        if not provider.console_zone_id:
            logger.warning("No console_zone_id, skipping DNS record creation")
            return

        creds_dict = provider.get_credentials()
        rg = provider.azure_resource_group
        zone_name = provider.console_zone_id

        dns_client = _get_dns_client(creds_dict)

        # Extract record name — strip the zone suffix from the hostname
        # e.g. "abc123.console.example.com" with zone "console.example.com"
        #  -> record_name = "abc123"
        if hostname.endswith("." + zone_name):
            record_name = hostname[: -(len(zone_name) + 1)]
        elif hostname.endswith(zone_name):
            record_name = hostname[: -len(zone_name)].rstrip(".")
        else:
            record_name = hostname

        dns_client.record_sets.create_or_update(
            rg,
            zone_name,
            record_name,
            "A",
            {
                "ttl": 60,
                "arecords": [{"ipv4_address": ip_address}],
            },
        )
        logger.info("DNS: created %s -> %s", hostname, ip_address)

    def delete_console_record(self, provider, host, hostname, ip_address):
        if not provider.console_zone_id:
            return

        creds_dict = provider.get_credentials()
        rg = provider.azure_resource_group
        zone_name = provider.console_zone_id

        dns_client = _get_dns_client(creds_dict)

        if hostname.endswith("." + zone_name):
            record_name = hostname[: -(len(zone_name) + 1)]
        elif hostname.endswith(zone_name):
            record_name = hostname[: -len(zone_name)].rstrip(".")
        else:
            record_name = hostname

        try:
            dns_client.record_sets.delete(rg, zone_name, record_name, "A")
            logger.info("DNS: deleted %s", hostname)
        except Exception as e:
            if _resource_not_found(e):
                logger.info("DNS record %s already gone", hostname)
            else:
                logger.warning("DNS: failed to delete %s (%s)", hostname, e)

    def delete_console(self, provider):
        if not provider.console_zone_id:
            return

        creds_dict = provider.get_credentials()
        rg = provider.azure_resource_group
        zone_name = provider.console_zone_id

        dns_client = _get_dns_client(creds_dict)

        try:
            dns_client.zones.begin_delete(rg, zone_name).result()
            logger.info("Deleted Azure DNS zone %s", zone_name)
        except Exception as e:
            if _resource_not_found(e):
                logger.info("Azure DNS zone %s already gone", zone_name)
            else:
                raise

    def delete_key_pair(self, provider, key_pair_name):
        # Azure doesn't have cloud-managed key pairs — SSH keys are in VM config
        pass

    def allocate_eip(self, provider, host, eip_id):
        creds_dict = provider.get_credentials()
        rg = provider.azure_resource_group
        location = provider.azure_location or provider.default_region

        network_client = _get_network_client(creds_dict)
        ip_name = f"troshka-eip-{eip_id[:12]}"

        poller = network_client.public_ip_addresses.begin_create_or_update(
            rg,
            ip_name,
            {
                "location": location,
                "sku": {"name": "Standard"},
                "public_ip_allocation_method": "Static",
                "tags": {"managed-by": "troshka", "troshka-eip-id": eip_id[:12]},
            },
        )
        pip = poller.result()
        public_ip = pip.ip_address

        logger.info("Allocated static IP %s (%s)", public_ip, ip_name)
        return {"public_ip": public_ip, "allocation_id": ip_name}

    def associate_eip(self, provider, host, allocation_id):
        creds_dict = provider.get_credentials()
        rg = provider.azure_resource_group

        network_client = _get_network_client(creds_dict)

        # Get the public IP resource
        pip = network_client.public_ip_addresses.get(rg, allocation_id)

        # Get the host's NIC
        suffix = host.instance_id.replace("troshka-", "", 1)
        nic_name = f"troshka-nic-{suffix}"
        nic = network_client.network_interfaces.get(rg, nic_name)

        # Add a secondary IP config pointing to this public IP
        secondary_name = f"eip-{allocation_id}"
        # Check if this IP config already exists
        existing_names = {ipc.name for ipc in nic.ip_configurations}
        if secondary_name not in existing_names:
            nic.ip_configurations.append(
                {
                    "name": secondary_name,
                    "subnet": {"id": nic.ip_configurations[0].subnet.id},
                    "public_ip_address": {"id": pip.id},
                    "private_ip_allocation_method": "Dynamic",
                }
            )
            poller = network_client.network_interfaces.begin_create_or_update(
                rg, nic_name, nic.serialize()
            )
            poller.result()

        logger.info(
            "Associated EIP %s with %s via NIC %s",
            allocation_id,
            host.instance_id,
            nic_name,
        )
        return {}

    def release_eip(self, provider, allocation_id, namespace=None):
        creds_dict = provider.get_credentials()
        rg = provider.azure_resource_group

        network_client = _get_network_client(creds_dict)

        _delete_resource(
            f"public IP {allocation_id}",
            lambda: network_client.public_ip_addresses.begin_delete(rg, allocation_id),
        )

    def update_eip_ports(self, provider, host, allocation_id, ports):
        """Update NSG rules for port forwarding on an EIP.

        Azure uses NSG rules rather than per-IP port mappings.
        """
        if not provider.azure_nsg_id:
            logger.debug(
                "No NSG configured, skipping port update for %s", allocation_id
            )
            return

        creds_dict = provider.get_credentials()
        rg = provider.azure_resource_group
        network_client = _get_network_client(creds_dict)

        # Extract NSG name from the full resource ID
        nsg_name = provider.azure_nsg_id.rsplit("/", 1)[-1]

        # Get the EIP's public IP address for use in the rule
        try:
            pip = network_client.public_ip_addresses.get(rg, allocation_id)
            dest_ip = pip.ip_address
        except Exception:
            logger.warning(
                "Could not resolve IP for %s, skipping port update", allocation_id
            )
            return

        # Create/update an inbound allow rule for each port
        # Use a priority base derived from the allocation_id to avoid collisions
        priority_base = 2000 + (hash(allocation_id) % 1000)

        for i, port_spec in enumerate(ports):
            port = port_spec.get("port") or port_spec.get("targetPort")
            if not port:
                continue

            rule_name = f"troshka-eip-{allocation_id[-8:]}-{port}"
            priority = priority_base + i

            try:
                network_client.security_rules.begin_create_or_update(
                    rg,
                    nsg_name,
                    rule_name,
                    {
                        "protocol": "Tcp",
                        "source_address_prefix": "*",
                        "source_port_range": "*",
                        "destination_address_prefix": dest_ip,
                        "destination_port_range": str(port),
                        "access": "Allow",
                        "direction": "Inbound",
                        "priority": priority,
                    },
                ).result()
            except Exception as e:
                logger.warning("Failed to create NSG rule %s: %s", rule_name, e)

        logger.info(
            "Updated NSG rules for EIP %s (%d ports)", allocation_id, len(ports)
        )
