import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AssistantScope = Literal["project", "guest"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(StrictModel):
    pass


class VMArgs(StrictModel):
    vm_id: uuid.UUID


class NetworkArgs(VMArgs):
    source: Literal["backend", "gateway", "ops", "guest"] = "backend"
    endpoint_id: uuid.UUID | None = None
    external_port: int | None = Field(default=None, ge=1, le=65535)


class GuestArgs(VMArgs):
    check: Literal["addresses", "routes", "listeners", "service_status"]


class ClusterArgs(StrictModel):
    cluster_key: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    check: Literal["version", "operator_health"] = "version"


class RunArgs(StrictModel):
    run_id: uuid.UUID


class HelpArgs(StrictModel):
    query: str = Field(min_length=1, max_length=200)


class ToolRequest(StrictModel):
    name: str
    arguments: dict = Field(default_factory=dict)


class Evidence(StrictModel):
    evidence_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    check: str
    status: Literal["ok", "unknown", "denied", "unsupported", "timeout", "error"] = "ok"
    observed_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    source: str = "persisted project state"
    project_id: str
    resource_refs: list[str] = Field(default_factory=list)
    facts: dict = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    truncated: bool = False


@dataclass(frozen=True)
class AuthorizedContext:
    actor_id: str
    conversation_id: str
    project_id: str
    grant_id: str
    grant_expires_at: dt.datetime
