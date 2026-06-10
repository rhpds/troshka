import datetime

from pydantic import BaseModel


class DnsProviderCreate(BaseModel):
    name: str
    type: str
    config: dict


class DnsProviderUpdate(BaseModel):
    name: str | None = None
    config: dict | None = None


class DnsProviderResponse(BaseModel):
    id: str
    name: str
    type: str
    config: dict
    created_at: datetime.datetime

    model_config = {"from_attributes": True}
