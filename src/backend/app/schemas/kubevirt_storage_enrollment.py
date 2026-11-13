"""Independent operator enrollment, never derived from a collector report."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_serializer, model_validator

from app.schemas.kubevirt_storage import Evidence, PoolKey, ProjectRoot, Text
from app.schemas.kubevirt_storage_initialization import InitializationEvidence


class DatabaseProjectIdentity(Evidence):
    project_id: UUID
    host_id: UUID
    provider_id: UUID
    project_provider_id: UUID | None
    owner_id: UUID
    organization_id: UUID | None
    updated_at: datetime


class EnrollmentRequest(Evidence):
    target_id: UUID
    request_id: UUID
    projects: list[DatabaseProjectIdentity] = Field(min_length=1, max_length=10000)


class ProviderCluster(Evidence):
    provider_id: UUID
    cluster_key: Text


class CacheRoot(Evidence):
    cluster_key: Text
    namespace: Text
    namespace_uid: Text


class DomainExtension(Evidence):
    version: Literal["native-domain-extension-v1"]
    previous_enrollment_id: UUID
    previous_enrollment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    initialization_authorization_digest: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )

    @model_serializer(mode="wrap")
    def serialize_authorization(self, handler):
        data = handler(self)
        if data.get("initialization_authorization_digest") is None:
            data.pop("initialization_authorization_digest", None)
        return data


class VerificationPins(Evidence):
    domain_id: UUID
    collector_ids: list[Text] = Field(min_length=1, max_length=100)
    provider_clusters: list[ProviderCluster] = Field(min_length=1, max_length=100)
    project_roots: list[ProjectRoot] = Field(min_length=1, max_length=10000)
    cache_roots: list[CacheRoot] = Field(default_factory=list, max_length=100)
    pool_keys: list[PoolKey] = Field(min_length=1, max_length=100)
    initialization_evidence: InitializationEvidence | None = None
    domain_extension: DomainExtension | None = None

    @model_validator(mode="after")
    def initialization_scope(self):
        if self.initialization_evidence is None:
            return self
        target = self.initialization_evidence.target
        aliases = [
            p.cluster_key
            for p in self.provider_clusters
            if p.provider_id == target.provider_id
        ]
        pools = {(p.ceph_fsid, p.pool_id) for p in self.pool_keys}
        if (
            aliases != [target.cluster_key]
            or (target.ceph_fsid, target.pool_id) not in pools
        ):
            raise ValueError(
                "initialization target is outside independently pinned scope"
            )
        return self

    @model_serializer(mode="wrap")
    def serialize_initialization(self, handler):
        data = handler(self)
        for key in ("initialization_evidence", "domain_extension"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class VerifiedEnrollment(Evidence):
    version: str = "native-enrollment-v1"
    request: EnrollmentRequest
    pins: VerificationPins
    verified_at: datetime
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
