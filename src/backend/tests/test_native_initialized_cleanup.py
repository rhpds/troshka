"""Unreleased initialized disks retain ownership through API cleanup."""

from copy import deepcopy
from datetime import timedelta

import pytest

from app.api import projects
from app.models.project import Project
from app.models.quota import QuotaReservation
from app.models.user import User
from app.services import native_managed_collector as collector
from app.services import quota_cleanup
from tests.test_native_managed_deploy import advance_clock, report_for
from tests.test_native_managed_images import (
    deploy_case as deploy_case_fixture,
)
from tests.test_native_managed_images import (
    image_case as image_case_fixture,
)
from tests.test_native_managed_images import (
    isolated_quota_database as isolated_quota_database_fixture,
)
from tests.test_native_managed_images import (
    native_ledger as native_ledger_fixture,
)
from tests.test_native_managed_images import (
    prepare_verified_image_operation,
)
from tests.test_native_managed_images import (
    resume_case as resume_case_fixture,
)
from tests.test_native_managed_resume import FixedDatetime

deploy_case = deploy_case_fixture
image_case = image_case_fixture
isolated_quota_database = isolated_quota_database_fixture
native_ledger = native_ledger_fixture
resume_case = resume_case_fixture


@pytest.mark.parametrize(
    "change",
    [
        None,
        "incomplete_initialization",
        "incomplete_spec",
        "incomplete_intent",
        "incomplete_parent",
        "incomplete_unjournaled",
        "stale",
        "owner",
        "runtime",
        "writer",
        "dv_uid",
        "pv_uid",
        "namespace",
        "extra_pv",
        "journal",
    ],
)
def test_unreleased_full_copy_is_owned_during_api_undeploy(
    image_case, monkeypatch, change
):
    q = image_case
    from app.services import native_managed_deploy as deploy

    original_manifest = deploy.helpers().clone_manifest

    def old_manifest(*args, **kwargs):
        value = original_manifest(*args, **kwargs)
        value["metadata"]["annotations"][
            "cdi.kubevirt.io/storage.usePopulator"
        ] = "false"
        return value

    monkeypatch.setattr(deploy.helpers(), "clone_manifest", old_manifest)
    original_resource = q.support.resource

    def resource(kind, name, *args, **kwargs):
        value = original_resource(kind, name, *args, **kwargs)
        if kind == "PersistentVolume" and name == "tenant-clone-pv":
            value["spec"].update(
                csi={
                    "driver": q.profile.target.provisioner,
                    "volumeHandle": "synthetic-" + value["spec"]["claimRef"]["uid"],
                },
                persistentVolumeReclaimPolicy="Delete",
            )
        return value

    monkeypatch.setattr(q.support, "resource", resource)
    operation, target, cache = prepare_verified_image_operation(
        q, monkeypatch, publish_initialization=False
    )
    api = q.apis[q.new_id]
    create_request = api.request

    def full_copy(method, kind, namespace, name, body):
        value = create_request(method, kind, namespace, name, body)
        if method == "POST" and kind == "DataVolume":
            pvc = api.values["PersistentVolumeClaim", value["metadata"]["name"]]
            pvc["spec"].pop("dataSource", None)
            pvc["spec"].pop("dataSourceRef", None)
            pvc["metadata"].pop("annotations", None)
        return value

    monkeypatch.setattr(api, "request", full_copy)
    deploy.reconcile(q.new_id)
    deploy.helpers().reconcile(api, api.rows("TroshkaProject", None)[0])
    if change and change.startswith("incomplete_"):
        live = next(v for (k, _), v in api.values.items() if k == "TroshkaProject")
        state = live["status"]["managedDeployment"]
        state.pop("phase", None)
        state.pop("initializationDigest", None)
        state.pop("generatedClaims", None)
        for key in list(state["creates"]):
            kind, name = key.split(":", 1)
            if kind not in {"DataVolume", "TroshkaVM", "VirtualMachine"}:
                state["creates"].pop(key)
                api.values.pop((kind, name), None)
            elif kind == "VirtualMachine":
                state["creates"][key].pop("uid")
    # Only creation uses the historical renderer. Cleanup observes that frozen
    # false-mode plan through the current production renderer compatibility path.
    monkeypatch.setattr(deploy.helpers(), "clone_manifest", original_manifest)
    q.reader.api = object()
    advance_clock()
    report = collector.cycle(
        q.sessions,
        q.pins,
        q.target_id,
        q.report.collector_id,
        {q.provider_id: q.reader},
        q.backend,
        lambda manifest, previous, started: report_for(q, manifest),
    )
    assert not report.collection_errors

    if change == "stale":
        FixedDatetime.current += timedelta(minutes=16)
    queued = []
    monkeypatch.setattr(
        "app.core.redis.enqueue_job", lambda fn, *args: queued.append((fn, args))
    )
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation).one()
        assert row.state == "reconciling"
        assert (
            row.dispatch_metadata["native_deploy"]["phase"] == "initialization-intent"
        )
        assert row.observed["storage_bytes"] > 0
        from app.models.native_clone_lineage import NativeCloneLineage

        for lineage_id in target["clone_lineages"].values():
            lineage = db.get(NativeCloneLineage, lineage_id)
            assert lineage.creation and not lineage.proof
        if change in {"owner", "runtime"}:
            metadata = deepcopy(row.dispatch_metadata)
            metadata["native_deploy"][
                "owner_id" if change == "owner" else "release_handoff"
            ] = "changed"
            row.dispatch_metadata = metadata
            db.commit()
        if change in {"stale", "owner", "runtime"}:
            from fastapi import HTTPException

            with pytest.raises(HTTPException):
                projects.undeploy_project(q.new_id, db.get(User, q.owner_id), db)
            db.rollback()
            assert not row.dispatch_metadata.get("cancelled")
            return
        result = projects.undeploy_project(q.new_id, db.get(User, q.owner_id), db)
        assert result["status"] == "cleanup_pending"
        intent = quota_cleanup.native_intent(db, q.new_id)
        assert intent.bindings and intent.owned_images
        assert cache.image_key not in intent.owned_images
        assert row.observed["storage_bytes"] > 0
        assert db.get(Project, q.new_id).owner_id == q.owner_id
    from app.schemas.kubevirt_storage import NativeCleanupIntent
    from app.services import native_managed_deploy as deploy

    api = q.apis[q.new_id]
    original_client = deploy.power.with_client
    monkeypatch.setattr(
        deploy.power,
        "with_client",
        lambda provider, fn, *args: (
            fn(api, *args)
            if isinstance(args[0], NativeCleanupIntent)
            else original_client(provider, fn, *args)
        ),
    )
    api.find = lambda kind, namespace, name: deepcopy(api.values.get((kind, name)))
    original_request = api.request
    deleted = []

    def delete(method, kind, namespace, name, body):
        if method != "DELETE":
            return original_request(method, kind, namespace, name, body)
        value = api.values[kind, name]
        deleted.append((kind, name))
        assert body["preconditions"]["uid"] == value["metadata"]["uid"]
        if kind == "TroshkaProject":
            value["metadata"].update(
                generation=value["metadata"]["generation"] + 1,
                deletionTimestamp="2026-11-13T00:00:00Z",
            )
        else:
            del api.values[kind, name]
        return {"kind": "Status", "status": "Success"}

    monkeypatch.setattr(api, "request", delete)
    fn, args = queued.pop()
    fn(*args)
    if change and change.startswith("incomplete_"):
        live = next(v for (k, _), v in api.values.items() if k == "TroshkaProject")
        vm = next(v for (k, _), v in api.values.items() if k == "VirtualMachine")
        entry_key = "VirtualMachine:" + vm["metadata"]["name"]
        if change == "incomplete_spec":
            vm["spec"]["template"]["spec"]["domain"]["cpu"]["cores"] = 999
        elif change == "incomplete_intent":
            live["status"]["managedDeployment"]["creates"][entry_key][
                "intentDigest"
            ] = "changed"
        elif change == "incomplete_parent":
            vm["metadata"]["ownerReferences"][0]["uid"] = "replacement"
        elif change == "incomplete_unjournaled":
            del live["status"]["managedDeployment"]["creates"][entry_key]
    if change == "writer":
        api.values["Pod", "writer"] = q.support.resource("Pod", "writer")
    elif change in {"dv_uid", "pv_uid"}:
        kind = "DataVolume" if change == "dv_uid" else "PersistentVolume"
        next(v for (k, _), v in api.values.items() if k == kind)["metadata"][
            "uid"
        ] = "replacement"
    elif change == "namespace":
        api.values["Namespace", intent.root.namespace]["metadata"][
            "uid"
        ] = "replacement"
    elif change == "extra_pv":
        api.values["PersistentVolume", "foreign"] = q.support.resource(
            "PersistentVolume",
            "foreign",
            spec={"claimRef": {"namespace": intent.root.namespace}},
        )
    elif change == "journal":
        live = next(v for (k, _), v in api.values.items() if k == "TroshkaProject")
        next(iter(live["status"]["managedDeployment"]["creates"].values()))[
            "uid"
        ] = "replacement"
    deploy.power.helpers().reconcile_stop(api, api.rows("TroshkaProject", None)[0])
    fn(*args)
    if change and change != "incomplete_initialization":
        with q.sessions() as db:
            assert quota_cleanup.native_intent(db, q.new_id).phase == "stopping"
            assert (
                db.query(QuotaReservation)
                .filter_by(operation_id=operation)
                .one()
                .observed["storage_bytes"]
                > 0
            )
        assert not deleted
        return
    with q.sessions() as db:
        current = quota_cleanup.native_intent(db, q.new_id)
        assert current.phase == "pending", (
            current.last_error,
            api.rows("TroshkaProject", None)[0],
        )
    root = api.rows("TroshkaProject", None)[0]
    if change == "incomplete_initialization":
        state = root["status"]["managedDeployment"]
        assert "generatedClaims" not in state
        assert not state.get("phase")
        vm_entry = next(
            v for k, v in state["creates"].items() if k.startswith("VirtualMachine:")
        )
        assert "uid" not in vm_entry
    for _ in range(8):
        if deploy.power.helpers().cleanup_delete(api, root):
            break
    else:
        pytest.fail("Original initialized resources did not drain")
    del api.values["TroshkaProject", root["metadata"]["name"]]
    fn(*args)
    assert api.find("Namespace", None, intent.root.namespace) is None
    from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment
    from app.services.kubevirt_storage_reports import import_report
    from tests.test_native_cleanup_completion import collect_cleanup

    q.report = report
    with q.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(intent.domain_id))
        q.enrollment_id = domain.enrollment_id
        q.digest = db.get(NativeEnrollment, domain.enrollment_id).digest
    for client in q.apis.values():
        client.find = lambda kind, namespace, name, client=client: deepcopy(
            client.values.get((kind, name))
        )
        for pvc in client.rows("PersistentVolumeClaim", None):
            binding = next(
                b for b in report.volume_bindings if b.pvc_uid == pvc["metadata"]["uid"]
            )
            client.values["PersistentVolume", pvc["spec"]["volumeName"]]["spec"].update(
                csi={
                    "driver": binding.csi_driver,
                    "volumeHandle": binding.volume_handle,
                },
                persistentVolumeReclaimPolicy="Delete",
            )
    for key in list(api.values):
        if key[0] == "PersistentVolume":
            del api.values[key]
    for gone in (False, True):
        advance_clock()
        settled = collect_cleanup(q, current, gone=gone)
        with q.sessions() as db:
            import_report(
                db,
                settled,
                enrollment_id=q.enrollment_id,
                expected_enrollment_digest=q.digest,
                now=FixedDatetime.current,
            )
            db.commit()
        fn(*args)
        with q.sessions() as db:
            current = quota_cleanup.native_intent(db, q.new_id)
            row = db.query(QuotaReservation).filter_by(operation_id=operation).one()
            project = db.get(Project, q.new_id)
            assert current.phase == (
                "finalized" if gone else "pending"
            ), current.last_error
            assert project.state == ("draft" if gone else "cleanup_pending")
            assert (
                project.owner_id == q.owner_id
                and project.topology == target["payload"]["spec"]["topology"]
            )
            assert bool(row.observed["storage_bytes"]) is (not gone)
            assert row.dispatch_metadata["native_deploy"] == target
            for lineage_id in target["clone_lineages"].values():
                assert not db.get(NativeCloneLineage, lineage_id).proof
        assert cache.image_key not in current.owned_images
