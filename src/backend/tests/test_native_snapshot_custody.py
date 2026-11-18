"""Exact origin snapshot chain and protected pre-POST debt."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.kubevirt_storage_enrollment import digest
from tests import test_native_origin_producer_fences as checkpoint_tests

cleanup_case = checkpoint_tests.cleanup_case
deploy_case = checkpoint_tests.deploy_case
isolated_quota_database = checkpoint_tests.isolated_quota_database
native_ledger = checkpoint_tests.native_ledger
online_case = checkpoint_tests.online_case
resume_case = checkpoint_tests.resume_case
checkpoint_case = checkpoint_tests.checkpoint_case


def test_prepare_retains_snapshot_intent_before_post_and_no_fake_binding(
    checkpoint_case,
):
    from app.services import native_origin_custody as custody

    q = checkpoint_case
    pvc = q.reader.pvcs(q.root.namespace)[0]
    spec = {
        "source": {"persistentVolumeClaimName": pvc["metadata"]["name"]},
        "volumeSnapshotClassName": "rbd-snapshot",
    }
    producer = dict(
        id=str(uuid4()),
        operation_digest="a" * 64,
        kind="VolumeSnapshot",
        name="snapshot-new",
        spec_digest=digest(spec),
        role="snapshot",
        source_pvc_name=pvc["metadata"]["name"],
        snapshot_spec=spec,
    )
    with q.sessions() as db:
        assert "producer" in __import__("inspect").signature(custody.prepare).parameters
        prepared = custody.prepare(
            db,
            str(q.intent.id),
            mode="producer",
            pvc_uids=[pvc["metadata"]["uid"]],
            producer=producer,
        )
        db.commit()
    value = custody.verify(prepared, q.readers, q.backend)
    with q.sessions() as db:
        row = custody.apply(db, value)
        db.commit()
        receipt = custody._receipt(row)
    assert len(receipt.producers) == 1
    assert receipt.producers[0].uid is None
    assert receipt.snapshots == []
    assert {b.pvc_uid for b in receipt.bindings} == {pvc["metadata"]["uid"]}
    assert custody.unresolved_snapshots(receipt)


def test_legacy_custody_serialization_has_no_new_empty_fields(checkpoint_case):
    from app.services import native_origin_custody as custody

    q = checkpoint_case
    _, value = checkpoint_tests.custody_tests.publish(q)
    assert (
        not {"producers", "snapshots", "snapshot_restores", "snapshot_retirements"}
        & value.model_dump().keys()
    )
    with q.sessions() as db:
        receipt = custody._current(db, q.intent.id)[1]
    assert (
        not {"producers", "snapshots", "snapshot_restores", "snapshot_retirements"}
        & receipt.model_dump().keys()
    )


@pytest.fixture
def snapshot_case(checkpoint_case, monkeypatch):
    from app.schemas.kubevirt_storage import ImageKey, ReadResult, SnapshotKey
    from app.services import native_origin_custody as custody
    from tests.test_native_snapshot_journal import PROFILE

    q = checkpoint_case
    api = q.checkpoint_api
    get = q.reader.get
    rows = q.reader.rows
    monkeypatch.setattr(
        q.reader,
        "rows",
        lambda kind, namespace: api.rows(kind, namespace)
        if kind == "VolumeSnapshotContent"
        else rows(kind, namespace),
    )
    monkeypatch.setattr(
        q.reader,
        "get",
        lambda kind, namespace, name: api.get(kind, namespace, name)
        if kind in {"VolumeSnapshotClass", "VolumeSnapshotContent"}
        else get(kind, namespace, name),
    )
    source = q.reader.pvcs(q.root.namespace)[0]
    q.snap_spec = {
        "source": {"persistentVolumeClaimName": source["metadata"]["name"]},
        "volumeSnapshotClassName": "rbd-snapshot",
    }
    q.producer = dict(
        id=str(uuid4()),
        operation_digest="a" * 64,
        kind="VolumeSnapshot",
        name="snapshot-new",
        spec_digest=digest(q.snap_spec),
        role="snapshot",
        source_pvc_name=source["metadata"]["name"],
        snapshot_spec=q.snap_spec,
    )
    with q.sessions() as db:
        request = custody.prepare(
            db,
            str(q.intent.id),
            mode="producer",
            pvc_uids=[source["metadata"]["uid"]],
            producer=q.producer,
        )
        db.commit()
    with q.sessions() as db:
        custody.apply(db, custody.verify(request, q.readers, q.backend))
        db.commit()
    root = api.rows("TroshkaProject", q.root.namespace)[0]
    root.setdefault("status", {})["originProducerObjects"] = {
        q.producer["id"]: {
            "uid": "snapshot-original",
            "specDigest": digest(q.snap_spec),
        }
    }
    api.values["TroshkaProject", root["metadata"]["name"]] = root
    q.snapshot = dict(
        apiVersion="snapshot.storage.k8s.io/v1",
        kind="VolumeSnapshot",
        metadata=dict(
            name="snapshot-new",
            namespace=q.root.namespace,
            uid="snapshot-original",
            resourceVersion="1",
            ownerReferences=[
                dict(
                    apiVersion=root["apiVersion"],
                    kind=root["kind"],
                    name=root["metadata"]["name"],
                    uid=root["metadata"]["uid"],
                    controller=True,
                )
            ],
            annotations={
                "troshka.redhat.com/origin-producer": q.producer["id"],
                "troshka.redhat.com/origin-operation": "a" * 64,
                "troshka.redhat.com/origin-role": "snapshot",
            },
        ),
        spec=q.snap_spec,
        status=dict(readyToUse=True, boundVolumeSnapshotContentName="content-original"),
    )
    binding = q.report.volume_bindings[0]
    q.content = dict(
        apiVersion="snapshot.storage.k8s.io/v1",
        kind="VolumeSnapshotContent",
        metadata=dict(name="content-original", uid="content-original-uid"),
        spec=dict(
            driver=binding.csi_driver,
            deletionPolicy="Delete",
            volumeSnapshotClassName="rbd-snapshot",
            source=dict(volumeHandle=binding.volume_handle),
            volumeSnapshotRef=dict(
                uid="snapshot-original", name="snapshot-new", namespace=q.root.namespace
            ),
        ),
        status=dict(snapshotHandle="retained-handle", readyToUse=True),
    )
    q.profile = {**PROFILE, "driver": binding.csi_driver}
    q.image = ImageKey(
        **{**binding.image_key.model_dump(), "image_id": "snapshot-backing"}
    )
    q.backend_snapshot = SnapshotKey(image_key=q.image, snapshot_id="7")
    api.values["VolumeSnapshot", "snapshot-new"] = q.snapshot
    api.values["VolumeSnapshotContent", "content-original"] = q.content
    api.values["VolumeSnapshotClass", "rbd-snapshot"] = dict(
        metadata=dict(name="rbd-snapshot", uid="class-uid"),
        driver=binding.csi_driver,
        deletionPolicy="Delete",
        parameters={},
    )
    q.backend.snapshot_profiles = [q.profile]
    monkeypatch.setattr(
        q.backend,
        "pool_identity",
        lambda: ReadResult(
            status="complete",
            collected_at=datetime.now(UTC),
            value=[{"poolnum": q.image.pool_id, "poolname": "rbd"}],
        ),
    )
    monkeypatch.setattr(
        q.backend,
        "resolve_csi_snapshot",
        lambda *args, **kwargs: ReadResult(
            status="complete",
            collected_at=datetime.now(UTC),
            value=dict(
                image=q.image.model_dump(),
                snapshot=q.backend_snapshot.model_dump(),
                handle="retained-handle",
            ),
        ),
        raising=False,
    )
    info = q.backend.image_info
    snaps = q.backend.image_snapshots
    monkeypatch.setattr(q.backend, "image_info", lambda key: info(key))
    monkeypatch.setattr(
        q.backend,
        "image_snapshots",
        lambda key: ReadResult(
            status="complete",
            collected_at=datetime.now(UTC),
            value=[dict(id=7, name="csi-snap-test", namespace={"type": "user"})],
        )
        if key == q.image
        else snaps(key),
    )
    monkeypatch.setattr(
        q.backend,
        "image_children",
        lambda *args: ReadResult(
            status="complete", collected_at=datetime.now(UTC), value=[]
        ),
        raising=False,
    )
    return q


def snapshot_verify(q):
    from app.services import native_origin_custody as custody

    with q.sessions() as db:
        value = custody.prepare(
            db,
            str(q.intent.id),
            mode="removal",
            pvc_uids=[q.reader.pvcs(q.root.namespace)[0]["metadata"]["uid"]],
        )
        db.commit()
    return custody.verify(value, q.readers, q.backend)


def test_resolves_original_snapshot_separately_from_source_pvc(snapshot_case):
    q = snapshot_case
    value = snapshot_verify(q)
    assert len(value.snapshots) == 1
    proof = value.snapshots[0]
    assert proof.image == q.image
    assert proof.source.image_key != q.image
    assert proof.snapshot == q.backend_snapshot
    assert proof.content_uid == "content-original-uid"
    assert len(value.bindings) == 1


@pytest.mark.parametrize(
    "change",
    [
        "snapshot_uid",
        "content_ref",
        "source_handle",
        "retain",
        "static",
        "class",
        "unknown_profile",
    ],
)
def test_changed_snapshot_chain_never_acquires_authority(snapshot_case, change):
    q = snapshot_case
    if change == "snapshot_uid":
        q.snapshot["metadata"]["uid"] = "replacement"
    elif change == "content_ref":
        q.content["spec"]["volumeSnapshotRef"]["uid"] = "replacement"
    elif change == "source_handle":
        q.content["spec"]["source"]["volumeHandle"] = "replacement"
    elif change == "retain":
        q.content["spec"]["deletionPolicy"] = "Retain"
    elif change == "static":
        q.content["spec"]["source"] = {"snapshotHandle": "static"}
    elif change == "class":
        q.content["spec"]["volumeSnapshotClassName"] = "foreign"
    else:
        q.backend.snapshot_profiles = []
    with pytest.raises(Exception):
        snapshot_verify(q)


def test_create_ack_commits_pending_snapshot_before_original_post(checkpoint_case):
    import importlib

    from app.services import native_origin_checkpoints as checkpoints
    from app.services import native_origin_custody as custody

    q = checkpoint_case
    checkpoints.protocol()
    producer = importlib.import_module("helpers.native_snapshot_producer")
    api = q.checkpoint_api
    root = api.rows("TroshkaProject", q.root.namespace)[0]
    source = api.rows("PersistentVolumeClaim", q.root.namespace)[0]
    body = dict(
        kind="VolumeSnapshot",
        apiVersion="snapshot.storage.k8s.io/v1",
        metadata={"name": "new-snap"},
        spec=dict(
            source={"persistentVolumeClaimName": source["metadata"]["name"]},
            volumeSnapshotClassName="rbd-snapshot",
        ),
    )
    request = producer.build_request(
        api, root, q.root.namespace, producer.intent(root, body)
    )
    checkpoints.protocol().publish(api, root, request)
    ack = checkpoints.service_request(q.sessions, api, str(q.intent.id), request)
    assert checkpoints.verify_signature(
        root["spec"]["originCheckpoint"]["publicKey"], request, ack
    )
    with q.sessions() as db:
        receipt = custody._current(db, q.intent.id)[1]
        assert custody.unresolved_snapshots(receipt)
        assert receipt.producers[0].name == "new-snap"
    assert api.find("VolumeSnapshot", q.root.namespace, "new-snap") is None


@pytest.mark.parametrize("linked", [True, False])
def test_linked_or_flattened_restore_keeps_snapshot_as_separate_liability(
    snapshot_case, monkeypatch, linked
):
    from app.schemas.kubevirt_storage import ReadResult
    from app.schemas.native_snapshot_custody import OriginProducerIntent
    from app.services import native_snapshot_custody as snapshots

    q = snapshot_case
    verified = snapshot_verify(q)
    binding = verified.bindings[0].model_copy(
        update={
            "pvc_uid": "restore-pvc",
            "pv_uid": "restore-pv",
            "image_key": q.image.model_copy(update={"image_id": "restored-head"}),
        }
    )
    intent = OriginProducerIntent(
        id=uuid4(),
        operation_digest="a" * 64,
        kind="PersistentVolumeClaim",
        name="restore-new",
        spec_digest="b" * 64,
        role="restore",
        uid="restore-pvc",
        admitted_spec_digest="c" * 64,
    )
    api = q.checkpoint_api
    api.values["PersistentVolumeClaim", "restore-new"] = dict(
        metadata=dict(name="restore-new", uid="restore-pvc"),
        spec=dict(
            dataSource=dict(
                kind="VolumeSnapshot",
                name="snapshot-new",
                apiGroup="snapshot.storage.k8s.io",
            )
        ),
    )
    info = q.backend.image_info

    def image_info(key):
        if key != binding.image_key:
            return info(key)
        value = {"id": key.image_id}
        if linked:
            value["parent"] = {
                "pool": "rbd",
                "pool_namespace": "",
                "id": q.image.image_id,
                "snap_id": 7,
            }
        return ReadResult(
            status="complete", collected_at=datetime.now(UTC), value=value
        )

    monkeypatch.setattr(q.backend, "image_info", image_info)
    assert hasattr(snapshots, "restore_observations")
    facts = verified.model_copy(update={"producers": [*verified.producers, intent]})
    proofs = snapshots.restore_observations(facts, q.reader, q.backend, [binding])
    assert len(proofs) == 1
    assert proofs[0].binding == binding
    assert proofs[0].parent == (q.backend_snapshot if linked else None)
    assert proofs[0].snapshot_producer_id == verified.snapshots[0].producer_id


@pytest.mark.parametrize("duplicate", [False, True])
@pytest.mark.parametrize("unmatched", ["none", "sibling", "unknown_root"])
def test_snapshot_backing_is_accounted_without_fabricated_binding(
    snapshot_case, duplicate, unmatched
):
    from app.schemas.kubevirt_storage import CSISnapshotEvidence
    from app.services.kubevirt_storage_graph import attribute_graph
    from app.services.kubevirt_storage_reports import report_inventory

    q = snapshot_case
    facts = snapshot_verify(q)
    inventory = report_inventory(q.report)
    if unmatched != "none":
        root = next(
            r for r in inventory.project_roots if r.project_id != q.root.project_id
        )
        if unmatched == "unknown_root":
            root = root.model_copy(update={"namespace_uid": "unknown-namespace"})
        evidence = CSISnapshotEvidence(
            root=root,
            snapshot_uid=None,
            snapshot_name=None,
            content_name=None,
            content_uid=None,
            source_pvc_name=None,
            csi_driver=None,
            ready_to_use=False,
            snapshot_handle_sha256=None,
            source_snapshot_handle_sha256=None,
            source_volume_handle_sha256=None,
        )
        inventory = inventory.model_copy(update={"snapshot_evidence": [evidence]})
    source = next(i for i in q.report.images if i.key == facts.bindings[0].image_key)
    source = source.model_copy(update={"parent": None, "snapshots": [], "children": []})
    backing = source.model_copy(
        update={
            "key": q.image,
            "snapshots": [q.backend_snapshot],
            "allocated_bytes": 1234,
            "collected_at": datetime.now(UTC),
        }
    )
    source = source.model_copy(update={"collected_at": datetime.now(UTC)})
    images = [i if i.key != source.key else source for i in q.report.images] + [backing]
    from app.services.native_clone_graph import binding_identity

    assert binding_identity(facts.snapshots[0].source) in [
        binding_identity(b) for b in inventory.volume_bindings
    ]
    assert source.parent == facts.snapshots[0].source_parent
    assert backing.parent == facts.snapshots[0].parent
    assert (
        "snapshot_facts" in __import__("inspect").signature(attribute_graph).parameters
    )
    if duplicate:
        facts = facts.model_copy(
            update={
                "snapshots": [
                    facts.snapshots[0],
                    facts.snapshots[0].model_copy(update={"producer_id": uuid4()}),
                ]
            }
        )
    result = attribute_graph(inventory, images, snapshot_facts=[facts])
    project = next(p for p in result.projects if p.project_id == q.root.project_id)
    if duplicate or unmatched == "unknown_root":
        assert project.coverage != "complete"
        assert all(p.coverage != "complete" for p in result.projects)
        return
    assert project.coverage == "complete", project.blockers
    if unmatched == "sibling":
        sibling = next(p for p in result.projects if p.project_id != q.root.project_id)
        assert sibling.coverage != "complete"
        assert "origin_snapshot_authority_unverified" in sibling.blockers
    without = attribute_graph(inventory, [i for i in images if i.key != q.image])
    previous = next(p for p in without.projects if p.project_id == q.root.project_id)
    assert project.allocated_bytes == previous.allocated_bytes + 1234
    assert q.image not in {
        b.image_key for b in inventory.volume_bindings + inventory.cache_bindings
    }


def test_snapshot_private_image_collision_with_sibling_binding_denies(snapshot_case):
    from app.services.kubevirt_storage_graph import attribute_graph
    from app.services.kubevirt_storage_reports import report_inventory

    q = snapshot_case
    facts = snapshot_verify(q)
    sibling = next(
        b for b in q.report.volume_bindings if b.root.project_id != q.root.project_id
    )
    facts = facts.model_copy(
        update={
            "snapshots": [
                facts.snapshots[0].model_copy(update={"image": sibling.image_key})
            ]
        }
    )
    assert (
        "snapshot_facts" in __import__("inspect").signature(attribute_graph).parameters
    )
    result = attribute_graph(
        report_inventory(q.report), q.report.images, snapshot_facts=[facts]
    )
    assert all(p.coverage != "complete" for p in result.projects)


def test_collector_seeds_retained_snapshot_after_flatten_without_pvc(
    snapshot_case, monkeypatch
):
    from app.schemas.kubevirt_storage import CollectionLimits, ReadResult
    from app.services.kubevirt_storage_collector import collect_report
    from app.services.kubevirt_storage_reports import report_inventory

    q = snapshot_case
    facts = snapshot_verify(q)
    inventory = report_inventory(q.report)
    backend = q.backend

    def result(value):
        return ReadResult(
            status="complete", value=value, collected_at=datetime.now(UTC)
        )

    backend.capability = SimpleNamespace(
        command_version="test",
        merge_snapshots_validated=True,
        validation_reference="local-fixture",
        semantics_version="kubevirt-rbd-allocated-v1",
    )
    backend.command_version = lambda: result("test")
    backend.image_usage = lambda key: result(
        {"images": [{"id": key.image_id, "used_size": 12, "provisioned_size": 20}]}
    )
    backend.trash_inventory = lambda key: result([])
    backend.recheck_inventory = lambda: inventory
    backend.image_info = lambda key: result({"id": key.image_id})
    snapshots = backend.image_snapshots
    backend.image_snapshots = lambda key: result(snapshots(key).value)
    assert (
        "snapshot_facts" in __import__("inspect").signature(collect_report).parameters
    )
    report = collect_report(
        inventory,
        backend,
        limits=CollectionLimits(),
        clock=lambda: datetime.now(UTC),
        snapshot_facts=[facts],
    )
    assert q.image in {i.key for i in report.images}
    assert all(p.coverage == "complete" for p in report.projects), [
        p.blockers for p in report.projects
    ]
    assert len(report.volume_bindings) == len(q.report.volume_bindings)


def test_snapshot_debt_blocks_complete_even_when_objects_disappear(checkpoint_case):
    from app.services import native_origin_custody as custody

    q = checkpoint_case
    test_prepare_retains_snapshot_intent_before_post_and_no_fake_binding(q)
    with q.sessions() as db:
        request = custody.prepare(db, str(q.intent.id))
        db.commit()
    with pytest.raises(ValueError, match="snapshot"):
        value = custody.verify(request, q.readers, q.backend)
        with q.sessions() as db:
            custody.apply(db, value)


@pytest.mark.parametrize("settled_history", [False, True])
def test_cancelled_snapshot_reconciliation_retains_never_allocated_debt(
    checkpoint_case, monkeypatch, settled_history
):
    from app.models.native_deployment_origin import NativeDeploymentOriginActive
    from app.services import native_origin_custody as custody
    from app.services import native_snapshot_reconciliation as reconciliation

    q = checkpoint_case
    test_prepare_retains_snapshot_intent_before_post_and_no_fake_binding(q)
    if settled_history:
        from types import SimpleNamespace

        from app.models.project import Project
        from app.services import quota_cleanup

        old = next(r for r in q.pins.project_roots if str(r.project_id) != q.project_id)
        authority = SimpleNamespace(
            root=old,
            phase="finalized",
            settlement_report_id="settlement",
            settled_at=True,
        )
        monkeypatch.setattr(quota_cleanup, "native_intents", lambda db: [authority])
        with q.sessions() as db:
            db.get(Project, str(old.project_id)).host_id = None
            db.commit()
    with q.sessions() as db:
        db.get(NativeDeploymentOriginActive, str(q.root.project_id)).cancelled = True
        db.commit()
        with pytest.raises(ValueError, match="cancellation"):
            custody.prepare(db, str(q.intent.id))
        request = reconciliation.prepare(db, str(q.intent.id))
        db.commit()
    if settled_history:
        assert old in request.pins.project_roots
        assert old.project_id not in {p.project_id for p in request.enrollment.projects}
        assert str(old.project_id) not in {
            str(p.project_id) for p in request.enrollment.projects
        }
    value = reconciliation.verify(request, q.reader, q.backend)
    with q.sessions() as db:
        row = reconciliation.apply(db, value)
        db.commit()
        receipt = custody._receipt(row)
    assert custody.unresolved_snapshots(receipt)
    assert not receipt.complete
    assert not receipt.snapshot_retirements


def test_snapshot_reconciliation_freezes_authenticated_current_cache_scope(
    checkpoint_case, monkeypatch
):
    from app.models.project import Project
    from app.services import native_origin_custody as custody
    from app.services import native_snapshot_reconciliation as reconciliation
    from tests.test_native_origin_custody import _grow_cache_authority

    q = checkpoint_case
    test_prepare_retains_snapshot_intent_before_post_and_no_fake_binding(q)
    namespace = q.reader.namespace
    monkeypatch.setattr(
        q.reader,
        "namespace",
        lambda name: (
            {"metadata": {"uid": "second-cache-uid"}}
            if name == "second-cache"
            else namespace(name)
        ),
    )
    enrollment_id, manifest = _grow_cache_authority(q)
    with q.sessions() as db:
        original = custody._current(db, q.intent.id)[1]
        request = reconciliation.prepare(db, str(q.intent.id))
        db.commit()
    assert str(request.previous_enrollment_id) == enrollment_id
    assert request.pins == manifest.pins
    value = reconciliation.verify(request, q.reader, q.backend)
    with q.sessions() as db:
        row = reconciliation.apply(db, value)
        db.commit()
        assert custody.retained(db, db.get(Project, q.project_id))[0].id == row.id
        assert (
            custody._receipt(db.get(type(row), str(request.predecessor_id))) == original
        )


@pytest.mark.parametrize("stale", [False, True])
def test_resolved_snapshot_retirement_requires_backend_and_original_objects_absent(
    snapshot_case, monkeypatch, stale
):
    from datetime import timedelta

    from app.services import native_origin_custody as custody
    from app.services import native_snapshot_reconciliation as reconciliation

    q = snapshot_case
    with q.sessions() as db:
        custody.apply(db, snapshot_verify(q))
        db.commit()
        request = reconciliation.prepare(db, str(q.intent.id))
        db.commit()
    value = reconciliation.verify(request, q.reader, q.backend)
    assert not value.snapshot_retirements
    q.checkpoint_api.values.pop(("VolumeSnapshot", "snapshot-new"))
    q.checkpoint_api.values.pop(("VolumeSnapshotContent", "content-original"))
    from app.schemas.kubevirt_storage import BackendImageState, ReadResult

    def inventory(key, **kwargs):
        at = datetime.now(UTC) - timedelta(days=1 if stale else 0)
        proof = BackendImageState(
            **key.model_dump(), active=False, trash=False, collected_at=at
        )
        return ReadResult(
            status="complete", value=proof.model_dump(), collected_at=datetime.now(UTC)
        )

    monkeypatch.setattr(q.backend, "image_inventory", inventory, raising=False)
    if stale:
        with pytest.raises(ValueError, match="stale"):
            reconciliation.verify(request, q.reader, q.backend)
        return
    value = reconciliation.verify(request, q.reader, q.backend)
    assert len(value.snapshot_retirements) == 1
    with q.sessions() as db:
        row = reconciliation.apply(db, value)
        db.commit()
        receipt = custody._receipt(row)
    assert not custody.unresolved_snapshots(receipt)
    assert receipt.bindings[0].image_key != q.image


@pytest.mark.parametrize("field", ["cancelled", "io_token", "project_state"])
def test_snapshot_readback_cannot_publish_across_cancellation_state_change(
    checkpoint_case, field
):
    from app.models.native_deployment_origin import NativeDeploymentOriginActive
    from app.models.project import Project
    from app.services import native_snapshot_reconciliation as reconciliation

    q = checkpoint_case
    test_prepare_retains_snapshot_intent_before_post_and_no_fake_binding(q)
    with q.sessions() as db:
        request = reconciliation.prepare(db, str(q.intent.id))
        db.commit()
    value = reconciliation.verify(request, q.reader, q.backend)
    with q.sessions() as db:
        pointer = db.get(NativeDeploymentOriginActive, str(q.root.project_id))
        if field == "project_state":
            db.get(Project, str(q.root.project_id)).state = "destroying"
        else:
            setattr(pointer, field, True if field == "cancelled" else str(uuid4()))
        db.commit()
    with q.sessions() as db, pytest.raises(ValueError, match="state changed"):
        reconciliation.apply(db, value)


def test_rootless_cancelled_readback_keeps_unallocated_request_debt(checkpoint_case):
    from app.models.native_deployment_origin import NativeDeploymentOriginActive
    from app.services import native_origin_custody as custody
    from app.services import native_snapshot_reconciliation as reconciliation

    q = checkpoint_case
    test_prepare_retains_snapshot_intent_before_post_and_no_fake_binding(q)
    with q.sessions() as db:
        db.get(NativeDeploymentOriginActive, str(q.root.project_id)).cancelled = True
        db.commit()
        request = reconciliation.prepare(db, str(q.intent.id))
        db.commit()
    for key in list(q.checkpoint_api.values):
        if key[0] == "TroshkaProject":
            q.checkpoint_api.values.pop(key)
    value = reconciliation.verify(request, q.reader, q.backend)
    with q.sessions() as db:
        row = reconciliation.apply(db, value)
        db.commit()
        receipt = custody._receipt(row)
    assert not receipt.complete and custody.unresolved_snapshots(receipt)


def test_pending_content_original_uid_is_retained_before_handle(snapshot_case):
    from app.services import native_origin_custody as custody

    q = snapshot_case
    q.content["status"].pop("snapshotHandle")
    first = snapshot_verify(q)
    with q.sessions() as db:
        custody.apply(db, first)
        db.commit()
    assert first.producers[0].content_identity["uid"] == "content-original-uid"
    assert not first.snapshots
    q.content["metadata"]["uid"] = "replacement"
    with pytest.raises(ValueError, match="content"):
        snapshot_verify(q)


def test_snapshot_custody_rejects_unknown_backing_child(snapshot_case, monkeypatch):
    from app.schemas.kubevirt_storage import ReadResult

    q = snapshot_case
    monkeypatch.setattr(
        q.backend,
        "image_children",
        lambda *args: ReadResult(
            status="complete",
            collected_at=datetime.now(UTC),
            value=[{"pool": "rbd", "pool_namespace": "", "id": "foreign-child"}],
        ),
    )
    with pytest.raises(ValueError, match="graph"):
        snapshot_verify(q)


def test_exact_snapshot_can_progress_from_linked_to_flattened(
    snapshot_case, monkeypatch
):
    from app.schemas.kubevirt_storage import ReadResult
    from app.services import native_origin_custody as custody

    q = snapshot_case
    source = q.report.volume_bindings[0].image_key
    linked = True

    def value(v):
        return ReadResult(status="complete", value=v, collected_at=datetime.now(UTC))

    def info(key):
        result = {"id": key.image_id}
        if linked and key == q.image:
            result["parent"] = dict(
                pool="rbd", pool_namespace="", id=source.image_id, snap_id=8
            )
        return value(result)

    def snaps(key):
        ids = [7] if key == q.image else [8] if linked and key == source else []
        return value(
            [dict(id=i, name="fixture", namespace={"type": "user"}) for i in ids]
        )

    monkeypatch.setattr(q.backend, "image_info", info)
    monkeypatch.setattr(q.backend, "image_snapshots", snaps)
    monkeypatch.setattr(
        q.backend,
        "image_children",
        lambda key, *args: value(
            [dict(pool="rbd", pool_namespace="", id=q.image.image_id)]
            if linked and key == source
            else []
        ),
    )
    first = snapshot_verify(q)
    with q.sessions() as db:
        custody.apply(db, first)
        db.commit()
    linked = False
    flat = snapshot_verify(q)
    assert first.snapshots[0].parent is not None and flat.snapshots[0].parent is None
    assert first.snapshots[0].image == flat.snapshots[0].image
    assert first.snapshots[0].snapshot == flat.snapshots[0].snapshot


@pytest.mark.parametrize(
    "change", ["cancelled", "io_token", "project_state", "predecessor"]
)
def test_postgres_snapshot_publication_rechecks_state_after_lock_wait(
    checkpoint_case, change
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, get_ident

    from sqlalchemy import event

    from app.models.native_deployment_origin import NativeDeploymentOriginActive
    from app.models.project import Project
    from app.services import native_snapshot_reconciliation as reconciliation
    from app.services.quota_locks import lock_project
    from tests.test_native_origin_custody import _postgres

    q = checkpoint_case
    _postgres(q)
    test_prepare_retains_snapshot_intent_before_post_and_no_fake_binding(q)
    with q.sessions() as db:
        request = reconciliation.prepare(db, str(q.intent.id))
        db.commit()
    value = reconciliation.verify(request, q.reader, q.backend)
    waiting = Event()
    engine = q.sessions.kw["bind"]

    def contender():
        thread = get_ident()

        def before(_conn, _cursor, statement, _params, _context, _many):
            table = "native_reporting_targets" if change == "predecessor" else "users"
            if (
                get_ident() == thread
                and "FOR UPDATE" in statement
                and table in statement
            ):
                waiting.set()

        event.listen(engine, "before_cursor_execute", before)
        try:
            with q.sessions() as db:
                try:
                    reconciliation.apply(db, value)
                    db.commit()
                except ValueError as error:
                    return error
        finally:
            event.remove(engine, "before_cursor_execute", before)

    with ThreadPoolExecutor(max_workers=1) as pool, q.sessions() as holder:
        if change == "predecessor":
            from app.services import native_origin_custody as custody

            custody._publication_locks(holder, request)
        else:
            lock_project(holder, q.project_id, require_organization=False)
        future = pool.submit(contender)
        assert waiting.wait(5)
        pointer = holder.get(NativeDeploymentOriginActive, str(q.root.project_id))
        if change == "project_state":
            holder.get(Project, str(q.root.project_id)).state = "destroying"
        elif change == "predecessor":
            reconciliation.apply(holder, value)
        else:
            setattr(pointer, change, True if change == "cancelled" else str(uuid4()))
        holder.commit()
        error = future.result(timeout=8)
    assert isinstance(error, ValueError), error
    assert "changed" in str(error)
