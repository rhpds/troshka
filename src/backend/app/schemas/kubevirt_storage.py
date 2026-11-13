"""Database-independent evidence contracts for read-only native RBD accounting."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

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


class BackendScope(Evidence):
    """Observed enrollment scope, never a claim that a shared pool is empty."""

    cluster_keys: list[Text] = Field(min_length=1, max_length=100)
    pool_keys: list[PoolKey] = Field(min_length=1, max_length=100)


class CloneReference(Evidence):
    lineage_id: UUID
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class CsiWorkloadIdentity(Evidence):
    uid: Text
    spec_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    images: dict[Text, Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]] = Field(
        min_length=1, max_length=32
    )


class CsiInstallationIdentity(Evidence):
    version: Literal["odf-rbd-installation-v1"]
    namespace_uid: Text
    driver_uid: Text
    driver_spec_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    controller: CsiWorkloadIdentity
    node: CsiWorkloadIdentity


class StorageClassOptions(Evidence):
    volume_binding_mode: Literal["Immediate"]
    reclaim_policy: Literal["Delete", "Retain"]
    allow_volume_expansion: bool
    mount_options: list[Text] = Field(max_length=32)


class InitializationTarget(PoolKey):
    provider_id: UUID
    cluster_key: Text
    rbd_namespace: str = Field(max_length=128)
    storage_class_name: Text
    storage_class_uid: Text
    provisioner: Text
    storage_class_parameters: dict[Text, Text] = Field(max_length=100)
    csi_version: Text
    ceph_version: Text
    rbd_command_version: Text
    csi_installation: CsiInstallationIdentity | None = None
    storage_class_options: StorageClassOptions | None = None

    @model_serializer(mode="wrap")
    def serialize_optional_identity(self, handler):
        data = handler(self)
        for name in ("csi_installation", "storage_class_options"):
            if data.get(name) is None:
                data.pop(name, None)
        return data


class InitializationObservation(Evidence):
    version: Literal["native-initialization-observation-v1"]
    metric: Literal["rbd_allocated_extent_bytes"]
    measurement_version: Literal["native-rbd-measurement-v1"]
    profile_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    target: InitializationTarget
    observed_at: datetime
    # Legacy evidence remains readable/digest stable, but is no longer a gate.
    headroom_bytes: Bytes | None = None
    headroom_semantics: Text | None = None

    @model_serializer(mode="wrap")
    def serialize_legacy_headroom(self, handler):
        data = handler(self)
        for name in ("headroom_bytes", "headroom_semantics"):
            if name not in self.model_fields_set:
                data.pop(name, None)
        return data


class ImageKey(PoolKey):
    rbd_namespace: Annotated[str, Field(max_length=128)] = ""
    image_id: Text


class SnapshotKey(Evidence):
    image_key: ImageKey
    snapshot_id: Text


class BackendImageInventory(PoolKey):
    """Successful stable public directory and all-source trash observations."""

    rbd_namespace: str = Field(max_length=128)
    active_images: dict[Text, Text] = Field(max_length=10000)
    trash_ids: list[Text] = Field(max_length=10000)
    collected_at: datetime


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


class CleanupObject(Evidence):
    kind: Text
    name: Text
    uid: Text
    namespace: Text | None = None
    resource_version: Text
    reclaim_policy: Literal["Delete", "Retain"] | None = None

    namespace_bootstrap: dict | None = None


class NativeCleanupIntent(Evidence):
    version: Literal["native-cleanup-v1"] = "native-cleanup-v1"
    intent_id: UUID
    operation_id: Text
    operation_ids: list[Text] = Field(min_length=1, max_length=10000)
    generation: Text
    mode: Literal["delete", "undeploy"]
    namespace_mode: Literal["managed"]
    phase: Literal["stopping", "off", "teardown", "pending", "settled", "finalized"]
    owner_id: UUID
    organization_id: UUID | None
    host_id: UUID
    provider_id: UUID
    domain_id: UUID
    enrollment_id: UUID
    initial_report_id: UUID
    root: ProjectRoot
    owned_images: list[ImageKey] = Field(max_length=10000)
    bindings: list[VolumeBinding] = Field(max_length=10000)
    objects: list[CleanupObject] = Field(default_factory=list, max_length=10000)
    inventory_digest: str = ""
    stop_action_id: Text
    stop_proof: dict | None = None
    prepared_at: datetime
    teardown_at: datetime | None = None
    namespace_delete_at: datetime | None = None
    settlement_report_id: UUID | None = None
    settled_at: datetime | None = None
    last_error: str | None = None


class RetiringRoot(Evidence):
    intent_id: UUID
    operation_id: Text
    root: ProjectRoot
    inventory_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    owned_images: list[ImageKey] = Field(max_length=10000)
    objects_absent: bool
    observed_at: datetime


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


class CSISnapshotEvidence(Evidence):
    """Kubernetes evidence relevant to a root, not proof of retained ownership.

    Content references lack namespace-generation identity. An orphan's namespace
    name selects an uncertainty blocker only; it never establishes a binding.
    """

    root: ProjectRoot
    snapshot_uid: Text | None
    snapshot_name: Text | None
    content_name: Text | None
    content_uid: Text | None
    source_pvc_name: Text | None
    csi_driver: Text | None
    ready_to_use: bool
    snapshot_handle_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None
    source_snapshot_handle_sha256: Annotated[
        str, Field(pattern=r"^[a-f0-9]{64}$")
    ] | None
    source_volume_handle_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None
    reference_snapshot_uid: Text | None = None
    reference_snapshot_name: Text | None = None
    reference_namespace: Text | None = None
    deletion_policy: Text | None = None

    @model_serializer(mode="wrap")
    def serialize_reference(self, handler):
        data = handler(self)
        # Preserve old immutable payload/digest identities when fields were absent.
        for name in (
            "reference_snapshot_uid",
            "reference_snapshot_name",
            "reference_namespace",
            "deletion_policy",
        ):
            if data.get(name) is None:
                data.pop(name, None)
        return data


class NativeInventory(Evidence):
    backend_scope: BackendScope | None = None
    clone_references: list[CloneReference] = Field(
        default_factory=list, max_length=10000
    )

    @model_serializer(mode="wrap")
    def serialize_clone_references(self, handler):
        data = handler(self)
        if data.get("backend_scope") is None:
            data.pop("backend_scope", None)
        if not data.get("clone_references"):
            data.pop("clone_references", None)
        return data

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
    snapshot_evidence: list[CSISnapshotEvidence] = Field(
        default_factory=list, max_length=10000
    )
    retiring_roots: list[RetiringRoot] = Field(default_factory=list, max_length=10000)


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
    absence: BackendImageInventory | None = None

    @model_serializer(mode="wrap")
    def serialize_absence(self, handler):
        data = handler(self)
        if data.get("absence") is None:
            data.pop("absence", None)
        return data

    @model_validator(mode="after")
    def complete_requires_bytes(self):
        if self.absence is not None:
            proof = self.absence
            if (
                self.measurement_status != "complete"
                or self.allocated_bytes != 0
                or self.provisioned_bytes != 0
                or self.measurement_semantics_version != "kubevirt-rbd-absence-v1"
                or (proof.ceph_fsid, proof.pool_id, proof.rbd_namespace)
                != (self.key.ceph_fsid, self.key.pool_id, self.key.rbd_namespace)
                or self.key.image_id
                in set(proof.active_images.values()) | set(proof.trash_ids)
                or self.collected_at != proof.collected_at
                or self.parent
                or self.snapshots
                or self.children
            ):
                raise ValueError("invalid immutable image absence proof")
            return self
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
    backend_scope: BackendScope | None = None
    clone_references: list[CloneReference] = Field(
        default_factory=list, max_length=10000
    )
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
    inventory_blockers: list[Blocker] | None = Field(default=None, max_length=10000)
    enforcement_eligible: Literal[False] = False
    initialization_observation: InitializationObservation | None = None
    snapshot_evidence: list[CSISnapshotEvidence] = Field(
        default_factory=list, max_length=10000
    )
    retiring_roots: list[RetiringRoot] = Field(default_factory=list, max_length=10000)

    @model_serializer(mode="wrap")
    def serialize_initialization(self, handler):
        data = handler(self)
        if data.get("backend_scope") is None:
            data.pop("backend_scope", None)
        if not data.get("clone_references"):
            data.pop("clone_references", None)
        if data.get("initialization_observation") is None:
            data.pop("initialization_observation", None)
        if not data.get("retiring_roots"):
            data.pop("retiring_roots", None)
        return data

    @model_validator(mode="after")
    def ordered_times(self):
        if self.completed_at < self.started_at:
            raise ValueError("reversed report timestamps")
        if any(i.collected_at > self.completed_at for i in self.images):
            raise ValueError("observation after report completion")
        if (
            self.initialization_observation is not None
            and not self.started_at
            <= self.initialization_observation.observed_at
            <= self.completed_at
        ):
            raise ValueError("initialization observation outside collection")
        return self


class NativeStorageUsage(Evidence):
    scope: Literal["project", "user", "organization"]
    scope_id: str
    metric: Literal["rbd_allocated_extent_bytes"] = "rbd_allocated_extent_bytes"
    status: Literal["complete", "partial", "unavailable", "stale"]
    allocated_bytes: Bytes | None
    known_allocated_bytes: Bytes
    provisioned_bytes: Bytes | None
    observed_at: datetime | None
    covered_projects: Annotated[int, Field(strict=True, ge=0)]
    expected_projects: Annotated[int, Field(strict=True, ge=0)]
    reason_codes: list[Text]
    enforcement_eligible: Literal[False] = False
    platform_bytes: Bytes | None = None
    unattributed_retained_projects: Annotated[
        int, Field(strict=True, ge=0)
    ] | None = None
    unattributed_retained_known_bytes: Bytes | None = None


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
