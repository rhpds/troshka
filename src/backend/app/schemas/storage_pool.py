import datetime

from pydantic import BaseModel


class StoragePoolCreate(BaseModel):
    name: str
    mode: str  # "local", "shared-fsx", "shared-byo"
    provider_id: str
    az: str | None = None
    instance_types: list[str] | None = None  # for AZ probing
    fsx_throughput_mbps: int | None = None
    fsx_storage_gb: int | None = None
    nfs_endpoint: str | None = None


class StoragePoolUpdate(BaseModel):
    fsx_throughput_mbps: int | None = None
    fsx_storage_gb: int | None = None
    nfs_endpoint: str | None = None
    auto_extend_enabled: bool | None = None
    auto_extend_threshold_pct: int | None = None
    auto_extend_increment_gb: int | None = None
    auto_extend_max_gb: int | None = None


class StoragePoolResponse(BaseModel):
    id: str
    name: str
    mode: str
    az: str | None = None
    subnet_id: str | None = None
    fsx_filesystem_id: str | None = None
    fsx_dns_name: str | None = None
    fsx_mount_ip: str | None = None
    fsx_throughput_mbps: int | None = None
    fsx_storage_gb: int | None = None
    nfs_endpoint: str | None = None
    status: str
    provider_id: str
    host_count: int = 0
    worker_host_id: str | None = None
    worker_instance_type: str | None = None
    created_at: datetime.datetime
    auto_extend_enabled: bool = False
    auto_extend_threshold_pct: int = 80
    auto_extend_increment_gb: int = 64
    auto_extend_max_gb: int | None = None

    model_config = {"from_attributes": True}


class SharedCacheEntryResponse(BaseModel):
    id: str
    item_type: str
    item_id: str
    status: str
    file_path: str
    size_bytes: int | None = None
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class AzProbeResult(BaseModel):
    az: str
    supported_types: list[str]
    unsupported_types: list[str]


class AzProbeResponse(BaseModel):
    results: list[AzProbeResult]
    recommended_az: str | None = None
