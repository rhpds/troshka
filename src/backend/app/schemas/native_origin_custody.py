"""Secret-free origin authority, distinct from administrator custody."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_serializer, model_validator

from app.schemas.kubevirt_storage import (
    BindingRetirementReference,
    Evidence,
    ProjectRoot,
    Text,
    VolumeBinding,
)
from app.schemas.kubevirt_storage_enrollment import (
    EnrollmentRequest,
    VerificationPins,
    VerifiedEnrollment,
)
from app.schemas.native_deployment_origin import OriginIntent, RootReceipt
from app.schemas.native_online_enrollment import CurrentAncestryCustody
from app.schemas.native_snapshot_custody import SnapshotFacts


class PendingOriginClaim(Evidence):
    """An observed request is debt, never zero usage or terminal cancellation."""

    pvc_uid: Text
    pvc_name: Text
    spec_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    data_volume_uid: Text | None = None
    reason: Literal["request_lifecycle_unresolved"] = "request_lifecycle_unresolved"


class OriginCustodyRequest(SnapshotFacts):
    request_id: UUID
    intent: OriginIntent
    root_receipt: RootReceipt
    root: ProjectRoot
    enrollment: EnrollmentRequest
    pins: VerificationPins
    previous_enrollment_id: UUID
    previous_enrollment_digest: str
    predecessor_id: UUID | None
    predecessor_digest: str | None
    profile_digest: str
    admission_digest: str
    mode: Literal[
        "enroll",
        "removal",
        "complete",
        "cleanup",
        "retirement",
        "producer",
        "snapshot-reconcile",
    ]
    pvc_uids: list[Text] = Field(default_factory=list, max_length=100)
    snapshot_read_state: dict | None = None
    cleanup_read_state: dict | None = None

    @model_validator(mode="after")
    def cleanup_requires_fence(self):
        if self.mode == "cleanup" and self.cleanup_read_state is None:
            raise ValueError("Cleanup custody requires retained cancellation fence")
        return self

    @model_serializer(mode="wrap")
    def legacy(self, handler):
        value = super().legacy(handler)
        if self.snapshot_read_state is None:
            value.pop("snapshot_read_state", None)
        if self.cleanup_read_state is None:
            value.pop("cleanup_read_state", None)
        return value


class VerifiedSnapshotReconciliation(SnapshotFacts):
    prepared: OriginCustodyRequest
    verified_at: datetime
    digest: str


class VerifiedOriginCustody(SnapshotFacts):
    version: Literal["native-origin-custody-v1"] = "native-origin-custody-v1"
    prepared: OriginCustodyRequest
    enrollment: VerifiedEnrollment
    object_identities: list[dict] = Field(default_factory=list, max_length=1000)
    pvc_names: dict[str, str] = Field(default_factory=dict, max_length=10000)
    bindings: list[VolumeBinding] = Field(default_factory=list, max_length=10000)
    ancestry: list[CurrentAncestryCustody] = Field(
        default_factory=list, max_length=10000
    )
    pending_claims: list[PendingOriginClaim] = Field(
        default_factory=list, max_length=10000
    )
    verified_at: datetime
    digest: str


class OriginCustodyReceipt(SnapshotFacts):
    """Cumulative append-only liabilities; complete denotes latest namespace scan."""

    verified: VerifiedOriginCustody
    publication: OriginCustodyRequest
    complete: bool
    enrollment_id: UUID
    bindings: list[VolumeBinding]
    ancestry: list[CurrentAncestryCustody]
    pending_claims: list[PendingOriginClaim]
    pvc_names: dict[str, str]
    binding_retirements: list[BindingRetirementReference] = Field(default_factory=list)
