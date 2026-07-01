import datetime

from pydantic import BaseModel


class ProviderCreate(BaseModel):
    name: str
    type: str
    config: str | None = None
    region: str | None = None
    endpoint_url: str | None = None
    bucket: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    default_region: str | None = None
    default_image: str | None = None
    vpc_id: str | None = None
    subnet_id: str | None = None
    api_url: str | None = None
    token: str | None = None
    namespace: str | None = None
    verify_ssl: bool | None = None
    iso_pvc: str | None = None
    gcp_project_id: str | None = None
    service_account_json: str | None = None
    azure_tenant_id: str | None = None
    azure_client_id: str | None = None
    azure_client_secret: str | None = None
    azure_subscription_id: str | None = None
    azure_location: str | None = None


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
