"""New managed CSI clone authority, separate from collector evidence."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.schemas.kubevirt_cache_birth import CacheBirthReference
from app.schemas.kubevirt_storage import (
    CacheBinding,
    Evidence,
    ImageKey,
    InitializationTarget,
    ProjectRoot,
    SnapshotKey,
    Text,
    VolumeBinding,
)


class CloneIntent(Evidence):
    lineage_id: UUID
    operation_id: Text
    claim_name: Text
    root: ProjectRoot
    domain_id: UUID
    target_id: UUID
    target: InitializationTarget
    cache_reference: CacheBirthReference
    source: CacheBinding
    profile_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    demand_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime
    profile: Literal["odf-rbd-managed-csi-clone-v1"] = "odf-rbd-managed-csi-clone-v1"


class CloneCreation(Evidence):
    """Original operator create journal identity, never recovered by name adoption."""

    dv_uid: Text
    dv_name: Text
    namespace_uid: Text
    operation_id: Text


class CloneLineage(Evidence):
    lineage_id: UUID
    operation_id: Text
    claim_name: Text
    domain_id: UUID
    target_id: UUID
    cache_reference: CacheBirthReference
    binding: VolumeBinding
    source: CacheBinding
    intermediate: ImageKey
    head_parent: SnapshotKey
    intermediate_parent: SnapshotKey
    original_dv_uid: Text
    verified_at: datetime
    profile: Literal["odf-rbd-managed-csi-clone-v1"] = "odf-rbd-managed-csi-clone-v1"

    @model_validator(mode="after")
    def exact_three_image_chain(self):
        keys = [self.binding.image_key, self.intermediate, self.source.image_key]
        if (
            len(set(keys)) != 3
            or self.head_parent.image_key != self.intermediate
            or self.intermediate_parent.image_key != self.source.image_key
            or len({(k.ceph_fsid, k.pool_id, k.rbd_namespace) for k in keys}) != 1
            or self.binding.root.cluster_key != self.source.cluster_key
            or self.binding.csi_driver != self.source.csi_driver
        ):
            raise ValueError("unsupported managed clone chain")
        return self


class CloneObservation(Evidence):
    target: InitializationTarget
    lineage: CloneLineage
