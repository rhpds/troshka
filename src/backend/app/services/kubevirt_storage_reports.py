"""Append-only report import. No network clients, quota ledger or billing writes."""


from app.models.host import Host
from app.models.kubevirt_storage import (
    NativeCollectionDomain,
    NativeEnrollment,
    NativeProjectRoot,
    NativeRetainedBinding,
    NativeStorageSnapshot,
)
from app.models.project import Project
from app.schemas.kubevirt_storage import (
    Blocker,
    NativeInventory,
    NativeStorageReport,
    PoolKey,
)
from app.schemas.kubevirt_storage_enrollment import VerifiedEnrollment
from app.services.kubevirt_storage_enrollment import (
    digest,
    root_identity,
    root_key,
    utc,
)
from app.services.kubevirt_storage_graph import attribute_graph
from app.services.kubevirt_storage_inventory import inventory_digest


def report_inventory(report):
    """Reconstruct all original evidence, including unresolved CSI snapshots."""
    return NativeInventory(
        collector_id=report.collector_id,
        collected_at=report.started_at,
        project_roots=report.project_roots,
        volume_bindings=report.volume_bindings,
        retained_bindings=report.retained_bindings,
        cache_bindings=report.cache_bindings,
        blockers=report.inventory_blockers
        if report.inventory_blockers is not None
        else report.collection_errors,
        snapshot_evidence=report.snapshot_evidence,
        retiring_roots=report.retiring_roots,
        clone_references=report.clone_references,
    )


def normalized_inventory(report, roots, retained=()):
    inventory = report_inventory(report)
    # Provider aliases may repeat exact roots; identity, not provider count, owns bytes.
    expected_keys = {r.key for r in roots}
    present = {
        root_key(r): r for r in report.project_roots if root_key(r) in expected_keys
    }
    blockers = list(
        {digest(b): b for b in inventory.blockers + report.collection_errors}.values()
    )
    blockers.extend(
        Blocker(code="csi_snapshot_backend_unresolved", project_id=s.root.project_id)
        for s in report.snapshot_evidence
    )
    for root in roots:
        if root.key not in present:
            blockers.append(
                Blocker(code="expected_root_missing", project_id=root.project_id)
            )
    supplied = {
        binding_key(b)
        for b in report.volume_bindings
        + report.retained_bindings
        + report.cache_bindings
    }
    for binding in retained:
        if binding.key not in supplied:
            project_id = binding.payload.get("root", {}).get("project_id")
            blockers.append(
                Blocker(code="retained_binding_missing", project_id=project_id)
            )

    # CR generations describe scan consistency, not an immutable ownership root.
    def canonical_root(root):
        return root.model_copy(update={"deployment_generation": None})

    def canonical_binding(binding):
        return binding.model_copy(update={"root": canonical_root(binding.root)})

    return inventory.model_copy(
        update={
            "project_roots": [canonical_root(r) for r in present.values()],
            "blockers": blockers,
            "volume_bindings": [
                canonical_binding(b) for b in inventory.volume_bindings
            ],
            "retained_bindings": [
                canonical_binding(b) for b in inventory.retained_bindings
            ],
            "retiring_roots": [
                r.model_copy(update={"root": canonical_root(r.root)})
                for r in inventory.retiring_roots
                if root_key(r.root) in expected_keys
            ],
        }
    )


def binding_key(binding):
    if hasattr(binding, "root"):
        identity = {"root": root_key(binding.root), "pvc_uid": binding.pvc_uid}
    else:
        identity = {
            "cluster": binding.cluster_key,
            "namespace": binding.namespace_uid,
            "pvc_uid": binding.pvc_uid,
        }
    return digest(identity)


def _binding_identity(binding):
    identity = binding.model_dump(
        mode="json", exclude={"provisioned_bytes", "owner_reference_uids"}
    )
    if hasattr(binding, "root"):
        identity["root"] = root_identity(binding.root)
    return identity


def _live_identity_errors(db, roots, manifest):
    errors = set()
    aliases = {
        str(p.provider_id): p.cluster_key for p in manifest.pins.provider_clusters
    }
    for root in roots:
        row = (
            db.query(Project.provider_id, Host.provider_id.label("host_provider_id"))
            .outerjoin(Host, Host.id == Project.host_id)
            .filter(Project.id == root.project_id)
            .one_or_none()
        )
        if row is None:
            continue  # Retained root, deliberately admin-only in read projection.
        if (
            row.host_provider_id not in aliases
            or aliases.get(row.host_provider_id) != root.payload["cluster_key"]
            or row.provider_id not in (None, row.host_provider_id)
        ):
            errors.add("current_project_provider_mismatch")
    return errors


def _domain_errors(report, manifest, roots):
    errors = set()
    enrolled = {r.key: r for r in roots}
    for root in report.project_roots:
        saved = enrolled.get(root_key(root))
        if saved is None or saved.payload != root_identity(root):
            errors.add("unenrolled_project_root")
    clusters = {p.cluster_key for p in manifest.pins.provider_clusters}
    pools = set(manifest.pins.pool_keys)
    if not set(report.cluster_keys) <= clusters or not set(report.pool_keys) <= pools:
        errors.add("collection_domain_mismatch")
    identities = [
        b.image_key
        for b in report.volume_bindings
        + report.retained_bindings
        + report.cache_bindings
    ]
    identities += [i.key for i in report.images]
    identities += [i.parent.image_key for i in report.images if i.parent]
    identities += [s.image_key for i in report.images for s in i.snapshots]
    identities += [k for i in report.images for k in i.children]
    if any(
        PoolKey(ceph_fsid=k.ceph_fsid, pool_id=k.pool_id) not in pools
        for k in identities
    ):
        errors.add("unenrolled_backend_pool")
    for evidence in report.snapshot_evidence:
        saved = enrolled.get(root_key(evidence.root))
        if saved is None or saved.payload != root_identity(evidence.root):
            errors.add("unenrolled_snapshot_root")
    return errors


def _binding_discovery_complete(project_id, blockers, *, live=False):
    """Snapshot uncertainty alone cannot disprove a verified live PVC identity."""
    snapshot_codes = {
        "snapshot_identity_unresolved",
        "csi_snapshot_backend_unresolved",
    }
    applicable = [b for b in blockers if b.project_id in (None, project_id)]
    return all(
        live and b.project_id == project_id and b.code in snapshot_codes
        for b in applicable
    )


def _binding_errors(report, manifest, roots, retained):
    errors = set()
    enrolled = {r.key: r.payload for r in roots}
    saved = {b.key: b.payload for b in retained}
    discovery_blockers = report_inventory(report).blockers
    present = {r.project_id for r in report.project_roots}
    live_keys = {binding_key(b) for b in report.volume_bindings}
    cache_roots = {
        (c.cluster_key, c.namespace, c.namespace_uid) for c in manifest.pins.cache_roots
    }
    for binding in report.volume_bindings + report.retained_bindings:
        if enrolled.get(root_key(binding.root)) != root_identity(binding.root):
            errors.add("unenrolled_binding_root")
        if binding_key(binding) not in saved and (
            binding.root.project_id not in present
            or not _binding_discovery_complete(
                binding.root.project_id,
                discovery_blockers,
                live=binding_key(binding) in live_keys,
            )
        ):
            errors.add("new_binding_requires_complete_discovery")
    for binding in report.cache_bindings:
        if (
            binding.cluster_key,
            binding.namespace,
            binding.namespace_uid,
        ) not in cache_roots:
            errors.add("unenrolled_cache_namespace")
    identities = dict(saved)
    for binding in (
        report.volume_bindings + report.retained_bindings + report.cache_bindings
    ):
        identity = _binding_identity(binding)
        previous = identities.setdefault(binding_key(binding), identity)
        if previous != identity:
            errors.add("retained_binding_identity_changed")
    return errors


def _receipt_cache_identities(db, references):
    from app.services.native_cache_birth import validated_receipt

    identities = set()
    for reference in references:
        intent, receipt = validated_receipt(
            db, reference.operation_id, reference.digest
        )
        identities.add(
            digest(
                {
                    "cluster_key": intent.target.cluster_key,
                    "namespace": intent.namespace,
                    "namespace_uid": intent.namespace_uid,
                    "pvc_uid": receipt.pvc_uid,
                    "pv_uid": receipt.pv_uid,
                    "csi_driver": receipt.csi_driver,
                    "volume_handle": receipt.volume_handle,
                    "image_key": receipt.image_key.model_dump(mode="json"),
                }
            )
        )
    return identities


def _persist_bindings(db, report, domain_id, clone_lineages=(), cache_receipts=()):
    private = {p.intermediate for p in clone_lineages}
    caches = [b for b in report.cache_bindings if b.image_key not in private]
    if cache_receipts:
        # Namespace discovery may see CSI temporary PVCs before clone proof.
        # Only exact sealed source identities can become durable platform cache.
        authorized = _receipt_cache_identities(db, cache_receipts)
        caches = [b for b in caches if digest(_binding_identity(b)) in authorized]
    for binding in report.volume_bindings + report.retained_bindings + caches:
        key = binding_key(binding)
        if db.get(NativeRetainedBinding, key) is not None:
            continue
        root = root_key(binding.root) if hasattr(binding, "root") else "cache"
        db.add(
            NativeRetainedBinding(
                key=key,
                root_key=root,
                domain_id=domain_id,
                payload=_binding_identity(binding),
            )
        )
        db.flush()


def _advance_attempt(db, domain, snapshot):
    if domain.latest_started_at is None or utc(snapshot.started_at) > utc(
        domain.latest_started_at
    ):
        domain.latest_report_id = snapshot.id
        domain.latest_started_at = snapshot.started_at
        domain.conflicted = False
    elif utc(snapshot.started_at) == utc(domain.latest_started_at):
        previous = db.get(NativeStorageSnapshot, domain.latest_report_id)
        # IDs/finish time and untrusted aggregates do not define observation order.
        if _attempt_digest(previous.payload) != _attempt_digest(snapshot.payload):
            domain.conflicted = True


def _attempt_digest(payload):
    metadata = {
        "report_id",
        "collector_id",
        "completed_at",
        "projects",
        "platform_pools",
        "unattributed_images",
    }
    return digest({key: value for key, value in payload.items() if key not in metadata})


def import_report(db, report, *, enrollment_id, expected_enrollment_digest, now):
    """Validate, persist evidence and update attempt pointer in caller transaction."""
    # Python mode preserves bool/float values for strict byte revalidation; JSON
    # serialization can coerce an invalid bool in a model_copy into integer 1.
    report = NativeStorageReport.model_validate(
        report.model_dump(mode="python", warnings=False)
    )
    if report.completed_at > utc(now):
        raise ValueError("report is newer than import clock")
    enrollment = db.get(NativeEnrollment, enrollment_id)
    if enrollment is None or enrollment.digest != expected_enrollment_digest:
        raise ValueError("trusted enrollment ID and digest required")
    manifest = VerifiedEnrollment.model_validate(enrollment.payload)
    if report.collector_id not in manifest.pins.collector_ids:
        raise ValueError("collector is not enrolled")
    if inventory_digest(report_inventory(report)) != report.inventory_digest:
        reason = (
            "unsupported legacy inventory evidence"
            if report.inventory_blockers is None
            else "inventory digest mismatch"
        )
        raise ValueError(reason)
    content_digest, identity = digest(report), str(report.report_id)
    previous = db.get(NativeStorageSnapshot, identity)
    if previous is not None:
        if previous.digest != content_digest or previous.enrollment_id != enrollment_id:
            raise ValueError("changed same-ID report replay")
        return identity
    domain = (
        db.query(NativeCollectionDomain)
        .filter_by(id=enrollment.domain_id)
        .with_for_update()
        .one()
    )
    if domain.enrollment_id != enrollment_id:
        raise ValueError("obsolete enrollment version")
    from app.services.quota_cleanup import active_roots, settled_root_keys

    retired = settled_root_keys(db)
    roots = active_roots(
        db, db.query(NativeProjectRoot).filter_by(domain_id=domain.id).all()
    )
    retained = [
        b
        for b in db.query(NativeRetainedBinding).filter_by(domain_id=domain.id)
        if b.root_key not in retired
    ]
    inventory = normalized_inventory(report, roots, retained)
    from app.services.native_clone_lineage import resolve_lineages

    try:
        clone_lineages = resolve_lineages(db, report.clone_references, manifest)
        clone_errors = set()
    except ValueError:
        clone_lineages, clone_errors = [], {"managed_clone_authority_unverified"}
    # Validate the recomputed graph's exact quantities before accepting evidence.
    attribute_graph(inventory, report.images, clone_lineages=clone_lineages)
    errors = _domain_errors(report, manifest, roots) | _live_identity_errors(
        db, roots, manifest
    )
    errors |= _binding_errors(report, manifest, roots, retained)
    errors |= _retiring_errors(db, report, domain)
    errors |= clone_errors
    snapshot = NativeStorageSnapshot(
        id=identity,
        domain_id=domain.id,
        enrollment_id=enrollment_id,
        digest=content_digest,
        started_at=report.started_at,
        completed_at=report.completed_at,
        imported_at=utc(now),
        accepted=not errors,
        reason_codes=sorted(errors),
        payload=report.model_dump(mode="json"),
    )
    db.add(snapshot)
    if not errors:
        _persist_bindings(
            db, report, domain.id, clone_lineages, manifest.pins.cache_birth_receipts
        )
    db.flush()
    _advance_attempt(db, domain, snapshot)
    db.flush()
    return identity


def _retiring_errors(db, report, domain):
    from app.services.quota_cleanup import native_intents

    intents = {
        v.intent_id: v
        for v in native_intents(db)
        if str(v.domain_id) == domain.id and v.phase in {"teardown", "pending"}
    }
    errors = set()
    for proof in report.retiring_roots:
        intent = intents.get(proof.intent_id)
        if intent is None or (
            proof.operation_id != intent.operation_id
            or proof.root != intent.root
            or proof.inventory_digest != intent.inventory_digest
            or set(proof.owned_images) != set(intent.owned_images)
            or proof.observed_at < intent.teardown_at
            or proof.observed_at < report.started_at
            or proof.observed_at > report.completed_at
        ):
            errors.add("retiring_root_authorization_changed")
    reported = {p.intent_id for p in report.retiring_roots}
    roots = {root_key(r) for r in report.project_roots}
    if any(
        root_key(v.root) in roots and v.intent_id not in reported
        for v in intents.values()
    ):
        errors.add("retiring_root_evidence_missing")
    return errors
