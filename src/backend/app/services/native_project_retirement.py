"""Terminal whole-project storage proof and map-based orphan reclaim.

Project rows are released after namespace absence. Known private images are
reclaimed asynchronously on the durable cleanup row without collector settlement.
"""

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


def _pv_leftovers(api, namespace):
    try:
        pvs = api.rows("PersistentVolume", None)
    except Exception:
        return []
    leftover = []
    for pv in pvs:
        claim = (pv.get("spec") or {}).get("claimRef") or {}
        if claim.get("namespace") == namespace:
            leftover.append(pv.get("metadata", {}).get("name") or "?")
    return leftover


def release_after_namespace(db, cleanup_id, api=None):
    """Delete/undeploy the Project once the namespace is gone; keep the cleanup row."""
    from app.models.native_deployment_origin import NativeDeploymentOriginActive
    from app.models.provider import Provider
    from app.services import native_project_namespace_cleanup as cleanup
    from app.services.deploy_service import _destroy_cleanup_eips
    from app.services.quota_cleanup import _finish_project_record

    project, work = cleanup._locked(db, cleanup_id)
    origin.require(
        work.phase == "namespace_absent",
        "Namespace absence required before project release",
    )
    origin.require(project is not None, "Cleanup project unavailable before release")
    if TYPE_CHECKING:
        assert project is not None
    client = None
    if api is None:
        from app.services.native_namespace_client import NamespaceClient
        from app.services.providers.kubevirt import _get_k8s_clients

        provider = db.get(Provider, str(work.authority.provider_id))
        db.commit()
        _, _, client = _get_k8s_clients(provider)
        api = NamespaceClient(client)
        project, work = cleanup._locked(db, cleanup_id)
    try:
        origin.require(
            api.get("Namespace", None, work.authority.namespace) is None,
            "Namespace still present during project release",
        )
        leftovers = _pv_leftovers(api, work.authority.namespace)
        origin.require(
            not leftovers,
            "Project PV leftovers remain: " + ",".join(leftovers[:5]),
        )
    finally:
        if client is not None:
            client.close()

    project, current = cleanup._locked(db, cleanup_id, work)
    origin.require(project is not None, "Cleanup project unavailable before release")
    if TYPE_CHECKING:
        assert project is not None
    project_id = project.id
    dns = dict(
        dns_provider_id=project.dns_provider_id,
        domain=project.domain,
        topology=project.deployed_topology or project.topology or {},
    )
    mode = current.mode
    # External DNS/EIP must succeed before releasing the Project row so a failed
    # callback can retry without stranding orphan_reclaim mid-flight.
    db.commit()
    _release_dns(db, dns)
    _destroy_cleanup_eips(db, project_id)
    from app.models.elastic_ip import ElasticIp

    origin.require(
        not db.query(ElasticIp).filter_by(project_id=project_id).first(),
        "External EIP release remains pending",
    )
    project, current = cleanup._locked(db, cleanup_id)
    origin.require(project is not None, "Cleanup project unavailable before release")
    if TYPE_CHECKING:
        assert project is not None
    cleanup._evidence(
        db,
        current,
        "project-released",
        dict(
            project_id=project_id,
            mode=mode,
            liability_digest=current.liability_digest,
            private_images=[
                k.model_dump(mode="json") for k in current.liabilities.private_images
            ],
        ),
    )
    work = cleanup._save(
        db,
        current,
        phase="orphan_reclaim",
        last_error="Project released; reclaiming known private storage orphans",
    )
    if work.authority.source_kind == "origin":
        pointer = db.get(NativeDeploymentOriginActive, project_id)
        if pointer and pointer.intent_id == str(work.authority.source_id):
            db.delete(pointer)
    _finish_project_record(db, project, mode)
    return load_work(db, cleanup_id)


def load_work(db, cleanup_id):
    from app.services import native_project_namespace_cleanup as cleanup

    return cleanup.load(db.get(cleanup.State, str(cleanup_id)))


def reclaim_orphans(db, cleanup_id, *, core_api=None):
    """Bounded known-ID / soft-target reclaim; finalize when the map is clear."""
    from app.models.provider import Provider
    from app.services import native_orphan_reclaim as orphans
    from app.services import native_project_namespace_cleanup as cleanup
    from app.services.native_teardown_map import reclaim_targets

    _, work = cleanup._locked(db, cleanup_id)
    origin.require(
        work.phase == "orphan_reclaim",
        "Orphan reclaim requires released cleanup state",
    )
    private = list(work.liabilities.private_images)
    soft = reclaim_targets(db, work)
    if not private and not soft:
        return orphans.finalize_if_clear(db, work, [])

    client = None
    if core_api is None:
        from app.services.providers.kubevirt import _get_k8s_clients

        provider = db.get(Provider, str(work.authority.provider_id))
        db.commit()
        core_api, _, client = _get_k8s_clients(provider)
        _, work = cleanup._locked(db, cleanup_id)
    try:
        remaining = orphans.attempt(db, work, core_api=core_api)
    finally:
        if client is not None and hasattr(client, "close"):
            client.close()
    _, work = cleanup._locked(db, cleanup_id)
    return orphans.finalize_if_clear(db, work, remaining)


def settle(db, cleanup_id, api=None, *, core_api=None):
    """Release the Project after NS absence, then reclaim known orphans.

    Collector settlement reports are no longer required to drop the Project row.
    """
    from app.services import native_project_namespace_cleanup as cleanup

    _, work = cleanup._locked(db, cleanup_id)
    if work.phase == "finalized":
        db.commit()
        return work
    if work.phase == "namespace_absent":
        work = release_after_namespace(db, cleanup_id, api=api)
    if work.phase == "orphan_reclaim":
        return reclaim_orphans(db, cleanup_id, core_api=core_api)
    if work.phase == "settled":
        return finish(db, cleanup_id)
    origin.require(False, "Cleanup phase does not require settlement")


def finish(db, cleanup_id):
    """Legacy collector-settled finalization (pre map-based release)."""
    from app.models.native_deployment_origin import NativeDeploymentOriginActive
    from app.services import native_project_namespace_cleanup as cleanup
    from app.services.deploy_service import _destroy_cleanup_eips
    from app.services.quota_cleanup import _finish_project_record

    project, work = cleanup._locked(db, cleanup_id)
    if work.phase == "finalized":
        db.commit()
        return
    if project is None:
        origin.require(
            work.phase in {"orphan_reclaim", "settled"},
            "Durable cleanup settlement required",
        )
        cleanup._save(db, work, phase="finalized")
        db.commit()
        return
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
