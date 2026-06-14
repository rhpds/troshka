import datetime

from pydantic import BaseModel


class SnapshotCreate(BaseModel):
    name: str
    description: str | None = None


class LibraryItemDiskResponse(BaseModel):
    id: str
    s3_key: str
    format: str
    size_bytes: int
    virtual_size_bytes: int
    boot_order: int
    checksum_sha256: str | None = None
    state: str
    model_config = {"from_attributes": True}


class SnapshotResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    type: str
    format: str
    state: str
    vm_config: dict | None = None
    source_vm_id: str | None = None
    created_at: datetime.datetime
    model_config = {"from_attributes": True}
