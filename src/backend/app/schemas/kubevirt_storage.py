"""Database-independent evidence contracts for read-only native RBD accounting."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Bytes = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Text = Annotated[str, Field(min_length=1, max_length=256)]
Status = Literal[
    "complete",
    "unavailable",
    "timeout",
    "identity_changed",
    "invalid_output",
    "unsupported",
    "output_limit",
    "scan_limit",
]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("*", mode="after")
    @staticmethod
    def timestamps(value):
        if isinstance(value, datetime):
            offset = value.utcoffset()
            if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
                raise ValueError("UTC timestamp required")
            if value > datetime.now(UTC):
                raise ValueError("future source timestamp")
        return value


class PoolKey(Evidence):
    ceph_fsid: Text
    pool_id: Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]


class ImageKey(PoolKey):
    rbd_namespace: Annotated[str, Field(max_length=128)] = ""
    image_id: Text


class SnapshotKey(Evidence):
    image_key: ImageKey
    snapshot_id: Text


class ProjectRoot(Evidence):
    project_id: UUID
    cluster_key: Text
    namespace: Text
    namespace_uid: Text
    project_cr_uid: Text
    deployment_generation: Annotated[int, Field(strict=True, ge=0)] | None = None


class VolumeBinding(Evidence):
    root: ProjectRoot
    pvc_uid: Text
    pv_uid: Text
    csi_driver: Text
    volume_handle: Annotated[str, Field(min_length=1, max_length=1024)]
    image_key: ImageKey
    provisioned_bytes: Bytes | None = None
    owner_reference_uids: list[Text] = Field(default_factory=list, max_length=128)


class Blocker(Evidence):
    code: Text
    project_id: UUID | None = None
    image_key: ImageKey | None = None


class CacheBinding(Evidence):
    cluster_key: Text
    namespace: Text
    namespace_uid: Text
    pvc_uid: Text
    pv_uid: Text
    csi_driver: Text
    volume_handle: Annotated[str, Field(min_length=1, max_length=1024)]
    image_key: ImageKey


class NativeInventory(Evidence):
    collector_id: Text = "standalone"
    project_roots: list[ProjectRoot] = Field(default_factory=list, max_length=10000)
    volume_bindings: list[VolumeBinding] = Field(default_factory=list, max_length=10000)
    cache_bindings: list[CacheBinding] = Field(default_factory=list, max_length=10000)
    retained_bindings: list[VolumeBinding] = Field(
        default_factory=list, max_length=10000
    )
    blockers: list[Blocker] = Field(default_factory=list, max_length=10000)
    collected_at: datetime
    inventory_digest: str = ""


class ImageObservation(Evidence):
    key: ImageKey
    parent: SnapshotKey | None = None
    snapshots: list[SnapshotKey] = Field(default_factory=list, max_length=10000)
    children: list[ImageKey] = Field(default_factory=list, max_length=10000)
    collected_at: datetime
    allocated_bytes: Bytes | None
    provisioned_bytes: Bytes | None
    measurement_status: Status
    command_version: Annotated[str, Field(max_length=256)]
    measurement_semantics_version: Annotated[str, Field(max_length=256)] | None

    @model_validator(mode="after")
    def complete_requires_bytes(self):
        if self.measurement_status == "complete" and (
            self.allocated_bytes is None
            or self.measurement_semantics_version != "kubevirt-rbd-allocated-v1"
        ):
            raise ValueError("complete observation requires validated measurement")
        if self.measurement_status != "complete" and self.allocated_bytes is not None:
            raise ValueError("failed measurement cannot supply allocation")
        return self


class ProjectAllocation(Evidence):
    project_id: UUID
    namespace_uid: Text
    allocated_bytes: Bytes | None
    known_allocated_bytes: Bytes
    provisioned_bytes: Bytes | None
    measured_images: Annotated[int, Field(strict=True, ge=0)]
    required_images: Annotated[int, Field(strict=True, ge=0)]
    oldest_observed_at: datetime | None
    coverage: Literal["complete", "partial", "unavailable"]
    blockers: list[Text] = Field(default_factory=list, max_length=10000)


class PoolAllocation(Evidence):
    pool_key: PoolKey
    allocated_bytes: Bytes | None
    known_allocated_bytes: Bytes


class AttributionResult(Evidence):
    projects: list[ProjectAllocation] = Field(default_factory=list, max_length=10000)
    platform_pools: list[PoolAllocation] = Field(default_factory=list, max_length=10000)
    unattributed_images: list[ImageKey] = Field(default_factory=list, max_length=10000)


class NativeStorageReport(AttributionResult):
    version: Literal["kubevirt-rbd-allocated-v1"] = "kubevirt-rbd-allocated-v1"
    metric: Literal["rbd_allocated_extent_bytes"] = "rbd_allocated_extent_bytes"
    report_id: UUID
    collector_id: Text
    started_at: datetime
    completed_at: datetime
    inventory_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    cluster_keys: list[Text] = Field(max_length=10000)
    pool_keys: list[PoolKey] = Field(max_length=10000)
    project_roots: list[ProjectRoot] = Field(max_length=10000)
    volume_bindings: list[VolumeBinding] = Field(max_length=10000)
    images: list[ImageObservation] = Field(max_length=10000)
    retained_bindings: list[VolumeBinding] = Field(max_length=10000)
    cache_bindings: list[CacheBinding] = Field(default_factory=list, max_length=10000)
    collection_errors: list[Blocker] = Field(max_length=10000)
    enforcement_eligible: Literal[False] = False

    @model_validator(mode="after")
    def ordered_times(self):
        if self.completed_at < self.started_at:
            raise ValueError("reversed report timestamps")
        if any(i.collected_at > self.completed_at for i in self.images):
            raise ValueError("observation after report completion")
        return self


class CollectionLimits(Evidence):
    command_timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=3600)] = 15
    scan_timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=7200)] = 600
    max_objects: Annotated[int, Field(strict=True, ge=1, le=100000)] = 10000
    max_parent_depth: Annotated[int, Field(strict=True, ge=1, le=128)] = 32
    max_output_bytes: Annotated[
        int, Field(strict=True, ge=1024, le=64 * 1024 * 1024)
    ] = (16 * 1024 * 1024)
    max_parallel_commands: Literal[1] = 1


class ReadResult(Evidence):
    """Typed failure deliberately carries no stderr, command or credential data."""

    status: Status
    collected_at: datetime
    value: object | None = None
