import datetime

from pydantic import BaseModel


class ProjectCreate(BaseModel):
    name: str
    description: str | None = None
    provider_id: str | None = None
    host_type: str = "shared"
    auto_stop_minutes: int | None = None
    auto_delete_minutes: int | None = None
    poweroff_mode: str = "simultaneous"


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    host_type: str | None = None
    auto_stop_minutes: int | None = None
    auto_delete_minutes: int | None = None
    poweroff_mode: str | None = None
    guest_permission: str | None = None
    state: str | None = None
    topology: dict | None = None
    tags: dict | None = None
    guid: str | None = None
    clock_target: datetime.datetime | None = None
    guest_exec_enabled: bool | None = None


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    owner_id: str
    provider_id: str | None = None
    host_type: str
    state: str
    public_token: str | None = None
    guest_permission: str
    auto_stop_minutes: int | None = None
    auto_stop_expires_at: datetime.datetime | None = None
    auto_delete_minutes: int | None = None
    auto_stopped: bool = False
    lifetime_expires_at: datetime.datetime | None = None
    poweroff_mode: str
    host_id: str | None = None
    host_instance_id: str | None = None
    host_ip: str | None = None
    host_provider_name: str | None = None
    host_provider_type: str | None = None
    topology: dict | None = None
    deployed_topology: dict | None = None
    deploy_error: str | None = None
    tags: dict | None = None
    guid: str | None = None
    clock_target: datetime.datetime | None = None
    guest_exec_enabled: bool = True
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = {"from_attributes": True}


class ProjectShareRequest(BaseModel):
    user_id: str
    permission: str = "view"
