import datetime

from pydantic import BaseModel


class UserCreate(BaseModel):
    email: str
    display_name: str | None = None
    role: str = "user"
    password: str | None = None


class UserUpdate(BaseModel):
    display_name: str | None = None
    role: str | None = None
    quota_overrides: dict | None = None


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    role: str
    auth_source: str
    created_at: datetime.datetime

    model_config = {"from_attributes": True}
