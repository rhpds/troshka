import datetime

from pydantic import BaseModel


class DiskCreate(BaseModel):
    name: str
    size_gb: int = 20
    format: str = "qcow2"
    boot_order: int = 0


class DiskUpdate(BaseModel):
    name: str | None = None
    size_gb: int | None = None


class DiskResponse(BaseModel):
    id: str
    project_id: str
    vm_id: str | None = None
    name: str
    size_gb: int
    format: str
    boot_order: int
    attached: bool
    created_at: datetime.datetime

    model_config = {"from_attributes": True}
