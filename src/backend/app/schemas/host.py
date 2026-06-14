import datetime

from pydantic import BaseModel


class HostCreate(BaseModel):
    provider_id: str
    instance_type: str = "r8i.4xlarge"
    region: str | None = None
    host_type: str = "shared"
    storage_pool_id: str | None = None


class HostResponse(BaseModel):
    id: str
    provider_id: str | None = None
    instance_id: str | None = None
    instance_type: str | None = None
    region: str | None = None
    state: str
    host_type: str
    total_vcpus: int
    total_ram_mb: int
    used_vcpus: int
    used_ram_mb: int
    ip_address: str | None = None
    agent_status: str
    storage_size_gb: int = 500
    max_eips: int = 0
    used_eips: int = 0
    agent_version: str | None = None
    last_health_at: datetime.datetime | None = None
    created_at: datetime.datetime
    storage_pool_id: str | None = None
    storage_warnings: list | None = None
    auto_extend_enabled: bool = False
    auto_extend_threshold_pct: int = 80
    auto_extend_increment_gb: int = 100
    auto_extend_max_gb: int | None = None
    console_domain: str | None = None

    model_config = {"from_attributes": True}
