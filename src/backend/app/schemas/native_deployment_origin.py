"""Typed, secret-free immutable creation identities."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SupportIntent(Fact):
    kind: Literal["Secret", "RoleBinding"]
    name: str = Field(min_length=1)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class LaunchDescriptor(Fact):
    cluster_uid: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    root_name: str = Field(min_length=1)
    namespace_mode: Literal["managed", "external"]
    cr_spec_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    support_intents: tuple[SupportIntent, ...] = ()


class OriginIntent(LaunchDescriptor):
    schema_version: Literal["native-generic-origin-v1"] = "native-generic-origin-v1"
    mode: Literal["generic"] = "generic"
    id: UUID
    attempt_id: UUID
    operation_token: UUID
    project_id: UUID
    owner_id: UUID
    organization_id: UUID | None
    host_id: UUID
    provider_id: UUID


class NamespaceReceipt(Fact):
    intent_id: UUID
    cluster_uid: str
    namespace: str
    namespace_uid: str
    operation_token: UUID
    namespace_delete_authority: bool


class RootReceipt(NamespaceReceipt):
    root_name: str
    root_uid: str
    generation: int = Field(ge=1)
    cr_spec_digest: str
