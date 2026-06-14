"""Provider-level garbage collector — reconcile AWS resources with DB state."""
import logging

import boto3
from sqlalchemy.orm import Session

from app.models.elastic_ip import ElasticIp
from app.models.project import Project

logger = logging.getLogger(__name__)


def _get_ec2_client(provider):
    creds = provider.get_credentials()
    return boto3.client(
        "ec2",
        region_name=provider.default_region,
        aws_access_key_id=creds.get("access_key_id"),
        aws_secret_access_key=creds.get("secret_access_key"),
    )


def _gc_orphan_eips(db: Session, provider, ec2, dry_run: bool) -> dict:
    """Find and release Troshka-tagged EIPs whose project no longer exists."""
    result = ec2.describe_addresses(
        Filters=[
            {"Name": "tag:ManagedBy", "Values": ["troshka"]},
        ]
    )

    orphans = []
    for addr in result.get("Addresses", []):
        tags = {t["Key"]: t["Value"] for t in addr.get("Tags", [])}
        project_id = tags.get("troshka-project-id", "")
        if not project_id:
            orphans.append(addr)
            continue
        project = db.query(Project).filter_by(id=project_id).first()
        if not project:
            orphans.append(addr)

    released = 0
    for addr in orphans:
        alloc_id = addr["AllocationId"]
        if dry_run:
            logger.info(
                "GC dry-run: would release orphan EIP %s (%s)",
                alloc_id,
                addr.get("PublicIp"),
            )
            continue

        if addr.get("AssociationId"):
            try:
                ec2.disassociate_address(AssociationId=addr["AssociationId"])
            except Exception:
                logger.warning("Failed to disassociate orphan EIP %s", alloc_id)

        ec2.release_address(AllocationId=alloc_id)
        db_eip = db.query(ElasticIp).filter_by(allocation_id=alloc_id).first()
        if db_eip:
            db.delete(db_eip)
        released += 1
        logger.info("GC: released orphan EIP %s (%s)", alloc_id, addr.get("PublicIp"))

    db.commit()

    # Also clean stale DB rows with no matching AWS resource
    all_aws_alloc_ids = {a["AllocationId"] for a in result.get("Addresses", [])}
    stale_deleted = 0
    if all_aws_alloc_ids:
        stale_rows = (
            db.query(ElasticIp)
            .filter(
                ElasticIp.provider_id == provider.id,
                ~ElasticIp.allocation_id.in_(all_aws_alloc_ids),
            )
            .all()
        )
    else:
        stale_rows = (
            db.query(ElasticIp)
            .filter(
                ElasticIp.provider_id == provider.id,
            )
            .all()
        )
    for row in stale_rows:
        if not dry_run:
            db.delete(row)
            stale_deleted += 1
    if stale_deleted:
        db.commit()
        logger.info("GC: deleted %d stale DB rows", stale_deleted)

    return {
        "eips_released": released,
        "eips_would_release": len(orphans) if dry_run else 0,
        "stale_db_rows_deleted": stale_deleted,
    }


def _gc_stale_sg_rules(db: Session, provider, ec2, dry_run: bool) -> dict:
    """Remove SG ingress rules for projects that no longer exist."""
    if not provider.security_group_id:
        return {"sg_rules_removed": 0}

    sg = ec2.describe_security_groups(GroupIds=[provider.security_group_id])
    current_perms = sg["SecurityGroups"][0]["IpPermissions"]

    stale_rules = []
    for perm in current_perms:
        for ip_range in perm.get("IpRanges", []):
            desc = ip_range.get("Description", "")
            if not desc.startswith("troshka-pf:"):
                continue
            parts = desc.split(":")
            if len(parts) >= 2:
                project_id = parts[1]
                project = db.query(Project).filter_by(id=project_id).first()
                if not project or project.state not in ("active", "deploying"):
                    stale_rules.append(
                        {
                            "protocol": perm["IpProtocol"],
                            "port": perm["FromPort"],
                            "description": desc,
                        }
                    )

    removed = 0
    if stale_rules and not dry_run:
        ec2.revoke_security_group_ingress(
            GroupId=provider.security_group_id,
            IpPermissions=[
                {
                    "IpProtocol": r["protocol"],
                    "FromPort": r["port"],
                    "ToPort": r["port"],
                    "IpRanges": [
                        {"CidrIp": "0.0.0.0/0", "Description": r["description"]}
                    ],
                }
                for r in stale_rules
            ],
        )
        removed = len(stale_rules)
        logger.info("GC: removed %d stale SG rules", removed)

    return {
        "sg_rules_removed": removed,
        "sg_rules_would_remove": len(stale_rules) if dry_run else 0,
    }


def reconcile_provider(db: Session, provider, dry_run: bool = False) -> dict:
    """Full provider-level GC: orphan EIPs + stale SG rules."""
    ec2 = _get_ec2_client(provider)
    report = {"provider_id": provider.id, "provider_name": provider.name}

    eip_result = _gc_orphan_eips(db, provider, ec2, dry_run)
    report.update(eip_result)

    sg_result = _gc_stale_sg_rules(db, provider, ec2, dry_run)
    report.update(sg_result)

    logger.info("Provider GC %s: %s", provider.name, report)
    return report
