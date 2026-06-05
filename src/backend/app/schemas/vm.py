import datetime

from pydantic import BaseModel


class VMCreate(BaseModel):
    name: str
    description: str | None = None
    vcpus: int = 2
    ram_mb: int = 4096
    os_template: str | None = None
    boot_method: str = "template"
    boot_iso_id: str | None = None
    boot_order: int = 0
    console_type: str = "auto"
    cloud_init: str | None = None


class VMUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    vcpus: int | None = None
    ram_mb: int | None = None
    os_template: str | None = None
    boot_method: str | None = None
    boot_order: int | None = None
    console_type: str | None = None
    cloud_init: str | None = None


class VMResponse(BaseModel):
    id: str
    project_id: str
    host_id: str | None = None
    name: str
    description: str | None = None
    vcpus: int
    ram_mb: int
    os_template: str | None = None
    state: str
    boot_method: str
    boot_order: int
    console_type: str
    ip_address: str | None = None
    mac_address: str | None = None
    created_at: datetime.datetime

    model_config = {"from_attributes": True}
