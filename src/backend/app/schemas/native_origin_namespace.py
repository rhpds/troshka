"""Namespace cleanup is an absence receipt, never a storage usage report."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from app.schemas.native_deployment_origin import Fact, NamespaceReceipt


class InventoryObject(Fact):
    api_version: str
    kind: str
    name: str
    uid: str
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class NamespaceInventory(Fact):
    namespace: NamespaceReceipt
    discovery_digest: str
    objects: tuple[InventoryObject, ...]
    support_digest: str
    writers_digest: str
    root_never_submitted: Literal[True] = True


class NamespaceCleanup(Fact):
    version: Literal["native-origin-namespace-v1"] = "native-origin-namespace-v1"
    id: UUID
    intent_id: UUID
    project_id: UUID
    mode: Literal["delete", "undeploy"]
    phase: Literal[
        "cancelling", "sealed", "namespace_delete_requested", "absent", "finalized"
    ] = "cancelling"
    inventory: NamespaceInventory | None = None
    delete_resource_version: str | None = None
    last_error: str | None = None
