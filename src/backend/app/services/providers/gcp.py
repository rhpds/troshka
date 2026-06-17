"""GCP provider driver.

Provisions Compute Engine instances with nested virtualization,
manages Cloud DNS, static IPs, and persistent disks.
Self-contained with helper functions (same pattern as OCPVirt driver).
"""

import logging
import time

from app.services.providers.base import ProviderDriver

logger = logging.getLogger(__name__)

GCP_DEFAULT_INSTANCE_TYPE = "n2-highmem-32"
GCP_CURATED_INSTANCE_TYPES = [
    "n2-highmem-4",
    "n2-highmem-8",
    "n2-highmem-16",
    "n2-highmem-32",
    "n2-highmem-48",
    "n2-highmem-64",
    "n2-highmem-80",
]
GCP_RAM_PER_VCPU_GB = {
    "n2-highmem-4": 32,
    "n2-highmem-8": 64,
    "n2-highmem-16": 128,
    "n2-highmem-32": 256,
    "n2-highmem-48": 384,
    "n2-highmem-64": 512,
    "n2-highmem-80": 640,
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
    DATA_DEV=/dev/sdb
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
  - semanage fcontext -a -t virt_image_t '/var/lib/troshka(/.*)?' 2>/dev/null || true
  - restorecon -R /var/lib/troshka
  - 'echo "host_id: {host_id}" > /etc/troshka-agent/host-id'
  - firewall-cmd --add-port=31337/tcp --add-port=443/tcp --permanent 2>/dev/null || true
  - firewall-cmd --reload 2>/dev/null || true
"""


def _get_credentials(creds_dict):
    """Build google.oauth2.service_account.Credentials from stored creds."""
    from google.oauth2 import service_account

    sa_json = creds_dict.get("service_account_json")
    if isinstance(sa_json, str):
        import json

        sa_json = json.loads(sa_json)
    return service_account.Credentials.from_service_account_info(sa_json)


def _get_compute_client(creds):
    from google.cloud import compute_v1

    return compute_v1.InstancesClient(credentials=creds)


def _get_disks_client(creds):
    from google.cloud import compute_v1

    return compute_v1.DisksClient(credentials=creds)


def _get_addresses_client(creds):
    from google.cloud import compute_v1

    return compute_v1.AddressesClient(credentials=creds)


def _get_images_client(creds):
    from google.cloud import compute_v1

    return compute_v1.ImagesClient(credentials=creds)


def _parse_instance_type(instance_type):
    """Parse 'n2-highmem-32' into (vcpus, ram_mb)."""
    if not instance_type:
        instance_type = GCP_DEFAULT_INSTANCE_TYPE

    ram_gb = GCP_RAM_PER_VCPU_GB.get(instance_type)
    if ram_gb:
        # Extract vCPU count from the last segment
        parts = instance_type.rsplit("-", 1)
        try:
            vcpus = int(parts[-1])
        except (ValueError, IndexError):
            vcpus = 32
        return vcpus, ram_gb * 1024

    # Fallback: try parsing the last segment as vCPU count
    parts = instance_type.rsplit("-", 1)
    try:
        vcpus = int(parts[-1])
    except (ValueError, IndexError):
        vcpus = 32
    return vcpus, vcpus * 8 * 1024  # assume 8 GB/vCPU default


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


def _zone_to_region(zone):
    """Extract region from zone, e.g. 'us-central1-a' -> 'us-central1'."""
    return zone.rsplit("-", 1)[0]


def _wait_for_operation(operation, project, zone=None, region=None, creds=None):
    """Wait for a GCP operation to complete."""
    from google.cloud import compute_v1

    if zone:
        client = compute_v1.ZoneOperationsClient(credentials=creds)
        while True:
            result = client.get(project=project, zone=zone, operation=operation.name)
            if result.status == compute_v1.Operation.Status.DONE:
                if result.error:
                    errors = [e.message for e in result.error.errors]
                    raise RuntimeError(f"GCP operation failed: {'; '.join(errors)}")
                return result
            time.sleep(2)
    elif region:
        client = compute_v1.RegionOperationsClient(credentials=creds)
        while True:
            result = client.get(
                project=project, region=region, operation=operation.name
            )
            if result.status == compute_v1.Operation.Status.DONE:
                if result.error:
                    errors = [e.message for e in result.error.errors]
                    raise RuntimeError(f"GCP operation failed: {'; '.join(errors)}")
                return result
            time.sleep(2)
    else:
        client = compute_v1.GlobalOperationsClient(credentials=creds)
        while True:
            result = client.get(project=project, operation=operation.name)
            if result.status == compute_v1.Operation.Status.DONE:
                if result.error:
                    errors = [e.message for e in result.error.errors]
                    raise RuntimeError(f"GCP operation failed: {'; '.join(errors)}")
                return result
            time.sleep(2)


class GCPDriver(ProviderDriver):
    def provision_host(
        self, provider, host_id, instance_type, storage_size_gb, **kwargs
    ):
        from google.cloud import compute_v1

        creds_dict = provider.get_credentials()
        creds = _get_credentials(creds_dict)
        project = provider.gcp_project_id
        zone = provider.gcp_zone
        region = _zone_to_region(zone)

        instance_type = instance_type or GCP_DEFAULT_INSTANCE_TYPE
        vcpus, ram_mb = _parse_instance_type(instance_type)
        instance_name = f"troshka-{host_id[:12]}"
        data_disk_name = f"troshka-data-{host_id[:12]}"

        private_key, public_key = _generate_ssh_keypair()

        # Build cloud-init
        host_type = kwargs.get("host_type", "shared")
        nfs_server = kwargs.get("nfs_server")
        nfs_path = kwargs.get("nfs_path")
        user_data = _build_cloud_init(host_id, nfs_server, nfs_path)

        # Resolve boot image
        image_id = kwargs.get("image_id") or provider.default_image
        if not image_id:
            raise ValueError("No boot image specified — set image_id or default_image")

        if image_id.startswith("https://"):
            from urllib.parse import urlparse

            parsed = urlparse(image_id)
            if parsed.hostname not in (
                "compute.googleapis.com",
                "www.googleapis.com",
            ):
                raise ValueError(f"Untrusted image URL host: {parsed.hostname!r}")
            boot_image_url = image_id
        elif image_id.startswith("projects/"):
            boot_image_url = f"https://compute.googleapis.com/compute/v1/{image_id}"
        else:
            boot_image_url = (
                f"https://compute.googleapis.com/compute/v1/"
                f"projects/{project}/global/images/{image_id}"
            )

        # SSH key metadata (GCP uses metadata for SSH keys)
        ssh_keys_value = f"troshka:{public_key} troshka"

        # Create the data disk first
        disks_client = _get_disks_client(creds)
        data_disk = compute_v1.Disk(
            name=data_disk_name,
            size_gb=storage_size_gb,
            type_=f"zones/{zone}/diskTypes/pd-ssd",
            labels={"managed-by": "troshka", "troshka-host-id": host_id[:12]},
        )
        op = disks_client.insert(project=project, zone=zone, disk_resource=data_disk)
        _wait_for_operation(op, project, zone=zone, creds=creds)
        logger.info("Created data disk %s (%d GB)", data_disk_name, storage_size_gb)

        # Build instance config
        network_interface = compute_v1.NetworkInterface(
            subnetwork=provider.gcp_subnet_id,
            access_configs=[
                compute_v1.AccessConfig(
                    name="External NAT",
                    type_="ONE_TO_ONE_NAT",
                    network_tier="PREMIUM",
                )
            ],
        )

        instance = compute_v1.Instance(
            name=instance_name,
            machine_type=f"zones/{zone}/machineTypes/{instance_type}",
            disks=[
                # Boot disk
                compute_v1.AttachedDisk(
                    auto_delete=True,
                    boot=True,
                    initialize_params=compute_v1.AttachedDiskInitializeParams(
                        disk_size_gb=50,
                        disk_type=f"zones/{zone}/diskTypes/pd-ssd",
                        source_image=boot_image_url,
                    ),
                ),
                # Data disk
                compute_v1.AttachedDisk(
                    auto_delete=False,
                    boot=False,
                    source=f"zones/{zone}/disks/{data_disk_name}",
                ),
            ],
            network_interfaces=[network_interface],
            metadata=compute_v1.Metadata(
                items=[
                    compute_v1.Items(key="ssh-keys", value=ssh_keys_value),
                    compute_v1.Items(key="user-data", value=user_data),
                ]
            ),
            tags=compute_v1.Tags(items=["troshka-host"]),
            labels={
                "managed-by": "troshka",
                "troshka-host-id": host_id[:12],
            },
            advanced_machine_features=compute_v1.AdvancedMachineFeatures(
                enable_nested_virtualization=host_type != "pattern_buffer",
            ),
            scheduling=compute_v1.Scheduling(
                on_host_maintenance=(
                    "TERMINATE" if host_type != "pattern_buffer" else "MIGRATE"
                ),
            ),
        )

        compute_client = _get_compute_client(creds)
        op = compute_client.insert(
            project=project, zone=zone, instance_resource=instance
        )
        _wait_for_operation(op, project, zone=zone, creds=creds)
        logger.info("Created instance %s in %s", instance_name, zone)

        # Poll until RUNNING and get IPs
        public_ip = None
        private_ip = None
        for attempt in range(60):
            time.sleep(5)
            inst = compute_client.get(
                project=project, zone=zone, instance=instance_name
            )
            if inst.status == "RUNNING":
                for iface in inst.network_interfaces:
                    private_ip = iface.network_i_p
                    if iface.access_configs:
                        public_ip = iface.access_configs[0].nat_i_p
                break
        else:
            raise RuntimeError(
                f"Instance {instance_name} did not reach RUNNING within 5 minutes"
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
        creds = _get_credentials(creds_dict)
        project = provider.gcp_project_id
        zone = provider.gcp_zone

        compute_client = _get_compute_client(creds)

        # Delete the instance
        try:
            op = compute_client.delete(project=project, zone=zone, instance=instance_id)
            _wait_for_operation(op, project, zone=zone, creds=creds)
        except Exception as e:
            if "was not found" in str(e) or "notFound" in str(e):
                logger.info("Instance %s already gone", instance_id)
            else:
                raise

        # Delete data disk (derive name from instance name)
        data_disk_name = instance_id.replace("troshka-", "troshka-data-", 1)
        disks_client = _get_disks_client(creds)
        try:
            op = disks_client.delete(project=project, zone=zone, disk=data_disk_name)
            _wait_for_operation(op, project, zone=zone, creds=creds)
        except Exception as e:
            if "was not found" in str(e) or "notFound" in str(e):
                logger.info("Data disk %s already gone", data_disk_name)
            else:
                raise

        logger.info("Terminated GCP host %s", instance_id)

    def get_host_status(self, provider, instance_id):
        creds_dict = provider.get_credentials()
        creds = _get_credentials(creds_dict)
        project = provider.gcp_project_id
        zone = provider.gcp_zone

        compute_client = _get_compute_client(creds)
        try:
            inst = compute_client.get(project=project, zone=zone, instance=instance_id)
        except Exception as e:
            if "was not found" in str(e) or "notFound" in str(e):
                return None
            raise

        state_map = {
            "RUNNING": "running",
            "TERMINATED": "stopped",
            "STOPPED": "stopped",
            "SUSPENDED": "stopped",
            "STAGING": "pending",
            "PROVISIONING": "pending",
            "STOPPING": "stopping",
            "SUSPENDING": "stopping",
        }

        public_ip = None
        private_ip = None
        for iface in inst.network_interfaces:
            private_ip = iface.network_i_p
            if iface.access_configs:
                public_ip = iface.access_configs[0].nat_i_p

        return {
            "instance_id": instance_id,
            "state": state_map.get(inst.status, "unknown"),
            "public_ip": public_ip,
            "private_ip": private_ip,
        }

    def get_host_powerstate(self, provider, instance_id):
        status = self.get_host_status(provider, instance_id)
        return status["state"] if status else "unknown"

    def start_host(self, provider, instance_id):
        creds_dict = provider.get_credentials()
        creds = _get_credentials(creds_dict)
        project = provider.gcp_project_id
        zone = provider.gcp_zone

        compute_client = _get_compute_client(creds)
        op = compute_client.start(project=project, zone=zone, instance=instance_id)
        _wait_for_operation(op, project, zone=zone, creds=creds)
        logger.info("Started GCP instance %s", instance_id)

    def stop_host(self, provider, instance_id):
        creds_dict = provider.get_credentials()
        creds = _get_credentials(creds_dict)
        project = provider.gcp_project_id
        zone = provider.gcp_zone

        compute_client = _get_compute_client(creds)
        op = compute_client.stop(project=project, zone=zone, instance=instance_id)
        _wait_for_operation(op, project, zone=zone, creds=creds)
        logger.info("Stopped GCP instance %s", instance_id)

    def resize_host(self, provider, instance_id, new_instance_type):
        creds_dict = provider.get_credentials()
        creds = _get_credentials(creds_dict)
        project = provider.gcp_project_id
        zone = provider.gcp_zone

        compute_client = _get_compute_client(creds)

        # Stop the instance first
        op = compute_client.stop(project=project, zone=zone, instance=instance_id)
        _wait_for_operation(op, project, zone=zone, creds=creds)

        # Poll until actually stopped (up to 5 minutes)
        for _ in range(60):
            inst = compute_client.get(project=project, zone=zone, instance=instance_id)
            if inst.status == "TERMINATED":
                break
            time.sleep(5)
        else:
            raise RuntimeError(f"Instance {instance_id} did not stop within 5 minutes")

        # Change machine type
        from google.cloud import compute_v1

        op = compute_client.set_machine_type(
            project=project,
            zone=zone,
            instance=instance_id,
            instances_set_machine_type_request_resource=(
                compute_v1.InstancesSetMachineTypeRequest(
                    machine_type=f"zones/{zone}/machineTypes/{new_instance_type}"
                )
            ),
        )
        _wait_for_operation(op, project, zone=zone, creds=creds)

        # Start instance
        op = compute_client.start(project=project, zone=zone, instance=instance_id)
        _wait_for_operation(op, project, zone=zone, creds=creds)

        vcpus, ram_mb = _parse_instance_type(new_instance_type)
        logger.info("Resized %s to %s", instance_id, new_instance_type)
        return {
            "instance_type": new_instance_type,
            "total_vcpus": vcpus,
            "total_ram_mb": ram_mb,
        }

    def extend_host_storage(self, provider, host, db, increment_gb=None):
        creds_dict = provider.get_credentials()
        creds = _get_credentials(creds_dict)
        project = provider.gcp_project_id
        zone = provider.gcp_zone

        increment = increment_gb or host.auto_extend_increment_gb
        old_size = host.storage_size_gb
        new_size = old_size + increment
        if host.auto_extend_max_gb:
            new_size = min(new_size, host.auto_extend_max_gb)
        if new_size <= old_size:
            raise ValueError(f"Cannot extend: already at max ({old_size} GB)")

        data_disk_name = host.instance_id.replace("troshka-", "troshka-data-", 1)
        disks_client = _get_disks_client(creds)

        from google.cloud import compute_v1

        op = disks_client.resize(
            project=project,
            zone=zone,
            disk=data_disk_name,
            disks_resize_request_resource=compute_v1.DisksResizeRequest(
                size_gb=new_size,
            ),
        )
        _wait_for_operation(op, project, zone=zone, creds=creds)

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
        from google.cloud import dns

        creds_dict = provider.get_credentials()
        creds = _get_credentials(creds_dict)
        project = provider.gcp_project_id

        dns_client = dns.Client(project=project, credentials=creds)
        zone_name = base_domain.rstrip(".").replace(".", "-")
        dns_name = base_domain if base_domain.endswith(".") else f"{base_domain}."

        zone = dns_client.zone(zone_name, dns_name)
        if not zone.exists():
            zone.create()
            logger.info("Created Cloud DNS zone %s for %s", zone_name, base_domain)

        # Reload to get nameservers
        zone.reload()
        nameservers = list(zone.name_servers) if zone.name_servers else []

        return {
            "console_base_domain": base_domain,
            "console_zone_id": zone_name,
            "console_nameservers": nameservers,
        }

    def create_console_record(self, provider, host, hostname, ip_address):
        from google.cloud import dns

        if not provider.console_zone_id:
            logger.warning("No console_zone_id, skipping DNS record creation")
            return

        creds_dict = provider.get_credentials()
        creds = _get_credentials(creds_dict)
        project = provider.gcp_project_id

        dns_client = dns.Client(project=project, credentials=creds)
        zone = dns_client.zone(provider.console_zone_id)

        fqdn = hostname if hostname.endswith(".") else f"{hostname}."
        record_set = zone.resource_record_set(fqdn, "A", 60, [ip_address])

        # Delete existing record if present, then add
        changes = zone.changes()
        try:
            existing = [
                rs
                for rs in zone.list_resource_record_sets()
                if rs.name == fqdn and rs.record_type == "A"
            ]
            for rs in existing:
                changes.delete_record_set(rs)
        except Exception:
            pass
        changes.add_record_set(record_set)
        changes.create()
        logger.info("DNS: created %s -> %s", hostname, ip_address)

    def delete_console_record(self, provider, host, hostname, ip_address):
        from google.cloud import dns

        if not provider.console_zone_id:
            return

        creds_dict = provider.get_credentials()
        creds = _get_credentials(creds_dict)
        project = provider.gcp_project_id

        dns_client = dns.Client(project=project, credentials=creds)
        zone = dns_client.zone(provider.console_zone_id)

        fqdn = hostname if hostname.endswith(".") else f"{hostname}."
        try:
            existing = [
                rs
                for rs in zone.list_resource_record_sets()
                if rs.name == fqdn and rs.record_type == "A"
            ]
            if existing:
                changes = zone.changes()
                for rs in existing:
                    changes.delete_record_set(rs)
                changes.create()
                logger.info("DNS: deleted %s", hostname)
        except Exception:
            logger.warning("DNS: failed to delete %s (may already be gone)", hostname)

    def delete_console(self, provider):
        from google.cloud import dns

        if not provider.console_zone_id:
            return

        creds_dict = provider.get_credentials()
        creds = _get_credentials(creds_dict)
        project = provider.gcp_project_id

        dns_client = dns.Client(project=project, credentials=creds)
        zone = dns_client.zone(provider.console_zone_id)

        try:
            if not zone.exists():
                return

            # Delete all non-NS/SOA records first (GCP requires this)
            changes = zone.changes()
            has_deletions = False
            for rs in zone.list_resource_record_sets():
                if rs.record_type not in ("NS", "SOA"):
                    changes.delete_record_set(rs)
                    has_deletions = True
            if has_deletions:
                changes.create()
                # Wait for changes to propagate
                time.sleep(2)

            zone.delete()
            logger.info("Deleted Cloud DNS zone %s", provider.console_zone_id)
        except Exception as e:
            if "was not found" in str(e) or "notFound" in str(e):
                logger.info("Cloud DNS zone %s already gone", provider.console_zone_id)
            else:
                raise

    def delete_key_pair(self, provider, key_pair_name):
        # GCP doesn't have cloud-managed key pairs — SSH keys are in metadata
        pass

    def allocate_eip(self, provider, host, eip_id):
        from google.cloud import compute_v1

        creds_dict = provider.get_credentials()
        creds = _get_credentials(creds_dict)
        project = provider.gcp_project_id
        zone = provider.gcp_zone
        region = _zone_to_region(zone)

        addresses_client = _get_addresses_client(creds)
        address_name = f"troshka-eip-{eip_id[:12]}"

        address = compute_v1.Address(
            name=address_name,
            address_type="EXTERNAL",
            network_tier="PREMIUM",
            labels={
                "managed-by": "troshka",
                "troshka-eip-id": eip_id[:12],
            },
        )
        op = addresses_client.insert(
            project=project, region=region, address_resource=address
        )
        _wait_for_operation(op, project, region=region, creds=creds)

        # Get the allocated IP
        addr = addresses_client.get(
            project=project, region=region, address=address_name
        )
        public_ip = addr.address

        logger.info("Allocated static IP %s (%s)", public_ip, address_name)
        return {"public_ip": public_ip, "allocation_id": address_name}

    def associate_eip(self, provider, host, allocation_id):
        from google.cloud import compute_v1

        creds_dict = provider.get_credentials()
        creds = _get_credentials(creds_dict)
        project = provider.gcp_project_id
        zone = provider.gcp_zone
        region = _zone_to_region(zone)

        addresses_client = _get_addresses_client(creds)
        compute_client = _get_compute_client(creds)

        # Get the static IP address value
        addr = addresses_client.get(
            project=project, region=region, address=allocation_id
        )
        static_ip = addr.address

        # Remove existing access config on nic0
        try:
            op = compute_client.delete_access_config(
                project=project,
                zone=zone,
                instance=host.instance_id,
                access_config="External NAT",
                network_interface="nic0",
            )
            _wait_for_operation(op, project, zone=zone, creds=creds)
        except Exception as e:
            if "was not found" not in str(e) and "notFound" not in str(e):
                raise

        # Add new access config with the static IP
        op = compute_client.add_access_config(
            project=project,
            zone=zone,
            instance=host.instance_id,
            network_interface="nic0",
            access_config_resource=compute_v1.AccessConfig(
                name="External NAT",
                type_="ONE_TO_ONE_NAT",
                nat_i_p=static_ip,
                network_tier="PREMIUM",
            ),
        )
        _wait_for_operation(op, project, zone=zone, creds=creds)
        logger.info("Associated static IP %s with %s", static_ip, host.instance_id)
        return {}

    def release_eip(self, provider, allocation_id, namespace=None):
        creds_dict = provider.get_credentials()
        creds = _get_credentials(creds_dict)
        project = provider.gcp_project_id
        zone = provider.gcp_zone
        region = _zone_to_region(zone)

        addresses_client = _get_addresses_client(creds)
        try:
            op = addresses_client.delete(
                project=project, region=region, address=allocation_id
            )
            _wait_for_operation(op, project, region=region, creds=creds)
            logger.info("Released static IP %s", allocation_id)
        except Exception as e:
            if "was not found" in str(e) or "notFound" in str(e):
                logger.info("Static IP %s already gone", allocation_id)
            else:
                raise

    def update_eip_ports(self, provider, host, allocation_id, ports):
        # GCP uses firewall rules for port control, not per-EIP configuration
        pass
