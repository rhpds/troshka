"""Snapshot allocation authority is distinct from a PVC or cache binding."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_serializer, model_validator

from app.schemas.kubevirt_storage import (
    BackendImageState,
    Evidence,
    ImageKey,
    SnapshotKey,
    Text,
    VolumeBinding,
)


class OriginProducerIntent(Evidence):
    id: UUID
    operation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: Literal["VolumeSnapshot", "PersistentVolumeClaim", "Job", "Pod"]
    name: Text
    role: Literal["snapshot", "restore", "scratch", "export", "filepull"]
    spec_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_pvc_name: Text | None = None
    snapshot_spec: dict | None = None
    uid: Text | None = None
    admitted_spec_digest: str | None = None
    source_binding: VolumeBinding | None = None
    content_identity: dict | None = None

    @model_validator(mode="after")
    def safe_snapshot_spec(self):
        if self.kind == "VolumeSnapshot":
            expected = {"source", "volumeSnapshotClassName"}
            if not self.snapshot_spec or set(self.snapshot_spec) != expected:
                raise ValueError(
                    "Only dynamic source/class snapshot specification is supported"
                )
            if self.snapshot_spec["source"] != {
                "persistentVolumeClaimName": self.source_pvc_name
            }:
                raise ValueError("Snapshot source selector changed")
            if self.role != "snapshot" or not isinstance(
                self.snapshot_spec["volumeSnapshotClassName"], str
            ):
                raise ValueError("Snapshot producer role/class changed")
        elif self.snapshot_spec is not None or self.source_pvc_name is not None:
            raise ValueError("Only snapshots may retain a source specification")
        return self


class OriginSnapshotLiability(Evidence):
    producer_id: UUID
    source: VolumeBinding
    source_pvc_name: Text
    source_pv_name: Text
    snapshot_name: Text
    snapshot_uid: Text
    content_name: Text
    content_uid: Text
    snapshot_class: Text
    csi_driver: Text
    handle: Text
    image: ImageKey
    snapshot: SnapshotKey
    parent: SnapshotKey | None
    source_parent: SnapshotKey | None
    profile_digest: str
    verified_at: datetime


class OriginSnapshotRestore(Evidence):
    producer_id: UUID
    snapshot_producer_id: UUID
    binding: VolumeBinding
    parent: SnapshotKey | None
    verified_at: datetime


class SnapshotRetirement(Evidence):
    producer_id: UUID
    image_absence: BackendImageState
    kubernetes_digest: str
    kubernetes_final_digest: str
    verified_at: datetime


class SnapshotFacts(Evidence):
    producers: list[OriginProducerIntent] = Field(default_factory=list, max_length=1000)
    snapshots: list[OriginSnapshotLiability] = Field(
        default_factory=list, max_length=1000
    )
    snapshot_restores: list[OriginSnapshotRestore] = Field(
        default_factory=list, max_length=1000
    )
    snapshot_retirements: list[SnapshotRetirement] = Field(
        default_factory=list, max_length=1000
    )

    @model_serializer(mode="wrap")
    def legacy(self, handler):
        value = handler(self)
        for field in (
            "producers",
            "snapshots",
            "snapshot_restores",
            "snapshot_retirements",
        ):
            if not getattr(self, field):
                value.pop(field, None)
        return value


class SnapshotJournalProfile(Evidence):
    """Explicit fixture-tested layout; not an assertion about a live driver."""

    version: Literal[
        "ceph-csi-v3.13.0-rbd-snapshot-v1", "odf-4.20-a3db60e1-rbd-snapshot-v1"
    ]
    sidecar_version: Literal[
        "external-snapshotter-v8.0.1", "openshift-snapshotter-fd82be21387d"
    ]
    driver: Text
    snapshot_class: Text
    instance_id: Text

    @model_validator(mode="after")
    def pinned_build_pair(self):
        pairs = {
            "ceph-csi-v3.13.0-rbd-snapshot-v1": "external-snapshotter-v8.0.1",
            "odf-4.20-a3db60e1-rbd-snapshot-v1": "openshift-snapshotter-fd82be21387d",
        }
        if self.sidecar_version != pairs[self.version]:
            raise ValueError("Snapshot journal profile/build pair is not supported")
        return self


class SnapshotBackendIdentity(Evidence):
    image: ImageKey
    snapshot: SnapshotKey
    handle: Text
