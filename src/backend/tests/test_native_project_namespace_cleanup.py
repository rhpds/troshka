"""Actual retained administrator authority and namespace adapter, no live writes."""

import json
from copy import deepcopy

import pytest

from app.models.project import Project
from app.models.quota import QuotaReservation
from app.schemas.quota import AccountingUnavailable
from app.services import native_project_namespace_cleanup as cleanup
from app.services.kubevirt_storage_enrollment import digest
from app.services.native_namespace_client import ACK, ANNOTATION, OWN_KINDS
from tests import test_native_online_enrollment as online
from tests import test_native_origin_custody as origin_support
from tests import test_native_snapshot_custody as snapshot_support

cleanup_case = online.cleanup_case
deploy_case = online.deploy_case
isolated_quota_database = online.isolated_quota_database
native_ledger = online.native_ledger
online_case = online.online_case
resume_case = online.resume_case
checkpoint_case = snapshot_support.checkpoint_case
snapshot_case = snapshot_support.snapshot_case
custody_case = origin_support.custody_case


@pytest.fixture
def namespace_case(online_case, monkeypatch):
    online.apply_receipt(online_case)
    return namespace_adapter(online_case, monkeypatch)


@pytest.fixture
def origin_namespace_case(custody_case, monkeypatch):
    from app.services import native_origin_custody as custody
    from app.services import quota_cleanup

    q = namespace_adapter(custody_case, monkeypatch)
    monkeypatch.setattr(
        custody,
        "verify_configured",
        lambda request: custody.verify(request, q.readers, q.backend),
    )
    monkeypatch.setattr(custody, "domain_for", lambda db, intent: str(q.pins.domain_id))
    q.request_service = quota_cleanup.request_native
    return q


def namespace_adapter(q, monkeypatch):
    from app.core import redis
    from app.services import quota_operations
    from app.services.native_managed_power import helpers
    from tests.test_native_managed_resume import FixedDatetime

    q.project_id = q.project_ids[0]
    q.root = q.report.project_roots[0]
    q.namespace = q.root.namespace
    q.calls, q.enqueued = [], []
    q.cleanup_mode = "undeploy"
    q.request_service = cleanup.request
    q.stuck_vmi = q.ambiguous_delete = False
    base = q.apis[q.project_id]
    routes = helpers().KINDS | helpers().CLEANUP_KINDS

    class API:
        def new_observation(self):
            return self

        def get(self, kind, namespace, name):
            return deepcopy(base.values.get((kind, name)))

        def rows(self, kind, namespace):
            return [deepcopy(v) for (k, _), v in base.values.items() if k == kind]

        def operator_profile(self, namespace, cluster_uid):
            return {"version": "fixture-qualified"}, {
                "namespace": namespace,
                "name": "operator",
                "uid": "op",
                "containerId": "ctr",
            }

        def qualify_operator(self, pins, identity):
            assert pins == {"version": "fixture-qualified"}
            assert identity["uid"] == "op"

        def patch_namespace(self, namespace, uid, rv, annotations):
            assert base.values["Namespace", namespace]["metadata"]["uid"] == uid
            meta = base.values["Namespace", namespace]["metadata"]
            meta["annotations"] = deepcopy(annotations)
            marker = json.loads(annotations[ANNOTATION])
            meta["annotations"][ACK] = json.dumps(
                dict(
                    markerDigest=digest(marker),
                    namespaceUid=uid,
                    operator={**marker["operator"], "incarnation": "process"},
                )
            )

        def delete(self, kind, namespace, name, uid, rv, *, orphan=False):
            value = base.values[kind, name]
            assert value["metadata"]["uid"] == uid
            if kind in OWN_KINDS:
                assert orphan
                assert not self.rows("VirtualMachine", namespace)
                assert not self.rows("VirtualMachineInstance", namespace)
            q.calls.append((kind, name, orphan))
            base.values.pop((kind, name))
            if kind == "VirtualMachine" and not q.stuck_vmi:
                for key in list(base.values):
                    if key[0] == "VirtualMachineInstance":
                        base.values.pop(key)

        def delete_namespace(self, name, uid, rv):
            assert not any(
                self.rows(kind, name)
                for kind in ("VirtualMachine", "VirtualMachineInstance")
            )
            with q.sessions() as db:
                assert (
                    cleanup.states(db, q.project_id)[0].phase
                    == "namespace_delete_requested"
                )
                remaining = [v for kind in OWN_KINDS for v in self.rows(kind, name)]
                if remaining:
                    proofs = (
                        db.query(cleanup.Evidence)
                        .filter_by(cleanup_id=q.cleanup_id)
                        .all()
                    )
                    assert any(
                        p.phase.startswith("cr_external_complete:") for p in proofs
                    )
                    assert all(
                        v["metadata"]["finalizers"] == ["orphan"] for v in remaining
                    )
            q.calls.append(("Namespace", name, False))
            base.values.pop(("Namespace", name))
            if q.ambiguous_delete:
                raise TimeoutError("lost DELETE response")

        def path(self, path, query=()):
            groups = sorted(
                {version for version, _ in routes.values() if "/" in version}
            )
            if path == "/api":
                return dict(kind="APIVersions", versions=["v1"])
            if path == "/apis":
                return dict(
                    kind="APIGroupList",
                    groups=[
                        dict(
                            name=v.split("/")[0],
                            versions=[dict(version=v.split("/")[1], groupVersion=v)],
                        )
                        for v in groups
                    ],
                )
            if "/namespaces/" in path:
                plural = path.rsplit("/", 1)[1]
                kind = next(k for k, (_, r) in routes.items() if r == plural)
                items = self.rows(kind, q.namespace)
                return dict(metadata=dict(resourceVersion="1"), items=items)
            version = path.removeprefix("/apis/").removeprefix("/api/")
            resources = [
                dict(name=r, kind=k, namespaced=True, verbs=["list"])
                for k, (v, r) in routes.items()
                if v == version and k not in {"Namespace", "PersistentVolume"}
            ]
            return dict(
                kind="APIResourceList", groupVersion=version, resources=resources
            )

    q.api = API()
    q.values = base.values
    q.settlement_collections = []
    monkeypatch.setattr(redis, "enqueue_job", lambda *a, **k: q.enqueued.append(a))
    monkeypatch.setattr(
        cleanup,
        "request_settlement_collection",
        lambda work: q.settlement_collections.append(str(work.id)) or True,
    )
    monkeypatch.setattr(quota_operations, "now", lambda: FixedDatetime.current)
    # Map-based release/reclaim: unit tests do not talk to rook toolbox.
    from app.services import native_orphan_reclaim, native_project_retirement

    monkeypatch.setattr(
        native_orphan_reclaim, "attempt", lambda db, work, core_api=None: []
    )

    def reclaim_done(db, cleanup_id, **kwargs):
        _, work = cleanup._locked(db, cleanup_id)
        return native_orphan_reclaim.finalize_if_clear(db, work, [])

    monkeypatch.setattr(native_project_retirement, "reclaim_orphans", reclaim_done)
    real_settle = native_project_retirement.settle

    def settle_with_api(db, cleanup_id, api=None, **kwargs):
        return real_settle(db, cleanup_id, api=api or q.api, **kwargs)

    monkeypatch.setattr(native_project_retirement, "settle", settle_with_api)

    def request():
        with q.sessions() as db:
            result = q.request_service(
                db, db.get(Project, q.project_id), q.cleanup_mode
            )
        q.cleanup_id = result["cleanup_id"]
        return result

    def advance():
        with q.sessions() as db:
            return cleanup.advance(db, q.api, q.cleanup_id)

    q.request, q.advance = request, advance
    return q


def pending_orphan(q, monkeypatch):
    """Persist the real pre-delete inventory and request, then finish our duties."""
    owned = [v for (kind, _), v in q.values.items() if kind in OWN_KINDS]
    for own in owned:
        own["metadata"]["finalizers"] = ["troshka.redhat.com/finalizer"]
    delete = q.api.delete

    def terminating(kind, namespace, name, uid, rv, **kwargs):
        if kind not in OWN_KINDS:
            return delete(kind, namespace, name, uid, rv, **kwargs)
        q.values[kind, name]["metadata"]["deletionTimestamp"] = "2026-11-17T12:00:00Z"

    monkeypatch.setattr(q.api, "delete", terminating)
    q.request()
    q.advance()
    assert q.advance().phase == "vm_delete_requested"
    for own in owned:
        own["metadata"]["finalizers"] = ["orphan"]
    return owned[0]


def test_pending_orphan_recovers_with_immutable_finalizer_completion(
    namespace_case, monkeypatch
):
    q = namespace_case
    own = pending_orphan(q, monkeypatch)
    assert q.advance().phase == "namespace_delete_requested"
    with q.sessions() as db:
        rows = db.query(cleanup.Evidence).filter_by(cleanup_id=q.cleanup_id).all()
        proof = next(p for p in rows if p.phase.startswith("cr_external_complete:"))
        observed = next(v for v in proof.payload if v["uid"] == own["metadata"]["uid"])
        assert observed["uid"] == own["metadata"]["uid"]
        assert observed["finalizers"] == ["orphan"]
        assert observed["deletion_timestamp"] == "2026-11-17T12:00:00Z"
        inventory = next(p for p in rows if p.phase == observed["registration_phase"])
        assert inventory.digest == observed["registration_digest"]
        assert any(
            v["uid"] == observed["uid"]
            and "troshka.redhat.com/finalizer" in v["finalizers"]
            for v in inventory.payload
        )
        request = next(p for p in rows if p.phase == observed["request_phase"])
        assert request.digest == observed["request_digest"]
        assert any(v["uid"] == observed["uid"] for v in request.payload)
        namespace_request = next(
            p for p in rows if p.phase.startswith("namespace_request:")
        )
        assert namespace_request.payload["cr_completion_digest"] == proof.digest


@pytest.mark.parametrize(
    "change",
    [
        "uid",
        "unpersisted",
        "custom",
        "troshka",
        "empty",
        "missing_timestamp",
        "invalid_timestamp",
        "missing_proof",
        "corrupt_proof",
        "wrong_proof_uid",
    ],
)
def test_pending_orphan_holds_without_exact_completion_proof(
    namespace_case, monkeypatch, change
):
    q = namespace_case
    own = pending_orphan(q, monkeypatch)
    if change == "uid":
        own["metadata"]["uid"] = "replacement"
    elif change == "unpersisted":
        with q.sessions() as db:
            work = cleanup.states(db, q.project_id)[0]
            cleanup._save(db, work, cr_targets=[])
            db.commit()
    elif change in {"custom", "troshka", "empty"}:
        own["metadata"]["finalizers"] = {
            "custom": ["orphan", "other/finalizer"],
            "troshka": ["orphan", "troshka.redhat.com/finalizer"],
            "empty": [],
        }[change]
    elif change.endswith("timestamp"):
        own["metadata"]["deletionTimestamp"] = (
            None if change == "missing_timestamp" else "unknown"
        )
        monkeypatch.setattr(q.api, "delete", lambda *args, **kwargs: None)
    else:
        # Change test evidence through SQL: ORM mutation is forbidden in product.
        from sqlalchemy import update

        with q.sessions() as db:
            for row in (
                db.query(cleanup.Evidence).filter_by(cleanup_id=q.cleanup_id).all()
            ):
                if not row.phase.startswith("inventory:"):
                    continue
                payload = deepcopy(row.payload)
                for value in payload:
                    if value["uid"] == own["metadata"]["uid"]:
                        if change == "wrong_proof_uid":
                            value["uid"] = "other"
                        else:
                            value["finalizers"] = []
                signature = row.digest if change == "corrupt_proof" else digest(payload)
                db.execute(
                    update(cleanup.Evidence)
                    .where(cleanup.Evidence.id == row.id)
                    .values(payload=payload, digest=signature)
                )
            db.commit()
    try:
        q.advance()
    except AccountingUnavailable:
        pass
    assert not any(kind == "Namespace" for kind, *_ in q.calls)


@pytest.mark.parametrize("kind", ["VirtualMachine", "VirtualMachineInstance"])
def test_pending_orphan_still_requires_vm_and_vmi_absence(
    namespace_case, monkeypatch, kind
):
    q = namespace_case
    value = deepcopy(q.api.rows("VirtualMachine", q.namespace)[0])
    pending_orphan(q, monkeypatch)
    value["kind"] = kind
    value["metadata"]["uid"] = "late-compute"
    q.values[kind, value["metadata"]["name"]] = value
    try:
        q.advance()
    except AccountingUnavailable:
        pass
    assert not any(selected == "Namespace" for selected, *_ in q.calls)


@pytest.mark.parametrize(
    "change",
    ["uid", "new_cr", "finalizer", "ack", "namespace_uid", "vm", "vmi", "writer"],
)
def test_orphan_namespace_delete_rechecks_live_boundary(
    namespace_case, monkeypatch, change
):
    q = namespace_case
    own = pending_orphan(q, monkeypatch)
    delete_namespace = cleanup._delete_namespace

    def change_before_delete(db, api, work):
        if change == "uid":
            own["metadata"]["uid"] = "replacement"
        elif change == "new_cr":
            other = deepcopy(own)
            other["metadata"].update(name="unknown", uid="unknown")
            q.values[other["kind"], "unknown"] = other
        elif change == "finalizer":
            own["metadata"]["finalizers"].append("other/finalizer")
        elif change == "ack":
            q.values["Namespace", q.namespace]["metadata"]["annotations"].pop(ACK)
        elif change == "namespace_uid":
            q.values["Namespace", q.namespace]["metadata"]["uid"] = "replacement"
        elif change in {"vm", "vmi"}:
            kind = "VirtualMachine" if change == "vm" else "VirtualMachineInstance"
            q.values[kind, "late"] = dict(
                kind=kind, metadata=dict(name="late", uid="late")
            )
        else:

            def active_writer(*args):
                raise AccountingUnavailable(
                    "Original provider invocation is still running"
                )

            monkeypatch.setattr(
                cleanup.authority, "local_writers_closed", active_writer
            )
        return delete_namespace(db, api, work)

    monkeypatch.setattr(cleanup, "_delete_namespace", change_before_delete)
    with pytest.raises(AccountingUnavailable):
        q.advance()
    assert not any(kind == "Namespace" for kind, *_ in q.calls)


def test_actual_legacy_custody_runs_without_stop_or_reservation(namespace_case):
    q = namespace_case
    q.request()
    assert q.advance().phase == "fencing"
    assert q.advance().phase == "namespace_delete_requested"
    assert q.advance().phase == "namespace_absent"
    kinds = [c[0] for c in q.calls]
    assert (
        kinds.index("VirtualMachine")
        < kinds.index("TroshkaProject")
        < kinds.index("Namespace")
    )
    assert all(k in (*OWN_KINDS, "VirtualMachine", "Namespace") for k in kinds)
    with q.sessions() as db:
        assert db.query(QuotaReservation).count() == 0
        assert (
            cleanup.states(db, q.project_id)[0].authority.source_kind == "admin_custody"
        )
        assert db.get(Project, q.project_id).state == "cleanup_pending"


def test_unresolved_storage_inventory_blocks_every_delete(namespace_case):
    q = namespace_case
    q.request()
    q.advance()
    claim = deepcopy(q.api.rows("PersistentVolumeClaim", q.namespace)[0])
    claim["metadata"].update(name="unretained", uid="unretained")
    q.values["PersistentVolumeClaim", "unretained"] = claim
    with pytest.raises(AccountingUnavailable, match="storage coverage"):
        q.advance()
    assert not q.calls


def test_rootful_finite_permissions_ignore_unrelated_discovery(
    namespace_case, monkeypatch
):
    q = namespace_case
    path = q.api.path

    def finite(path_name, query=()):
        if path_name.endswith("/events") or path_name.endswith("/ordinarywidgets"):
            raise AccountingUnavailable("403 unrelated LIST is not granted")
        value = path(path_name, query)
        if path_name == "/api/v1":
            value["resources"] += [
                dict(name=name, kind=kind, namespaced=True, verbs=["list"])
                for name, kind in (
                    ("events", "Event"),
                    ("ordinarywidgets", "OrdinaryWidget"),
                )
            ]
        return value

    monkeypatch.setattr(q.api, "path", finite)
    q.request()
    q.advance()
    assert q.advance().phase == "namespace_delete_requested"
    assert q.advance().phase == "namespace_absent"


@pytest.mark.parametrize("kind", ["PersistentVolumeClaim", "VolumeSnapshot"])
def test_rootful_required_storage_list_denial_holds(namespace_case, monkeypatch, kind):
    q = namespace_case
    rows = q.api.rows
    monkeypatch.setattr(
        q.api,
        "rows",
        lambda selected, ns: (
            (_ for _ in ()).throw(AccountingUnavailable("403 required storage"))
            if selected == kind
            else rows(selected, ns)
        ),
    )
    q.request()
    q.advance()
    with pytest.raises(AccountingUnavailable, match="required storage"):
        q.advance()
    assert not q.calls


@pytest.mark.parametrize("change", ["duplicate", "kind"])
def test_required_storage_inventory_identity_is_complete(
    namespace_case, monkeypatch, change
):
    q = namespace_case
    rows = q.api.rows

    def changed(kind, namespace):
        values = rows(kind, namespace)
        if kind == "PersistentVolumeClaim":
            if change == "duplicate":
                values += deepcopy(values)
            else:
                for value in values:
                    value["kind"] = "Secret"
        return values

    monkeypatch.setattr(q.api, "rows", changed)
    q.request()
    q.advance()
    with pytest.raises(AccountingUnavailable, match="inventory"):
        q.advance()
    assert not q.calls


@pytest.mark.parametrize("when", ["before_ack", "after_ack", "after_vm", "after_owncr"])
@pytest.mark.parametrize("replacement", ["pod", "container"])
def test_completed_operator_replacement_resumes_same_cleanup(
    namespace_case, monkeypatch, when, replacement
):
    q = namespace_case
    q.request()
    first = q.advance()
    if when == "before_ack":
        q.values["Namespace", q.namespace]["metadata"]["annotations"].pop(ACK)
    if when == "after_vm":
        delete = q.api.delete
        monkeypatch.setattr(
            q.api,
            "delete",
            lambda kind, *a, **kw: (
                None if kind in OWN_KINDS else delete(kind, *a, **kw)
            ),
        )
        assert q.advance().phase == "vm_delete_requested"
        monkeypatch.setattr(q.api, "delete", delete)
    if when == "after_owncr":
        delete_namespace = q.api.delete_namespace
        monkeypatch.setattr(
            q.api, "delete_namespace", lambda *a: (_ for _ in ()).throw(TimeoutError())
        )
        with pytest.raises(TimeoutError):
            q.advance()
        monkeypatch.setattr(q.api, "delete_namespace", delete_namespace)
    identity = {
        **first.marker["operator"],
        "uid" if replacement == "pod" else "containerId": "replacement",
    }
    monkeypatch.setattr(
        q.api, "operator_profile", lambda *a: (first.marker["qualification"], identity)
    )
    monkeypatch.setattr(
        q.api,
        "qualify_operator",
        lambda pins, value: cleanup.origin.require(
            all(value.get(k) == v for k, v in identity.items()), "operator replaced"
        ),
    )
    second = q.advance()
    assert second.marker != first.marker and second.authority == first.authority
    assert q.advance().phase == "namespace_delete_requested"
    assert q.advance().phase == "namespace_absent"
    with q.sessions() as db:
        evidence = db.query(cleanup.Evidence).filter_by(cleanup_id=str(first.id)).all()
        assert any(row.payload == first.marker for row in evidence)
        assert any(row.payload == second.marker for row in evidence)


@pytest.mark.parametrize(
    "failure",
    ["unfinished", "missing_history", "overlap", "lost_patch", "two_rollouts"],
)
def test_operator_requalification_retains_unknown_and_recovers_patch(
    namespace_case, monkeypatch, failure
):
    q = namespace_case
    q.request()
    first = q.advance()
    identity = {**first.marker["operator"], "uid": "successor"}

    def profile(*args):
        if failure == "overlap":
            raise AccountingUnavailable("Operator singleton unavailable")
        return first.marker["qualification"], identity

    monkeypatch.setattr(q.api, "operator_profile", profile)
    monkeypatch.setattr(q.api, "qualify_operator", lambda *args: None)
    if failure == "unfinished":
        root = next(v for (kind, _), v in q.values.items() if kind == "TroshkaProject")
        root["status"] = {"originExecutions": {"old": {"state": "active"}}}
    if failure == "missing_history":
        for key in list(q.values):
            if key[0] == "TroshkaProject":
                q.values.pop(key)
    if failure not in {"lost_patch", "two_rollouts"}:
        with pytest.raises(AccountingUnavailable, match="unresolved|singleton|history"):
            q.advance()
        assert not q.calls
        return
    patch = q.api.patch_namespace
    monkeypatch.setattr(
        q.api, "patch_namespace", lambda *args: (_ for _ in ()).throw(TimeoutError())
    )
    with pytest.raises(TimeoutError):
        q.advance()
    if failure == "two_rollouts":
        identity["uid"] = "second-successor"
    monkeypatch.setattr(q.api, "patch_namespace", patch)
    q.advance()
    assert q.advance().phase == "namespace_delete_requested"


def test_vmi_blocks_namespace_even_after_vm_delete(namespace_case):
    q = namespace_case
    vm = q.api.rows("VirtualMachine", q.namespace)[0]
    vmi = deepcopy(vm)
    vmi["kind"] = "VirtualMachineInstance"
    vmi["metadata"]["uid"] = "running-vmi"
    q.values["VirtualMachineInstance", vmi["metadata"]["name"]] = vmi
    q.stuck_vmi = True
    q.request()
    q.advance()
    assert q.advance().phase == "vm_delete_requested"
    assert not any(k == "Namespace" for k, _, _ in q.calls)


def test_lost_namespace_delete_response_recovers_from_original_absence(namespace_case):
    q = namespace_case
    q.request()
    q.advance()
    q.ambiguous_delete = True
    with pytest.raises(TimeoutError):
        q.advance()
    assert q.advance().phase == "namespace_absent"
    assert len([k for k, _, _ in q.calls if k == "Namespace"]) == 1


def test_replaced_namespace_rejected_before_delete(namespace_case):
    q = namespace_case
    q.request()
    q.values["Namespace", q.namespace]["metadata"]["uid"] = "replacement"
    with pytest.raises(AccountingUnavailable, match="replaced"):
        q.advance()
    assert not q.calls


@pytest.mark.parametrize("failure", ["before_patch", "rv_conflict", "lost_success"])
def test_committed_marker_publication_retries_exact_original(
    namespace_case, monkeypatch, failure
):
    q = namespace_case
    patch = q.api.patch_namespace
    calls = []

    def interrupted(namespace, uid, rv, annotations):
        calls.append((uid, rv, json.loads(annotations[ANNOTATION])))
        if len(calls) == 1:
            if failure == "lost_success":
                patch(namespace, uid, rv, annotations)
            if failure == "rv_conflict":
                q.values["Namespace", q.namespace]["metadata"]["resourceVersion"] = "2"
            raise TimeoutError(failure)
        patch(namespace, uid, rv, annotations)

    monkeypatch.setattr(q.api, "patch_namespace", interrupted)
    q.request()
    with pytest.raises(TimeoutError):
        q.advance()
    with q.sessions() as db:
        marker = cleanup.states(db, q.project_id)[0].marker
    assert marker is not None and not q.calls
    q.advance()
    q.advance()
    assert q.advance().phase == "namespace_absent"
    assert all(c[0] == q.root.namespace_uid and c[2] == marker for c in calls)
    assert len(calls) == (1 if failure == "lost_success" else 2)
    if failure == "rv_conflict":
        assert calls[1][1] == "2"


def test_committed_marker_never_overwrites_different_marker(
    namespace_case, monkeypatch
):
    q = namespace_case
    patch = q.api.patch_namespace
    monkeypatch.setattr(
        q.api, "patch_namespace", lambda *args: (_ for _ in ()).throw(TimeoutError())
    )
    q.request()
    with pytest.raises(TimeoutError):
        q.advance()
    q.values["Namespace", q.namespace]["metadata"].setdefault("annotations", {})[
        ANNOTATION
    ] = json.dumps({"other": True})
    monkeypatch.setattr(q.api, "patch_namespace", patch)
    with pytest.raises(AccountingUnavailable, match="marker"):
        q.advance()
    assert not q.calls


def test_failed_legacy_with_genuine_cleanup_custody_is_admitted(namespace_case):
    q = namespace_case
    with q.sessions() as db:
        db.get(Project, q.project_id).state = "error"
        db.commit()
    assert q.request()["status"] == "cleanup_pending"


def collect_absence(q):
    from tests.test_native_cleanup_completion import collect_cleanup

    q.request()
    q.advance()
    q.advance()
    q.advance()
    for key in list(q.values):
        if key[0] == "PersistentVolume":
            q.values.pop(key)
    with q.sessions() as db:
        scope = cleanup.storage_scopes(db)[0]
    return scope, collect_cleanup(q, scope, gone=True)


@pytest.mark.parametrize("mode", ["delete", "undeploy"])
def test_real_collector_import_and_no_reservation_settlement(namespace_case, mode):
    from app.models.kubevirt_storage import NativeStorageSnapshot
    from app.services.kubevirt_storage_reports import import_report
    from app.services.native_project_retirement import settle
    from tests.test_native_managed_resume import FixedDatetime

    q = namespace_case
    q.cleanup_mode = mode
    scope, report = collect_absence(q)
    assert report.retiring_roots[0].objects_absent
    assert not report.collection_errors
    sibling = q.project_ids[1]
    with q.sessions() as db:
        report_id = import_report(
            db,
            report,
            enrollment_id=q.enrollment_id,
            expected_enrollment_digest=q.digest,
            now=FixedDatetime.current,
        )
        db.commit()
        assert db.get(NativeStorageSnapshot, report_id).accepted
        from app.services.kubevirt_storage_reports import report_inventory
        from app.services.native_project_retirement import verify_project_retirement

        verify_project_retirement(scope, report_inventory(report), report.images)
        from app.models.kubevirt_storage import (
            NativeCollectionDomain,
            NativeProjectRoot,
        )
        from app.services.kubevirt_storage_usage import domain_evidence

        domain = db.get(NativeCollectionDomain, str(scope.domain_id))
        graph, _, failures = domain_evidence(
            db,
            domain,
            db.query(NativeProjectRoot).filter_by(domain_id=domain.id).all(),
            q.project_id,
        )
        assert not failures and not any(p.blockers for p in graph.projects), [
            p.blockers for p in graph.projects
        ]
        settle(db, q.cleanup_id)
    with q.sessions() as db:
        project = db.get(Project, q.project_id)
        assert project is None if mode == "delete" else project.state == "draft"
        assert db.get(Project, sibling).state == "active"
        assert cleanup.states(db, q.project_id)[0].phase == "finalized"
        assert db.query(QuotaReservation).count() == 0
    assert q.apis[sibling].rows("VirtualMachine", None)
    assert all(
        i.absence is None for i in report.images if i.key not in scope.owned_images
    )


@pytest.mark.parametrize("missing_source_chain", [False, True])
@pytest.mark.parametrize(
    "current_shape,dependency_timeout,verified",
    [
        (False, False, False),
        (True, False, False),
        (True, True, True),
        (True, True, False),
    ],
)
def test_terminal_settlement_with_other_unresolved_snapshot_keeps_unknown_chain_pending(
    namespace_case,
    monkeypatch,
    missing_source_chain,
    current_shape,
    dependency_timeout,
    verified,
):
    from types import SimpleNamespace
    from uuid import uuid4

    from app.models.kubevirt_storage import NativeStorageSnapshot
    from app.schemas.kubevirt_storage import SnapshotKey
    from app.schemas.native_online_enrollment import CurrentAncestryCustody
    from app.schemas.native_snapshot_custody import OriginProducerIntent
    from app.services import native_snapshot_graph
    from app.services.kubevirt_storage_inventory import inventory_digest
    from app.services.kubevirt_storage_reports import import_report, report_inventory
    from app.services.native_project_retirement import settle
    from tests.test_native_managed_resume import FixedDatetime

    q = namespace_case
    scope, report = collect_absence(q)
    source = next(
        b for b in report.volume_bindings if b.root.project_id != scope.root.project_id
    )
    cache = report.cache_bindings[0]
    middle = source.image_key.model_copy(
        update={"image_id": "other-unresolved-intermediate"}
    )
    head_parent = SnapshotKey(image_key=middle, snapshot_id="1")
    cache_parent = SnapshotKey(image_key=cache.image_key, snapshot_id="2")
    lineage = CurrentAncestryCustody(
        binding=source,
        source=cache,
        intermediate=middle,
        head_parent=head_parent,
        intermediate_parent=cache_parent,
        verified_at=report.started_at,
    )
    producer = OriginProducerIntent(
        id=uuid4(),
        operation_digest="a" * 64,
        kind="VolumeSnapshot",
        name="old-unresolved",
        role="snapshot",
        spec_digest="b" * 64,
        source_pvc_name="old-source",
        source_binding=source,
        snapshot_spec={
            "source": {"persistentVolumeClaimName": "old-source"},
            "volumeSnapshotClassName": "rbd",
        },
    )
    # The resolver is the protected-authority boundary; report uploads cannot
    # provide these facts. Exercise real import, graph recomputation and settle.
    facts = SimpleNamespace(
        publication=SimpleNamespace(root=source.root),
        bindings=[source],
        ancestry=[lineage],
        producers=[producer],
        snapshots=[],
        snapshot_restores=[],
        snapshot_retirements=[],
    )
    monkeypatch.setattr(
        native_snapshot_graph, "resolve", lambda *args, **kwargs: [facts]
    )
    images = {i.key: i for i in report.images}
    images[source.image_key] = images[source.image_key].model_copy(
        update={"parent": head_parent}
    )
    images[cache.image_key] = images[cache.image_key].model_copy(
        update={"snapshots": [cache_parent], "children": [middle]}
    )
    if not missing_source_chain:
        images[middle] = images[source.image_key].model_copy(
            update={
                "key": middle,
                "parent": cache_parent,
                "snapshots": [head_parent],
                "children": [source.image_key],
            }
        )
    if current_shape:
        from tests.test_native_snapshot_scope import with_dependencies

        child = source.image_key.model_copy(
            update={"image_id": "current-snapshot-child"}
        )
        snapshot = SnapshotKey(image_key=source.image_key, snapshot_id="113025")
        images[source.image_key] = images[source.image_key].model_copy(
            update={"snapshots": [snapshot], "children": [child]}
        )
        images[child] = images[source.image_key].model_copy(
            update={
                "key": child,
                "parent": snapshot,
                "children": [],
                "snapshots": [SnapshotKey(image_key=child, snapshot_id="113026")],
            }
        )
        if middle in images:
            images[middle] = images[middle].model_copy(update={"parent": None})
        images[cache.image_key] = images[cache.image_key].model_copy(
            update={"snapshots": [], "children": []}
        )
        images = {
            key: with_dependencies(image) if image.absence is None else image
            for key, image in images.items()
        }
        if dependency_timeout:
            from tests.test_native_snapshot_scope import with_usage_timeout

            images[child] = with_usage_timeout(
                images[child], report.completed_at if verified else None
            )
    report = report.model_copy(
        update={
            "images": list(images.values()),
            "project_scope": scope.root.project_id,
            "project_roots": [scope.root],
            "volume_bindings": [
                b
                for b in report.volume_bindings
                if b.root.project_id == scope.root.project_id
            ],
            "retained_bindings": [
                b
                for b in report.retained_bindings
                if b.root.project_id == scope.root.project_id
            ],
        }
    )
    report = report.model_copy(
        update={"inventory_digest": inventory_digest(report_inventory(report))}
    )
    with q.sessions() as db:
        report_id = import_report(
            db,
            report,
            enrollment_id=q.enrollment_id,
            expected_enrollment_digest=q.digest,
            now=FixedDatetime.current,
        )
        db.commit()
        assert db.get(NativeStorageSnapshot, report_id).accepted
        # Map-based release no longer waits on collector proof completeness.
        settle(db, q.cleanup_id)
    with q.sessions() as db:
        assert cleanup.states(db, q.project_id)[0].phase == "finalized"
        assert db.get(Project, q.project_id).state == "draft"
        assert db.get(Project, str(source.root.project_id)).state == "active"


@pytest.mark.parametrize(
    "change", ["cleanup_id", "digest", "private_set", "other_project"]
)
def test_report_import_rejects_changed_cleanup_claim(namespace_case, change):
    from uuid import uuid4

    from app.models.kubevirt_storage import NativeStorageSnapshot
    from app.services.kubevirt_storage_inventory import inventory_digest
    from app.services.kubevirt_storage_reports import import_report, report_inventory
    from tests.test_native_managed_resume import FixedDatetime

    q = namespace_case
    _, report = collect_absence(q)
    proof = report.retiring_roots[0]
    values = {
        "cleanup_id": {"intent_id": uuid4()},
        "digest": {"inventory_digest": "0" * 64},
        "private_set": {"owned_images": []},
        "other_project": {"root": q.report.project_roots[1]},
    }
    report = report.model_copy(
        update={"retiring_roots": [proof.model_copy(update=values[change])]}
    )
    report = report.model_copy(
        update={"inventory_digest": inventory_digest(report_inventory(report))}
    )
    with q.sessions() as db:
        report_id = import_report(
            db,
            report,
            enrollment_id=q.enrollment_id,
            expected_enrollment_digest=q.digest,
            now=FixedDatetime.current,
        )
        db.commit()
        assert not db.get(NativeStorageSnapshot, report_id).accepted


def test_pending_namespace_cleanup_blocks_redeploy(namespace_case):
    from app.services.quota_cleanup import guard_pending

    q = namespace_case
    q.request()
    with q.sessions() as db, pytest.raises(
        Exception, match="cleanup|pending|read-only|deleting"
    ):
        guard_pending(db, db.get(Project, q.project_id))


def test_deleting_state_blocks_topology_writes_without_native_intent(
    namespace_case,
):
    from app.services.quota_cleanup import guard_pending

    q = namespace_case
    with q.sessions() as db:
        project = db.get(Project, q.project_id)
        project.state = "deleting"
        db.commit()
        with pytest.raises(Exception, match="read-only|deleting"):
            guard_pending(db, project)


def test_namespace_absent_requests_cleanup_priority_collection(namespace_case):
    q = namespace_case
    q.request()
    q.advance()
    assert q.advance().phase == "namespace_delete_requested"
    assert q.settlement_collections  # warms as soon as NS DELETE is retained
    assert q.advance().phase == "namespace_absent"
    # Collector settlement is optional audit; project release no longer depends on it.


def test_missing_storage_identity_remains_debt_after_namespace_absence(namespace_case):
    from app.services.kubevirt_storage_reports import report_inventory
    from app.services.native_project_retirement import verify_project_retirement

    q = namespace_case
    scope, report = collect_absence(q)
    liabilities = scope.liabilities.model_copy(
        update={"unresolved": ["unknown_snapshot_handle"]}
    )
    scope = scope.model_copy(
        update={
            "liabilities": liabilities,
            "inventory_digest": digest(liabilities.model_dump(mode="json")),
        }
    )
    with pytest.raises(AccountingUnavailable, match="unresolved"):
        verify_project_retirement(scope, report_inventory(report), report.images)


@pytest.mark.parametrize(
    "change",
    ["missing_private", "live_private", "missing_shared", "absent_shared", "old_proof"],
)
def test_terminal_storage_requires_private_absence_and_shared_survival(
    namespace_case, change
):
    from datetime import timedelta

    from app.services.kubevirt_storage_reports import report_inventory
    from app.services.native_project_retirement import verify_project_retirement

    q = namespace_case
    scope, report = collect_absence(q)
    shared = report.cache_bindings[0].image_key
    liabilities = scope.liabilities.model_copy(update={"shared_images": [shared]})
    signature = digest(liabilities.model_dump(mode="json"))
    scope = scope.model_copy(
        update={"liabilities": liabilities, "inventory_digest": signature}
    )
    inventory = report_inventory(report)
    proof = inventory.retiring_roots[0].model_copy(
        update={"inventory_digest": signature}
    )
    inventory = inventory.model_copy(update={"retiring_roots": [proof]})
    images = list(report.images)
    verify_project_retirement(scope, inventory, images)
    private = scope.owned_images[0]
    if change.startswith("missing"):
        key = private if change == "missing_private" else shared
        images = [i for i in images if i.key != key]
    elif change == "live_private":
        images = [
            i.model_copy(update={"absence": None}) if i.key == private else i
            for i in images
        ]
    elif change == "absent_shared":
        absence = next(i.absence for i in images if i.key == private)
        images = [
            i.model_copy(update={"absence": absence}) if i.key == shared else i
            for i in images
        ]
    else:
        inventory = inventory.model_copy(
            update={
                "retiring_roots": [
                    proof.model_copy(
                        update={"observed_at": scope.teardown_at - timedelta(seconds=1)}
                    )
                ]
            }
        )
    with pytest.raises(AccountingUnavailable):
        verify_project_retirement(scope, inventory, images)


def test_own_cr_finalizer_blocks_namespace_and_preserves_rolebinding(
    namespace_case, monkeypatch
):
    q = namespace_case
    q.values["RoleBinding", "operator"] = dict(
        apiVersion="rbac.authorization.k8s.io/v1",
        kind="RoleBinding",
        metadata=dict(
            name="operator", namespace=q.namespace, uid="rb-uid", resourceVersion="1"
        ),
    )
    delete = q.api.delete

    def keep_finalizer(kind, namespace, name, uid, rv, **kwargs):
        if kind == "TroshkaProject":
            q.values[kind, name]["metadata"][
                "deletionTimestamp"
            ] = "2026-01-01T00:00:00Z"
        else:
            delete(kind, namespace, name, uid, rv, **kwargs)

    monkeypatch.setattr(q.api, "delete", keep_finalizer)
    q.request()
    q.advance()
    work = q.advance()
    assert "finalizers" in work.last_error
    assert ("RoleBinding", "operator") in q.values
    assert not any(k == "Namespace" for k, _, _ in q.calls)


def test_interrupted_operator_history_remains_storage_debt(namespace_case):
    q = namespace_case
    root = q.api.rows("TroshkaProject", q.namespace)[0]
    q.values["TroshkaProject", root["metadata"]["name"]]["status"] = dict(
        originExecutions={"old-incarnation": {"state": "active"}}
    )
    q.request()
    q.advance()
    with pytest.raises(AccountingUnavailable, match="storage coverage"):
        q.advance()
    assert not q.calls
    with q.sessions() as db:
        work = cleanup.states(db, q.project_id)[0]
        assert work.phase == "fencing"
        assert (
            "interrupted_operator_execution:old-incarnation"
            in work.liabilities.unresolved
        )


def test_verified_whole_project_snapshot_retirement_keeps_ordinary_rules(snapshot_case):
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.schemas.kubevirt_storage import BackendImageState, RetiringRoot
    from app.schemas.native_project_cleanup import CleanupStorageScope
    from app.services.kubevirt_storage_reports import report_inventory
    from app.services.native_cleanup_authority import _liabilities
    from app.services.native_project_retirement import verify_project_retirement
    from app.services.native_snapshot_graph import authorize

    q = snapshot_case
    from app.services import native_origin_custody as custody

    verified = snapshot_support.snapshot_verify(q)
    with q.sessions() as db:
        receipt = custody._receipt(custody.apply(db, verified))
        db.commit()
    liabilities = _liabilities(receipt, q.root, [])
    now = datetime.now(UTC)
    scope = CleanupStorageScope(
        intent_id=uuid4(),
        operation_id="test-project-retirement",
        domain_id=q.pins.domain_id,
        root=q.root,
        phase="namespace_absent",
        inventory_digest=digest(liabilities.model_dump(mode="json")),
        owned_images=liabilities.private_images,
        bindings=liabilities.bindings,
        teardown_at=now,
        liabilities=liabilities,
    )
    proof = RetiringRoot(
        **scope.model_dump(
            include={
                "intent_id",
                "operation_id",
                "root",
                "inventory_digest",
                "owned_images",
            }
        ),
        objects_absent=True,
        observed_at=now,
    )
    template = q.report.images[0]
    images = [
        template.model_copy(
            update=dict(
                key=key,
                parent=None,
                snapshots=[],
                children=[],
                collected_at=now,
                allocated_bytes=0,
                provisioned_bytes=0,
                measurement_status="complete",
                measurement_semantics_version="kubevirt-rbd-absence-v1",
                absence=BackendImageState(
                    **key.model_dump(), active=False, trash=False, collected_at=now
                ),
            )
        )
        for key in scope.owned_images
    ]
    inventory = report_inventory(q.report).model_copy(
        update={"retiring_roots": [proof]}
    )
    terminal = verify_project_retirement(scope, inventory, images)
    observations = {i.key: i for i in images}
    _, ordinary_errors = authorize(inventory, observations, [receipt])
    assert ordinary_errors
    _, terminal_errors = authorize(
        inventory, observations, [receipt], project_retirements=[terminal]
    )
    assert not terminal_errors
    with pytest.raises(AccountingUnavailable, match="Private"):
        verify_project_retirement(
            scope, inventory, [i for i in images if i.key != q.image]
        )


def test_dns_failure_keeps_settlement_and_retries_original_records(
    namespace_case, monkeypatch
):
    from types import SimpleNamespace

    from app.models.dns_provider import DnsProvider
    from app.services import dns_service
    from app.services.kubevirt_storage_reports import import_report
    from app.services.providers import kubevirt
    from tests.test_native_managed_resume import FixedDatetime

    q = namespace_case
    monkeypatch.setattr(
        kubevirt,
        "_get_k8s_clients",
        lambda _: (None, None, SimpleNamespace(close=lambda: None)),
    )
    monkeypatch.setattr(cleanup, "NamespaceClient", lambda _: q.api)
    records = [{"name": "original.example.invalid", "type": "A", "value": "192.0.2.1"}]
    with q.sessions() as db:
        provider = DnsProvider(name="fixture", type="route53", config={})
        db.add(provider)
        db.flush()
        project = db.get(Project, q.project_id)
        project.dns_provider_id = provider.id
        project.deployed_topology = {
            **(project.deployed_topology or project.topology),
            "_dns_records": records,
        }
        db.commit()
    _, report = collect_absence(q)
    calls = []
    monkeypatch.setattr(
        dns_service,
        "delete_dns_records",
        lambda kind, config, value: calls.append(value) or ["failed"],
    )
    with q.sessions() as db:
        import_report(
            db,
            report,
            enrollment_id=q.enrollment_id,
            expected_enrollment_digest=q.digest,
            now=FixedDatetime.current,
        )
        db.commit()
    with pytest.raises(AccountingUnavailable, match="DNS removal"):
        cleanup.reconcile(q.cleanup_id)
    with q.sessions() as db:
        work = cleanup.states(db, q.project_id)[0]
        assert work.phase == "namespace_absent"
        project = db.get(Project, q.project_id)
        assert project is not None
        assert project.deployed_topology["_dns_records"] == records
    monkeypatch.setattr(
        dns_service,
        "delete_dns_records",
        lambda kind, config, value: calls.append(value) or [],
    )
    cleanup.reconcile(q.cleanup_id)
    assert records in calls
    with q.sessions() as db:
        assert cleanup.states(db, q.project_id)[0].phase == "finalized"
        assert db.get(Project, q.project_id).state == "draft"


def test_postgres_concurrent_requests_keep_one_retained_state(namespace_case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    q = namespace_case
    if q.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locking required")
    start = Barrier(2)

    def request():
        start.wait(timeout=5)
        return q.request()["cleanup_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request) for _ in range(2)]
        ids = [f.result(timeout=10) for f in futures]
    assert ids[0] == ids[1]
    with q.sessions() as db:
        assert len(cleanup.states(db, q.project_id)) == 1


def test_root_absence_keeps_retained_original_authority(namespace_case):
    q = namespace_case
    for key in list(q.values):
        if key[0] == "TroshkaProject":
            q.values.pop(key)
    q.request()
    q.advance()
    q.advance()
    assert q.advance().phase == "namespace_absent"


def test_retained_old_cleanup_does_not_block_new_root_generation(namespace_case):
    from app.services.kubevirt_storage_reports import report_inventory
    from app.services.native_project_retirement import verified_retirements

    q = namespace_case
    scope, report = collect_absence(q)
    new_root = scope.root.model_copy(
        update={"namespace_uid": "new-namespace", "project_cr_uid": "new-root"}
    )
    inventory = report_inventory(report).model_copy(
        update={"project_roots": [new_root], "retiring_roots": []}
    )
    verified, errors = verified_retirements(
        [scope.model_copy(update={"phase": "finalized"})], inventory, []
    )
    assert verified == errors == []


def test_root_submitted_origin_captures_storage_before_destruction(
    origin_namespace_case,
):
    from app.models.native_origin_custody import NativeOriginCustodyReceipt
    from app.services import native_origin_writers

    q = origin_namespace_case
    with q.sessions() as db:
        assert native_origin_writers.root_submitted(db, str(q.intent.id))
        assert db.query(NativeOriginCustodyReceipt).count() == 0
    q.request()
    q.advance()
    q.advance()
    with q.sessions() as db:
        work = cleanup.states(db, q.project_id)[0]
        assert work.authority.domain_id is not None
        assert work.liabilities.bindings and not work.liabilities.unresolved
    q.advance()
    q.advance()
    work = q.advance()
    assert work.phase == "namespace_absent"
    assert (
        work.authority.source_kind == "origin" and work.authority.domain_id is not None
    )
    assert not work.liabilities.unresolved


def _strip_cleanup_root_uid(q):
    """Reproduce delete-before-root-bind: cleanup frozen with root_uid=None."""
    with q.sessions() as db:
        work = cleanup.states(db, q.project_id)[0]
        root_uid = work.authority.root_uid
        assert root_uid
        stripped = work.authority.model_copy(update={"root_uid": None})
        cleanup._save(
            db,
            work,
            authority=stripped,
            last_error="Original root UID unresolved; producer boundary unavailable",
        )
        db.commit()
    return root_uid


def test_fencing_adopts_sealed_origin_root_before_marker(origin_namespace_case):
    """Cleanup prepared without root_uid must adopt a later-sealed receipt."""
    q = origin_namespace_case
    q.request()
    root_uid = _strip_cleanup_root_uid(q)
    assert any(kind == "TroshkaProject" for kind, _ in q.values)
    advanced = q.advance()
    assert advanced.authority.root_uid == root_uid
    assert advanced.marker is not None
    assert advanced.marker["rootUid"] == root_uid
    with q.sessions() as db:
        assert any(
            row.phase.startswith("origin_root_recovered:")
            for row in db.query(cleanup.Evidence).filter_by(cleanup_id=q.cleanup_id)
        )


def test_fencing_seals_late_cancelled_root_via_reconcile(
    origin_namespace_case, monkeypatch
):
    """Cancelled mid-root POST must be sealed before fencing while the CR lives."""
    from uuid import uuid4

    from app.models.native_deployment_origin import NativeDeploymentOriginActive
    from app.services import native_deployment_origin as origin

    q = origin_namespace_case
    monkeypatch.setattr(origin, "require_idle", lambda *_a, **_k: None)
    q.request()
    root_uid = _strip_cleanup_root_uid(q)
    with q.sessions() as db:
        pointer = db.get(NativeDeploymentOriginActive, q.project_id)
        pointer.cancelled = True
        pointer.io_phase = "root"
        pointer.io_token = str(uuid4())
        pointer.io_job_id = "late-root-job"
        db.commit()
    real_receipts = origin.receipts
    sealed = {"done": False}

    def receipts(db, intent_id):
        namespace, root = real_receipts(db, intent_id)
        return (namespace, None) if not sealed["done"] else (namespace, root)

    def reconcile_cancelled(db, api, intent):
        sealed["done"] = True
        pointer = db.get(NativeDeploymentOriginActive, str(intent.project_id))
        pointer.io_phase = None
        pointer.io_token = None
        pointer.io_job_id = None
        db.commit()
        return {"root": True}

    monkeypatch.setattr(origin, "receipts", receipts)
    monkeypatch.setattr(origin, "reconcile_cancelled", reconcile_cancelled)
    advanced = q.advance()
    assert sealed["done"]
    assert advanced.authority.root_uid == root_uid
    assert advanced.marker is not None


def test_root_spec_matches_accepts_launch_canonical_for_pre_canonical_intent():
    """Already-stuck intents may store raw float digests; launch match still seals."""
    from types import SimpleNamespace
    from uuid import uuid4

    from app.services import native_deployment_origin as origin
    from app.services.kubevirt_storage_enrollment import digest, digest_cr_spec

    float_spec = {"projectId": str(uuid4()), "topology": {"x": 300.0, "ram": 32.0}}
    int_spec = {"projectId": float_spec["projectId"], "topology": {"x": 300, "ram": 32}}
    intent = SimpleNamespace(cr_spec_digest=digest(float_spec))
    assert digest(int_spec) != intent.cr_spec_digest
    assert digest_cr_spec(int_spec) != intent.cr_spec_digest
    assert origin._root_spec_matches(intent, int_spec, launch_spec=float_spec)
    assert not origin._root_spec_matches(intent, int_spec, launch_spec=None)


def test_null_root_marker_still_requires_absent_project_cr(origin_namespace_case):
    """Without recovery, fencing keeps the original producer-boundary hold."""
    q = origin_namespace_case
    q.request()
    with q.sessions() as db:
        work = cleanup.states(db, q.project_id)[0]
        work = cleanup._save(
            db,
            work,
            authority=work.authority.model_copy(update={"root_uid": None}),
        )
        db.commit()
        namespace = q.api.get("Namespace", None, work.authority.namespace)
        assert any(kind == "TroshkaProject" for kind, _ in q.values)
        with pytest.raises(AccountingUnavailable, match="producer boundary"):
            cleanup._publish_marker(db, q.api, work, namespace, "troshka-operator")


def legacy_missing_custody(q, monkeypatch):
    # The API durably records cancellation without remote I/O. This is also
    # the exact pre-fix legacy state supported by background retention.
    q.request()
    return q.advance()


def test_cleanup_rejects_referenced_claim_and_pv_missing_before_fence(
    origin_namespace_case, monkeypatch
):
    q = origin_namespace_case
    vm = next(v for (k, _), v in q.values.items() if k == "VirtualMachine")
    name = next(
        v["metadata"]["name"]
        for (k, _), v in q.values.items()
        if k == "PersistentVolumeClaim"
    )
    vm["spec"]["template"]["spec"]["volumes"].append(
        {"name": "retained-disk", "persistentVolumeClaim": {"claimName": name}}
    )
    pvc = q.values.pop(("PersistentVolumeClaim", name))
    remaining = [
        p
        for p in q.reader.pvs()
        if p.get("spec", {}).get("claimRef", {}).get("uid") != pvc["metadata"]["uid"]
    ]
    monkeypatch.setattr(q.reader, "pvs", lambda: remaining)
    q.values.pop(("PersistentVolume", "retained-pv"))
    q.request()
    from app.services.native_managed_power import helpers

    with pytest.raises(helpers().ManagedPowerError, match="managed_claim_missing"):
        q.advance()
        q.advance()
    assert not q.calls


@pytest.mark.parametrize("unsettled", ["project", "child"])
def test_cancelled_cleanup_retains_storage_without_runtime_custody(
    origin_namespace_case, unsettled
):
    from app.models.native_origin_custody import NativeOriginCustodyReceipt
    from app.schemas.native_origin_custody import OriginCustodyReceipt
    from app.services import native_origin_custody as custody
    from app.services.native_managed_power import helpers

    q = origin_namespace_case
    kind = "TroshkaProject" if unsettled == "project" else "TroshkaVM"
    resource = next(v for (k, _), v in q.values.items() if k == kind)
    if unsettled == "project":
        resource["status"]["phase"] = "Deploying"
    else:
        resource["status"]["observedGeneration"] = 0
    with pytest.raises(helpers().ManagedPowerError):
        custody.verify(origin_support.prepare(q), q.readers, q.backend)
    with pytest.raises(ValueError, match="cancellation fence"):
        origin_support.prepare(q, mode="cleanup")
    q.request()
    q.advance()
    q.advance()
    with q.sessions() as db:
        row = db.query(NativeOriginCustodyReceipt).one()
        receipt = OriginCustodyReceipt.model_validate(row.payload)
        assert receipt.bindings
        assert receipt.complete is False


def test_cancelled_missing_custody_recovers_atomically_and_reacks(
    origin_namespace_case, monkeypatch
):
    from app.models.native_deployment_origin import NativeDeploymentOriginActive
    from app.models.native_origin_custody import NativeOriginCustodyReceipt

    q = origin_namespace_case
    old = legacy_missing_custody(q, monkeypatch)
    recovered = q.advance()
    assert recovered.authority.domain_id is not None and recovered.liabilities.bindings
    assert recovered.marker == old.marker and not q.calls
    with q.sessions() as db:
        assert db.get(NativeDeploymentOriginActive, q.project_id).cancelled
        assert db.query(NativeOriginCustodyReceipt).count() == 1
        original = (
            db.query(cleanup.Evidence)
            .filter_by(cleanup_id=q.cleanup_id, phase="requested")
            .one()
        )
        transition = (
            db.query(cleanup.Evidence)
            .filter_by(cleanup_id=q.cleanup_id, phase="storage_retained")
            .one()
        )
        assert original.payload["authority"]["domain_id"] is None
        assert transition.payload["previous"] == old.model_dump(mode="json")
    renewed = q.advance()
    assert renewed.marker != old.marker and not q.calls
    assert q.advance().phase == "namespace_delete_requested"
    assert q.advance().phase == "namespace_absent"
    with q.sessions() as db:
        assert db.query(NativeOriginCustodyReceipt).count() == 1


@pytest.mark.parametrize(
    "after_verify",
    [
        "stable",
        "bookkeeping",
        "spec",
        "owner",
        "generation",
        "execution",
        "deleting",
        "timeout",
        "claim_changed",
        "ack_changed",
        "namespace_changed",
        "qualification_changed",
    ],
)
def test_slow_retention_uses_separate_bounded_transport_phase(
    origin_namespace_case, monkeypatch, after_verify
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.services import native_cleanup_storage as retention
    from app.services import native_origin_custody as custody
    from app.services import native_origin_inventory_transport as transport
    from app.services import native_origin_namespace_cleanup as inventory
    from app.services.native_managed_power import helpers
    from app.services.native_namespace_client import NamespaceClient

    q = origin_namespace_case
    work = legacy_missing_custody(q, monkeypatch)
    live_root = next(
        value for (kind, _), value in q.values.items() if kind == "TroshkaProject"
    )
    progress = dict(
        started="2026-11-18T02:28:15.623386+00:00",
        delayed="2026-11-18T03:32:50.866166+00:00",
        purpose="resume",
        retries=64,
        success=False,
        failure=False,
        message="namespace_cleanup_production_fenced",
    )
    live_root["metadata"].setdefault("annotations", {})[
        "kopf.zalando.org/project_managed_power_resume"
    ] = json.dumps(progress)
    live_root.setdefault("status", {})["kopf"] = {
        "progress": {"project_managed_power_resume": deepcopy(progress)}
    }
    clock, calls = [1000.0], []
    monkeypatch.setattr(inventory.time, "monotonic", lambda: clock[0])
    api = NamespaceClient(
        SimpleNamespace(
            default_headers={},
            configuration=SimpleNamespace(host="https://fixture.invalid"),
            update_params_for_auth=Mock(),
            rest_client=SimpleNamespace(pool_manager=object()),
        )
    )
    phases = []
    new_observation = NamespaceClient.new_observation

    def next_phase(client):
        fresh = new_observation(client)
        phases.append(fresh)
        return fresh

    monkeypatch.setattr(NamespaceClient, "new_observation", next_phase)
    routes = helpers().KINDS | helpers().CLEANUP_KINDS

    def read(pool, url, **kwargs):
        calls.append(url)
        parts = url.split("/")
        resource = (
            parts[-1] if parts[-1] in {r for _, r in routes.values()} else parts[-2]
        )
        kind = next(k for k, (_, r) in routes.items() if r == resource)
        if parts[-1] == resource:
            value = dict(
                metadata=dict(resourceVersion="1"), items=q.api.rows(kind, q.namespace)
            )
        else:
            value = q.api.get(kind, q.namespace, parts[-1])
        if after_verify == "timeout" and clock[0] > 1100:
            clock[0] += 56
        return (200, json.dumps(value).encode()) if value is not None else (404, b"{}")

    monkeypatch.setattr(transport, "read", read)

    def qualify(self, pins, identity):
        cleanup.origin.require(
            after_verify != "qualification_changed", "operator qualification changed"
        )
        q.api.qualify_operator(pins, identity)

    monkeypatch.setattr(NamespaceClient, "qualify_operator", qualify)
    verify = custody.verify_configured

    def slow(request):
        result = verify(request)
        clock[0] += 180
        root = next(
            value for (kind, _), value in q.values.items() if kind == "TroshkaProject"
        )
        if after_verify == "bookkeeping":
            root["metadata"]["resourceVersion"] = "new-status-version"
            root["metadata"].setdefault("annotations", {})[
                "kopf.zalando.org/project_managed_power_resume"
            ] = json.dumps(
                {**progress, "delayed": "2026-11-18T03:40:00", "retries": 65}
            )
            root.setdefault("status", {})["kopf"] = {
                "progress": {
                    "project_managed_power_resume": {
                        **progress,
                        "delayed": "2026-11-18T03:40:00",
                        "retries": 65,
                    }
                }
            }
            root["metadata"]["managedFields"] = [
                {"manager": "kopf", "time": "2026-11-18T03:40:00Z"}
            ]
        elif after_verify == "spec":
            root["spec"]["changed"] = True
        elif after_verify == "owner":
            root["metadata"]["ownerReferences"] = [{"uid": "different"}]
        elif after_verify == "generation":
            root["metadata"]["generation"] += 1
        elif after_verify == "execution":
            root.setdefault("status", {})["originExecutions"] = {
                "new": {"state": "active"}
            }
        elif after_verify == "deleting":
            root["metadata"]["deletionTimestamp"] = "2026-11-18T03:40:00Z"
        if after_verify == "claim_changed":
            key = next(k for k in q.values if k[0] == "PersistentVolumeClaim")
            q.values[key]["metadata"]["uid"] = "replacement"
        if after_verify == "ack_changed":
            q.values["Namespace", q.namespace]["metadata"]["annotations"][ACK] = "{}"
        if after_verify == "namespace_changed":
            q.values["Namespace", q.namespace]["metadata"]["uid"] = "replacement"
        return result

    monkeypatch.setattr(custody, "verify_configured", slow)
    with q.sessions() as db:
        if after_verify in {"stable", "bookkeeping"}:
            recovered = retention.recover(db, api, work)
            assert recovered.authority.domain_id is not None
        else:
            with pytest.raises(
                AccountingUnavailable,
                match="budget exceeded|resources changed|acknowledgement changed|namespace replaced|qualification changed",
            ):
                retention.recover(db, api, work)
    assert not q.calls
    assert api.deadline == 1055 and api.requests == 6
    assert len(calls) > 6
    assert len(phases) == 1 and phases[0].deadline == 1235


@pytest.mark.parametrize(
    "field",
    [
        "uid",
        "generation",
        "ownerReferences",
        "finalizers",
        "labels",
        "annotations",
        "deletionTimestamp",
    ],
)
def test_retention_inventory_keeps_metadata_authority(field):
    from app.services.native_cleanup_storage import _same_inventory

    original = dict(
        kind="TroshkaProject",
        metadata=dict(name="original", namespace="ns", uid="uid", generation=1),
        spec={},
    )
    changed = deepcopy(original)
    changed["metadata"][field] = (
        {"different": True} if field in {"labels", "annotations"} else "different"
    )
    assert not _same_inventory([original], [changed])


@pytest.mark.parametrize(
    "kind,field",
    [
        ("PersistentVolumeClaim", "phase"),
        ("VolumeSnapshot", "boundVolumeSnapshotContentName"),
        ("VolumeSnapshot", "snapshotHandle"),
        ("TroshkaProject", "originExecutions"),
        ("TroshkaProject", "originProducerObjects"),
    ],
)
def test_retention_inventory_keeps_storage_and_producer_status(kind, field):
    from app.services.native_cleanup_storage import _same_inventory

    original = dict(
        kind=kind,
        metadata=dict(name="original", namespace="ns", uid="uid"),
        spec={},
        status={field: "original"},
    )
    changed = deepcopy(original)
    changed["status"][field] = "replacement"
    assert not _same_inventory([original], [changed])


@pytest.mark.parametrize(
    "kind,handler",
    [
        ("TroshkaProject", "project_managed_power_resume"),
        ("TroshkaNetwork", "network_resume"),
    ],
)
def test_retention_comparison_only_ignores_exact_fenced_retry_schedule(kind, handler):
    from app.services.native_cleanup_storage import _same_inventory

    progress = dict(
        purpose="resume",
        message="namespace_cleanup_production_fenced",
        success=False,
        failure=False,
        started="original",
        delayed="first",
        retries=1,
    )
    original = dict(
        kind=kind,
        metadata=dict(
            name="root",
            uid="uid",
            annotations={"kopf.zalando.org/" + handler: json.dumps(progress)},
        ),
        status={"kopf": {"progress": {handler: progress}}},
    )
    retried = deepcopy(original)
    retry = retried["status"]["kopf"]["progress"][handler]
    retry.update(delayed="later", retries=2)
    retried["metadata"]["annotations"]["kopf.zalando.org/" + handler] = json.dumps(
        retry
    )
    assert _same_inventory([original], [retried])
    for key in ("success", "failure", "message", "purpose", "started"):
        changed = deepcopy(original)
        changed["status"]["kopf"]["progress"][handler][key] = "changed"
        assert not _same_inventory([original], [changed])
    changed = deepcopy(original)
    changed["metadata"]["annotations"]["kopf.zalando.org/other_handler"] = "changed"
    assert not _same_inventory([original], [changed])
    assert _same_inventory([original, changed], [changed, original])


@pytest.mark.parametrize("failure", ["verify", "adopt"])
def test_failed_retention_preserves_cancelled_request_and_resources(
    origin_namespace_case, monkeypatch, failure
):
    from app.models.native_origin_custody import NativeOriginCustodyReceipt
    from app.services import native_origin_custody as custody

    q = origin_namespace_case
    old = legacy_missing_custody(q, monkeypatch)
    if failure == "verify":
        monkeypatch.setattr(
            custody,
            "verify_configured",
            lambda *args: (_ for _ in ()).throw(AccountingUnavailable("read failed")),
        )
    else:
        save = cleanup._save

        def fail_adoption(db, work, **updates):
            if "authority" in updates:
                raise AccountingUnavailable("adoption crash")
            return save(db, work, **updates)

        monkeypatch.setattr(cleanup, "_save", fail_adoption)
    with pytest.raises(AccountingUnavailable):
        q.advance()
    assert not q.calls
    with q.sessions() as db:
        assert cleanup.states(db, q.project_id)[0] == old
        assert db.query(NativeOriginCustodyReceipt).count() == 0


@pytest.mark.parametrize(
    "change",
    [
        "root_missing",
        "root_replaced",
        "claim_missing",
        "claim_replaced",
        "duplicate_claim",
        "namespace_replaced",
        "execution_active",
    ],
)
def test_recovery_rejects_missing_replaced_or_ambiguous_resources(
    origin_namespace_case, monkeypatch, change
):
    q = origin_namespace_case
    legacy_missing_custody(q, monkeypatch)
    kind = "TroshkaProject" if change.startswith("root") else "PersistentVolumeClaim"
    kind = "Namespace" if change == "namespace_replaced" else kind
    key = next(
        key
        for key in q.values
        if key[0] == kind and (kind != "Namespace" or key[1] == q.namespace)
    )
    if change.endswith("missing"):
        q.values.pop(key)
    elif change == "duplicate_claim":
        q.values[kind, "duplicate"] = deepcopy(q.values[key])
    elif change == "execution_active":
        key = next(key for key in q.values if key[0] == "TroshkaProject")
        q.values[key].setdefault("status", {})["originExecutions"] = {
            "pending": {"state": "active"}
        }
    else:
        q.values[key]["metadata"]["uid"] = "replacement"
    with pytest.raises((AccountingUnavailable, ValueError, KeyError)):
        q.advance()
    assert not q.calls


@pytest.mark.parametrize(
    "change", ["state", "cancelled", "io", "owner", "enrollment", "claim"]
)
def test_recovery_revalidates_after_independent_verification(
    origin_namespace_case, monkeypatch, change
):
    from app.models.native_deployment_origin import NativeDeploymentOriginActive
    from app.models.native_origin_custody import NativeOriginCustodyReceipt
    from app.services import native_origin_custody as custody

    q = origin_namespace_case
    legacy_missing_custody(q, monkeypatch)
    verify = custody.verify_configured

    def race(request):
        verified = verify(request)
        if change == "enrollment":
            extend_sibling_enrollment(q)
        elif change == "claim":
            key = next(key for key in q.values if key[0] == "PersistentVolumeClaim")
            q.values[key]["metadata"]["uid"] = "replacement"
        else:
            with q.sessions() as db:
                project = db.get(Project, q.project_id)
                pointer = db.get(NativeDeploymentOriginActive, q.project_id)
                if change == "state":
                    project.state = "active"
                elif change == "cancelled":
                    pointer.cancelled = False
                elif change == "io":
                    pointer.io_phase, pointer.io_token = (
                        "root",
                        "00000000-0000-4000-8000-000000000099",
                    )
                else:
                    from app.models.user import User

                    other = User(
                        display_name="other", email="retention-race@example.invalid"
                    )
                    db.add(other)
                    db.flush()
                    project.owner_id = other.id
                db.commit()
        return verified

    monkeypatch.setattr(custody, "verify_configured", race)
    with pytest.raises((AccountingUnavailable, ValueError)):
        q.advance()
    assert not q.calls
    with q.sessions() as db:
        assert db.query(NativeOriginCustodyReceipt).count() == 0


def test_normal_request_defers_capture_and_failed_capture_does_not_delete(
    origin_namespace_case, monkeypatch
):
    from app.models.native_deployment_origin import NativeDeploymentOriginActive
    from app.services import native_origin_custody as custody
    from app.services import native_teardown_map as teardown

    q = origin_namespace_case
    monkeypatch.setattr(
        custody,
        "verify_configured",
        lambda *args: (_ for _ in ()).throw(
            AccountingUnavailable("capture unavailable")
        ),
    )
    # Recovery fails; keep seal from authorizing teardown so delete stays blocked.
    monkeypatch.setattr(teardown, "seal", lambda db, api, work: work)
    monkeypatch.setattr(teardown, "map_sealed", lambda work: False)
    q.request()
    q.advance()
    with pytest.raises(AccountingUnavailable, match="capture unavailable"):
        q.advance()
    assert not q.calls
    with q.sessions() as db:
        assert db.get(NativeDeploymentOriginActive, q.project_id).cancelled
        assert cleanup.states(db, q.project_id)[0].phase == "fencing"


def test_missing_collection_pins_seals_map_and_skips_recover_on_retry(
    origin_namespace_case, monkeypatch
):
    """Lifecycle worker lacks /etc/troshka/collection pins; seal instead of hang."""
    from app.schemas.native_project_cleanup import StorageLiabilities
    from app.services import native_cleanup_storage as retention
    from app.services import native_teardown_map as teardown

    q = origin_namespace_case
    q.request()
    q.advance()
    recover_calls = []

    def missing_pins(db, api, work):
        recover_calls.append(work.phase)
        raise FileNotFoundError(
            "/etc/troshka/collection/ocpv01.dfw3-authorized-pins.json"
        )

    monkeypatch.setattr(retention, "recover", missing_pins)

    def seal_ok(db, api, work):
        liabilities = StorageLiabilities(unresolved=[])
        return cleanup._save(
            db,
            work,
            liabilities=liabilities,
            liability_digest=digest(liabilities.model_dump(mode="json")),
            last_error=None,
        )

    monkeypatch.setattr(teardown, "seal", seal_ok)
    monkeypatch.setattr(
        teardown,
        "map_sealed",
        lambda work: "storage_coverage_unavailable" not in work.liabilities.unresolved
        and "storage_coverage_incomplete" not in work.liabilities.unresolved,
    )
    work = q.advance()
    # Same tick continues past recover into VM/NS drain (no early return).
    assert work.phase in {"vm_delete_requested", "namespace_delete_requested"}
    assert recover_calls == ["fencing"]
    # Later ticks must not re-enter recover (would FileNotFoundError again).
    work = q.advance()
    assert recover_calls == ["fencing"]
    assert work.phase in {
        "vm_delete_requested",
        "namespace_delete_requested",
        "namespace_absent",
    }


def test_origin_null_domain_soft_seal_retains_inventory_for_orphan_proof(
    origin_namespace_case, monkeypatch
):
    """Origin without enrollment must still snapshot troshka finalizers before drain."""
    from app.schemas.native_project_cleanup import StorageLiabilities
    from app.services import native_cleanup_storage as retention
    from app.services import native_origin_custody as custody
    from app.services import native_teardown_map as teardown

    q = origin_namespace_case
    monkeypatch.setattr(custody, "retained", lambda *a, **k: None)

    owned = [v for (kind, _), v in q.values.items() if kind in OWN_KINDS]
    for own in owned:
        own["metadata"]["finalizers"] = ["troshka.redhat.com/finalizer"]

    def missing_pins(db, api, work):
        raise FileNotFoundError(
            "/etc/troshka/collection/ocpv01.dfw3-authorized-pins.json"
        )

    monkeypatch.setattr(retention, "recover", missing_pins)

    def seal_ok(db, api, work):
        liabilities = StorageLiabilities(unresolved=[])
        return cleanup._save(
            db,
            work,
            liabilities=liabilities,
            liability_digest=digest(liabilities.model_dump(mode="json")),
            last_error=None,
        )

    monkeypatch.setattr(teardown, "seal", seal_ok)
    monkeypatch.setattr(
        teardown,
        "map_sealed",
        lambda work: "storage_coverage_unavailable" not in work.liabilities.unresolved
        and "storage_coverage_incomplete" not in work.liabilities.unresolved,
    )

    delete = q.api.delete

    def terminating(kind, namespace, name, uid, rv, **kwargs):
        if kind not in OWN_KINDS:
            return delete(kind, namespace, name, uid, rv, **kwargs)
        q.values[kind, name]["metadata"]["deletionTimestamp"] = "2026-11-24T12:00:00Z"

    monkeypatch.setattr(q.api, "delete", terminating)

    q.request()
    with q.sessions() as db:
        work = cleanup.states(db, q.project_id)[0]
        assert work.authority.source_kind == "origin"
        assert work.authority.domain_id is None

    q.advance()
    work = q.advance()
    assert work.phase == "vm_delete_requested"
    with q.sessions() as db:
        rows = db.query(cleanup.Evidence).filter_by(cleanup_id=q.cleanup_id).all()
        inventory = next(p for p in rows if p.phase.startswith("inventory:"))
        assert any(
            "troshka.redhat.com/finalizer" in v.get("finalizers", [])
            for v in inventory.payload
        )

    for own in owned:
        own["metadata"]["finalizers"] = ["orphan"]
    assert q.advance().phase == "namespace_delete_requested"


def test_incomplete_retained_custody_cannot_authorize_deletion(origin_namespace_case):
    q = origin_namespace_case
    origin_support.publish(q, mode="enroll")
    q.request()
    q.advance()
    with pytest.raises(AccountingUnavailable, match="storage coverage"):
        q.advance()
    assert not q.calls


def test_recovery_cannot_forget_previously_inventoried_storage(
    origin_namespace_case, monkeypatch
):
    q = origin_namespace_case
    work = legacy_missing_custody(q, monkeypatch)
    old = dict(
        kind="PersistentVolumeClaim",
        name="lost-original",
        uid="original-uid",
        volume_name="old-pv",
    )
    with q.sessions() as db:
        cleanup._evidence(db, work, "inventory:" + digest([old])[:32], [old])
        db.commit()
    with pytest.raises(AccountingUnavailable, match="inventoried resource missing"):
        q.advance()
    assert not q.calls


def test_recovery_receipt_cannot_replay_after_adoption_or_into_other_cleanup(
    origin_namespace_case, monkeypatch
):
    from uuid import uuid4

    from app.services import native_origin_custody as custody

    q = origin_namespace_case
    legacy_missing_custody(q, monkeypatch)
    captured = []
    verify = custody.verify_configured

    def retain(request):
        value = verify(request)
        captured.append(value)
        return value

    monkeypatch.setattr(custody, "verify_configured", retain)
    q.advance()
    value = captured[0]
    with q.sessions() as db, pytest.raises((AccountingUnavailable, ValueError)):
        custody.apply(db, value)
    changed = value.prepared.model_copy(
        update={
            "cleanup_read_state": {
                **value.prepared.cleanup_read_state,
                "id": str(uuid4()),
            }
        }
    )
    value = value.model_copy(update={"prepared": changed})
    value = value.model_copy(
        update={"digest": digest(value.model_dump(mode="json", exclude={"digest"}))}
    )
    with q.sessions() as db, pytest.raises((AccountingUnavailable, ValueError)):
        custody.apply(db, value)
    assert not q.calls


def test_postgres_concurrent_retention_adopts_once(origin_namespace_case, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.models.native_origin_custody import NativeOriginCustodyReceipt
    from app.services import native_origin_custody as custody

    q = origin_namespace_case
    if q.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locking required")
    legacy_missing_custody(q, monkeypatch)
    verify = custody.verify_configured
    barrier = Barrier(2)

    def concurrent(request):
        value = verify(request)
        barrier.wait(timeout=15)
        return value

    monkeypatch.setattr(custody, "verify_configured", concurrent)

    def recover():
        try:
            return q.advance()
        except (AccountingUnavailable, ValueError):
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(recover) for _ in range(2)]
        outcomes = [future.result(timeout=30) for future in futures]
    assert sum(outcome is not None for outcome in outcomes) == 1
    assert not q.calls
    with q.sessions() as db:
        assert db.query(NativeOriginCustodyReceipt).count() == 1
        assert (
            db.query(cleanup.Evidence)
            .filter_by(cleanup_id=q.cleanup_id, phase="storage_retained")
            .count()
            == 1
        )


@pytest.mark.parametrize(
    "support_kind",
    ["Secret", "Pod", "exec", "exec_during_cleanup", "filepull", "exec_unknown"],
)
@pytest.mark.parametrize("mode", ["delete", "undeploy"])
def test_support_outcome_controls_namespace_storage_completion(
    origin_namespace_case, monkeypatch, support_kind, mode
):
    from datetime import UTC, datetime

    from app.services import native_origin_ops as ops
    from app.services import native_origin_remote as remote
    from app.services import native_origin_writers as writers
    from app.services.kubevirt_storage_reports import import_report
    from app.services.native_project_retirement import settle
    from tests.test_native_managed_resume import FixedDatetime
    from tests.test_native_origin_cleanup import rq_invocation
    from tests.test_native_origin_ops import body

    q = origin_namespace_case
    q.cleanup_mode = mode
    origin_support.publish(q, mode="complete")
    monkeypatch.setattr(FixedDatetime, "current", datetime.now(UTC))

    def post(kind, value):
        value = deepcopy(value)
        value["metadata"].update(uid="support-uid", resourceVersion="1")
        q.values[kind, value["metadata"]["name"]] = value
        return deepcopy(value)

    monkeypatch.setattr(q.api, "post", post, raising=False)

    def execute():
        if support_kind == "filepull":
            _successful_filepull(q, monkeypatch)
            return
        ops.create(
            q.project_id,
            q.api,
            body(q.intent, "Secret" if support_kind == "Secret" else "Pod"),
        )
        if support_kind.startswith("exec"):

            def invoke(argv):
                if support_kind == "exec_during_cleanup":
                    q.request()
                if support_kind == "exec_unknown":
                    raise TimeoutError("remote response lost")
                return "OK"

            assert remote.exec_command(q.project_id, q.api, ["true"], invoke) == "OK"

    _, _, run = rq_invocation(monkeypatch, execute, "support-execution")
    assert run() is (support_kind != "exec_unknown")
    if support_kind == "exec_unknown":
        q.request()
        q.advance()
        with pytest.raises(AccountingUnavailable, match="storage coverage"):
            q.advance()
        assert not q.calls
        with q.sessions() as db:
            work = cleanup.states(db, q.project_id)[0]
            assert work.phase == "fencing"
            assert any(
                v.startswith("support_outcome_unresolved:")
                for v in work.liabilities.unresolved
            )
            assert db.get(Project, q.project_id).state == "cleanup_pending"
        return
    scope, report = collect_absence(q)
    with q.sessions() as db:
        if support_kind != "Secret":
            with pytest.raises(AccountingUnavailable, match="remote operations"):
                writers.observe_terminal(db, str(q.intent.id))
        import_report(
            db,
            report,
            enrollment_id=q.enrollment_id,
            expected_enrollment_digest=q.digest,
            now=FixedDatetime.current,
        )
        db.commit()
        settle(db, q.cleanup_id)
    with q.sessions() as db:
        assert cleanup.states(db, q.project_id)[0].phase == "finalized"
        project = db.get(Project, q.project_id)
        assert project is None if mode == "delete" else project.state == "draft"


def _successful_filepull(q, monkeypatch):
    import base64
    from types import SimpleNamespace

    from app.services import native_origin_ops as ops
    from app.services.providers import kubevirt

    root = next(
        value for (kind, _), value in q.values.items() if kind == "TroshkaProject"
    )
    requested = []
    monkeypatch.setattr(q.api, "root", lambda *a: deepcopy(root), raising=False)
    monkeypatch.setattr(
        q.api, "request_file", lambda *a: requested.append(a), raising=False
    )
    monkeypatch.setattr(ops, "client", lambda _: q.api)

    def read(**kwargs):
        payload = json.loads(requested[0][-1]["troshka.redhat.com/file-pull-request"])
        return {
            "metadata": deepcopy(root["metadata"]),
            "status": {
                "filePull": {
                    "requestId": payload["requestId"],
                    "ready": True,
                    "contentB64": base64.b64encode(b"data").decode(),
                }
            },
        }

    custom = SimpleNamespace(get_namespaced_custom_object=read)
    monkeypatch.setattr(
        kubevirt, "_get_k8s_clients", lambda _: (custom, object(), object())
    )
    monkeypatch.setattr(kubevirt, "_project_ns", lambda *a: q.namespace)
    monkeypatch.setattr(kubevirt, "_resolve_vm_root_pvc", lambda *a: ("source-pvc", 20))
    assert (
        kubevirt.KubeVirtDriver().pull_file(
            SimpleNamespace(id=q.intent.provider_id),
            q.project_id,
            "vm-id",
            "/guest/file",
        )
        == b"data"
    )


@pytest.mark.parametrize("field", ["owner_id", "host_id", "provider_id"])
@pytest.mark.parametrize("when", ["request", "advance"])
def test_no_custody_origin_rejects_changed_current_identity(
    origin_namespace_case, field, when
):
    q = origin_namespace_case
    if when == "advance":
        q.request()
    with q.sessions() as db:
        project = db.get(Project, q.project_id)
        if field == "owner_id":
            from app.models.user import User

            user = User(display_name="other-owner", email="other@example.invalid")
            db.add(user)
            db.flush()
            project.owner_id = user.id
        elif field == "host_id":
            from app.models.host import Host

            original = db.get(Host, project.host_id)
            host = Host(provider_id=original.provider_id)
            db.add(host)
            db.flush()
            project.host_id = host.id
        else:
            from app.models.provider import Provider

            provider = Provider(name="other-provider", type="kubevirt")
            db.add(provider)
            db.flush()
            project.provider_id = provider.id
        db.commit()
    with pytest.raises(Exception, match="identity|provider"):
        q.request() if when == "request" else q.advance()
    assert not q.calls


def extend_sibling_enrollment(q):
    from uuid import UUID

    from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment
    from app.schemas.kubevirt_storage_enrollment import (
        DomainExtension,
        VerifiedEnrollment,
    )
    from app.services import kubevirt_storage_enrollment as enrollment
    from tests.test_native_managed_resume import FixedDatetime

    with q.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        previous = enrollment.validated_enrollment(
            db.get(NativeEnrollment, domain.enrollment_id)
        )
        source = db.get(Project, q.project_id)
        sibling = Project(
            name="extension-sibling",
            owner_id=source.owner_id,
            host_id=source.host_id,
            provider_id=source.provider_id,
        )
        db.add(sibling)
        db.flush()
        root = q.root.model_copy(
            update=dict(
                project_id=UUID(sibling.id),
                namespace="new-sibling",
                namespace_uid="new-sibling-namespace",
                project_cr_uid="new-sibling-root",
            )
        )
        pins = previous.pins.model_copy(
            update=dict(
                project_roots=[*previous.pins.project_roots, root],
                domain_extension=DomainExtension(
                    version="native-domain-extension-v1",
                    previous_enrollment_id=domain.enrollment_id,
                    previous_enrollment_digest=previous.digest,
                ),
            )
        )
        request = enrollment.prepare_native_enrollment(
            db, [str(r.project_id) for r in pins.project_roots]
        )
        manifest = VerifiedEnrollment(
            request=request,
            pins=pins,
            verified_at=FixedDatetime.current,
            digest="0" * 64,
        )
        manifest = manifest.model_copy(
            update={
                "digest": digest(manifest.model_dump(mode="json", exclude={"digest"}))
            }
        )
        enrolled = enrollment.enroll_native_roots(
            db, manifest, expected_target_id=str(request.target_id)
        )
        db.commit()
        return enrolled, manifest.digest


@pytest.mark.parametrize("authority_kind", ["admin", "origin"])
@pytest.mark.parametrize("when", ["before_request", "between_advances"])
def test_retained_cleanup_accepts_supported_sibling_extension(
    request, isolated_quota_database, authority_kind, when
):
    q = request.getfixturevalue(
        "origin_namespace_case" if authority_kind == "origin" else "namespace_case"
    )
    if authority_kind == "origin":
        origin_support.publish(q, mode="complete")
    if when == "before_request":
        live_id, _ = extend_sibling_enrollment(q)
    q.request()
    original = q.advance().authority
    if when == "between_advances":
        live_id, _ = extend_sibling_enrollment(q)
    assert q.advance().phase == "namespace_delete_requested"
    work = q.advance()
    assert work.phase == "namespace_absent" and work.authority == original
    assert str(original.enrollment_id) != live_id
    with q.sessions() as db:
        # This validates the chain again while a pending cleanup is projected;
        # it used to recurse via settled_root_keys -> storage_scopes -> retained.
        from app.services.quota_cleanup import settled_root_keys

        assert settled_root_keys(db) == set()
        scopes = cleanup.storage_scopes(db)
        assert len(scopes) == 1 and scopes[0].root == original.root


@pytest.mark.parametrize("authority_kind", ["admin", "origin"])
def test_sibling_extension_during_unlocked_io_requires_fresh_retry(
    request, isolated_quota_database, authority_kind, monkeypatch
):
    q = request.getfixturevalue(
        "origin_namespace_case" if authority_kind == "origin" else "namespace_case"
    )
    if authority_kind == "origin":
        origin_support.publish(q, mode="complete")
    q.request()
    original = q.advance().authority
    get = q.api.get
    extensions = []

    def extend_during_read(kind, namespace, name):
        if kind == "Namespace" and name == q.namespace and not extensions:
            extensions.append(extend_sibling_enrollment(q))
        return get(kind, namespace, name)

    monkeypatch.setattr(q.api, "get", extend_during_read)
    with pytest.raises(
        AccountingUnavailable, match="live enrollment changed during unlocked I/O"
    ):
        q.advance()
    assert extensions and not q.calls
    assert q.advance().phase == "namespace_delete_requested"
    work = q.advance()
    assert work.phase == "namespace_absent" and work.authority == original
    assert work.enrollment_boundary == dict(
        enrollment_id=extensions[0][0], digest=extensions[0][1]
    )


@pytest.mark.parametrize("authority_kind", ["admin", "origin"])
def test_retained_cleanup_rejects_unrelated_enrollment(
    request, isolated_quota_database, authority_kind
):
    from uuid import uuid4

    from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment
    from app.services.kubevirt_storage_enrollment import validated_enrollment

    q = request.getfixturevalue(
        "origin_namespace_case" if authority_kind == "origin" else "namespace_case"
    )
    if authority_kind == "origin":
        origin_support.publish(q, mode="complete")
    q.request()
    q.advance()
    extend_sibling_enrollment(q)
    with q.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        manifest = validated_enrollment(db.get(NativeEnrollment, domain.enrollment_id))
        manifest = manifest.model_copy(
            update={"pins": manifest.pins.model_copy(update={"domain_extension": None})}
        )
        manifest = manifest.model_copy(
            update={
                "digest": digest(manifest.model_dump(mode="json", exclude={"digest"}))
            }
        )
        unrelated = NativeEnrollment(
            id=str(uuid4()),
            domain_id=domain.id,
            digest=manifest.digest,
            payload=manifest.model_dump(mode="json"),
        )
        db.add(unrelated)
        db.flush()
        domain.enrollment_id = unrelated.id
        db.commit()
    with pytest.raises(
        ValueError,
        match="without a supported domain extension|no exact protected pins anchor",
    ):
        q.advance()
    assert not q.calls
