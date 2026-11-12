"""Offline fresh-era conversion; no application lifespan or transport calls."""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.models import (
    VM,
    ApiKey,
    Disk,
    Host,
    ImageCache,
    Library,
    LibraryItem,
    LibraryItemDisk,
    LibraryShare,
    ManagedGroup,
    ManagedGroupMember,
    MeteringInterval,
    MonthlyStatement,
    Network,
    Pattern,
    PatternDisk,
    PatternShare,
    Project,
    ProjectInvoice,
    ProjectShare,
    Provider,
    QuotaAction,
    QuotaAllocation,
    QuotaCharge,
    QuotaPolicy,
    QuotaReadiness,
    QuotaReservation,
    User,
)
from app.schemas.quota import AccountingUnavailable
from app.services import group_migration as migration
from app.services import quota_accounting as accounting
from tests.test_quota_concurrency import postgres as postgres_fixture

START = datetime(2026, 11, 11, 12, tzinfo=UTC)
postgres = postgres_fixture


@pytest.fixture(params=["sqlite", "postgres"])
def database(request, isolated_quota_database):
    if request.param == "sqlite":
        return isolated_quota_database
    import os

    if not os.getenv("QUOTA_TEST_POSTGRES_URL"):
        pytest.skip("disposable PostgreSQL URL not configured")
    return request.getfixturevalue("postgres")[0]


def seed(sessions):
    with sessions.begin() as db:
        # The supplied postgres fixture has unrelated existing draft identities.
        from app.services.quota_baseline import DEFAULT_ORGANIZATION_ID

        default = ManagedGroup(
            id=DEFAULT_ORGANIZATION_ID,
            name="Default organization",
            normalized_name=f"quota-default-{DEFAULT_ORGANIZATION_ID}",
        )
        groups = [
            ManagedGroup(name=n, normalized_name=n.lower(), is_organization=False)
            for n in ("CCLM", "7openshift-services")
        ]
        db.add_all([default, *groups])
        db.flush()
        users = [
            User(
                email=f"migration-{i}@test.invalid",
                organization_id=default.id,
                role="user",
                access_enabled=i != 8,
            )
            for i in range(9)
        ]
        db.add_all(users)
        db.flush()
        for i, user in enumerate(users):
            db.add(
                ManagedGroupMember(
                    group_id=groups[0 if i < 2 else 1].id, user_id=user.id
                )
            )
        provider = Provider(name="migration", type="libvirt")
        db.add(provider)
        db.flush()
        host = Host(provider_id=provider.id, state="ready")
        db.add(host)
        db.flush()
        project = Project(
            name="owned",
            owner_id=users[0].id,
            state="running",
            host_id=host.id,
            topology={"secret": "synthetic-secret"},
        )
        library = Library(owner_id=users[0].id, type="personal")
        other_library = Library(owner_id=users[2].id, type="personal")
        pattern = Pattern(
            name="pattern", owner_id=users[0].id, topology={}, state="ready"
        )
        db.add_all([project, library, other_library, pattern])
        db.flush()
        item = LibraryItem(
            library_id=library.id,
            name="image",
            type="image",
            format="qcow2",
            s3_key="unchanged/image",
            state="ready",
        )
        db.add(item)
        db.flush()
        db.add_all(
            [
                ProjectShare(project_id=project.id, user_id=users[2].id),
                PatternShare(pattern_id=pattern.id, user_id=users[2].id),
                PatternDisk(
                    pattern_id=pattern.id,
                    source_disk_id="disk",
                    s3_key="unchanged/pattern",
                    format="qcow2",
                ),
                LibraryShare(item_id=item.id, shared_with_id=users[2].id),
                LibraryItemDisk(
                    library_item_id=item.id, s3_key="unchanged/disk", format="qcow2"
                ),
                ImageCache(
                    item_id=item.id, host_id=host.id, local_path="/unchanged/cache"
                ),
                VM(project_id=project.id, host_id=host.id, name="owned-vm"),
                Disk(project_id=project.id, name="owned-disk"),
                Network(
                    project_id=project.id, name="owned-network", cidr="192.0.2.0/24"
                ),
                MeteringInterval(
                    project_id=project.id,
                    kind="vcpu",
                    resource_id="observed-vm",
                    qty=2,
                    unit_rate=3,
                    started_at=START - timedelta(days=60),
                ),
                QuotaPolicy(scope="organization", scope_id=groups[0].id, cpu=12),
                QuotaAllocation(
                    project_id=project.id,
                    owner_id=users[0].id,
                    organization_id=default.id,
                    operation_id="original-operation",
                    state="active",
                    observed={"cpu": 2},
                    observation_id="original-observation",
                ),
            ]
        )
        ids = dict(
            users=[u.id for u in users],
            groups=[g.id for g in groups],
            project=project.id,
            item=item.id,
            pattern=pattern.id,
            default=default.id,
        )
    return ids


def apply(sessions, digest=None):
    with sessions() as db:
        digest = digest or migration.inventory(db)["inventory_digest"]
    with sessions() as db:
        return migration.run(
            db,
            apply=True,
            expected_digest=digest,
            accounting_start=START,
            writers_fenced=True,
        )


def test_nine_user_conversion_preserves_assets_access_and_replay(
    database, record_property
):
    ids = seed(database)
    from fastapi import HTTPException

    from app.api.library import list_items
    from app.api.patterns import get_pattern
    from app.api.projects import get_project

    def access(db):
        owner, shared = [db.get(User, ids["users"][i]) for i in (0, 2)]
        with pytest.raises(HTTPException) as shared_project:
            get_project(ids["project"], shared, db)
        return {
            "shared_project_status": shared_project.value.status_code,
            "owner_projects": get_project(ids["project"], owner, db)["id"],
            "owner_patterns": get_pattern(ids["pattern"], owner, db)["id"],
            "shared_patterns": get_pattern(ids["pattern"], shared, db)["id"],
            "owner_images": sorted(row["id"] for row in list_items(owner, db)),
            "shared_images": sorted(row["id"] for row in list_items(shared, db)),
        }

    with database() as db:
        before = migration.inventory(db)
        visibility = access(db)
    result = apply(database, before["inventory_digest"])
    import json

    record_property(
        "sanitized_conversion_evidence",
        json.dumps(
            {key: value for key, value in result.items() if key != "mapping"},
            sort_keys=True,
        ),
    )
    assert result["accounting_started_at"] == START.isoformat()
    assert result["preservation"] == before["preservation"]
    assert (
        apply(database, before["inventory_digest"])["receipt_id"]
        == result["receipt_id"]
    )
    with database() as db:
        assert access(db) == visibility
        assert migration.preservation(db) == before["preservation"]
        assert [db.get(User, uid).organization_id for uid in ids["users"]] == [
            ids["groups"][0]
        ] * 2 + [ids["groups"][1]] * 7
        assert (
            db.query(ManagedGroupMember)
            .filter(ManagedGroupMember.user_id.in_(ids["users"]))
            .count()
            == 9
        )
        assert db.query(QuotaCharge).count() == 0
        old, successor = (
            db.query(MeteringInterval)
            .filter_by(project_id=ids["project"])
            .order_by(MeteringInterval.started_at)
            .all()
        )
        assert accounting.utc(old.ended_at) == START
        assert old.quota_attribution is None
        assert accounting.utc(successor.started_at) == START
        assert successor.quota_attribution["organization_id"] == ids["groups"][0]
        assert (successor.qty, successor.unit_rate) == (2, 3)
        ledger = (
            db.query(QuotaAllocation).filter_by(operation_id="original-operation").one()
        )
        assert ledger.observation_id == "original-observation"
        assert ledger.organization_id == ids["groups"][0]
        assert not db.get(QuotaReadiness, "baseline").ready
        with pytest.raises(AccountingUnavailable, match="baseline"):
            accounting.require_ready(db)


@pytest.mark.parametrize(
    "conflict", ["overlap", "nondefault", "stale", "pending", "action", "future_charge"]
)
def test_ambiguous_inventory_rejected_without_writes(database, conflict):
    ids = seed(database)
    with database() as db:
        digest = migration.inventory(db)["inventory_digest"]
    with database.begin() as db:
        if conflict == "overlap":
            db.add(
                ManagedGroupMember(group_id=ids["groups"][1], user_id=ids["users"][0])
            )
        elif conflict == "nondefault":
            db.get(User, ids["users"][0]).organization_id = ids["groups"][1]
        elif conflict == "stale":
            db.get(Project, ids["project"]).name = "changed"
        elif conflict == "pending":
            db.add(
                QuotaReservation(
                    project_id=ids["project"],
                    owner_id=ids["users"][0],
                    organization_id=ids["default"],
                    operation_id="ambiguous",
                    state="pending",
                )
            )
        elif conflict == "action":
            db.add(
                QuotaAction(
                    scope="organization",
                    scope_id=ids["default"],
                    policy_revision=1,
                    period_key="2026-11",
                    kind="stop",
                    threshold=100,
                    state="pending",
                )
            )
        else:
            stamp = accounting.attribution(db, db.get(Project, ids["project"]))
            db.add(
                QuotaCharge(
                    **stamp,
                    source="interval",
                    source_id="preexisting-future",
                    start_at=START,
                    end_at=START + timedelta(hours=1),
                    kind="vcpu",
                    quantity=1,
                    rate=1,
                    amount=1,
                    provenance={},
                )
            )
    if conflict != "stale":
        with database() as db:
            digest = migration.inventory(db)["inventory_digest"]
    with database() as db:
        before = migration.inventory(db)
    with pytest.raises(AccountingUnavailable):
        apply(database, digest)
    with database() as db:
        assert migration.inventory(db) == before


def test_dry_run_and_mid_transaction_failure_rollback(database):
    seed(database)
    with database() as db:
        before = migration.inventory(db)
        migration.run(db, accounting_start=START)
        assert migration.inventory(db) == before
    engine = database.kw["bind"]

    def fail(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO metering_intervals"):
            raise RuntimeError("injected successor insert failure")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            apply(database)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    with database() as db:
        assert migration.inventory(db) == before


def test_fresh_era_accrual_reporting_transfer_and_legacy_guard(database):
    ids = seed(database)
    apply(database)
    from app.services import quota_baseline
    from app.services.group_metering import group_report
    from app.services.metering_service import _freeze_quota_invoice
    from app.services.organization_service import transfer_user

    with database.begin() as db:
        accounting.accrue(db, ids["project"], START + timedelta(hours=1))
        assert sum(row.amount for row in db.query(QuotaCharge)) == Decimal(6)
        actor = db.get(User, ids["users"][1])
        actor.role = "admin"
        transfer_user(
            db, actor, ids["users"][0], ids["groups"][1], START + timedelta(hours=1)
        )
        accounting.accrue(db, ids["project"], START + timedelta(hours=2))
        assert sum(row.amount for row in db.query(QuotaCharge)) == Decimal(12)
        group = db.get(ManagedGroup, ids["groups"][0])
        report = group_report(db, group, [], 2026, 11, START + timedelta(hours=2))
        assert report["accounting_started_at"] == START.isoformat()
        assert Decimal(report["known_subtotal_usd"]) == 6
        assert report["period_start"] == datetime(2026, 11, 1, tzinfo=UTC).isoformat()
        for row in db.query(MeteringInterval).filter_by(
            project_id=ids["project"], ended_at=None
        ):
            row.ended_at = START + timedelta(hours=2)
        invoice = _freeze_quota_invoice(
            db,
            db.get(Project, ids["project"]),
            START + timedelta(hours=2),
            refresh_live=False,
        )
        assert invoice.total_usd == Decimal(12)
    with database() as db, pytest.raises(AccountingUnavailable, match="fresh"):
        quota_baseline.run(db, now=START + timedelta(hours=2), apply=True)


def test_old_stamped_sources_charges_and_documents_never_import(database):
    ids = seed(database)
    from unittest.mock import patch

    from app.services import quota_baseline
    from app.services.group_metering import group_report

    with database.begin() as db:
        project = db.get(Project, ids["project"])
        stamp = accounting.attribution(db, project)
        old = MeteringInterval(
            project_id=project.id,
            kind="vcpu",
            resource_id="observed-vm",
            qty=10,
            unit_rate=10,
            started_at=START - timedelta(hours=10),
            ended_at=START - timedelta(hours=5),
            quota_attribution=stamp,
        )
        db.add(old)
        db.flush()
        db.add(
            QuotaCharge(
                **stamp,
                source="interval",
                source_id=old.id,
                start_at=START - timedelta(hours=10),
                end_at=START - timedelta(hours=9),
                kind="ram_gb",
                quantity=10,
                rate=10,
                amount=100,
                provenance={},
            )
        )
        db.add(
            ProjectInvoice(
                project_id=project.id,
                project_name=project.name,
                owner_id=project.owner_id,
                total_usd=9999,
                period_end=START - timedelta(days=1),
            )
        )
        db.add(
            MonthlyStatement(
                owner_id=project.owner_id,
                total_usd=8888,
                period_start=START - timedelta(days=60),
                period_end=START - timedelta(days=30),
            )
        )
    apply(database)
    with database.begin() as db:
        accounting.accrue(db, ids["project"], START + timedelta(hours=1))
        assert sum(
            row.amount for row in accounting.charge_rows(db, "project", ids["project"])
        ) == Decimal(6)
        assert db.query(QuotaCharge).count() == 2
        group = db.get(ManagedGroup, ids["groups"][0])
        report = group_report(db, group, [], 2026, 11, START + timedelta(hours=1))
        assert Decimal(report["known_subtotal_usd"]) == Decimal(6)
        assert (
            db.query(ProjectInvoice)
            .filter_by(project_id=ids["project"])
            .one()
            .total_usd
            == 9999
        )
        assert (
            db.query(MonthlyStatement)
            .filter_by(owner_id=ids["users"][0])
            .one()
            .total_usd
            == 8888
        )
        with pytest.raises(AccountingUnavailable, match="fresh"):
            accounting.import_legacy_charge(
                db,
                project_id=ids["project"],
                source="invoice",
                source_id="never-import",
                start=START - timedelta(hours=2),
                end=START - timedelta(hours=1),
                amount=Decimal(99),
            )
        # Overlapping pre-era owner history must not block a normal new transfer.
        from app.services.organization_service import transfer_user

        actor = db.get(User, ids["users"][1])
        actor.role = "admin"
        transfer_user(
            db, actor, ids["users"][0], ids["groups"][1], START + timedelta(hours=1)
        )
        accounting.accrue(db, ids["project"], START + timedelta(hours=2))
        assert sum(
            row.amount for row in accounting.charge_rows(db, "project", ids["project"])
        ) == Decimal(12)
    with database() as db, patch.object(
        quota_baseline, "_history", side_effect=AssertionError("must not import")
    ):
        with pytest.raises(AccountingUnavailable, match="fresh"):
            quota_baseline.run(db, apply=True)


@pytest.mark.parametrize(
    "case", ["no_fence", "no_digest", "no_start", "future", "enabled", "replay"]
)
def test_apply_contract_rejects_unsafe_or_conflicting_requests(
    database, monkeypatch, case
):
    seed(database)
    with database() as db:
        digest = migration.inventory(db)["inventory_digest"]
    options = dict(
        apply=True, expected_digest=digest, accounting_start=START, writers_fenced=True
    )
    if case == "replay":
        apply(database, digest)
        options["accounting_start"] = START + timedelta(seconds=1)
    elif case == "future":
        options["accounting_start"] = datetime.now(UTC) + timedelta(days=1)
    elif case == "enabled":
        monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    else:
        key = {
            "no_fence": "writers_fenced",
            "no_digest": "expected_digest",
            "no_start": "accounting_start",
        }[case]
        options[key] = None
    with database() as db:
        before = migration.inventory(db)
        with pytest.raises(AccountingUnavailable):
            migration.run(db, **options)
        assert migration.inventory(db) == before


def test_only_unrelated_tunnel_auth_timestamp_is_volatile(database):
    ids = seed(database)
    with database.begin() as db:
        key = ApiKey(
            user_id=ids["users"][0],
            name="synthetic-tunnel",
            key_hash="synthetic-key-hash",
            key_prefix="test",
        )
        db.add(key)
        db.flush()
        key_id = key.id
    with database() as db:
        before = migration.inventory(db)
    with database.begin() as db:
        db.get(ApiKey, key_id).last_used_at = START
    with database() as db:
        assert migration.inventory(db) == before
    with database.begin() as db:
        db.get(ApiKey, key_id).scopes = ["changed-access"]
    with database() as db:
        assert migration.inventory(db)["inventory_digest"] != before["inventory_digest"]


def test_arbitrary_default_named_group_is_not_the_foundation_identity(database):
    ids = seed(database)
    with database.begin() as db:
        for index, uid in enumerate(ids["users"]):
            db.get(User, uid).organization_id = ids["groups"][0 if index < 2 else 1]
        for row in db.query(QuotaAllocation).all():
            row.organization_id = ids["groups"][0]
        db.flush()
        db.delete(db.get(ManagedGroup, ids["default"]))
        db.add(ManagedGroup(name="Default", normalized_name="default"))
    with pytest.raises(AccountingUnavailable, match="foundation"):
        apply(database)


@pytest.mark.parametrize("operation", ["accrue", "transfer"])
def test_unsplit_crossing_source_after_conversion_fails_closed(database, operation):
    ids = seed(database)
    apply(database)
    with database.begin() as db:
        project = db.get(Project, ids["project"])
        db.add(
            MeteringInterval(
                project_id=project.id,
                kind="vcpu",
                resource_id="unexpected-old-writer",
                qty=1,
                unit_rate=1,
                started_at=START - timedelta(hours=1),
                quota_attribution=accounting.attribution(db, project),
            )
        )
    with database() as db:
        before = migration.inventory(db)
    with pytest.raises(AccountingUnavailable, match="pre-fresh"):
        with database.begin() as db:
            if operation == "accrue":
                accounting.accrue(db, ids["project"], START + timedelta(hours=1))
            else:
                from app.services.organization_service import transfer_user

                actor = db.get(User, ids["users"][1])
                actor.role = "admin"
                transfer_user(
                    db,
                    actor,
                    ids["users"][0],
                    ids["groups"][1],
                    START + timedelta(hours=1),
                )
    with database() as db:
        assert migration.inventory(db) == before


@pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL URL not configured",
)
def test_operator_cli_dry_run_apply_and_exact_replay(postgres, monkeypatch, capsys):
    import json
    import runpy
    import sys
    from pathlib import Path

    from sqlalchemy import text

    sessions, _, engine = postgres
    seed(sessions)
    with engine.connect() as connection:
        schema = connection.execute(text("SELECT current_schema()")).scalar_one()
    target = engine.url.update_query_dict({"options": f"-c search_path={schema}"})
    monkeypatch.setenv(
        "GROUP_MIGRATION_TEST_TARGET", target.render_as_string(hide_password=False)
    )
    monkeypatch.setenv("TROSHKA_DATABASE__URL", os.environ["TROSHKA_DATABASE__URL"])
    main = runpy.run_path(
        str(Path(__file__).resolve().parents[3] / "scripts/migrate_groups.py")
    )["main"]
    args = [
        "migrate_groups.py",
        "--database-url-env",
        "GROUP_MIGRATION_TEST_TARGET",
        "--accounting-start",
        START.isoformat(),
    ]
    monkeypatch.setattr(sys, "argv", args + ["--dry-run"])
    assert main() == 0
    dryrun = json.loads(capsys.readouterr().out)
    assert dryrun["mode"] == "dry-run"
    with sessions() as db:
        assert db.get(QuotaReadiness, migration.RECEIPT_ID) is None
    monkeypatch.setattr(
        sys,
        "argv",
        args
        + [
            "--apply",
            "--expected-digest",
            dryrun["inventory_digest"],
            "--writers-fenced",
        ],
    )
    assert main() == 0
    output = capsys.readouterr().out
    applied = json.loads(output)
    assert "mapping" not in applied
    assert "migration-0@" not in output
    assert "postgresql" not in output
    assert main() == 0
    assert json.loads(capsys.readouterr().out) == applied


@pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL URL not configured",
)
def test_postgres_revision_changes_only_future_group_default(postgres):
    import importlib.util
    from pathlib import Path
    from uuid import uuid4

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    _, _, engine = postgres
    filename = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/8fbde773a642_unified_group_default.py"
    )
    spec = importlib.util.spec_from_file_location("group_default_revision", filename)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    old_id, new_id = str(uuid4()), str(uuid4())
    statement = text(
        "INSERT INTO managed_groups (id,name,normalized_name,description) VALUES (:id,:name,:name,'')"
    )
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            revision.downgrade()
            connection.execute(statement, {"id": old_id, "name": "before-default"})
            revision.upgrade()
            connection.execute(statement, {"id": new_id, "name": "after-default"})
        rows = {
            str(identity): value
            for identity, value in connection.execute(
                text(
                    "SELECT id,is_organization FROM managed_groups WHERE id IN (:old,:new)"
                ),
                {"old": old_id, "new": new_id},
            ).all()
        }
    assert rows[old_id] is False
    assert rows[new_id] is True
