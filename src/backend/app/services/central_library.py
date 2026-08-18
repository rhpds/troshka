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


def _process_manifest_entry(
    entry, existing, local_fingerprints, db, lib_id, provider_id
):
    """Process a single manifest entry during central library sync.

    Returns "created", "updated", "skipped", or "removed" (removed implies also skipped).
    """
    from app.models.library import LibraryItem

    s3_key = entry["s3_key"]
    fingerprint = (entry.get("size_bytes", 0), entry.get("format", "qcow2"))

    if fingerprint in local_fingerprints:
        if s3_key in existing:
            db.delete(existing[s3_key])
            return "removed"
        return "skipped"

    if s3_key in existing:
        item = existing[s3_key]
        if item.size_bytes != entry.get("size_bytes", 0):
            item.size_bytes = entry.get("size_bytes", 0)
            return "updated"
        return "skipped"

    item = LibraryItem(
        library_id=lib_id,
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
    return "created"


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
    removed = 0

    for entry in manifest:
        action = _process_manifest_entry(
            entry, existing, local_fingerprints, db, lib.id, provider_id
        )
        if action == "created":
            created += 1
        elif action == "updated":
            updated += 1
        elif action == "removed":
            removed += 1
            skipped += 1
        else:
            skipped += 1

    current_keys = {e["s3_key"] for e in manifest}
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


def _process_s3_object(s3_client, bucket, key, size, groups, pid):
    """Process a single S3 object into the pattern groups dict."""
    import json

    if key.endswith("/metadata.json"):
        try:
            resp = s3_client.get_object(Bucket=bucket, Key=key)
            groups[pid]["metadata"] = json.loads(resp["Body"].read())
        except Exception:
            pass
    else:
        groups[pid]["files"].append({"key": key, "size": size})


def _scan_s3_pattern_groups(s3_client, bucket):
    """Scan S3 patterns/ prefix and group objects by pattern ID."""
    groups: dict[str, dict] = {}
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="patterns/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            parts = key.split("/")
            if len(parts) < 3:
                continue
            pid = parts[1]
            if pid not in groups:
                groups[pid] = {"metadata": None, "files": []}
            _process_s3_object(s3_client, bucket, key, obj.get("Size", 0), groups, pid)
    return groups


def _create_pattern_record(db, pid, meta, owner_id, provider_id):
    """Create a Pattern + PatternDisk records from metadata."""
    import uuid as _uuid

    from app.models.pattern import Pattern, PatternDisk
    from app.models.pattern_location import PatternLocation

    topology = meta.get("topology", {"nodes": [], "edges": []})
    _remap_library_refs(topology, db)

    from app.services.ocp_topology_flags import apply_sno_ocp_vm_flags

    apply_sno_ocp_vm_flags(topology, recert=bool(meta.get("recert")))

    pattern = Pattern(
        id=pid,
        name=meta.get("name", f"pattern-{pid[:8]}"),
        description=meta.get("description"),
        owner_id=owner_id or meta.get("owner_id", "system"),
        visibility="public",
        topology=topology,
        state="available",
        recert=bool(meta.get("recert")),
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
        disk_id = disk.get("id", str(_uuid.uuid4()))
        db.add(
            PatternDisk(
                id=disk_id,
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
        # The disk lives in the central S4 bucket — record a central location so
        # pattern_disk_source_for_cluster() can resolve it on any cluster that
        # has the central read-only provider configured.
        db.add(
            PatternLocation(
                pattern_disk_id=disk_id,
                provider_id=None,
                location_type="central",
                s3_key=disk["s3_key"],
                state="synced",
                size_bytes=disk.get("size_bytes", 0),
            )
        )


def sync_central_patterns(
    db: Session, client=None, cfg: dict | None = None, owner_id: str | None = None
) -> dict:
    """Scan central S4 for patterns and create local Pattern + PatternDisk records."""
    from app.models.pattern import Pattern
    from app.services import s3_storage

    if not cfg:
        cfg = s3_storage._get_readonly_s3_config()
    if not cfg:
        return {"error": "No s3_readonly provider configured"}
    if not client:
        client = s3_storage._get_readonly_s3_client()
    if not client:
        return {"error": "Could not create S3 client for readonly provider"}

    bucket = cfg["bucket"]
    provider_id = cfg["provider_id"]
    pattern_groups = _scan_s3_pattern_groups(client, bucket)

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

        _create_pattern_record(db, pid, meta, owner_id, provider_id)
        created += 1

    if created:
        db.commit()

    logger.info("Central pattern sync: %d created, %d skipped", created, skipped)
    return {"created": created, "skipped": skipped}


def _remap_storage_node(data, db, local_items, local_by_size):
    """Remap library item references for a single storage node.

    Tries name match first, then size+format match. Mutates data in place.
    """
    from app.models.library import LibraryItem

    item_id = data.get("libraryItemId")
    if not item_id or data.get("source") == "pattern":
        return

    existing = db.query(LibraryItem).filter_by(id=item_id).first()
    if existing:
        return

    fmt = data.get("format", "qcow2")
    size = data.get("sizeBytes", 0)
    ref_name = (data.get("libraryItemName") or data.get("label") or "").lower()
    for local in local_items:
        if local.format == fmt and local.name.lower() == ref_name:
            data["libraryItemId"] = local.id
            data["libraryItemName"] = local.name
            logger.info(
                "Remapped library ref %s -> %s (%s)",
                item_id[:8],
                local.id[:8],
                local.name,
            )
            return

    matched = local_by_size.get((size, fmt))
    if matched:
        data["libraryItemId"] = matched.id
        data["libraryItemName"] = matched.name
        logger.info(
            "Remapped library ref %s -> %s (%s) by size",
            item_id[:8],
            matched.id[:8],
            matched.name,
        )


def _remap_library_refs(topology: dict, db: Session):
    """Remap libraryItemId references in topology to match local library items.

    Pattern topologies may reference library items by UUID from the original
    instance. This remaps them to local items matched by size+format.
    """
    from app.models.library import LibraryItem

    # Include central-synced items: a dedicated Troshka's library is entirely
    # source=="central", so restricting to "local" would leave nothing to
    # remap the pattern's library refs onto.
    local_items = (
        db.query(LibraryItem).filter(LibraryItem.source.in_(["local", "central"])).all()
    )
    local_by_size = {}
    for item in local_items:
        key = (item.size_bytes, item.format)
        if key not in local_by_size:
            local_by_size[key] = item

    for node in topology.get("nodes", []):
        if node.get("type") != "storageNode":
            continue
        _remap_storage_node(node.get("data", {}), db, local_items, local_by_size)


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


def _s3_key_to_item(key, size):
    """Convert an S3 key and size to a library item dict.

    Returns None for directory entries, manifest.json, or patterns/ prefix.
    """
    if key.endswith("/") or key == "library/manifest.json":
        return None
    if key.startswith("patterns/"):
        return None
    name = key.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    fmt = ext if ext in ("qcow2", "iso", "raw", "vmdk") else "qcow2"
    item_type = "iso" if ext == "iso" else "image"
    stem = name.rsplit(".", 1)[0]
    display_name = stem.replace("-", " ").replace("_", " ").title()
    return {
        "s3_key": key,
        "name": display_name,
        "type": item_type,
        "format": fmt,
        "size_bytes": size,
    }


def _scan_bucket(client, bucket: str) -> list[dict]:
    """List all objects in the central bucket and infer metadata from keys."""
    items = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            item = _s3_key_to_item(obj["Key"], obj.get("Size", 0))
            if item:
                items.append(item)
    return items


def resolve_manifest_item_s3_key(
    manifest: list[dict], *, name: str, item_type: str = "iso"
) -> str:
    """Look up an s3_key in library/manifest.json by display name."""
    for entry in manifest:
        if entry.get("name") == name and entry.get("type") == item_type:
            key = entry.get("s3_key")
            if key:
                return key
    raise ValueError(f"{item_type} library item {name!r} not found in manifest")
