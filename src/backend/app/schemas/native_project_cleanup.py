"""Namespace retirement authority is independent of quota and live CR existence."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_serializer

from app.schemas.kubevirt_storage import Evidence, ImageKey, ProjectRoot, VolumeBinding
from app.schemas.native_online_enrollment import CurrentAncestryCustody
from app.schemas.native_snapshot_custody import SnapshotFacts


class CleanupAuthority(Evidence):
    source_kind: Literal["origin", "admin_custody"]
    source_id: UUID
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    project_id: UUID
    owner_id: UUID
    host_id: UUID
    provider_id: UUID
    cluster_uid: str
    namespace: str
    namespace_uid: str
    root_uid: str | None = None
    domain_id: UUID | None = None
    enrollment_id: UUID | None = None
    root: ProjectRoot | None = None


class SupportBoundary(Evidence):
    operation_id: UUID
    operation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    namespace_uid: str
    target_kind: Literal["Pod", "TroshkaProject"]
    target_uid: str


class StorageLiabilities(SnapshotFacts):
    bindings: list[VolumeBinding] = Field(default_factory=list)
    ancestry: list[CurrentAncestryCustody] = Field(default_factory=list)
    private_images: list[ImageKey] = Field(default_factory=list)
    shared_images: list[ImageKey] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    support_boundaries: list[SupportBoundary] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def legacy(self, handler):
        value = super().legacy(handler)
        if not self.support_boundaries:
            value.pop("support_boundaries", None)
        return value


class VMTarget(Evidence):
    kind: str = "VirtualMachine"
    name: str
    uid: str
    resource_version: str


class ProjectNamespaceCleanup(Evidence):
    version: Literal["native-project-namespace-v1"] = "native-project-namespace-v1"
    id: UUID
    authority: CleanupAuthority
    mode: Literal["delete", "undeploy"]
    phase: Literal[
        "fencing",
        "vm_delete_requested",
        "namespace_delete_requested",
        "namespace_absent",
        "orphan_reclaim",
        "settled",
        "finalized",
    ] = "fencing"
    liabilities: StorageLiabilities
    liability_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    prepared_at: datetime
    vm_targets: list[VMTarget] = Field(default_factory=list)
    cr_targets: list[VMTarget] = Field(default_factory=list)
    namespace_resource_version: str | None = None
    namespace_delete_at: datetime | None = None
    settlement_report_id: UUID | None = None
    settled_at: datetime | None = None
    last_error: str | None = None
    marker: dict | None = None
    enrollment_boundary: dict | None = None


class CleanupStorageScope(Evidence):
    """Read projection only: never a reservation or another orchestration state."""

    version: Literal["native-project-namespace-v1"] = "native-project-namespace-v1"
    intent_id: UUID
    operation_id: str
    domain_id: UUID
    root: ProjectRoot
    phase: str
    inventory_digest: str
    owned_images: list[ImageKey]
    bindings: list[VolumeBinding]
    teardown_at: datetime
    liabilities: StorageLiabilities
    settlement_report_id: UUID | None = None
    settled_at: datetime | None = None


class ProjectRetirement(Evidence):
    cleanup_id: UUID
    liability_digest: str
    root: ProjectRoot
    private_images: list[ImageKey]
    shared_images: list[ImageKey]
    observed_at: datetime
