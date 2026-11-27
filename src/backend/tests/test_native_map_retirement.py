"""Released projects retire accounting only after complete independent proof."""
from datetime import timedelta

import pytest

from app.models.project import Project
from app.schemas.quota import AccountingUnavailable
from app.services import native_map_retirement as retirement
from app.services import native_project_namespace_cleanup as cleanup
from app.services.kubevirt_storage_enrollment import root_key
from app.services.kubevirt_storage_reports import report_inventory
from app.services.native_project_retirement import reclaim_orphans as real_reclaim
from tests import test_native_project_namespace_cleanup as support

namespace_case = support.namespace_case
online_case = support.online_case
resume_case = support.resume_case
cleanup_case = support.cleanup_case
deploy_case = support.deploy_case
native_ledger = support.native_ledger
isolated_quota_database = support.isolated_quota_database


@pytest.fixture(params=["delete", "undeploy"])
def released_case(namespace_case, request):
    q = namespace_case
    scope, report = support.collect_absence(q)
    with q.sessions() as db:
        work = cleanup.states(db, q.project_id)[0]
        work = cleanup._save(
            db,
            work,
            phase="orphan_reclaim",
            mode=request.param,
            namespace_delete_at=report.started_at - timedelta(seconds=1),
        )
        cleanup._evidence(
            db,
            work,
            "project-released",
            {
                "project_id": q.project_id,
                "mode": request.param,
                "liability_digest": work.liability_digest,
            },
        )
        project = db.get(Project, q.project_id)
        if request.param == "delete":
            db.delete(project)
        else:
            project.state = "draft"
        db.commit()
    q.work = work
    q.inventory = report_inventory(report).model_copy(
        update={"project_roots": [scope.root]}
    )
    keys = set(scope.owned_images) | set(scope.liabilities.shared_images)
    q.images = [i for i in report.images if i.key in keys]
    return q


def test_reclaim_records_independent_proof_without_name_based_deletion(
    released_case, monkeypatch
):
    q = released_case

    def configured(cleanup_id, *, administrator, reason, apply):
        assert apply
        with q.sessions() as db:
            retirement.record(
                db,
                q.work,
                q.inventory,
                q.images,
                administrator=administrator,
                reason=reason,
            )
            db.commit()

    monkeypatch.setattr(retirement, "reconcile_configured", configured)
    from app.services.native_collection_jobs import _retire_released_project

    assert (
        _retire_released_project(str(q.work.authority.domain_id), q.project_id)
        == "retired"
    )
    with q.sessions() as db:
        work = cleanup.states(db, q.project_id)[0]
        assert work.phase == "finalized"
        assert root_key(q.root) in retirement.settled_keys(db)


def test_reclaim_dispatches_to_collector_without_provider_deletion(
    released_case, monkeypatch
):
    q = released_case
    q.settlement_collections.clear()
    with q.sessions() as db:
        assert real_reclaim(db, str(q.work.id)).phase == "orphan_reclaim"
    assert q.settlement_collections == [str(q.work.id)]


def test_late_lifecycle_failure_preserves_finalized_receipt(released_case, monkeypatch):
    from types import SimpleNamespace

    from app.services.providers import kubevirt

    q = released_case
    monkeypatch.setattr(
        kubevirt,
        "_get_k8s_clients",
        lambda _: (None, None, SimpleNamespace(close=lambda: None)),
    )
    monkeypatch.setattr(cleanup, "NamespaceClient", lambda _: q.api)

    def late_failure(*args, **kwargs):
        with q.sessions() as db:
            retirement.record(
                db,
                q.work,
                q.inventory,
                q.images,
                administrator="worker",
                reason="verified absence",
            )
            db.commit()
        raise RuntimeError("late provider read failed")

    monkeypatch.setattr(cleanup, "advance", late_failure)
    cleanup.reconcile(str(q.work.id))
    with q.sessions() as db:
        assert root_key(q.root) in retirement.settled_keys(db)
        assert cleanup.states(db, q.project_id)[0].last_error is None


def test_verified_released_map_retires_without_fabricating_collector_report(
    released_case,
):
    from app.services.quota_cleanup import settled_root_keys

    q = released_case
    with q.sessions() as db:
        retirement.record(
            db,
            q.work,
            q.inventory,
            q.images,
            administrator="operator",
            reason="independent absence proof",
        )
        db.commit()
        assert root_key(q.root) in settled_root_keys(db)
        work = cleanup.states(db, q.project_id)[0]
        assert work.phase == "finalized"
        assert work.settlement_report_id is None
        assert work.settled_at is None
        if work.mode == "undeploy":
            # A subsequent deployment must not invalidate the old root's proof.
            db.get(Project, q.project_id).state = "active"
            db.commit()
            assert root_key(q.root) in settled_root_keys(db)


def test_map_receipt_does_not_resolve_unrelated_active_cleanup(
    released_case, monkeypatch
):
    from uuid import uuid4

    q = released_case
    sibling_id = str(uuid4())
    with q.sessions() as db:
        db.add(
            Project(
                id=sibling_id,
                name="active-cleanup-sibling",
                owner_id=str(q.work.authority.owner_id),
                state="cleanup_pending",
            )
        )
        db.commit()
    sibling = q.work.model_copy(
        update={
            "id": str(uuid4()),
            "phase": "namespace_delete_requested",
            "authority": q.work.authority.model_copy(update={"project_id": sibling_id}),
        }
    )
    original_states = cleanup.states
    monkeypatch.setattr(
        cleanup, "states", lambda db, *args: [*original_states(db, *args), sibling]
    )

    def active_authority(db, project):
        # Real origin resolution re-enters enrollment -> settled_root_keys here.
        raise AssertionError("unrelated active authority resolution")

    monkeypatch.setattr(cleanup.authority, "resolve", active_authority)
    with q.sessions() as db:
        retirement.record(
            db,
            q.work,
            q.inventory,
            q.images,
            administrator="operator",
            reason="independent absence proof",
        )
        db.commit()
        assert root_key(q.root) in retirement.settled_keys(db)
        # Broad callers must still validate active authority, never silently omit it.
        with pytest.raises(AssertionError, match="unrelated active authority"):
            cleanup.storage_scopes(db)


@pytest.mark.parametrize(
    "fault",
    [
        "private_present",
        "image_missing",
        "objects_present",
        "changed_work",
        "release_missing",
        "name_only_target",
    ],
)
def test_map_retirement_rejects_incomplete_or_changed_proof(released_case, fault):
    q = released_case
    inventory = q.inventory.model_copy(deep=True)
    images = list(q.images)
    if fault == "private_present":
        key = q.work.liabilities.private_images[0]
        images = [
            i.model_copy(update={"absence": None}) if i.key == key else i
            for i in images
        ]
    elif fault == "image_missing":
        images = images[:-1]
    elif fault == "objects_present":
        inventory.retiring_roots[0] = inventory.retiring_roots[0].model_copy(
            update={"objects_absent": False}
        )
    with q.sessions() as db:
        if fault == "name_only_target":
            row = (
                db.query(cleanup.Evidence)
                .filter_by(cleanup_id=str(q.work.id), phase="teardown-map")
                .one_or_none()
            )
            payload = {"reclaim_targets": [{"pool": "pool", "image": "name-only"}]}
            from app.services.kubevirt_storage_enrollment import digest

            if row is None:
                cleanup._evidence(db, q.work, "teardown-map", payload)
            else:
                row.payload = {**row.payload, **payload}
                row.digest = digest(row.payload)
            db.commit()
        if fault == "release_missing":
            db.query(cleanup.Evidence).filter_by(
                cleanup_id=str(q.work.id), phase="project-released"
            ).delete()
            db.commit()
        if fault == "changed_work":
            cleanup._save(db, q.work, last_error="changed")
            db.commit()
        with pytest.raises((ValueError, AccountingUnavailable)):
            retirement.record(
                db, q.work, inventory, images, administrator="operator", reason="verify"
            )
        db.rollback()
        assert root_key(q.root) not in retirement.settled_keys(db)
