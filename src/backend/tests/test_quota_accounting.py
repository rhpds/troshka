from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.models.managed_group import ManagedGroup
from app.models.metering import MeteringInterval, ProjectInvoice
from app.models.project import Project
from app.models.quota import QuotaCharge, QuotaReadiness
from app.models.user import User
from app.schemas.quota import AccountingUnavailable
from app.services import quota_accounting as accounting
from tests.conftest import TestSession


@pytest.fixture
def ledger():
    db = TestSession()
    org = ManagedGroup(
        name=str(uuid4()), normalized_name=str(uuid4()), is_organization=True
    )
    db.add(org)
    db.flush()
    user = User(email=f"{uuid4()}@test.invalid", organization_id=org.id)
    db.add(user)
    db.flush()
    project = Project(name="accounted", owner_id=user.id, state="draft")
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
    start = datetime(2026, 1, 31, 23, tzinfo=UTC)
    row = MeteringInterval(
        project_id=project.id,
        kind="vcpu",
        resource_id="vm",
        qty=2,
        unit_rate=3,
        started_at=start,
        ended_at=start + timedelta(hours=2),
        quota_attribution=accounting.attribution(db, project),
    )
    db.add(row)
    db.flush()
    yield db, project, user, org, row
    db.rollback()
    db.close()


def test_month_split_idempotence_and_deleted_project(ledger):
    db, project, user, org, row = ledger
    end = row.ended_at
    accounting.accrue(db, project.id, end)
    accounting.accrue(db, project.id, end)
    rows = (
        db.query(QuotaCharge)
        .filter_by(project_id=project.id)
        .order_by(QuotaCharge.start_at)
        .all()
    )
    assert [r.amount for r in rows] == [Decimal("6"), Decimal("6")]
    assert accounting.spend(db, "project", project.id, end) == Decimal("12")
    assert accounting.spend(db, "user", user.id, end) == Decimal("6")
    assert accounting.spend(db, "organization", org.id, end) == Decimal("6")
    pid = project.id
    db.query(MeteringInterval).filter_by(project_id=pid).delete()
    db.delete(project)
    db.flush()
    assert accounting.spend(db, "project", pid, end) == Decimal("12")
    assert accounting.spend(db, "user", user.id, end) == Decimal("6")
    assert rows[0].project_name == "accounted"
    assert rows[0].provenance["provider_type"] is None


def test_zero_requires_ready_baseline(ledger):
    db, project, _, _, row = ledger
    db.delete(row)
    db.flush()
    assert (
        accounting.spend(db, "project", project.id, datetime(2026, 2, 1, tzinfo=UTC))
        == 0
    )
    db.get(QuotaReadiness, "baseline").ready = False
    with pytest.raises(AccountingUnavailable):
        accounting.spend(db, "project", project.id, datetime(2026, 2, 1, tzinfo=UTC))


def test_closed_retained_accrual_after_unassignment_is_idempotent(ledger):
    db, project, user, org, row = ledger
    row.quota_attribution = accounting.attribution(db, project)
    user.organization_id = None
    db.get(QuotaReadiness, "baseline").ready = False
    accounting.accrue(db, project.id, row.ended_at, retained=True)
    accounting.accrue(db, project.id, row.ended_at, retained=True)
    charges = db.query(QuotaCharge).filter_by(project_id=project.id).all()
    assert sum(charge.amount for charge in charges) == Decimal("12")
    assert {charge.organization_id for charge in charges} == {org.id}


def test_retained_accrual_does_not_invent_missing_attribution(ledger):
    db, project, user, _, row = ledger
    row.quota_attribution = None
    user.organization_id = None
    with pytest.raises(AccountingUnavailable):
        accounting.accrue(db, project.id, row.ended_at, retained=True)
    assert db.query(QuotaCharge).filter_by(project_id=project.id).count() == 0
    assert row.quota_accrued_through is None


def test_retained_accrual_cannot_bypass_live_source_membership(ledger):
    db, project, user, _, row = ledger
    through = row.ended_at
    row.quota_attribution = accounting.attribution(db, project)
    row.ended_at = None
    user.organization_id = None
    with pytest.raises(AccountingUnavailable, match="closed source"):
        accounting.accrue(db, project.id, through, retained=True)
    assert db.query(QuotaCharge).filter_by(project_id=project.id).count() == 0


def test_transfer_splits_original_attribution(ledger):
    db, project, user, org, row = ledger
    destination = ManagedGroup(
        name=str(uuid4()), normalized_name=str(uuid4()), is_organization=True
    )
    db.add(destination)
    db.flush()
    row.ended_at = None
    boundary = row.started_at + timedelta(minutes=30)
    accounting.split_attribution(db, project.id, boundary, destination.id)
    user.organization_id = destination.id
    accounting.accrue(db, project.id, boundary + timedelta(minutes=30))
    charges = db.query(QuotaCharge).filter_by(project_id=project.id).all()
    assert {c.organization_id: c.amount for c in charges} == {
        org.id: Decimal("3"),
        destination.id: Decimal("3"),
    }


def test_accrual_precision_is_partition_invariant(ledger):
    db, project, _, _, row = ledger
    row.started_at = datetime(2026, 2, 1, tzinfo=UTC)
    row.ended_at = None
    row.qty = Decimal("1")
    row.unit_rate = Decimal("0.000001")
    for second in range(1, 4):
        accounting.accrue(db, project.id, row.started_at + timedelta(seconds=second))
    charges = db.query(QuotaCharge).filter_by(project_id=project.id).all()
    assert sum(c.amount for c in charges) == Decimal("0.000000000833")


def test_non_utc_inputs_use_utc_month(ledger):
    db, project, user, _, row = ledger
    now = row.ended_at.astimezone(timezone(timedelta(hours=-6)))
    accounting.accrue(db, project.id, now)
    assert accounting.spend(db, "user", user.id, now) == Decimal("6")


def test_unknown_live_project_is_not_zero(ledger):
    db, project, _, _, row = ledger
    db.delete(row)
    project.state = "active"
    db.flush()
    with pytest.raises(AccountingUnavailable):
        accounting.spend(db, "project", project.id, datetime(2026, 2, 1, tzinfo=UTC))


def test_summary_cannot_duplicate_interval_coverage(ledger):
    db, project, _, _, row = ledger
    with pytest.raises(AccountingUnavailable):
        accounting.import_legacy_charge(
            db,
            project_id=project.id,
            source="invoice",
            source_id="legacy",
            start=row.started_at,
            end=row.ended_at,
            amount=Decimal("12"),
        )


def test_safe_freeze_propagates_enabled_accounting_errors(ledger):
    from app.services import metering_service

    db, project, _, _, _ = ledger
    with patch.object(
        metering_service, "quotas_enabled", return_value=True
    ), patch.object(
        metering_service,
        "freeze_invoice",
        side_effect=AccountingUnavailable("unavailable"),
    ), pytest.raises(
        AccountingUnavailable
    ):
        metering_service.safe_freeze_invoice(db, project)


def test_spend_clips_future_accrual_and_backwards_cursor(ledger):
    db, project, user, _, row = ledger
    accounting.accrue(db, project.id, row.ended_at)
    accounting.accrue(db, project.id, row.started_at + timedelta(minutes=1))
    assert accounting.spend(
        db, "project", project.id, row.started_at + timedelta(minutes=30)
    ) == Decimal("3")
    assert accounting.spend(
        db, "user", user.id, row.started_at + timedelta(minutes=30)
    ) == Decimal("3")
    assert db.query(QuotaCharge).filter_by(project_id=project.id).count() == 2


def test_enabled_stopped_retains_storage_only_with_observed_power(ledger):
    from app.models.host import Host
    from app.services import metering_service

    db, project, _, _, row = ledger
    db.delete(row)
    host = Host(state="running")
    db.add(host)
    db.flush()
    project.host_id = host.id
    project.state = "stopped"
    snapshot = {
        "provider_type": "libvirt",
        "host_id": host.id,
        "project_state": "stopped",
        "host_rates": {"disk_gib_hour": 1},
        "vms": [{"id": "vm", "live_state": "stopped", "vcpus": 2, "ram_gib": 4}],
        "disks": [{"id": "disk", "size_gib": 10}],
    }
    now = datetime(2026, 2, 1, tzinfo=UTC)
    snapshot["observed_at"] = now
    with patch.object(metering_service, "quotas_enabled", return_value=True):
        metering_service.reconcile_project(db, project, now, snapshot)
        rows = (
            db.query(MeteringInterval)
            .filter_by(project_id=project.id, ended_at=None)
            .all()
        )
        assert [(r.kind, r.qty) for r in rows] == [("disk", Decimal(10))]
        snapshot["vms"][0].pop("live_state")
        with pytest.raises(AccountingUnavailable):
            metering_service.reconcile_project(db, project, now, snapshot)


def test_legacy_import_is_disjoint_and_idempotent(ledger):
    db, project, _, _, row = ledger
    start, end = row.started_at - timedelta(hours=2), row.started_at - timedelta(
        hours=1
    )
    for _ in range(2):
        accounting.import_legacy_charge(
            db,
            project_id=project.id,
            source="invoice",
            source_id="old",
            start=start,
            end=end,
            amount=Decimal("2.10"),
        )
    accounting.accrue(db, project.id, row.ended_at)
    assert accounting.spend(db, "project", project.id, row.ended_at) == Decimal("14.10")
    # Frozen displays are projections, not additive sources in quota spending.
    db.add(
        ProjectInvoice(
            project_id=project.id,
            project_name=project.name,
            owner_id=project.owner_id,
            total_usd=999,
            period_end=end,
        )
    )
    db.flush()
    assert accounting.spend(db, "project", project.id, row.ended_at) == Decimal("14.10")


def test_enabled_project_display_uses_same_charge_total(ledger):
    db, project, _, _, row = ledger
    view = accounting.project_view(db, project, row.ended_at)
    assert Decimal(view["total_usd"]) == Decimal("12")
    assert Decimal(view["by_kind"]["vcpu"]) == Decimal("12")


def test_database_error_is_unavailable(ledger):
    from sqlalchemy.exc import SQLAlchemyError

    db, project, _, _, row = ledger
    with patch.object(db, "get", side_effect=SQLAlchemyError("offline")), pytest.raises(
        AccountingUnavailable
    ):
        accounting.spend(db, "project", project.id, row.ended_at)


def test_state_cache_preserves_observation_age_on_failed_poll():
    from types import SimpleNamespace

    from app.services import ws_pubsub

    now = datetime(2026, 2, 1, tzinfo=UTC)
    project = SimpleNamespace(
        id="cache-quota",
        state="stopped",
        deploy_error=None,
        name="cache",
        topology={"nodes": []},
    )
    with patch.object(ws_pubsub, "notify_project"):
        ws_pubsub._check_and_notify_project_changes(
            project.id, project, {}, {"vm": "stopped"}, {}, {}, observed_at=now
        )
        assert ws_pubsub.get_cached_vm_states(project.id)["observed_at"] == now
        ws_pubsub._check_and_notify_project_changes(project.id, project, {}, {}, {}, {})
        cached = ws_pubsub.get_cached_vm_states(project.id)
        assert cached["observed_at"] == now
        assert cached["states"] == {"vm": "stopped"}
    ws_pubsub._last_states.pop(project.id)


def test_long_poll_batch_preserves_oldest_provider_observation():
    from types import SimpleNamespace

    from app.services import metering_service, ws_pubsub

    start = datetime(2026, 2, 1, tzinfo=UTC)
    later = start + timedelta(seconds=180)
    first = SimpleNamespace(id="early", agent_status="connected")
    second = SimpleNamespace(id="slow", agent_status="connected")
    batch = {}
    with patch(
        "app.services.troshkad_client.get_all_vm_states", return_value={"vm": "stopped"}
    ), patch.object(ws_pubsub, "datetime") as clock:
        clock.now.side_effect = [start, later]
        ws_pubsub._fetch_troshkad_host_states(first, batch)
        ws_pubsub._fetch_troshkad_host_states(second, batch)
    project = SimpleNamespace(
        host_id="early",
        host_assignments={"one": "early", "two": "slow"},
        state="stopped",
        topology={"nodes": [{"id": "vm", "type": "vmNode"}]},
    )
    observed = ws_pubsub._observation_time(project, {"vm": "stopped"}, batch, None)
    assert observed == start
    project.host_assignments = None
    assert ws_pubsub._observation_time(project, {"vm": "stopped"}, batch, None) == start
    with pytest.raises(AccountingUnavailable, match="Fresh metering observation"):
        metering_service._quota_snapshot(
            project,
            {
                "observed_at": observed,
                "provider_type": "libvirt",
                "project_state": "stopped",
            },
            later,
        )


def test_kubevirt_batch_requires_every_host_response_and_retains_oldest_time():
    from types import SimpleNamespace

    from app.services import ws_pubsub

    start = datetime(2026, 2, 1, tzinfo=UTC)
    project = SimpleNamespace(
        id="p",
        host_id="h1",
        host_assignments={"vm": "h1", "vm2": "h2"},
        topology={"nodes": [{"id": "vm", "type": "vmNode"}]},
    )
    states = {}
    with patch.object(
        ws_pubsub, "_fetch_kubevirt_vm_states", return_value={"vm": "Stopped"}
    ), patch.object(ws_pubsub, "datetime") as clock:
        clock.now.side_effect = [start, start + timedelta(seconds=180)]
        ws_pubsub._fetch_kubevirt_states_for_host(
            "h1", {"p": project}, None, None, states
        )
        assert (
            ws_pubsub._observation_time(project, {"vm": "stopped"}, {}, states["p"])
            is None
        )
        ws_pubsub._fetch_kubevirt_states_for_host(
            "h2", {"p": project}, None, None, states
        )
    assert (
        ws_pubsub._observation_time(project, {"vm": "stopped"}, {}, states["p"])
        == start
    )
