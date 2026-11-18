"""Terminal whole-project storage proof; ordinary snapshot source checks stay strict."""

from typing import TYPE_CHECKING

from app.schemas.native_project_cleanup import ProjectRetirement
from app.services import native_deployment_origin as origin
from app.services.kubevirt_storage_enrollment import digest, root_identity


def verify_project_retirement(scope, inventory, images):
    """Only caller-resolved DB authority may supply scope; reports never do."""
    origin.require(
        not scope.liabilities.unresolved,
        "Cleanup storage identity or producer outcome remains unresolved",
    )
    origin.require(
        all(
            boundary.namespace_uid == scope.root.namespace_uid
            for boundary in scope.liabilities.support_boundaries
        ),
        "Support execution namespace boundary changed",
    )
    proofs = [p for p in inventory.retiring_roots if p.intent_id == scope.intent_id]
    origin.require(len(proofs) == 1, "Cleanup retiring root evidence missing")
    proof = proofs[0]
    origin.require(
        root_identity(proof.root) == root_identity(scope.root)
        and proof.operation_id == scope.operation_id
        and proof.inventory_digest == scope.inventory_digest
        and set(proof.owned_images) == set(scope.owned_images)
        and proof.objects_absent
        and proof.observed_at >= scope.teardown_at,
        "Cleanup namespace/PV/snapshot-content absence unavailable",
    )
    origin.require(
        digest(scope.liabilities.model_dump(mode="json")) == scope.inventory_digest,
        "Cleanup storage liability digest changed",
    )
    observations = {image.key: image for image in images}
    origin.require(
        len(observations) == len(images), "Duplicate cleanup image observation"
    )
    for key in scope.owned_images:
        image = observations.get(key)
        origin.require(
            image is not None
            and image.absence is not None
            and image.absence.excludes(key)
            and image.measurement_status == "complete"
            and image.collected_at >= proof.observed_at,
            "Private cleanup image directory/trash absence unavailable",
        )
    for key in scope.liabilities.shared_images:
        image = observations.get(key)
        origin.require(
            image is not None
            and image.absence is None
            and image.measurement_status == "complete"
            and image.collected_at >= proof.observed_at,
            "Shared cleanup ancestry survival unavailable",
        )
    origin.require(
        not set(scope.owned_images) & set(scope.liabilities.shared_images),
        "Shared/private cleanup overlap",
    )
    return ProjectRetirement(
        cleanup_id=scope.intent_id,
        liability_digest=scope.inventory_digest,
        root=scope.root,
        private_images=scope.owned_images,
        shared_images=scope.liabilities.shared_images,
        observed_at=proof.observed_at,
    )


def verified_retirements(scopes, inventory, images):
    from app.schemas.kubevirt_storage import Blocker
    from app.schemas.quota import AccountingUnavailable

    verified, errors = [], []
    roots = [root_identity(root) for root in inventory.project_roots]
    for scope in scopes:
        if (
            inventory.project_scope not in (None, scope.root.project_id)
            or root_identity(scope.root) not in roots
        ):
            continue
        try:
            verified.append(verify_project_retirement(scope, inventory, images))
        except AccountingUnavailable:
            errors.append(
                Blocker(
                    code="project_namespace_storage_pending",
                    project_id=scope.root.project_id,
                )
            )
    return verified, errors


def settle(db, cleanup_id):
    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeEnrollment,
        NativeProjectRoot,
    )
    from app.services import native_project_namespace_cleanup as cleanup
    from app.services import quota_cleanup, quota_operations, quota_storage
    from app.services.kubevirt_storage_enrollment import validated_enrollment
    from app.services.kubevirt_storage_reports import report_inventory

    project, work = cleanup._locked(db, cleanup_id)
    origin.require(
        work.phase in {"namespace_absent", "settled"},
        "Namespace absence required before storage settlement",
    )
    if work.phase == "settled":
        db.commit()
        return finish(db, cleanup_id)
    selected = work.authority
    origin.require(
        selected.domain_id is not None and selected.root is not None,
        "Storage scope unavailable; debt retained",
    )
    domain = db.get(NativeCollectionDomain, str(selected.domain_id))
    manifest = validated_enrollment(db.get(NativeEnrollment, domain.enrollment_id))
    roots = db.query(NativeProjectRoot).filter_by(domain_id=domain.id).all()
    graph, source, row, _ = quota_cleanup._cleanup_report(
        db, domain, roots, manifest, project.id
    )
    scope = next(v for v in cleanup.storage_scopes(db) if v.intent_id == work.id)
    proof = verify_project_retirement(scope, report_inventory(source), source.images)
    allocation = [p for p in graph.projects if p.project_id == selected.project_id]
    origin.require(
        len(allocation) == 1
        and allocation[0].coverage == "complete"
        and allocation[0].allocated_bytes == 0
        and not allocation[0].blockers,
        "Cleanup storage allocation remains pending",
    )
    rows = quota_operations.current_generation(quota_operations._rows(db, project.id))
    if rows:
        from app.services import quota_native

        identity = quota_storage.measurement_row(rows).dispatch_metadata[
            "native_storage_identity"
        ]
        measurement = quota_native.storage_measurement(
            db,
            project,
            quota_operations._generation(rows[0]),
            identity,
            quota_operations.now(),
        )
        origin.require(
            str(measurement.report_id) == row.id and measurement.allocated_bytes == 0,
            "Cleanup accounting report changed",
        )
        quota_storage.apply_measurement(
            db, project, rows, measurement, quota_operations.now()
        )
    quota_cleanup.finalize_absence(db, project, proof.observed_at)
    cleanup._evidence(
        db,
        work,
        "storage_settled",
        dict(report_id=row.id, proof=proof.model_dump(mode="json")),
    )
    cleanup._save(
        db,
        work,
        phase="settled",
        settlement_report_id=row.id,
        settled_at=proof.observed_at,
        last_error=None,
    )
    from app.services.metering_service import _freeze_quota_invoice

    _freeze_quota_invoice(db, project, proof.observed_at, refresh_live=False)
    db.commit()
    return finish(db, cleanup_id)


def finish(db, cleanup_id):
    from app.models.native_deployment_origin import NativeDeploymentOriginActive
    from app.services import native_project_namespace_cleanup as cleanup
    from app.services.deploy_service import _destroy_cleanup_eips
    from app.services.quota_cleanup import _finish_project_record

    project, work = cleanup._locked(db, cleanup_id)
    origin.require(
        work.phase == "settled" and work.settlement_report_id,
        "Durable cleanup settlement required",
    )
    project_id = project.id
    dns = dict(
        dns_provider_id=project.dns_provider_id,
        domain=project.domain,
        topology=project.deployed_topology or project.topology or {},
    )
    db.commit()
    _release_dns(db, dns)
    _destroy_cleanup_eips(db, project_id)
    from app.models.elastic_ip import ElasticIp

    origin.require(
        not db.query(ElasticIp).filter_by(project_id=project_id).first(),
        "External EIP release remains pending",
    )
    project, current = cleanup._locked(db, cleanup_id, work)
    cleanup._save(db, current, phase="finalized")
    if current.authority.source_kind == "origin":
        pointer = db.get(NativeDeploymentOriginActive, project_id)
        origin.require(
            pointer and pointer.intent_id == str(current.authority.source_id),
            "Cleanup origin pointer changed",
        )
        db.delete(pointer)
    _finish_project_record(db, project, current.mode)


def _release_dns(db, context):
    from app.models.dns_provider import DnsProvider
    from app.services.dns_service import delete_dns_records

    records = context["topology"].get("_dns_records", [])
    if not records:
        return
    provider = (
        db.get(DnsProvider, context["dns_provider_id"])
        if context["dns_provider_id"]
        else None
    )
    origin.require(
        provider is not None,
        "External DNS provider unavailable; records remain pending",
    )
    if TYPE_CHECKING:
        assert provider is not None
    provider_type, config = provider.type, provider.config
    db.commit()
    errors = delete_dns_records(provider_type, config, records)
    origin.require(not errors, "External DNS removal remains pending")
