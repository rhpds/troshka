"""Retrospective verification must not manufacture deletion authority."""

from copy import deepcopy

import pytest

from app.models.native_project_cleanup import NativeProjectNamespaceCleanup as State
from app.models.quota import QuotaAudit
from app.schemas.quota import AccountingUnavailable
from app.services.kubevirt_storage_enrollment import root_key
from tests.test_native_manual_retirement import (  # noqa: F401
    cleanup_case,
    deploy_case,
    isolated_quota_database,
    namespace_case,
    native_ledger,
    online_case,
    resume_case,
)
from tests.test_native_manual_retirement import (
    manual_case as manual_fixture,
)

manual_case = manual_fixture


@pytest.fixture
def legacy_case(manual_case):
    from app.services import native_manual_retirement as manual

    q = manual_case
    with q.sessions() as db:
        original = manual._manual_evidence(db, q.manual_work).payload
        audit = QuotaAudit(
            actor_id=str(q.manual_work.authority.owner_id),
            scope="project",
            scope_id=q.project_id,
            action="manual-legacy-project-retirement",
            before={"name": "legacy"},
            after=original,
        )
        db.add(audit)
        db.query(State).filter_by(project_id=q.project_id).delete()
        db.commit()
        q.audit_id = audit.id
    bindings = q.manual_work.liabilities.bindings
    q.capture = {
        "provenance": {"source": "preserved pre-deletion provider inventory"},
        "bindings": [b.model_dump(mode="json") for b in bindings],
        "pvs": [
            dict(
                claim_uid=b.pvc_uid,
                uid=b.pv_uid,
                driver=b.csi_driver,
                handle=b.volume_handle,
            )
            for b in bindings
        ],
    }
    return q


def proof(q, scope):
    inventory = q.manual_inventory.model_copy(deep=True)
    inventory.retiring_roots[0] = inventory.retiring_roots[0].model_copy(
        update={
            "operation_id": scope.operation_id,
            "inventory_digest": scope.inventory_digest,
            "intent_id": scope.intent_id,
        }
    )
    return inventory


def test_legacy_receipt_retires_root_without_creating_cleanup_authority(legacy_case):
    from app.services import native_legacy_retirement as legacy
    from app.services.quota_cleanup import settled_root_keys

    q = legacy_case
    with q.sessions() as db:
        scope, source = legacy.prepare(db, q.audit_id, q.capture)
        legacy.record(
            db,
            scope,
            source,
            q.capture,
            proof(q, scope),
            q.manual_images,
            administrator="operator",
            reason="Verified prior manual deletion",
        )
        db.commit()
        assert root_key(q.root) in settled_root_keys(db)
        assert db.query(State).filter_by(project_id=q.project_id).count() == 0


@pytest.mark.parametrize(
    "fault", ["missing_image", "objects_present", "audit_changed", "wrong_binding"]
)
def test_legacy_receipt_rejects_incomplete_or_changed_evidence(legacy_case, fault):
    from app.services import native_legacy_retirement as legacy
    from app.services.quota_cleanup import settled_root_keys

    q = legacy_case
    with q.sessions() as db:
        scope, source = legacy.prepare(db, q.audit_id, q.capture)
        inventory, images = proof(q, scope), list(q.manual_images)
        extras = deepcopy(q.capture)
        if fault == "missing_image":
            images = images[1:]
        elif fault == "objects_present":
            inventory.retiring_roots[0] = inventory.retiring_roots[0].model_copy(
                update={"objects_absent": False}
            )
        elif fault == "audit_changed":
            audit = db.get(QuotaAudit, q.audit_id)
            audit.after = {**audit.after, "namespace_uid": "other"}
            db.flush()
        else:
            binding = scope.bindings[0]
            extras["bindings"] = [
                binding.model_copy(update={"volume_handle": "other"}).model_dump(
                    mode="json"
                )
            ]
        with pytest.raises((ValueError, AccountingUnavailable)):
            legacy.record(
                db,
                scope,
                source,
                extras,
                inventory,
                images,
                administrator="operator",
                reason="Verified prior manual deletion",
            )
        db.rollback()
        assert root_key(q.root) not in settled_root_keys(db)


def test_historical_temporary_pvc_cannot_be_omitted(legacy_case):
    from app.services import native_legacy_retirement as legacy

    q = legacy_case
    capture = deepcopy(q.capture)
    extra = {**capture["pvs"][0], "claim_uid": "historical-temp", "uid": "temp-pv"}
    capture["pvs"].append(extra)
    with q.sessions() as db:
        with pytest.raises((ValueError, AccountingUnavailable), match="incomplete"):
            legacy.prepare(db, q.audit_id, capture)
        binding = deepcopy(capture["bindings"][0])
        binding.update(pvc_uid="historical-temp", pv_uid="temp-pv")
        binding["image_key"]["image_id"] = "temporary-image"
        capture["bindings"].append(binding)
        scope, source = legacy.prepare(db, q.audit_id, capture)
        with pytest.raises((ValueError, AccountingUnavailable)):
            legacy.record(
                db,
                scope,
                source,
                capture,
                proof(q, scope),
                q.manual_images,
                administrator="operator",
                reason="Prior manual cleanup",
            )
