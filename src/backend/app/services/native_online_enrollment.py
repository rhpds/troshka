"""Read-only online adoption with explicit custody, never invented deploy history.

Provider I/O is restricted to verify(). Database callers own their transactions.
Enrollment leaves quota accounting and ordinary power operations untouched.
"""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from app.models.native_custody import NativeCustodyReceipt, NativeCustodyRequest
from app.models.project import Project
from app.models.quota import QuotaAllocation, QuotaReservation
from app.schemas.kubevirt_storage import VolumeBinding
from app.schemas.native_online_enrollment import (
    CustodyAttestation,
    PreparedCustody,
    VerifiedCustody,
)
from app.schemas.quota import AccountingUnavailable, Resources
from app.services import kubevirt_storage_enrollment as enrollment
from app.services import quota_accounting as accounting
from app.services import quota_operations as operations


def _require(condition, message):
    if not condition:
        raise AccountingUnavailable(message)


def _disabled():
    _require(not accounting.quotas_enabled(), "Online custody requires disabled quotas")


def prepare(db, project_ids):
    _disabled()
    request = enrollment.prepare_native_enrollment(db, project_ids)
    topologies = {}
    for identity in request.projects:
        project = db.get(Project, str(identity.project_id))
        _legacy(db, project)
        topologies[project.id] = operations.retained_topology(project)
    prepared = PreparedCustody(request=request, topologies=topologies)
    db.add(
        NativeCustodyRequest(
            id=str(request.request_id),
            digest=enrollment.digest(prepared),
            payload=prepared.model_dump(mode="json"),
        )
    )
    db.flush()
    return prepared.model_dump(mode="json")


def _legacy(db, project):
    _require(project.state in {"active", "stopped"}, "Legacy project must be stable")
    _require(
        project.deployed_topology is not None, "Retained deployed topology required"
    )
    _require(
        not operations._rows(db, project.id),
        "Existing ledger requires ordinary reconciliation",
    )
    _require(
        not db.query(QuotaAllocation).filter_by(project_id=project.id).first(),
        "Existing allocation requires ordinary reconciliation",
    )


def canonical_topology(topology):
    """Mirror only the documented native deploy finalization projection.

    Runtime resolved source URLs/cloud-init and observed VM domain UUID are
    deliberately absent from the persisted deployed topology. All other fields
    remain exact, and the receipt separately retains the full live topology.
    """
    value = deepcopy(topology)
    for node in value.get("nodes", []):
        data = node.get("data", {})
        for field in ("resolvedS3Path", "presignedUrl", "ciGeneratedUserData"):
            data.pop(field, None)
        if node.get("type") == "vmNode":
            data.pop("domainUuid", None)
            data["bmcIp"] = data.get("bmcIp") or ""
    return value


def _topology_matches(saved, live, review=None):
    if review is not None:
        return (
            enrollment.digest(saved) == review.saved_digest
            and enrollment.digest(live) == review.live_digest
        )
    return canonical_topology(saved) == canonical_topology(live)


def _root(kube, root, topology, review=None):
    enrollment._verify_project(kube, root)
    namespace = kube.namespace(root.namespace)
    projects = kube.projects(root.namespace)
    _require(len(projects) == 1, "Online root inventory changed")
    value = projects[0]
    _require(
        namespace["metadata"]["uid"] == root.namespace_uid
        and not namespace["metadata"].get("deletionTimestamp")
        and value["metadata"].get("uid") == root.project_cr_uid
        and value["metadata"].get("generation") == root.deployment_generation
        and not value["metadata"].get("deletionTimestamp")
        and value["spec"].get("projectId") == str(root.project_id)
        and _topology_matches(topology, value["spec"].get("topology", {}), review),
        "Online namespace/root/topology identity changed",
    )
    return value["spec"]["topology"]


def _bindings(kube, root, pools):
    from app.services.kubevirt_storage_inventory import _pvc_binding

    pvs = kube.pvs()
    by_name = {v["metadata"]["name"]: v for v in pvs}
    _require(len(by_name) == len(pvs), "Duplicate persistent volume identity")
    result = []
    for pvc in kube.pvcs(root.namespace):
        _require(
            not pvc["metadata"].get("deletionTimestamp"),
            "Deleting PVC cannot be enrolled",
        )
        binding, _ = _pvc_binding(kube, pvc, by_name, root.namespace)
        value = VolumeBinding(root=root, **binding)
        _require(
            (value.image_key.ceph_fsid, value.image_key.pool_id) in pools,
            "Storage binding lies outside pinned pools",
        )
        result.append(value)
    _require(bool(result), "Complete nonempty storage bindings required")
    return sorted(result, key=lambda value: value.model_dump_json())


class ReadOnlyManaged:
    """Expose only bounded GET/list operations on the independently selected API."""

    def __init__(self, kube):
        from app.services.native_managed_power import helpers

        self._reader = helpers().PowerClient(getattr(kube, "custody_api", kube.api))
        self._reader.begin_inventory()

    def rows(self, kind, namespace):
        return self._reader.rows(kind, namespace)

    def get(self, kind, namespace, name):
        return self._reader.get(kind, namespace, name)


def validate_namespace(api, context):
    """Reject unsupported or foreign namespace occupants before managed stop."""
    from app.services import native_managed_power as power

    module = power.helpers()
    root = power._live_root(api, context, "custody-preflight")
    import json

    from helpers.native_custody import identity, stable

    from app.services.quota_cleanup import _namespace_rows

    full = _namespace_rows(api, context["root"]["namespace"])
    pins = sorted(
        [identity(v) for entries in full.values() for v in entries if stable(v)],
        key=lambda v: (v["kind"], v["name"]),
    )
    if "cleanup_custody" in context:
        expected = context["cleanup_custody"]
        marker = module.intent(root) or {}
        draining = (
            marker.get("mode") == "stopped"
            and marker.get("cleanupCustody") == expected
            and marker.get("allocationGeneration") == context.get("generation")
        )
        missing = [p for p in expected if p not in pins]
        module.require(
            all(p in expected for p in pins)
            and (
                not missing or (draining and all(p["kind"] == "Pod" for p in missing))
            ),
            "custody_support_changed",
        )
        pins = expected
    root = deepcopy(root)
    root["metadata"].setdefault("annotations", {})[module.ANNOTATION] = json.dumps(
        {
            "version": 1,
            "mode": "stopped",
            "requestId": "custody-preflight",
            "allocationGeneration": "custody-preflight",
            "cleanupCustody": pins,
        }
    )
    rows, by_uid = module.inventory(api, root)
    module.profile(root, rows, by_uid, api)
    values = [root] + [value for entries in full.values() for value in entries]
    module.require(len(values) <= 1000, "custody_inventory_limit")
    by_uid = {value["metadata"]["uid"]: value for value in values}
    module.require(len(by_uid) == len(values), "custody_duplicate_object_identity")
    for value in values:
        module.require(
            value["kind"] != "VolumeSnapshot", "custody_snapshot_unsupported"
        )
        if value is not root and module.cleanup_bootstrap(value, by_uid) is None:
            module.verify_chain(value, root, by_uid)
    final = power._live_root(api, context, "custody-preflight")
    module.require(final["spec"] == root["spec"], "custody_root_spec_changed")
    return pins


def preflight_cleanup(provider, rows, intent):
    if not any(row.dispatch_metadata.get("cleanup_only") for row in rows):
        return
    from app.services import native_managed_power as power

    context = {
        "root": intent.root.model_dump(mode="json"),
        "generation": intent.generation,
        "cleanup_custody": next(
            row.dispatch_metadata["cleanup_custody"]
            for row in rows
            if row.dispatch_metadata.get("cleanup_only")
        ),
    }
    power.with_client(provider, validate_namespace, context)


def verify(prepared, kube_readers, backend_identity_reader, *, pins, attestation):
    prepared = PreparedCustody.model_validate(prepared)
    attestation = CustodyAttestation.model_validate(attestation)
    manifest = enrollment.verify_native_enrollment(
        prepared.request,
        kube_readers,
        backend_identity_reader,
        pins=pins,
    )
    providers = {
        str(p.project_id): str(p.provider_id) for p in prepared.request.projects
    }
    _require(
        set(prepared.topologies) == set(providers), "Prepared topology coverage changed"
    )
    _require(
        set(attestation.topology_reviews) <= set(providers),
        "Topology review names an unenrolled project",
    )
    pools = {(p.ceph_fsid, p.pool_id) for p in pins.pool_keys}
    bindings, runtime_topologies, object_identities = [], {}, {}
    for root in pins.project_roots:
        kube = kube_readers[providers[str(root.project_id)]]
        topology = prepared.topologies[str(root.project_id)]
        review = attestation.topology_reviews.get(str(root.project_id))
        runtime = _root(kube, root, topology, review)
        runtime_topologies[str(root.project_id)] = runtime
        object_identities[str(root.project_id)] = validate_namespace(
            ReadOnlyManaged(kube), {"root": root.model_dump(mode="json")}
        )
        first = _bindings(kube, root, pools)
        _require(
            _root(kube, root, topology, review) == runtime,
            "Runtime topology changed during verification",
        )
        _require(
            first == _bindings(kube, root, pools), "Online storage identity changed"
        )
        _require(
            _root(kube, root, topology, review) == runtime,
            "Runtime topology changed during verification",
        )
        _require(
            kube.cluster_identity() == root.cluster_key,
            "Online cluster identity changed",
        )
        bindings.extend(first)
    from app.services.native_ancestry_custody import observe

    _require(len(kube_readers) == 1, "Custody ancestry requires one provider domain")
    backend = backend_identity_reader
    if isinstance(backend, dict):
        _require(len(backend) == 1, "Custody ancestry requires one backend")
        backend = next(iter(backend.values()))
    ancestry = observe(
        bindings, next(iter(kube_readers.values())), backend, pins, manifest.verified_at
    )
    for root in pins.project_roots:
        kube = kube_readers[providers[str(root.project_id)]]
        _require(
            _root(
                kube,
                root,
                prepared.topologies[str(root.project_id)],
                attestation.topology_reviews.get(str(root.project_id)),
            )
            == runtime_topologies[str(root.project_id)]
            and _bindings(kube, root, pools) == [b for b in bindings if b.root == root],
            "Custody root or storage binding changed during ancestry verification",
        )
        validate_namespace(
            ReadOnlyManaged(kube),
            {
                "root": root.model_dump(mode="json"),
                "cleanup_custody": object_identities[str(root.project_id)],
            },
        )
    # Revalidate independently selected identities at completion. A bounded
    # read-only ancestry scan can exceed the short verify-to-apply freshness gate.
    manifest = enrollment.verify_native_enrollment(
        prepared.request,
        kube_readers,
        backend_identity_reader,
        pins=pins,
    )
    ancestry = [
        p.model_copy(update={"verified_at": manifest.verified_at}) for p in ancestry
    ]
    receipt = VerifiedCustody(
        prepared=prepared,
        enrollment=manifest,
        attestation=attestation,
        runtime_topologies=runtime_topologies,
        object_identities=object_identities,
        bindings=bindings,
        ancestry=ancestry,
        digest="0" * 64,
    )
    _validate_coverage(receipt)
    return receipt.model_copy(
        update={
            "digest": enrollment.digest(
                receipt.model_dump(mode="json", exclude={"digest"})
            )
        }
    ).model_dump(mode="json")


def _validated(value):
    receipt = VerifiedCustody.model_validate(value)
    _require(
        receipt.digest
        == enrollment.digest(receipt.model_dump(mode="json", exclude={"digest"})),
        "Online custody receipt digest mismatch",
    )
    _require(
        receipt.prepared.request == receipt.enrollment.request,
        "Online enrollment request mismatch",
    )
    _validate_coverage(receipt)
    return receipt


def _validate_coverage(receipt):
    _require(
        all(p.binding in receipt.bindings for p in receipt.ancestry)
        and len({p.binding.image_key for p in receipt.ancestry})
        == len(receipt.ancestry),
        "Custody ancestry binding coverage changed",
    )
    projects = {str(p.project_id) for p in receipt.prepared.request.projects}
    roots = {str(r.project_id): r for r in receipt.enrollment.pins.project_roots}
    _require(
        projects
        == set(roots)
        == set(receipt.prepared.topologies)
        == set(receipt.runtime_topologies)
        == set(receipt.object_identities),
        "Custody topology coverage changed",
    )
    for project_id in projects:
        _require(
            _topology_matches(
                receipt.prepared.topologies[project_id],
                receipt.runtime_topologies[project_id],
                receipt.attestation.topology_reviews.get(project_id),
            ),
            "Custody runtime topology mismatch",
        )
    _require(
        set(receipt.attestation.topology_reviews) <= projects,
        "Topology review names an unenrolled project",
    )
    _require(
        {str(b.root.project_id) for b in receipt.bindings} == projects,
        "Custody storage coverage changed",
    )
    _require(
        all(roots.get(str(b.root.project_id)) == b.root for b in receipt.bindings),
        "Custody binding root changed",
    )
    for values in (
        [b.pvc_uid for b in receipt.bindings],
        [b.pv_uid for b in receipt.bindings],
        [b.image_key.model_dump_json() for b in receipt.bindings],
    ):
        _require(
            len(values) == len(set(values)), "Custody storage identity is ambiguous"
        )


def apply(db, verified, *, expected_target_id):
    _disabled()
    receipt = _validated(verified)
    prepared = db.get(NativeCustodyRequest, str(receipt.prepared.request.request_id))
    _require(
        prepared is not None and prepared.digest == enrollment.digest(receipt.prepared),
        "Online custody was not independently prepared",
    )
    _require(
        0 <= (datetime.now(UTC) - receipt.enrollment.verified_at).total_seconds() <= 60,
        "Online verification must be fresh; verify again",
    )
    enrollment._validate_manifest(db, receipt.enrollment, expected_target_id)
    for identity in receipt.prepared.request.projects:
        project = db.get(Project, str(identity.project_id))
        _legacy(db, project)
        _require(
            operations.retained_topology(project)
            == receipt.prepared.topologies[project.id],
            "Retained topology changed; prepare again",
        )
    enrollment_id = _storage_enrollment(db, receipt.enrollment, expected_target_id)
    result = []
    for identity in receipt.prepared.request.projects:
        result.append(_save(db, receipt, str(identity.project_id), enrollment_id))
    db.flush()
    return result


def _storage_enrollment(db, manifest, target_id):
    from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment

    domain = db.get(NativeCollectionDomain, str(manifest.pins.domain_id))
    if domain is not None:
        previous = enrollment.validated_enrollment(
            db.get(NativeEnrollment, domain.enrollment_id)
        )
        if previous.pins == manifest.pins:
            before = {
                str(p.project_id): p.model_dump(exclude={"updated_at"})
                for p in previous.request.projects
            }
            after = {
                str(p.project_id): p.model_dump(exclude={"updated_at"})
                for p in manifest.request.projects
            }
            _require(
                before == after, "Existing enrollment identity differs from custody"
            )
            return domain.enrollment_id
    return enrollment.enroll_native_roots(db, manifest, expected_target_id=target_id)


def _save(db, receipt, project_id, enrollment_id):
    previous = (
        db.query(NativeCustodyReceipt).filter_by(project_id=project_id).one_or_none()
    )
    if previous is not None:
        _require(
            previous.digest == receipt.digest,
            "Existing custody differs; explicit reconciliation required",
        )
        return previous.id
    row = NativeCustodyReceipt(
        id=str(uuid4()),
        project_id=project_id,
        digest=receipt.digest,
        payload={
            "receipt": receipt.model_dump(mode="json"),
            "enrollment_id": enrollment_id,
        },
    )
    db.add(row)
    return row.id


def retained(db, project):
    row = db.query(NativeCustodyReceipt).filter_by(project_id=project.id).one_or_none()
    if row is None:
        return None
    receipt = _validated(row.payload["receipt"])
    _require(receipt.digest == row.digest, "Stored custody digest changed")
    identities = [
        p for p in receipt.prepared.request.projects if str(p.project_id) == project.id
    ]
    roots = [
        r
        for r in receipt.enrollment.pins.project_roots
        if str(r.project_id) == project.id
    ]
    _require(len(identities) == len(roots) == 1, "Custody project identity ambiguous")
    current = enrollment._database_identity(db, project.id)
    _require(
        current.model_dump(exclude={"updated_at"})
        == identities[0].model_dump(exclude={"updated_at"}),
        "Custody owner/provider identity changed",
    )
    _require(
        operations.retained_topology(project)
        == receipt.prepared.topologies[project.id],
        "Custody retained topology changed",
    )
    return row, receipt, roots[0]


def cleanup_anchor(db, project, rows):
    """Create a cleanup-only identity anchor, never observed zero compute usage."""
    if rows:
        return rows
    custody = retained(db, project)
    if custody is None:
        return rows
    _disabled()
    receipt_row, receipt, root = custody
    _require(
        project.state in {"active", "stopped"}, "Cleanup requires stable legacy project"
    )
    identity = {
        "domain_id": str(receipt.enrollment.pins.domain_id),
        "enrollment_id": receipt_row.payload["enrollment_id"],
        "host_id": project.host_id,
        "provider_id": str(
            next(
                p.provider_id
                for p in receipt.prepared.request.projects
                if str(p.project_id) == project.id
            )
        ),
        "root": root.model_dump(mode="json"),
    }
    from app.services.quota_native import _measurement_binding, _measurement_domain

    enrolled, domain = _measurement_domain(db, project.id, identity)
    _measurement_binding(db, project, enrolled, domain, identity)
    attribution = accounting.attribution(db, project)
    operation_id = f"native-custody:{receipt_row.id}"
    vector = Resources(projects=1).model_dump()
    row = QuotaReservation(
        operation_id=operation_id,
        project_id=project.id,
        owner_id=attribution["owner_id"],
        organization_id=attribution["organization_id"],
        requested=vector,
        observed=vector,
        state="confirmed",
        dispatch_metadata={
            "kind": "custody",
            "adapter": "kubevirt",
            "generation_id": operation_id,
            "native_storage_identity": identity,
            "native_custody_id": receipt_row.id,
            "cleanup_only": True,
            "cleanup_custody": receipt.object_identities[project.id],
            "compute_accounting": "unavailable",
            "worker_finished": True,
            "worker_finished_at": operations.now().isoformat(),
            "custody_boundary": "No worker dispatched; explicit owner cleanup only",
            "desired_digest": operations.digest(operations.retained_topology(project)),
        },
    )
    db.add(row)
    db.flush()
    return [row]


def namespace_authority(db, project, row):
    custody = retained(db, project)
    if custody is None:
        raise AccountingUnavailable("Native custody receipt unavailable")
    receipt_row, _, root = custody
    _require(
        row.dispatch_metadata.get("native_custody_id") == receipt_row.id
        and row.dispatch_metadata.get("native_storage_identity", {}).get("root")
        == root.model_dump(mode="json"),
        "Cleanup custody anchor changed",
    )
    return "managed"


def validate_cleanup_bindings(db, project, rows, bindings):
    if not any(row.dispatch_metadata.get("native_custody_id") for row in rows):
        return
    custody = retained(db, project)
    if custody is None:
        raise AccountingUnavailable("Native custody receipt unavailable")
    _, receipt, root = custody
    expected = [b for b in receipt.bindings if b.root == root]
    from app.services.kubevirt_storage_reports import _binding_identity

    _require(
        sorted(enrollment.digest(_binding_identity(b)) for b in expected)
        == sorted(enrollment.digest(_binding_identity(b)) for b in bindings),
        "Cleanup storage identities differ from explicit custody",
    )
