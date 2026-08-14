"""One-time backfill: central PatternLocation rows for legacy patterns.

For every PatternDisk without a synced central location, HEAD the object in
central S4 (troshka-images). Present -> create a synced central row (DB-only,
instant). Absent -> the pattern's disks are OBC-only; enqueue a sync.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import select

from app.models.pattern import PatternDisk
from app.models.pattern_location import PatternLocation

log = logging.getLogger(__name__)


def backfill_central_locations(db, s3_client, bucket) -> tuple[int, list[str]]:
    """Create synced central rows for disks present in central S4.

    Returns (rows_created, pattern_ids_needing_sync).
    """
    created = 0
    need_sync: list[str] = []
    now = datetime.datetime.now(datetime.UTC)

    for pd in db.scalars(select(PatternDisk)).all():
        existing = db.scalars(
            select(PatternLocation).filter_by(
                pattern_disk_id=pd.id, location_type="central", state="synced"
            )
        ).first()
        if existing:
            continue
        try:
            s3_client.head_object(Bucket=bucket, Key=pd.s3_key)
        except Exception:
            if pd.pattern_id not in need_sync:
                need_sync.append(pd.pattern_id)
            continue
        db.add(
            PatternLocation(
                pattern_disk_id=pd.id,
                provider_id=None,
                location_type="central",
                s3_key=pd.s3_key,
                state="synced",
                synced_at=now,
                size_bytes=pd.size_bytes or 0,
            )
        )
        created += 1

    db.commit()
    return created, need_sync


def run_backfill() -> None:
    from app.core.database import SessionLocal
    from app.core.redis import enqueue_job
    from app.services.pattern_sync import sync_pattern_to_central
    from app.services.s3_storage import _bucket, _get_s3_client

    db = SessionLocal()
    try:
        created, need_sync = backfill_central_locations(db, _get_s3_client(), _bucket())
        for pattern_id in need_sync:
            enqueue_job(sync_pattern_to_central, pattern_id, queue_name="default")
        log.info(
            "Backfill complete: %d central rows created, %d patterns enqueued for sync",
            created,
            len(need_sync),
        )
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_backfill()
