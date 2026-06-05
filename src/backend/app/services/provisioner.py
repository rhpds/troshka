"""
Host provisioning and pool management.

Admins use this to add/remove EC2 hosts to the pool.
The placement service (separate) assigns projects to available hosts.
"""
import logging
import uuid

import boto3

from app.core.config import config

logger = logging.getLogger(__name__)


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


def ensure_security_group(vpc_id: str, name: str = "troshka-host-sg", credentials: dict | None = None) -> str:
    client = _get_ec2_client(credentials=credentials)
    existing = client.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )
    if existing["SecurityGroups"]:
        return existing["SecurityGroups"][0]["GroupId"]

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
            {"IpProtocol": "tcp", "FromPort": 8443, "ToPort": 8443, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "Agent WS"}]},
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


CLOUD_INIT = """#cloud-config
hostname: {hostname}

packages:
  - qemu-kvm
  - libvirt
  - libvirt-devel
  - virt-install
  - python3
  - python3-pip
  - python3-libvirt
  - dnsmasq
  - nftables

runcmd:
  - bash -c 'if systemctl list-unit-files virtqemud.service &>/dev/null; then systemctl enable --now virtqemud.socket virtnetworkd.socket virtstoraged.socket; else systemctl enable --now libvirtd; fi'
  - systemctl enable --now nftables
  - mkdir -p /var/lib/troshka/images /var/lib/troshka/vms /etc/troshka-agent
  - echo "host_id: {host_id}" > /etc/troshka-agent/host-id
"""


def provision_host(
    instance_type: str | None = None,
    ami_id: str | None = None,
    host_id: str | None = None,
    region: str | None = None,
    credentials: dict | None = None,
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
        vpc_id, subnet_id = get_default_vpc_and_subnet(credentials=credentials)

    sg_id = kwargs.get("security_group_id") or getattr(config.aws, "security_group_id", None)
    if not sg_id:
        sg_id = ensure_security_group(vpc_id, credentials=credentials)

    key_name = f"troshka-{host_id[:8]}"
    key_result = client.create_key_pair(KeyName=key_name)
    private_key = key_result.get("KeyMaterial", "")
    logger.info("Created key pair %s", key_name)

    user_data = CLOUD_INIT.format(hostname=hostname, host_id=host_id)

    logger.info("Provisioning host %s (%s, %s)", hostname, instance_type, ami_id)

    response = client.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        KeyName=key_name,
        MinCount=1,
        MaxCount=1,
        CpuOptions={"NestedVirtualization": "enabled"},
        UserData=user_data,
        BlockDeviceMappings=[{
            "DeviceName": "/dev/sda1",
            "Ebs": {"VolumeSize": 50, "VolumeType": "gp3", "DeleteOnTermination": True},
        }],
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
            "SubnetId": subnet_id,
            "Groups": [sg_id],
            "AssociatePublicIpAddress": True,
        }],
    )

    instance_id = response["Instances"][0]["InstanceId"]
    logger.info("Launched %s, waiting for running state", instance_id)

    waiter = client.get_waiter("instance_running")
    waiter.wait(InstanceIds=[instance_id])

    desc = client.describe_instances(InstanceIds=[instance_id])
    inst = desc["Reservations"][0]["Instances"][0]

    # Get instance specs for capacity tracking
    types = client.describe_instance_types(InstanceTypes=[instance_type])
    type_info = types["InstanceTypes"][0] if types["InstanceTypes"] else {}

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
        "key_pair_name": key_name,
        "private_key": private_key,
    }


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
