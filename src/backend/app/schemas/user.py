import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


class UserCreate(BaseModel):
    email: str
    display_name: str | None = None
    role: str = "user"
    password: str | None = None
    access_starts_at: datetime.datetime | None = None
    access_ends_at: datetime.datetime | None = None

    @model_validator(mode="after")
    def _validate_window(self):
        start, end = self.access_starts_at, self.access_ends_at
        if start is not None and end is not None and start >= end:
            raise ValueError("access_starts_at must be before access_ends_at")
        return self


class UserUpdate(BaseModel):
    display_name: str | None = None
    role: str | None = None
    access_enabled: bool | None = None
    quota_overrides: dict | None = None
    access_starts_at: datetime.datetime | None = None
    access_ends_at: datetime.datetime | None = None


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    role: str
    auth_source: str
    created_at: datetime.datetime
    access_enabled: bool = True
    access_starts_at: datetime.datetime | None = None
    access_ends_at: datetime.datetime | None = None
    organization_id: str | None = None

    model_config = {"from_attributes": True}


class OrganizationAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID | None
