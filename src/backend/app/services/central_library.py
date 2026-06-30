"""
Central library sync service.

Scans a read-only central S4 bucket and creates local LibraryItem records
with source="central" so they appear in every user's library. Items are
read-only — users can deploy from them but cannot modify or delete them.
"""

import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _get_or_create_central_library(db: Session):
    """Get or create the shared central library."""
    from app.models.library import Library

    lib = db.query(Library).filter_by(type="central").first()
    if not lib:
        lib = Library(type="central", owner_id=None)
        db.add(lib)
        db.commit()
        db.refresh(lib)
    return lib


def sync_central_library(db: Session) -> dict:
    """Scan central S4 bucket and sync items into the local DB.

    Returns summary: {"created": N, "updated": N, "skipped": N}
    """
    from app.models.library import LibraryItem
    from app.services import s3_storage

    cfg = s3_storage._get_readonly_s3_config()
    if not cfg:
        return {"error": "No s3_readonly provider configured"}

    client = s3_storage._get_readonly_s3_client()
    bucket = cfg["bucket"]
    provider_id = cfg["provider_id"]

    lib = _get_or_create_central_library(db)

    manifest = _load_manifest(client, bucket)

    existing = {
        item.s3_key: item
        for item in db.query(LibraryItem).filter_by(library_id=lib.id, source="central")
    }

    created = 0
    updated = 0
    skipped = 0

    for entry in manifest:
        s3_key = entry["s3_key"]
        if s3_key in existing:
            item = existing[s3_key]
            if item.size_bytes != entry.get("size_bytes", 0):
                item.size_bytes = entry.get("size_bytes", 0)
                updated += 1
            else:
                skipped += 1
            continue

        item = LibraryItem(
            library_id=lib.id,
            name=entry["name"],
            type=entry.get("type", "image"),
            format=entry.get("format", "qcow2"),
            size_bytes=entry.get("size_bytes", 0),
            s3_key=s3_key,
            os_variant=entry.get("os_variant"),
            state="ready",
            source="central",
            source_provider_id=provider_id,
            tags=entry.get("tags"),
        )
        db.add(item)
        created += 1

    db.commit()
    logger.info(
        "Central library sync: %d created, %d updated, %d skipped",
        created,
        updated,
        skipped,
    )
    return {"created": created, "updated": updated, "skipped": skipped}


def _load_manifest(client, bucket: str) -> list[dict]:
    """Load manifest.json from central bucket, or fall back to listing objects."""
    import json

    try:
        resp = client.get_object(Bucket=bucket, Key="library/manifest.json")
        data = json.loads(resp["Body"].read())
        if isinstance(data, list):
            return data
    except Exception:
        pass

    return _scan_bucket(client, bucket)


def _scan_bucket(client, bucket: str) -> list[dict]:
    """List all objects in the central bucket and infer metadata from keys."""
    items = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or key == "library/manifest.json":
                continue
            name = key.rsplit("/", 1)[-1]
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            fmt = ext if ext in ("qcow2", "iso", "raw", "vmdk") else "qcow2"
            item_type = "iso" if ext == "iso" else "image"
            display_name = name.rsplit(".", 1)[0].replace("-", " ").replace("_", " ")
            items.append(
                {
                    "s3_key": key,
                    "name": display_name,
                    "type": item_type,
                    "format": fmt,
                    "size_bytes": obj.get("Size", 0),
                }
            )
    return items
