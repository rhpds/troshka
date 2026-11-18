"""Local supported-layout integration across signed producer and custody seams."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.kubevirt_storage import (
    BackendImageState,
    CollectionLimits,
    ReadResult,
    SnapshotKey,
)
from app.services import native_origin_checkpoints as checkpoints
from app.services import native_origin_custody as custody
from tests import test_native_snapshot_custody as support

cleanup_case = support.cleanup_case
deploy_case = support.deploy_case
isolated_quota_database = support.isolated_quota_database
native_ledger = support.native_ledger
online_case = support.online_case
resume_case = support.resume_case
checkpoint_case = support.checkpoint_case


def result(value):
    return ReadResult(status="complete", value=value, collected_at=datetime.now(UTC))


class SnapshotHarness:
    def __init__(self, q, monkeypatch, operation):
        import importlib

        checkpoints.protocol()
        self.power = importlib.import_module("helpers.managed_power")
        self.guard = checkpoints.protocol()
        self.workflow = importlib.import_module("helpers.native_snapshot_workflow")
        self.q, self.api = q, q.checkpoint_api
        self.root = self.api.rows("TroshkaProject", q.root.namespace)[0]
        self.root["metadata"]["annotations"][
            "troshka.redhat.com/" + operation + "-request"
        ] = "operation-1"
        self.api.values["TroshkaProject", self.root["metadata"]["name"]] = self.root
        self.source = q.reader.pvcs(q.root.namespace)[0]
        self.source_binding = q.report.volume_bindings[0]
        self.extra_pvs, self.bindings, self.created, self.deleted = [], [], [], []
        self.image = self.source_binding.image_key.model_copy(
            update={"image_id": "snapshot-private"}
        )
        self.snapshot = SnapshotKey(image_key=self.image, snapshot_id="7")
        self.absent = set()
        self._readers(monkeypatch)
        self._backend(monkeypatch)
        monkeypatch.setattr(self.api, "request", self.delete)
        monkeypatch.setattr(
            custody, "verify_retirement_configured", self.retire_binding
        )
        from app.services import native_binding_retirement

        monkeypatch.setattr(native_binding_retirement, "datetime", datetime)
        monkeypatch.setattr(custody, "datetime", datetime)

    def _readers(self, monkeypatch):
        q, api = self.q, self.api
        pvcs, pvs, get, rows, resolve = (
            q.reader.pvcs,
            q.reader.pvs,
            q.reader.get,
            q.reader.rows,
            q.reader.resolve_csi,
        )
        monkeypatch.setattr(
            q.reader,
            "pvcs",
            lambda ns: [
                p
                for p in pvcs(ns)
                if not p["metadata"]["name"].startswith("operation-")
            ]
            + [
                deepcopy(v)
                for (kind, name), v in api.values.items()
                if kind == "PersistentVolumeClaim" and name.startswith("operation-")
            ],
        )
        monkeypatch.setattr(q.reader, "pvs", lambda: pvs() + deepcopy(self.extra_pvs))
        monkeypatch.setattr(
            q.reader,
            "get",
            lambda kind, ns, name: api.get(kind, ns, name)
            if kind in {"VolumeSnapshotClass", "VolumeSnapshotContent"}
            else get(kind, ns, name),
        )
        monkeypatch.setattr(
            q.reader,
            "rows",
            lambda kind, ns: api.rows(kind, ns)
            if kind in {"VolumeSnapshotClass", "VolumeSnapshotContent"}
            else rows(kind, ns),
        )
        monkeypatch.setattr(
            q.reader,
            "snapshots",
            lambda ns: api.rows("VolumeSnapshot", ns),
            raising=False,
        )
        monkeypatch.setattr(
            q.reader,
            "snapshot_contents",
            lambda: api.rows("VolumeSnapshotContent", None),
            raising=False,
        )
        monkeypatch.setattr(
            q.reader,
            "resolve_csi",
            lambda driver, handle, **kw: result(
                next(
                    b.image_key for b in self.bindings if b.volume_handle == handle
                ).model_dump()
            )
            if any(b.volume_handle == handle for b in self.bindings)
            else resolve(driver, handle, **kw),
        )

    def _backend(self, monkeypatch):
        from tests.test_native_snapshot_journal import PROFILE

        q = self.q
        q.backend.snapshot_profiles = [
            {
                **PROFILE,
                "driver": self.source_binding.csi_driver,
                "snapshot_class": "fixture-class",
            }
        ]
        monkeypatch.setattr(
            q.backend,
            "resolve_csi_snapshot",
            lambda *a, **kw: result(
                dict(
                    image=self.image.model_dump(),
                    snapshot=self.snapshot.model_dump(),
                    handle="snapshot-handle",
                )
            ),
            raising=False,
        )
        monkeypatch.setattr(
            q.backend, "image_info", lambda key: result({"id": key.image_id})
        )
        monkeypatch.setattr(
            q.backend,
            "image_snapshots",
            lambda key: result(
                [{"id": 7, "name": "csi-snap-fixture", "namespace": {"type": "user"}}]
                if key == self.image
                else []
            ),
        )
        monkeypatch.setattr(
            q.backend, "image_children", lambda *a: result([]), raising=False
        )
        monkeypatch.setattr(
            q.backend,
            "pool_identity",
            lambda: result([{"poolnum": self.image.pool_id, "poolname": "rbd"}]),
        )
        monkeypatch.setattr(
            q.backend,
            "image_inventory",
            lambda key, **kw: result(
                BackendImageState(
                    **key.model_dump(),
                    active=key not in self.absent,
                    trash=False,
                    collected_at=datetime.now(UTC),
                )
            ),
            raising=False,
        )

    def create(self, body):
        method = {
            "VolumeSnapshot": "create_namespaced_custom_object",
            "PersistentVolumeClaim": "create_namespaced_persistent_volume_claim",
            "Job": "create_namespaced_job",
            "Pod": "create_namespaced_pod",
        }[body["kind"]]
        raw = SimpleNamespace(**{method: lambda **kw: self.post(kw["body"])})
        kwargs = dict(namespace=self.q.root.namespace, body=body)
        if body["kind"] == "VolumeSnapshot":
            kwargs.update(
                group="snapshot.storage.k8s.io", version="v1", plural="volumesnapshots"
            )
        self.turn(lambda: getattr(self.guard.guard_client(raw), method)(**kwargs))

    def turn(self, action):
        for _ in range(12):
            token = self.power._context.set(
                (self.root, self.q.root.namespace, self.api)
            )
            held = self.guard._held.set(None)
            try:
                return action()
            except self.guard.CheckpointHold:
                self.acknowledge()
            finally:
                self.guard._held.reset(held)
                self.power._context.reset(token)
        pytest.fail("workflow failed to progress through bounded signed checkpoints")

    def acknowledge(self):
        import json

        root = self.api.rows("TroshkaProject", self.q.root.namespace)[0]
        request = json.loads(root["metadata"]["annotations"][checkpoints.REQUEST])
        ack = checkpoints.service_request(
            self.q.sessions, self.api, str(self.q.intent.id), request
        )
        root = self.api.rows("TroshkaProject", self.q.root.namespace)[0]
        self.guard.publish_ack(self.api, root, request, ack)

    def post(self, body):
        value = deepcopy(body)
        value["metadata"].update(
            uid=str(uuid4()), resourceVersion="1", namespace=self.q.root.namespace
        )
        key = value["kind"], value["metadata"]["name"]
        assert key not in self.api.values
        self.api.values[key] = value
        self.created.append(key)
        return deepcopy(value)

    def bind(self, name):
        pvc = self.api.values["PersistentVolumeClaim", name]
        pv = deepcopy(self.q.reader.pvs()[0])
        pv["metadata"].update(name=name + "-pv", uid=str(uuid4()))
        pv["spec"]["claimRef"] = dict(
            name=name, namespace=self.q.root.namespace, uid=pvc["metadata"]["uid"]
        )
        handle = name + "-handle"
        pv["spec"]["csi"]["volumeHandle"] = handle
        pvc["spec"]["volumeName"] = pv["metadata"]["name"]
        pvc["status"] = {"phase": "Bound"}
        binding = self.source_binding.model_copy(
            update=dict(
                pvc_uid=pvc["metadata"]["uid"],
                pv_uid=pv["metadata"]["uid"],
                volume_handle=handle,
                image_key=self.image.model_copy(update={"image_id": name + "-image"}),
                owner_reference_uids=[self.q.root.project_cr_uid],
            )
        )
        self.extra_pvs.append(pv)
        self.bindings.append(binding)

    def ready_snapshot(self, name):
        snapshot = self.api.values["VolumeSnapshot", name]
        snapshot["status"] = dict(
            readyToUse=True, boundVolumeSnapshotContentName="content"
        )
        self.api.values["VolumeSnapshotContent", "content"] = dict(
            kind="VolumeSnapshotContent",
            metadata=dict(name="content", uid="content-uid"),
            spec=dict(
                driver=self.source_binding.csi_driver,
                deletionPolicy="Delete",
                volumeSnapshotClassName="fixture-class",
                source={"volumeHandle": self.source_binding.volume_handle},
                volumeSnapshotRef=dict(
                    uid=snapshot["metadata"]["uid"],
                    name=name,
                    namespace=self.q.root.namespace,
                ),
            ),
            status=dict(snapshotHandle="snapshot-handle", readyToUse=True),
        )
        self.api.values["VolumeSnapshotClass", "fixture-class"] = dict(
            kind="VolumeSnapshotClass",
            metadata=dict(name="fixture-class", uid="class-uid"),
            driver=self.source_binding.csi_driver,
            deletionPolicy="Delete",
        )

    def delete(self, method, kind, namespace, name, body):
        assert method == "DELETE"
        live = self.api.values[kind, name]
        assert body["preconditions"] == {
            k: live["metadata"][k] for k in ("uid", "resourceVersion")
        }
        self.deleted.append((kind, name))
        self.api.values.pop((kind, name))
        if kind == "PersistentVolumeClaim":
            self.extra_pvs = [
                p
                for p in self.extra_pvs
                if p["spec"]["claimRef"]["uid"] != live["metadata"]["uid"]
            ]
            self.absent.add(
                next(
                    b.image_key
                    for b in self.bindings
                    if b.pvc_uid == live["metadata"]["uid"]
                )
            )
        if kind == "VolumeSnapshot":
            self.api.values.pop(("VolumeSnapshotContent", "content"))
            self.absent.add(self.image)

    def retire_binding(self, request):
        from app.services import native_binding_retirement

        return native_binding_retirement.verify(request, self.q.reader, self.q.backend)


def test_original_uid_retry_and_ambiguous_lost_post_remain_distinct(
    checkpoint_case, monkeypatch
):
    from unittest.mock import Mock

    h = SnapshotHarness(checkpoint_case, monkeypatch, "capture")
    from helpers.patterns import build_volume_snapshot

    body = build_volume_snapshot(
        "operation-snapshot", h.q.root.namespace, h.source["metadata"]["name"]
    )
    body["spec"]["volumeSnapshotClassName"] = "fixture-class"
    h.create(body)
    h.create(body)
    assert len(h.created) == 1
    root = h.api.values["TroshkaProject", h.root["metadata"]["name"]]
    root["status"]["originProducerObjects"] = {}
    raw = Mock()
    token = h.power._context.set((h.root, h.q.root.namespace, h.api))
    held = h.guard._held.set(None)
    try:
        with pytest.raises(h.guard.CheckpointHold, match="original_uid_unresolved"):
            h.guard.guard_client(raw).create_namespaced_custom_object(
                namespace=h.q.root.namespace,
                group="snapshot.storage.k8s.io",
                version="v1",
                plural="volumesnapshots",
                body=body,
            )
    finally:
        h.guard._held.reset(held)
        h.power._context.reset(token)
    assert not raw.method_calls


def test_snapshot_reconciliation_survives_completed_controller_retries(
    checkpoint_case, monkeypatch
):
    from helpers.patterns import build_volume_snapshot

    from app.services import native_snapshot_reconciliation as reconciliation

    h = SnapshotHarness(checkpoint_case, monkeypatch, "capture")
    body = build_volume_snapshot(
        "operation-snapshot", h.q.root.namespace, h.source["metadata"]["name"]
    )
    body["spec"]["volumeSnapshotClassName"] = "fixture-class"
    h.create(body)
    h.ready_snapshot("operation-snapshot")
    with h.q.sessions() as db:
        request = reconciliation.prepare(db, str(h.q.intent.id))
        db.commit()
    original = h.q.reader.projects
    reads = 0

    def projects(namespace):
        nonlocal reads
        reads += 1
        rows = deepcopy(original(namespace))
        for row in rows:
            row["metadata"]["resourceVersion"] = str(reads)
            row.setdefault("status", {})["originExecutions"] = {
                "operator": {
                    "state": "complete",
                    "outcome": "failed",
                    "operator": {"uid": "operator"},
                    "executionId": str(reads),
                }
            }
        return rows

    monkeypatch.setattr(h.q.reader, "projects", projects)
    value = reconciliation.verify(request, h.q.reader, h.q.backend)
    with h.q.sessions() as db:
        row = reconciliation.apply(db, value)
        db.commit()
        assert len(custody._receipt(row).snapshots) == 1
    assert reads >= 2
    assert not h.deleted


def test_resolved_snapshot_only_failure_can_cleanup_through_signed_protocol(
    checkpoint_case, monkeypatch
):
    from app.services import native_snapshot_reconciliation as reconciliation

    h = SnapshotHarness(checkpoint_case, monkeypatch, "capture")
    from helpers.patterns import build_volume_snapshot

    body = build_volume_snapshot(
        "operation-snapshot", h.q.root.namespace, h.source["metadata"]["name"]
    )
    body["spec"]["volumeSnapshotClassName"] = "fixture-class"
    h.create(body)
    h.ready_snapshot("operation-snapshot")
    resources = h.turn(h.workflow.recorded_resources)
    assert resources == [{"snapName": "operation-snapshot"}]
    h.turn(lambda: h.workflow.begin("capture", resources))
    h.turn(lambda: h.workflow.cleanup(resources))
    with h.q.sessions() as db:
        request = reconciliation.prepare(db, str(h.q.intent.id))
        db.commit()
    value = reconciliation.verify(request, h.q.reader, h.q.backend)
    with h.q.sessions() as db:
        row = reconciliation.apply(db, value)
        db.commit()
        assert not custody.unresolved_snapshots(custody._receipt(row))
    assert h.deleted == [("VolumeSnapshot", "operation-snapshot")]
    assert all(i.key not in h.absent for i in h.q.report.images)


@pytest.mark.parametrize("backend_absent", [False, True])
def test_actual_observer_retires_previous_snapshot_before_servicing_next_producer(
    checkpoint_case, monkeypatch, backend_absent
):
    from app.services import native_snapshot_reconciliation as reconciliation

    h = SnapshotHarness(checkpoint_case, monkeypatch, "capture")
    from helpers.patterns import build_volume_snapshot

    def snapshot(name):
        body = build_volume_snapshot(
            name, h.q.root.namespace, h.source["metadata"]["name"]
        )
        body["spec"]["volumeSnapshotClassName"] = "fixture-class"
        return body

    h.create(snapshot("operation-first"))
    h.ready_snapshot("operation-first")
    h.turn(lambda: h.workflow.cleanup([{"snapName": "operation-first"}]))
    with h.q.sessions() as db:
        assert custody.unresolved_snapshots(custody._current(db, h.q.intent.id)[1])
    image_info = h.q.backend.image_info
    monkeypatch.setattr(
        h.q.backend,
        "image_info",
        lambda key: ReadResult(status="unsupported", collected_at=datetime.now(UTC))
        if key in h.absent
        else image_info(key),
    )
    monkeypatch.setattr(
        custody,
        "verify_configured",
        lambda request: reconciliation.verify(request, h.q.reader, h.q.backend)
        if request.mode == "snapshot-reconcile"
        else custody.verify(request, h.q.readers, h.q.backend),
    )
    monkeypatch.setattr(
        h,
        "acknowledge",
        lambda: checkpoints._observe_live(h.api, h.q.sessions, h.q.intent),
    )
    if not backend_absent:
        monkeypatch.setattr(
            h.q.backend,
            "image_inventory",
            lambda key, **kw: result(
                BackendImageState(
                    **key.model_dump(),
                    active=True,
                    trash=False,
                    collected_at=datetime.now(UTC),
                )
            ),
        )
        with pytest.raises(ValueError, match="active or in trash"):
            h.create(snapshot("operation-next"))
        assert h.created == [("VolumeSnapshot", "operation-first")]
        return
    h.create(snapshot("operation-next"))
    with h.q.sessions() as db:
        receipt = custody._current(db, h.q.intent.id)[1]
        assert len(receipt.snapshot_retirements) == 1
        assert len(receipt.producers) == 2
    assert h.created == [
        ("VolumeSnapshot", "operation-first"),
        ("VolumeSnapshot", "operation-next"),
    ]
    assert h.deleted == [("VolumeSnapshot", "operation-first")]
    assert all(i.key not in h.absent for i in h.q.report.images)


@pytest.mark.parametrize("operation", ["capture", "file-pull"])
def test_signed_snapshot_operation_reaches_collection_and_exact_retirement(
    checkpoint_case, monkeypatch, operation
):
    from app.services import native_snapshot_reconciliation as reconciliation
    from app.services.kubevirt_storage_collector import collect_report
    from app.services.kubevirt_storage_reports import import_report, report_inventory

    h = SnapshotHarness(checkpoint_case, monkeypatch, operation)
    q = h.q
    monkeypatch.setattr(
        custody,
        "verify_configured",
        lambda request: reconciliation.verify(request, q.reader, q.backend)
        if request.mode == "snapshot-reconcile"
        else custody.verify(request, q.readers, q.backend),
    )
    monkeypatch.setattr(
        h, "acknowledge", lambda: checkpoints._observe_live(h.api, q.sessions, q.intent)
    )
    from helpers.filepull import build_filepull_pod
    from helpers.patterns import (
        build_export_job,
        build_scratch_pvc,
        build_temp_pvc_from_snapshot,
        build_volume_snapshot,
    )

    snapshot = build_volume_snapshot(
        "operation-snapshot", q.root.namespace, h.source["metadata"]["name"]
    )
    snapshot["spec"]["volumeSnapshotClassName"] = "fixture-class"
    h.create(snapshot)
    h.ready_snapshot("operation-snapshot")
    restore = build_temp_pvc_from_snapshot(
        "operation-restore", q.root.namespace, "operation-snapshot", 1
    )
    h.create(restore)
    h.bind("operation-restore")
    kind = "Job" if operation == "capture" else "Pod"
    if kind == "Job":
        h.create(build_scratch_pvc("operation-scratch", q.root.namespace, 1))
        h.bind("operation-scratch")
        helper = build_export_job(
            "operation-helper",
            q.root.namespace,
            "operation-restore",
            "disk.qcow2",
            {"endpoint": "https://fixture.invalid", "bucket": "fixture"},
            1,
        )
        helper.pop("_deadline", None)
        helper.pop("_scratchPvcName", None)
        helper["spec"]["template"]["spec"]["volumes"][1]["persistentVolumeClaim"][
            "claimName"
        ] = "operation-scratch"
        helper["spec"]["template"]["spec"]["containers"][0].setdefault(
            "env", []
        ).append({"name": "SECRET", "value": "never-persist-this-credential"})
    else:
        helper = build_filepull_pod(
            "operation-helper", q.root.namespace, "operation-restore", "/fixture"
        )
    helper_name = helper["metadata"]["name"]
    h.create(helper)
    with q.sessions() as db:
        receipt = custody._current(db, q.intent.id)[1]
        assert "never-persist-this-credential" not in str(receipt.model_dump())
    assert receipt.snapshots and receipt.snapshot_restores
    q.backend.capability = SimpleNamespace(
        command_version="fixture",
        merge_snapshots_validated=True,
        validation_reference="fixture",
        semantics_version="kubevirt-rbd-allocated-v1",
    )
    q.backend.command_version = lambda: result("fixture")
    q.backend.image_usage = lambda key: result(
        {"images": [{"id": key.image_id, "used_size": 123, "provisioned_size": 1000}]}
    )
    q.backend.trash_inventory = lambda key: result([])
    inventory = report_inventory(q.report).model_copy(
        update={"volume_bindings": q.report.volume_bindings + h.bindings}
    )
    q.backend.recheck_inventory = lambda: inventory
    report = collect_report(
        inventory,
        q.backend,
        limits=CollectionLimits(),
        clock=lambda: datetime.now(UTC),
        snapshot_facts=[receipt],
    )
    assert all(p.coverage == "complete" for p in report.projects), [
        p.blockers for p in report.projects
    ]
    assert h.image in {i.key for i in report.images}
    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeEnrollment,
        NativeStorageSnapshot,
    )

    with q.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        enrolled = db.get(NativeEnrollment, domain.enrollment_id)
        imported = import_report(
            db,
            report,
            enrollment_id=enrolled.id,
            expected_enrollment_digest=enrolled.digest,
            now=datetime.now(UTC),
        )
        db.commit()
        assert db.get(NativeStorageSnapshot, imported).accepted
    resources = [
        dict(
            snapName="operation-snapshot",
            tempPvcName="operation-restore",
            **(
                {"jobName": helper_name, "scratchPvcName": "operation-scratch"}
                if kind == "Job"
                else {"podName": helper_name}
            ),
        )
    ]
    h.turn(lambda: h.workflow.begin(operation, resources))
    h.turn(lambda: h.workflow.cleanup(resources))
    expected = [(kind, helper_name), ("PersistentVolumeClaim", "operation-restore")]
    if kind == "Job":
        expected.append(("PersistentVolumeClaim", "operation-scratch"))
    assert h.deleted == expected + [("VolumeSnapshot", "operation-snapshot")]
    assert len(h.created) == (4 if kind == "Job" else 3)
    for binding in h.bindings:
        with q.sessions() as db:
            prepared = custody.prepare_retirement(db, str(q.intent.id), binding.pvc_uid)
            db.commit()
        verified = h.retire_binding(prepared)
        with q.sessions() as db:
            custody.apply_retirement(db, verified)
            db.commit()
    with q.sessions() as db:
        request = reconciliation.prepare(db, str(q.intent.id))
        db.commit()
    value = reconciliation.verify(request, q.reader, q.backend)
    with q.sessions() as db:
        row = reconciliation.apply(db, value)
        db.commit()
        assert not custody.unresolved_snapshots(custody._receipt(row))
    assert h.source_binding.image_key not in h.absent
    assert all(b.image_key not in h.absent for b in q.report.volume_bindings)
    assert all(i.key not in h.absent for i in q.report.images)
    root = h.api.values["TroshkaProject", h.root["metadata"]["name"]]
    root["metadata"]["annotations"].pop("troshka.redhat.com/" + operation + "-request")
    with q.sessions() as db:
        complete_request = custody.prepare(db, str(q.intent.id))
        db.commit()
    complete = custody.verify(complete_request, q.readers, q.backend)
    with q.sessions() as db:
        row = custody.apply(db, complete)
        db.commit()
        receipt = custody._receipt(row)
        assert receipt.complete
        from app.services.native_clone_graph import binding_identity

        assert [binding_identity(b) for b in custody.active_bindings(db, receipt)] == [
            binding_identity(h.source_binding)
        ]
    from app.services.native_binding_retirement_collection import (
        protected_inputs,
        scan_evidence,
    )

    q.backend.scan_started_at = datetime.now(UTC)
    inventory = report_inventory(q.report)
    q.backend.recheck_inventory = lambda: inventory
    with q.sessions() as db:
        domain, manifest = custody._domain(db, str(q.pins.domain_id))
        selected = protected_inputs(db, manifest)
        evidence = scan_evidence(
            selected, inventory, q.reader, q.backend, manifest.pins.project_roots
        )
        inventory = inventory.model_copy(update={"binding_retirements": evidence})
        report = collect_report(
            inventory,
            q.backend,
            limits=CollectionLimits(),
            clock=lambda: datetime.now(UTC),
            binding_retirements=[e.observation for e in evidence],
            snapshot_facts=[receipt],
        )
        assert all(p.coverage == "complete" for p in report.projects), [
            p.blockers for p in report.projects
        ]
        enrolled = db.get(NativeEnrollment, domain.enrollment_id)
        imported = import_report(
            db,
            report,
            enrollment_id=enrolled.id,
            expected_enrollment_digest=enrolled.digest,
            now=datetime.now(UTC),
        )
        db.commit()
        assert db.get(NativeStorageSnapshot, imported).accepted
    assert next(i for i in report.images if i.key == h.image).absence.excludes(h.image)
