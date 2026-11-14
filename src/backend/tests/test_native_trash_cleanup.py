"""Project cleanup may remove only its original, exclusively owned trash IDs."""

from datetime import timedelta

import pytest
from kubernetes.client.exceptions import ApiException

from app.schemas.kubevirt_storage import ReadResult
from tests.test_native_cleanup_completion import (
    cleanup_case as cleanup_case_fixture,
)
from tests.test_native_cleanup_completion import (
    collect_cleanup,
)
from tests.test_native_cleanup_completion import (
    deploy_case as deploy_case,
)
from tests.test_native_cleanup_completion import (
    isolated_quota_database as isolated_quota_database,
)
from tests.test_native_cleanup_completion import (
    native_ledger as native_ledger,
)
from tests.test_native_cleanup_completion import (
    resume_case as resume_case,
)
from tests.test_native_managed_resume import FixedDatetime

cleanup_case = cleanup_case_fixture


def pending(q):
    from app.api import projects
    from app.models.user import User
    from app.services import native_managed_power as power
    from app.services import quota_cleanup
    from app.services.kubevirt_storage_reports import import_report

    pid = q.project_ids[0]
    with q.sessions() as db:
        projects.undeploy_project(pid, db.get(User, q.owner_id), db)
    fn, args = q.queued.pop()
    fn(*args)
    api = q.apis[pid]
    root = api.rows("TroshkaProject", None)[0]
    for _ in range(8):
        if power.helpers().cleanup_delete(api, root):
            break
    del api.values["TroshkaProject", root["metadata"]["name"]]
    fn(*args)
    for key in list(api.values):
        if key[0] == "PersistentVolume":
            del api.values[key]
    FixedDatetime.current += timedelta(seconds=1)
    with q.sessions() as db:
        intent = quota_cleanup.native_intent(db, pid)
    report = collect_cleanup(q, intent)
    assert report.retiring_roots[0].objects_absent
    with q.sessions() as db:
        import_report(
            db,
            report,
            enrollment_id=q.enrollment_id,
            expected_enrollment_digest=q.digest,
            now=FixedDatetime.current,
        )
        db.commit()
    return intent, report


@pytest.mark.parametrize(
    "condition",
    [
        "owned",
        "quotas_disabled",
        "submitted",
        "superseded",
        "digest_mismatch",
        "ambiguous",
        "failed",
        "active",
        "retain",
        "namespace",
        "cache",
        "foreign",
        "stale",
        "owner",
        "wrong_id",
        "unavailable",
    ],
)
def test_cleanup_removes_only_owned_trash_once(cleanup_case, condition, monkeypatch):
    from app.models.project import Project
    from app.schemas.kubevirt_storage import BackendImageState
    from app.services import native_trash_cleanup, quota_cleanup

    q = cleanup_case
    if condition == "quotas_disabled":
        monkeypatch.setattr(
            "app.services.quota_accounting.quotas_enabled", lambda: False
        )
    intent, report = pending(q)
    calls = []
    if condition == "submitted":
        from app.schemas.kubevirt_storage import NativeTrashRemoval
        from app.services.kubevirt_storage_enrollment import digest

        key = intent.bindings[0].image_key
        with q.sessions() as db:
            quota_cleanup._save_native(
                db,
                intent,
                trash_removals={
                    digest(key): NativeTrashRemoval(
                        image_key=key,
                        report_id=report.report_id,
                        submitted_at=FixedDatetime.current,
                    )
                },
            )
            db.commit()
    if condition in {"superseded", "digest_mismatch"}:
        from app.models.kubevirt_storage import (
            NativeCollectionDomain,
            NativeStorageSnapshot,
        )

        with q.sessions() as db:
            if condition == "superseded":
                db.get(
                    NativeCollectionDomain, str(intent.domain_id)
                ).latest_report_id = None
            else:
                db.get(NativeStorageSnapshot, str(report.report_id)).digest = "0" * 64
            db.commit()
    if condition == "retain":
        with q.sessions() as db:
            objects = [
                (
                    v.model_copy(update={"reclaim_policy": "Retain"})
                    if v.kind == "PersistentVolume"
                    else v
                )
                for v in intent.objects
            ]
            quota_cleanup._save_native(db, intent, objects=objects)
            db.commit()
    if condition == "owner":
        with q.sessions() as db:
            db.get(Project, q.project_ids[0]).host_id = None
            db.commit()
    if condition in {"cache", "foreign"}:
        from app.models.kubevirt_storage import NativeRetainedBinding

        with q.sessions() as db:
            rows = db.query(NativeRetainedBinding).all()
            row = next(
                r
                for r in rows
                if (
                    "root" not in r.payload
                    if condition == "cache"
                    else r.payload.get("root", {}).get("project_id") == q.project_ids[1]
                )
            )
            row.payload = {
                **row.payload,
                "image_key": intent.bindings[0].image_key.model_dump(mode="json"),
            }
            db.commit()
    if condition == "stale":
        FixedDatetime.current += timedelta(minutes=16)

    class Reader:
        def cluster_identity(self):
            return intent.root.cluster_key

        def namespace(self, name):
            if condition == "namespace":
                return {"metadata": {"uid": intent.root.namespace_uid}}
            raise ApiException(status=404)

    class Backend:
        def image_inventory(self, key, **kwargs):
            if condition == "unavailable":
                return ReadResult(
                    status="unavailable", collected_at=FixedDatetime.current
                )
            return ReadResult(
                status="complete",
                collected_at=FixedDatetime.current,
                value=BackendImageState(
                    **{
                        **key.model_dump(),
                        "image_id": (
                            "foreign" if condition == "wrong_id" else key.image_id
                        ),
                    },
                    active=condition == "active",
                    trash=True,
                    collected_at=FixedDatetime.current,
                ),
            )

        def remove_trash(self, key):
            with q.sessions() as db:
                saved = quota_cleanup.native_intent(db, q.project_ids[0])
                assert any(
                    v.image_key == key and v.status == "submitted"
                    for v in saved.trash_removals.values()
                )
            calls.append(key)
            if condition == "ambiguous":
                raise TimeoutError("lost response")
            return ReadResult(
                status="unavailable" if condition == "failed" else "complete",
                collected_at=FixedDatetime.current,
            )

    for _ in range(2):
        native_trash_cleanup.reconcile(
            q.sessions, report, {str(intent.provider_id): Reader()}, Backend()
        )
    assert calls == (
        [intent.bindings[0].image_key]
        if condition in {"owned", "quotas_disabled", "ambiguous", "failed"}
        else []
    )
    with q.sessions() as db:
        saved = quota_cleanup.native_intent(db, q.project_ids[0])
        assert saved.phase == "pending"
        assert db.get(Project, q.project_ids[0]).state == "cleanup_pending"
        if calls:
            assert len(saved.trash_removals) == len(calls)
            assert all(
                v.status
                == ("uncertain" if condition in {"ambiguous", "failed"} else "accepted")
                for v in saved.trash_removals.values()
            )
