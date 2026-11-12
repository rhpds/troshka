"""Disposable baseline rehearsal: no provider requests or application DB access."""

import importlib.util
import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.elastic_ip import ElasticIp
from app.models.host import Host
from app.models.managed_group import ManagedGroup, ManagedGroupMember
from app.models.metering import MeteringInterval, MonthlyStatement, ProjectInvoice
from app.models.project import Project
from app.models.provider import Provider
from app.models.quota import (
    QuotaAllocation,
    QuotaCharge,
    QuotaReadiness,
    QuotaReservation,
)
from app.models.user import User
from app.services import quota_accounting as accounting
from app.services import quota_baseline as baseline
from app.services import quota_operations as operations
from app.services.quota_policy import get_limits
from tests.test_quota_concurrency import postgres as postgres_fixture

postgres = postgres_fixture

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)
START = NOW - timedelta(hours=1)


def topology():
    return {
        "nodes": [
            {"id": "vm", "type": "vmNode", "data": {"vcpus": 2, "ram": 4}},
            {
                "id": "disk",
                "type": "storageNode",
                "data": {"size": 10, "format": "qcow2"},
            },
        ],
        "edges": [{"source": "vm", "sourceHandle": "dp-controller", "target": "disk"}],
    }


@pytest.fixture
def sample():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        default = ManagedGroup(
            id=baseline.DEFAULT_ORGANIZATION_ID,
            name="Default organization",
            normalized_name="default",
            is_organization=True,
        )
        current = ManagedGroup(
            name="Current employer", normalized_name="current", is_organization=True
        )
        db.add_all([default, current])
        db.flush()
        owner = User(email="baseline@test.invalid", organization_id=current.id)
        provider = Provider(name="baseline-provider", type="aws")
        db.add_all([owner, provider])
        db.flush()
        host = Host(
            provider_id=provider.id,
            state="ready",
            billing_mode="shared",
            metering_rates={
                "vcpu_hour": "0.1",
                "ram_gib_hour": "0.01",
                "disk_gib_hour": "0.001",
            },
        )
        db.add(host)
        db.flush()
        projects = [
            Project(
                name=state,
                owner_id=owner.id,
                state=state,
                topology=topology(),
                deployed_topology=topology() if state != "draft" else None,
                host_id=host.id if state != "draft" else None,
                budget_usd=Decimal("12.25"),
            )
            for state in ("active", "stopped", "draft")
        ]
        db.add_all(projects)
        db.flush()
        # Today's overlapping reporting membership cannot own a historical charge.
        for n in range(2):
            report = ManagedGroup(name=f"Report {n}", normalized_name=f"report-{n}")
            db.add(report)
            db.flush()
            db.add(ManagedGroupMember(group_id=report.id, user_id=owner.id))
        for project in projects[:2]:
            db.add(
                MeteringInterval(
                    project_id=project.id,
                    kind="disk",
                    resource_id="disk",
                    qty=10,
                    unit_rate=Decimal("0.001"),
                    host_id=host.id,
                    provider_type="aws",
                    started_at=START,
                )
            )
        db.add_all(
            [
                MeteringInterval(
                    project_id=projects[0].id,
                    kind=kind,
                    resource_id="vm",
                    qty=qty,
                    unit_rate=rate,
                    host_id=host.id,
                    provider_type="aws",
                    started_at=START,
                )
                for kind, qty, rate in [
                    ("vcpu", 2, Decimal("0.1")),
                    ("ram", 4, Decimal("0.01")),
                ]
            ]
        )
        deleted = str(uuid4())
        db.add(
            ProjectInvoice(
                project_id=deleted,
                project_name="Deleted",
                owner_id=owner.id,
                total_usd=Decimal("3"),
                period_start=START,
                period_end=NOW,
            )
        )
        db.add(
            ProjectInvoice(
                project_id=projects[0].id,
                project_name="active",
                owner_id=owner.id,
                total_usd=Decimal("0.25"),
                period_start=START,
                period_end=NOW,
            )
        )
        db.add(
            MonthlyStatement(
                owner_id=owner.id,
                period_start=START,
                period_end=NOW,
                total_usd=Decimal("3.25"),
                status="finalized",
                line_items={
                    "by_project": [
                        {
                            "project_id": deleted,
                            "project_name": "Deleted",
                            "subtotal": "3",
                        },
                        {
                            "project_id": projects[0].id,
                            "project_name": "active",
                            "subtotal": "0.25",
                        },
                    ]
                },
            )
        )
        db.commit()
        yield db, projects, owner, current, deleted
    engine.dispose()


def manifest(db, projects):
    entries = {}
    for project in projects:
        if project.state == "draft":
            continue
        entries[project.id] = {
            "host_id": project.host_id,
            "observed_at": NOW.isoformat(),
            "source": "operator-readonly-virsh",
            "observation_id": f"evidence:{project.id}",
            "resources": observed_resources(project),
            "storage": physical_evidence(project),
            "states": {"vm": "running" if project.state == "active" else "shutoff"},
            "jobs_checked_at": (NOW - timedelta(seconds=1)).isoformat(),
            "jobs": [],
            "worker_fenced_at": (NOW - timedelta(seconds=2)).isoformat(),
            "fence_evidence": "maintenance ticket and stopped workers; agent job inventory empty",
        }
    return {
        "version": 1,
        "inventory_digest": baseline.inventory_digest(db),
        "maintenance": {
            "writers_stopped": True,
            "history_complete": True,
            "evidence": "maintenance ticket; API, workers, schedulers and external writers stopped",
            "since": (NOW - timedelta(minutes=1)).isoformat(),
        },
        "projects": entries,
        "inflight": [],
    }


def observed_resources(project):
    result = operations.resource_map(project, project.topology)
    result["disks"] = {
        path: size for path, size in result["disks"].items() if "/shared/" not in path
    }
    return result


def physical_evidence(project):
    from tests.test_quota_storage import evidence

    result = evidence(project, generation=f"baseline:{project.id}", amount=4096)
    result["files"][0]["path"] = next(
        iter(observed_resources(project)["disks"]), result["files"][0]["path"]
    )
    result["observed_at"] = NOW.isoformat()
    return result


def snapshot(db):
    return {
        table.name: [tuple(map(str, row)) for row in db.execute(select(table)).all()]
        for table in Base.metadata.sorted_tables
    }


def test_default_dry_run_rolls_back_every_row_cursor_and_marker(sample):
    db, projects, *_ = sample
    evidence = manifest(db, projects)
    before = snapshot(db)
    report = baseline.run(db, evidence=evidence, now=NOW)
    assert report["ready"] and report["mode"] == "dry-run"
    assert snapshot(db) == before
    assert db.query(QuotaCharge).count() == 0


def test_apply_mixed_baseline_disjoint_and_idempotent(sample):
    db, projects, owner, current, deleted = sample
    evidence = manifest(db, projects)
    report = baseline.run(db, evidence=evidence, now=NOW, apply=True)
    assert report["ready"], report
    rows = db.query(QuotaCharge).all()
    assert sum(row.amount for row in rows) == Decimal("3.26")
    assert {r.organization_id for r in rows} == {baseline.DEFAULT_ORGANIZATION_ID}
    assert all(r.provenance["migration_attribution"] for r in rows)
    assert owner.organization_id == current.id
    assert db.query(QuotaCharge).filter_by(project_id=deleted).count() == 1
    assert db.query(QuotaAllocation).count() == 2
    stopped = db.query(QuotaAllocation).filter_by(project_id=projects[1].id).one()
    assert stopped.observed == dict(cpu=0, ram_bytes=0, storage_bytes=4096, projects=1)
    assert stopped.organization_id == current.id
    assert get_limits(db, "project", projects[0].id).spending_usd == Decimal("12.25")
    before = snapshot(db)
    assert baseline.run(db, evidence=evidence, now=NOW, apply=True)["ready"]
    assert snapshot(db) == before
    row = db.query(QuotaReservation).filter_by(project_id=projects[1].id).one()
    assert row.dispatch_metadata["generation_id"] == row.operation_id
    assert row.dispatch_metadata["desired_digest"] == operations.digest(
        projects[1].topology
    )
    assert row.dispatch_metadata["resources"] == operations.resource_map(
        projects[1], projects[1].topology
    )
    assert operations._start_generation([row]) == row.operation_id
    assert operations._observation_follows_worker(row, NOW)


def test_history_import_remains_useful_without_maintenance_but_not_ready(sample):
    db, *_ = sample
    report = baseline.run(db, now=NOW, apply=True)
    assert not report["ready"]
    assert db.query(QuotaCharge).count() > 0
    assert not db.get(QuotaReadiness, "baseline").ready
    with pytest.raises(accounting.AccountingUnavailable):
        accounting.require_ready(db)


@pytest.mark.parametrize(
    "change,expected",
    [
        ("override", "legacy_quota_override"),
        ("missing_rate", "rate"),
        ("unsupported", "supported"),
        ("stale", "observation"),
        ("resources", "resource"),
        ("unfenced", "fenc"),
        ("jobs", "job"),
        ("digest", "inventory"),
        ("maintenance", "maintenance"),
    ],
)
def test_untrusted_or_unsupported_baseline_never_ready(
    sample, change, expected, monkeypatch
):
    db, projects, owner, *_ = sample
    evidence = manifest(db, projects)
    entry = evidence["projects"][projects[0].id]
    if change == "override":
        owner.quota_overrides = {"old_unknown": 12}
    elif change == "missing_rate":
        db.get(Host, projects[0].host_id).metering_rates = {}
        monkeypatch.setattr(baseline.metering, "load_catalog", lambda: {})
        entry["rates"] = {}  # Cannot supply a rate from the manifest.
    elif change == "unsupported":
        projects[0].topology = {"nodes": [{"id": "container", "type": "containerNode"}]}
        projects[0].deployed_topology = deepcopy(projects[0].topology)
    elif change == "stale":
        entry["observed_at"] = (NOW - timedelta(hours=1)).isoformat()
    elif change == "resources":
        entry["resources"]["vms"] = {}
    elif change == "unfenced":
        entry.pop("fence_evidence")
    elif change == "jobs":
        entry["jobs"] = [{"id": "lost", "status": "unknown"}]
    elif change == "digest":
        evidence["inventory_digest"] = "wrong"
    else:
        evidence["maintenance"]["writers_stopped"] = False
    db.commit()
    if change not in {"digest"}:
        evidence["inventory_digest"] = baseline.inventory_digest(db)
    report = baseline.run(db, evidence=evidence, now=NOW, apply=True)
    assert not report["ready"]
    assert any(expected in str(row).lower() for row in report["blockers"]), report
    assert not db.get(QuotaReadiness, "baseline").ready
    assert owner.quota_overrides == (
        {"old_unknown": 12} if change == "override" else None
    )


def test_ambiguous_partial_summary_blocks_without_double_count(sample):
    db, projects, owner, *_ = sample
    invoice = db.query(ProjectInvoice).filter_by(project_id=projects[0].id).one()
    invoice.period_start = START - timedelta(hours=1)
    invoice.total_usd = 1
    db.commit()
    report = baseline.run(db, evidence=manifest(db, projects), now=NOW, apply=True)
    assert not report["ready"]
    assert any("coverage" in str(row).lower() for row in report["blockers"])
    assert (
        db.query(QuotaCharge)
        .filter_by(source="legacy:invoice", project_id=projects[0].id)
        .count()
        == 0
    )


def test_inflight_import_is_durable_and_cannot_claim_completion(sample):
    db, projects, *_ = sample
    evidence = manifest(db, projects)
    evidence["inflight"] = [
        {
            "operation_id": "legacy-ticket-42",
            "project_id": projects[0].id,
            "requested": dict(
                cpu=2, ram_bytes=4 * 2**30, storage_bytes=10 * 2**30, projects=1
            ),
            "provider_jobs": [{"id": "unknown-ticket", "status": "unknown"}],
        }
    ]
    report = baseline.run(db, evidence=evidence, now=NOW, apply=True)
    assert not report["ready"]
    row = db.query(QuotaReservation).filter_by(operation_id="legacy-ticket-42").one()
    assert row.state == "reconciling" and not row.dispatch_metadata["worker_finished"]
    assert row.requested["cpu"] == 2 and row.observed["cpu"] == 0
    assert db.query(QuotaAllocation).filter_by(project_id=projects[0].id).count() == 0
    assert not baseline.run(db, evidence=evidence, now=NOW, apply=True)["ready"]
    assert (
        db.query(QuotaReservation).filter_by(operation_id="legacy-ticket-42").count()
        == 1
    )


def test_unknown_deleted_owner_and_cross_month_summary_require_manual_mapping(sample):
    db, projects, *_ = sample
    invoice = db.query(ProjectInvoice).filter_by(project_name="Deleted").one()
    invoice.owner_id = None
    invoice.period_start = datetime(2026, 10, 30, tzinfo=UTC)
    db.commit()
    report = baseline.run(db, evidence=manifest(db, projects), now=NOW, apply=True)
    assert not report["ready"]
    assert any("identity" in str(row).lower() for row in report["blockers"])
    assert (
        db.query(QuotaCharge)
        .filter_by(project_id=invoice.project_id, source="legacy:invoice")
        .count()
        == 0
    )


def test_existing_stamped_history_survives_current_membership(sample):
    db, projects, owner, current, *_ = sample
    interval = (
        db.query(MeteringInterval)
        .filter_by(project_id=projects[0].id, kind="vcpu")
        .one()
    )
    interval.quota_attribution = accounting.attribution(db, projects[0])
    db.commit()
    report = baseline.run(db, evidence=manifest(db, projects), now=NOW, apply=True)
    assert report["ready"], report
    charge = db.query(QuotaCharge).filter_by(source_id=interval.id).one()
    assert charge.organization_id == current.id
    assert not charge.provenance.get("migration_attribution")


def test_cutover_keeps_history_default_and_future_current_organization(sample):
    db, projects, owner, current, *_ = sample
    report = baseline.run(db, evidence=manifest(db, projects), now=NOW, apply=True)
    assert report["ready"], report
    open_rows = (
        db.query(MeteringInterval)
        .filter_by(project_id=projects[0].id, ended_at=None)
        .all()
    )
    assert all(r.quota_attribution["organization_id"] == current.id for r in open_rows)
    assert all(accounting.utc(r.started_at) == NOW for r in open_rows)
    accounting.accrue(db, projects[0].id, NOW + timedelta(hours=1))
    assert accounting.summarize(accounting.charge_rows(db, "organization", current.id))[
        "total"
    ] == Decimal("0.25")
    assert accounting.summarize(
        accounting.charge_rows(db, "organization", baseline.DEFAULT_ORGANIZATION_ID)
    )["total"] == Decimal("3.26")


def test_actual_start_can_use_imported_stopped_generation(sample, monkeypatch):
    db, projects, *_ = sample
    assert baseline.run(db, evidence=manifest(db, projects), now=NOW, apply=True)[
        "ready"
    ]
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    monkeypatch.setattr(accounting, "spend", lambda *args: Decimal(0))
    monkeypatch.setattr(operations, "now", lambda: NOW + timedelta(seconds=1))
    operation_id = operations.prepare(db, projects[1], "start")
    row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
    assert row.requested == dict(
        cpu=2, ram_bytes=4 * 2**30, storage_bytes=0, projects=0
    )
    assert row.dispatch_metadata["generation_id"] == f"baseline:{projects[1].id}"


def test_cursor_without_complete_charge_coverage_cannot_prove_zero_history(sample):
    db, projects, *_ = sample
    interval = db.query(MeteringInterval).filter_by(project_id=projects[1].id).one()
    interval.quota_accrued_through = NOW
    db.commit()
    report = baseline.run(db, evidence=manifest(db, projects), now=NOW, apply=True)
    assert not report["ready"]
    assert any("cursor" in str(row).lower() for row in report["blockers"])


def test_late_detail_cannot_be_added_over_already_imported_summary(sample):
    db, projects, owner, *_ = sample
    project = projects[2]
    db.add(
        ProjectInvoice(
            project_id=project.id,
            project_name=project.name,
            owner_id=owner.id,
            total_usd=Decimal("1"),
            period_start=START,
            period_end=NOW,
        )
    )
    db.commit()
    assert baseline.run(db, evidence=manifest(db, projects), now=NOW, apply=True)[
        "ready"
    ]
    db.add(
        MeteringInterval(
            project_id=project.id,
            kind="vcpu",
            resource_id="old",
            qty=1,
            unit_rate=1,
            provider_type="aws",
            started_at=START,
            ended_at=NOW,
        )
    )
    db.commit()
    report = baseline.run(db, evidence=manifest(db, projects), now=NOW, apply=True)
    assert not report["ready"]
    rows = accounting.charge_rows(db, "project", project.id)
    assert accounting.summarize(rows)["total"] == Decimal(1)


def test_transitional_project_state_does_not_prove_finished_worker(sample):
    db, projects, *_ = sample
    projects[0].state = "deploying"
    db.commit()
    evidence = manifest(db, projects)
    evidence["projects"][projects[0].id]["states"] = {"vm": "running"}
    report = baseline.run(db, evidence=evidence, now=NOW, apply=True)
    assert not report["ready"]
    assert db.query(QuotaAllocation).filter_by(project_id=projects[0].id).count() == 0


def cli():
    path = Path(__file__).parents[3] / "scripts/backfill_quotas.py"
    spec = importlib.util.spec_from_file_location("backfill_quotas_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_requires_explicit_database_and_defaults_to_dryrun(monkeypatch):
    monkeypatch.delenv("QUOTA_BASELINE_DATABASE_URL", raising=False)
    module = cli()
    with pytest.raises(SystemExit) as error:
        module.arguments([])
    assert error.value.code == 2
    assert module.arguments(["--database-url", "sqlite://"]).apply is False
    assert module.arguments(["--database-url", "sqlite://", "--apply"]).apply is True


def test_cli_inventory_and_dryrun_disposable_database(sample, monkeypatch, capsys):
    db, *_ = sample
    module = cli()
    # A dedicated factory is injected so this exercises exactly the disposable
    # fixture engine, never the application's configured SessionLocal.
    monkeypatch.setattr(module, "database_engine", lambda url: db.get_bind())
    monkeypatch.setattr(db.get_bind(), "dispose", lambda: None)
    before = snapshot(db)
    assert module.main(["--database-url", "sqlite://", "--inventory"]) == 0
    assert '"inventory_digest"' in capsys.readouterr().out
    assert module.main(["--database-url", "sqlite://"]) == 2
    assert '"mode": "dry-run"' in capsys.readouterr().out
    assert snapshot(db) == before


def test_unsupported_retained_eip_is_not_omitted_from_live_inventory(sample):
    db, projects, *_ = sample
    db.add(
        ElasticIp(
            project_id=projects[0].id,
            host_id=projects[0].host_id,
            provider_id=db.get(Host, projects[0].host_id).provider_id,
            public_ip="192.0.2.1",
            canvas_eip_id="eip",
            allocation_id="allocation",
            state="allocated",
        )
    )
    db.commit()
    report = baseline.run(db, evidence=manifest(db, projects), now=NOW, apply=True)
    assert not report["ready"]
    assert any("eip" in str(item).lower() for item in report["blockers"])


def test_cli_does_not_echo_database_url_on_failure(monkeypatch, capsys):
    module = cli()
    monkeypatch.setattr(
        module, "database_engine", lambda url: (_ for _ in ()).throw(ValueError(url))
    )
    assert module.main(["--database-url", "postgresql://SECRET:SECRET@invalid/db"]) == 2
    assert "SECRET" not in capsys.readouterr().out


def test_preflight_old_schema_fails_before_orm_import(monkeypatch, capsys):
    module = cli()
    engine = create_engine("sqlite://")
    monkeypatch.setattr(module, "database_engine", lambda url: engine)
    monkeypatch.setattr(
        module, "_backend", lambda: pytest.fail("Old schema must not load the ORM")
    )
    assert module.main(["--database-url", "sqlite://", "--preflight"]) == 2
    import json

    report = json.loads(capsys.readouterr().out)
    assert report["phase"] == "schema_required"
    assert report["schema"]["expected_revisions"] == _current_schema_heads()
    assert "users" in report["schema"]["missing_columns"]
    assert not report["ready"]


def _current_schema_heads():
    from alembic.script import ScriptDirectory

    return sorted(
        ScriptDirectory(
            str(Path(__file__).resolve().parents[1] / "alembic")
        ).get_heads()
    )


def _stamp_schema(db, revision=None):
    revisions = _current_schema_heads() if revision is None else [revision]
    db.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
    db.execute(
        text("INSERT INTO alembic_version VALUES (:revision)"),
        [{"revision": item} for item in revisions],
    )
    db.commit()


def test_preflight_current_schema_is_readonly_and_does_not_enable(sample):
    db, *_ = sample
    module = cli()
    _stamp_schema(db)
    before = snapshot(db)
    report = module.preflight(db)
    assert report["deployment_ready"]
    assert not report["ready"] and not report["baseline_gate"]["ready"]
    assert report["phase"] == "baseline_required"
    assert len(report["inventory"]["projects"]) == 3
    assert snapshot(db) == before


def test_preflight_rejects_wrong_schema_revision_without_orm(sample, monkeypatch):
    db, *_ = sample
    module = cli()
    _stamp_schema(db, "e47c2a9b6103")
    monkeypatch.setattr(
        module, "_backend", lambda: pytest.fail("Wrong revision must not load ORM")
    )
    report = module.preflight(db)
    assert not report["deployment_ready"]
    assert report["schema"]["actual_revisions"] == ["e47c2a9b6103"]


def test_preflight_enabled_enforcement_is_not_offline_rollout_ready(
    sample, monkeypatch
):
    db, *_ = sample
    _stamp_schema(db)
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    report = cli().preflight(db)
    assert not report["deployment_ready"]
    assert report["phase"] == "enforcement_already_enabled"


def test_preflight_preserves_v1_allocations_and_requires_reconciliation(sample):
    db, projects, *_ = sample
    assert baseline.run(db, evidence=manifest(db, projects), now=NOW, apply=True)[
        "ready"
    ]
    _stamp_schema(db)
    operation = db.query(QuotaReservation).filter_by(project_id=projects[0].id).one()
    operation.dispatch_metadata = {
        **operation.dispatch_metadata,
        "storage_version": "virtual-v1",
    }
    marker = db.get(QuotaReadiness, "baseline")
    marker.baseline_version = "operator-baseline-v1"
    db.commit()
    before = snapshot(db)
    report = cli().preflight(db)
    assert report["phase"] == "generation_reconciliation_required"
    assert report["retained_operation_blockers"] == [
        {
            "operation_id": operation.operation_id,
            "project_id": projects[0].id,
            "code": "physical_storage_reconciliation_required",
        }
    ]
    assert not report["baseline_gate"]["ready"]
    assert snapshot(db) == before


def test_cli_runs_from_installed_image_layout(tmp_path):
    import subprocess
    import sys

    root = tmp_path / "installed-backend"
    (root / "scripts").mkdir(parents=True)
    (root / "app").mkdir()
    (root / "alembic").mkdir()
    installed = root / "scripts/backfill_quotas.py"
    installed.write_text(Path(cli().__file__).read_text())
    probe = "import runpy; m = runpy.run_path('scripts/backfill_quotas.py'); print(m['backend_root']())"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == str(root)


def test_pre_enable_readiness_rejects_disabled_source_inventory_drift(sample):
    db, projects, *_ = sample
    assert baseline.run(db, evidence=manifest(db, projects), now=NOW, apply=True)[
        "ready"
    ]
    assert baseline.check_ready(db)["ready"]
    projects[2].topology = {"nodes": []}
    db.commit()
    assert not baseline.check_ready(db)["ready"]


def test_foreign_currency_document_is_not_imported_as_usd(sample):
    db, projects, *_ = sample
    invoice = db.query(ProjectInvoice).filter_by(project_name="Deleted").one()
    invoice.currency = "EUR"
    db.commit()
    report = baseline.run(db, evidence=manifest(db, projects), now=NOW, apply=True)
    assert not report["ready"]
    assert any("currency" in str(item).lower() for item in report["blockers"])
    assert (
        db.query(QuotaCharge)
        .filter_by(source="legacy:invoice", project_id=invoice.project_id)
        .count()
        == 0
    )


def test_missing_imported_allocation_cannot_hide_behind_matching_observation_id(sample):
    db, projects, *_ = sample
    evidence = manifest(db, projects)
    assert baseline.run(db, evidence=evidence, now=NOW, apply=True)["ready"]
    db.query(QuotaAllocation).filter_by(project_id=projects[0].id).delete()
    db.commit()
    report = baseline.run(db, evidence=evidence, now=NOW, apply=True)
    assert not report["ready"]
    assert any("allocation" in str(item).lower() for item in report["blockers"])


@pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL URL not configured",
)
def test_postgresql_baseline_boundary_blocks_unguarded_database_growth(postgres):
    sessions, (projects, *_), _ = postgres
    with sessions() as importer:
        baseline._boundary(importer)
        with sessions() as legacy_writer:
            legacy_writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
            with pytest.raises(OperationalError):
                legacy_writer.execute(
                    text("UPDATE projects SET state = 'deploying' WHERE id = :id"),
                    {"id": projects[0]},
                )
            legacy_writer.rollback()
            legacy_writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
            with pytest.raises(OperationalError):
                legacy_writer.execute(
                    text("SELECT id FROM projects WHERE id = :id FOR UPDATE"),
                    {"id": projects[0]},
                )
            legacy_writer.rollback()
        importer.rollback()


@pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL URL not configured",
)
def test_postgresql_active_unguarded_writer_rejects_import_before_any_changes(postgres):
    sessions, (projects, *_), _ = postgres
    with sessions() as writer, sessions() as importer:
        writer.execute(
            text("UPDATE projects SET state = 'deploying' WHERE id = :id"),
            {"id": projects[0]},
        )
        with pytest.raises(OperationalError):
            baseline.run(importer, now=NOW, apply=True)
        writer.rollback()
        assert importer.query(QuotaCharge).count() == 0
        assert importer.get(QuotaReadiness, "baseline").details == {
            "accounting_ready": True
        }
