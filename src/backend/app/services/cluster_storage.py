"""Per-cluster RGW/OBC storage operations.

Handles delete and orphan cleanup for pattern disk images stored on
KubeVirt cluster-local Ceph RGW buckets (via ObjectBucketClaims).
"""

import logging

import boto3
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _get_client_and_bucket(
    db: Session, provider_id: str
) -> tuple["boto3.client", str] | None:  # type: ignore[type-arg]
    from app.services.s3_storage import get_cluster_s3_config

    cfg = get_cluster_s3_config(db, provider_id)
    if not cfg:
        return None

    kwargs = {"region_name": cfg.get("region", "us-east-1")}
    if cfg.get("access_key_id"):
        kwargs["aws_access_key_id"] = cfg["access_key_id"]
    if cfg.get("secret_access_key"):
        kwargs["aws_secret_access_key"] = cfg["secret_access_key"]
    endpoint = cfg.get("endpoint") or cfg.get("endpoint_url")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
        kwargs["verify"] = False

    client = boto3.client("s3", **kwargs)
    bucket = cfg.get("bucket", "")
    return client, bucket


def delete_pattern(db: Session, provider_id: str, pattern_id: str) -> int:
    """Delete all objects under patterns/{pattern_id}/ from a cluster's RGW."""
    result = _get_client_and_bucket(db, provider_id)
    if not result:
        logger.warning(
            "No cluster S3 config for provider %s — skipping RGW cleanup",
            provider_id[:8],
        )
        return 0

    client, bucket = result
    prefix = f"patterns/{pattern_id}/"
    deleted = 0

    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if objects:
                client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
                deleted += len(objects)
    except Exception:
        logger.exception(
            "Failed to clean RGW prefix %s on provider %s",
            prefix,
            provider_id[:8],
        )
        return deleted

    if deleted:
        logger.info(
            "Cluster RGW cleanup: deleted %d objects under %s (provider %s)",
            deleted,
            prefix,
            provider_id[:8],
        )
    return deleted


def clean_orphans(db: Session, provider_id: str, dry_run: bool = False) -> dict:
    """Delete orphaned pattern objects from a cluster's RGW bucket."""
    from app.models.pattern import Pattern

    result = _get_client_and_bucket(db, provider_id)
    if not result:
        return {"error": f"No cluster S3 config for provider {provider_id[:8]}"}

    client, bucket = result
    active_pattern_ids = {p.id for p in db.query(Pattern).all()}

    deleted = 0
    deleted_bytes = 0
    orphan_ids: list[str] = []

    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=bucket, Prefix="patterns/", Delimiter="/"
        ):
            for cp in page.get("CommonPrefixes", []):
                pid = cp["Prefix"].removeprefix("patterns/").rstrip("/")
                if pid not in active_pattern_ids:
                    orphan_ids.append(pid)
    except Exception:
        logger.exception("Failed to list RGW prefixes on provider %s", provider_id[:8])
        return {"error": "Failed to list RGW bucket"}

    for pid in orphan_ids:
        if dry_run:
            logger.info(
                "Cluster RGW dry-run: would delete orphan pattern %s (provider %s)",
                pid[:8],
                provider_id[:8],
            )
            continue
        try:
            prefix = f"patterns/{pid}/"
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                objects = page.get("Contents", [])
                if objects:
                    deleted_bytes += sum(o.get("Size", 0) for o in objects)
                    client.delete_objects(
                        Bucket=bucket,
                        Delete={"Objects": [{"Key": o["Key"]} for o in objects]},
                    )
                    deleted += len(objects)
        except Exception:
            logger.exception(
                "Failed to delete orphan pattern %s from RGW (provider %s)",
                pid[:8],
                provider_id[:8],
            )

    report = {
        "provider_id": provider_id,
        "orphan_patterns": len(orphan_ids),
        "deleted": deleted,
    }
    if deleted_bytes:
        report["deleted_gb"] = round(deleted_bytes / (1024**3), 1)
    if dry_run:
        report["dry_run"] = True

    if orphan_ids:
        logger.info(
            "Cluster RGW GC (provider %s): %d orphan patterns, %d objects deleted",
            provider_id[:8],
            len(orphan_ids),
            deleted,
        )

    return report
