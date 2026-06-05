import datetime

from pydantic import BaseModel


class ProviderCreate(BaseModel):
    name: str
    type: str
    config: str | None = None
    region: str | None = None


class ProviderUpdate(BaseModel):
    name: str | None = None
    config: str | None = None
    region: str | None = None
    state: str | None = None


class ProviderResponse(BaseModel):
    id: str
    name: str
    type: str
    region: str | None = None
    state: str
    created_at: datetime.datetime

    model_config = {"from_attributes": True}
