"""Optional operator-reviewed metadata, never a physical capability certificate.

No default bound or real profile is shipped. Collector/request data cannot
authorize an amount; only the existing protected enrollment pins input owns it.
An exact deployed CSI/Ceph profile still requires independent physical acceptance.
"""

from typing import Literal

from pydantic import Field, model_validator

from app.schemas.kubevirt_storage import Bytes, Evidence, InitializationTarget, Text


class InitializationEvidence(Evidence):
    version: Literal["native-blank-initialization-v1"]
    profile_name: Text
    operation: Literal["blank-pvc"]
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

    @model_validator(mode="after")
    def capacity_range(self):
        if not 0 < self.minimum_capacity_bytes <= self.maximum_capacity_bytes:
            raise ValueError("positive ordered capacity applicability required")
        return self
