"""Read-only online adoption with explicit custody, never invented deploy history.

Provider I/O is restricted to verify(). Database callers own their transactions.
Enrollment leaves quota accounting and ordinary power operations untouched.
"""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from app.models.native_custody import (
    NativeCustodyReceipt,
    NativeCustodyRequest,
    NativeCustodySuccessor,
)
from app.models.project import Project
from app.models.quota import QuotaAllocation, QuotaReservation
from app.schemas.kubevirt_storage import ProjectRoot, VolumeBinding
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


def prepare(
    db,
    project_ids,
    *,
    accounting_only_project_ids=(),
    accounting_only_missing_object_uids=None,
    binding_retirements=(),
):
    _disabled()
    request = enrollment.prepare_native_enrollment(db, project_ids)
    accounting_only = sorted(set(accounting_only_project_ids))
    _require(
        set(accounting_only) <= {str(p.project_id) for p in request.projects},
        "Accounting-only selection names an unprepared project",
    )
    topologies = {}
    missing = accounting_only_missing_object_uids or {}
    _require(
        set(missing) <= set(accounting_only),
        "Missing support requires accounting-only selection",
    )
    retained_support = {}
    for identity in request.projects:
        project = db.get(Project, str(identity.project_id))
        _legacy(db, project, accounting_only=project.id in accounting_only)
        topologies[project.id] = operations.retained_topology(project)
        if project.id in missing:
            retained_support[project.id] = _prepare_retained_support(
                db, project, missing[project.id]
            )
    prepared = PreparedCustody(
        request=request,
        topologies=topologies,
        accounting_only_project_ids=accounting_only,
        retained_accounting_support=retained_support,
        binding_retirements=list(binding_retirements),
    )
    db.add(
        NativeCustodyRequest(
            id=str(request.request_id),
            digest=enrollment.digest(prepared),
            payload=prepared.model_dump(mode="json"),
        )
    )
    db.flush()
    return prepared.model_dump(mode="json")


def _prepare_retained_support(db, project, missing):
    previous = retained(db, project)
    if previous is None:
        raise AccountingUnavailable("Missing support requires retained custody")
    row, receipt, _ = previous
    objects = receipt.object_identities[project.id]
    selected = set(missing)
    _require(
        bool(selected) and len(selected) == len(missing),
        "Missing support selection must be explicit and unique",
    )
    _require(
        selected <= {v["uid"] for v in objects if _missing_network_support(v)},
        "Only retained network support may be selected",
    )
    return {
        "receipt_id": row.id,
        "receipt_digest": row.digest,
        "objects": deepcopy(objects),
        "missing_object_uids": sorted(selected),
    }


def _missing_network_support(value):
    names = {
        "TroshkaNetwork": ("net-",),
        "NetworkAttachmentDefinition": ("net-",),
        "ConfigMap": ("dnsmasq-net-",),
        "Deployment": ("dnsmasq-net-", "gateway-troshka-"),
    }
    return value["name"].startswith(names.get(value["kind"], ()))


def _retained_support_subset(current, support):
    expected = support.objects
    missing = [v for v in expected if v not in current]
    _require(
        all(v in expected for v in current),
        "Retained accounting support changed or added",
    )
    _require(
        {v["uid"] for v in missing} == set(support.missing_object_uids)
        and all(_missing_network_support(v) for v in missing),
        "Retained accounting support missing selection changed",
    )


def _accounting_owner_chain(value, root, by_uid, expected, module):
    from helpers.native_custody import identity_matches, stable

    seen = set()
    while value["metadata"]["uid"] != root["metadata"]["uid"]:
        if stable(value) and any(identity_matches(value, pin) for pin in expected):
            return
        uid = value["metadata"]["uid"]
        module.require(uid not in seen, "managed_owner_cycle")
        seen.add(uid)
        refs = value["metadata"].get("ownerReferences", [])
        module.require(
            len(refs) == 1 and refs[0].get("controller") is True,
            "managed_ownership_unverified",
        )
        ref = refs[0]
        parent = by_uid.get(ref["uid"])
        module.require(parent is not None, "managed_owner_missing")
        module.require(
            all(ref.get(k) == parent.get(k) for k in ("kind", "apiVersion"))
            and ref["name"] == parent["metadata"]["name"],
            "managed_owner_changed",
        )
        value = parent


def _validate_retained_accounting(api, context, support):
    """Read-only subset verification; never called by cleanup or power paths."""
    from app.services import native_managed_power as power
    from app.services.quota_cleanup import _namespace_rows

    module = power.helpers()
    root = power._live_root(api, context, "custody-preflight")
    full = _namespace_rows(api, context["root"]["namespace"])
    values = [root] + [v for entries in full.values() for v in entries]
    module.require(len(values) <= 1000, "custody_inventory_limit")
    by_uid = {v["metadata"]["uid"]: v for v in values}
    module.require(len(by_uid) == len(values), "custody_duplicate_object_identity")
    pins = _cleanup_custody_pins(values[1:], support.objects)
    _retained_support_subset(pins, support)
    for value in values[1:]:
        module.require(
            value["kind"] != "VolumeSnapshot", "custody_snapshot_unsupported"
        )
        _accounting_owner_chain(value, root, by_uid, support.objects, module)
    root = _custody_root(root, pins, module)
    by_uid[root["metadata"]["uid"]] = root
    rows = {kind: full.get(kind, []) for kind in module.INVENTORY}
    _accounting_profile(root, rows, by_uid, api, module, support)
    final = power._live_root(api, context, "custody-preflight")
    module.require(final["spec"] == root["spec"], "custody_root_spec_changed")
    return pins


def _verify_namespace(kube, root, prepared, expected=None):
    support = prepared.retained_accounting_support.get(str(root.project_id))
    context = {"root": root.model_dump(mode="json")}
    if support is not None:
        result = _validate_retained_accounting(ReadOnlyManaged(kube), context, support)
        _require(
            expected is None or result == expected,
            "Accounting namespace changed during verification",
        )
        return result
    return verify_namespace(
        kube,
        root,
        expected=expected,
        accounting_only=str(root.project_id) in prepared.accounting_only_project_ids,
    )


def verify_namespace(kube, root, *, expected=None, accounting_only=False):
    """Read-only full custody verification without administrator identity input."""
    context = {"root": root.model_dump(mode="json")}
    if expected is not None:
        context["cleanup_custody"] = expected
    return validate_namespace(
        ReadOnlyManaged(kube), context, accounting_only=accounting_only
    )


def verify_namespace_ownership(kube, root, *, producers=()):
    """Scoped retention checks ownership, without claiming settled runtime custody."""
    from app.services.native_managed_power import helpers
    from app.services.quota_cleanup import _namespace_rows

    module = helpers()
    api = ReadOnlyManaged(kube)
    roots = kube.projects(root.namespace)
    _require(len(roots) == 1, "Origin root inventory changed")
    full = _namespace_rows(api, root.namespace)
    children = [v for entries in full.values() for v in entries]
    pins = _cleanup_custody_pins(children, [])
    live = _custody_root(roots[0], pins, module)
    values = [live, *children]
    _require(len(values) <= 1000, "Origin inventory limit")
    by_uid = {v["metadata"]["uid"]: v for v in values}
    _require(len(by_uid) == len(values), "Origin duplicate object identity")
    for value in children:
        module.require(
            value["kind"] != "VolumeSnapshot"
            or any(
                p.kind == "VolumeSnapshot"
                and p.name == value["metadata"]["name"]
                and p.uid == value["metadata"]["uid"]
                for p in producers
            ),
            "custody_snapshot_unsupported",
        )
        if module.cleanup_bootstrap(value, by_uid) is None:
            module.verify_chain(value, live, by_uid)
    return pins


def _custody_root(root, pins, module):
    import json

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
    return root


def _accounting_runtime_guard(root, rows, module):
    status = root.get("status", {})
    module.require(
        status.get("phase") in {"Running", "Error"}
        or (
            status.get("phase") == "CaptureComplete"
            and status.get("captureProgress") == "Done"
        ),
        "managed_project_not_settled",
    )
    module.require(
        not any(status.get(k) for k in ("recertConfig", "capture", "filePull"))
        and not any(
            "capture" in k or "file-pull" in k
            for k in root["metadata"].get("annotations", {})
        ),
        "managed_active_work_unsupported",
    )
    topology = root["spec"].get("topology", {})
    module.require(not topology.get("projectCephCapture"), "managed_ceph_unsupported")
    module.validate_profile_nodes(topology, True)
    for kind in ("StatefulSet", "DaemonSet", "CronJob"):
        module.require(not rows[kind], "managed_controller_unsupported")
    for vm in rows["TroshkaVM"]:
        module.require(
            vm.get("status", {}).get("observedGeneration")
            == vm["metadata"]["generation"],
            "managed_child_not_settled",
        )
        module.require(not vm["spec"].get("recertEnabled"))


def _retained_helper(value, support):
    if support is None:
        return False
    refs = value["metadata"].get("ownerReferences", [])
    return (not refs and value["metadata"]["name"].startswith("bmc-")) or (
        len(refs) == 1 and refs[0]["uid"] in support.missing_object_uids
    )


def _accounting_profile(root, rows, by_uid, api, module, support=None):
    """Keep runtime/storage guards, allowing absent VMs in a failed deployment."""
    from helpers.native_custody import verify_ceph

    _accounting_runtime_guard(root, rows, module)
    verify_ceph(root, rows, by_uid)
    module.verify_storage(rows, by_uid, api, custody=True)
    vm_owners = {module.parent_kind(v, by_uid) for v in rows["VirtualMachine"]}
    module.require(vm_owners <= {"TroshkaVM"})
    parent_uids = {
        v["metadata"]["ownerReferences"][0]["uid"] for v in rows["VirtualMachine"]
    }
    present = dict(
        rows,
        TroshkaVM=[v for v in rows["TroshkaVM"] if v["metadata"]["uid"] in parent_uids],
    )
    module.verify_virtual_machines(present, by_uid)
    for value in rows["Deployment"]:
        module.require(
            not value["metadata"].get("deletionTimestamp"), "managed_helper_deleting"
        )
        replicas = value["spec"].get("replicas")
        module.require(type(replicas) is int and replicas >= 0)
    # Exact retained helpers may outlive selected network parents. Keep them in
    # storage/mount checks and runtime ownership maps; only reconstruction skips.
    helpers = dict(
        rows,
        Deployment=[v for v in rows["Deployment"] if not _retained_helper(v, support)],
    )
    module.verify_helpers(helpers, by_uid)
    module.verify_pods(root, rows, by_uid)


def _legacy(db, project, *, accounting_only=False):
    _require(
        project.state in {"active", "stopped"}
        or (accounting_only and project.state == "error"),
        "Legacy project must be stable",
    )
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

    def __init__(self, kube, *, scan_deadline=None):
        from app.services.native_managed_power import helpers

        self._reader = helpers().PowerClient(getattr(kube, "custody_api", kube.api))
        self._reader.begin_inventory()
        if scan_deadline is not None:
            # Snapshot verification shares one deadline across API and Ceph reads.
            # PowerClient still caps every individual request at five seconds.
            self._reader.deadline = scan_deadline

    def rows(self, kind, namespace):
        return self._reader.rows(kind, namespace)

    def get(self, kind, namespace, name):
        return self._reader.get(kind, namespace, name)


def _cleanup_custody_pins(values, expected):
    """Preserve receipt pins only for exact identities or verified restart drift."""
    from helpers.native_custody import custody_identity, identity_matches, stable

    by_name = {(pin["kind"], pin["name"]): pin for pin in expected}
    pins = []
    for value in values:
        if not stable(value):
            continue
        pin = by_name.get((value["kind"], value["metadata"]["name"]))
        pins.append(
            pin if pin and identity_matches(value, pin) else custody_identity(value)
        )
    return sorted(pins, key=lambda pin: (pin["kind"], pin["name"]))


def validate_namespace(api, context, *, accounting_only=False):
    """Reject unsupported or foreign namespace occupants before managed stop."""
    from app.services import native_managed_power as power

    module = power.helpers()
    root = power._live_root(api, context, "custody-preflight")
    import json

    from app.services.quota_cleanup import _namespace_rows

    full = _namespace_rows(api, context["root"]["namespace"])
    pins = _cleanup_custody_pins(
        [v for entries in full.values() for v in entries],
        context.get("cleanup_custody", []),
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
    if accounting_only:
        _accounting_profile(root, rows, by_uid, api, module)
    else:
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


def _storage_descriptors(kube, root):
    """Seal selected Kubernetes storage without reusing CSI journal evidence."""
    pvcs = kube.pvcs(root.namespace)
    pvs = kube.pvs()
    by_name = {v["metadata"]["name"]: v for v in pvs}
    _require(len(by_name) == len(pvs), "Duplicate persistent volume identity")
    names = [p.get("spec", {}).get("volumeName") for p in pvcs]
    _require(all(name in by_name for name in names), "Custody selected PV missing")
    rows = {"pvcs": pvcs, "pvs": [by_name[name] for name in names]}
    descriptors = {}
    for kind, values in rows.items():
        sealed = []
        for value in values:
            metadata = deepcopy(value.get("metadata", {}))
            metadata.pop("resourceVersion", None)
            metadata.pop("managedFields", None)
            sealed.append(
                {
                    "metadata": metadata,
                    "spec": deepcopy(value.get("spec", {})),
                    "phase": value.get("status", {}).get("phase"),
                }
            )
        descriptors[kind] = sorted(sealed, key=enrollment.digest)
    return descriptors


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
        object_identities[str(root.project_id)] = _verify_namespace(
            kube, root, prepared
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
    descriptors = {}
    if prepared.binding_retirements:
        descriptors = {
            str(root.project_id): _storage_descriptors(
                kube_readers[providers[str(root.project_id)]], root
            )
            for root in pins.project_roots
        }
    retirement_observations = _verify_binding_retirements(
        prepared, kube_readers, backend
    )
    for root in pins.project_roots:
        kube = kube_readers[providers[str(root.project_id)]]
        if prepared.binding_retirements:
            _require(
                _storage_descriptors(kube, root) == descriptors[str(root.project_id)],
                "Custody storage descriptors changed during retirement verification",
            )
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
        _verify_namespace(kube, root, prepared, object_identities[str(root.project_id)])
        if prepared.binding_retirements:
            _require(
                _storage_descriptors(kube, root) == descriptors[str(root.project_id)],
                "Custody storage descriptors changed during CSI verification",
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
    receipt = VerifiedCustody.model_validate(
        dict(
            prepared=prepared,
            enrollment=manifest,
            attestation=attestation,
            runtime_topologies=runtime_topologies,
            object_identities=object_identities,
            bindings=bindings,
            ancestry=ancestry,
            binding_retirements=retirement_observations,
            digest="0" * 64,
        )
    )
    _validate_coverage(receipt)
    return receipt.model_copy(
        update={
            "digest": enrollment.digest(
                receipt.model_dump(mode="json", exclude={"digest"})
            )
        }
    ).model_dump(mode="json")


def _verify_binding_retirements(prepared, kube_readers, backend):
    if not prepared.binding_retirements:
        return []
    from app.schemas.native_binding_retirement import PreparedBindingRetirement
    from app.services.native_binding_retirement import verify_many

    providers = {
        str(p.project_id): str(p.provider_id) for p in prepared.request.projects
    }
    requests, selected_readers = [], {}
    for item in prepared.binding_retirements:
        retirement = PreparedBindingRetirement.model_validate(item["prepared"])
        _require(
            retirement.target_id == prepared.request.target_id,
            "Binding retirement target changed",
        )
        provider_id = providers.get(str(retirement.binding.root.project_id))
        _require(provider_id in kube_readers, "Binding retirement project changed")
        requests.append(retirement)
        selected_readers[provider_id] = kube_readers[provider_id]
    _require(
        len(selected_readers) == 1,
        "Retirement verification requires one provider domain",
    )
    results = verify_many(requests, next(iter(selected_readers.values())), backend)
    _require(len(results) == len(requests), "Retirement verification coverage changed")
    return [
        {
            "reference": item["reference"],
            "observation": result.observation.model_dump(mode="json"),
        }
        for item, result in zip(prepared.binding_retirements, results)
    ]


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


def apply(db, verified, *, expected_target_id, preserve_existing=False):
    _disabled()
    receipt = _validated(verified)
    _require(
        not receipt.prepared.binding_retirements and not receipt.binding_retirements,
        "Binding retirements require explicit custody reconciliation",
    )
    _require(
        not receipt.prepared.retained_accounting_support or preserve_existing,
        "Retained accounting support requires preserving existing custody",
    )
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
    preserved = {}
    for identity in receipt.prepared.request.projects:
        project = db.get(Project, str(identity.project_id))
        _legacy(
            db,
            project,
            accounting_only=project.id in receipt.prepared.accounting_only_project_ids,
        )
        _require(
            operations.retained_topology(project)
            == receipt.prepared.topologies[project.id],
            "Retained topology changed; prepare again",
        )
        if preserve_existing:
            preserved[project.id] = _preserved_custody(db, project, receipt)
    enrollment_id = _storage_enrollment(db, receipt.enrollment, expected_target_id)
    result = []
    for identity in receipt.prepared.request.projects:
        project_id = str(identity.project_id)
        result.append(
            preserved.get(project_id) or _save(db, receipt, project_id, enrollment_id)
        )
    db.flush()
    return result


def _project_evidence(receipt, project_id):
    """Compare ownership evidence, excluding verification time and sibling roots."""
    return {
        "target": str(receipt.enrollment.request.target_id),
        "domain": str(receipt.enrollment.pins.domain_id),
        "saved_topology": receipt.prepared.topologies[project_id],
        "runtime_topology": receipt.runtime_topologies[project_id],
        "objects": receipt.object_identities[project_id],
        "bindings": sorted(
            enrollment.digest(b)
            for b in receipt.bindings
            if str(b.root.project_id) == project_id
        ),
        "ancestry": sorted(
            enrollment.digest(p.model_dump(mode="json", exclude={"verified_at"}))
            for p in receipt.ancestry
            if str(p.binding.root.project_id) == project_id
        ),
    }


def _preserved_custody(db, project, receipt):
    previous = retained(db, project)
    support = receipt.prepared.retained_accounting_support.get(project.id)
    if support is not None:
        _require(previous is not None, "Prepared retained custody disappeared")
    if previous is None:
        return
    row, original, _ = previous
    before = _project_evidence(original, project.id)
    after = _project_evidence(receipt, project.id)
    if support is not None:
        _require(
            row.id == support.receipt_id
            and row.digest == support.receipt_digest
            and before["objects"] == support.objects,
            "Prepared retained custody changed",
        )
        _retained_support_subset(after["objects"], support)
        before.pop("objects")
        after.pop("objects")
    _require(
        before == after,
        "Existing custody evidence changed; explicit reconciliation required",
    )
    return row.id


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
    from app.services.native_custody_reconciliation import effective

    try:
        current = effective(db, project_id)
    except AccountingUnavailable as exc:
        raise AccountingUnavailable(
            "Existing custody differs; explicit reconciliation required"
        ) from exc
    if current is not None:
        previous, _ = current
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


RetainedCustody = tuple[
    NativeCustodyReceipt | NativeCustodySuccessor, VerifiedCustody, ProjectRoot
]


def retained(db, project, *, require_topology=True):
    from app.services.native_custody_reconciliation import effective

    current_receipt = effective(db, project.id)
    if current_receipt is None:
        return None
    row, receipt = current_receipt
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
    # Cleanup targets the receipt's original root and storage identities, never
    # the editable canvas. Live namespace custody is checked again before stop
    # and deletion. Enrollment/reconciliation still require exact DB topology.
    if require_topology:
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
    custody = retained(db, project, require_topology=False)
    if custody is None:
        return rows
    _disabled()
    receipt_row, receipt, root = custody
    _require_cleanup_authority(receipt, project.id)
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
    custody = retained(db, project, require_topology=False)
    if custody is None:
        raise AccountingUnavailable("Native custody receipt unavailable")
    receipt_row, receipt, root = custody
    _require_cleanup_authority(receipt, project.id)
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
    custody = retained(db, project, require_topology=False)
    if custody is None:
        raise AccountingUnavailable("Native custody receipt unavailable")
    _, receipt, root = custody
    _require_cleanup_authority(receipt, project.id)
    expected = [b for b in receipt.bindings if b.root == root]
    from app.services.kubevirt_storage_reports import _binding_identity

    _require(
        sorted(enrollment.digest(_binding_identity(b)) for b in expected)
        == sorted(enrollment.digest(_binding_identity(b)) for b in bindings),
        "Cleanup storage identities differ from explicit custody",
    )


def _require_cleanup_authority(receipt, project_id):
    _require(
        project_id not in receipt.prepared.accounting_only_project_ids,
        "An accounting-only receipt cannot authorize cleanup",
    )
