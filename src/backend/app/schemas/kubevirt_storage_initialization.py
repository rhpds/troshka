"""Protected startup allowance and installation applicability, never a byte cap.

No default allowance or real profile is shipped. Collector/request data cannot
authorize an amount; only the existing protected enrollment pins input owns it.
An exact deployed CSI/Ceph profile still requires reviewed installed identity and
explicit consent to temporary creation-time overage before activation.
"""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_serializer, model_validator

from app.schemas.kubevirt_storage import Bytes, Evidence, InitializationTarget, Text


class ManagedSourcePolicy(Evidence):
    version: Literal["central-qcow2-direct-import-v1"]
    provider_id: UUID
    bucket: Text
    key_prefix: Text
    cache_secret_ref: Text


class InitializationEvidence(Evidence):
    version: Literal["native-blank-initialization-v1", "native-clone-initialization-v1"]
    profile_name: Text
    operation: Literal["blank-pvc", "managed-cache-clone"]
    source_policy: ManagedSourcePolicy | None = None
    metric: Literal["rbd_allocated_extent_bytes"]
    measurement_version: Literal["native-rbd-measurement-v1"]
    validation_reference: Text
    validation_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bound_justification: Text
    target: InitializationTarget
    minimum_capacity_bytes: Bytes
    maximum_capacity_bytes: Bytes
    preallocation: Literal["disabled"]
    initial_storage_bound_bytes: Bytes
    # Required explicit consent to possible creation-time overage. The existing
    # amount field is a per-PVC allowance, not a proved physical upper bound.
    reservation_semantics: Literal["measured-startup-allowance-v1"]
    vnc_image: str = Field(
        pattern=r"^quay\.io/redhat-gpte/troshka-vnc-proxy:v[0-9][A-Za-z0-9_.-]{0,126}$"
    )

    @model_validator(mode="after")
    def capacity_range(self):
        clone = self.operation == "managed-cache-clone"
        if clone != (self.source_policy is not None) or clone != (
            self.version == "native-clone-initialization-v1"
        ):
            raise ValueError("clone initialization requires explicit source policy")
        if not 0 < self.minimum_capacity_bytes <= self.maximum_capacity_bytes:
            raise ValueError("positive ordered capacity applicability required")
        if not self.initial_storage_bound_bytes:
            raise ValueError("positive explicit startup allowance required")
        return self

    @model_serializer(mode="wrap")
    def serialize_policy(self, handler):
        data = handler(self)
        if data.get("source_policy") is None:
            data.pop("source_policy", None)
        return data
