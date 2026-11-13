"""Protected, new-only direct import authority. Never collector authorization."""

from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit
from uuid import UUID

from pydantic import Field, model_validator

from app.schemas.kubevirt_storage import (
    Bytes,
    Evidence,
    ImageKey,
    InitializationTarget,
    SnapshotKey,
    Text,
)

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
USE_POPULATOR = "cdi.kubevirt.io/storage.usePopulator"


def validate_import_annotations(metadata):
    annotations = metadata.get("annotations", {})
    forbidden = (
        "cdi.kubevirt.io/storage.prePopulated",
        "cdi.kubevirt.io/storage.populatedFor",
        "cdi.kubevirt.io/allowClaimAdoption",
        "cdi.kubevirt.io/storage.checkStaticVolume",
        "cdi.kubevirt.io/storage.persistentVolumeList",
    )
    if annotations.get(USE_POPULATOR) != "false" or any(
        k in annotations for k in forbidden
    ):
        raise ValueError("direct non-adopting import annotation required")


class CacheSource(Evidence):
    endpoint: str = Field(max_length=2048)
    bucket: Text
    key: str = Field(min_length=1, max_length=1024)
    etag: Text
    size_bytes: Bytes
    last_modified: datetime
    version_id: Text | None = None
    content_sha256: Digest | None = None
    immutability_policy: Literal["trusted-admin-frozen-source-v1"]

    @model_validator(mode="after")
    def safe_endpoint(self):
        parts = urlsplit(self.endpoint)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in ("", "/")
        ):
            raise ValueError("source endpoint must be credential-free HTTPS origin")
        if "/" in self.bucket or self.key.startswith("/"):
            raise ValueError("invalid source object identity")
        return self

    def import_url(self):
        return f"{self.endpoint.rstrip('/')}/{quote(self.bucket, safe='')}/{quote(self.key, safe='/')}"


def validate_import_spec(spec):
    """An exact narrow direct S4 import; presence of an empty adoption field fails."""
    if set(spec) != {"source", "pvc"} or set(spec["source"]) != {"s3"}:
        raise ValueError("only direct S4 import is supported")
    source, pvc = spec["source"]["s3"], spec["pvc"]
    if set(source) != {"url", "secretRef"} or not source["secretRef"]:
        raise ValueError("only a credential reference may be persisted")
    required = {"storageClassName", "accessModes", "resources"}
    if not required <= set(pvc) or set(pvc) - required - {"volumeMode"}:
        raise ValueError("fresh provisioning cannot adopt a PVC or backend image")
    if "volumeMode" in pvc and pvc["volumeMode"] != "Filesystem":
        raise ValueError("managed image imports require Filesystem volume mode")
    if (
        pvc["accessModes"] != ["ReadWriteOnce"]
        or set(pvc["resources"]) != {"requests"}
        or set(pvc["resources"]["requests"]) != {"storage"}
    ):
        raise ValueError("unsupported import PVC spec")


class CacheBirthIntent(Evidence):
    version: Literal["native-cache-birth-intent-v1"] = "native-cache-birth-intent-v1"
    operation_id: UUID
    target_id: UUID
    domain_id: UUID
    target: InitializationTarget
    namespace: Text
    namespace_uid: Text
    source: CacheSource
    name: Text
    spec: dict
    spec_digest: Digest
    created_at: datetime
    profile: Literal["odf-rbd-direct-import-v1"] = "odf-rbd-direct-import-v1"
    population: Literal["direct-pvc-no-populator-v1"] = "direct-pvc-no-populator-v1"
    seal_policy: Literal[
        "troshka-no-source-mutations-v1"
    ] = "troshka-no-source-mutations-v1"

    @model_validator(mode="after")
    def direct_source(self):
        from app.services.kubevirt_storage_enrollment import digest

        validate_import_spec(self.spec)
        if (
            self.name != "managed-cache-" + self.operation_id.hex
            or self.spec["source"]["s3"]["url"] != self.source.import_url()
            or self.spec["pvc"]["storageClassName"] != self.target.storage_class_name
            or digest(self.spec) != self.spec_digest
        ):
            raise ValueError("frozen source/spec identity mismatch")
        return self


class CacheBirthReceipt(Evidence):
    version: Literal["native-cache-birth-receipt-v1"] = "native-cache-birth-receipt-v1"
    operation_id: UUID
    intent_digest: Digest
    dv_uid: Text
    pvc_uid: Text
    pv_uid: Text
    csi_driver: Text
    volume_handle: str = Field(min_length=1, max_length=1024)
    image_key: ImageKey
    original_snapshots: list[SnapshotKey] = Field(max_length=10000)
    dv_created_at: datetime
    pvc_created_at: datetime
    pv_created_at: datetime
    verified_at: datetime
    verification_digest: Digest


class CacheBirthReference(Evidence):
    operation_id: UUID
    digest: Digest
