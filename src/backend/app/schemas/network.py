import datetime

from pydantic import BaseModel


class NetworkCreate(BaseModel):
    name: str
    cidr: str
    dhcp_enabled: bool = False
    dns_enabled: bool = False
    dns_domain: str | None = None
    dns_upstream: bool = False


class NetworkUpdate(BaseModel):
    name: str | None = None
    cidr: str | None = None
    dhcp_enabled: bool | None = None
    dns_enabled: bool | None = None
    dns_domain: str | None = None
    dns_upstream: bool | None = None


class NetworkResponse(BaseModel):
    id: str
    project_id: str
    name: str
    cidr: str
    dhcp_enabled: bool
    dns_enabled: bool
    dns_domain: str | None = None
    dns_upstream: bool
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class SecurityRuleCreate(BaseModel):
    direction: str
    protocol: str = "all"
    port_range_start: int | None = None
    port_range_end: int | None = None
    source_cidr: str | None = None
    action: str = "allow"
    priority: int = 100
    description: str | None = None


class SecurityRuleResponse(BaseModel):
    id: int
    network_id: str
    direction: str
    protocol: str
    port_range_start: int | None = None
    port_range_end: int | None = None
    source_cidr: str | None = None
    action: str
    priority: int
    description: str | None = None

    model_config = {"from_attributes": True}
