"""One-time backfill: reconcile provider-less PatternLocation rows to the store
where each object actually lives.

Historically ``location_type="central"`` was overloaded: pattern_sync used it for
the shared read/write bucket (user patterns), while the central_library scan used
it for the read-only admin gold store. Deploy now routes "central" -> read/write
bucket and "gold" -> read-only store, so the rows must be reclassified to match
reality.

For every provider-less central/gold row: if the object is readable in the
read/write bucket -> ``central``; else if readable in the gold store -> ``gold``;
else -> ``error`` (so deploy fails fast with a clear reason instead of a cryptic
missing-backing-file error).
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable

from sqlalchemy import select

from app.models.pattern_location import PatternLocation

log = logging.getLogger(__name__)


def reconcile_pattern_locations(
    db,
    exists_in_central: Callable[[str], bool],
    exists_in_gold: Callable[[str], bool],
) -> dict[str, int]:
    """Reclassify provider-less central/gold rows to match object reality.

    ``exists_in_central`` / ``exists_in_gold`` take an s3_key and return whether
    the object is readable in that store. Returns a summary count per outcome.
    """
    summary = {"central": 0, "gold": 0, "error": 0}
    now = datetime.datetime.now(datetime.UTC)
    rows = db.scalars(
        select(PatternLocation)
        .where(PatternLocation.provider_id.is_(None))
        .where(PatternLocation.location_type.in_(["central", "gold"]))
    ).all()
    for row in rows:
        if exists_in_central(row.s3_key):
            row.location_type = "central"
            row.state = "synced"
            row.synced_at = now
            row.error_message = None
            summary["central"] += 1
        elif exists_in_gold(row.s3_key):
            row.location_type = "gold"
            row.state = "synced"
            row.synced_at = now
            row.error_message = None
            summary["gold"] += 1
        else:
            row.state = "error"
            row.error_message = "object not found in central (read/write) or gold store"
            summary["error"] += 1
    db.commit()
    return summary


def _make_exists(client, bucket) -> Callable[[str], bool]:
    """Build an s3_key -> bool existence checker for a boto3 client/bucket."""
    if not client or not bucket:
        return lambda _key: False

    def _exists(key: str) -> bool:
        try:
            client.head_object(Bucket=bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001 — any failure means "not readable here"
            return False

    return _exists


def run_reconcile() -> None:
    import boto3

    from app.core.database import SessionLocal
    from app.services.s3_storage import (
        _bucket,
        _get_readonly_s3_config,
        _get_s3_client,
    )

    ro_cfg = _get_readonly_s3_config()
    ro_client = None
    ro_bucket = ""
    if ro_cfg:
        ro_client = boto3.client(
            "s3",
            region_name=ro_cfg.get("region", "us-east-1"),
            aws_access_key_id=ro_cfg.get("access_key_id", ""),
            aws_secret_access_key=ro_cfg.get("secret_access_key", ""),
            endpoint_url=ro_cfg.get("endpoint_url") or None,
        )
        ro_bucket = ro_cfg.get("bucket", "")

    db = SessionLocal()
    try:
        summary = reconcile_pattern_locations(
            db,
            _make_exists(_get_s3_client(), _bucket()),
            _make_exists(ro_client, ro_bucket),
        )
        log.info(
            "Reconcile complete: %d central, %d gold, %d errored",
            summary["central"],
            summary["gold"],
            summary["error"],
        )
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_reconcile()
