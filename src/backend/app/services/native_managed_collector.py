"""One bounded existing-collector cycle; protected pins stay the authority.

Database sessions end before network reads. The same enrollment/import helpers
own persisted evidence; application payloads cannot promote authorization.
"""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from app.models.kubevirt_storage import (
    NativeCollectionDomain,
    NativeReportingTarget,
    NativeRetainedBinding,
    NativeStorageSnapshot,
)
from app.models.project import Project
from app.models.quota import QuotaReservation
from app.schemas.kubevirt_storage import (
    Blocker,
    CacheBinding,
    NativeInventory,
    NativeStorageReport,
    ProjectRoot,
    VolumeBinding,
)
from app.schemas.kubevirt_storage_enrollment import DomainExtension
from app.services import native_initialization as initialization
from app.services import native_managed_deploy as deploy
from app.services import quota_admission as admission
from app.services import quota_operations as operations
from app.services.kubevirt_storage_enrollment import (
    _enrollment_chain,
    digest,
    enroll_native_roots,
    prepare_native_enrollment,
    verify_native_enrollment,
)
from app.services.kubevirt_storage_inventory import inventory_digest
from app.services.kubevirt_storage_reports import import_report


def anchored(db, pins):
    domain = db.get(NativeCollectionDomain, str(pins.domain_id))
    if domain is None:
        raise ValueError("managed collection requires an existing protected domain")
    chain = list(_enrollment_chain(db, domain, domain.enrollment_id))
    if not any(digest(manifest.pins) == digest(pins) for _, manifest in chain):
        raise ValueError("current enrollment has no exact protected pins anchor")
    current = chain[0][1]
    if current.pins.initialization_evidence != pins.initialization_evidence:
        raise ValueError(
            "managed collector cannot adopt changed initialization authorization"
        )
    return domain, current


def candidates(db, domain, manifest):
    from app.services.kubevirt_storage_enrollment import root_key
    from app.services.quota_cleanup import settled_root_keys

    retired = settled_root_keys(db)
    roots = {
        str(r.project_id): r
        for r in manifest.pins.project_roots
        if root_key(r) not in retired
    }
    selected = []
    for row in db.query(QuotaReservation).filter(
        QuotaReservation.state.in_(admission.PENDING)
    ):
        target = row.dispatch_metadata.get("native_deploy", {})
        if target.get("domain_id") != domain.id or target.get("phase") != "bound":
            continue
        if row.dispatch_metadata.get("cancelled") or not row.dispatch_metadata.get(
            "claim"
        ):
            continue
        project = db.get(Project, row.project_id)
        deploy.validate(db, project, row, physical=False)
        root = ProjectRoot.model_validate(target["root"])
        if str(root.project_id) in roots:
            if roots[str(root.project_id)] != root:
                raise ValueError("admitted native root conflicts with enrolled root")
            continue
        roots[str(root.project_id)] = root
        selected.append((row.operation_id, deepcopy(target)))
    return list(roots.values()), selected


def all_operation_ids(db, project_ids):
    return sorted(
        r.operation_id
        for r in db.query(QuotaReservation).filter(
            QuotaReservation.project_id.in_(project_ids)
        )
    )


def retained_inputs(sessions, domain_id):
    """Durable binding hints survive failed attempts; never copied measurements."""
    volumes, caches = [], []
    with sessions() as db:
        from app.models.kubevirt_storage import NativeEnrollment
        from app.services.kubevirt_storage_enrollment import (
            root_key,
            validated_enrollment,
        )
        from app.services.quota_cleanup import settled_root_keys

        retired = settled_root_keys(db)
        domain = db.get(NativeCollectionDomain, str(domain_id))
        manifest = validated_enrollment(db.get(NativeEnrollment, domain.enrollment_id))
        roots = {root_key(r): r for r in manifest.pins.project_roots}
        for row in db.query(NativeRetainedBinding).filter_by(domain_id=str(domain_id)):
            if row.root_key in retired:
                continue
            if "root" in row.payload:
                binding = VolumeBinding.model_validate(row.payload)
                pinned = roots.get(root_key(binding.root))
                if pinned is None:
                    raise ValueError(
                        "retained binding root is outside current enrollment"
                    )
                volumes.append(binding.model_copy(update={"root": pinned}))
            else:
                caches.append(CacheBinding.model_validate(row.payload))
    return volumes, caches


def retiring_inputs(sessions, domain_id):
    from app.services.quota_cleanup import native_intents, settled_root_keys

    with sessions() as db:
        return (
            [
                v
                for v in native_intents(db)
                if str(v.domain_id) == str(domain_id)
                and v.phase in {"teardown", "pending"}
            ],
            settled_root_keys(db),
        )


def snapshot(sessions, pins, target_id):
    with sessions() as db:
        domain, manifest = anchored(db, pins)
        reporting = db.get(NativeReportingTarget, 1)
        if reporting is None or reporting.target_id != target_id:
            raise ValueError("managed collector reporting target changed")
        roots, selected = candidates(db, domain, manifest)
        request = (
            prepare_native_enrollment(db, [str(r.project_id) for r in roots])
            if selected
            else None
        )
        ids = all_operation_ids(db, [str(r.project_id) for r in roots])
        previous = (
            db.get(NativeStorageSnapshot, domain.latest_report_id)
            if domain.latest_report_id
            else None
        )
        result = {
            "manifest": manifest,
            "enrollment_id": domain.enrollment_id,
            "request": request,
            "roots": roots,
            "selected": selected,
            "operations": ids,
            "previous": deepcopy(previous.payload) if previous else None,
        }
        from app.services.native_managed_verification import pending

        result["images"] = pending(db, domain)
        db.commit()
        return result


def verify_new_roots(work, readers, backend):
    manifest = work["manifest"]
    profile = manifest.pins.initialization_evidence
    initialization.observe(
        profile,
        readers[str(profile.target.provider_id)],
        backend,
        clock=lambda: datetime.now(UTC),
    )
    for operation_id, target in work["selected"]:
        reader = readers[target["provider_id"]]
        result = reader.pending_root(target, operation_id, profile)
        if result != target["root"]:
            raise ValueError("independently observed pending root changed")
    pins = manifest.pins.model_copy(
        update={
            "project_roots": work["roots"],
            "empty_domain_bootstrap": None,
            "domain_extension": DomainExtension(
                version="native-domain-extension-v1",
                previous_enrollment_id=work["enrollment_id"],
                previous_enrollment_digest=manifest.digest,
            ),
        }
    )
    # The protected authorization envelope is copied unchanged. In particular,
    # this roots-only extension never supplies initialization_authorization_digest.
    return verify_native_enrollment(work["request"], readers, backend, pins=pins)


def apply_extension(sessions, anchor, work, verified, target_id):
    with sessions() as db:
        admission.lock_operations(db, work["operations"])
        domain, current = anchored(db, anchor)
        if domain.enrollment_id != work["enrollment_id"]:
            raise ValueError(
                "managed collector predecessor changed; restart complete cycle"
            )
        for operation_id, target in work["selected"]:
            row = (
                db.query(QuotaReservation)
                .filter_by(operation_id=operation_id)
                .populate_existing()
                .one()
            )
            if (
                row.dispatch_metadata.get("native_deploy") != target
                or row.state not in admission.PENDING
                or row.dispatch_metadata.get("cancelled")
            ):
                raise ValueError(
                    "managed pending operation changed during independent verification"
                )
        result = enroll_native_roots(db, verified, expected_target_id=target_id)
        from app.services.native_collection_jobs import assert_collection_lease

        assert_collection_lease()
        db.commit()
        return result


def scoped_roots(manifest, project_id=None):
    roots = manifest.pins.project_roots
    if project_id is None:
        return roots
    selected = [r for r in roots if str(r.project_id) == str(project_id)]
    if len(selected) != 1:
        raise ValueError("project collection requires exactly one enrolled root")
    return selected


def project_snapshot(sessions, pins, target_id, project_id):
    from app.services.kubevirt_storage_reports import report_cursor

    with sessions() as db:
        domain, manifest = anchored(db, pins)
        reporting = db.get(NativeReportingTarget, 1)
        if reporting is None or reporting.target_id != target_id:
            raise ValueError("managed collector reporting target changed")
        scoped_roots(manifest, project_id)
        cursor = report_cursor(db, domain, project_id)
        previous = (
            db.get(NativeStorageSnapshot, cursor.latest_report_id)
            if cursor.latest_report_id
            else None
        )
        return {
            "manifest": manifest,
            "previous": deepcopy(previous.payload) if previous else None,
            "selected": [],
            "images": [],
        }


def failed_report(manifest, collector_id, started, project_id=None):
    roots = scoped_roots(manifest, project_id)
    blockers = [
        Blocker(code="project_discovery_unavailable", project_id=r.project_id)
        for r in roots
    ]
    if not blockers:
        blockers = [Blocker(code="project_discovery_unavailable")]
    inventory = NativeInventory(
        collector_id=collector_id,
        project_scope=project_id,
        project_roots=roots,
        collected_at=started,
        blockers=blockers,
    )
    return NativeStorageReport(
        report_id=uuid4(),
        collector_id=collector_id,
        project_scope=project_id,
        started_at=started,
        completed_at=datetime.now(UTC),
        inventory_digest=inventory_digest(inventory),
        cluster_keys=sorted({r.cluster_key for r in roots}),
        pool_keys=manifest.pins.pool_keys,
        project_roots=roots,
        volume_bindings=[],
        images=[],
        retained_bindings=[],
        collection_errors=blockers,
        inventory_blockers=blockers,
    )


def reconcile_allocations(sessions, manifest, project_id=None):
    from app.models.user import User
    from app.services import quota_storage
    from app.services.quota_locks import lock_rows

    with sessions() as db:
        from app.services.kubevirt_storage_enrollment import root_key
        from app.services.quota_cleanup import settled_root_keys

        retired = settled_root_keys(db)
        ids = [
            str(r.project_id)
            for r in scoped_roots(manifest, project_id)
            if root_key(r) not in retired
        ]
        operation_ids = all_operation_ids(db, ids)
        admission.lock_operations(db, operation_ids)
        owners = (
            db.query(Project.owner_id, User.organization_id)
            .join(User, User.id == Project.owner_id)
            .filter(Project.id.in_(ids))
            .all()
        )
        lock_rows(
            db,
            user_ids=[r.owner_id for r in owners],
            organization_ids=[r.organization_id for r in owners if r.organization_id],
            project_ids=ids,
        )
        if all_operation_ids(db, ids) != operation_ids:
            raise ValueError("native operation membership changed during collection")
        for project in (
            db.query(Project).filter(Project.id.in_(ids)).order_by(Project.id)
        ):
            rows = operations.current_generation(operations._rows(db, project.id))
            if rows and all(
                r.dispatch_metadata.get("native_storage_identity") for r in rows
            ):
                quota_storage.apply_measurement(
                    db, project, rows, None, operations.now()
                )
        from app.services.native_collection_jobs import assert_collection_lease

        assert_collection_lease()
        db.commit()
    from app.services.quota_cleanup import native_intents, reconcile_native

    with sessions() as db:
        pending = [
            (str(v.root.project_id), str(v.intent_id))
            for v in native_intents(db)
            if str(v.domain_id) == str(manifest.pins.domain_id)
            and v.phase != "finalized"
            and (project_id is None or str(v.root.project_id) == str(project_id))
        ]
    for project_id, intent_id in pending:
        assert_collection_lease()
        reconcile_native(project_id, intent_id)


def prepare_work(sessions, pins, target_id, readers, backend):
    """Coordinate enrollment and image verification once per domain."""
    from app.services.native_collection_jobs import assert_collection_lease
    from app.services.native_managed_verification import (
        publish_sources,
        verify_clones,
        verify_sources,
    )

    assert_collection_lease()
    work = snapshot(sessions, pins, target_id)
    if work["selected"]:
        verified = verify_new_roots(work, readers, backend)
        assert_collection_lease()
        apply_extension(sessions, pins, work, verified, target_id)
    if work["images"]:
        profile = work["manifest"].pins.initialization_evidence
        references = verify_sources(sessions, work["images"], readers, backend, profile)
        assert_collection_lease()
        publish_sources(sessions, pins, target_id, references, readers, backend)
        assert_collection_lease()
        verify_clones(sessions, work["images"], readers, backend, profile)
    return work


def cycle(
    sessions,
    pins,
    target_id,
    collector_id,
    readers,
    backend,
    collect,
    *,
    project_id=None,
):
    """Collect a domain or an explicitly scoped project without changing authority."""
    started = datetime.now(UTC)
    try:
        work = (
            prepare_work(sessions, pins, target_id, readers, backend)
            if project_id is None
            else project_snapshot(sessions, pins, target_id, project_id)
        )
        with sessions() as db:
            domain, manifest = anchored(db, pins)
            enrollment_id = domain.enrollment_id
        report = collect(manifest, work["previous"], started)
        profile = manifest.pins.initialization_evidence
        if profile is None:
            raise ValueError(
                "managed collection requires protected initialization evidence"
            )
        observation = (
            initialization.observe(
                profile,
                readers[str(profile.target.provider_id)],
                backend,
                clock=lambda: datetime.now(UTC),
            )
            if project_id is None
            else None
        )
        # Never stamp an older observation with a new receipt time.
        report = report.model_copy(
            update={
                "initialization_observation": observation,
                "completed_at": datetime.now(UTC),
            }
        )
    except Exception:
        with sessions() as db:
            domain, manifest = anchored(db, pins)
            enrollment_id = domain.enrollment_id
        report = failed_report(manifest, collector_id, started, project_id)
    if str(report.project_scope) != str(project_id):
        raise ValueError("collection returned a different project scope")
    from app.services.native_collection_jobs import assert_collection_lease

    assert_collection_lease()
    with sessions() as db:
        import_report(
            db,
            report,
            enrollment_id=enrollment_id,
            expected_enrollment_digest=manifest.digest,
            now=datetime.now(UTC),
        )
        assert_collection_lease()
        db.commit()
    try:
        from app.services.native_trash_cleanup import reconcile

        assert_collection_lease()
        reconcile(sessions, report, readers, backend)
        assert_collection_lease()
        reconcile_allocations(sessions, manifest, project_id)
    except Exception:
        pass  # Imported partial attempts invalidate readiness; old charges remain.
    return report
