"""Manual deletion cannot retire accounting without independent storage proof."""

from datetime import timedelta

import pytest

from app.models.project import Project
from app.schemas.quota import AccountingUnavailable
from app.services import native_project_namespace_cleanup as cleanup
from app.services.kubevirt_storage_enrollment import digest, root_key
from app.services.kubevirt_storage_reports import report_inventory
from tests import test_native_project_namespace_cleanup as support

namespace_case = support.namespace_case
online_case = support.online_case
resume_case = support.resume_case
cleanup_case = support.cleanup_case
deploy_case = support.deploy_case
native_ledger = support.native_ledger
isolated_quota_database = support.isolated_quota_database


@pytest.fixture
def manual_case(namespace_case):
    q = namespace_case
    scope, report = support.collect_absence(q)
    with q.sessions() as db:
        work = cleanup.states(db, q.project_id)[0]
        liabilities = work.liabilities.model_copy(
            update={"unresolved": ["storage_coverage_incomplete"]}
        )
        work = cleanup._save(
            db,
            work,
            phase="finalized",
            mode="delete",
            namespace_delete_at=None,
            liabilities=liabilities,
            liability_digest=digest(liabilities.model_dump(mode="json")),
        )
        cleanup._evidence(
            db,
            work,
            "manual-legacy-retirement",
            {
                "method": "user-authorized-manual-legacy-cleanup",
                "namespace": work.authority.namespace,
                "namespace_uid": work.authority.namespace_uid,
                "namespace_absent": True,
                "pv_absent": True,
                "observed_at": (report.started_at - timedelta(seconds=1)).isoformat(),
                "automatic_settlement_claimed": False,
            },
        )
        db.delete(db.get(Project, q.project_id))
        db.commit()
    q.manual_work = work
    q.manual_inventory = report_inventory(report).model_copy(
        update={"project_roots": [scope.root]}
    )
    q.manual_inventory.retiring_roots[0] = q.manual_inventory.retiring_roots[
        0
    ].model_copy(update={"operation_id": "manual-retirement:" + str(work.id)})
    keys = set(scope.owned_images) | set(scope.liabilities.shared_images)
    q.manual_images = [i for i in report.images if i.key in keys]
    return q


def test_verified_manual_receipt_retires_deleted_root_only(manual_case):
    from app.services import native_manual_retirement as manual
    from app.services.quota_cleanup import settled_root_keys

    q = manual_case
    with q.sessions() as db:
        assert root_key(q.root) not in settled_root_keys(db)
        manual.record(
            db,
            q.manual_work,
            q.manual_inventory,
            q.manual_images,
            administrator="test-operator",
            reason="Verified manual cleanup",
        )
        db.commit()
        assert root_key(q.root) in settled_root_keys(db)
        # Preserve the distinction from automated settlement.
        work = cleanup.states(db, q.project_id)[0]
        assert work.settlement_report_id is None
        assert work.settled_at is None


@pytest.mark.parametrize(
    "fault",
    [
        "private_present",
        "image_missing",
        "objects_present",
        "wrong_root",
        "unknown_debt",
        "changed_work",
    ],
)
def test_manual_retirement_rejects_incomplete_or_changed_proof(manual_case, fault):
    from app.services import native_manual_retirement as manual
    from app.services.quota_cleanup import settled_root_keys

    q = manual_case
    inventory, images = q.manual_inventory.model_copy(deep=True), list(q.manual_images)
    private = q.manual_work.liabilities.private_images[0]
    if fault == "private_present":
        images = [
            i.model_copy(update={"absence": None}) if i.key == private else i
            for i in images
        ]
    elif fault == "image_missing":
        images = images[1:]
    elif fault == "objects_present":
        inventory.retiring_roots[0] = inventory.retiring_roots[0].model_copy(
            update={"objects_absent": False}
        )
    elif fault == "wrong_root":
        inventory.retiring_roots[0] = inventory.retiring_roots[0].model_copy(
            update={"root": q.root.model_copy(update={"namespace_uid": "other"})}
        )
    with q.sessions() as db:
        if fault in {"unknown_debt", "changed_work"}:
            work = cleanup.states(db, q.project_id)[0]
            if fault == "unknown_debt":
                liabilities = work.liabilities.model_copy(
                    update={"unresolved": ["unknown_image"]}
                )
                work = cleanup._save(
                    db,
                    work,
                    liabilities=liabilities,
                    liability_digest=digest(liabilities.model_dump(mode="json")),
                )
                q.manual_work = work
            else:
                cleanup._save(db, work, last_error="changed")
            db.commit()
        with pytest.raises((ValueError, AccountingUnavailable)):
            manual.record(
                db,
                q.manual_work,
                inventory,
                images,
                administrator="test-operator",
                reason="Verified manual cleanup",
            )
        db.rollback()
        assert root_key(q.root) not in settled_root_keys(db)


@pytest.mark.parametrize("remaining", [False, True, "unavailable"])
def test_provider_verification_checks_full_private_image_absence(
    manual_case, remaining
):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.schemas.kubevirt_storage import CollectionLimits, ReadResult
    from app.services import native_manual_retirement as manual

    q = manual_case
    with q.sessions() as db:
        scope = manual._scope(
            q.manual_work, manual._manual_evidence(db, q.manual_work).payload
        )

    def result(value):
        return ReadResult(
            status="complete", value=value, collected_at=datetime.now(UTC)
        )

    class Kube:
        def cluster_identity(self):
            return q.root.cluster_key

        def namespace_if_exists(self, name):
            return None

        def pvs(self):
            return []

        def snapshot_contents(self):
            return []

    class Backend:
        capability = SimpleNamespace(
            command_version="test",
            merge_snapshots_validated=True,
            validation_reference="fixture",
            semantics_version="kubevirt-rbd-allocated-v1",
        )

        def cluster_identity(self):
            return result(scope.owned_images[0].ceph_fsid)

        def pool_identity(self):
            return result([{"poolnum": scope.owned_images[0].pool_id}])

        def command_version(self):
            return result("test")

        def image_inventory(self, key, *, max_objects):
            if remaining == "unavailable":
                return ReadResult(status="unavailable", collected_at=datetime.now(UTC))
            original = next(i.absence for i in q.manual_images if i.key == key)
            changes = {"collected_at": datetime.now(UTC)}
            if remaining:
                changes["active"] = True
            return result(original.model_copy(update=changes))

    if remaining:
        with pytest.raises((ValueError, AccountingUnavailable)):
            manual.observe(scope, Kube(), Backend(), CollectionLimits())
    else:
        inventory, images = manual.observe(scope, Kube(), Backend(), CollectionLimits())
        assert inventory.retiring_roots[0].objects_absent
        assert all(i.absence.excludes(i.key) for i in images)


def test_shared_image_must_survive_manual_retirement(manual_case):
    from app.services import native_manual_retirement as manual

    q = manual_case
    with q.sessions() as db:
        scope = manual._scope(
            q.manual_work, manual._manual_evidence(db, q.manual_work).payload
        )
    shared = next(
        i
        for i in q.report.images
        if i.key not in scope.owned_images and i.absence is None
    )
    scope = scope.model_copy(
        update={
            "liabilities": scope.liabilities.model_copy(
                update={"shared_images": [shared.key]}
            )
        }
    )
    scope = scope.model_copy(
        update={"inventory_digest": digest(scope.liabilities.model_dump(mode="json"))}
    )
    inventory = q.manual_inventory.model_copy(deep=True)
    inventory.retiring_roots[0] = inventory.retiring_roots[0].model_copy(
        update={"inventory_digest": scope.inventory_digest}
    )
    images = q.manual_images + [shared]
    shared = shared.model_copy(
        update={"collected_at": inventory.retiring_roots[0].observed_at}
    )
    images[-1] = shared
    manual._verify(scope, inventory, images)
    images[-1] = shared.model_copy(update={"measurement_status": "unavailable"})
    with pytest.raises(AccountingUnavailable, match="Shared cleanup ancestry survival"):
        manual._verify(scope, inventory, images)
