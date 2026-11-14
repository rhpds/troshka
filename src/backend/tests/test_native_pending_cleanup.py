"""Interrupted initialization cancels without replaying its ambiguous create."""

from copy import deepcopy

import pytest

from tests.test_native_cleanup_completion import deploy_case as deploy_case_fixture
from tests.test_native_cleanup_completion import (
    isolated_quota_database as isolated_fixture,
)
from tests.test_native_cleanup_completion import native_ledger as ledger_fixture
from tests.test_native_cleanup_completion import resume_case as resume_case_fixture
from tests.test_native_managed_deploy import (
    advance_clock,
    current_target,
    report_for,
    start,
)

deploy_case = deploy_case_fixture
isolated_quota_database = isolated_fixture
resume_case = resume_case_fixture
native_ledger = ledger_fixture


@pytest.fixture
def pending_case(deploy_case, monkeypatch):
    from app.services import native_managed_collector as collector
    from app.services import native_managed_deploy as deploy
    from tests.test_native_managed_resume import FixedDatetime

    q = deploy_case
    from app.models.project import Project

    with q.sessions() as db:
        project = db.get(Project, q.new_id)
        topology = deepcopy(project.topology)
        topology["nodes"][0] = {
            "id": "vm-one",
            "type": "vmNode",
            "data": {"vcpus": 2, "ram": 1},
        }
        topology["edges"] = [{"source": "new-disk", "target": "vm-one"}]
        project.topology = topology
        db.commit()
    q.operation_id = start(q)
    advance_clock()
    collector.cycle(
        q.sessions,
        q.pins,
        q.target_id,
        q.report.collector_id,
        {q.provider_id: q.reader},
        q.backend,
        lambda manifest, previous, started: report_for(q, manifest),
    )
    deploy.reconcile(q.new_id)
    deploy.reconcile(q.new_id)
    api = q.apis[q.new_id]
    original = api.request

    def interrupted(method, kind, namespace, name, body):
        if method == "POST" and kind == "PersistentVolumeClaim":
            raise TimeoutError("original create response unknown")
        if method == "DELETE":
            value = api.values[kind, name]
            assert body["preconditions"]["uid"] == value["metadata"]["uid"]
            assert (
                body["preconditions"]["resourceVersion"]
                == value["metadata"]["resourceVersion"]
            )
            api.calls.append((method, (kind, name), deepcopy(body)))
            if kind == "TroshkaProject":
                value["metadata"]["generation"] += 1
                value["metadata"][
                    "deletionTimestamp"
                ] = FixedDatetime.current.isoformat()
            else:
                del api.values[kind, name]
            return {"kind": "Status", "status": "Success"}
        return original(method, kind, namespace, name, body)

    api.request = interrupted
    api.find = lambda kind, namespace, name: deepcopy(api.values.get((kind, name)))
    root = api.rows("TroshkaProject", None)[0]
    wanted = deploy.helpers().request(root)
    child = next(
        v
        for v in deploy.helpers().render(root, wanted["storageClass"])
        if v["kind"] == "TroshkaVM"
    )
    deploy.helpers().ensure_resource(
        api, root, deploy.helpers().state_for(root, wanted), wanted, child
    )
    with pytest.raises(TimeoutError):
        deploy.helpers().reconcile(api, api.rows("TroshkaProject", None)[0])
    q.queued = []
    previous_client = deploy.power.with_client
    from app.schemas.kubevirt_storage import NativeCleanupIntent

    monkeypatch.setattr(
        deploy.power,
        "with_client",
        lambda provider, fn, *args: (
            fn(api, *args)
            if isinstance(args[0], NativeCleanupIntent)
            else previous_client(provider, fn, *args)
        ),
    )
    monkeypatch.setattr(
        "app.core.redis.enqueue_job",
        lambda fn, *args, **kwargs: q.queued.append((fn, args)),
    )
    return q


def request(q):
    from app.api import projects
    from app.models.user import User

    with q.sessions() as db:
        return projects.undeploy_project(q.new_id, db.get(User, q.owner_id), db)


def test_pending_cancellation_persists_journal_and_requires_operator_drain(
    pending_case,
):
    from app.models.project import Project
    from app.services import native_managed_deploy as deploy
    from app.services import quota_cleanup as cleanup

    q = pending_case
    api = q.apis[q.new_id]
    root = api.rows("TroshkaProject", None)[0]
    journal = deepcopy(root["status"]["managedDeployment"])
    topology = deepcopy(
        current_target(q, q.operation_id)["payload"]["spec"]["topology"]
    )
    result = request(q)
    assert result["status"] == "cleanup_pending"
    fn, args = q.queued.pop()
    fn(*args)
    assert not any(c[0] == "DELETE" for c in api.calls)
    deploy.reconcile(q.new_id)
    assert api.rows("TroshkaProject", None)[0]["status"]["managedDeployment"] == journal
    deploy.power.helpers().reconcile_stop(api, api.rows("TroshkaProject", None)[0])
    from app.services import native_pending_cleanup as pending

    with q.sessions() as db:
        intent = cleanup.native_intent(db, q.new_id)
        assert pending.observe(api, intent) is not None, api.rows(
            "TroshkaProject", None
        )[0]
        pending.seal(api, intent)
    fn(*args)
    with q.sessions() as db:
        intent = cleanup.native_intent(db, q.new_id)
        assert intent.phase == "pending", intent.last_error
        assert intent.pending_initialization["journal"]["creates"] == journal["creates"]
        project = db.get(Project, q.new_id)
        assert project.state == "cleanup_pending"
        assert project.owner_id == q.owner_id and project.topology == topology
    assert sum(c[0] == "DELETE" for c in api.calls) == 1
    fn(*args)
    assert sum(c[0] == "DELETE" for c in api.calls) == 1


def test_pending_cancel_waits_for_deploy_guard_and_stale_advance_stays_cancelled(
    pending_case,
):
    from types import SimpleNamespace

    from app.services import native_managed_deploy as deploy
    from app.services.quota_enforcement import _execution_guard

    q = pending_case
    api = q.apis[q.new_id]
    stale = current_target(q, q.operation_id)
    with q.sessions() as guard:
        with _execution_guard(guard, "native-deploy:" + q.new_id) as acquired:
            assert acquired
            request(q)
            fn, args = q.queued.pop()
            before = deepcopy(api.calls)
            fn(*args)
            assert api.calls == before
    deploy.advance(q.new_id, q.operation_id, stale, q.profile, SimpleNamespace())
    assert api.calls == before
    fn(*args)
    assert (
        deploy.power.helpers().intent(api.rows("TroshkaProject", None)[0])["mode"]
        == "stopped"
    )


@pytest.mark.parametrize(
    "change",
    ["worker", "runtime", "owner", "host", "payload", "profile", "plan", "generation"],
)
def test_pending_undeploy_rejects_changed_original_before_mutation(
    pending_case, change
):
    from fastapi import HTTPException

    from app.models.project import Project
    from app.models.quota import QuotaReservation

    q = pending_case
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=q.operation_id).one()
        metadata = deepcopy(row.dispatch_metadata)
        target = metadata["native_deploy"]
        if change == "worker":
            metadata["worker_finished"] = False
        elif change == "runtime":
            target["phase"] = "runtime-intent"
        elif change == "owner":
            target["owner_id"] = "other-owner"
        elif change == "host":
            db.get(Project, q.new_id).host_id = None
        elif change == "generation":
            metadata["generation_id"] = "other-generation"
        else:
            target[change + "_digest"] = "0" * 64
            if change == "plan":
                target["initialization_request"]["planDigest"] = "0" * 64
        row.dispatch_metadata = metadata
        db.commit()
    calls = deepcopy(q.apis[q.new_id].calls)
    with pytest.raises(HTTPException):
        request(q)
    assert q.apis[q.new_id].calls == calls
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=q.operation_id).one()
        assert not row.dispatch_metadata.get("cancelled")


def test_pending_undeploy_accepts_stale_measurement_without_releasing_resources(
    pending_case,
):
    from datetime import timedelta

    from app.models.project import Project
    from app.services import quota_cleanup as cleanup
    from tests.test_native_managed_resume import FixedDatetime

    q = pending_case
    FixedDatetime.current += timedelta(minutes=20)
    first = request(q)
    second = request(q)
    assert first["cleanup_id"] == second["cleanup_id"]
    with q.sessions() as db:
        assert cleanup.native_intent(db, q.new_id).phase == "stopping"
        assert db.get(Project, q.new_id).state == "cleanup_pending"


@pytest.mark.parametrize("change", ["replacement", "foreign", "disk", "journal"])
def test_pending_inventory_changes_do_not_delete(pending_case, change):
    from app.services import native_managed_deploy as deploy
    from app.services import quota_cleanup as cleanup

    q = pending_case
    api = q.apis[q.new_id]
    request(q)
    fn, args = q.queued.pop()
    fn(*args)
    root = api.rows("TroshkaProject", None)[0]
    if change == "replacement":
        child = next(v for (kind, _), v in api.values.items() if kind == "TroshkaVM")
        child["metadata"]["uid"] = "replaced"
    elif change == "journal":
        child = next(
            v for (kind, _), v in api.values.items() if kind == "TroshkaProject"
        )
        next(iter(child["status"]["managedDeployment"]["creates"].values()))[
            "intentDigest"
        ] = "changed"
    else:
        kind = "PersistentVolumeClaim" if change == "disk" else "ConfigMap"
        api.values[kind, "unknown"] = q.support.resource(kind, "unknown")
    deploy.power.helpers().reconcile_stop(
        api, root if change != "journal" else api.rows("TroshkaProject", None)[0]
    )
    fn(*args)
    assert not any(c[0] == "DELETE" for c in api.calls)
    with q.sessions() as db:
        assert cleanup.native_intent(db, q.new_id).phase == "stopping"


@pytest.mark.parametrize("root_change", [None, "spec", "uid", "generation"])
def test_pending_undeploy_settles_after_namespace_and_storage_absence(
    pending_case, root_change
):
    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeEnrollment,
        NativeStorageSnapshot,
    )
    from app.models.project import Project
    from app.models.quota import QuotaReservation
    from app.schemas.kubevirt_storage import NativeStorageReport
    from app.services import native_managed_deploy as deploy
    from app.services import quota_cleanup as cleanup
    from app.services.kubevirt_storage_reports import import_report
    from tests.test_native_cleanup_completion import collect_cleanup
    from tests.test_native_managed_resume import FixedDatetime

    q = pending_case
    api = q.apis[q.new_id]
    bootstrap = q.support.resource("ConfigMap", "odh-kserve-custom-ca-bundle")
    bootstrap["metadata"]["namespace"] = api.rows("TroshkaProject", None)[0][
        "metadata"
    ]["namespace"]
    bootstrap["metadata"]["labels"] = {"opendatahub.io/managed": "true"}
    bootstrap["data"] = {"cabundle.crt": "synthetic public CA"}
    api.values["ConfigMap", "odh-kserve-custom-ca-bundle"] = bootstrap
    before = deepcopy(current_target(q, q.operation_id))
    request(q)
    fn, args = q.queued.pop()
    fn(*args)
    deploy.power.helpers().reconcile_stop(api, api.rows("TroshkaProject", None)[0])
    fn(*args)
    with q.sessions() as db:
        intent = cleanup.native_intent(db, q.new_id)
        assert intent.phase == "pending"
        domain = db.get(NativeCollectionDomain, str(intent.domain_id))
        q.report = NativeStorageReport.model_validate(
            db.get(NativeStorageSnapshot, domain.latest_report_id).payload
        )
        q.enrollment_id = domain.enrollment_id
        q.digest = db.get(NativeEnrollment, domain.enrollment_id).digest
    root = api.rows("TroshkaProject", None)[0]
    assert root["metadata"]["generation"] == intent.root.deployment_generation + 1
    if root_change:
        live = api.values["TroshkaProject", root["metadata"]["name"]]
        if root_change == "spec":
            live["spec"]["action"] = "changed"
        elif root_change == "uid":
            live["metadata"]["uid"] = "replacement"
        else:
            live["metadata"]["generation"] += 1
        calls = deepcopy(api.calls)
        with pytest.raises(deploy.power.helpers().ManagedPowerError):
            cleanup.submit_native_teardown(
                api, {"intent": intent.model_dump(mode="json")}
            )
        assert api.calls == calls
        return
    # Retry must recognize the original deleting root before retirement.
    cleanup.submit_native_teardown(api, {"intent": intent.model_dump(mode="json")})
    fn(*args)
    assert deploy.power.helpers().cleanup_delete(api, root) is False
    assert deploy.power.helpers().cleanup_delete(api, root) is True
    assert api.values["ConfigMap", "odh-kserve-custom-ca-bundle"] == bootstrap
    assert not any(
        call[0] == "DELETE" and call[1][0] == "ConfigMap" for call in api.calls
    )
    del api.values["TroshkaProject", root["metadata"]["name"]]
    fn(*args)
    assert api.find("Namespace", None, intent.root.namespace) is None
    with q.sessions() as db:
        assert db.get(Project, q.new_id).state == "cleanup_pending"
    # Use real discovery/import/ledger settlement, preserving sibling images.
    for project_id, client in q.apis.items():
        client.find = lambda kind, namespace, name, client=client: deepcopy(
            client.values.get((kind, name))
        )
        for pvc in client.rows("PersistentVolumeClaim", None):
            binding = next(
                b
                for b in q.report.volume_bindings
                if b.pvc_uid == pvc["metadata"]["uid"]
            )
            pv = client.values["PersistentVolume", pvc["spec"]["volumeName"]]
            pv["spec"].update(
                csi={
                    "driver": binding.csi_driver,
                    "volumeHandle": binding.volume_handle,
                },
                persistentVolumeReclaimPolicy="Delete",
            )
    advance_clock()
    report = collect_cleanup(q, intent)
    assert report.retiring_roots[0].objects_absent, report.collection_errors
    with q.sessions() as db:
        import_report(
            db,
            report,
            enrollment_id=q.enrollment_id,
            expected_enrollment_digest=q.digest,
            now=FixedDatetime.current,
        )
        db.commit()
    fn(*args)
    with q.sessions() as db:
        final = cleanup.native_intent(db, q.new_id)
        assert final.phase == "finalized", final.last_error
        project = db.get(Project, q.new_id)
        assert project.state == "draft" and project.host_id is None
        assert (
            project.owner_id == q.owner_id
            and project.topology == before["payload"]["spec"]["topology"]
        )
        row = db.query(QuotaReservation).filter_by(operation_id=q.operation_id).one()
        assert row.dispatch_metadata["native_deploy"] == before
        assert final.pending_initialization["journal"]["creates"]
        assert all(db.get(Project, identity) is not None for identity in q.project_ids)
    fn(*args)
