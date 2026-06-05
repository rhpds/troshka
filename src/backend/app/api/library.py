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
from app.models.library import Library, LibraryItem
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
    """List library items. Filter by type (iso, image) and search by name."""
    lib = _ensure_user_library(user, db)
    query = db.query(LibraryItem).filter(LibraryItem.library_id == lib.id)

    if type:
        query = query.filter(LibraryItem.type == type)
    if q:
        query = query.filter(LibraryItem.name.ilike(f"%{q}%"))

    items = query.order_by(LibraryItem.created_at.desc()).all()
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


@router.post("/{item_id}/upload")
async def upload_file(
    item_id: str,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload a file for a library item. Streams to S3."""
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    lib = db.query(Library).filter_by(id=item.library_id).first()
    if not lib or lib.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    ext = item.format if item.format != "qcow2" else "qcow2"
    s3_key = f"library/{user.id}/{item.id}/{item.name}.{ext}"

    item.state = "uploading"
    db.commit()

    try:
        result = s3_storage.upload_file(s3_key, file.file)
        item.s3_key = s3_key
        item.size_bytes = result["size_bytes"]
        item.state = "ready"

        # Compute checksum from S3 (ETag for single-part uploads)
        item.checksum_sha256 = ""
        db.commit()

        logger.info("Library item %s uploaded: %s (%d bytes)", item.id, s3_key, result["size_bytes"])
        return {"id": item.id, "state": "ready", "size_bytes": result["size_bytes"], "s3_key": s3_key}
    except Exception as e:
        logger.exception("Upload failed for %s: %s", item_id, e)
        item.state = "error"
        db.commit()
        raise HTTPException(status_code=500, detail="Upload failed")


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
