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
    """Import a file from a URL — downloads server-side and uploads to S3."""
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
    def _download_and_upload():
        from app.core.database import SessionLocal as SL
        from app.services.s3_storage import _get_s3_client, _bucket
        import requests

        sess = SL()
        try:
            it = sess.query(LibraryItem).filter_by(id=item_id).first()
            if not it:
                return

            client = _get_s3_client()
            bucket = _bucket()

            # Stream download from URL and multipart upload to S3
            resp = requests.get(body.url, stream=True, timeout=(30, 60))
            resp.raise_for_status()

            mpu = client.create_multipart_upload(Bucket=bucket, Key=s3_key, ContentType="application/octet-stream")
            upload_id = mpu["UploadId"]

            import threading
            from concurrent.futures import ThreadPoolExecutor, Future

            parts = []
            parts_lock = threading.Lock()
            part_num = 0
            chunk_size = 200 * 1024 * 1024
            buf = b""
            total_downloaded = 0
            total_uploaded = 0
            upload_futures: list[Future] = []

            def upload_part_async(part_data, pn):
                r = client.upload_part(Bucket=bucket, Key=s3_key, UploadId=upload_id, PartNumber=pn, Body=part_data)
                with parts_lock:
                    parts.append({"PartNumber": pn, "ETag": r["ETag"]})
                return len(part_data)

            executor = ThreadPoolExecutor(max_workers=2)
            last_commit = 0

            it.state = "downloading"
            sess.commit()

            for data in resp.iter_content(chunk_size=8 * 1024 * 1024):
                buf += data
                total_downloaded += len(data)

                if total_downloaded - last_commit >= 50 * 1024 * 1024:
                    it.size_bytes = total_downloaded
                    it.state = "downloading"
                    sess.commit()
                    last_commit = total_downloaded

                while len(buf) >= chunk_size:
                    part_num += 1
                    part_data = buf[:chunk_size]
                    buf = buf[chunk_size:]

                    it.state = "uploading_s3"
                    it.size_bytes = total_downloaded
                    sess.commit()

                    future = executor.submit(upload_part_async, part_data, part_num)
                    upload_futures.append(future)
                    logger.info("Import %s: queued part %d, downloaded %d MB", item_id[:8], part_num, total_downloaded // (1024*1024))

            # Upload remaining buffer
            if buf:
                part_num += 1
                future = executor.submit(upload_part_async, buf, part_num)
                upload_futures.append(future)

            # Wait for all uploads to finish
            it.state = "uploading_s3"
            sess.commit()
            for f in upload_futures:
                total_uploaded += f.result()
                it.size_bytes = total_uploaded
                sess.commit()

            executor.shutdown(wait=True)

            client.complete_multipart_upload(Bucket=bucket, Key=s3_key, UploadId=upload_id, MultipartUpload={"Parts": parts})

            head = client.head_object(Bucket=bucket, Key=s3_key)
            it.size_bytes = head["ContentLength"]
            it.state = "ready"
            sess.commit()
            logger.info("Import %s complete: %d bytes", item_id[:8], it.size_bytes)
        except Exception:
            logger.exception("Import failed for %s", item_id[:8])
            it = sess.query(LibraryItem).filter_by(id=item_id).first()
            if it:
                it.state = "error"
                sess.commit()
        finally:
            sess.close()

    threading.Thread(target=_download_and_upload, daemon=True).start()

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
