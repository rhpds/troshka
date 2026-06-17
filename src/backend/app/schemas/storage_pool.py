import datetime

from pydantic import BaseModel


class StoragePoolCreate(BaseModel):
    name: str
    mode: str  # "local", "shared-fsx", "shared-byo", "shared-ceph-nfs", "shared-netapp", "shared-azure-files"
    provider_id: str
    az: str | None = None
    instance_types: list[str] | None = None  # for AZ probing
    fsx_throughput_mbps: int | None = None
    fsx_storage_gb: int | None = None
    nfs_endpoint: str | None = None
    # GCP NetApp Volumes
    netapp_capacity_gb: int | None = None
    netapp_service_level: str | None = None
    # Azure Files NFS
    azure_files_capacity_gb: int | None = None
    azure_files_iops: int | None = None
    azure_files_throughput: int | None = None


class StoragePoolUpdate(BaseModel):
    fsx_throughput_mbps: int | None = None
    fsx_storage_gb: int | None = None
    nfs_endpoint: str | None = None
    auto_extend_enabled: bool | None = None
    auto_extend_threshold_pct: int | None = None
    auto_extend_increment_gb: int | None = None
    auto_extend_max_gb: int | None = None
    pb_auto_sleep_minutes: int | None = None


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
    nfs_port: int | None = None
    # GCP NetApp Volumes
    netapp_pool_id: str | None = None
    netapp_mount_ip: str | None = None
    netapp_volume_name: str | None = None
    netapp_service_level: str | None = None
    netapp_capacity_gb: int | None = None
    # Azure Files NFS
    azure_storage_account: str | None = None
    azure_file_share_name: str | None = None
    azure_file_share_url: str | None = None
    azure_files_capacity_gb: int | None = None
    azure_files_iops: int | None = None
    azure_files_throughput: int | None = None
    status: str
    provider_id: str
    host_count: int = 0
    worker_host_id: str | None = None
    worker_instance_type: str | None = None
    worker_status: str | None = None
    worker_error: str | None = None
    worker_ip: str | None = None
    worker_private_ip: str | None = None
    worker_instance_id: str | None = None
    worker_agent_version: str | None = None
    created_at: datetime.datetime
    auto_extend_enabled: bool = False
    auto_extend_threshold_pct: int = 80
    auto_extend_increment_gb: int = 64
    auto_extend_max_gb: int | None = None
    pb_auto_sleep_minutes: int = 30
    pb_last_activity_at: datetime.datetime | None = None

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
