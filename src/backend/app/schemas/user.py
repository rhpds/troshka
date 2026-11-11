import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class UserCreate(BaseModel):
    email: str
    display_name: str | None = None
    role: str = "user"
    password: str | None = None


class UserUpdate(BaseModel):
    display_name: str | None = None
    role: str | None = None
    access_enabled: bool | None = None
    quota_overrides: dict | None = None


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    role: str
    auth_source: str
    created_at: datetime.datetime
    access_enabled: bool = True
    organization_id: str | None = None

    model_config = {"from_attributes": True}


class OrganizationAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID | None
