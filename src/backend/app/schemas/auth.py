from pydantic import BaseModel


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user_id: str
    email: str
    display_name: str | None = None
    role: str


class UserIdentity(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    role: str
    auth_source: str | None = None

    model_config = {"from_attributes": True}
