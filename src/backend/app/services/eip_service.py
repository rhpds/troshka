"""EIP lifecycle management — allocate, associate, disassociate, release."""
import logging
import boto3
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.elastic_ip import ElasticIp
from app.models.provider import Provider

logger = logging.getLogger(__name__)


def _get_ec2_client(provider: Provider):
    """Create boto3 EC2 client from provider credentials."""
    creds = provider.get_credentials()
    return boto3.client(
        "ec2",
        region_name=provider.default_region,
        aws_access_key_id=creds.get("access_key_id"),
        aws_secret_access_key=creds.get("secret_access_key"),
    )


def _get_primary_eni(ec2, instance_id: str) -> str:
    """Get the primary ENI (device index 0) of an instance."""
    desc = ec2.describe_instances(InstanceIds=[instance_id])
    for eni in desc["Reservations"][0]["Instances"][0]["NetworkInterfaces"]:
        if eni["Attachment"]["DeviceIndex"] == 0:
            return eni["NetworkInterfaceId"]
    raise ValueError(f"No primary ENI found for {instance_id}")


def allocate_eip(
    db: Session, provider: Provider, project_id: str, canvas_eip_id: str
) -> ElasticIp:
    """
    Allocate a new EIP from AWS.

    Args:
        db: Database session
        provider: AWS provider with credentials
        project_id: Troshka project ID
        canvas_eip_id: Canvas node ID for this EIP

    Returns:
        ElasticIp database object with state="allocated"
    """
    ec2 = _get_ec2_client(provider)

    # Allocate the EIP
    response = ec2.allocate_address(Domain="vpc")
    allocation_id = response["AllocationId"]
    public_ip = response["PublicIp"]

    logger.info(
        f"Allocated EIP {public_ip} ({allocation_id}) for project {project_id[:8]}"
    )

    # Tag the EIP
    tags = [
        {"Key": "ManagedBy", "Value": "troshka"},
        {"Key": "troshka-provider-id", "Value": provider.id},
        {"Key": "troshka-project-id", "Value": project_id},
        {"Key": "troshka-canvas-eip-id", "Value": canvas_eip_id},
    ]
    ec2.create_tags(Resources=[allocation_id], Tags=tags)

    # Create DB row
    eip = ElasticIp(
        provider_id=provider.id,
        project_id=project_id,
        canvas_eip_id=canvas_eip_id,
        allocation_id=allocation_id,
        public_ip=public_ip,
        state="allocated",
        tags={t["Key"]: t["Value"] for t in tags},
    )
    db.add(eip)
    db.commit()
    db.refresh(eip)

    return eip


def associate_eip(db: Session, eip: ElasticIp, host) -> None:
    """
    Associate an EIP with a host.

    Assigns a secondary private IP to the host's primary ENI, associates the EIP
    to that private IP, and configures the IP on the host via SSH.

    Args:
        db: Database session
        eip: ElasticIp to associate (must be state="allocated")
        host: Host object with instance_id, ip_address, private_key
    """
    # Look up provider
    provider = db.query(Provider).filter_by(id=eip.provider_id).first()
    if not provider:
        raise ValueError(f"Provider {eip.provider_id} not found")

    ec2 = _get_ec2_client(provider)

    # Get primary ENI
    eni_id = _get_primary_eni(ec2, host.instance_id)
    logger.info(f"Primary ENI for {host.instance_id}: {eni_id}")

    # Assign secondary private IP
    assign_resp = ec2.assign_private_ip_addresses(
        NetworkInterfaceId=eni_id, SecondaryPrivateIpAddressCount=1
    )
    private_ip = assign_resp["AssignedPrivateIpAddresses"][0]["PrivateIpAddress"]
    logger.info(f"Assigned private IP {private_ip} to ENI {eni_id}")

    # Associate EIP to the private IP
    assoc_resp = ec2.associate_address(
        AllocationId=eip.allocation_id,
        NetworkInterfaceId=eni_id,
        PrivateIpAddress=private_ip,
    )
    association_id = assoc_resp["AssociationId"]
    logger.info(f"Associated EIP {eip.public_ip} to {private_ip} ({association_id})")

    # Update DB
    eip.private_ip = private_ip
    eip.host_id = host.id
    eip.association_id = association_id
    eip.state = "associated"
    db.commit()

    logger.info(
        f"EIP {eip.public_ip} associated to host {host.id[:8]} "
        f"with private IP {private_ip}"
    )


def disassociate_eip(db: Session, eip: ElasticIp, host) -> None:
    """
    Disassociate an EIP from a host.

    Disassociates the EIP, unassigns the private IP from the ENI, and removes
    the IP from the host via SSH.

    Args:
        db: Database session
        eip: ElasticIp to disassociate (must be state="associated")
        host: Host object the EIP is currently associated with
    """
    if eip.state != "associated":
        logger.warning(f"EIP {eip.id} is not associated, skipping disassociation")
        return

    # Look up provider
    provider = db.query(Provider).filter_by(id=eip.provider_id).first()
    if not provider:
        raise ValueError(f"Provider {eip.provider_id} not found")

    ec2 = _get_ec2_client(provider)

    # Disassociate EIP
    if eip.association_id:
        ec2.disassociate_address(AssociationId=eip.association_id)
        logger.info(
            f"Disassociated EIP {eip.public_ip} (association {eip.association_id})"
        )

    # Unassign private IP from ENI
    if eip.private_ip:
        eni_id = _get_primary_eni(ec2, host.instance_id)
        ec2.unassign_private_ip_addresses(
            NetworkInterfaceId=eni_id, PrivateIpAddresses=[eip.private_ip]
        )
        logger.info(f"Unassigned private IP {eip.private_ip} from ENI {eni_id}")

    # Update DB
    eip.private_ip = None
    eip.host_id = None
    eip.association_id = None
    eip.state = "allocated"
    db.commit()

    logger.info(f"EIP {eip.public_ip} disassociated, returned to allocated state")


def release_eip(db: Session, eip: ElasticIp) -> None:
    """
    Release an EIP back to AWS.

    If the EIP is associated, it will be disassociated first.

    Args:
        db: Database session
        eip: ElasticIp to release
    """
    # Disassociate if needed
    if eip.state == "associated" and eip.host_id:
        from app.models.host import Host
        host = db.query(Host).filter_by(id=eip.host_id).first()
        if host:
            disassociate_eip(db, eip, host)

    # Look up provider
    provider = db.query(Provider).filter_by(id=eip.provider_id).first()
    if not provider:
        raise ValueError(f"Provider {eip.provider_id} not found")

    ec2 = _get_ec2_client(provider)

    # Release the EIP
    ec2.release_address(AllocationId=eip.allocation_id)
    logger.info(f"Released EIP {eip.public_ip} ({eip.allocation_id})")

    # Delete DB row
    db.delete(eip)
    db.commit()


def migrate_eip(db: Session, eip: ElasticIp, from_host, to_host) -> None:
    """
    Migrate an EIP from one host to another.

    Disassociates from the old host and associates to the new host.

    Args:
        db: Database session
        eip: ElasticIp to migrate (must be state="associated")
        from_host: Current host the EIP is associated with
        to_host: Target host to associate the EIP with
    """
    logger.info(
        f"Migrating EIP {eip.public_ip} from host {from_host.id[:8]} "
        f"to host {to_host.id[:8]}"
    )

    disassociate_eip(db, eip, from_host)
    associate_eip(db, eip, to_host)

    logger.info(f"EIP {eip.public_ip} migration complete")


def get_host_eip_usage(db: Session, host_id: str) -> int:
    """
    Get the number of EIPs currently associated with a host.

    Args:
        db: Database session
        host_id: Host UUID

    Returns:
        Count of EIPs with state="associated" for this host
    """
    return (
        db.query(func.count(ElasticIp.id))
        .filter(ElasticIp.host_id == host_id, ElasticIp.state == "associated")
        .scalar()
    )


def sync_security_group_rules(db: Session, provider, desired_rules: list[dict]) -> dict:
    """Reconcile SG ingress rules. Only touches rules with 'troshka-pf:' description prefix."""
    if not provider.security_group_id:
        return {"added": 0, "removed": 0, "error": "No security group configured"}

    ec2 = _get_ec2_client(provider)
    sg_id = provider.security_group_id

    sg = ec2.describe_security_groups(GroupIds=[sg_id])
    current_perms = sg["SecurityGroups"][0]["IpPermissions"]

    current_pf_rules = {}
    for perm in current_perms:
        for ip_range in perm.get("IpRanges", []):
            desc = ip_range.get("Description", "")
            if desc.startswith("troshka-pf:"):
                key = f"{perm['IpProtocol']}:{perm['FromPort']}"
                current_pf_rules[key] = {
                    "protocol": perm["IpProtocol"],
                    "port": perm["FromPort"],
                    "description": desc,
                }

    desired_set = {}
    for rule in desired_rules:
        key = f"{rule.get('protocol', 'tcp')}:{rule['ext_port']}"
        desired_set[key] = {
            "protocol": rule.get("protocol", "tcp"),
            "port": rule["ext_port"],
            "description": f"troshka-pf:{rule['project_id']}:{rule['ext_port']}",
        }

    to_add = {k: v for k, v in desired_set.items() if k not in current_pf_rules}
    to_remove = {k: v for k, v in current_pf_rules.items() if k not in desired_set}

    if to_add:
        try:
            ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{
                    "IpProtocol": r["protocol"],
                    "FromPort": r["port"],
                    "ToPort": r["port"],
                    "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": r["description"]}],
                } for r in to_add.values()],
            )
        except Exception as e:
            if "InvalidPermission.Duplicate" not in str(e):
                raise

    if to_remove:
        ec2.revoke_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{
                "IpProtocol": r["protocol"],
                "FromPort": r["port"],
                "ToPort": r["port"],
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": r["description"]}],
            } for r in to_remove.values()],
        )

    added = len(to_add)
    removed = len(to_remove)
    if added or removed:
        logger.info("SG %s sync: +%d -%d rules", sg_id, added, removed)
    return {"added": added, "removed": removed}
