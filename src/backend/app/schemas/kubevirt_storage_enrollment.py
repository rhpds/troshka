"""Independent operator enrollment, never derived from a collector report."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_serializer, model_validator

from app.schemas.kubevirt_cache_birth import CacheBirthReference
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


class EmptyDomainBootstrap(Evidence):
    version: Literal["native-empty-domain-bootstrap-v1"]
    domain_id: UUID
    provider_id: UUID
    profile_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    pins_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class EnrollmentRequest(Evidence):
    target_id: UUID
    request_id: UUID
    projects: list[DatabaseProjectIdentity] = Field(max_length=10000)
    empty_domain_bootstrap: EmptyDomainBootstrap | None = None

    @model_validator(mode="after")
    def project_scope(self):
        if bool(self.projects) == bool(self.empty_domain_bootstrap):
            raise ValueError("nonempty projects or explicit empty bootstrap required")
        return self

    @model_serializer(mode="wrap")
    def serialize_bootstrap(self, handler):
        data = handler(self)
        if data.get("empty_domain_bootstrap") is None:
            data.pop("empty_domain_bootstrap", None)
        return data


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
    cache_authorization_digest: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    root_transition_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_serializer(mode="wrap")
    def serialize_authorization(self, handler):
        data = handler(self)
        if data.get("initialization_authorization_digest") is None:
            data.pop("initialization_authorization_digest", None)
        if data.get("cache_authorization_digest") is None:
            data.pop("cache_authorization_digest", None)
        if data.get("root_transition_digest") is None:
            data.pop("root_transition_digest", None)
        return data


class VerificationPins(Evidence):
    domain_id: UUID
    collector_ids: list[Text] = Field(min_length=1, max_length=100)
    provider_clusters: list[ProviderCluster] = Field(min_length=1, max_length=100)
    project_roots: list[ProjectRoot] = Field(max_length=10000)
    empty_domain_bootstrap: EmptyDomainBootstrap | None = None
    cache_roots: list[CacheRoot] = Field(default_factory=list, max_length=100)
    cache_birth_receipts: list[CacheBirthReference] = Field(
        default_factory=list, max_length=1000
    )
    pool_keys: list[PoolKey] = Field(min_length=1, max_length=100)
    initialization_evidence: InitializationEvidence | None = None
    domain_extension: DomainExtension | None = None

    @model_validator(mode="after")
    def initialization_scope(self):
        bootstrap = self.empty_domain_bootstrap
        if bool(self.project_roots) == bool(bootstrap):
            raise ValueError("nonempty roots or explicit empty bootstrap required")
        if bootstrap is not None and (
            bootstrap.domain_id != self.domain_id
            or len(self.provider_clusters) != 1
            or self.provider_clusters[0].provider_id != bootstrap.provider_id
            or len(self.pool_keys) != 1
            or not self.cache_roots
            or self.initialization_evidence is None
            or self.domain_extension is not None
            or self.cache_birth_receipts
        ):
            raise ValueError(
                "empty bootstrap requires exact provider/cache/profile scope"
            )
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
        for key in (
            "initialization_evidence",
            "domain_extension",
            "empty_domain_bootstrap",
        ):
            if data.get(key) is None:
                data.pop(key, None)
        if not data.get("cache_birth_receipts"):
            data.pop("cache_birth_receipts", None)
        return data


class VerifiedEnrollment(Evidence):
    version: str = "native-enrollment-v1"
    request: EnrollmentRequest
    pins: VerificationPins
    verified_at: datetime
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
