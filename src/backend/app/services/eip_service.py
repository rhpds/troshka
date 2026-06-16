"""EIP lifecycle management — allocate, associate, disassociate, release.

Dispatches cloud-specific operations through the ProviderDriver interface.
No cloud SDK imports in this module.
"""

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.elastic_ip import ElasticIp
from app.models.provider import Provider
from app.services.providers import get_provider_driver

logger = logging.getLogger(__name__)

TRANSIT_PORT_START = 40000
TRANSIT_PORT_END = 49999


def allocate_eip(
    db: Session, provider: Provider, project_id: str, canvas_eip_id: str, host
) -> ElasticIp:
    """Allocate a new EIP via the provider driver."""
    import uuid

    eip_id = str(uuid.uuid4())
    driver = get_provider_driver(provider)
    result = driver.allocate_eip(provider, host, eip_id)

    eip = ElasticIp(
        id=eip_id,
        provider_id=provider.id,
        project_id=project_id,
        canvas_eip_id=canvas_eip_id,
        allocation_id=result["allocation_id"],
        public_ip=result["public_ip"],
        state="allocated",
    )
    db.add(eip)
    db.commit()
    db.refresh(eip)

    logger.info(
        "Allocated EIP %s (%s) for project %s",
        eip.public_ip,
        eip.allocation_id,
        project_id[:8],
    )
    return eip


def associate_eip(db: Session, eip: ElasticIp, host) -> None:
    """Associate an EIP with a host via the provider driver."""
    provider = db.query(Provider).filter_by(id=eip.provider_id).first()
    if not provider:
        raise ValueError(f"Provider {eip.provider_id} not found")

    driver = get_provider_driver(provider)
    result = driver.associate_eip(provider, host, eip.allocation_id)

    eip.private_ip = result.get("private_ip")
    eip.association_id = result.get("association_id")
    eip.host_id = host.id
    eip.state = "associated"
    db.commit()

    logger.info(
        "EIP %s associated to host %s",
        eip.public_ip,
        host.id[:8],
    )


def disassociate_eip(db: Session, eip: ElasticIp, host) -> None:
    """Disassociate an EIP from a host.

    For EC2: disassociates address and unassigns private IP via driver.
    For OCP Virt: no-op at infra level (LB Service stays, just DB update).
    """
    if eip.state != "associated":
        logger.warning("EIP %s is not associated, skipping", eip.id)
        return

    provider = db.query(Provider).filter_by(id=eip.provider_id).first()
    if not provider:
        raise ValueError(f"Provider {eip.provider_id} not found")

    if provider.type == "ec2":
        if eip.association_id:
            from app.services.provisioner import _get_ec2_client

            creds = provider.get_credentials()
            ec2 = _get_ec2_client(credentials=creds)
            ec2.disassociate_address(AssociationId=eip.association_id)

            if eip.private_ip:
                desc = ec2.describe_instances(InstanceIds=[host.instance_id])
                for eni in desc["Reservations"][0]["Instances"][0]["NetworkInterfaces"]:
                    if eni["Attachment"]["DeviceIndex"] == 0:
                        ec2.unassign_private_ip_addresses(
                            NetworkInterfaceId=eni["NetworkInterfaceId"],
                            PrivateIpAddresses=[eip.private_ip],
                        )
                        break

    eip.private_ip = None
    eip.host_id = None
    eip.association_id = None
    eip.port_map = None
    eip.state = "allocated"
    db.commit()

    logger.info("EIP %s disassociated", eip.public_ip)


def release_eip(db: Session, eip: ElasticIp) -> None:
    """Release an EIP back to the provider."""
    if eip.state == "associated" and eip.host_id:
        from app.models.host import Host

        host = db.query(Host).filter_by(id=eip.host_id).first()
        if host:
            disassociate_eip(db, eip, host)

    provider = db.query(Provider).filter_by(id=eip.provider_id).first()
    if not provider:
        raise ValueError(f"Provider {eip.provider_id} not found")

    driver = get_provider_driver(provider)
    ns = None
    if provider.type == "ocpvirt":
        ns = provider.get_credentials().get("namespace", "troshka")
    driver.release_eip(provider, eip.allocation_id, namespace=ns)

    logger.info("Released EIP %s (%s)", eip.public_ip, eip.allocation_id)
    db.delete(eip)
    db.commit()


def migrate_eip(db: Session, eip: ElasticIp, from_host, to_host) -> None:
    """Migrate an EIP from one host to another."""
    logger.info(
        "Migrating EIP %s from host %s to host %s",
        eip.public_ip,
        from_host.id[:8],
        to_host.id[:8],
    )
    disassociate_eip(db, eip, from_host)
    associate_eip(db, eip, to_host)
    logger.info("EIP %s migration complete", eip.public_ip)


def get_host_eip_usage(db: Session, host_id: str) -> int:
    """Get count of EIPs associated with a host."""
    return (
        db.query(func.count(ElasticIp.id))
        .filter(ElasticIp.host_id == host_id, ElasticIp.state == "associated")
        .scalar()
    )


def allocate_transit_ports(
    db: Session, eip: ElasticIp, host, port_forwards: list[dict]
) -> dict:
    """Allocate transit ports for OCP Virt EIP port forwards.

    Scans existing port_map values on the same host to avoid collisions.
    Returns dict mapping ext_port (str) to transit_port (int).
    """
    used = set()
    for other in db.query(ElasticIp).filter(
        ElasticIp.host_id == host.id, ElasticIp.port_map.isnot(None)
    ):
        used.update(other.port_map.values())

    port_map = {}
    next_port = TRANSIT_PORT_START
    for pf in port_forwards:
        while next_port in used:
            next_port += 1
        if next_port > TRANSIT_PORT_END:
            raise RuntimeError("Transit port range exhausted")
        port_map[str(pf["extPort"])] = next_port
        used.add(next_port)
        next_port += 1

    eip.port_map = port_map
    db.commit()
    return port_map


def sync_security_group_rules(db: Session, provider, desired_rules: list[dict]) -> dict:
    """Reconcile SG ingress rules. EC2 only — no-op for other providers."""
    if provider.type != "ec2":
        return {"added": 0, "removed": 0}

    if not provider.security_group_id:
        return {"added": 0, "removed": 0, "error": "No security group configured"}

    from app.services.provisioner import _get_ec2_client

    creds = provider.get_credentials()
    ec2 = _get_ec2_client(credentials=creds)
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
                IpPermissions=[
                    {
                        "IpProtocol": r["protocol"],
                        "FromPort": r["port"],
                        "ToPort": r["port"],
                        "IpRanges": [
                            {
                                "CidrIp": "0.0.0.0/0",
                                "Description": r["description"],
                            }
                        ],
                    }
                    for r in to_add.values()
                ],
            )
        except Exception as e:
            if "InvalidPermission.Duplicate" not in str(e):
                raise

    if to_remove:
        ec2.revoke_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": r["protocol"],
                    "FromPort": r["port"],
                    "ToPort": r["port"],
                    "IpRanges": [
                        {
                            "CidrIp": "0.0.0.0/0",
                            "Description": r["description"],
                        }
                    ],
                }
                for r in to_remove.values()
            ],
        )

    added = len(to_add)
    removed = len(to_remove)
    if added or removed:
        logger.info("SG %s sync: +%d -%d rules", sg_id, added, removed)
    return {"added": added, "removed": removed}
