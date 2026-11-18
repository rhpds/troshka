"""Origin storage custody with independent configuration and exact predecessors.

Remote verification runs between caller-owned short transactions. These trusted
service interfaces accept independently selected readers, never tenant reports.
Task transport must authenticate requests before invoking prepare/verify/apply.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from app.core.config import config
from app.models.kubevirt_storage import (
    NativeCollectionDomain,
    NativeEnrollment,
    NativeReportingTarget,
)
from app.models.native_deployment_origin import (
    NativeDeploymentOriginActive,
    NativeDeploymentOriginIntent,
)
from app.models.native_origin_custody import (
    NativeOriginCustodyCurrent,
    NativeOriginCustodyReceipt,
    NativeOriginCustodyRequest,
)
from app.models.project import Project
from app.schemas.kubevirt_storage import ProjectRoot, VolumeBinding
from app.schemas.kubevirt_storage_enrollment import DomainExtension, VerificationPins
from app.schemas.native_deployment_origin import OriginIntent
from app.schemas.native_origin_custody import (
    OriginCustodyReceipt,
    OriginCustodyRequest,
    PendingOriginClaim,
    VerifiedOriginCustody,
)
from app.schemas.native_snapshot_custody import SnapshotFacts
from app.services import kubevirt_storage_enrollment as enrollment
from app.services import native_deployment_origin as origin
from app.services import native_online_enrollment as online
from app.services.kubevirt_storage_enrollment import digest

CurrentCustody = tuple[NativeOriginCustodyReceipt, OriginCustodyReceipt]
RetainedCustody = tuple[NativeOriginCustodyReceipt, OriginCustodyReceipt, ProjectRoot]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def admission(domain_id):
    """Only an exact explicit domain opt-in permits creating new authority."""
    profiles = config.get("native_collection.profiles", [])
    matches = [p for p in profiles if str(p.get("domain_id")) == str(domain_id)]
    require(
        len(matches) == 1 and matches[0].get("origin_admission") is True,
        "Origin domain admission is disabled or ambiguous",
    )
    return digest({"domain_id": str(domain_id), "origin_admission": True})


def runtime_profile(domain_id):
    """Resolve operator-owned paths and contents; persist only their digest."""
    from app.services.native_collection_config import _managed_inputs
    from app.services.native_collection_runtime import profile_for

    args = profile_for(domain_id)
    pins, inventory, backend = _managed_inputs(args)
    require(str(pins.domain_id) == str(domain_id), "Origin runtime domain changed")
    scope = _scope(pins)
    seal = digest(
        {
            "target": args.target_id,
            "scope": scope,
            "inventory": inventory.model_dump(mode="json"),
            "backend": backend.model_dump(mode="json"),
        }
    )
    return args.target_id, pins, seal


def _scope(pins):
    fields = (
        "domain_id",
        "collector_ids",
        "provider_clusters",
        "pool_keys",
        "cache_roots",
        "cache_birth_receipts",
        "initialization_evidence",
    )
    body = pins.model_dump(mode="json")
    return {k: body.get(k) for k in fields}


def _domain(db, domain_id):
    domain = (
        db.query(NativeCollectionDomain)
        .filter_by(id=str(domain_id))
        .populate_existing()
        .one_or_none()
    )
    require(domain is not None, "Origin requires an independently enrolled domain")
    row = db.get(NativeEnrollment, domain.enrollment_id)
    require(row is not None, "Origin domain enrollment missing")
    return domain, enrollment.validated_enrollment(row)


def _anchored_domain(db, pins):
    """Authenticate current authority through exact protected historical pins."""
    from app.services.native_managed_collector import anchored

    _domain(db, pins.domain_id)  # Refresh a possibly cached domain pointer first.
    try:
        domain, manifest = anchored(db, pins)
    except ValueError as exc:
        raise ValueError(f"Origin protected domain anchor invalid: {exc}") from exc
    enrollment._validate_cache_receipts(db, manifest)
    return domain, manifest


def _configured_domain(db, intent, domain_id):
    if domain_id is None:
        domain_id = domain_for(db, intent)
    admission_digest = admission(domain_id)
    target, pins, profile_digest = runtime_profile(domain_id)
    domain, previous = _anchored_domain(db, pins)
    require(
        str(previous.request.target_id) == target,
        "Origin configured target/pool/cache scope differs from enrolled domain",
    )
    aliases = [
        p.cluster_key
        for p in pins.provider_clusters
        if str(p.provider_id) == str(intent.provider_id)
    ]
    require(
        aliases == [intent.cluster_uid],
        "Origin provider target is not independently enrolled",
    )
    return domain, previous, profile_digest, admission_digest


def domain_for(db, intent):
    """Select only explicitly admitted, already enrolled provider/cluster domains."""
    matches = []
    for profile in config.get("native_collection.profiles", []):
        if profile.get("origin_admission") is not True:
            continue
        domain_id = profile.get("domain_id")
        domain = db.get(NativeCollectionDomain, str(domain_id))
        if domain is None:
            continue
        previous = enrollment.validated_enrollment(
            db.get(NativeEnrollment, domain.enrollment_id)
        )
        aliases = [
            p.cluster_key
            for p in previous.pins.provider_clusters
            if p.provider_id == intent.provider_id
        ]
        if aliases == [intent.cluster_uid]:
            matches.append(domain.id)
    require(len(matches) == 1, "Origin requires one matching explicit domain admission")
    return matches[0]


def _root(intent, receipt):
    return ProjectRoot(
        project_id=intent.project_id,
        cluster_key=intent.cluster_uid,
        namespace=intent.namespace,
        namespace_uid=receipt.namespace_uid,
        project_cr_uid=receipt.root_uid,
        deployment_generation=receipt.generation,
    )


def _active_identity(db, intent, *, publishing=False, mode=None):
    current = enrollment._database_identity(db, intent.project_id)
    for field in (
        "project_id",
        "owner_id",
        "organization_id",
        "host_id",
        "provider_id",
    ):
        require(
            getattr(current, field) == getattr(intent, field),
            "Origin owner/provider identity changed",
        )
    require(
        origin.active(db, str(intent.project_id)) == intent,
        "Origin active attempt changed",
    )
    pointer = db.get(NativeDeploymentOriginActive, str(intent.project_id))
    if publishing:
        project = db.get(Project, str(intent.project_id))
        require(
            not pointer.cancelled
            and pointer.io_phase is None
            and pointer.io_token is None,
            "Origin cancellation or outstanding I/O holds publication",
        )
        allowed = {"deploying", "active", "stopped", "error"}
        if mode in {"removal", "retirement"}:
            allowed.add("reconfiguring")
        require(
            project.state in allowed,
            "Origin cleanup or operation state holds publication",
        )


def _current(db, intent_id):
    pointer = (
        db.query(NativeOriginCustodyCurrent)
        .filter_by(intent_id=str(intent_id))
        .populate_existing()
        .one_or_none()
    )
    if pointer is None:
        return None
    row = db.get(NativeOriginCustodyReceipt, pointer.receipt_id)
    require(row is not None, "Origin checkpoint missing")
    return row, _receipt(row)


def prepare(
    db,
    intent_id,
    *,
    domain_id=None,
    mode: Literal[
        "enroll", "removal", "complete", "retirement", "producer", "snapshot-reconcile"
    ] = "complete",
    pvc_uids=(),
    producer=None,
    cleanup_read_state=None,
):
    row = db.get(NativeDeploymentOriginIntent, str(intent_id))
    require(row is not None, "Origin intent missing")
    intent = origin._validated(row, OriginIntent)
    if cleanup_read_state is None:
        _active_identity(db, intent, publishing=True, mode=mode)
    else:
        from app.services.native_cleanup_storage import validate_state

        validate_state(db, intent, mode, cleanup_read_state)
    _, root_receipt = origin.receipts(db, str(intent_id))
    require(root_receipt is not None, "Origin root receipt required")
    domain, previous, profile_digest, admission_digest = _configured_domain(
        db, intent, domain_id
    )
    root = _root(intent, root_receipt)
    pins = _extended_pins(
        previous, domain.enrollment_id, root, retired=_settled_roots(db)
    )
    request = enrollment.prepare_native_enrollment(
        db, [str(r.project_id) for r in pins.project_roots]
    )
    current = _current(db, intent.id)
    require(len(set(pvc_uids)) == len(pvc_uids), "Origin removal selection duplicates")
    require(
        (mode in {"removal", "retirement", "producer"}) == bool(pvc_uids),
        "Origin removal requires an exact nonempty claim selection",
    )
    from app.services.native_snapshot_custody import prepare_facts

    prepared = OriginCustodyRequest(
        **prepare_facts(current[1] if current else None, producer, mode),
        request_id=uuid4(),
        intent=intent,
        root_receipt=root_receipt,
        root=root,
        enrollment=request,
        pins=pins,
        previous_enrollment_id=domain.enrollment_id,
        previous_enrollment_digest=previous.digest,
        predecessor_id=current[0].id if current else None,
        predecessor_digest=current[0].digest if current else None,
        profile_digest=profile_digest,
        admission_digest=admission_digest,
        mode=mode,
        cleanup_read_state=cleanup_read_state,
        pvc_uids=sorted(pvc_uids),
    )
    db.add(
        NativeOriginCustodyRequest(
            id=str(prepared.request_id),
            digest=digest(prepared),
            payload=prepared.model_dump(mode="json"),
        )
    )
    db.flush()
    return prepared


def _settled_roots(db):
    from app.services.quota_cleanup import settled_root_keys

    return settled_root_keys(db)


def _extended_pins(previous, enrollment_id, root, *, retired=()):
    roots = [
        r
        for r in previous.pins.project_roots
        if r == root or enrollment.root_key(r) not in retired
    ]
    matches = [r for r in roots if r.project_id == root.project_id]
    require(
        not matches or matches == [root],
        "Origin root generation transition requires explicit settled review",
    )
    if matches and roots == previous.pins.project_roots:
        return previous.pins
    body = previous.pins.model_dump(mode="json")
    body.pop("empty_domain_bootstrap", None)
    body["project_roots"] = [r.model_dump(mode="json") for r in roots]
    if not matches:
        body["project_roots"].append(root.model_dump(mode="json"))
    body["domain_extension"] = DomainExtension(
        version="native-domain-extension-v1",
        previous_enrollment_id=enrollment_id,
        previous_enrollment_digest=previous.digest,
    ).model_dump(mode="json")
    return VerificationPins.model_validate(body)


def _live_root(kube, prepared):
    root, intent = prepared.root, prepared.intent
    require(kube.cluster_identity() == root.cluster_key, "Origin cluster changed")
    enrollment._verify_project(kube, root)
    namespace = kube.namespace(root.namespace)
    rows = kube.projects(root.namespace)
    require(len(rows) == 1, "Origin root inventory changed")
    value = rows[0]
    meta = value["metadata"]
    require(
        meta.get("name") == intent.root_name
        and meta.get("uid") == root.project_cr_uid
        and meta.get("generation") == root.deployment_generation
        and not meta.get("deletionTimestamp")
        and not namespace["metadata"].get("deletionTimestamp"),
        "Origin root identity changed",
    )
    require(
        digest(value["spec"]) == intent.cr_spec_digest,
        "Origin root specification changed",
    )
    require(
        all(
            meta.get("annotations", {}).get(k) == v
            for k, v in origin.annotations(intent).items()
        ),
        "Origin root provenance changed",
    )


def _storage(kube, prepared):
    from app.services.kubevirt_storage_inventory import _pvc_binding

    pvs = kube.pvs()
    by_name = {v["metadata"]["name"]: v for v in pvs}
    require(len(by_name) == len(pvs), "Origin duplicate PV identity")
    pvcs = kube.pvcs(prepared.root.namespace)
    require(
        len({v["metadata"]["uid"] for v in pvcs}) == len(pvcs),
        "Origin duplicate claim identity",
    )
    selected = set(prepared.pvc_uids)
    if prepared.mode in {"removal", "producer"}:
        pvcs = [v for v in pvcs if v["metadata"]["uid"] in selected]
        require(
            {v["metadata"]["uid"] for v in pvcs} == selected,
            "Origin selected claim missing",
        )
    bindings, pending, names = [], [], {}
    pools = {(p.ceph_fsid, p.pool_id) for p in prepared.pins.pool_keys}
    for pvc in pvcs:
        meta = pvc["metadata"]
        require(not meta.get("deletionTimestamp"), "Origin claim already deleting")
        names[meta["uid"]] = meta["name"]
        volume_name = _claim_volume(pvc, pvs, prepared.root.namespace)
        if volume_name is None:
            pending.append(_pending(pvc))
            continue
        value, _ = _pvc_binding(
            kube,
            pvc,
            by_name,
            prepared.root.namespace,
            require_bound=False,
            bound_volume_name=volume_name,
        )
        binding = VolumeBinding(root=prepared.root, **value)
        require(
            (binding.image_key.ceph_fsid, binding.image_key.pool_id) in pools,
            "Origin binding outside enrolled pools",
        )
        bindings.append(binding)
    return sorted(bindings, key=digest), sorted(pending, key=digest), names


def _claim_volume(pvc, pvs, namespace):
    """A real PV can establish a lifecycle before PVC status catches up."""
    selected = pvc.get("spec", {}).get("volumeName")
    matches = [
        p
        for p in pvs
        if p.get("spec", {}).get("claimRef", {}).get("uid") == pvc["metadata"]["uid"]
    ]
    require(len(matches) <= 1, "Origin claim has ambiguous PV identities")
    if not matches:
        return selected
    pv = matches[0]
    claim = pv["spec"]["claimRef"]
    require(
        claim.get("namespace") == namespace
        and claim.get("name") == pvc["metadata"]["name"]
        and selected in (None, pv["metadata"]["name"]),
        "Origin claim PV reference changed",
    )
    return pv["metadata"]["name"]


def _pending(pvc):
    refs = [
        r["uid"]
        for r in pvc["metadata"].get("ownerReferences", [])
        if r.get("kind") == "DataVolume"
    ]
    require(len(refs) <= 1, "Origin ambiguous pending claim owner")
    return PendingOriginClaim(
        pvc_uid=pvc["metadata"]["uid"],
        pvc_name=pvc["metadata"]["name"],
        spec_digest=digest(pvc.get("spec", {})),
        data_volume_uid=refs[0] if refs else None,
    )


def verify(request, kube_readers, backend):
    """Provider reads only. Scoped retention never implies namespace eligibility."""
    from app.services import native_snapshot_custody as snapshots
    from app.services.native_ancestry_custody import observe

    prepared = OriginCustodyRequest.model_validate(request)
    require(
        prepared.mode not in {"retirement", "snapshot-reconcile"},
        "Origin retirement requires independent absence verification",
    )
    manifest = enrollment.verify_native_enrollment(
        prepared.enrollment, kube_readers, backend, pins=prepared.pins
    )
    kube = kube_readers[str(prepared.intent.provider_id)]
    _live_root(kube, prepared)
    bindings, pending, names, objects, ancestry = [], [], {}, [], []
    facts = snapshots.prepare_facts(prepared, None, "observation")
    if prepared.mode != "enroll":
        bindings, pending, names = _storage(kube, prepared)
        facts = snapshots.observe(prepared, kube, backend, bindings)
        objects = online.verify_namespace_ownership(
            kube, prepared.root, producers=facts.producers
        )
        _claim_coverage(prepared, objects, names)
        if bindings:
            restored = {p.binding.pvc_uid for p in facts.snapshot_restores}
            ordinary = [b for b in bindings if b.pvc_uid not in restored]
            if ordinary:
                ancestry = observe(
                    ordinary,
                    kube,
                    backend,
                    prepared.pins,
                    manifest.verified_at,
                    snapshot_edges=snapshots.source_edges(facts),
                )
        require(
            (bindings, pending, names) == _storage(kube, prepared),
            "Origin storage/journal changed during verification",
        )
        require(
            objects
            == online.verify_namespace_ownership(
                kube, prepared.root, producers=facts.producers
            ),
            "Origin namespace changed during verification",
        )
        rechecked = snapshots.observe(prepared, kube, backend, bindings)
        require(
            snapshots.stable_facts(facts) == snapshots.stable_facts(rechecked),
            "Origin snapshot facts changed during verification",
        )
        facts = rechecked
    if prepared.mode == "complete":
        require(not pending, "Origin unresolved claims prevent complete custody")
        require(
            not snapshots.outstanding(facts),
            "Origin snapshot debt prevents complete custody",
        )
        objects = online.verify_namespace(kube, prepared.root)
        online.verify_namespace(kube, prepared.root, expected=objects)
        require(
            (bindings, pending, names) == _storage(kube, prepared),
            "Origin storage changed during namespace verification",
        )
    _live_root(kube, prepared)
    enrollment.verify_native_enrollment(
        prepared.enrollment, kube_readers, backend, pins=prepared.pins
    )
    value = VerifiedOriginCustody(
        **(
            cast(SnapshotFacts, facts).model_dump()
            if hasattr(facts, "model_dump")
            else cast(dict[str, Any], facts)
        ),
        prepared=prepared,
        enrollment=manifest,
        bindings=bindings,
        ancestry=ancestry,
        pending_claims=pending,
        pvc_names=names,
        object_identities=objects,
        verified_at=datetime.now(UTC),
        digest="",
    )
    return value.model_copy(
        update={"digest": digest(value.model_dump(mode="json", exclude={"digest"}))}
    )


def _claim_coverage(prepared, objects, names):
    observed = {
        p["uid"]: p["name"] for p in objects if p["kind"] == "PersistentVolumeClaim"
    }
    require(
        all(observed.get(uid) == name for uid, name in names.items()),
        "Origin namespace storage identity changed",
    )
    if prepared.mode == "complete":
        require(observed == names, "Origin namespace claim coverage incomplete")


def verify_configured(request):
    """Production read-only entry point; no Session or caller-provided credentials."""
    return _verify_configured(request)


def verify_retirement_configured(prepared):
    """Load protected request in a short session, then independently read absence."""
    from app.core.database import SessionLocal
    from app.services import native_binding_retirement as retirement

    with SessionLocal() as db:
        value = retirement.validate_prepared(db, prepared)
        require(value.source_kind == "origin", "Origin retirement source required")
        row = db.get(NativeOriginCustodyRequest, str(value.origin_request_id))
        require(
            row is not None and row.digest == digest(row.payload),
            "Origin retirement request changed",
        )
        if TYPE_CHECKING:
            assert row is not None and row.digest == digest(row.payload)
        request = OriginCustodyRequest.model_validate(row.payload)
    return _verify_configured(request, retiring=value)


def _verify_configured(request, *, retiring=None):
    import time
    from contextlib import ExitStack

    from app.core.database import SessionLocal
    from app.schemas.kubevirt_storage import CollectionLimits
    from app.services.kubevirt_storage_inventory import (
        KubernetesReader,
        read_static_kubeconfig,
    )
    from app.services.native_collection_config import (
        ToolboxBackendConfig,
        _backend_reader,
        _managed_inputs,
    )
    from app.services.native_collection_runtime import profile_for

    prepared = OriginCustodyRequest.model_validate(request)
    args = profile_for(prepared.pins.domain_id)
    _, inventory, backend_config = _managed_inputs(args)
    with SessionLocal() as db:
        domain, _ = _domain(db, prepared.pins.domain_id)
        _publication_request(db, prepared, domain)
    pins = prepared.pins
    require(
        len(pins.provider_clusters) == 1,
        "Origin runtime requires one protected provider",
    )
    limits = CollectionLimits(
        command_timeout_seconds=args.command_timeout_seconds,
        scan_timeout_seconds=args.verification_timeout_seconds,
    )
    deadline = time.monotonic() + limits.scan_timeout_seconds
    with ExitStack() as cleanup:
        data = (
            read_static_kubeconfig(backend_config.kubeconfig)
            if isinstance(backend_config, ToolboxBackendConfig)
            else None
        )
        backend = _backend_reader(backend_config, limits, deadline, cleanup, data)
        backend.capability = inventory.capability
        kube = KubernetesReader(
            args.kubeconfig,
            backend,
            collector_id=inventory.collector_id,
            cache_roots=[r.model_dump(mode="json") for r in pins.cache_roots],
            scan_deadline=deadline,
            csi_requests=inventory.csi_requests,
            max_objects=limits.max_objects,
        )
        cleanup.callback(kube.api.close)
        if prepared.mode == "snapshot-reconcile":
            from app.services.native_snapshot_reconciliation import (
                verify as verify_snapshots,
            )

            return verify_snapshots(prepared, kube, backend)
        if retiring is not None:
            from app.services import native_binding_retirement as retirement

            require(
                retiring.origin_request_id == prepared.request_id,
                "Origin retirement request identity changed",
            )
            _live_root(kube, prepared)
            value = retirement.verify(retiring, kube, backend)
            _live_root(kube, prepared)
            return value
        return verify(prepared, {str(prepared.intent.provider_id): kube}, backend)


def _verified(value):
    value = VerifiedOriginCustody.model_validate(value)
    require(
        value.digest == digest(value.model_dump(mode="json", exclude={"digest"})),
        "Origin verification digest changed",
    )
    require(
        value.enrollment.request == value.prepared.enrollment
        and value.enrollment.pins == value.prepared.pins,
        "Origin verification enrollment changed",
    )
    require(
        all(b.root == value.prepared.root for b in value.bindings),
        "Origin binding root changed",
    )
    require(
        all(p.binding in value.bindings for p in value.ancestry),
        "Origin ancestry binding missing",
    )
    require(
        len({b.pvc_uid for b in value.bindings}) == len(value.bindings),
        "Origin duplicate binding",
    )
    return value


def _receipt(row):
    value = OriginCustodyReceipt.model_validate(row.payload)
    verified = _verified(value.verified)
    require(
        row.digest == digest(value)
        and str(verified.prepared.intent.id) == row.intent_id
        and str(verified.prepared.intent.project_id) == row.project_id
        and str(verified.prepared.pins.domain_id) == row.domain_id,
        "Origin protected receipt changed",
    )
    require(
        str(value.publication.request_id) == row.request_id
        and value.publication.intent == verified.prepared.intent
        and value.publication.root == verified.prepared.root,
        "Origin publication identity changed",
    )
    return value


def _publication_locks(db, prepared):
    from app.services.quota_locks import lock_rows

    target = (
        db.query(NativeReportingTarget)
        .filter_by(id=1)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    require(
        target is not None and target.target_id == str(prepared.enrollment.target_id),
        "Origin reporting target changed",
    )
    domain = (
        db.query(NativeCollectionDomain)
        .filter_by(id=str(prepared.pins.domain_id))
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    require(domain is not None, "Origin domain missing")
    identities = prepared.enrollment.projects
    lock_rows(
        db,
        user_ids=[str(p.owner_id) for p in identities],
        organization_ids=[
            str(p.organization_id) for p in identities if p.organization_id
        ],
        project_ids=[str(p.project_id) for p in identities],
    )
    if prepared.cleanup_read_state is not None:
        from app.services.native_cleanup_storage import validate_state

        validate_state(db, prepared.intent, prepared.mode, prepared.cleanup_read_state)
    elif prepared.mode == "snapshot-reconcile":
        from app.services.native_snapshot_reconciliation import validate_state

        validate_state(db, prepared)
    else:
        _active_identity(db, prepared.intent, publishing=True, mode=prepared.mode)
    require(
        all(enrollment._database_identity(db, p.project_id) == p for p in identities),
        "Origin prepared project identity changed",
    )
    return domain


def _publication_request(db, prepared, domain):
    row = db.get(NativeOriginCustodyRequest, str(prepared.request_id))
    require(
        row is not None and row.digest == digest(row.payload) == digest(prepared),
        "Origin request not independently prepared",
    )
    require(
        domain.enrollment_id == str(prepared.previous_enrollment_id),
        "Origin enrollment predecessor changed",
    )
    previous = enrollment.validated_enrollment(
        db.get(NativeEnrollment, domain.enrollment_id)
    )
    require(
        previous.digest == prepared.previous_enrollment_digest,
        "Origin enrollment predecessor digest changed",
    )
    require(
        origin.receipts(db, str(prepared.intent.id))[1] == prepared.root_receipt,
        "Origin immutable root receipt changed",
    )
    if prepared.mode != "snapshot-reconcile":
        require(
            admission(domain.id) == prepared.admission_digest,
            "Origin admission changed",
        )
    target, pins, profile_digest = runtime_profile(domain.id)
    anchored_domain, anchored_manifest = _anchored_domain(db, pins)
    expected_pins = (
        previous.pins
        if prepared.mode == "snapshot-reconcile"
        else _extended_pins(
            previous, domain.enrollment_id, prepared.root, retired=_settled_roots(db)
        )
    )
    require(
        profile_digest == prepared.profile_digest
        and target == str(prepared.enrollment.target_id)
        and anchored_domain.enrollment_id == str(prepared.previous_enrollment_id)
        and anchored_manifest == previous
        and expected_pins == prepared.pins,
        "Origin runtime profile changed",
    )
    current = _current(db, prepared.intent.id)
    require(
        (current[0].id if current else None)
        == (str(prepared.predecessor_id) if prepared.predecessor_id else None)
        and (current[0].digest if current else None) == prepared.predecessor_digest,
        "Origin checkpoint predecessor changed",
    )
    return previous, current


def _merge(before, after, key, *, immutable=True):
    result = {key(v): v for v in before}
    for value in after:
        identity = key(value)
        require(
            not immutable or identity not in result or result[identity] == value,
            "Origin retained liability changed",
        )
        result[identity] = value
    return list(result.values())


def _combined(db, value, previous, enrollment_id):
    from app.services.kubevirt_storage_reports import _binding_identity

    old = previous[1] if previous else None
    bindings = _merge(old.bindings if old else [], value.bindings, lambda b: b.pvc_uid)
    ancestry = _merge_ancestry(old.ancestry if old else [], value.ancestry)
    pending = _merge(
        old.pending_claims if old else [], value.pending_claims, lambda p: p.pvc_uid
    )
    names = dict(old.pvc_names if old else {})
    require(
        all(k not in names or names[k] == v for k, v in value.pvc_names.items()),
        "Origin retained PVC name changed",
    )
    names.update(value.pvc_names)
    if value.prepared.mode == "complete":
        retired = _retired_uids(db, old) if old else set()
        require(
            {digest(_binding_identity(b)) for b in bindings if b.pvc_uid not in retired}
            == {digest(_binding_identity(b)) for b in value.bindings},
            "Origin complete custody missing old binding; explicit retirement required",
        )
        require(
            not {p.pvc_uid for p in pending} - {b.pvc_uid for b in bindings},
            "Origin unresolved claim debt prevents cleanup",
        )
    return OriginCustodyReceipt(
        producers=value.producers,
        snapshots=value.snapshots,
        snapshot_restores=value.snapshot_restores,
        snapshot_retirements=value.snapshot_retirements,
        verified=value,
        publication=value.prepared,
        complete=value.prepared.mode == "complete",
        enrollment_id=enrollment_id,
        bindings=bindings,
        ancestry=ancestry,
        pending_claims=pending,
        pvc_names=names,
        binding_retirements=old.binding_retirements if old else [],
    )


def _merge_ancestry(before, after):
    result = _merge(before, after, lambda p: p.binding.pvc_uid, immutable=False)
    for proof in before:
        newer = next(p for p in result if p.binding.pvc_uid == proof.binding.pvc_uid)
        require(
            proof.model_dump(exclude={"verified_at"})
            == newer.model_dump(exclude={"verified_at"}),
            "Origin retained ancestry changed",
        )
    return result


def _complete_ledger(db, receipt, domain_id):
    from app.models.kubevirt_storage import NativeRetainedBinding
    from app.services.kubevirt_storage_reports import binding_key

    if not receipt.complete:
        return
    root = enrollment.root_key(receipt.publication.root)
    keys = {binding_key(b) for b in receipt.bindings}
    stored = (
        db.query(NativeRetainedBinding)
        .filter_by(domain_id=domain_id, root_key=root)
        .all()
    )
    require(
        all(b.key in keys for b in stored),
        "Origin complete custody omits retained ledger liability",
    )


def apply(db, verified):
    """Atomically enroll and retain before acknowledgement. Caller commits."""
    from app.services.kubevirt_storage_reports import retain_verified_bindings
    from app.services.native_custody_reconciliation import (
        _invalidate_retirement_observations,
        _retirement_observation_state,
    )

    value = _verified(verified)
    prepared = value.prepared
    domain = _publication_locks(db, prepared)
    existing = (
        db.query(NativeOriginCustodyReceipt)
        .filter_by(request_id=str(prepared.request_id))
        .one_or_none()
    )
    if existing:
        require(_receipt(existing).verified == value, "Origin idempotency conflict")
        require(
            cast(CurrentCustody, _current(db, prepared.intent.id))[0].id == existing.id,
            "Origin checkpoint already superseded",
        )
        return existing
    previous, current = _publication_request(db, prepared, domain)
    require(
        0
        <= (datetime.now(UTC) - enrollment.utc(value.verified_at)).total_seconds()
        <= 60,
        "Origin verification must be fresh",
    )
    prior_observations = _retirement_observation_state(db, domain.id)
    enrollment_id = domain.enrollment_id
    if previous.pins != prepared.pins:
        enrollment.validate_domain_extension(
            previous, value.enrollment, retired=_settled_roots(db)
        )
        enrollment_id = enrollment.enroll_native_roots(
            db, value.enrollment, expected_target_id=str(prepared.enrollment.target_id)
        )
    receipt = _combined(db, value, current, enrollment_id)
    _complete_ledger(db, receipt, domain.id)
    retain_verified_bindings(db, receipt.bindings, domain.id)
    row = NativeOriginCustodyReceipt(
        id=str(uuid4()),
        intent_id=str(prepared.intent.id),
        project_id=str(prepared.intent.project_id),
        domain_id=domain.id,
        request_id=str(prepared.request_id),
        digest=digest(receipt),
        payload=receipt.model_dump(mode="json"),
    )
    db.add(row)
    pointer = db.get(NativeOriginCustodyCurrent, str(prepared.intent.id))
    if pointer is None:
        db.add(
            NativeOriginCustodyCurrent(
                intent_id=str(prepared.intent.id), receipt_id=row.id
            )
        )
    else:
        pointer.receipt_id = row.id
    _invalidate_retirement_observations(db, domain.id, prior_observations)
    db.flush()
    return row


def retained(db, project, *, require_complete=False):
    """Resolve protected authority on every read, even with admission disabled."""
    intent = origin.active(db, project.id)
    if intent is None:
        return None
    current = _current(db, intent.id)
    if current is None:
        return None
    _active_identity(db, intent)
    row, receipt = current
    prepared = receipt.publication
    require(
        prepared.intent == intent
        and origin.receipts(db, str(intent.id))[1] == prepared.root_receipt,
        "Origin protected creation identity changed",
    )
    domain, manifest = _anchored_domain(db, prepared.pins)
    require(
        domain.id == row.domain_id and prepared.root in manifest.pins.project_roots,
        "Origin protected domain scope changed",
    )
    target, pins, profile_digest = runtime_profile(domain.id)
    _, configured_manifest = _anchored_domain(db, pins)
    require(
        target == str(manifest.request.target_id)
        and configured_manifest == manifest
        and profile_digest == prepared.profile_digest,
        "Origin protected runtime profile changed",
    )
    enrollment.retained_enrollment(
        db,
        domain,
        {
            "enrollment_id": str(receipt.enrollment_id),
            "root": prepared.root.model_dump(mode="json"),
        },
    )
    _retained_bindings(db, receipt, domain.id)
    if require_complete:
        require(
            receipt.complete
            and not unresolved_claims(receipt)
            and not unresolved_snapshots(receipt),
            "Origin cleanup requires complete custody without unresolved claims",
        )
    return row, receipt, prepared.root


def unresolved_claims(receipt):
    """Only an actual retained PV lifecycle can resolve a formerly pending claim."""
    bound = {b.pvc_uid for b in receipt.bindings}
    return [p for p in receipt.pending_claims if p.pvc_uid not in bound]


def unresolved_snapshots(receipt):
    from app.services.native_snapshot_custody import outstanding

    return outstanding(receipt)


def _retained_bindings(db, receipt, domain_id):
    from app.models.kubevirt_storage import NativeRetainedBinding
    from app.services.kubevirt_storage_reports import _binding_identity, binding_key

    for binding in receipt.bindings:
        row = db.get(NativeRetainedBinding, binding_key(binding))
        require(
            row is not None
            and row.domain_id == domain_id
            and row.payload == _binding_identity(binding),
            "Origin retained binding missing or changed",
        )


def resolve_ancestry(db, manifest, retired):
    result = []
    for pointer in db.query(NativeOriginCustodyCurrent):
        row = db.get(NativeOriginCustodyReceipt, pointer.receipt_id)
        if row.domain_id != str(manifest.pins.domain_id):
            continue
        receipt = _receipt(row)
        if enrollment.root_key(receipt.verified.prepared.root) in retired:
            continue
        project = db.get(Project, row.project_id)
        require(project is not None, "Origin custody project missing before settlement")
        if TYPE_CHECKING:
            assert project is not None
        _, receipt, _ = cast(RetainedCustody, retained(db, project))
        require(
            not unresolved_claims(receipt),
            "Origin unresolved claim debt prevents storage accounting",
        )
        require(
            receipt.verified.prepared.enrollment.target_id
            == manifest.request.target_id,
            "Origin target changed",
        )
        require(
            receipt.publication.root in manifest.pins.project_roots,
            "Origin source root outside current scan",
        )
        retired_uids = _retired_uids(db, receipt)
        for proof in receipt.ancestry:
            if proof.binding.pvc_uid in retired_uids:
                continue
            require(
                any(
                    r.cluster_key == proof.source.cluster_key
                    and r.namespace_uid == proof.source.namespace_uid
                    and r.namespace == proof.source.namespace
                    for r in manifest.pins.cache_roots
                ),
                "Origin cache ancestry authority changed",
            )
            result.append(proof)
    return result


def _historical(db, receipt_id, receipt_digest):
    """Bounded validated predecessor chain; a detached row never confers authority."""
    row = db.get(NativeOriginCustodyReceipt, str(receipt_id))
    require(
        row is not None and row.digest == receipt_digest,
        "Origin retirement source changed",
    )
    if TYPE_CHECKING:
        assert row is not None
    current = _current(db, row.intent_id)
    require(current is not None, "Origin custody current pointer missing")
    if TYPE_CHECKING:
        assert current is not None
    candidate, value = current
    for _ in range(128):
        if candidate.id == row.id:
            return row, _receipt(row)
        predecessor = value.publication.predecessor_id
        require(predecessor is not None, "Origin source outside checkpoint chain")
        candidate = db.get(NativeOriginCustodyReceipt, str(predecessor))
        require(
            candidate is not None
            and candidate.digest == value.publication.predecessor_digest
            and candidate.intent_id == row.intent_id,
            "Origin checkpoint predecessor changed",
        )
        if TYPE_CHECKING:
            assert candidate is not None
        value = _receipt(candidate)
    raise ValueError("Origin checkpoint ancestry limit exceeded")


def _selected_retirement_source(db, prepared):
    """A cumulative protected selection proves membership without a history walk.

    Publication validates each retirement before selecting it, and subsequent
    checkpoints preserve those selections. Match the entire prepared evidence
    so an unselected request cannot borrow another retirement's membership.
    """
    from app.models.native_binding_retirement import NativeBindingRetirementReceipt
    from app.services.native_binding_retirement import _validated

    selected = (
        db.query(NativeBindingRetirementReceipt)
        .filter_by(request_id=str(prepared.request_id))
        .one_or_none()
    )
    if selected is None:
        return None
    source = db.get(NativeOriginCustodyReceipt, str(prepared.source_custody_id))
    require(
        source is not None and source.digest == prepared.source_custody_digest,
        "Origin retirement source changed",
    )
    if TYPE_CHECKING:
        assert source is not None and source.digest == prepared.source_custody_digest
    current = _current(db, source.intent_id)
    require(current is not None, "Origin custody current pointer missing")
    if TYPE_CHECKING:
        assert current is not None
    references = [
        r for r in current[1].binding_retirements if str(r.receipt_id) == selected.id
    ]
    if not references:
        return None
    value = _validated(selected.payload)
    require(
        len(references) == 1
        and references[0].digest == selected.digest == value.digest
        and value.prepared == prepared
        and selected.project_id == source.project_id == current[0].project_id
        and prepared.root == current[1].publication.root,
        "Origin selected retirement source changed",
    )
    return source, _receipt(source)


def retirement_source(db, prepared):
    """Typed source resolver called again by collector and report importer."""
    source = _selected_retirement_source(db, prepared)
    row, receipt = source or _historical(
        db, prepared.source_custody_id, prepared.source_custody_digest
    )
    request_row = db.get(NativeOriginCustodyRequest, str(prepared.origin_request_id))
    require(
        request_row is not None and request_row.digest == digest(request_row.payload),
        "Origin retirement request missing",
    )
    if TYPE_CHECKING:
        assert request_row is not None
    request = OriginCustodyRequest.model_validate(request_row.payload)
    require(
        str(request.predecessor_id) == row.id
        and request.predecessor_digest == row.digest,
        "Origin retirement checkpoint source changed",
    )
    project = db.get(Project, row.project_id)
    require(project is not None, "Origin retirement project missing before settlement")
    # Avoid recursively resolving retirement references here; retained validates
    # protected creation/domain/owner identity independently of selected absences.
    retained(db, project)
    require(
        prepared.target_id == request.enrollment.target_id
        and prepared.domain_id == request.pins.domain_id
        and prepared.root == request.root
        and prepared.binding in receipt.bindings,
        "Origin retirement binding scope changed",
    )
    require(
        receipt.pvc_names.get(prepared.binding.pvc_uid) == prepared.pvc_name,
        "Origin retirement PVC name changed",
    )
    expected = [p for p in receipt.ancestry if p.binding == prepared.binding]
    require(
        expected == ([prepared.ancestry] if prepared.ancestry else []),
        "Origin retirement ancestry changed",
    )
    from app.services.native_binding_retirement import _shape

    _shape(prepared)
    return row


def prepare_retirement(db, intent_id, pvc_uid):
    """Prepare exact retained storage absence while deployment may still run."""
    from app.models.native_binding_retirement import NativeBindingRetirementRequest
    from app.schemas.native_binding_retirement import PreparedBindingRetirement

    current = _current(db, intent_id)
    require(current is not None, "Origin retirement requires retained custody")
    if TYPE_CHECKING:
        assert current is not None
    row, receipt = current
    request = prepare(
        db, intent_id, domain_id=row.domain_id, mode="retirement", pvc_uids=[pvc_uid]
    )
    bindings = [b for b in receipt.bindings if b.pvc_uid == pvc_uid]
    require(
        len(bindings) == 1 and pvc_uid not in _retired_uids(db, receipt),
        "Origin retirement requires an unretired exact binding",
    )
    binding = bindings[0]
    proofs = [p for p in receipt.ancestry if p.binding == binding]
    require(len(proofs) <= 1, "Origin retirement ancestry ambiguous")
    value = PreparedBindingRetirement(
        request_id=uuid4(),
        target_id=request.enrollment.target_id,
        domain_id=request.pins.domain_id,
        source_kind="origin",
        origin_request_id=request.request_id,
        source_custody_id=row.id,
        source_custody_digest=row.digest,
        root=request.root,
        binding=binding,
        pvc_name=receipt.pvc_names[pvc_uid],
        ancestry=proofs[0] if proofs else None,
        reason="Independently verified origin binding retirement",
        prepared_at=datetime.now(UTC),
    )
    retirement_source(db, value)
    db.add(
        NativeBindingRetirementRequest(
            id=str(value.request_id),
            digest=digest(value),
            payload=value.model_dump(mode="json"),
        )
    )
    db.flush()
    return value


def apply_retirement(db, verified):
    """Atomically publish an origin-selected absence and invalidate old scans."""
    from app.models.native_binding_retirement import NativeBindingRetirementReceipt
    from app.schemas.kubevirt_storage import BindingRetirementReference
    from app.services import native_binding_retirement as retirement
    from app.services.native_custody_reconciliation import (
        _invalidate_retirement_observations,
    )

    value = retirement._validated(verified)
    prepared = value.prepared
    require(prepared.source_kind == "origin", "Origin retirement source required")
    request_row = db.get(NativeOriginCustodyRequest, str(prepared.origin_request_id))
    require(request_row is not None, "Origin retirement publication request missing")
    request = OriginCustodyRequest.model_validate(request_row.payload)
    domain = _publication_locks(db, request)
    existing = (
        db.query(NativeBindingRetirementReceipt)
        .filter_by(request_id=str(prepared.request_id))
        .one_or_none()
    )
    if existing:
        require(
            existing.digest == value.digest
            and existing.payload == value.model_dump(mode="json"),
            "Origin retirement idempotency conflict",
        )
        return BindingRetirementReference(
            receipt_id=existing.id, digest=existing.digest
        )
    _, current = _publication_request(db, request, domain)
    current = cast(CurrentCustody, current)
    retirement._protected(db, prepared)
    require(value.observation.root == prepared.root, "Origin retirement root changed")
    require(
        0
        <= (
            datetime.now(UTC) - enrollment.utc(value.observation.verified_at)
        ).total_seconds()
        <= 60,
        "Origin retirement verification must be fresh",
    )
    row = NativeBindingRetirementReceipt(
        id=str(uuid4()),
        project_id=str(prepared.root.project_id),
        request_id=str(prepared.request_id),
        digest=value.digest,
        payload=value.model_dump(mode="json"),
    )
    db.add(row)
    reference = BindingRetirementReference.model_validate(
        dict(receipt_id=row.id, digest=row.digest)
    )
    receipt = current[1].model_copy(
        update={
            "publication": request,
            "complete": False,
            "binding_retirements": [*current[1].binding_retirements, reference],
        }
    )
    successor = NativeOriginCustodyReceipt(
        id=str(uuid4()),
        intent_id=str(request.intent.id),
        project_id=row.project_id,
        domain_id=domain.id,
        request_id=str(request.request_id),
        digest=digest(receipt),
        payload=receipt.model_dump(mode="json"),
    )
    db.add(successor)
    db.get(NativeOriginCustodyCurrent, str(request.intent.id)).receipt_id = successor.id
    _invalidate_retirement_observations(db, domain.id)
    db.flush()
    return reference


def _retired_uids(db, receipt):
    from app.services.native_binding_retirement import resolve

    return {
        resolve(db, reference).prepared.binding.pvc_uid
        for reference in receipt.binding_retirements
    }


def active_bindings(db, receipt):
    retired = _retired_uids(db, receipt)
    return [b for b in receipt.bindings if b.pvc_uid not in retired]


def retirement_references(db, domain_id):
    """Only selections reached through the validated current protected source."""
    from app.services.quota_cleanup import settled_root_keys

    settled = settled_root_keys(db)
    result = []
    for pointer in db.query(NativeOriginCustodyCurrent):
        row = db.get(NativeOriginCustodyReceipt, pointer.receipt_id)
        if row.domain_id != str(domain_id):
            continue
        receipt = _receipt(row)
        if enrollment.root_key(receipt.publication.root) in settled:
            continue
        project = db.get(Project, row.project_id)
        require(
            project is not None, "Origin retirement project missing before settlement"
        )
        if TYPE_CHECKING:
            assert project is not None
        _, receipt, _ = cast(RetainedCustody, retained(db, project))
        result.extend(r.model_dump(mode="json") for r in receipt.binding_retirements)
    return result
