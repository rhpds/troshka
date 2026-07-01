"""
Library API — manage ISOs and disk images in S3.
"""

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.library import Library, LibraryItem, LibraryShare
from app.models.user import User
from app.services import s3_storage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/library", tags=["library"])


class LibraryItemCreate(BaseModel):
    name: str
    description: str = ""
    type: str = "image"
    format: str = "qcow2"
    os_variant: str = ""
    tags: list[str] | None = None


class LibraryItemResponse(BaseModel):
    id: str
    library_id: str
    name: str
    description: str | None = None
    type: str
    format: str
    size_bytes: int
    s3_key: str | None = None
    checksum_sha256: str | None = None
    os_variant: str | None = None
    state: str
    tags: list | None = None
    created_at: str

    model_config = {"from_attributes": True}


def _check_not_central(item: LibraryItem):
    """Reject mutations on central (read-only) library items."""
    if getattr(item, "source", "local") == "central":
        raise HTTPException(
            status_code=403, detail="Central library items are read-only"
        )


def _ensure_user_library(user: User, db: Session) -> Library:
    """Get or create the user's personal library."""
    lib = db.query(Library).filter_by(owner_id=user.id, type="personal").first()
    if not lib:
        lib = Library(owner_id=user.id, type="personal")
        db.add(lib)
        db.commit()
        db.refresh(lib)
    return lib


@router.get("/")
def list_items(
    type: str | None = Query(None),
    q: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List library items: user's own + shared with them."""
    from sqlalchemy import or_

    lib = _ensure_user_library(user, db)

    # Get IDs of items shared with this user
    shared_ids = [
        s.item_id
        for s in db.query(LibraryShare.item_id).filter_by(shared_with_id=user.id).all()
    ]

    central_lib = db.query(Library).filter_by(type="central").first()
    central_lib_id = central_lib.id if central_lib else None

    query = db.query(LibraryItem).filter(
        or_(
            LibraryItem.library_id == lib.id,
            LibraryItem.id.in_(shared_ids) if shared_ids else False,
            LibraryItem.library_id == central_lib_id if central_lib_id else False,
        )
    )

    if type:
        query = query.filter(LibraryItem.type == type)
    if q:
        query = query.filter(LibraryItem.name.ilike(f"%{q}%"))

    items = query.order_by(LibraryItem.created_at.desc()).all()

    # Get owner info for shared items
    owner_libs = {lib.id: lib.owner_id}
    for i in items:
        if i.library_id not in owner_libs:
            item_lib = db.query(Library).filter_by(id=i.library_id).first()
            if item_lib:
                owner_libs[i.library_id] = item_lib.owner_id

    return [
        {
            "id": i.id,
            "name": i.name,
            "description": i.description,
            "type": i.type,
            "format": i.format,
            "size_bytes": i.size_bytes,
            "os_variant": i.os_variant,
            "state": i.state,
            "tags": i.tags,
            "created_at": str(i.created_at),
            "owned": i.library_id == lib.id,
            "owner_id": owner_libs.get(i.library_id),
            "source": getattr(i, "source", "local"),
            "readonly": getattr(i, "source", "local") == "central",
        }
        for i in items
    ]


@router.get("/{item_id}")
def get_item(
    item_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "type": item.type,
        "format": item.format,
        "size_bytes": item.size_bytes,
        "s3_key": item.s3_key,
        "os_variant": item.os_variant,
        "state": item.state,
        "tags": item.tags,
        "created_at": str(item.created_at),
    }


class LibraryItemUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    source_url: str | None = None
    tags: dict | None = None


@router.patch("/{item_id}")
def update_item(
    item_id: str,
    body: LibraryItemUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    _check_not_central(item)
    lib = db.query(Library).filter_by(id=item.library_id).first()
    if not lib or lib.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    if body.name is not None:
        item.name = body.name
    if body.description is not None:
        item.description = body.description
    if body.source_url is not None:
        item.source_url = body.source_url
    if body.tags is not None:
        new_tags = body.tags
        # Enforce only one default per type
        if new_tags.get("ocp_default_image"):
            for other in (
                db.query(LibraryItem)
                .filter(
                    LibraryItem.id != item.id, LibraryItem.library_id == item.library_id
                )
                .all()
            ):
                other_tags = other.tags if isinstance(other.tags, dict) else {}
                if other_tags.get("ocp_default_image"):
                    other.tags = {
                        k: v for k, v in other_tags.items() if k != "ocp_default_image"
                    }
                    flag_modified(other, "tags")
        if new_tags.get("ocp_default_iso"):
            for other in (
                db.query(LibraryItem)
                .filter(
                    LibraryItem.id != item.id, LibraryItem.library_id == item.library_id
                )
                .all()
            ):
                other_tags = other.tags if isinstance(other.tags, dict) else {}
                if other_tags.get("ocp_default_iso"):
                    other.tags = {
                        k: v for k, v in other_tags.items() if k != "ocp_default_iso"
                    }
                    flag_modified(other, "tags")
        item.tags = new_tags
        flag_modified(item, "tags")
    db.commit()
    return {"id": item.id, "name": item.name, "description": item.description}


@router.post("/", status_code=201)
def create_item(
    body: LibraryItemCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a library item (metadata only). Upload file separately."""
    lib = _ensure_user_library(user, db)
    existing = (
        db.query(LibraryItem).filter_by(library_id=lib.id, name=body.name).first()
    )
    if existing:
        raise HTTPException(
            status_code=409, detail=f'You already have an item named "{body.name}"'
        )
    item = LibraryItem(
        library_id=lib.id,
        name=body.name,
        description=body.description,
        type=body.type,
        format=body.format,
        os_variant=body.os_variant,
        state="pending",
        tags=body.tags,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "state": item.state}


@router.post("/{item_id}/upload-start")
def start_multipart_upload(
    item_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start a multipart S3 upload and return presigned URLs for each part."""

    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    lib = db.query(Library).filter_by(id=item.library_id).first()
    if not lib or lib.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    ext = item.format if item.format != "qcow2" else "qcow2"
    s3_key = f"library/{user.id}/{item.id}/{item.name}.{ext}"

    item.s3_key = s3_key
    item.state = "uploading"
    db.commit()

    from app.services.s3_storage import _bucket, _get_s3_client

    client = _get_s3_client()

    # Parse file_size from query param

    # We'll receive file_size in the JSON body instead
    return _do_start_upload(client, _bucket(), s3_key, item_id)


def _do_start_upload(client, bucket, s3_key, item_id):
    mpu = client.create_multipart_upload(
        Bucket=bucket, Key=s3_key, ContentType="application/octet-stream"
    )
    upload_id = mpu["UploadId"]
    return {"upload_id": upload_id, "s3_key": s3_key}


@router.post("/{item_id}/upload-part-url")
def get_part_upload_url(
    item_id: str,
    upload_id: str = Query(...),
    part_number: int = Query(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a presigned URL for uploading one part of a multipart upload."""
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item or not item.s3_key:
        raise HTTPException(status_code=404, detail="Item not found")

    from app.services.s3_storage import _bucket, _get_s3_client

    client = _get_s3_client()
    url = client.generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": _bucket(),
            "Key": item.s3_key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        },
        ExpiresIn=7200,
    )
    return {"url": url, "part_number": part_number}


class CompleteUploadRequest(BaseModel):
    upload_id: str
    parts: list[dict]


@router.post("/{item_id}/upload-complete")
def complete_upload(
    item_id: str,
    body: CompleteUploadRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Complete a multipart S3 upload."""
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    lib = db.query(Library).filter_by(id=item.library_id).first()
    if not lib or lib.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if not item.s3_key:
        raise HTTPException(status_code=400, detail="No S3 key set")

    from app.services.s3_storage import _bucket, _get_s3_client

    client = _get_s3_client()
    try:
        client.complete_multipart_upload(
            Bucket=_bucket(),
            Key=item.s3_key,
            UploadId=body.upload_id,
            MultipartUpload={
                "Parts": [
                    {"PartNumber": p["part_number"], "ETag": p["etag"]}
                    for p in body.parts
                ]
            },
        )

        head = client.head_object(Bucket=_bucket(), Key=item.s3_key)
        item.size_bytes = head["ContentLength"]
        item.state = "ready"
        db.commit()
        logger.info(
            "Library item %s upload complete: %s (%d bytes)",
            item.id,
            item.s3_key,
            item.size_bytes,
        )
        return {"id": item.id, "state": "ready", "size_bytes": item.size_bytes}
    except Exception as e:
        logger.exception("Upload completion failed for %s: %s", item_id, e)
        item.state = "error"
        db.commit()
        raise HTTPException(status_code=500, detail="Upload completion failed")


@router.post("/{item_id}/upload-proxy")
async def upload_proxy(
    item_id: str,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stream a file upload to S3/MinIO through the backend.

    Used when MinIO is behind a cluster-internal service and presigned
    URLs are not browser-reachable.
    """
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    lib = db.query(Library).filter_by(id=item.library_id).first()
    if not lib or lib.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    ext = item.format if item.format != "qcow2" else "qcow2"
    s3_key = f"library/{user.id}/{item.id}/{item.name}.{ext}"

    from app.services.s3_storage import _bucket, _get_s3_client

    client = _get_s3_client()
    client.upload_fileobj(
        file.file,
        _bucket(),
        s3_key,
        ExtraArgs={"ContentType": file.content_type or "application/octet-stream"},
    )

    head = client.head_object(Bucket=_bucket(), Key=s3_key)
    item.s3_key = s3_key
    item.size_bytes = head["ContentLength"]
    item.state = "ready"
    db.commit()

    logger.info("Proxy upload: %s → %s (%d bytes)", item.name, s3_key, item.size_bytes)
    return {"s3_key": s3_key, "size_bytes": item.size_bytes}


class FinalizeSeedRequest(BaseModel):
    seed_key: str
    tags: list[str] = []
    skip_copy: bool = False


@router.post("/{item_id}/finalize-seed")
def finalize_seed(
    item_id: str,
    body: FinalizeSeedRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Move a seeded S3 object to the canonical library path and mark ready."""
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    lib = db.query(Library).filter_by(id=item.library_id).first()
    if not lib or lib.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    from app.services.s3_storage import _bucket, _get_s3_client

    client = _get_s3_client()
    bucket = _bucket()

    if body.skip_copy:
        head = client.head_object(Bucket=bucket, Key=body.seed_key)
        item.s3_key = body.seed_key
        item.size_bytes = head["ContentLength"]
    else:
        dest_key = f"library/{user.id}/{item.id}/{item.name}.{item.format}"
        client.copy_object(
            Bucket=bucket,
            CopySource={"Bucket": bucket, "Key": body.seed_key},
            Key=dest_key,
        )
        client.delete_object(Bucket=bucket, Key=body.seed_key)
        head = client.head_object(Bucket=bucket, Key=dest_key)
        item.s3_key = dest_key
        item.size_bytes = head["ContentLength"]
    item.state = "ready"
    if body.tags:
        item.tags = {t: True for t in body.tags}
    db.commit()

    logger.info("Finalized seed: %s (%d bytes)", body.seed_key, item.size_bytes)
    return {
        "id": item.id,
        "s3_key": item.s3_key,
        "size_bytes": item.size_bytes,
        "state": "ready",
    }


@router.delete("/{item_id}", status_code=204)
def delete_item(
    item_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Delete a library item and its S3 object."""
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    _check_not_central(item)

    lib = db.query(Library).filter_by(id=item.library_id).first()
    if not lib or lib.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if item.s3_key:
        try:
            s3_storage.delete_file(item.s3_key)
        except Exception:
            logger.warning("Failed to delete S3 object %s", item.s3_key)

    db.delete(item)
    db.commit()


class ImportUrlRequest(BaseModel):
    url: str


@router.post("/{item_id}/import-url")
def import_from_url(
    item_id: str,
    body: ImportUrlRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Import a file from a URL via host — no data passes through the app."""
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    lib = db.query(Library).filter_by(id=item.library_id).first()
    if not lib or lib.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    ext = item.format if item.format != "qcow2" else "qcow2"
    s3_key = f"library/{user.id}/{item.id}/{item.name}.{ext}"

    item.s3_key = s3_key
    item.source_url = body.url
    item.state = "importing"
    db.commit()

    import threading

    def _host_download():
        from app.core.database import SessionLocal as SL
        from app.models.host import Host
        from app.services.s3_storage import _bucket, _get_s3_client
        from app.services.troshkad_client import (
            TroshkadError,
            check_disk_usage,
            start_job,
            wait_for_job,
        )

        sess = SL()
        try:
            it = sess.query(LibraryItem).filter_by(id=item_id).first()
            if not it:
                return

            host = (
                sess.query(Host)
                .filter_by(state="active", agent_status="connected")
                .first()
            )
            if not host:
                logger.error("Import %s: no active host available", item_id[:8])
                it.state = "error"
                sess.commit()
                return

            bucket = _bucket()

            disk = check_disk_usage(host)
            if disk.get("used_pct", 100) >= 90:
                free_gb = disk.get("free_bytes", 0) / (1024**3)
                logger.error(
                    "Import %s: host storage %d%% full (%.1f GB free)",
                    item_id[:8],
                    disk.get("used_pct", 100),
                    free_gb,
                )
                it.state = "error"
                sess.commit()
                return

            it.state = "importing"
            sess.commit()

            cache_path = f"/var/lib/troshka/tmp/import-{item_id[:8]}"
            s3_upload_url = f"s3://{bucket}/{s3_key}"
            from app.services.s3_storage import _get_s3_config

            s3_creds = _get_s3_config()

            try:
                job_id = start_job(
                    host,
                    "/library/import",
                    {
                        "download_url": body.url,
                        "cache_path": cache_path,
                        "s3_upload_url": s3_upload_url,
                        "aws_access_key_id": s3_creds.get("access_key_id", ""),
                        "aws_secret_access_key": s3_creds.get("secret_access_key", ""),
                        "aws_region": s3_creds.get("region", "us-east-1"),
                        "aws_endpoint_url": s3_creds.get("endpoint_url", ""),
                    },
                )
                job = wait_for_job(host, job_id, timeout=7200, poll_interval=10)

                if job["status"] == "failed":
                    logger.error(
                        "Import %s: troshkad job failed: %s",
                        item_id[:8],
                        job.get("error", "unknown"),
                    )
                    it.state = "error"
                    sess.commit()
                    return

                client = _get_s3_client()
                head = client.head_object(Bucket=bucket, Key=s3_key)
                it.size_bytes = head["ContentLength"]
                it.state = "ready"
                it.tags = None
                sess.commit()
                logger.info(
                    "Import %s complete via troshkad: %d bytes",
                    item_id[:8],
                    it.size_bytes,
                )

            except TroshkadError as e:
                logger.error("Import %s: troshkad error: %s", item_id[:8], e)
                it.state = "error"
                sess.commit()

        except Exception:
            logger.exception("Import failed for %s", item_id[:8])
            it = sess.query(LibraryItem).filter_by(id=item_id).first()
            if it:
                it.state = "error"
                sess.commit()
        finally:
            sess.close()

    threading.Thread(target=_host_download, daemon=True).start()

    return {"id": item.id, "state": "importing"}


@router.post("/{item_id}/cancel")
def cancel_import(
    item_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Cancel an in-progress import and clean up S3."""
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    lib = db.query(Library).filter_by(id=item.library_id).first()
    if not lib or lib.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if item.s3_key:
        from app.services.s3_storage import _bucket, _get_s3_client

        client = _get_s3_client()
        bucket = _bucket()
        # Abort any in-progress multipart uploads for this key
        try:
            mpus = client.list_multipart_uploads(Bucket=bucket, Prefix=item.s3_key)
            for u in mpus.get("Uploads", []):
                client.abort_multipart_upload(
                    Bucket=bucket, Key=u["Key"], UploadId=u["UploadId"]
                )
        except Exception:
            pass
        # Delete any partial object
        try:
            client.delete_object(Bucket=bucket, Key=item.s3_key)
        except Exception:
            pass

    db.delete(item)
    db.commit()
    return {"status": "cancelled"}


class ShareRequest(BaseModel):
    user_email: str
    permission: str = "use"


@router.post("/{item_id}/share")
def share_item(
    item_id: str,
    body: ShareRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Share a library item with another user."""
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    lib = db.query(Library).filter_by(id=item.library_id).first()
    if not lib or lib.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Only the owner can share")

    target_user = db.query(User).filter_by(email=body.user_email).first()
    if not target_user:
        raise HTTPException(status_code=404, detail=f"User {body.user_email} not found")
    if target_user.id == user.id:
        raise HTTPException(status_code=400, detail="Cannot share with yourself")

    existing = (
        db.query(LibraryShare)
        .filter_by(item_id=item_id, shared_with_id=target_user.id)
        .first()
    )
    if existing:
        existing.permission = body.permission
    else:
        db.add(
            LibraryShare(
                item_id=item_id,
                shared_with_id=target_user.id,
                permission=body.permission,
            )
        )
    db.commit()

    return {"shared_with": body.user_email, "permission": body.permission}


@router.delete("/{item_id}/share/{user_email}")
def unshare_item(
    item_id: str,
    user_email: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove sharing for a library item."""
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    lib = db.query(Library).filter_by(id=item.library_id).first()
    if not lib or lib.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Only the owner can unshare")

    target_user = db.query(User).filter_by(email=user_email).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    share = (
        db.query(LibraryShare)
        .filter_by(item_id=item_id, shared_with_id=target_user.id)
        .first()
    )
    if share:
        db.delete(share)
        db.commit()

    return {"unshared": user_email}


@router.post("/sync-central")
def sync_central(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Sync library items from the central read-only S4 bucket."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    from app.services.central_library import sync_central_library

    return sync_central_library(db, owner_id=user.id)


@router.post("/scan-s3")
def scan_s3(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Scan S3 bucket and import any library items not already in the DB."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    lib = _ensure_user_library(user, db)
    client = s3_storage._get_s3_client()
    bucket = s3_storage._bucket()

    imported = 0
    continuation_token = None

    while True:
        kwargs = {"Bucket": bucket, "Prefix": "library/", "MaxKeys": 1000}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        resp = client.list_objects_v2(**kwargs)

        for obj in resp.get("Contents", []):
            key = obj["Key"]
            parts = key.split("/")
            # Expected: library/{user_id}/{item_id}/{filename}.{ext}
            if len(parts) < 4:
                continue

            item_id = parts[2]
            filename = "/".join(parts[3:])  # handle nested paths
            size = obj.get("Size", 0)

            # Skip if already in DB
            if db.query(LibraryItem).filter_by(id=item_id).first():
                continue

            # Parse name and format from filename
            if "." in filename:
                name, fmt = filename.rsplit(".", 1)
            else:
                name, fmt = filename, "unknown"

            item_type = "iso" if fmt.lower() == "iso" else "image"

            item = LibraryItem(
                id=item_id,
                library_id=lib.id,
                name=name,
                type=item_type,
                format=fmt.lower(),
                size_bytes=size,
                s3_key=key,
                state="ready",
            )
            db.add(item)
            imported += 1

        if resp.get("IsTruncated"):
            continuation_token = resp.get("NextContinuationToken")
        else:
            break

    if imported:
        db.commit()

    # Scan snapshots/ and patterns/ — collect all objects grouped by ID
    import json

    from app.models.library import LibraryItemDisk
    from app.models.pattern import Pattern, PatternDisk

    def _scan_prefix(prefix):
        """List all S3 objects under a prefix, grouped by parent ID."""
        groups = {}  # id -> {metadata: dict|None, files: [{key, size}]}
        cont = None
        while True:
            kw = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
            if cont:
                kw["ContinuationToken"] = cont
            r = client.list_objects_v2(**kw)
            for obj in r.get("Contents", []):
                key = obj["Key"]
                parts = key.split("/")
                if len(parts) < 3:
                    continue
                obj_id = parts[1]
                if obj_id not in groups:
                    groups[obj_id] = {"metadata": None, "files": []}
                if key.endswith("/metadata.json"):
                    try:
                        meta_resp = client.get_object(Bucket=bucket, Key=key)
                        groups[obj_id]["metadata"] = json.loads(
                            meta_resp["Body"].read()
                        )
                    except Exception:
                        pass
                else:
                    groups[obj_id]["files"].append(
                        {"key": key, "size": obj.get("Size", 0)}
                    )
            if r.get("IsTruncated"):
                cont = r.get("NextContinuationToken")
            else:
                break
        return groups

    # Import snapshots
    snapshot_count = 0
    for item_id, group in _scan_prefix("snapshots/").items():
        if db.query(LibraryItem).filter_by(id=item_id).first():
            continue
        meta = group["metadata"]
        if meta:
            name = meta.get("name", f"snapshot-{item_id[:8]}")
            fmt = meta.get("format", "qcow2")
            size = meta.get("size_bytes", 0)
            os_variant = meta.get("os_variant")
            vm_config = meta.get("vm_config")
            tags = meta.get("tags")
        else:
            name = f"orphan-snapshot-{item_id[:8]}"
            fmt = "qcow2"
            size = sum(f["size"] for f in group["files"])
            os_variant = None
            vm_config = None
            tags = {"orphaned": True}

        item = LibraryItem(
            id=item_id,
            library_id=lib.id,
            name=name,
            type="image",
            format=fmt,
            size_bytes=size,
            os_variant=os_variant,
            vm_config=vm_config,
            tags=tags,
            state="ready",
        )
        db.add(item)
        db.flush()

        if meta and meta.get("disks"):
            for disk in meta["disks"]:
                db.add(
                    LibraryItemDisk(
                        library_item_id=item_id,
                        s3_key=disk["s3_key"],
                        format=disk.get("format", "qcow2"),
                        size_bytes=disk.get("size_bytes", 0),
                        virtual_size_bytes=disk.get("virtual_size_bytes", 0),
                        boot_order=disk.get("boot_order", 0),
                        state="available",
                    )
                )
        else:
            for i, f in enumerate(group["files"]):
                file_fmt = f["key"].rsplit(".", 1)[-1] if "." in f["key"] else "qcow2"
                db.add(
                    LibraryItemDisk(
                        library_item_id=item_id,
                        s3_key=f["key"],
                        format=file_fmt,
                        size_bytes=f["size"],
                        boot_order=i,
                        state="available",
                    )
                )
        snapshot_count += 1

    # Import patterns
    pattern_count = 0
    for pattern_id, group in _scan_prefix("patterns/").items():
        if db.query(Pattern).filter_by(id=pattern_id).first():
            continue
        meta = group["metadata"]
        if meta:
            pattern = Pattern(
                id=pattern_id,
                name=meta.get("name", f"pattern-{pattern_id[:8]}"),
                description=meta.get("description"),
                owner_id=user.id,
                visibility=meta.get("visibility", "private"),
                topology=meta.get("topology", {}),
                state="available",
                total_size_bytes=meta.get("total_size_bytes", 0),
                tags=meta.get("tags"),
            )
            db.add(pattern)
            db.flush()
            for disk in meta.get("disks", []):
                import uuid as _uuid

                db.add(
                    PatternDisk(
                        id=disk.get("id", str(_uuid.uuid4())),
                        pattern_id=pattern_id,
                        source_disk_id=disk.get("source_disk_id", ""),
                        source_vm_id=disk.get("source_vm_id", ""),
                        s3_key=disk["s3_key"],
                        format=disk.get("format", "qcow2"),
                        size_bytes=disk.get("size_bytes", 0),
                        virtual_size_bytes=disk.get("virtual_size_bytes", 0),
                        state="available",
                    )
                )
        else:
            total_size = sum(f["size"] for f in group["files"])
            pattern = Pattern(
                id=pattern_id,
                name=f"orphan-pattern-{pattern_id[:8]}",
                owner_id=user.id,
                visibility="private",
                topology={"nodes": [], "edges": []},
                state="available",
                total_size_bytes=total_size,
                tags={"orphaned": True},
            )
            db.add(pattern)
            db.flush()
            for f in group["files"]:
                import uuid as _uuid

                file_fmt = f["key"].rsplit(".", 1)[-1] if "." in f["key"] else "qcow2"
                disk_id = (
                    f["key"].split("/")[-1].rsplit(".", 1)[0]
                    if "/" in f["key"]
                    else str(_uuid.uuid4())
                )
                db.add(
                    PatternDisk(
                        id=str(_uuid.uuid4()),
                        pattern_id=pattern_id,
                        source_disk_id=disk_id,
                        source_vm_id="",
                        s3_key=f["key"],
                        format=file_fmt,
                        size_bytes=f["size"],
                        state="available",
                    )
                )
        pattern_count += 1

    if snapshot_count or pattern_count:
        db.commit()

    return {
        "imported": imported,
        "snapshots": snapshot_count,
        "patterns": pattern_count,
        "bucket": bucket,
    }
