"""Read-only employer attribution, incomplete evidence and private owner history."""

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import event

from app.models.elastic_ip import ElasticIp
from app.models.managed_group import ManagedGroup, ManagedGroupMember
from app.models.metering import MeteringInterval, MonthlyStatement, ProjectInvoice
from app.models.project import Project
from app.models.provider import Provider
from app.models.quota import QuotaCharge, QuotaReadiness
from app.models.user import User
from app.services import group_metering
from app.services import quota_accounting as accounting
from app.services.group_metering import group_report, scoped_members
from app.services.organization_service import transfer_user
from tests.conftest import TestSession
from tests.test_groups_api import actors as group_actors
from tests.test_groups_api import make_group
from tests.test_metering_api import _add_invoice, _add_statement, _as_user, client
from tests.test_quota_concurrency import postgres as postgres_fixture

START = datetime(2026, 11, 1, tzinfo=UTC)
NOW = START + timedelta(hours=2)
END = datetime(2026, 12, 1, tzinfo=UTC)
actors = group_actors
postgres = postgres_fixture


class CountedCharges(list):
    """Count actual production ledger visits, independently of elapsed time."""

    def __init__(self, values):
        super().__init__(values)
        self.visits = 0

    def __iter__(self):
        for value in super().__iter__():
            self.visits += 1
            yield value


def disjoint_charge(index):
    return SimpleNamespace(
        id=str(index),
        project_id="project",
        source="interval",
        source_id="source",
        organization_id="group",
        start_at=START + timedelta(minutes=index),
        end_at=START + timedelta(minutes=index + 1),
    )


def test_lifetime_evidence_without_summaries_has_bounded_charge_visits():
    charges = CountedCharges(disjoint_charge(index) for index in range(1000))
    rows = group_metering._evidence([], charges, "group", NOW)
    assert len(rows) == 1000
    assert {row.id for row in rows} == {str(index) for index in range(1000)}
    assert charges.visits <= 8000


@pytest.mark.parametrize("kind", ["resource", "charge"])
def test_disjoint_lifetime_intervals_have_bounded_time_boundary_visits(
    monkeypatch, kind
):
    calls = 0
    real_utc = group_metering.utc

    def counted_utc(value):
        nonlocal calls
        calls += 1
        return real_utc(value)

    monkeypatch.setattr(group_metering, "utc", counted_utc)
    charges = [disjoint_charge(index) for index in range(1000)]
    if kind == "charge":
        invalid = group_metering._charge_conflicts(charges)
    else:
        sources = [
            SimpleNamespace(id=row.id, started_at=row.start_at, ended_at=row.end_at)
            for row in charges
        ]
        invalid = group_metering._resource_overlaps(sources, NOW)
    assert invalid == set()
    assert calls <= 6000


@pytest.mark.parametrize("kind", ["resource", "charge"])
def test_overlap_sweeps_mark_every_nested_and_chained_conflict(kind):
    spans = [
        ("outer", 0, 10),
        ("nested", 1, 2),
        ("nested_peer", 2, 3),
        ("bridge", 9, 12),
        ("chain", 11, 13),
        ("adjacent", 13, 14),
        ("far", 20, 21),
    ]
    charges = [
        SimpleNamespace(
            id=identity,
            project_id="project",
            source="interval",
            source_id="source",
            start_at=START + timedelta(hours=start),
            end_at=START + timedelta(hours=end),
        )
        for identity, start, end in reversed(spans)
    ]
    if kind == "charge":
        invalid = group_metering._charge_conflicts(charges)
    else:
        invalid = group_metering._resource_overlaps(
            [
                SimpleNamespace(id=row.id, started_at=row.start_at, ended_at=row.end_at)
                for row in charges
            ],
            START + timedelta(hours=22),
        )
    assert invalid == {"outer", "nested", "nested_peer", "bridge", "chain"}


def source(db, member, *, rate="0.5", kind="vcpu", start=START, end=None):
    project = Project(name="Original project", owner_id=member.id, state="active")
    db.add(project)
    db.flush()
    interval = MeteringInterval(
        project_id=project.id,
        kind=kind,
        resource_id=uuid.uuid4().hex,
        qty=2,
        unit_rate=Decimal(rate),
        started_at=start,
        ended_at=end,
        quota_attribution=accounting.attribution(db, project),
    )
    db.add(interval)
    db.flush()
    return project, interval


def report(db, gid, *, now=NOW, month=11, owner_id=None):
    return group_report(
        db,
        db.get(ManagedGroup, gid),
        scoped_members(db, gid),
        2026,
        month,
        now,
        owner_id=owner_id,
    )


def document_bytes(db, invoice, statement):
    return json.dumps(
        [
            {
                column.name: str(getattr(row, column.name))
                for column in row.__table__.columns
            }
            for row in [
                db.get(ProjectInvoice, invoice),
                db.get(MonthlyStatement, statement),
            ]
        ],
        sort_keys=True,
    )


def test_stamped_money_without_baseline_ignores_documents_and_clips_month(actors):
    admin, manager, member, _, _ = actors
    gid = make_group(admin, manager, member)
    invoice = _add_invoice(member.id, "Owner history", 999)
    statement = _add_statement(member.id, 999)
    with TestSession() as db:
        project, interval = source(db, member, start=START - timedelta(hours=1))
        accounting.accrue(db, project.id, START + timedelta(hours=1))
        db.commit()
        before = document_bytes(db, invoice, statement)
        with patch(
            "app.services.quota_accounting.refresh",
            side_effect=AssertionError("report refreshed"),
        ), patch(
            "app.services.metering_service.reconcile_project",
            side_effect=AssertionError("provider poll"),
        ):
            result = report(db, gid)
        assert result["known_subtotal_usd"] == "2.000000000000"
        assert result["total_usd"] is None
        assert result["accounting_status"] == "partial"
        assert result["capacity_state"] == "unverified"
        assert (
            result["members"][0]["by_project"][0]["project_name"] == "Original project"
        )
        assert result["active_projects"][0]["known_subtotal_usd"] == "3.000000000000"
        assert document_bytes(db, invoice, statement) == before
        assert interval.quota_accrued_through.replace(tzinfo=UTC) == START + timedelta(
            hours=1
        )
        assert db.query(QuotaCharge).filter_by(project_id=project.id).count() == 2
        assert not db.new and not db.dirty


def test_transfer_history_is_scoped_and_former_owner_documents_are_revoked(actors):
    admin, manager, member, peer, _ = actors
    first = make_group(admin, manager, member)
    second = make_group(admin, peer, peer)
    invoice = _add_invoice(member.id, "Owner lifetime", 42)
    statement = _add_statement(member.id, 42)
    with TestSession() as db:
        before = document_bytes(db, invoice, statement)
        project, _ = source(db, member, rate="3")
        transfer_user(db, admin, member.id, second, START + timedelta(hours=1))
        project.name = "Confidential new employer name"
        project.state = "stopped"
        source(db, member, rate="100")
        db.commit()
        old = report(db, first)
        new = report(db, second)
        assert old["known_subtotal_usd"] == "6.000000000000"
        assert old["members"][0]["current_member"] is False
        assert old["members"][0]["owner_email"] is None
        assert old["members"][0]["by_project"][0]["project_name"] == "Original project"
        assert old["active_projects"] == []
        assert "Confidential" not in json.dumps(old)
        assert new["known_subtotal_usd"] == "406.000000000000"
        assert old["total_usd"] is None
        assert document_bytes(db, invoice, statement) == before
    _as_user(manager)
    path = f"/api/v1/metering/groups/{first}"
    detailed = client.get(f"{path}/report?year=2026&month=11&owner_id={member.id}")
    assert detailed.status_code == 200
    assert {
        row["project_name"] for row in detailed.json()["members"][0]["by_project"]
    } == {"Original project"}
    assert client.get(f"{path}/invoices?owner_id={member.id}").status_code == 404
    assert client.get(f"{path}/statements?owner_id={member.id}").status_code == 404
    assert client.get(f"/api/v1/metering/invoices/{invoice}").status_code == 403
    assert client.get(f"/api/v1/metering/statements/{statement}").status_code == 403
    assert (
        client.get(f"{path}/report?year=2026&month=11&owner_id={peer.id}").status_code
        == 404
    )
    _as_user(peer)
    assert client.get(f"{path}/report?year=2026&month=11").status_code == 404


def test_missing_native_sources_never_become_complete_or_free(actors):
    admin, manager, member, _, _ = actors
    gid = make_group(admin, manager, member)
    with TestSession() as db:
        free, _ = source(db, member, rate="0")
        missing = Project(
            name="Native missing evidence", owner_id=member.id, state="active"
        )
        db.add(missing)
        db.merge(
            QuotaReadiness(
                id="baseline", ready=True, details={"accounting_ready": True}
            )
        )
        db.flush()
        result = report(db, gid)
        assert result["total_usd"] is None
        assert result["known_subtotal_usd"] == "0.000000000000"
        assert result["members"][0]["total_usd"] is None
        active = {p["id"]: p for p in result["active_projects"]}
        assert active[missing.id]["total_usd"] is None
        assert active[missing.id]["accounting_status"] == "unavailable"
        assert active[free.id]["known_subtotal_usd"] == "0.000000000000"
        assert active[free.id]["accounting_status"] == "partial"


def test_empty_and_draft_zero_require_positive_absence_of_history(actors):
    admin, manager, member, _, _ = actors
    gid = make_group(admin, manager, member)
    with TestSession() as db:
        db.add(Project(name="Never deployed", owner_id=member.id, state="draft"))
        db.flush()
        assert report(db, gid)["total_usd"] == "0"
        db.add(
            MonthlyStatement(
                owner_id=member.id,
                period_start=START,
                period_end=END,
                total_usd=0,
                status="draft",
                line_items={},
            )
        )
        db.flush()
        result = report(db, gid)
        assert result["total_usd"] is None
        assert result["known_subtotal_usd"] == "0"
        assert result["accounting_status"] == "unavailable"


def test_draft_retained_external_ip_is_not_proven_zero(actors):
    admin, manager, member, _, _ = actors
    gid = make_group(admin, manager, member)
    with TestSession() as db:
        project = Project(
            name="Draft with retained IP", owner_id=member.id, state="draft"
        )
        provider = Provider(name=uuid.uuid4().hex, type="aws")
        db.add_all([project, provider])
        db.flush()
        db.add(
            ElasticIp(
                provider_id=provider.id,
                project_id=project.id,
                canvas_eip_id="ip",
                allocation_id="retained",
                public_ip="192.0.2.1",
            )
        )
        db.flush()
        result = report(db, gid)
        assert result["total_usd"] is None
        assert result["known_subtotal_usd"] == "0"


def test_closed_decimal_sources_and_future_month_zero_are_explicitly_incomplete(actors):
    admin, manager, member, _, _ = actors
    gid = make_group(admin, manager, member)
    with TestSession() as db:
        source(db, member, rate="0.123456", end=START + timedelta(microseconds=1))
        result = report(db, gid)
        assert result["known_subtotal_usd"] == "0.000000000069"
        assert result["total_usd"] is None
        later = report(db, gid, month=12, now=END)
        assert later["known_subtotal_usd"] == "0"
        assert later["total_usd"] is None
        assert later["members"][0]["by_project"] == []


@pytest.mark.parametrize(
    "broken", ["unknown", "cursor_gap", "overlap", "wrong_stamp", "summary_overlap"]
)
def test_ambiguous_sources_do_not_invent_known_money(actors, broken):
    admin, manager, member, _, _ = actors
    gid = make_group(admin, manager, member)
    with TestSession() as db:
        project, interval = source(db, member)
        if broken == "unknown":
            interval.quota_attribution = None
        elif broken == "cursor_gap":
            interval.quota_accrued_through = START + timedelta(hours=1)
        elif broken == "wrong_stamp":
            interval.quota_attribution = {
                **interval.quota_attribution,
                "project_id": str(uuid.uuid4()),
            }
        elif broken == "overlap":
            db.add(
                MeteringInterval(
                    project_id=project.id,
                    kind="vcpu",
                    resource_id=interval.resource_id,
                    qty=2,
                    unit_rate=Decimal("0.5"),
                    started_at=START,
                    quota_attribution=interval.quota_attribution,
                )
            )
        else:
            accounting.import_legacy_charge(
                db,
                project_id=str(uuid.uuid4()),
                source="invoice",
                source_id=str(uuid.uuid4()),
                start=START,
                end=NOW,
                amount=Decimal("7"),
                identity={**interval.quota_attribution, "project_id": project.id},
            )
        db.flush()
        result = report(db, gid)
        assert result["total_usd"] is None
        assert result["known_subtotal_usd"] == (
            "7.000000000000" if broken == "summary_overlap" else "0"
        )
        assert result["members"][0]["total_usd"] is None


def test_legacy_roster_cannot_restore_reporting_membership(actors):
    admin, manager, member, peer, _ = actors
    gid = make_group(admin, manager, member)
    with TestSession() as db:
        db.get(ManagedGroup, gid).is_organization = False
        db.add(ManagedGroupMember(group_id=gid, user_id=peer.id))
        db.flush()
        assert [u.id for u in scoped_members(db, gid)] == [member.id]


def test_cached_source_cursor_and_multiple_segments_cannot_duplicate_tails(actors):
    admin, manager, member, _, _ = actors
    gid = make_group(admin, manager, member)
    with TestSession() as reader:
        project, interval = source(reader, member)
        reader.commit()
        assert interval.quota_accrued_through is None
        with TestSession.begin() as writer:
            accounting.accrue(writer, project.id, START + timedelta(minutes=30))
            accounting.accrue(writer, project.id, START + timedelta(hours=1))
        result = report(reader, gid)
        assert result["known_subtotal_usd"] == "2.000000000000"
        assert interval.quota_accrued_through.replace(tzinfo=UTC) == START + timedelta(
            hours=1
        )
        assert reader.query(QuotaCharge).filter_by(project_id=project.id).count() == 2


@pytest.mark.parametrize("summary", [False, True])
def test_conflicting_materialized_coverage_is_never_added_twice(actors, summary):
    admin, manager, member, _, _ = actors
    gid = make_group(admin, manager, member)
    with TestSession() as db:
        project, interval = source(db, member)
        accounting.accrue(db, project.id, START + timedelta(hours=1))
        db.add(
            QuotaCharge(
                **interval.quota_attribution,
                source="legacy:invoice" if summary else "interval",
                source_id=str(uuid.uuid4()) if summary else interval.id,
                start_at=START + timedelta(minutes=30),
                end_at=START + timedelta(hours=1),
                kind="legacy_summary" if summary else "vcpu",
                quantity=2,
                rate=Decimal("0.5"),
                amount=Decimal("7") if summary else Decimal("0.5"),
                provenance={},
            )
        )
        db.flush()
        result = report(db, gid)
        assert result["known_subtotal_usd"] == ("7.000000000000" if summary else "0")
        assert result["total_usd"] is None


def test_deleted_project_and_owner_use_only_attributed_charge_snapshots(actors):
    admin, manager, member, _, _ = actors
    gid = make_group(admin, manager, member)
    with TestSession() as db:
        project, interval = source(db, member)
        accounting.accrue(db, project.id, NOW)
        db.delete(interval)
        db.delete(project)
        db.flush()
        db.delete(db.get(User, member.id))
        db.flush()
        result = report(db, gid)
        assert result["known_subtotal_usd"] == "2.000000000000"
        assert result["total_usd"] is None
        assert result["active_projects"] == []
        assert result["members"][0]["owner_email"] is None
        assert (
            result["members"][0]["by_project"][0]["project_name"] == "Original project"
        )


def test_former_member_with_invalid_cursor_is_unknown_rather_than_empty_zero(actors):
    admin, manager, member, _, _ = actors
    gid = make_group(admin, manager, member)
    with TestSession() as db:
        _, interval = source(db, member, end=NOW)
        interval.quota_accrued_through = START + timedelta(hours=1)
        db.get(User, member.id).organization_id = None
        db.flush()
        result = report(db, gid)
        assert result["total_usd"] is None
        assert result["known_subtotal_usd"] == "0"
        assert result["members"][0]["current_member"] is False
        assert result["members"][0]["total_usd"] is None


@pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL URL not configured",
)
@pytest.mark.parametrize("mutation", ["accrue", "transfer"])
def test_postgres_money_snapshot_survives_commit_after_select(postgres, mutation):
    sessions, (projects, users, gid, destination), engine = postgres
    with sessions.begin() as db:
        db.get(Project, projects[0]).state = "active"
        owner = db.get(User, users[0])
        project, _ = source(db, owner)
        pid = project.id
    changed = False

    def commit_after_money_select(
        _connection, _cursor, statement, _params, _context, _many
    ):
        nonlocal changed
        if changed or "FULL OUTER JOIN quota_charges" not in statement:
            return
        changed = True
        with sessions.begin() as writer:
            if mutation == "accrue":
                accounting.accrue(writer, pid, START + timedelta(hours=1))
            else:
                transfer_user(
                    writer,
                    writer.get(User, users[0]),
                    users[0],
                    destination,
                    START + timedelta(hours=1),
                )

    event.listen(engine, "after_cursor_execute", commit_after_money_select)
    try:
        with sessions() as reader:
            before_commit = report(reader, gid)
            after_commit = report(reader, gid)
        assert changed
        assert before_commit["known_subtotal_usd"] == "2.000000000000"
        assert after_commit["known_subtotal_usd"] == (
            "2.000000000000" if mutation == "accrue" else "1.000000000000"
        )
        assert before_commit["total_usd"] is None
    finally:
        event.remove(engine, "after_cursor_execute", commit_after_money_select)


def test_group_documents_owner_filter_and_disabled_metering(actors):
    admin, manager, member, peer, _ = actors
    gid = make_group(admin, manager, member)
    invoice = _add_invoice(member.id, "History")
    statement = _add_statement(member.id)
    _add_invoice(peer.id, "Forbidden")
    _as_user(manager)
    for route, identity in [("invoices", invoice), ("statements", statement)]:
        rows = client.get(f"/api/v1/metering/groups/{gid}/{route}").json()
        assert [r["id"] for r in rows] == [identity]
        assert rows[0]["billing_view"] == "owner_billing_history"
        assert (
            client.get(
                f"/api/v1/metering/groups/{gid}/{route}?owner_id={peer.id}"
            ).status_code
            == 404
        )
    assert (
        client.get(
            f"/api/v1/metering/groups/{gid}/report?year=2026&month=13"
        ).status_code
        == 422
    )
    with patch("app.api.metering.metering_enabled", return_value=False):
        assert (
            client.get(
                f"/api/v1/metering/groups/{gid}/report?year=2026&month=11"
            ).status_code
            == 404
        )
        assert client.get(f"/api/v1/groups/{gid}").status_code == 200
