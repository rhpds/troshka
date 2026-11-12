"""Frozen totals and provisional interval accounting for current members."""

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

from app.models.managed_group import (
    ManagedGroup,
    ManagedGroupManager,
    ManagedGroupMember,
)
from app.models.metering import MeteringInterval, MonthlyStatement
from app.models.project import Project
from app.services.group_metering import group_report
from tests.conftest import TestSession
from tests.test_groups_api import actors as group_actors
from tests.test_groups_api import make_group
from tests.test_metering_api import _add_invoice, _add_statement, _as_user, client

START = datetime(2026, 11, 1, tzinfo=UTC)
actors = group_actors
NOW = datetime(2026, 11, 1, 2, tzinfo=UTC)
END = datetime(2026, 12, 1, tzinfo=UTC)


def legacy_group(manager, member):
    """Retained legacy arithmetic fixture; new API groups are organizations.

    Unified organization reporting replaces this coverage in the reporting task.
    """
    with TestSession.begin() as db:
        group = ManagedGroup(
            name=uuid.uuid4().hex,
            normalized_name=uuid.uuid4().hex,
            is_organization=False,
        )
        db.add(group)
        db.flush()
        db.add(ManagedGroupManager(group_id=group.id, user_id=manager.id))
        db.add(ManagedGroupMember(group_id=group.id, user_id=member.id))
        return group.id


def setup_usage(db, owner):
    project = Project(name="Live", owner_id=owner.id, state="active")
    db.add(project)
    db.flush()
    for qty in [2, 4]:
        db.add(
            MeteringInterval(
                project_id=project.id,
                kind="vcpu",
                resource_id=uuid.uuid4().hex,
                qty=qty,
                unit_rate=0.5,
                started_at=datetime(2026, 10, 31, 23, tzinfo=UTC),
            )
        )
    db.flush()
    return project


def test_provisional_clipping_deleted_drafts_and_duplicate_suppression(actors):
    admin, manager, member, _, _ = actors
    gid = legacy_group(manager, member)
    with TestSession() as db:
        project = setup_usage(db, member)
        db.add(
            MonthlyStatement(
                owner_id=member.id,
                period_start=START,
                period_end=END,
                total_usd=999,
                status="draft",
                line_items={
                    "by_project": [
                        {
                            "project_id": project.id,
                            "project_name": "Old",
                            "subtotal": 999,
                            "by_kind": [],
                        },
                        {
                            "project_id": "deleted",
                            "project_name": "Deleted",
                            "subtotal": 3,
                            "by_kind": [],
                        },
                    ]
                },
            )
        )
        db.commit()
        result = group_report(
            db,
            db.get(ManagedGroup, gid),
            [db.get(type(member), member.id)],
            2026,
            11,
            NOW,
        )
    report = result["members"][0]
    assert report["status"] == "provisional"
    assert report["total_usd"] == 9
    assert len(report["by_project"]) == 2
    live = next(p for p in report["by_project"] if p["project_name"] == "Live")
    assert live["by_kind"][0]["qty_hours"] == 12
    assert live["by_kind"][0]["hours"] == 2
    assert live["by_kind"][0]["unit_rate"] == 0.5
    assert (
        result["active_projects"][0]["total_usd"] == 9
    )  # lifetime includes October hour
    assert result["total_usd"] == 9  # active lifetime is never added


def test_frozen_final_precedence_and_unavailable(actors):
    admin, manager, member, peer, _ = actors
    gid = legacy_group(manager, member)
    with TestSession() as db:
        setup_usage(db, member)
        frozen = [
            {
                "project_id": "frozen",
                "project_name": "Frozen",
                "subtotal": 42,
                "by_kind": [],
            }
        ]
        db.add(
            MonthlyStatement(
                owner_id=member.id,
                period_start=START,
                period_end=END,
                total_usd=42,
                status="final",
                line_items={"by_project": frozen},
            )
        )
        db.commit()
        users = [db.get(type(member), member.id), db.get(type(peer), peer.id)]
        result = group_report(db, db.get(ManagedGroup, gid), users, 2026, 11, NOW)
    assert result["members"][0]["total_usd"] == 42
    assert result["members"][0]["by_project"] == frozen
    assert result["members"][0]["status"] == "final"
    assert result["members"][1]["status"] == "unavailable"
    assert result["members"][1]["total_usd"] is None


def test_group_documents_owner_filter_and_disabled_metering(actors):
    admin, manager, member, peer, _ = actors
    gid = make_group(admin, manager, member)
    invoice = _add_invoice(member.id, "History")
    statement = _add_statement(member.id)
    _add_invoice(peer.id, "Forbidden")
    _as_user(manager)
    assert [
        r["id"] for r in client.get(f"/api/v1/metering/groups/{gid}/invoices").json()
    ] == [invoice]
    assert [
        r["id"] for r in client.get(f"/api/v1/metering/groups/{gid}/statements").json()
    ] == [statement]
    assert invoice not in {
        r["id"] for r in client.get("/api/v1/metering/invoices?own_only=true").json()
    }
    assert (
        client.get(
            f"/api/v1/metering/groups/{gid}/invoices?owner_id={peer.id}"
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


def test_empty_group_and_draft_zero(actors):
    admin, manager, member, _, _ = actors
    gid = legacy_group(manager, member)
    with TestSession() as db:
        result = group_report(db, db.get(ManagedGroup, gid), [], 2026, 11, NOW)
        assert result["members"] == []
        assert result["total_usd"] == 0
        db.add(
            MonthlyStatement(
                owner_id=member.id,
                period_start=START,
                period_end=END,
                total_usd=0,
                status="draft",
                line_items={"by_project": []},
            )
        )
        db.commit()
        result = group_report(
            db,
            db.get(ManagedGroup, gid),
            [db.get(type(member), member.id)],
            2026,
            11,
            NOW,
        )
        assert result["members"][0]["status"] == "provisional"
        assert result["members"][0]["total_usd"] == 0


def test_closed_intervals_quantity_units_and_zero_replacement(actors):
    admin, manager, member, _, _ = actors
    gid = legacy_group(manager, member)
    with TestSession() as db:
        project = Project(name="Closed", owner_id=member.id, state="stopped")
        db.add(project)
        db.flush()
        for kind, qty, rate in [("ram", 8, 0.1), ("disk", 20, 0.01), ("eip", 1, 0.5)]:
            db.add(
                MeteringInterval(
                    project_id=project.id,
                    kind=kind,
                    resource_id=kind,
                    qty=qty,
                    unit_rate=rate,
                    started_at=START,
                    ended_at=datetime(2026, 11, 1, 1, tzinfo=UTC),
                )
            )
        db.add(
            MonthlyStatement(
                owner_id=member.id,
                period_start=datetime(2026, 12, 1, tzinfo=UTC),
                period_end=datetime(2027, 1, 1, tzinfo=UTC),
                total_usd=500,
                status="draft",
                line_items={
                    "by_project": [{"project_id": project.id, "subtotal": 500}]
                },
            )
        )
        db.commit()
        members = [db.get(type(member), member.id)]
        group = db.get(ManagedGroup, gid)
        report = group_report(db, group, members, 2026, 11, NOW)
        assert report["total_usd"] == 1.5
        kinds = {k["kind"]: k for k in report["members"][0]["by_project"][0]["by_kind"]}
        assert kinds["ram"]["qty_hours"] == 8
        assert kinds["disk"]["qty_hours"] == 20
        assert kinds["eip"]["qty_hours"] == 1
        later = group_report(
            db, group, members, 2026, 12, datetime(2027, 1, 1, tzinfo=UTC)
        )
        assert later["total_usd"] == 0
        assert later["members"][0]["by_project"] == []


def test_active_spend_distinguishes_missing_intervals_from_recorded_zero(actors):
    admin, manager, member, _, _ = actors
    gid = legacy_group(manager, member)
    with TestSession() as db:
        missing = Project(
            name="No interval evidence", owner_id=member.id, state="active"
        )
        free = Project(name="Recorded free usage", owner_id=member.id, state="active")
        db.add_all([missing, free])
        db.flush()
        db.add(
            MeteringInterval(
                project_id=free.id,
                kind="vcpu",
                resource_id="free-vcpu",
                qty=2,
                unit_rate=0,
                started_at=START,
            )
        )
        db.commit()
        result = group_report(
            db,
            db.get(ManagedGroup, gid),
            [db.get(type(member), member.id)],
            2026,
            11,
            NOW,
        )
        active = {project["id"]: project for project in result["active_projects"]}
        assert active[missing.id]["total_usd"] is None
        assert active[missing.id]["line_items"]["by_kind"] == []
        assert active[free.id]["total_usd"] == 0
        assert active[free.id]["line_items"]["by_kind"][0]["qty_hours"] == 4
