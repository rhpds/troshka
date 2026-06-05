import datetime

from pydantic import BaseModel


class HostCreate(BaseModel):
    provider_id: str
    instance_type: str = "r8i.4xlarge"
    region: str | None = None
    host_type: str = "shared"


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
    created_at: datetime.datetime

    model_config = {"from_attributes": True}
