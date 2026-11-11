"""Privileged operator-attested bootstrap, independent of report parsing.

Callers own transactions. Holders of import DB credentials are trusted operators;
these manifests are not a signed multi-tenant ingestion protocol.
"""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from app.models.host import Host
from app.models.kubevirt_storage import (
    NativeCollectionDomain,
    NativeEnrollment,
    NativeEnrollmentRequest,
    NativePoolClaim,
    NativeProjectRoot,
    NativeReportingTarget,
)
from app.models.project import Project
from app.models.provider import Provider
from app.models.user import User
from app.schemas.kubevirt_storage_enrollment import (
    DatabaseProjectIdentity,
    EnrollmentRequest,
    VerifiedEnrollment,
)
from app.services.quota_locks import lock_rows


def digest(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def root_identity(root):
    return root.model_dump(mode="json", exclude={"deployment_generation"})


def root_key(root):
    return digest(
        {
            "cluster_key": root.cluster_key,
            "namespace_uid": root.namespace_uid,
            "project_cr_uid": root.project_cr_uid,
        }
    )


def _database_identity(db, project_id):
    row = (
        db.query(
            Project.id,
            Project.host_id,
            Host.provider_id,
            Project.provider_id.label("project_provider_id"),
            Project.owner_id,
            User.organization_id,
            Project.updated_at,
            Provider.type,
        )
        .join(User, User.id == Project.owner_id)
        .join(Host, Host.id == Project.host_id)
        .join(Provider, Provider.id == Host.provider_id)
        .filter(Project.id == str(project_id))
        .one_or_none()
    )
    if row is None or row.type != "kubevirt":
        raise ValueError("project has no assigned native provider")
    if row.project_provider_id and row.project_provider_id != row.provider_id:
        raise ValueError("conflicting project provider")
    return DatabaseProjectIdentity(
        project_id=row.id,
        host_id=row.host_id,
        provider_id=row.provider_id,
        project_provider_id=row.project_provider_id,
        owner_id=row.owner_id,
        organization_id=row.organization_id,
        updated_at=utc(row.updated_at),
    )


def prepare_native_enrollment(db, project_ids):
    target = db.get(NativeReportingTarget, 1)
    if target is None:
        raise ValueError("explicit reporting target UUID must be configured first")
    identities = [
        _database_identity(db, identity) for identity in sorted(set(project_ids))
    ]
    request = EnrollmentRequest(
        target_id=target.target_id, request_id=uuid4(), projects=identities
    )
    db.add(
        NativeEnrollmentRequest(
            id=str(request.request_id),
            digest=digest(request),
            payload=request.model_dump(mode="json"),
        )
    )
    db.flush()
    return request


def _verify_roots(request, pins, readers):
    projects = {str(p.project_id): p for p in request.projects}
    if {str(r.project_id) for r in pins.project_roots} != set(projects):
        raise ValueError("verification roots must exactly cover requested projects")
    aliases = {str(p.provider_id): p.cluster_key for p in pins.provider_clusters}
    if len(aliases) != len(pins.provider_clusters):
        raise ValueError("duplicate provider alias")
    if set(aliases) != {str(p.provider_id) for p in request.projects}:
        raise ValueError("provider aliases must exactly cover requested dependencies")
    for root in pins.project_roots:
        provider_id = str(projects[str(root.project_id)].provider_id)
        kube = readers[provider_id]
        if (
            aliases[provider_id] != root.cluster_key
            or kube.cluster_identity() != root.cluster_key
        ):
            raise ValueError("cluster identity mismatch")
        _verify_project(kube, root)
    for cache in pins.cache_roots:
        providers = [
            p for p, cluster in aliases.items() if cluster == cache.cluster_key
        ]
        if not providers:
            raise ValueError("cache cluster is not enrolled")
        for provider_id in providers:
            if (
                readers[provider_id]
                .namespace(cache.namespace)
                .get("metadata", {})
                .get("uid")
                != cache.namespace_uid
            ):
                raise ValueError("cache namespace identity mismatch")


def _verify_project(kube, root):
    if (
        kube.namespace(root.namespace).get("metadata", {}).get("uid")
        != root.namespace_uid
    ):
        raise ValueError("namespace identity mismatch")
    projects = kube.projects(root.namespace)
    if len(projects) != 1:
        raise ValueError("ambiguous TroshkaProject identity")
    project = projects[0]
    if project.get("metadata", {}).get("uid") != root.project_cr_uid or project.get(
        "spec", {}
    ).get("projectId") != str(root.project_id):
        raise ValueError("full TroshkaProject identity mismatch")


def _verify_pools(pins, backend):
    for pool in set(pins.pool_keys):
        reader = backend[pool.ceph_fsid] if isinstance(backend, dict) else backend
        fsid, pools = reader.cluster_identity(), reader.pool_identity()
        if fsid.status != "complete" or str(fsid.value).strip() != pool.ceph_fsid:
            raise ValueError("Ceph identity mismatch")
        if pools.status != "complete" or not any(
            p.get("poolnum") == pool.pool_id for p in pools.value
        ):
            raise ValueError("pool identity mismatch")


def verify_native_enrollment(request, kube_readers, backend_identity_reader, *, pins):
    """Read only independently selected API identities; never consume a report."""
    _verify_roots(request, pins, kube_readers)
    _verify_pools(pins, backend_identity_reader)
    manifest = VerifiedEnrollment(
        request=request, pins=pins, verified_at=datetime.now(UTC), digest="0" * 64
    )
    return manifest.model_copy(
        update={"digest": digest(manifest.model_dump(mode="json", exclude={"digest"}))}
    )


def _validate_manifest(db, manifest, expected_target_id):
    if manifest.version != "native-enrollment-v1" or manifest.digest != digest(
        manifest.model_dump(mode="json", exclude={"digest"})
    ):
        raise ValueError("enrollment digest mismatch")
    # Serialize rare bootstrap/enrollment writes before the shared identity tiers.
    target = (
        db.query(NativeReportingTarget).filter_by(id=1).with_for_update().one_or_none()
    )
    if (
        target is None
        or target.target_id != expected_target_id
        or str(manifest.request.target_id) != expected_target_id
    ):
        raise ValueError("reporting target identity mismatch")
    prepared = db.get(NativeEnrollmentRequest, str(manifest.request.request_id))
    if prepared is None or prepared.digest != digest(manifest.request):
        raise ValueError("enrollment was not independently prepared for this database")
    identities = manifest.request.projects
    lock_rows(
        db,
        user_ids=[str(p.owner_id) for p in identities],
        organization_ids=[
            str(p.organization_id) for p in identities if p.organization_id
        ],
        project_ids=[str(p.project_id) for p in identities],
    )
    if any(_database_identity(db, p.project_id) != p for p in identities):
        raise ValueError("prepared project identity changed; prepare and verify again")


def _enroll_pool_claims(db, manifest):
    domain_id = str(manifest.pins.domain_id)
    for pool in set(manifest.pins.pool_keys):
        key = digest(pool)
        claim = db.get(NativePoolClaim, key)
        if claim is not None and claim.domain_id != domain_id:
            raise ValueError(
                "physical pool already belongs to another collection domain"
            )
        if claim is None:
            db.add(
                NativePoolClaim(
                    key=key, domain_id=domain_id, payload=pool.model_dump(mode="json")
                )
            )


def _enroll_roots(db, manifest, enrollment_id):
    projects = {str(p.project_id): p for p in manifest.request.projects}
    domain_id = str(manifest.pins.domain_id)
    claims = {
        (r.payload["cluster_key"], r.payload["namespace_uid"]): (
            r.project_id,
            r.domain_id,
        )
        for r in db.query(NativeProjectRoot)
    }
    for root in manifest.pins.project_roots:
        claim = claims.get((root.cluster_key, root.namespace_uid))
        if claim is not None and claim != (str(root.project_id), domain_id):
            raise ValueError("namespace already belongs to another project or domain")
        key = root_key(root)
        existing = db.get(NativeProjectRoot, key)
        if existing is not None:
            if (
                existing.payload != root_identity(root)
                or existing.domain_id != domain_id
            ):
                raise ValueError("namespace root already bound to another identity")
            continue
        db.add(
            NativeProjectRoot(
                key=key,
                domain_id=domain_id,
                project_id=str(root.project_id),
                provider_id=str(projects[str(root.project_id)].provider_id),
                enrollment_id=enrollment_id,
                payload=root_identity(root),
            )
        )


def enroll_native_roots(db, manifest, *, expected_target_id):
    """Recheck prepared DB identities under canonical user -> org -> project locks."""
    _validate_manifest(db, manifest, expected_target_id)
    previous = (
        db.query(NativeEnrollment).filter_by(digest=manifest.digest).one_or_none()
    )
    if previous:
        return previous.id
    enrollment_id, domain_id = str(uuid4()), str(manifest.pins.domain_id)
    domain = (
        db.query(NativeCollectionDomain)
        .filter_by(id=domain_id)
        .with_for_update()
        .one_or_none()
    )
    _enroll_pool_claims(db, manifest)
    _enroll_roots(db, manifest, enrollment_id)
    db.add(
        NativeEnrollment(
            id=enrollment_id,
            domain_id=domain_id,
            digest=manifest.digest,
            payload=manifest.model_dump(mode="json"),
        )
    )
    if domain is None:
        db.add(NativeCollectionDomain(id=domain_id, enrollment_id=enrollment_id))
    else:
        # Prior observations survive but cannot certify a changed domain version.
        domain.enrollment_id = enrollment_id
        domain.latest_report_id = None
        domain.latest_started_at = None
        domain.conflicted = False
    db.flush()
    return enrollment_id
