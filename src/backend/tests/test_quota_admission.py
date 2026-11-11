from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.managed_group import ManagedGroup
from app.models.project import Project
from app.models.quota import (
    QuotaAllocation,
    QuotaCharge,
    QuotaPolicy,
    QuotaReadiness,
    QuotaReservation,
)
from app.models.user import User
from app.schemas.quota import AccountingUnavailable, Limits, QuotaDenied, Resources
from app.services import quota_admission as admission
from app.services.quota_policy import set_limits
from app.services.quota_resources import requested_resources, topology_resources
from tests.conftest import TestSession

NOW = datetime(2026, 11, 11, tzinfo=UTC)
GIB = 2**30


@pytest.fixture
def ledger():
    with TestSession() as db:
        org = ManagedGroup(
            name=str(uuid4()), normalized_name=str(uuid4()), is_organization=True
        )
        db.add(org)
        db.flush()
        user = User(email=f"{uuid4()}@test.invalid", organization_id=org.id)
        db.add(user)
        db.flush()
        project = Project(name="admission", owner_id=user.id, state="draft")
        db.add(project)
        db.merge(
            QuotaReadiness(
                id="baseline",
                ready=True,
                baseline_version="operator-baseline-v2-allocated",
                details={"accounting_ready": True},
            )
        )
        db.flush()
        yield db, project, user, org
        db.rollback()


def topology():
    return {
        "nodes": [
            {"id": "vm", "type": "vmNode", "data": {"vcpus": 2, "ram": 4}},
            {
                "id": "container",
                "type": "containerNode",
                "data": {"cpus": 0.25, "memory": 256},
            },
            {"id": "disk", "type": "storageNode", "data": {"size": 10}},
        ],
        "edges": [
            {"source": "disk", "target": "vm", "targetHandle": "disk-controller-0"}
        ],
    }


def test_resource_transitions_keep_persistent_capacity():
    desired = topology()
    assert requested_resources(desired, "draft") == Resources()
    full = topology_resources(desired)
    assert full == Resources(
        cpu=3, ram_bytes=4 * GIB + 256 * 2**20, storage_bytes=10 * GIB, projects=1
    )
    assert requested_resources(desired, "deploy") == full
    assert requested_resources(desired, "start") == Resources(
        cpu=3, ram_bytes=full.ram_bytes
    )
    assert topology_resources(desired, state="stopped") == Resources(
        storage_bytes=10 * GIB, projects=1
    )
    assert (
        requested_resources(desired, "stop")
        == requested_resources(desired, "destroy")
        == Resources()
    )
    desired["nodes"][2]["data"]["size"] = 15
    assert requested_resources(desired, "reconfigure", current=full) == Resources(
        storage_bytes=5 * GIB
    )


def test_resource_mapper_fails_closed_and_uses_virtual_capacity():
    desired = topology()
    desired["nodes"][2]["data"]["libraryItemId"] = "image"
    with pytest.raises(AccountingUnavailable):
        topology_resources(desired)
    assert (
        topology_resources(desired, virtual_sizes={"disk": 20}).storage_bytes
        == 20 * GIB
    )
    desired["nodes"].append({"id": "unknown", "type": "cephNode", "data": {}})
    with pytest.raises(AccountingUnavailable):
        topology_resources(desired, virtual_sizes={"disk": 20})
    with pytest.raises(AccountingUnavailable):
        requested_resources({}, "invented")


def test_shared_disk_and_pods_require_explicit_supported_mapping():
    desired = topology()
    desired["edges"].append({"source": "disk", "target": "container"})
    with pytest.raises(AccountingUnavailable):
        topology_resources(desired)
    desired = topology()
    desired["nodes"][1]["data"]["isPod"] = True
    with pytest.raises(AccountingUnavailable):
        topology_resources(desired)


def test_reservation_limits_all_scopes_and_retry_identity(ledger):
    db, project, user, org = ledger
    db.add_all(
        [
            QuotaPolicy(scope="organization", scope_id=org.id, cpu=6),
            QuotaPolicy(scope="user", scope_id=user.id, cpu=0),
        ]
    )
    with pytest.raises(QuotaDenied) as error:
        admission.reserve(db, project.id, "denied", Resources(cpu=1), NOW)
    assert error.value.scope == "user"
    db.query(QuotaPolicy).filter_by(scope="user", scope_id=user.id).one().cpu = None
    identity = admission.reserve(db, project.id, "same", Resources(cpu=6), NOW)
    assert admission.reserve(db, project.id, "same", Resources(cpu=6), NOW) == identity
    with pytest.raises(admission.OperationConflict):
        admission.reserve(db, project.id, "same", Resources(cpu=5), NOW)
    with pytest.raises(QuotaDenied):
        admission.reserve(db, project.id, "next", Resources(cpu=1), NOW)
    assert db.query(QuotaReservation).filter_by(project_id=project.id).count() == 1


def test_spending_and_readiness_checked_even_for_zero_start_delta(ledger):
    db, project, user, org = ledger
    db.add(QuotaPolicy(scope="organization", scope_id=org.id, spending_usd=Decimal(0)))
    with pytest.raises(QuotaDenied) as error:
        admission.reserve(db, project.id, "start", Resources(), NOW)
    assert error.value.reason == "spending_limit"
    db.query(QuotaPolicy).filter_by(scope="organization", scope_id=org.id).delete()
    db.get(QuotaReadiness, "baseline").details = {}
    with pytest.raises(AccountingUnavailable):
        admission.reserve(db, project.id, "unready", Resources(cpu=1), NOW)


def test_confirm_partial_uncertain_and_replayed_observations(ledger):
    db, project, _, org = ledger
    db.add(QuotaPolicy(scope="organization", scope_id=org.id, cpu=6))
    admission.reserve(
        db, project.id, "deploy", Resources(cpu=6, storage_bytes=10, projects=1), NOW
    )
    admission.confirm(
        db,
        "deploy",
        Resources(cpu=2, storage_bytes=10, projects=1),
        "partial",
        NOW,
        complete=False,
    )
    assert admission.usage(db, "organization", org.id) == (
        Resources(cpu=2, storage_bytes=10, projects=1),
        Resources(cpu=4),
    )
    with pytest.raises(QuotaDenied):
        admission.reserve(db, project.id, "other", Resources(cpu=1), NOW)
    admission.confirm(
        db,
        "deploy",
        Resources(cpu=6, storage_bytes=10, projects=1),
        "complete",
        NOW + timedelta(seconds=1),
    )
    assert admission.usage(db, "organization", org.id) == (
        Resources(cpu=6, storage_bytes=10, projects=1),
        Resources(),
    )
    admission.confirm(
        db,
        "deploy",
        Resources(cpu=2, storage_bytes=10, projects=1),
        "partial",
        NOW,
        complete=False,
    )
    assert (
        db.query(QuotaAllocation).filter_by(project_id=project.id).one().observed["cpu"]
        == 6
    )
    admission.confirm(
        db,
        "deploy",
        Resources(storage_bytes=10, projects=1),
        "off",
        NOW + timedelta(seconds=2),
    )
    with pytest.raises(admission.OperationConflict):
        admission.confirm(
            db,
            "deploy",
            Resources(cpu=6, storage_bytes=10, projects=1),
            "restart-unreserved",
            NOW + timedelta(seconds=3),
        )
    assert admission.usage(db, "organization", org.id)[0] == Resources(
        storage_bytes=10, projects=1
    )


def test_observation_cannot_invent_capacity_or_reuse_identity(ledger):
    db, project, _, _ = ledger
    admission.reserve(db, project.id, "deploy", Resources(cpu=2), NOW)
    with pytest.raises(admission.OperationConflict):
        admission.confirm(db, "deploy", Resources(cpu=3), "too-large", NOW)
    admission.confirm(db, "deploy", Resources(cpu=2), "observed", NOW)
    with pytest.raises(admission.OperationConflict):
        admission.confirm(
            db, "deploy", Resources(), "observed", NOW + timedelta(seconds=1)
        )


def test_resource_reduction_does_not_reject_other_exceeded_dimensions(ledger):
    db, project, _, org = ledger
    admission.reserve(db, project.id, "old", Resources(cpu=6), NOW)
    admission.confirm(db, "old", Resources(cpu=6), "old-observation", NOW)
    db.add(QuotaPolicy(scope="organization", scope_id=org.id, cpu=2, storage_bytes=10))
    admission.reserve(db, project.id, "disk-only", Resources(storage_bytes=10), NOW)
    assert admission.usage(db, "organization", org.id)[1].storage_bytes == 10


def test_all_spend_scopes_checked_including_history(ledger):
    db, project, user, org = ledger
    db.add_all(
        [
            QuotaPolicy(scope="user", scope_id=user.id, spending_usd=Decimal(1)),
            QuotaCharge(
                project_id=project.id,
                project_name=project.name,
                owner_id=user.id,
                organization_id=org.id,
                source="test",
                source_id="historic",
                kind="vcpu",
                start_at=NOW - timedelta(hours=1),
                end_at=NOW,
                quantity=1,
                rate=1,
                amount=1,
            ),
        ]
    )
    with pytest.raises(QuotaDenied) as error:
        admission.reserve(db, project.id, "blocked", Resources(cpu=1), NOW)
    assert error.value.scope == "user"


def test_dispatch_metadata_survives_session_reload(ledger):
    db, project, _, _ = ledger
    identity = admission.reserve(db, project.id, "metadata", Resources(cpu=1), NOW)
    row = db.get(QuotaReservation, identity)
    row.dispatch_metadata = {
        "kind": "start",
        "target_digest": "sha256:abc",
        "resource_ids": ["vm-1"],
        "attempt": 1,
    }
    db.flush()
    db.expunge(row)
    row = db.get(QuotaReservation, identity)
    assert row.dispatch_metadata["resource_ids"] == ["vm-1"]


@pytest.mark.parametrize("field", ["bmcIp", "deferOcpInstall"])
def test_external_power_topology_is_not_silently_supported(field):
    desired = topology()
    desired["nodes"][0]["data"][field] = "192.0.2.1" if field == "bmcIp" else True
    with pytest.raises(AccountingUnavailable):
        topology_resources(desired)


def test_dispatch_revalidation_checks_new_limits_and_excludes_own_reservation(ledger):
    db, project, _, org = ledger
    db.add(QuotaPolicy(scope="organization", scope_id=org.id, cpu=6))
    admission.reserve(db, project.id, "queued", Resources(cpu=6), NOW)
    admission.revalidate(db, "queued", NOW)
    policy = (
        db.query(QuotaPolicy).filter_by(scope="organization", scope_id=org.id).one()
    )
    policy.cpu = 5
    with pytest.raises(QuotaDenied):
        admission.revalidate(db, "queued", NOW)
    policy.cpu = 6
    policy.spending_usd = 0
    with pytest.raises(QuotaDenied) as error:
        admission.revalidate(db, "queued", NOW)
    assert error.value.reason == "spending_limit"


def test_completed_operation_cannot_dispatch_again(ledger):
    db, project, _, _ = ledger
    admission.reserve(db, project.id, "done", Resources(cpu=2), NOW)
    admission.confirm(db, "done", Resources(cpu=2), "seen", NOW)
    with pytest.raises(admission.OperationConflict):
        admission.revalidate(db, "done", NOW)


def test_confirm_retained_ledger_after_project_metadata_deletion(ledger):
    db, project, _, org = ledger
    admission.reserve(
        db, project.id, "retained", Resources(cpu=2, storage_bytes=10), NOW
    )
    admission.confirm(db, "retained", Resources(cpu=2, storage_bytes=10), "seen", NOW)
    db.delete(project)
    db.flush()
    admission.confirm(
        db, "retained", Resources(), "verified-absent", NOW + timedelta(seconds=1)
    )
    assert admission.usage(db, "organization", org.id) == (Resources(), Resources())


def test_unassigned_draft_owner_can_set_project_guardrail(ledger):
    db, project, user, _ = ledger
    user.organization_id = None
    set_limits(db, user, "project", project.id, Limits(cpu=1), NOW)
    assert (
        db.query(QuotaPolicy).filter_by(scope="project", scope_id=project.id).one().cpu
        == 1
    )
