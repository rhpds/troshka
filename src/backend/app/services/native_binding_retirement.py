"""Protected per-binding absence proofs. Provider operations are reads only.

Receipts authorize no deletion and release no accounting by themselves. A
reviewed custody successor and fresh collector observations must consume them.
"""

from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

from app.models.native_binding_retirement import (
    NativeBindingRetirementReceipt,
    NativeBindingRetirementRequest,
)
from app.schemas.kubevirt_storage import (
    BackendImageState,
    BindingRetirementObservation,
    BindingRetirementReference,
    ProjectRoot,
)
from app.schemas.native_binding_retirement import (
    PreparedBindingRetirement,
    VerifiedBindingRetirement,
)
from app.services.kubevirt_storage_collector import _call as _collector_call
from app.services.kubevirt_storage_collector import _snapshot_rows
from app.services.kubevirt_storage_enrollment import digest, root_identity, utc


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _call(method, *args):
    try:
        return _collector_call(method, *args)
    except Exception as exc:
        raise ValueError("Retirement provider observation incomplete") from exc


def private_images(prepared):
    keys = [prepared.binding.image_key]
    if prepared.ancestry is not None:
        require(
            prepared.ancestry.binding == prepared.binding,
            "Retirement ancestry binding changed",
        )
        keys.append(prepared.ancestry.intermediate)
    return keys


def _shape(prepared):
    require(
        root_identity(prepared.root) == root_identity(prepared.binding.root),
        "Retirement root identity changed",
    )
    require(
        type(prepared.root.deployment_generation) is int,
        "Retirement requires a pinned root generation",
    )
    old = prepared.binding.root.deployment_generation
    require(
        old is None or prepared.root.deployment_generation >= old,
        "Retirement root generation regressed",
    )
    private_images(prepared)


def _source(db, prepared):
    if prepared.source_kind == "origin":
        from app.services.native_origin_custody import retirement_source

        return retirement_source(db, prepared)
    from app.models.native_custody import NativeCustodyReceipt, NativeCustodySuccessor
    from app.services import native_online_enrollment as online

    row = db.get(NativeCustodyReceipt, str(prepared.source_custody_id))
    if row is None:
        row = db.get(NativeCustodySuccessor, str(prepared.source_custody_id))
    require(
        row is not None and row.digest == prepared.source_custody_digest,
        "Retirement source custody changed",
    )
    if TYPE_CHECKING:
        assert row is not None
    receipt = online._validated(row.payload["receipt"])
    require(
        receipt.digest == row.digest
        and row.project_id == str(prepared.root.project_id),
        "Retirement source custody identity changed",
    )
    require(
        receipt.enrollment.request.target_id == prepared.target_id
        and receipt.enrollment.pins.domain_id == prepared.domain_id
        and prepared.binding in receipt.bindings,
        "Retirement binding not in original custody",
    )
    expected = [p for p in receipt.ancestry if p.binding == prepared.binding]
    require(
        expected == ([prepared.ancestry] if prepared.ancestry else []),
        "Retirement private ancestry changed",
    )
    names = [
        p["name"]
        for p in receipt.object_identities[row.project_id]
        if p["kind"] == "PersistentVolumeClaim" and p["uid"] == prepared.binding.pvc_uid
    ]
    require(names == [prepared.pvc_name], "Retirement original PVC identity changed")
    _shape(prepared)
    return row


def _idle(db, project_id):
    from app.services import native_online_enrollment as online
    from app.services.kubevirt_storage_enrollment import _guard_root_transitions

    online._disabled()
    _guard_root_transitions(db, [{"before": {"project_id": project_id}}])


def _locks(db, project_id, target_id=None):
    from app.models.kubevirt_storage import NativeReportingTarget
    from app.models.project import Project
    from app.models.user import User
    from app.services.quota_locks import lock_rows

    target = (
        db.query(NativeReportingTarget).filter_by(id=1).with_for_update().one_or_none()
    )
    require(
        target is not None
        and (target_id is None or target.target_id == str(target_id)),
        "Retirement reporting target changed",
    )
    project = db.get(Project, project_id)
    require(project is not None, "Retirement project unavailable")
    owner = db.get(User, project.owner_id)
    require(owner is not None, "Retirement owner unavailable")
    lock_rows(
        db,
        user_ids=[owner.id],
        organization_ids=[owner.organization_id] if owner.organization_id else [],
        project_ids=[project_id],
    )
    db.refresh(project)


def prepare(db, project_id, pvc_uid, *, administrator, reason, root=None):
    from app.models.project import Project
    from app.services.native_online_enrollment import retained

    _locks(db, project_id)
    _idle(db, project_id)
    project = db.get(Project, project_id)
    previous = retained(db, project, require_topology=False)
    require(previous is not None, "Retirement requires existing custody")
    if TYPE_CHECKING:
        assert previous is not None
    row, receipt, original_root = previous
    bindings = [
        b
        for b in receipt.bindings
        if b.pvc_uid == pvc_uid and str(b.root.project_id) == project_id
    ]
    require(len(bindings) == 1, "Retirement requires one exact original binding")
    binding = bindings[0]
    ancestry = [p for p in receipt.ancestry if p.binding == binding]
    require(len(ancestry) <= 1, "Retirement ancestry ambiguous")
    names = [
        p["name"]
        for p in receipt.object_identities[project_id]
        if p["kind"] == "PersistentVolumeClaim" and p["uid"] == pvc_uid
    ]
    require(len(names) == 1, "Retirement original PVC name unavailable")
    value = PreparedBindingRetirement(
        request_id=uuid4(),
        target_id=receipt.enrollment.request.target_id,
        domain_id=receipt.enrollment.pins.domain_id,
        source_custody_id=row.id,
        source_custody_digest=row.digest,
        root=ProjectRoot.model_validate(root) if root is not None else original_root,
        binding=binding,
        pvc_name=names[0],
        ancestry=ancestry[0] if ancestry else None,
        administrator=administrator,
        reason=reason,
        prepared_at=datetime.now(UTC),
    )
    _source(db, value)
    db.add(
        NativeBindingRetirementRequest(
            id=str(value.request_id),
            digest=digest(value),
            payload=value.model_dump(mode="json"),
        )
    )
    db.flush()
    return value.model_dump(mode="json")


def _root(kube, root):
    from app.services.kubevirt_storage_enrollment import _verify_project

    require(
        kube.cluster_identity() == root.cluster_key,
        "Retirement Kubernetes cluster changed",
    )
    _verify_project(kube, root)
    values = kube.projects(root.namespace)
    require(len(values) == 1, "Retirement root inventory changed")
    value = values[0]
    require(
        value["metadata"].get("uid") == root.project_cr_uid
        and value["metadata"].get("generation") == root.deployment_generation,
        "Retirement root changed during read",
    )
    return digest(value.get("spec", {}))


def _backend(backend, binding):
    fsid, pools = (
        _call(backend.cluster_identity).value,
        _call(backend.pool_identity).value,
    )
    require(
        str(fsid).strip() == binding.image_key.ceph_fsid
        and isinstance(pools, list)
        and any(p.get("poolnum") == binding.image_key.pool_id for p in pools),
        "Retirement backend identity changed",
    )
    if TYPE_CHECKING:
        assert (
            str(fsid).strip() == binding.image_key.ceph_fsid
            and isinstance(pools, list)
            and any(p.get("poolnum") == binding.image_key.pool_id for p in pools)
        )
    return digest({"fsid": fsid, "pools": sorted(pools, key=digest)})


def _inventory(kube, prepared, rows=None):
    if rows is None:
        rows = _read_inventory(kube, [prepared.root.namespace])
    require(
        all(isinstance(v, list) and len(v) <= 10000 for v in rows.values()),
        "Retirement inventory incomplete",
    )
    _validate_inventory_rows(rows)
    binding = prepared.binding
    require(
        not any(
            p.get("metadata", {}).get("uid") == binding.pvc_uid for p in rows["pvc"]
        ),
        "Retirement original PVC remains",
    )
    for pv in rows["pv"]:
        spec = pv.get("spec", {})
        csi = spec.get("csi", {})
        require(
            pv.get("metadata", {}).get("uid") != binding.pv_uid
            and spec.get("claimRef", {}).get("uid") != binding.pvc_uid
            and not (
                csi.get("driver") == binding.csi_driver
                and csi.get("volumeHandle") == binding.volume_handle
            ),
            "Retirement original PV or CSI binding remains",
        )
    _snapshots_absent(rows, prepared)
    # Digest identity/spec only: unrelated runtime status and resourceVersion
    # churn cannot hide a changed object, but need not invalidate a stable scan.
    stable = {
        kind: sorted(
            [
                {
                    "uid": v.get("metadata", {}).get("uid"),
                    "name": v.get("metadata", {}).get("name"),
                    "namespace": v.get("metadata", {}).get("namespace"),
                    "spec": v.get("spec", {}),
                }
                for v in entries
            ],
            key=digest,
        )
        for kind, entries in rows.items()
    }
    return digest(stable)


def _validate_inventory_rows(rows):
    for entries in rows.values():
        require(
            all(
                isinstance(v, dict)
                and isinstance(v.get("metadata"), dict)
                and isinstance(v.get("spec", {}), dict)
                for v in entries
            ),
            "Retirement inventory malformed",
        )
        uids = [v["metadata"].get("uid") for v in entries]
        require(
            all(isinstance(uid, str) and uid for uid in uids)
            and len(set(uids)) == len(uids),
            "Retirement inventory identity missing or duplicated",
        )


def _read_inventory(kube, namespaces):
    # PVCs and VolumeSnapshots cannot migrate namespaces. All PV claim UIDs,
    # CSI handles and snapshot contents remain cluster-wide checks.
    return {
        "pv": kube.pvs(),
        "pvc": [
            value
            for namespace in sorted(set(namespaces))
            for value in kube.pvcs(namespace)
        ],
        "snapshot": [
            value
            for namespace in sorted(set(namespaces))
            for value in kube.snapshots(namespace)
        ],
        "content": kube.snapshot_contents(),
    }


def _snapshots_absent(rows, prepared):
    for snapshot in rows["snapshot"]:
        require(
            not (
                snapshot.get("metadata", {}).get("namespace") == prepared.root.namespace
                and snapshot.get("spec", {})
                .get("source", {})
                .get("persistentVolumeClaimName")
                == prepared.pvc_name
            ),
            "Retirement PVC snapshot remains",
        )
    for content in rows["content"]:
        spec = content.get("spec", {})
        if spec.get("driver") != prepared.binding.csi_driver:
            continue
        source = spec.get("source", {})
        # Static snapshot handles have no supported backend resolver. Never
        # infer that such an unresolved object is unrelated to this binding.
        require(
            bool(source.get("volumeHandle"))
            and not source.get("snapshotHandle")
            and source["volumeHandle"] != prepared.binding.volume_handle,
            "Retirement CSI snapshot remains or is unresolved",
        )


def _source_snapshot_absent(backend, prepared):
    if prepared.ancestry is None:
        return
    ancestry = prepared.ancestry
    rows = _call(backend.image_snapshots, ancestry.source.image_key).value
    snapshots, _ = _snapshot_rows(rows, ancestry.source.image_key)
    require(
        ancestry.intermediate_parent not in snapshots,
        "Retirement source snapshot remains",
    )


def verify(prepared, kube, backend, *, root=None):
    roots = (
        None
        if root is None
        else {str(ProjectRoot.model_validate(root).project_id): root}
    )
    return verify_many([prepared], kube, backend, roots=roots)[0]


def _observation_root(prepared, root):
    root = ProjectRoot.model_validate(root)
    require(
        root_identity(root) == root_identity(prepared.root)
        and type(root.deployment_generation) is int
        and root.deployment_generation >= prepared.root.deployment_generation,
        "Retirement observation root changed",
    )
    return root


def _image_proofs(prepared, backend):
    keys = list(dict.fromkeys(k for p in prepared for k in private_images(p)))
    if hasattr(backend, "images_inventory"):
        values = _call(partial(backend.images_inventory, keys, max_objects=10000)).value
    else:
        values = [
            _call(partial(backend.image_inventory, k, max_objects=10000)).value
            for k in keys
        ]
    require(
        isinstance(values, list) and len(values) == len(keys),
        "Retirement backend batch incomplete",
    )
    if TYPE_CHECKING:
        assert isinstance(values, list) and len(values) == len(keys)
    result = {}
    for value in values:
        proof = BackendImageState.model_validate(value)
        key = next(
            (
                k
                for k in keys
                if proof.model_dump(
                    exclude={"method", "active", "trash", "collected_at"}
                )
                == k.model_dump()
            ),
            None,
        )
        require(
            key is not None and key not in result and proof.excludes(key),
            "Retirement private image remains or identity changed",
        )
        result[key] = proof
    return result


def verify_many(prepared, kube, backend, *, roots=None):
    """One bounded read-only batch; immutable proof timestamps are never relabeled."""
    prepared = [PreparedBindingRetirement.model_validate(p) for p in prepared]
    require(0 < len(prepared) <= 100, "Retirement batch size unsupported")
    started = datetime.now(UTC)
    before = []
    observed_roots = []
    for value in prepared:
        _shape(value)
        root = _observation_root(
            value, (roots or {}).get(str(value.root.project_id), value.root)
        )
        observed_roots.append((root, _root(kube, root)))
    namespaces = [p.root.namespace for p in prepared]
    initial_inventory = _read_inventory(kube, namespaces)
    initial_backends = {}
    for value, (root, root_spec) in zip(prepared, observed_roots):
        pool = (value.binding.image_key.ceph_fsid, value.binding.image_key.pool_id)
        if pool not in initial_backends:
            initial_backends[pool] = _backend(backend, value.binding)
        before.append(
            (
                root,
                root_spec,
                initial_backends[pool],
                _inventory(kube, value, initial_inventory),
            )
        )
        _source_snapshot_absent(backend, value)
    proofs = _image_proofs(prepared, backend)
    result = []
    final_inventory = _read_inventory(kube, namespaces)
    final_backends = {}
    for value, (root, root_spec, identity, inventory) in zip(prepared, before):
        _source_snapshot_absent(backend, value)
        # Both complete inventories must independently exclude every selected
        # liability. Unrelated objects may legitimately change between reads.
        final_digest = _inventory(kube, value, final_inventory)
        pool = (value.binding.image_key.ceph_fsid, value.binding.image_key.pool_id)
        if pool not in final_backends:
            final_backends[pool] = _backend(backend, value.binding)
        require(
            identity == final_backends[pool],
            "Retirement backend changed during verification",
        )
        require(
            root_spec == _root(kube, root),
            "Retirement root changed during verification",
        )
        observation = BindingRetirementObservation(
            request_id=value.request_id,
            root=root,
            binding=value.binding,
            private_images=private_images(value),
            objects_absent=True,
            image_absence=[proofs[k] for k in private_images(value)],
            kubernetes_digest=inventory,
            kubernetes_final_digest=final_digest,
            verified_at=started,
        )
        validate_observation(value, observation)
        verified = VerifiedBindingRetirement(
            prepared=value, observation=observation, digest="0" * 64
        )
        result.append(
            verified.model_copy(
                update={
                    "digest": digest(
                        verified.model_dump(mode="json", exclude={"digest"})
                    )
                }
            )
        )
    require(
        (datetime.now(UTC) - started).total_seconds() <= 60,
        "Retirement scan exceeded freshness limit; retry a smaller batch",
    )
    return result


def validate_observation(prepared, observation):
    _observation_root(prepared, observation.root)
    require(
        observation.request_id == prepared.request_id
        and observation.binding == prepared.binding
        and observation.objects_absent is True
        and observation.private_images == private_images(prepared),
        "Retirement observation identities changed",
    )
    require(
        len(observation.image_absence) == len(observation.private_images),
        "Retirement image absence incomplete",
    )
    for key, proof in zip(observation.private_images, observation.image_absence):
        require(
            proof.model_dump(exclude={"method", "active", "trash", "collected_at"})
            == key.model_dump()
            and proof.excludes(key)
            and 0
            <= (utc(proof.collected_at) - utc(observation.verified_at)).total_seconds()
            <= 60,
            "Retirement immutable absence proof invalid or stale",
        )


def _validated(value):
    value = VerifiedBindingRetirement.model_validate(value)
    require(
        value.digest == digest(value.model_dump(mode="json", exclude={"digest"})),
        "Retirement receipt digest mismatch",
    )
    _shape(value.prepared)
    validate_observation(value.prepared, value.observation)
    return value


def review(verified):
    value = _validated(verified)
    body = value.model_dump(mode="json", exclude={"digest"})
    return {"review": body, "digest": digest(body)}


def _protected(db, prepared):
    row = db.get(NativeBindingRetirementRequest, str(prepared.request_id))
    require(
        row is not None and row.digest == digest(row.payload) == digest(prepared),
        "Retirement was not independently prepared",
    )
    _source(db, prepared)


def validate_prepared(db, prepared):
    """Check protected selection before starting any provider reads."""
    value = PreparedBindingRetirement.model_validate(prepared)
    _protected(db, value)
    return value


def apply(db, verified, *, review_digest):
    from app.models.project import Project
    from app.services.native_online_enrollment import retained

    value = _validated(verified)
    require(
        value.prepared.source_kind == "administrator",
        "Origin retirement requires origin publication",
    )
    require(
        review(value)["digest"] == review_digest,
        "Exact retirement review digest required",
    )
    prepared = value.prepared
    _locks(db, str(prepared.root.project_id), prepared.target_id)
    require(value.observation.root == prepared.root, "Retirement prepared root changed")
    _protected(db, prepared)
    project_id = str(prepared.root.project_id)
    _idle(db, project_id)
    previous = retained(db, db.get(Project, project_id), require_topology=False)
    require(
        previous is not None
        and previous[0].id == str(prepared.source_custody_id)
        and previous[0].digest == prepared.source_custody_digest,
        "Retirement custody predecessor changed",
    )
    age = (datetime.now(UTC) - utc(value.observation.verified_at)).total_seconds()
    require(0 <= age <= 60, "Retirement verification must be fresh")
    row = (
        db.query(NativeBindingRetirementReceipt)
        .filter_by(request_id=str(prepared.request_id))
        .one_or_none()
    )
    if row is None:
        row = NativeBindingRetirementReceipt(
            id=str(uuid4()),
            project_id=project_id,
            request_id=str(prepared.request_id),
            digest=value.digest,
            payload=value.model_dump(mode="json"),
        )
        db.add(row)
        db.flush()
    require(
        row.digest == value.digest, "Retirement request already has different evidence"
    )
    return BindingRetirementReference.model_validate(
        dict(receipt_id=row.id, digest=row.digest)
    )


def resolve(db, reference):
    reference = BindingRetirementReference.model_validate(reference)
    row = db.get(NativeBindingRetirementReceipt, str(reference.receipt_id))
    require(
        row is not None and row.digest == reference.digest,
        "Protected retirement receipt unavailable",
    )
    if TYPE_CHECKING:
        assert row is not None
    value = _validated(row.payload)
    require(
        value.digest == row.digest
        and str(value.prepared.root.project_id) == row.project_id
        and str(value.prepared.request_id) == row.request_id,
        "Protected retirement receipt changed",
    )
    _protected(db, value.prepared)
    return value
