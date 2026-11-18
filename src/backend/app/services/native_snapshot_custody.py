"""Protected new-producer snapshot facts; no historical snapshot adoption."""

from datetime import UTC, datetime
from functools import partial
from typing import Any, cast

from app.schemas.native_snapshot_custody import (
    OriginProducerIntent,
    OriginSnapshotLiability,
    OriginSnapshotRestore,
    SnapshotBackendIdentity,
    SnapshotFacts,
    SnapshotJournalProfile,
)
from app.services.kubevirt_storage_enrollment import digest


def require(condition, message):
    if not condition:
        raise ValueError(message)


def prepare_facts(previous, producer, mode):
    facts = (
        SnapshotFacts(
            **{key: getattr(previous, key) for key in SnapshotFacts.model_fields}
        )
        if previous
        else SnapshotFacts()
    )
    require(
        (producer is not None) == (mode == "producer"),
        "Origin producer intent mode mismatch",
    )
    if producer is not None:
        producer = OriginProducerIntent.model_validate(producer)
        require(
            producer.uid is None
            and producer.admitted_spec_digest is None
            and producer.source_binding is None
            and producer.content_identity is None,
            "Origin producer must be retained before POST",
        )
        old = [p for p in facts.producers if p.id == producer.id]
        fields = {"uid", "admitted_spec_digest", "source_binding", "content_identity"}
        require(
            not old
            or old[0].model_dump(exclude=fields) == producer.model_dump(exclude=fields),
            "Origin producer intent changed",
        )
        require(
            not any(
                p.kind == producer.kind
                and p.name == producer.name
                and p.id != producer.id
                for p in facts.producers
            ),
            "Origin producer name cannot be reused",
        )
        if not old:
            facts = facts.model_copy(update={"producers": [*facts.producers, producer]})
    return facts.model_dump()


def outstanding(receipt):
    retired = retired_ids(receipt)
    return [
        p
        for p in receipt.producers
        if p.kind == "VolumeSnapshot" and p.id not in retired
    ]


def retired_ids(facts):
    return {r.producer_id for r in facts.snapshot_retirements}


def same_allocation(before, after):
    """Only loss of the original parent link may progress; identity never changes."""
    return before.model_dump(exclude={"verified_at", "parent"}) == after.model_dump(
        exclude={"verified_at", "parent"}
    ) and (before.parent == after.parent or after.parent is None)


def stable_facts(facts):
    return dict(
        producers=[p.model_dump() for p in facts.producers],
        snapshots=[p.model_dump(exclude={"verified_at"}) for p in facts.snapshots],
        restores=[
            p.model_dump(exclude={"verified_at"}) for p in facts.snapshot_restores
        ],
        retirements=[p.model_dump() for p in facts.snapshot_retirements],
    )


def _api(kube):
    from app.services.native_online_enrollment import ReadOnlyManaged

    return ReadOnlyManaged(kube)


def _find(api, kind, namespace, name):
    matches = [v for v in api.rows(kind, namespace) if v["metadata"]["name"] == name]
    require(len(matches) <= 1, "Snapshot object name ambiguous")
    return matches[0] if matches else None


def _producer(prepared, intent, api, root):
    value = _find(api, intent.kind, prepared.root.namespace, intent.name)
    if value is None:
        return intent, None
    recorded = (
        root.get("status", {}).get("originProducerObjects", {}).get(str(intent.id))
    )
    require(isinstance(recorded, dict), "Snapshot producer original UID unavailable")
    uid, spec_digest = recorded.get("uid"), recorded.get("specDigest")
    spec = dict(value.get("spec", {}))
    if intent.kind == "PersistentVolumeClaim":
        spec.pop("volumeName", None)
    require(
        uid == value["metadata"].get("uid") and spec_digest == digest(spec),
        "Snapshot producer identity changed",
    )
    require(
        intent.uid in (None, uid)
        and intent.admitted_spec_digest in (None, spec_digest),
        "Snapshot retained producer changed",
    )
    metadata = value["metadata"]
    annotations = metadata.get("annotations", {})
    require(
        all(
            annotations.get("troshka.redhat.com/origin-" + field) == expected
            for field, expected in (
                ("producer", str(intent.id)),
                ("operation", intent.operation_digest),
                ("role", intent.role),
            )
        ),
        "Snapshot operation metadata changed",
    )
    owners = metadata.get("ownerReferences", [])
    require(
        len(owners) == 1
        and owners[0].get("uid") == prepared.root.project_cr_uid
        and owners[0].get("kind") == "TroshkaProject"
        and owners[0].get("controller") is True,
        "Snapshot producer root owner changed",
    )
    return (
        intent.model_copy(update={"uid": uid, "admitted_spec_digest": spec_digest}),
        value,
    )


def _profile(backend, driver, class_name):
    values = [
        SnapshotJournalProfile.model_validate(v)
        for v in getattr(backend, "snapshot_profiles", [])
    ]
    matches = [
        v for v in values if v.driver == driver and v.snapshot_class == class_name
    ]
    require(len(matches) == 1, "Snapshot journal profile unavailable or ambiguous")
    return matches[0]


def _source(prepared, intent, bindings, kube):
    from app.schemas.kubevirt_storage import VolumeBinding
    from app.services.kubevirt_storage_inventory import _pvc_binding

    pvcs = [
        v
        for v in kube.pvcs(prepared.root.namespace)
        if v["metadata"]["name"] == intent.source_pvc_name
    ]
    require(len(pvcs) == 1, "Snapshot original source PVC missing")
    pvs = kube.pvs()
    by_name = {p["metadata"]["name"]: p for p in pvs}
    require(len(by_name) == len(pvs), "Snapshot source PV ambiguous")
    value, _ = _pvc_binding(kube, pvcs[0], by_name, prepared.root.namespace)
    source = VolumeBinding(root=prepared.root, **value)
    expected = (
        [intent.source_binding]
        if intent.source_binding
        else [b for b in bindings if b.pvc_uid == source.pvc_uid]
    )
    require(expected == [source], "Snapshot original source binding changed")
    return source, pvcs[0]["spec"]["volumeName"]


def _chain(api, prepared, intent, snapshot, source):
    spec = snapshot.get("spec", {})
    require(
        spec == intent.snapshot_spec and digest(spec) == intent.spec_digest,
        "Snapshot requested specification changed",
    )
    name = snapshot.get("status", {}).get("boundVolumeSnapshotContentName")
    if name is None:
        return None
    content = api.get("VolumeSnapshotContent", None, name)
    cspec, ref = content["spec"], content["spec"].get("volumeSnapshotRef", {})
    require(
        ref.get("uid") == intent.uid
        and ref.get("name") == intent.name
        and ref.get("namespace") == prepared.root.namespace,
        "Snapshot content reference changed",
    )
    require(
        cspec.get("source") == {"volumeHandle": source.volume_handle}
        and cspec.get("driver") == source.csi_driver
        and cspec.get("deletionPolicy") == "Delete"
        and cspec.get("volumeSnapshotClassName") == spec.get("volumeSnapshotClassName"),
        "Snapshot dynamic source, class or deletion policy changed",
    )
    klass = api.get("VolumeSnapshotClass", None, cspec["volumeSnapshotClassName"])
    require(
        klass.get("driver") == source.csi_driver
        and klass.get("deletionPolicy") == "Delete",
        "Snapshot class changed",
    )
    identity = dict(
        name=content["metadata"]["name"],
        uid=content["metadata"]["uid"],
        class_uid=klass["metadata"]["uid"],
        class_digest=digest({k: v for k, v in klass.items() if k != "metadata"}),
    )
    require(
        intent.content_identity in (None, identity),
        "Snapshot original content or class changed",
    )
    return content


def _content_intent(prepared, intent, live, api):
    content = _chain(api, prepared, intent, live, intent.source_binding)
    if content is None:
        return intent
    klass = api.get(
        "VolumeSnapshotClass", None, live["spec"]["volumeSnapshotClassName"]
    )
    identity = dict(
        name=content["metadata"]["name"],
        uid=content["metadata"]["uid"],
        class_uid=klass["metadata"]["uid"],
        class_digest=digest({k: v for k, v in klass.items() if k != "metadata"}),
    )
    return intent.model_copy(update={"content_identity": identity})


def _resolve(prepared, intent, snapshot, bindings, kube, backend, api):
    from app.services.kubevirt_storage_collector import _call, _parent

    source, pv_name = _source(prepared, intent, bindings, kube)
    content = _chain(api, prepared, intent, snapshot, source)
    if content is None or not content.get("status", {}).get("snapshotHandle"):
        return None
    handle = content["status"]["snapshotHandle"]
    profile = _profile(
        backend, source.csi_driver, snapshot["spec"]["volumeSnapshotClassName"]
    )
    resolved = _call(
        partial(
            backend.resolve_csi_snapshot,
            profile,
            handle,
            expected_request="snapshot-" + intent.uid,
            source=source.image_key,
            source_handle=source.volume_handle,
            source_request=pv_name,
        )
    )
    identity = SnapshotBackendIdentity.model_validate(resolved.value)
    require(
        identity.handle == handle and identity.image != source.image_key,
        "Snapshot immutable backend identity changed",
    )
    pools = _call(backend.pool_identity).value
    parent = _parent(
        _call(backend.image_info, identity.image).value, identity.image, pools, backend
    )
    source_parent = _parent(
        _call(backend.image_info, source.image_key).value,
        source.image_key,
        pools,
        backend,
    )
    require(
        parent is None or parent.image_key == source.image_key,
        "Snapshot parent outside original source",
    )
    return OriginSnapshotLiability(
        producer_id=intent.id,
        source=source,
        source_pvc_name=intent.source_pvc_name,
        source_pv_name=pv_name,
        snapshot_name=intent.name,
        snapshot_uid=intent.uid,
        content_name=content["metadata"]["name"],
        content_uid=content["metadata"]["uid"],
        snapshot_class=profile.snapshot_class,
        csi_driver=source.csi_driver,
        handle=handle,
        image=identity.image,
        snapshot=identity.snapshot,
        parent=parent,
        source_parent=source_parent,
        profile_digest=digest(profile),
        verified_at=datetime.now(UTC),
    )


def observe(prepared, kube, backend, bindings):
    if not prepared.producers:
        return SnapshotFacts()
    api = _api(kube)
    root = kube.projects(prepared.root.namespace)[0]
    producers, snapshots = [], {p.producer_id: p for p in prepared.snapshots}
    for intent in prepared.producers:
        if intent.kind == "VolumeSnapshot" and intent.source_binding is None:
            source, _ = _source(prepared, intent, bindings, kube)
            intent = intent.model_copy(update={"source_binding": source})
        intent, live = _producer(prepared, intent, api, root)
        if (
            live is None
            or intent.kind != "VolumeSnapshot"
            or intent.id in retired_ids(prepared)
        ):
            producers.append(intent)
            continue
        intent = _content_intent(prepared, intent, live, api)
        producers.append(intent)
        proof = _resolve(prepared, intent, live, bindings, kube, backend, api)
        if proof is not None:
            old = snapshots.get(intent.id)
            require(
                old is None or same_allocation(old, proof),
                "Snapshot retained authority changed",
            )
            snapshots[intent.id] = proof
    facts = SnapshotFacts(
        producers=producers,
        snapshots=list(snapshots.values()),
        snapshot_restores=prepared.snapshot_restores,
        snapshot_retirements=prepared.snapshot_retirements,
    )
    restores = {p.producer_id: p for p in facts.snapshot_restores}
    for proof in restore_observations(facts, kube, backend, bindings):
        old = restores.get(proof.producer_id)
        require(
            old is None or same_allocation(old, proof),
            "Snapshot restore identity changed",
        )
        restores[proof.producer_id] = proof
    facts = facts.model_copy(update={"snapshot_restores": list(restores.values())})
    from app.services.native_snapshot_observation import verify_graph

    verify_graph(facts, kube, backend)
    return facts


def restore_observations(facts, kube, backend, bindings):
    from app.services.kubevirt_storage_collector import _call, _parent

    api, proofs = _api(kube), []
    pools = _call(backend.pool_identity).value
    for intent in facts.producers:
        if intent.role != "restore" or intent.uid is None:
            continue
        selected = [b for b in bindings if b.pvc_uid == intent.uid]
        if not selected:
            continue
        require(len(selected) == 1, "Snapshot restore binding ambiguous")
        binding = selected[0]
        pvc = api.get("PersistentVolumeClaim", binding.root.namespace, intent.name)
        reference = pvc.get("spec", {}).get("dataSource", {})
        snapshots = [
            p for p in facts.snapshots if p.snapshot_name == reference.get("name")
        ]
        require(
            reference.get("kind") == "VolumeSnapshot"
            and reference.get("apiGroup") == "snapshot.storage.k8s.io"
            and len(snapshots) == 1,
            "Snapshot restore source unresolved",
        )
        source = snapshots[0]
        info = cast(dict[str, Any], _call(backend.image_info, binding.image_key).value)
        parent = _parent(info, binding.image_key, pools, backend)
        require(
            info.get("id") == binding.image_key.image_id
            and parent in (None, source.snapshot)
            and binding.image_key not in {source.image, source.source.image_key},
            "Snapshot restore backend changed",
        )
        proofs.append(
            OriginSnapshotRestore(
                producer_id=intent.id,
                snapshot_producer_id=source.producer_id,
                binding=binding,
                parent=parent,
                verified_at=datetime.now(UTC),
            )
        )
    return proofs


def source_edges(facts):
    from collections import defaultdict

    edges = defaultdict(dict)
    for proof in facts.snapshots:
        if proof.producer_id not in retired_ids(facts) and proof.parent is not None:
            edges[proof.source.image_key][proof.parent] = proof.image
    return edges
