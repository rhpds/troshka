import datetime

from pydantic import BaseModel


class ProjectCreate(BaseModel):
    name: str
    description: str | None = None
    provider_id: str | None = None
    host_type: str = "shared"
    run_timer_hours: int | None = None
    lifetime_expires_at: datetime.datetime | None = None
    poweroff_mode: str = "simultaneous"


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    host_type: str | None = None
    run_timer_hours: int | None = None
    run_timer_max_ext_hours: int | None = None
    lifetime_expires_at: datetime.datetime | None = None
    poweroff_mode: str | None = None
    guest_permission: str | None = None
    state: str | None = None
    topology: dict | None = None
    tags: dict | None = None


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
    run_timer_hours: int | None = None
    poweroff_mode: str
    host_id: str | None = None
    host_instance_id: str | None = None
    host_ip: str | None = None
    host_provider_name: str | None = None
    topology: dict | None = None
    deployed_topology: dict | None = None
    deploy_error: str | None = None
    tags: dict | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = {"from_attributes": True}


class ProjectShareRequest(BaseModel):
    user_id: str
    permission: str = "view"
