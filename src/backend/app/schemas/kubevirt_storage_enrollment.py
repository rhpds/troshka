"""Independent operator enrollment, never derived from a collector report."""

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.kubevirt_storage import Evidence, PoolKey, ProjectRoot, Text


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


class VerificationPins(Evidence):
    domain_id: UUID
    collector_ids: list[Text] = Field(min_length=1, max_length=100)
    provider_clusters: list[ProviderCluster] = Field(min_length=1, max_length=100)
    project_roots: list[ProjectRoot] = Field(min_length=1, max_length=10000)
    cache_roots: list[CacheRoot] = Field(default_factory=list, max_length=100)
    pool_keys: list[PoolKey] = Field(min_length=1, max_length=100)


class VerifiedEnrollment(Evidence):
    version: str = "native-enrollment-v1"
    request: EnrollmentRequest
    pins: VerificationPins
    verified_at: datetime
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
