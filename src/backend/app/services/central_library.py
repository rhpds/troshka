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


def sync_central_library(db: Session, owner_id: str | None = None) -> dict:
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

    local_items = (
        db.query(LibraryItem)
        .filter(
            LibraryItem.library_id != lib.id,
            LibraryItem.source == "local",
        )
        .all()
    )
    local_fingerprints = {(item.size_bytes, item.format) for item in local_items}

    created = 0
    updated = 0
    skipped = 0

    for entry in manifest:
        s3_key = entry["s3_key"]
        fingerprint = (entry.get("size_bytes", 0), entry.get("format", "qcow2"))

        if fingerprint in local_fingerprints:
            if s3_key in existing:
                db.delete(existing[s3_key])
            skipped += 1
            continue

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

    current_keys = {e["s3_key"] for e in manifest}
    removed = 0
    for s3_key, item in existing.items():
        if s3_key not in current_keys:
            db.delete(item)
            removed += 1

    db.commit()
    logger.info(
        "Central library sync: %d created, %d updated, %d skipped, %d removed",
        created,
        updated,
        skipped,
        removed,
    )
    pattern_result = sync_central_patterns(
        db, client=client, cfg=cfg, owner_id=owner_id
    )

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "removed": removed,
        "patterns": pattern_result,
    }


def sync_central_patterns(
    db: Session, client=None, cfg: dict | None = None, owner_id: str | None = None
) -> dict:
    """Scan central S4 for patterns and create local Pattern + PatternDisk records."""
    import json
    import uuid as _uuid

    from app.models.pattern import Pattern, PatternDisk
    from app.services import s3_storage

    if not cfg:
        cfg = s3_storage._get_readonly_s3_config()
    if not cfg:
        return {"error": "No s3_readonly provider configured"}
    if not client:
        client = s3_storage._get_readonly_s3_client()

    bucket = cfg["bucket"]
    provider_id = cfg["provider_id"]

    pattern_groups: dict[str, dict] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="patterns/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            parts = key.split("/")
            if len(parts) < 3:
                continue
            pid = parts[1]
            if pid not in pattern_groups:
                pattern_groups[pid] = {"metadata": None, "files": []}
            if key.endswith("/metadata.json"):
                try:
                    resp = client.get_object(Bucket=bucket, Key=key)
                    pattern_groups[pid]["metadata"] = json.loads(resp["Body"].read())
                except Exception:
                    pass
            else:
                pattern_groups[pid]["files"].append(
                    {"key": key, "size": obj.get("Size", 0)}
                )

    created = 0
    skipped = 0

    for pid, group in pattern_groups.items():
        if db.query(Pattern).filter_by(id=pid).first():
            skipped += 1
            continue

        meta = group["metadata"]
        if not meta:
            skipped += 1
            continue

        pattern = Pattern(
            id=pid,
            name=meta.get("name", f"pattern-{pid[:8]}"),
            description=meta.get("description"),
            owner_id=owner_id or meta.get("owner_id", "system"),
            visibility="public",
            topology=meta.get("topology", {"nodes": [], "edges": []}),
            state="available",
            total_size_bytes=meta.get("total_size_bytes", 0),
            tags={
                **(meta.get("tags") or {}),
                "source": "central",
                "source_provider_id": provider_id,
            },
        )
        db.add(pattern)
        db.flush()

        for disk in meta.get("disks", []):
            db.add(
                PatternDisk(
                    id=disk.get("id", str(_uuid.uuid4())),
                    pattern_id=pid,
                    source_disk_id=disk.get("source_disk_id", ""),
                    source_vm_id=disk.get("source_vm_id", ""),
                    s3_key=disk["s3_key"],
                    format=disk.get("format", "qcow2"),
                    size_bytes=disk.get("size_bytes", 0),
                    virtual_size_bytes=disk.get("virtual_size_bytes", 0),
                    checksum_sha256=disk.get("checksum_sha256"),
                    state="available",
                )
            )
        created += 1

    if created:
        db.commit()

    logger.info("Central pattern sync: %d created, %d skipped", created, skipped)
    return {"created": created, "skipped": skipped}


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
            if key.startswith("patterns/"):
                continue
            name = key.rsplit("/", 1)[-1]
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            fmt = ext if ext in ("qcow2", "iso", "raw", "vmdk") else "qcow2"
            item_type = "iso" if ext == "iso" else "image"
            stem = name.rsplit(".", 1)[0]
            display_name = stem.replace("-", " ").replace("_", " ").title()
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
