"""Online custody is read-only; later cleanup still requires real managed proof."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest

from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment
from app.models.native_custody import NativeCustodyReceipt
from app.models.project import Project
from app.models.quota import QuotaAllocation, QuotaReservation
from app.schemas.kubevirt_storage import ReadResult
from app.schemas.quota import AccountingUnavailable
from app.services import native_online_enrollment as online
from app.services import quota_cleanup
from tests.test_native_cleanup_completion import cleanup_case as cleanup_fixture
from tests.test_native_cleanup_completion import deploy_case as deploy_fixture
from tests.test_native_cleanup_completion import (
    isolated_quota_database as database_fixture,
)
from tests.test_native_cleanup_completion import native_ledger as ledger_fixture
from tests.test_native_cleanup_completion import resume_case as resume_fixture
from tests.test_native_managed_resume import FixedDatetime

cleanup_case = cleanup_fixture
deploy_case = deploy_fixture
isolated_quota_database = database_fixture
native_ledger = ledger_fixture
resume_case = resume_fixture


@pytest.fixture
def online_case(cleanup_case, monkeypatch):
    from app.services.native_managed_power import helpers

    q = cleanup_case
    monkeypatch.setattr(online.accounting, "quotas_enabled", lambda: False)
    monkeypatch.setattr(online, "datetime", FixedDatetime)
    monkeypatch.setattr(online.enrollment, "datetime", FixedDatetime)
    for api in q.apis.values():
        root = next(
            value for (kind, _), value in api.values.items() if kind == "TroshkaProject"
        )
        root["metadata"].get("annotations", {}).pop(helpers().ANNOTATION, None)
        root.get("status", {}).pop("managedPower", None)
        rows = {
            kind: api.rows(kind, root["metadata"]["namespace"])
            for kind in helpers().INVENTORY
        }
        for pod in helpers().container_manifests(root, rows).values():
            pod["metadata"].update(
                uid=pod["metadata"]["name"] + "-legacy", resourceVersion="1"
            )
            pod["status"] = {"phase": "Running"}
            api.values["Pod", pod["metadata"]["name"]] = pod
    with q.sessions() as db:
        db.query(QuotaAllocation).delete()
        db.query(QuotaReservation).delete()
        for project_id in q.project_ids:
            project = db.get(Project, project_id)
            project.state = "active"
            # Source evidence uses the fixture clock, not a skewed container clock.
            project.updated_at = FixedDatetime.current
            project.deployed_topology = deepcopy(
                q.apis[project_id].rows("TroshkaProject", None)[0]["spec"]["topology"]
            )
        db.commit()
        q.prepared = online.prepare(db, q.project_ids)
        q.online_pins = q.pins
        db.commit()
    q.attestation = {
        "administrator": "admin@test.invalid",
        "reason": "Reviewed dedicated legacy namespaces",
        "namespace_custody": True,
    }

    class Reader:
        def get(self, kind, namespace, name):
            if kind == "Namespace":
                api = (
                    next(iter(q.apis.values()))
                    if name == "kube-system"
                    else self.api(name)
                )
            elif kind == "PersistentVolume":
                api = self.current
            else:
                api = self.api(namespace)
            return api.get(kind, namespace, name)

        def rows(self, kind, namespace):
            return self.api(namespace).rows(kind, namespace)

        def cluster_identity(self):
            return "cluster"

        def namespace(self, namespace):
            if namespace == "trusted-cache":
                return {"metadata": {"uid": "cache-ns"}}
            api = self.api(namespace)
            return api.get("Namespace", None, namespace)

        def api(self, namespace):
            root = next(r for r in q.report.project_roots if r.namespace == namespace)
            self.current = q.apis[str(root.project_id)]
            return self.current

        def projects(self, namespace):
            return self.api(namespace).rows("TroshkaProject", namespace)

        def pvs(self):
            result = []
            for index, api in enumerate(q.apis.values()):
                pv = deepcopy(api.values["PersistentVolume", "retained-pv"])
                pv["metadata"]["name"] = f"retained-pv-{index}"
                result.append(pv)
            return result

        def pvcs(self, namespace):
            api = self.api(namespace)
            index = list(q.apis.values()).index(api)
            result = deepcopy(api.rows("PersistentVolumeClaim", namespace))
            for pvc in result:
                pvc["spec"]["volumeName"] = f"retained-pv-{index}"
                pvc["status"] = {"phase": "Bound"}
            return result

        def resolve_csi(self, driver, handle, **kwargs):
            binding = next(
                b for b in q.report.volume_bindings if b.volume_handle == handle
            )
            return ReadResult(
                status="complete",
                collected_at=FixedDatetime.current,
                value=binding.image_key.model_dump(mode="json"),
            )

    class Backend:
        def image_info(self, key):
            return ReadResult(
                status="complete",
                collected_at=FixedDatetime.current,
                value={"id": key.image_id},
            )

        def image_snapshots(self, key):
            return ReadResult(
                status="complete", collected_at=FixedDatetime.current, value=[]
            )

        def cluster_identity(self):
            return ReadResult(
                status="complete",
                collected_at=FixedDatetime.current,
                value=q.pins.pool_keys[0].ceph_fsid,
            )

        def pool_identity(self):
            return ReadResult(
                status="complete",
                collected_at=FixedDatetime.current,
                value=[{"poolnum": p.pool_id} for p in q.pins.pool_keys],
            )

    q.reader = Reader()
    monkeypatch.setattr(online, "ReadOnlyManaged", lambda kube: kube)
    q.readers = {str(q.pins.provider_clusters[0].provider_id): q.reader}
    q.backend = Backend()
    return q


def verified(q):
    return online.verify(
        q.prepared, q.readers, q.backend, pins=q.online_pins, attestation=q.attestation
    )


def apply_receipt(q):
    value = verified(q)
    with q.sessions() as db:
        ids = online.apply(db, value, expected_target_id=q.target_id)
        db.commit()
        q.enrollment_id = db.get(
            NativeCollectionDomain, str(q.pins.domain_id)
        ).enrollment_id
        q.digest = db.get(NativeEnrollment, q.enrollment_id).digest
    return ids


def test_online_enrollment_preserves_live_workloads_and_accounting(online_case):
    q = online_case
    before = {key: deepcopy(api.values) for key, api in q.apis.items()}
    ids = apply_receipt(q)
    assert len(ids) == len(q.project_ids)
    assert before == {key: api.values for key, api in q.apis.items()}
    assert all(not api.calls for api in q.apis.values())
    with q.sessions() as db:
        assert db.query(QuotaReservation).count() == 0
        assert db.query(QuotaAllocation).count() == 0
        assert db.query(NativeCustodyReceipt).count() == len(q.project_ids)
        assert {db.get(Project, key).state for key in q.project_ids} == {"active"}


def test_reconcile_preserves_existing_receipt_and_adds_missing_project(online_case):
    q = online_case
    apply_receipt(q)
    with q.sessions() as db:
        rows = db.query(NativeCustodyReceipt).order_by(NativeCustodyReceipt.id).all()
        kept_id, kept_payload = rows[0].id, deepcopy(rows[0].payload)
        db.delete(rows[1])
        q.prepared = online.prepare(db, q.project_ids)
        db.commit()
    value = verified(q)
    with q.sessions() as db:
        online.apply(db, value, expected_target_id=q.target_id, preserve_existing=True)
        db.commit()
        assert db.get(NativeCustodyReceipt, kept_id).payload == kept_payload
        assert db.query(NativeCustodyReceipt).count() == len(q.project_ids)
        assert db.query(QuotaReservation).count() == 0
    assert all(not api.calls for api in q.apis.values())


def test_reconcile_rejects_changed_existing_storage_identity(online_case):
    q = online_case
    apply_receipt(q)
    with q.sessions() as db:
        q.prepared = online.prepare(db, q.project_ids)
        db.commit()
    value = verified(q)
    value["bindings"][0]["pvc_uid"] = "replaced-pvc"
    value["digest"] = online.enrollment.digest(
        {k: v for k, v in value.items() if k != "digest"}
    )
    with q.sessions() as db:
        with pytest.raises(
            AccountingUnavailable, match="Existing custody evidence changed"
        ):
            online.apply(
                db, value, expected_target_id=q.target_id, preserve_existing=True
            )
        db.rollback()


@pytest.mark.parametrize(
    "change", ["namespace", "root", "generation", "topology", "pvc", "pool"]
)
@pytest.mark.parametrize("reviewed", [False, True])
def test_online_rejects_changed_independent_identity(
    online_case, change, monkeypatch, reviewed
):
    q = online_case
    if reviewed:
        reviewed_topology_case(q)
    api = q.apis[q.project_ids[0]]
    root = api.rows("TroshkaProject", None)[0]
    if change == "namespace":
        api.values["Namespace", root["metadata"]["namespace"]]["metadata"][
            "uid"
        ] = "replacement"
    elif change == "root":
        api.values["TroshkaProject", root["metadata"]["name"]]["metadata"][
            "uid"
        ] = "replacement"
    elif change == "generation":
        api.values["TroshkaProject", root["metadata"]["name"]]["metadata"][
            "generation"
        ] += 1
    elif change == "topology":
        api.values["TroshkaProject", root["metadata"]["name"]]["spec"]["topology"] = {}
    elif change == "pvc":
        pvcs = q.reader.pvcs
        calls = []

        def changing(namespace):
            values = pvcs(namespace)
            calls.append(namespace)
            if len(calls) > 1:
                values[0]["metadata"]["uid"] = "replacement"
            return values

        monkeypatch.setattr(q.reader, "pvcs", changing)
    else:
        monkeypatch.setattr(
            q.backend,
            "pool_identity",
            lambda: ReadResult(
                status="complete", collected_at=FixedDatetime.current, value=[]
            ),
        )
    with pytest.raises((ValueError, AccountingUnavailable)):
        verified(q)
    assert all(not api.calls for api in q.apis.values())


@pytest.mark.parametrize("change", ["owner", "topology", "stale", "quotas"])
@pytest.mark.parametrize("reviewed", [False, True])
def test_apply_rechecks_prepared_state(online_case, change, monkeypatch, reviewed):
    q = online_case
    if reviewed:
        reviewed_topology_case(q)
    value = verified(q)
    with q.sessions() as db:
        project = db.get(Project, q.project_ids[0])
        if change == "owner":
            project.owner_id = q.admin_id
            db.commit()
        elif change == "topology":
            project.deployed_topology = {}
            db.commit()
        elif change == "stale":
            FixedDatetime.current += timedelta(seconds=61)
        else:
            monkeypatch.setattr(online.accounting, "quotas_enabled", lambda: True)
        with pytest.raises((ValueError, AccountingUnavailable)):
            online.apply(db, value, expected_target_id=q.target_id)
        db.rollback()
        assert db.query(NativeCustodyReceipt).count() == 0


def test_long_read_only_scan_freshness_starts_after_final_identity_checks(
    online_case, monkeypatch
):
    from app.services import native_ancestry_custody

    q = online_case
    start = FixedDatetime.current
    original = native_ancestry_custody.observe

    def slow_scan(*args, **kwargs):
        result = original(*args, **kwargs)
        FixedDatetime.current += timedelta(seconds=120)
        return result

    monkeypatch.setattr(native_ancestry_custody, "observe", slow_scan)
    value = verified(q)
    receipt = online._validated(value)
    assert receipt.enrollment.verified_at == start + timedelta(seconds=120)
    with q.sessions() as db:
        assert online.apply(db, value, expected_target_id=q.target_id)
        db.rollback()
        FixedDatetime.current += timedelta(seconds=61)
        with pytest.raises(AccountingUnavailable, match="fresh"):
            online.apply(db, value, expected_target_id=q.target_id)


def test_helper_replacement_during_ancestry_scan_rejects_receipt(
    online_case, monkeypatch
):
    from app.services import native_ancestry_custody
    from app.services.native_managed_power import helpers

    module = helpers()
    from helpers.vnc import build_vnc_rbac

    q = online_case
    api = q.apis[q.project_ids[0]]
    ns = q.report.project_roots[0].namespace
    for value in build_vnc_rbac(ns):
        value["metadata"].update(uid=value["kind"] + "-original", resourceVersion="1")
        api.values[value["kind"], value["metadata"]["name"]] = value
    original = native_ancestry_custody.observe

    def replacing_scan(*args, **kwargs):
        result = original(*args, **kwargs)
        FixedDatetime.current += timedelta(seconds=120)
        api.values["Role", "troshka-vnc-access"]["metadata"]["uid"] = "replacement"
        return result

    monkeypatch.setattr(native_ancestry_custody, "observe", replacing_scan)
    with pytest.raises(module.ManagedPowerError, match="custody_support_changed"):
        verified(q)
    assert all(not api.calls for api in q.apis.values())
    with q.sessions() as db:
        assert db.query(NativeCustodyReceipt).count() == 0


@pytest.mark.parametrize("reviewed", [False, True])
def test_legacy_cleanup_uses_real_stop_and_exact_storage_graph(online_case, reviewed):
    from app.services.kubevirt_storage_reports import import_report
    from tests.test_quota_native_storage import revised

    q = online_case
    if reviewed:
        reviewed_topology_case(q)
    apply_receipt(q)
    with q.sessions() as db:
        import_report(
            db,
            revised(q.report, report_id=uuid4()),
            enrollment_id=q.enrollment_id,
            expected_enrollment_digest=q.digest,
            now=FixedDatetime.current,
        )
        db.commit()
        project = db.get(Project, q.project_ids[0])
        # Ordinary quota-off lifecycle does not change the root CR/topology.
        project.updated_at = FixedDatetime.current + timedelta(seconds=1)
        db.commit()
        intent = quota_cleanup.prepare_native(db, project, "undeploy")
        assert intent.phase == "stopping"
        rows = online.operations._rows(db, project.id)
        assert len(rows) == 1
        assert rows[0].dispatch_metadata["cleanup_only"]
        assert "native_deploy" not in rows[0].dispatch_metadata
        assert not quota_cleanup._already_off(rows)
        db.commit()
    quota_cleanup.reconcile_native(q.project_ids[0], str(intent.intent_id))
    with q.sessions() as db:
        current = quota_cleanup.native_intent(db, q.project_ids[0])
        from app.models.quota import QuotaAction

        action = db.get(QuotaAction, current.stop_action_id)
        import json

        assert current.stop_proof, json.dumps(
            {
                "root": q.apis[q.project_ids[0]].rows("TroshkaProject", None)[0],
                "errors": q.errors,
                "state": action.state,
                "error": action.last_error,
                "details": action.details,
            },
            indent=2,
        )
        assert current.objects
        assert db.get(Project, q.project_ids[0]).state == "cleanup_pending"


@pytest.mark.parametrize("mode", ["delete", "undeploy"])
def test_legacy_owner_cleanup_requires_exact_final_settlement(
    online_case, monkeypatch, mode
):
    from tests.test_native_cleanup_completion import (
        test_cleanup_worker_waits_for_original_resource_and_physical_settlement,
    )

    q = online_case
    apply_receipt(q)
    test_cleanup_worker_waits_for_original_resource_and_physical_settlement(
        q, monkeypatch, mode, "quotas_disabled"
    )


@pytest.mark.parametrize("kind", ["Secret", "ConfigMap", "Service", "Role"])
def test_foreign_namespace_objects_rejected_before_enrollment(online_case, kind):
    q = online_case
    api = q.apis[q.project_ids[0]]
    namespace = q.report.project_roots[0].namespace
    api.values[kind, "foreign"] = {
        "kind": kind,
        "apiVersion": "v1",
        "metadata": {
            "name": "foreign",
            "namespace": namespace,
            "uid": "foreign",
            "resourceVersion": "1",
        },
    }
    with pytest.raises(ValueError):
        verified(q)
    assert not api.calls


def test_troshkad_cleanup_never_enters_native_custody(monkeypatch):
    from types import SimpleNamespace

    from app.services import quota_enforcement

    monkeypatch.setattr(quota_enforcement.accounting, "quotas_enabled", lambda: False)
    monkeypatch.setattr(
        quota_enforcement.operations, "select_adapter", lambda *_: "libvirt"
    )
    monkeypatch.setattr(
        quota_enforcement,
        "_lock_action_project",
        lambda *_: pytest.fail("troshkad entered native custody cleanup"),
    )
    assert (
        quota_enforcement.request_native_stop(
            object(), SimpleNamespace(id="troshkad"), for_cleanup=True
        )
        is None
    )


def test_new_foreign_object_blocks_cleanup_before_stop(online_case):
    q = online_case
    apply_receipt(q)
    with q.sessions() as db:
        intent = quota_cleanup.prepare_native(
            db, db.get(Project, q.project_ids[0]), "delete"
        )
        db.commit()
    api = q.apis[q.project_ids[0]]
    api.values["Secret", "foreign"] = {
        "kind": "Secret",
        "apiVersion": "v1",
        "metadata": {
            "name": "foreign",
            "namespace": intent.root.namespace,
            "uid": "foreign",
            "resourceVersion": "1",
        },
    }
    quota_cleanup.reconcile_native(q.project_ids[0], str(intent.intent_id))
    assert not api.calls
    with q.sessions() as db:
        assert quota_cleanup.native_intent(db, q.project_ids[0]).phase == "stopping"


def test_legacy_console_and_s3_enroll_read_only_and_replacement_blocks_stop(
    online_case,
):
    import json

    from app.services.native_managed_power import helpers

    helpers()
    from helpers.vnc import build_vnc_rbac

    q = online_case
    project_id = q.project_ids[0]
    api = q.apis[project_id]
    ns = q.report.project_roots[0].namespace
    values = list(build_vnc_rbac(ns)) + [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "troshka-vnc", "namespace": ns},
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "s3-credentials", "namespace": ns},
            "type": "Opaque",
            "data": {
                k: "never-record-this-secret"
                for k in (
                    "accessKeyId",
                    "secretKey",
                    "AWS_ACCESS_KEY_ID",
                    "AWS_SECRET_ACCESS_KEY",
                    "AWS_DEFAULT_REGION",
                )
            },
        },
    ]
    for value in values:
        value["metadata"].update(
            uid=value["kind"] + "-" + value["metadata"]["name"], resourceVersion="1"
        )
        api.values[value["kind"], value["metadata"]["name"]] = value
    before = deepcopy(api.values)
    receipt = verified(q)
    assert "never-record-this-secret" not in json.dumps(receipt)
    apply_receipt(q)
    assert api.values == before and not api.calls
    with q.sessions() as db:
        intent = quota_cleanup.prepare_native(db, db.get(Project, project_id), "delete")
        db.commit()
    api.values["Role", "troshka-vnc-access"]["metadata"]["uid"] = "replacement"
    quota_cleanup.reconcile_native(project_id, str(intent.intent_id))
    assert not api.calls


def test_cleanup_anchor_is_not_validated_zero_quota_usage(online_case):
    from app.services.quota_admission import usage

    q = online_case
    apply_receipt(q)
    with q.sessions() as db:
        project = db.get(Project, q.project_ids[0])
        rows = online.cleanup_anchor(db, project, [])
        assert rows[0].dispatch_metadata["kind"] == "custody"
        with pytest.raises(
            AccountingUnavailable, match="not verified quota accounting"
        ):
            usage(db, "project", project.id)


def test_native_finalization_projection_retains_runtime_evidence(online_case):
    q = online_case
    api = q.apis[q.project_ids[0]]
    root = next(v for (kind, _), v in api.values.items() if kind == "TroshkaProject")
    vm = next(n for n in root["spec"]["topology"]["nodes"] if n["type"] == "vmNode")
    vm["data"]["ciGeneratedUserData"] = "generated-runtime-data"
    storage = {
        "id": "storage-metadata",
        "type": "storageNode",
        "data": {"resolvedS3Path": "resolved/source"},
    }
    root["spec"]["topology"]["nodes"].append(storage)
    # Test the exact finalization projection separately: storage identities are
    # still verified independently against PVC/PV and CSI immutable image IDs.
    persisted = deepcopy(root["spec"]["topology"])
    for node in persisted["nodes"]:
        node["data"].pop("ciGeneratedUserData", None)
        node["data"].pop("resolvedS3Path", None)
        if node["type"] == "vmNode":
            node["data"]["domainUuid"] = "observed-domain"
    assert online.canonical_topology(persisted) == online.canonical_topology(
        root["spec"]["topology"]
    )
    changed = deepcopy(persisted)
    changed["nodes"][0]["data"]["cpu"] = 99
    assert online.canonical_topology(changed) != online.canonical_topology(
        root["spec"]["topology"]
    )


@pytest.mark.parametrize("value", [1, "true", False, None])
def test_custody_requires_explicit_boolean_attestation(value):
    from app.schemas.native_online_enrollment import CustodyAttestation

    with pytest.raises(ValueError):
        CustodyAttestation(
            administrator="operator", reason="reviewed", namespace_custody=value
        )


def reviewed_topology_case(q):
    project_id = q.project_ids[0]
    root = next(
        v
        for (kind, _), v in q.apis[project_id].values.items()
        if kind == "TroshkaProject"
    )
    root["spec"]["topology"]["workloadsDone"] = True
    q.attestation["topology_reviews"] = {
        project_id: {
            "saved_digest": online.enrollment.digest(
                q.prepared["topologies"][project_id]
            ),
            "live_digest": online.enrollment.digest(root["spec"]["topology"]),
        }
    }
    return project_id, root


def test_reviewed_topology_preserves_both_versions_and_provider(online_case):
    q = online_case
    project_id, root = reviewed_topology_case(q)
    before = deepcopy(q.apis[project_id].values)
    receipt = verified(q)
    assert receipt["runtime_topologies"][project_id] == root["spec"]["topology"]
    assert receipt["prepared"]["topologies"] == q.prepared["topologies"]
    with q.sessions() as db:
        online.apply(db, receipt, expected_target_id=q.target_id)
        db.commit()
        assert (
            db.get(Project, project_id).deployed_topology
            == q.prepared["topologies"][project_id]
        )
    assert q.apis[project_id].values == before
    assert not q.apis[project_id].calls


@pytest.mark.parametrize("side", ["saved_digest", "live_digest"])
def test_reviewed_topology_rejects_wrong_digest(online_case, side):
    q = online_case
    project_id, _ = reviewed_topology_case(q)
    q.attestation["topology_reviews"][project_id][side] = "0" * 64
    with pytest.raises(AccountingUnavailable, match="topology"):
        verified(q)


def test_reviewed_topology_rejects_later_runtime_change(online_case):
    q = online_case
    _, root = reviewed_topology_case(q)
    root["spec"]["topology"]["workloadsDone"] = False
    with pytest.raises(AccountingUnavailable, match="topology"):
        verified(q)


def test_topology_review_cannot_name_another_project(online_case):
    q = online_case
    project_id, _ = reviewed_topology_case(q)
    q.attestation["topology_reviews"][str(uuid4())] = q.attestation["topology_reviews"][
        project_id
    ]
    with pytest.raises(AccountingUnavailable, match="review"):
        verified(q)


def test_legacy_attestation_serialization_preserves_digest():
    from app.schemas.native_online_enrollment import CustodyAttestation

    value = {
        "administrator": "operator",
        "reason": "reviewed",
        "namespace_custody": True,
    }
    assert CustodyAttestation.model_validate(value).model_dump(mode="json") == value


def test_receipt_validation_rechecks_review_even_with_recomputed_outer_digest(
    online_case,
):
    q = online_case
    project_id, _ = reviewed_topology_case(q)
    receipt = verified(q)
    receipt["runtime_topologies"][project_id]["workloadsDone"] = False
    receipt["digest"] = online.enrollment.digest(
        {key: value for key, value in receipt.items() if key != "digest"}
    )
    with pytest.raises(AccountingUnavailable, match="topology"):
        online._validated(receipt)
