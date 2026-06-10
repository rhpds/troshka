"""
Host provisioning and pool management.

Admins use this to add/remove EC2 hosts to the pool.
The placement service (separate) assigns projects to available hosts.
"""
import logging
import math
import uuid

import boto3

from app.core.config import config

logger = logging.getLogger(__name__)


def get_public_ip() -> str | None:
    """Discover this backend's public IP via AWS checkip service."""
    import urllib.request
    try:
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=5) as resp:
            return resp.read().decode().strip()
    except Exception:
        logger.warning("Could not determine public IP")
        return None


def _get_ec2_client(region: str | None = None, credentials: dict | None = None):
    creds = credentials or {}
    return boto3.client(
        "ec2",
        region_name=region or config.aws.default_region,
        aws_access_key_id=creds.get("access_key_id") or config.aws.access_key_id or None,
        aws_secret_access_key=creds.get("secret_access_key") or config.aws.secret_access_key or None,
    )


def find_rhel_ami(region: str | None = None) -> str:
    client = _get_ec2_client(region)
    response = client.describe_images(
        Owners=["309956199498"],
        Filters=[
            {"Name": "name", "Values": ["RHEL-9.4*x86_64*Access2-GP3"]},
            {"Name": "state", "Values": ["available"]},
        ],
    )
    images = sorted(response["Images"], key=lambda x: x["CreationDate"])
    if not images:
        raise ValueError("No RHEL 9.4 Access2 AMI found")
    return images[-1]["ImageId"]


def _ensure_troshkad_rule(client, sg_id: str):
    """Ensure the SG has a troshkad port (31337) rule. Idempotent."""
    sg = client.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]
    has_31337 = any(
        p.get("FromPort") == 31337 and p.get("ToPort") == 31337
        for p in sg.get("IpPermissions", [])
    )
    if not has_31337:
        backend_ip = get_public_ip()
        troshkad_cidr = f"{backend_ip}/32" if backend_ip else "0.0.0.0/0"
        try:
            client.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{
                    "IpProtocol": "tcp", "FromPort": 31337, "ToPort": 31337,
                    "IpRanges": [{"CidrIp": troshkad_cidr, "Description": "Troshkad API"}],
                }],
            )
            logger.info("Added troshkad rule (port 31337) to SG %s", sg_id)
        except Exception:
            logger.warning("Failed to add troshkad rule to SG %s", sg_id, exc_info=True)


def ensure_security_group(vpc_id: str, name: str = "troshka-host-sg", credentials: dict | None = None) -> str:
    client = _get_ec2_client(credentials=credentials)
    existing = client.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )
    if existing["SecurityGroups"]:
        sg_id = existing["SecurityGroups"][0]["GroupId"]
        _ensure_troshkad_rule(client, sg_id)
        return sg_id

    backend_ip = get_public_ip()
    troshkad_cidr = f"{backend_ip}/32" if backend_ip else "0.0.0.0/0"

    sg = client.create_security_group(
        GroupName=name,
        Description="Troshka host agent",
        VpcId=vpc_id,
    )
    sg_id = sg["GroupId"]
    client.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}]},
            {"IpProtocol": "tcp", "FromPort": 31337, "ToPort": 31337, "IpRanges": [{"CidrIp": troshkad_cidr, "Description": "Troshkad API"}]},
            {"IpProtocol": "udp", "FromPort": 4789, "ToPort": 4789, "UserIdGroupPairs": [{"GroupId": sg_id, "Description": "VXLAN mesh"}]},
        ],
    )
    client.create_tags(Resources=[sg_id], Tags=[{"Key": "Project", "Value": "troshka"}, {"Key": "ManagedBy", "Value": "troshka"}])
    logger.info("Created security group %s (%s)", name, sg_id)
    return sg_id


def get_default_vpc_and_subnet(credentials: dict | None = None) -> tuple[str, str]:
    client = _get_ec2_client(credentials=credentials)
    vpcs = client.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    if not vpcs["Vpcs"]:
        raise ValueError("No default VPC found")
    vpc_id = vpcs["Vpcs"][0]["VpcId"]
    subnets = client.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    if not subnets["Subnets"]:
        raise ValueError("No subnets in default VPC")
    return vpc_id, subnets["Subnets"][0]["SubnetId"]


def update_sg_troshkad_ip(sg_id: str, new_ip: str, credentials: dict | None = None):
    """Update the troshkad port (31337) SG rule to a new backend IP."""
    client = _get_ec2_client(credentials=credentials)

    # Get current rules
    sg = client.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]

    # Find and revoke the old troshkad rule
    for perm in sg.get("IpPermissions", []):
        if perm.get("FromPort") == 31337 and perm.get("ToPort") == 31337:
            try:
                client.revoke_security_group_ingress(GroupId=sg_id, IpPermissions=[perm])
            except Exception:
                pass
            break

    # Add new rule with updated IP
    client.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": 31337, "ToPort": 31337,
            "IpRanges": [{"CidrIp": f"{new_ip}/32", "Description": "Troshkad API"}],
        }],
    )
    logger.info("Updated SG %s troshkad rule to %s/32", sg_id, new_ip)


CLOUD_INIT = """#cloud-config
hostname: {hostname}

packages:
  - qemu-kvm
  - libvirt
  - libvirt-client
  - virt-install
  - python3
  - python3-pip
  - python3-libvirt
  - dnsmasq
  - nftables
  - nmap-ncat

runcmd:
  - bash -c 'if systemctl list-unit-files virtqemud.service &>/dev/null; then systemctl enable --now virtqemud.socket virtnetworkd.socket virtstoraged.socket; else systemctl enable --now libvirtd; fi'
  - systemctl enable --now nftables
  - systemctl disable --now dnsmasq 2>/dev/null || true
  - dnf install -y nvme-cli 2>/dev/null || true
  - |
    # Detect data volume (/dev/sdf) via nvme id-ctrl — NVMe ordering is not guaranteed
    find_nvme_dev() {{
      local target="$1"
      for dev in /dev/nvme*n1; do
        [ -b "$dev" ] || continue
        DEVNAME=$(nvme id-ctrl "$dev" 2>/dev/null | grep -oE '/dev/sd[a-z]+|sd[a-z]+' | head -1)
        if [ "$DEVNAME" = "$target" ] || [ "$DEVNAME" = "/dev/$target" ]; then
          echo "$dev"; return
        fi
      done
    }}
    # Mount data volume
    DATA_DEV=$(find_nvme_dev sdf)
    if [ -n "$DATA_DEV" ]; then
      blkid "$DATA_DEV" || mkfs.xfs "$DATA_DEV"
      mkdir -p /var/lib/troshka
      mount "$DATA_DEV" /var/lib/troshka
      grep -q /var/lib/troshka /etc/fstab || echo "$DATA_DEV /var/lib/troshka xfs defaults,nofail 0 2" >> /etc/fstab
    fi
    # Mount swap volume
    SWAP_DEV=$(find_nvme_dev sdg)
    if [ -n "$SWAP_DEV" ]; then
      mkswap "$SWAP_DEV" 2>/dev/null || true
      swapon "$SWAP_DEV" 2>/dev/null || true
      grep -q "$SWAP_DEV" /etc/fstab || echo "$SWAP_DEV none swap defaults,nofail 0 0" >> /etc/fstab
    fi
  - mkdir -p /var/lib/troshka/images /var/lib/troshka/vms /var/lib/troshka/tmp /etc/troshka-agent
  - echo "host_id: {host_id}" > /etc/troshka-agent/host-id
  - |
    # Kernel tuning for VM memory overcommit
    sysctl -w vm.overcommit_memory=1 vm.swappiness=10 2>/dev/null || true
    cat > /etc/sysctl.d/99-troshka.conf << EOF2
    vm.overcommit_memory = 1
    vm.swappiness = 10
    EOF2
    # KSM
    echo 1 > /sys/kernel/mm/ksm/run 2>/dev/null || true
    echo 5000 > /sys/kernel/mm/ksm/pages_to_scan 2>/dev/null || true
  - |
    mkdir -p /etc/libvirt/hooks
    cat > /etc/libvirt/hooks/qemu << 'HOOKEOF'
    #!/bin/bash
    DOMAIN=$1
    ACTION=$2
    if [ "$ACTION" = "started" ]; then
        PID=$(echo "$DOMAIN" | sed -n 's/^troshka-\([a-f0-9]*\)-.*/\1/p')
        [ -z "$PID" ] && exit 0
        NS="troshka-$PID"
        ip netns list 2>/dev/null | grep -q "^$NS " || exit 0
        BRIDGE=$(ip netns exec "$NS" ip -o link show type bridge 2>/dev/null | awk -F': ' '{{print $2}}' | head -1)
        [ -z "$BRIDGE" ] && exit 0
        for TAP in $(virsh domiflist "$DOMAIN" 2>/dev/null | awk 'NR>2 && NF>0 {{print $1}}'); do
            ip link set "$TAP" netns "$NS" 2>/dev/null
            ip netns exec "$NS" ip link set "$TAP" master "$BRIDGE" 2>/dev/null
            ip netns exec "$NS" ip link set "$TAP" up 2>/dev/null
        done
    fi
    HOOKEOF
    chmod +x /etc/libvirt/hooks/qemu
"""


def provision_host(
    instance_type: str | None = None,
    ami_id: str | None = None,
    host_id: str | None = None,
    region: str | None = None,
    credentials: dict | None = None,
    storage_size_gb: int = 500,
    **kwargs,
) -> dict:
    """Provision a new EC2 host and add it to the pool. Admin operation."""
    client = _get_ec2_client(region=region, credentials=credentials)

    host_id = host_id or str(uuid.uuid4())
    hostname = f"troshka-host-{host_id[:8]}"
    instance_type = instance_type or config.aws.default_instance_type or "m8i.xlarge"

    if not ami_id:
        ami_id = getattr(config.aws, "default_ami", None) or find_rhel_ami()

    vpc_id = kwargs.get("vpc_id") or getattr(config.aws, "vpc_id", None)
    subnet_id = kwargs.get("subnet_id") or getattr(config.aws, "subnet_id", None)
    if not vpc_id or not subnet_id:
        raise ValueError("VPC and subnet must be configured on the provider — run Setup VPC first")

    sg_id = kwargs.get("security_group_id") or getattr(config.aws, "security_group_id", None)
    if not sg_id:
        sg_id = ensure_security_group(vpc_id, credentials=credentials)

    # Get all subnets in the VPC for AZ fallback
    all_subnets = client.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    subnet_ids = [subnet_id] + [s["SubnetId"] for s in all_subnets["Subnets"] if s["SubnetId"] != subnet_id]

    key_name = f"troshka-{host_id[:8]}"
    key_result = client.create_key_pair(KeyName=key_name)
    private_key = key_result.get("KeyMaterial", "")
    logger.info("Created key pair %s", key_name)

    user_data = CLOUD_INIT.format(hostname=hostname, host_id=host_id)

    # Look up instance specs before launch (need RAM size for swap volume)
    types = client.describe_instance_types(InstanceTypes=[instance_type])
    type_info = types["InstanceTypes"][0] if types["InstanceTypes"] else {}
    total_ram_mb = type_info.get("MemoryInfo", {}).get("SizeInMiB", 0)
    swap_size_gb = max(math.ceil(total_ram_mb / 1024), 1)

    logger.info("Provisioning host %s (%s, %s, swap=%dGB)", hostname, instance_type, ami_id, swap_size_gb)

    # Try each subnet (AZ) until one supports the instance type
    response = None
    last_error = None
    for try_subnet in subnet_ids:
        try:
            response = client.run_instances(
                ImageId=ami_id,
                InstanceType=instance_type,
                KeyName=key_name,
                MinCount=1,
                MaxCount=1,
                CpuOptions={"NestedVirtualization": "enabled"},
                UserData=user_data,
                BlockDeviceMappings=[
                    {
                        "DeviceName": "/dev/sda1",
                        "Ebs": {"VolumeSize": 50, "VolumeType": "gp3", "DeleteOnTermination": True},
                    },
                    {
                        "DeviceName": "/dev/sdf",
                        "Ebs": {"VolumeSize": storage_size_gb, "VolumeType": "gp3", "DeleteOnTermination": True},
                    },
                    {
                        "DeviceName": "/dev/sdg",
                        "Ebs": {"VolumeSize": swap_size_gb, "VolumeType": "gp3", "DeleteOnTermination": True},
                    },
                ],
                TagSpecifications=[{
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": hostname},
                        {"Key": "Project", "Value": "troshka"},
                        {"Key": "ManagedBy", "Value": "troshka"},
                        {"Key": "troshka-host-id", "Value": host_id},
                    ],
                }],
                NetworkInterfaces=[{
                    "DeviceIndex": 0,
                    "SubnetId": try_subnet,
                    "Groups": [sg_id],
                    "AssociatePublicIpAddress": True,
                }],
            )
            break
        except client.exceptions.ClientError as e:
            if "Unsupported" in str(e):
                logger.warning("Instance type %s not supported in subnet %s, trying next AZ", instance_type, try_subnet)
                last_error = e
                continue
            raise
    if not response:
        raise last_error or ValueError(f"Instance type {instance_type} not supported in any AZ")

    instance_id = response["Instances"][0]["InstanceId"]
    logger.info("Launched %s, waiting for running state", instance_id)

    waiter = client.get_waiter("instance_running")
    waiter.wait(InstanceIds=[instance_id])

    desc = client.describe_instances(InstanceIds=[instance_id])
    inst = desc["Reservations"][0]["Instances"][0]

    return {
        "host_id": host_id,
        "instance_id": instance_id,
        "instance_type": instance_type,
        "public_ip": inst.get("PublicIpAddress"),
        "private_ip": inst.get("PrivateIpAddress"),
        "ami_id": ami_id,
        "state": "active",
        "total_vcpus": type_info.get("VCpuInfo", {}).get("DefaultVCpus", 0),
        "total_ram_mb": type_info.get("MemoryInfo", {}).get("SizeInMiB", 0),
        "max_eips": type_info.get("NetworkInfo", {}).get("Ipv4AddressesPerInterface", 1) - 1,
        "key_pair_name": key_name,
        "private_key": private_key,
        "storage_size_gb": storage_size_gb,
    }


def resize_instance(instance_id: str, new_instance_type: str, credentials: dict | None = None) -> dict:
    """Change instance type of a stopped EC2 instance, resize swap volume, and return new specs."""
    client = _get_ec2_client(credentials=credentials)
    client.modify_instance_attribute(
        InstanceId=instance_id,
        InstanceType={"Value": new_instance_type},
    )
    logger.info("Changed %s to %s", instance_id, new_instance_type)

    types = client.describe_instance_types(InstanceTypes=[new_instance_type])
    type_info = types["InstanceTypes"][0] if types["InstanceTypes"] else {}
    new_ram_mb = type_info.get("MemoryInfo", {}).get("SizeInMiB", 0)
    new_swap_gb = max(math.ceil(new_ram_mb / 1024), 1)

    # Resize the swap volume (/dev/sdg) to match the new RAM
    _resize_swap_volume(client, instance_id, new_swap_gb)

    return {
        "instance_type": new_instance_type,
        "total_vcpus": type_info.get("VCpuInfo", {}).get("DefaultVCpus", 0),
        "total_ram_mb": new_ram_mb,
        "max_eips": type_info.get("NetworkInfo", {}).get("Ipv4AddressesPerInterface", 1) - 1,
    }


def _resize_swap_volume(client, instance_id: str, new_size_gb: int):
    """Delete and recreate the swap volume (/dev/sdg) at the new size."""
    volumes = client.describe_volumes(Filters=[
        {"Name": "attachment.instance-id", "Values": [instance_id]},
        {"Name": "attachment.device", "Values": ["/dev/sdg"]},
    ])
    if not volumes["Volumes"]:
        logger.info("No swap volume found on %s — skipping resize", instance_id)
        return

    old_vol = volumes["Volumes"][0]
    old_vol_id = old_vol["VolumeId"]
    old_size = old_vol["Size"]
    az = old_vol["AvailabilityZone"]

    if old_size == new_size_gb:
        logger.info("Swap volume %s already %d GB — no resize needed", old_vol_id, new_size_gb)
        return

    # Detach, delete, create, attach
    client.detach_volume(VolumeId=old_vol_id, InstanceId=instance_id, Device="/dev/sdg")
    waiter = client.get_waiter("volume_available")
    waiter.wait(VolumeIds=[old_vol_id])
    client.delete_volume(VolumeId=old_vol_id)
    logger.info("Deleted old swap volume %s (%d GB)", old_vol_id, old_size)

    new_vol = client.create_volume(
        Size=new_size_gb,
        VolumeType="gp3",
        AvailabilityZone=az,
        TagSpecifications=[{
            "ResourceType": "volume",
            "Tags": [
                {"Key": "Name", "Value": f"troshka-swap-{instance_id}"},
                {"Key": "Project", "Value": "troshka"},
                {"Key": "ManagedBy", "Value": "troshka"},
                {"Key": "troshka-role", "Value": "swap"},
            ],
        }],
    )
    new_vol_id = new_vol["VolumeId"]
    waiter.wait(VolumeIds=[new_vol_id])
    client.attach_volume(VolumeId=new_vol_id, InstanceId=instance_id, Device="/dev/sdg")
    logger.info("Created new swap volume %s (%d GB) for %s", new_vol_id, new_size_gb, instance_id)


def terminate_host(instance_id: str, credentials: dict | None = None):
    """Remove a host from the pool and terminate the EC2 instance."""
    client = _get_ec2_client(credentials=credentials)
    client.terminate_instances(InstanceIds=[instance_id])
    logger.info("Terminated %s", instance_id)


def get_host_status(instance_id: str, credentials: dict | None = None) -> dict | None:
    """Get current status of a host instance."""
    client = _get_ec2_client(credentials=credentials)
    try:
        desc = client.describe_instances(InstanceIds=[instance_id])
        inst = desc["Reservations"][0]["Instances"][0]
        return {
            "instance_id": instance_id,
            "state": inst["State"]["Name"],
            "public_ip": inst.get("PublicIpAddress"),
            "private_ip": inst.get("PrivateIpAddress"),
        }
    except Exception:
        return None
