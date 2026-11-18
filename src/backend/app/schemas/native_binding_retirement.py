"""Independently verified per-binding absence, never namespace retirement."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_serializer, model_validator

from app.schemas.kubevirt_storage import (
    BindingRetirementObservation,
    BindingRetirementReference,  # noqa: F401 -- public reference type
    Evidence,
    ProjectRoot,
    Text,
    VolumeBinding,
)
from app.schemas.native_online_enrollment import CurrentAncestryCustody


class PreparedBindingRetirement(Evidence):
    request_id: UUID
    target_id: UUID
    domain_id: UUID
    source_custody_id: UUID
    source_custody_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    root: ProjectRoot
    binding: VolumeBinding
    pvc_name: Text
    ancestry: CurrentAncestryCustody | None
    administrator: Text | None = None
    reason: str = Field(min_length=1, max_length=4096)
    prepared_at: datetime
    source_kind: Literal["administrator", "origin"] = "administrator"
    origin_request_id: UUID | None = None

    @model_validator(mode="after")
    def authority(self):
        if self.source_kind == "administrator":
            if self.administrator is None or self.origin_request_id is not None:
                raise ValueError(
                    "Administrator retirement requires original administrator authority"
                )
        elif self.administrator is not None or self.origin_request_id is None:
            raise ValueError("Origin retirement requires protected origin authority")
        return self

    @model_serializer(mode="wrap")
    def preserve_legacy_digest(self, handler):
        value = handler(self)
        if self.source_kind == "administrator":
            value.pop("source_kind", None)
            value.pop("origin_request_id", None)
        else:
            value.pop("administrator", None)
        return value


class VerifiedBindingRetirement(Evidence):
    prepared: PreparedBindingRetirement
    observation: BindingRetirementObservation
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
