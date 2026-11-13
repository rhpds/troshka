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
    VerificationPins,
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


def prepare_native_enrollment(db, project_ids, *, empty_domain_pins=None):
    target = db.get(NativeReportingTarget, 1)
    if target is None:
        raise ValueError("explicit reporting target UUID must be configured first")
    identities = [
        _database_identity(db, identity) for identity in sorted(set(project_ids))
    ]
    request = EnrollmentRequest(
        target_id=target.target_id,
        request_id=uuid4(),
        projects=identities,
        empty_domain_bootstrap=(
            empty_domain_pins.empty_domain_bootstrap if empty_domain_pins else None
        ),
    )
    if empty_domain_pins is not None:
        _validate_bootstrap(request, empty_domain_pins)
        _bootstrap_provider(db, request)
    db.add(
        NativeEnrollmentRequest(
            id=str(request.request_id),
            digest=digest(request),
            payload=request.model_dump(mode="json"),
        )
    )
    db.flush()
    return request


def authorize_empty_domain(payload):
    """Build the explicit admin-only pins envelope; never infer from inventory."""
    profile = payload.get("initialization_evidence")
    if not profile or payload.get("empty_domain_bootstrap") is not None:
        raise ValueError(
            "fresh empty pins and explicit initialization profile required"
        )
    authorization = {
        "version": "native-empty-domain-bootstrap-v1",
        "domain_id": payload["domain_id"],
        "provider_id": profile["target"]["provider_id"],
        "profile_digest": digest(profile),
        "pins_digest": "0" * 64,
    }
    pins = VerificationPins.model_validate(
        {**payload, "empty_domain_bootstrap": authorization}
    )
    authorization["profile_digest"] = digest(pins.initialization_evidence)
    authorization["pins_digest"] = digest(
        pins.model_dump(mode="json", exclude={"empty_domain_bootstrap"})
    )
    return VerificationPins.model_validate(
        {**payload, "empty_domain_bootstrap": authorization}
    )


def _bootstrap_provider(db, request):
    bootstrap = request.empty_domain_bootstrap
    if bootstrap is None:
        return
    provider = db.get(Provider, str(bootstrap.provider_id))
    if provider is None or provider.type != "kubevirt":
        raise ValueError("empty bootstrap requires an existing native provider")


def _validate_bootstrap(request, pins):
    bootstrap = request.empty_domain_bootstrap
    if bootstrap != pins.empty_domain_bootstrap:
        raise ValueError("prepared empty bootstrap authorization mismatch")
    if bootstrap is None:
        return
    from app.services.native_initialization import implementation

    if bootstrap.pins_digest != digest(
        pins.model_dump(mode="json", exclude={"empty_domain_bootstrap"})
    ) or bootstrap.profile_digest != digest(pins.initialization_evidence):
        raise ValueError("prepared empty bootstrap pins/profile mismatch")
    implementation(pins.initialization_evidence)


def requested_providers(request):
    if request.empty_domain_bootstrap is not None:
        return {str(request.empty_domain_bootstrap.provider_id)}
    return {str(p.provider_id) for p in request.projects}


def _verify_roots(request, pins, readers):
    _validate_bootstrap(request, pins)
    projects = {str(p.project_id): p for p in request.projects}
    if {str(r.project_id) for r in pins.project_roots} != set(projects):
        raise ValueError("verification roots must exactly cover requested projects")
    aliases = {str(p.provider_id): p.cluster_key for p in pins.provider_clusters}
    if len(aliases) != len(pins.provider_clusters):
        raise ValueError("duplicate provider alias")
    if set(aliases) != requested_providers(request):
        raise ValueError("provider aliases must exactly cover requested dependencies")
    for provider_id, cluster in aliases.items():
        if readers[provider_id].cluster_identity() != cluster:
            raise ValueError("cluster identity mismatch")
    for root in pins.project_roots:
        provider_id = str(projects[str(root.project_id)].provider_id)
        kube = readers[provider_id]
        if aliases[provider_id] != root.cluster_key:
            raise ValueError("cluster identity mismatch")
        _verify_project(kube, root)
    _verify_cache_roots(pins, readers, aliases)


def _verify_cache_roots(pins, readers, aliases):
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
    if request.empty_domain_bootstrap is not None:
        from app.services.native_initialization import observe

        profile = pins.initialization_evidence
        backend = (
            backend_identity_reader[profile.target.ceph_fsid]
            if isinstance(backend_identity_reader, dict)
            else backend_identity_reader
        )
        observe(profile, kube_readers[str(profile.target.provider_id)], backend)
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
    _validate_bootstrap(manifest.request, manifest.pins)
    _bootstrap_provider(db, manifest.request)
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


def validated_enrollment(row):
    manifest = VerifiedEnrollment.model_validate(row.payload)
    if (
        manifest.version != "native-enrollment-v1"
        or str(manifest.pins.domain_id) != row.domain_id
        or manifest.digest != row.digest
        or manifest.digest
        != digest(manifest.model_dump(mode="json", exclude={"digest"}))
    ):
        raise ValueError("enrollment content identity mismatch")
    return manifest


def validate_domain_extension(previous, current, *, retired=()):
    """Preserve measurement identity; initialization authority is separately explicit."""
    old, new = previous.pins, current.pins
    old_roots = {
        str(r.project_id): r for r in old.project_roots if root_key(r) not in retired
    }
    new_roots = {str(r.project_id): r for r in new.project_roots}
    if (
        old.domain_id != new.domain_id
        or previous.request.target_id != current.request.target_id
        or len({str(r.project_id) for r in old.project_roots}) != len(old.project_roots)
        or len(new_roots) != len(new.project_roots)
        or not set(old_roots) <= set(new_roots)
        or any(new_roots.get(key) != value for key, value in old_roots.items())
        or {str(p.project_id) for p in current.request.projects} != set(new_roots)
    ):
        raise ValueError("domain extension must retain every exact old root")
    for field in ("collector_ids", "provider_clusters", "pool_keys"):
        before, after = getattr(old, field), getattr(new, field)
        if sorted(map(digest, before)) != sorted(map(digest, after)):
            raise ValueError("domain extension cannot change collection identities")
    _validate_cache_authorization(old, new)
    _validate_initialization_authorization(old, new)
    before = {
        str(p.project_id): p
        for p in previous.request.projects
        if str(p.project_id) in old_roots
    }
    after = {str(p.project_id): p for p in current.request.projects}
    for project_id, identity in before.items():
        # Authorized owner/org transfers remain governed by live operation checks.
        fields = ("host_id", "provider_id", "project_provider_id")
        if project_id not in after or any(
            getattr(identity, key) != getattr(after[project_id], key) for key in fields
        ):
            raise ValueError("domain extension changed retained provider placement")


def _validate_initialization_authorization(old, new):
    extension = new.domain_extension
    authorization = extension.initialization_authorization_digest if extension else None
    changed = old.initialization_evidence != new.initialization_evidence
    if authorization is None:
        if changed:
            raise ValueError(
                "root extension cannot change initialization authorization"
            )
        if set(old.project_roots) == set(new.project_roots) and not (
            extension and extension.cache_authorization_digest
        ):
            raise ValueError("root extension must add a new project root")
        return
    if (
        not changed
        or new.initialization_evidence is None
        or digest(new.initialization_evidence) != authorization
    ):
        raise ValueError("explicit reviewed initialization authorization mismatch")


def cache_authorization_digest(pins):
    return digest(
        {
            "cache_roots": [c.model_dump(mode="json") for c in pins.cache_roots],
            "cache_birth_receipts": [
                r.model_dump(mode="json") for r in pins.cache_birth_receipts
            ],
        }
    )


def _validate_cache_authorization(old, new):
    extension = new.domain_extension
    authorization = extension.cache_authorization_digest if extension else None
    fields = ("cache_roots", "cache_birth_receipts")
    before = {f: set(map(digest, getattr(old, f))) for f in fields}
    after = {f: set(map(digest, getattr(new, f))) for f in fields}
    if authorization is None:
        if before != after:
            raise ValueError("ordinary extension cannot change cache authority")
        return
    if (
        authorization != cache_authorization_digest(new)
        or before == after
        or any(not before[f] <= after[f] for f in fields)
    ):
        raise ValueError("explicit additive cache authorization mismatch")


def _validate_cache_receipts(db, manifest):
    from app.services.native_cache_birth import validated_receipt

    pins = manifest.pins
    seen = set()
    for ref in pins.cache_birth_receipts:
        intent, receipt = validated_receipt(db, ref.operation_id, ref.digest)
        cache = {
            "cluster_key": intent.target.cluster_key,
            "namespace": intent.namespace,
            "namespace_uid": intent.namespace_uid,
        }
        if (
            ref.operation_id in seen
            or intent.domain_id != pins.domain_id
            or intent.target_id != manifest.request.target_id
            or cache not in [c.model_dump(mode="json") for c in pins.cache_roots]
            or not any(
                p.provider_id == intent.target.provider_id
                and p.cluster_key == intent.target.cluster_key
                for p in pins.provider_clusters
            )
            or not any(
                p.ceph_fsid == receipt.image_key.ceph_fsid
                and p.pool_id == receipt.image_key.pool_id
                for p in pins.pool_keys
            )
        ):
            raise ValueError("cache receipt outside protected enrollment scope")
        seen.add(ref.operation_id)


def _extension_predecessor(db, manifest, domain):
    extension = manifest.pins.domain_extension
    if extension is None:
        if domain is not None:
            raise ValueError(
                "existing domain advancement requires an explicit domain extension"
            )
        return
    if domain is None or domain.enrollment_id != str(extension.previous_enrollment_id):
        raise ValueError("domain extension predecessor is no longer current")
    previous = db.get(NativeEnrollment, domain.enrollment_id)
    if previous is None or previous.digest != extension.previous_enrollment_digest:
        raise ValueError("domain extension predecessor digest mismatch")
    original = validated_enrollment(previous)
    from app.services.quota_cleanup import settled_root_keys

    retired = settled_root_keys(db)
    validate_domain_extension(original, manifest, retired=retired)
    # Reject before advancing a version that would orphan an oldest operation.
    list(_enrollment_chain(db, domain, previous.id, maximum=63))
    stored = db.query(NativeProjectRoot).filter_by(domain_id=domain.id).all()
    if {r.key for r in stored if r.key not in retired} != {
        root_key(r) for r in original.pins.project_roots if root_key(r) not in retired
    }:
        raise ValueError("domain extension does not cover retained domain roots")


def _enrollment_chain(db, domain, enrollment_id, *, maximum=64):
    current = db.get(NativeEnrollment, enrollment_id)
    seen = set()
    for _ in range(maximum):
        if current is None or current.id in seen or current.domain_id != domain.id:
            raise ValueError("invalid domain extension ancestry")
        seen.add(current.id)
        manifest = validated_enrollment(current)
        yield current, manifest
        extension = manifest.pins.domain_extension
        if extension is None:
            return
        previous = db.get(NativeEnrollment, str(extension.previous_enrollment_id))
        if previous is None or previous.digest != extension.previous_enrollment_digest:
            raise ValueError("domain extension predecessor unavailable")
        original = validated_enrollment(previous)
        from app.services.quota_cleanup import settled_root_keys

        validate_domain_extension(original, manifest, retired=settled_root_keys(db))
        current = previous
    raise ValueError("domain extension depth limit exceeded")


def retained_enrollment(db, domain, identity):
    """Resolve only an explicit bounded chain; never equate unrelated enrollments."""
    for current, manifest in _enrollment_chain(db, domain, domain.enrollment_id):
        if current.id == identity.get("enrollment_id"):
            pinned = [r.model_dump(mode="json") for r in manifest.pins.project_roots]
            if identity.get("root") not in pinned:
                raise ValueError("original retained root identity changed")
            return
    raise ValueError("native enrollment changed without a supported domain extension")


def enroll_native_roots(db, manifest, *, expected_target_id):
    """Recheck prepared DB identities under canonical user -> org -> project locks."""
    # model_copy is not validation. Preserve strict bool/int distinctions while
    # rechecking the entire trusted document before any enrollment write.
    manifest = VerifiedEnrollment.model_validate(
        manifest.model_dump(mode="python", warnings=False)
    )
    _validate_manifest(db, manifest, expected_target_id)
    _validate_cache_receipts(db, manifest)
    previous = (
        db.query(NativeEnrollment).filter_by(digest=manifest.digest).one_or_none()
    )
    if previous:
        return previous.id
    if manifest.request.empty_domain_bootstrap is not None and not (
        0 <= (datetime.now(UTC) - utc(manifest.verified_at)).total_seconds() <= 60
    ):
        raise ValueError("empty bootstrap verification must be fresh")
    enrollment_id, domain_id = str(uuid4()), str(manifest.pins.domain_id)
    domain = (
        db.query(NativeCollectionDomain)
        .filter_by(id=domain_id)
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    _fresh_bootstrap_domain(db, manifest, domain)
    _extension_predecessor(db, manifest, domain)
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


def _fresh_bootstrap_domain(db, manifest, domain):
    if manifest.request.empty_domain_bootstrap is None or domain is not None:
        return
    from app.models.kubevirt_cache_birth import NativeCacheBirth
    from app.models.kubevirt_storage import NativeRetainedBinding, NativeStorageSnapshot
    from app.models.native_clone_lineage import NativeCloneLineage

    domain_id = str(manifest.pins.domain_id)
    for model in (
        NativeEnrollment,
        NativeProjectRoot,
        NativeRetainedBinding,
        NativePoolClaim,
        NativeStorageSnapshot,
        NativeCacheBirth,
        NativeCloneLineage,
    ):
        if db.query(model).filter_by(domain_id=domain_id).first() is not None:
            raise ValueError("empty bootstrap cannot adopt existing domain accounting")
