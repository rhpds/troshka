"""Admin-only online custody evidence; never a creation or power proof."""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.schemas.kubevirt_storage import (
    CacheBinding,
    Evidence,
    ImageKey,
    SnapshotKey,
    Text,
    VolumeBinding,
)
from app.schemas.kubevirt_storage_enrollment import (
    EnrollmentRequest,
    VerifiedEnrollment,
)


class CurrentAncestryCustody(Evidence):
    """Current administrator custody, explicitly not a clone creation receipt."""

    profile: Literal["native-current-ancestry-v1"] = "native-current-ancestry-v1"
    binding: VolumeBinding
    source: CacheBinding
    intermediate: ImageKey
    head_parent: SnapshotKey
    intermediate_parent: SnapshotKey
    verified_at: datetime

    @model_validator(mode="after")
    def chain(self):
        keys = [self.binding.image_key, self.intermediate, self.source.image_key]
        if (
            len(set(keys)) != 3
            or self.head_parent.image_key != self.intermediate
            or self.intermediate_parent.image_key != self.source.image_key
            or len({(k.ceph_fsid, k.pool_id, k.rbd_namespace) for k in keys}) != 1
            or self.binding.root.cluster_key != self.source.cluster_key
            or self.binding.csi_driver != self.source.csi_driver
        ):
            raise ValueError("Unsupported current custody ancestry")
        return self


class CustodyAttestation(Evidence):
    administrator: Text
    reason: str = Field(min_length=1, max_length=4096)
    namespace_custody: Literal[True]

    @field_validator("namespace_custody", mode="before")
    @classmethod
    def explicit_custody(cls, value):
        if value is not True:
            raise ValueError("Explicit namespace custody attestation required")
        return value


class PreparedCustody(Evidence):
    request: EnrollmentRequest
    topologies: dict[str, dict]


class VerifiedCustody(Evidence):
    version: Literal["native-online-custody-v1"] = "native-online-custody-v1"
    prepared: PreparedCustody
    enrollment: VerifiedEnrollment
    attestation: CustodyAttestation
    runtime_topologies: dict[str, dict]
    object_identities: dict[str, list[dict]]
    bindings: list[VolumeBinding] = Field(min_length=1, max_length=10000)
    ancestry: list[CurrentAncestryCustody] = Field(
        default_factory=list, max_length=10000
    )
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
