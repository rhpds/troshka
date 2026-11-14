"""Existing scheduled collector's bounded pending-image verification phases."""

from copy import deepcopy

from app.models.kubevirt_cache_birth import NativeCacheBirth
from app.models.native_clone_lineage import NativeCloneLineage
from app.models.quota import QuotaReservation
from app.schemas.kubevirt_storage import CloneReference
from app.schemas.kubevirt_storage_enrollment import DomainExtension


def pending(db, domain):
    from app.models.project import Project
    from app.services import native_managed_deploy as deploy

    selected = []
    for row in db.query(QuotaReservation).filter(
        QuotaReservation.state.in_(("pending", "dispatched", "reconciling"))
    ):
        # Cancelled allocations remain charged until retirement proves absence;
        # they must no longer enter image verification or create transfer.
        if row.dispatch_metadata.get("cancelled"):
            continue
        target = row.dispatch_metadata.get("native_deploy", {})
        if target.get("domain_id") != domain.id or not target.get("sources"):
            continue
        project = db.get(Project, row.project_id)
        # The API commits admission before placement. Validate that original
        # draft against its input digest until placement has actually committed.
        unplaced = (
            target.get("phase") == "reserved"
            and not row.dispatch_metadata.get("claim")
            and project.state == "draft"
        )
        deploy.validate(db, project, row, placed=not unplaced, physical=False)
        selected.append((row.operation_id, deepcopy(target)))
    if len(selected) > 100:
        raise ValueError("pending managed image verification limit")
    return selected


def verify_sources(sessions, selected, readers, backend, profile):
    from app.services.native_cache_birth import verify_cache_birth
    from app.services.native_cache_provider import CacheVerificationAdapter
    from app.services.native_managed_images import source_client

    references = []
    with sessions() as db:
        client = source_client(db, profile.source_policy)
    try:
        adapter = CacheVerificationAdapter(
            readers[str(profile.target.provider_id)], backend, client, profile
        )
        for operation_id, target in selected:
            for source in target["sources"].values():
                with sessions() as db:
                    row = db.get(NativeCacheBirth, source["operation_id"])
                    state = row.state if row else None
                if state not in {"created", "verified"}:
                    continue
                try:
                    reference = verify_cache_birth(
                        sessions, source["operation_id"], adapter
                    )
                except ValueError:
                    diagnostic(
                        sessions, operation_id, target, "cache_verification_pending"
                    )
                    continue  # Provisioning is observed again next scheduled cycle.
                references.append(reference)
    finally:
        client.close()
    return references


def publish_sources(sessions, anchor, target_id, references, readers, backend):
    from app.services.kubevirt_storage_enrollment import (
        cache_authorization_digest,
        enroll_native_roots,
        prepare_native_enrollment,
        verify_native_enrollment,
    )
    from app.services.native_managed_collector import anchored

    with sessions() as db:
        domain, manifest = anchored(db, anchor)
        added = [r for r in references if r not in manifest.pins.cache_birth_receipts]
        if not added:
            return
        selected = pending(db, domain)
        allowed = {
            v["operation_id"] for _, t in selected for v in t["sources"].values()
        }
        if any(str(r.operation_id) not in allowed for r in added):
            raise ValueError("cache source operation changed before enrollment")
        request = prepare_native_enrollment(
            db, [str(r.project_id) for r in manifest.pins.project_roots]
        )
        previous = domain.enrollment_id
        pins = manifest.pins.model_copy(
            update={
                "cache_birth_receipts": [*manifest.pins.cache_birth_receipts, *added]
            }
        )
        pins = pins.model_copy(
            update={
                "domain_extension": DomainExtension(
                    version="native-domain-extension-v1",
                    previous_enrollment_id=previous,
                    previous_enrollment_digest=manifest.digest,
                    cache_authorization_digest=cache_authorization_digest(pins),
                )
            }
        )
        db.commit()
    verified = verify_native_enrollment(request, readers, backend, pins=pins)
    with sessions.begin() as db:
        domain, _ = anchored(db, anchor)
        if domain.enrollment_id != previous or pending(db, domain) != selected:
            raise ValueError("cache enrollment predecessor or operation changed")
        enroll_native_roots(db, verified, expected_target_id=target_id)


def lineage_inputs(sessions, manifest):
    from app.services.native_clone_lineage import resolve_lineages

    with sessions() as db:
        references = [
            CloneReference(lineage_id=r.id, digest=r.proof_digest)
            for r in db.query(NativeCloneLineage).filter_by(
                domain_id=str(manifest.pins.domain_id)
            )
            if r.proof_digest
        ]
        return references, resolve_lineages(db, references, manifest)


def transfer_creation(sessions, operation_id, target, reader):
    from app.services import native_managed_deploy as deploy
    from app.services.native_clone_lineage import record_clone_created
    from app.services.native_managed_power import helpers

    api = helpers().PowerClient(reader.api)
    root = deploy.power._live_root(
        api, {"root": target["root"], "generation": operation_id}, operation_id
    )
    wanted = helpers().intent(root)
    request = target["initialization_request"]
    unpublished = request[
        "phase"
    ] == "initializing" and wanted == deploy.publication_predecessor(request)
    if wanted != request and not unpublished:
        raise ValueError("clone initialization request changed")
    if root["spec"] != target["payload"]["spec"]:
        raise ValueError("original clone root plan changed")
    with sessions.begin() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        if row.dispatch_metadata.get(
            "native_deploy"
        ) != target or row.dispatch_metadata.get("cancelled"):
            raise ValueError("clone creation operation changed")
        if unpublished:
            return False
        creates = root.get("status", {}).get("managedDeployment", {}).get("creates", {})
        planned = {
            v["metadata"]["name"]: v
            for v in deploy.helpers().render_observed(root, wanted["storageClass"])
            if v["kind"] == "DataVolume"
        }
        for claim, lineage in target.get("clone_lineages", {}).items():
            original = creates.get("DataVolume:" + claim, {})
            if original.get("uid"):
                if original.get("intentDigest") != helpers().digest(planned[claim]):
                    raise ValueError("original clone create plan changed")
                record_clone_created(
                    db,
                    lineage,
                    dict(
                        dv_uid=original["uid"],
                        dv_name=claim,
                        namespace_uid=target["root"]["namespace_uid"],
                        operation_id=operation_id,
                    ),
                )
    return True


def verify_clones(sessions, selected, readers, backend, profile):
    from app.services.native_clone_lineage import verify_clone
    from app.services.native_clone_provider import CloneVerificationAdapter
    from app.services.native_managed_images import source_client
    from app.services.native_managed_power import helpers

    with sessions() as db:
        client = source_client(db, profile.source_policy)
    try:
        reader = readers[str(profile.target.provider_id)]
        adapter = CloneVerificationAdapter(sessions, reader, backend, client, profile)
        for operation_id, target in selected:
            if target["phase"] != "initialization-intent":
                continue
            if not transfer_creation(sessions, operation_id, target, reader):
                continue
            for identity in target.get("clone_lineages", {}).values():
                try:
                    verify_clone(sessions, identity, adapter)
                except (ValueError, KeyError, helpers().ManagedPowerError):
                    diagnostic(
                        sessions, operation_id, target, "clone_verification_pending"
                    )
                    continue
    finally:
        client.close()


def diagnostic(sessions, operation_id, target, code):
    """Sanitized operation diagnostics do not alter frozen source/plan authority."""
    with sessions.begin() as db:
        row = (
            db.query(QuotaReservation)
            .filter_by(operation_id=operation_id)
            .one_or_none()
        )
        if row is not None and row.dispatch_metadata.get("native_deploy") == target:
            row.dispatch_metadata = {
                **row.dispatch_metadata,
                "native_image_diagnostic": code,
            }
