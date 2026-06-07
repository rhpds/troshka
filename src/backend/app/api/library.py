"""
Library API — manage ISOs and disk images in S3.
"""
import hashlib
import logging

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

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
    shared_ids = [s.item_id for s in db.query(LibraryShare.item_id).filter_by(shared_with_id=user.id).all()]

    query = db.query(LibraryItem).filter(
        or_(
            LibraryItem.library_id == lib.id,
            LibraryItem.id.in_(shared_ids) if shared_ids else False,
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
        }
        for i in items
    ]


@router.get("/{item_id}")
def get_item(item_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
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


@router.patch("/{item_id}")
def update_item(item_id: str, body: LibraryItemUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    lib = db.query(Library).filter_by(id=item.library_id).first()
    if not lib or lib.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    if body.name is not None:
        item.name = body.name
    if body.description is not None:
        item.description = body.description
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
    existing = db.query(LibraryItem).filter_by(library_id=lib.id, name=body.name).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"You already have an item named \"{body.name}\"")
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
    from pydantic import BaseModel as BM
    import math

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

    from app.services.s3_storage import _get_s3_client, _bucket
    client = _get_s3_client()

    # Parse file_size from query param
    from fastapi import Request
    # We'll receive file_size in the JSON body instead
    return _do_start_upload(client, _bucket(), s3_key, item_id)


def _do_start_upload(client, bucket, s3_key, item_id):
    mpu = client.create_multipart_upload(Bucket=bucket, Key=s3_key, ContentType="application/octet-stream")
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

    from app.services.s3_storage import _get_s3_client, _bucket
    client = _get_s3_client()
    url = client.generate_presigned_url(
        "upload_part",
        Params={"Bucket": _bucket(), "Key": item.s3_key, "UploadId": upload_id, "PartNumber": part_number},
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

    from app.services.s3_storage import _get_s3_client, _bucket
    client = _get_s3_client()
    try:
        client.complete_multipart_upload(
            Bucket=_bucket(),
            Key=item.s3_key,
            UploadId=body.upload_id,
            MultipartUpload={"Parts": [{"PartNumber": p["part_number"], "ETag": p["etag"]} for p in body.parts]},
        )

        head = client.head_object(Bucket=_bucket(), Key=item.s3_key)
        item.size_bytes = head["ContentLength"]
        item.state = "ready"
        db.commit()
        logger.info("Library item %s upload complete: %s (%d bytes)", item.id, item.s3_key, item.size_bytes)
        return {"id": item.id, "state": "ready", "size_bytes": item.size_bytes}
    except Exception as e:
        logger.exception("Upload completion failed for %s: %s", item_id, e)
        item.state = "error"
        db.commit()
        raise HTTPException(status_code=500, detail="Upload completion failed")


@router.delete("/{item_id}", status_code=204)
def delete_item(item_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Delete a library item and its S3 object."""
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

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
    item.state = "importing"
    db.commit()

    import threading
    def _host_download():
        from app.core.database import SessionLocal as SL
        from app.services.s3_storage import _get_s3_client, _bucket
        from app.services.deploy_service import run_ssh_script
        from app.models.host import Host
        import time as _time

        sess = SL()
        try:
            it = sess.query(LibraryItem).filter_by(id=item_id).first()
            if not it:
                return

            host = sess.query(Host).filter_by(state="active", agent_status="connected").first()
            if not host or not host.ip_address or not host.private_key:
                logger.error("Import %s: no active host available", item_id[:8])
                it.state = "error"
                sess.commit()
                return

            client = _get_s3_client()
            bucket = _bucket()

            # Check disk space before downloading
            from app.services.deploy_service import check_host_disk_space
            disk = check_host_disk_space(host.ip_address, host.private_key)
            if disk["used_pct"] >= 90:
                free_gb = disk["free_bytes"] / (1024 ** 3)
                logger.error("Import %s: host storage %d%% full (%.1f GB free)", item_id[:8], disk["used_pct"], free_gb)
                it.state = "error"
                sess.commit()
                return

            it.state = "downloading"
            sess.commit()

            # Step 1: Download file on host in background, poll size
            cache_path = f"/var/lib/troshka/tmp/import-{item_id[:8]}"
            run_ssh_script(host.ip_address, host.private_key, f"mkdir -p /var/lib/troshka/tmp", timeout=10)
            run_ssh_script(host.ip_address, host.private_key,
                f"nohup bash -c \"curl -sfL -o {cache_path} '{body.url}' && echo DONE > {cache_path}.status || echo FAIL > {cache_path}.status\" > /dev/null 2>&1 &",
                timeout=15)

            # Poll file size while downloading
            import time as _time
            while True:
                _time.sleep(5)
                poll = run_ssh_script(host.ip_address, host.private_key,
                    f"cat {cache_path}.status 2>/dev/null; stat -c%s {cache_path} 2>/dev/null || echo 0",
                    timeout=10)
                lines = [l.strip() for l in poll["output"].strip().split("\n") if not l.strip().startswith("Warning:")]
                status_line = lines[0] if lines else ""
                size_line = lines[-1] if len(lines) > 1 else lines[0] if lines else "0"

                if status_line == "DONE":
                    if size_line.isdigit():
                        it.size_bytes = int(size_line)
                    it.state = "uploading_s3"
                    sess.commit()
                    run_ssh_script(host.ip_address, host.private_key, f"rm -f {cache_path}.status", timeout=10)
                    break
                elif status_line == "FAIL":
                    logger.error("Import %s: download failed on host", item_id[:8])
                    it.state = "error"
                    sess.commit()
                    run_ssh_script(host.ip_address, host.private_key, f"rm -f {cache_path} {cache_path}.status", timeout=10)
                    return
                else:
                    # Still downloading — update size
                    if size_line.isdigit() and int(size_line) > 0:
                        it.size_bytes = int(size_line)
                        sess.commit()

            # Step 2: Split file on host
            file_size = it.size_bytes or 0
            chunk_size = 500 * 1024 * 1024
            tmp_dir = "/var/lib/troshka/tmp"
            prefix = f"part-{item_id[:8]}"

            split_result = run_ssh_script(host.ip_address, host.private_key,
                f"cd {tmp_dir} && split -b {chunk_size} -d {cache_path} {prefix}- && ls -1 {tmp_dir}/{prefix}-* | wc -l",
                timeout=300)
            if not split_result["success"]:
                logger.error("Import %s: split failed", item_id[:8])
                it.state = "error"
                sess.commit()
                return

            num_parts = 0
            for line in split_result["output"].strip().split("\n"):
                if line.strip().isdigit():
                    num_parts = int(line.strip())

            # Step 3: Upload each part individually with progress
            mpu = client.create_multipart_upload(Bucket=bucket, Key=s3_key, ContentType="application/octet-stream")
            upload_id = mpu["UploadId"]
            parts = []
            uploaded_bytes = 0

            it.state = "uploading_s3"
            it.tags = {"total_parts": num_parts, "uploaded_parts": 0}
            sess.commit()

            for pn in range(1, num_parts + 1):
                idx = f"{pn - 1:02d}"
                part_file = f"{tmp_dir}/{prefix}-{idx}"
                url = client.generate_presigned_url(
                    "upload_part",
                    Params={"Bucket": bucket, "Key": s3_key, "UploadId": upload_id, "PartNumber": pn},
                    ExpiresIn=14400,
                )

                result = run_ssh_script(host.ip_address, host.private_key,
                    f"ETAG=$(curl -sfL -X PUT -T {part_file} '{url}' -D- -o /dev/null | grep -i etag | tr -d '\\r' | awk '{{print $2}}') && rm -f {part_file} && echo $ETAG",
                    timeout=600)

                if result["success"]:
                    etag = result["output"].strip().split("\n")[-1].strip()
                    if etag:
                        parts.append({"PartNumber": pn, "ETag": etag})
                        uploaded_bytes += chunk_size
                        it.tags = {"total_parts": num_parts, "uploaded_parts": pn}
                        sess.commit()
                        logger.info("Import %s: part %d/%d uploaded", item_id[:8], pn, num_parts)
                    else:
                        logger.error("Import %s: no etag for part %d", item_id[:8], pn)
                    client.abort_multipart_upload(Bucket=bucket, Key=s3_key, UploadId=upload_id)
                    it.state = "error"
                    sess.commit()
                else:
                    logger.error("Import %s: part %d upload failed: %s", item_id[:8], pn, result["output"][-200:])
                    client.abort_multipart_upload(Bucket=bucket, Key=s3_key, UploadId=upload_id)
                    run_ssh_script(host.ip_address, host.private_key, f"rm -f {cache_path} {tmp_dir}/{prefix}-*", timeout=15)
                    it.state = "error"
                    sess.commit()
                    return

            # Complete multipart upload
            run_ssh_script(host.ip_address, host.private_key, f"rm -f {cache_path}", timeout=15)
            if parts:
                client.complete_multipart_upload(Bucket=bucket, Key=s3_key, UploadId=upload_id, MultipartUpload={"Parts": parts})
                head = client.head_object(Bucket=bucket, Key=s3_key)
                it.size_bytes = head["ContentLength"]
                it.state = "ready"
                it.tags = None
                sess.commit()
                logger.info("Import %s complete via host: %d bytes", item_id[:8], it.size_bytes)
            else:
                client.abort_multipart_upload(Bucket=bucket, Key=s3_key, UploadId=upload_id)
                it.state = "error"
                sess.commit()
        except Exception:
            logger.exception("Import failed for %s", item_id[:8])
            it = sess.query(LibraryItem).filter_by(id=item_id).first()
            if it:
                it.state = "error"
                sess.commit()
        finally:
            # Always clean up temp files on the host
            try:
                if host and host.ip_address and host.private_key:
                    run_ssh_script(host.ip_address, host.private_key,
                        f"rm -f /var/lib/troshka/tmp/import-{item_id[:8]}* /var/lib/troshka/tmp/part-{item_id[:8]}-*",
                        timeout=15)
            except Exception:
                pass
            sess.close()

    threading.Thread(target=_host_download, daemon=True).start()

    return {"id": item.id, "state": "importing"}


@router.post("/{item_id}/cancel")
def cancel_import(item_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Cancel an in-progress import and clean up S3."""
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    lib = db.query(Library).filter_by(id=item.library_id).first()
    if not lib or lib.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if item.s3_key:
        from app.services.s3_storage import _get_s3_client, _bucket
        client = _get_s3_client()
        bucket = _bucket()
        # Abort any in-progress multipart uploads for this key
        try:
            mpus = client.list_multipart_uploads(Bucket=bucket, Prefix=item.s3_key)
            for u in mpus.get("Uploads", []):
                client.abort_multipart_upload(Bucket=bucket, Key=u["Key"], UploadId=u["UploadId"])
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
def share_item(item_id: str, body: ShareRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
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

    existing = db.query(LibraryShare).filter_by(item_id=item_id, shared_with_id=target_user.id).first()
    if existing:
        existing.permission = body.permission
    else:
        db.add(LibraryShare(item_id=item_id, shared_with_id=target_user.id, permission=body.permission))
    db.commit()

    return {"shared_with": body.user_email, "permission": body.permission}


@router.delete("/{item_id}/share/{user_email}")
def unshare_item(item_id: str, user_email: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
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

    share = db.query(LibraryShare).filter_by(item_id=item_id, shared_with_id=target_user.id).first()
    if share:
        db.delete(share)
        db.commit()

    return {"unshared": user_email}
