#!/usr/bin/env python3
"""Rehearse/import an operator-reviewed quota baseline; never enables quotas."""

import argparse
import json
import os
import sys
from pathlib import Path


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.getenv("QUOTA_BASELINE_DATABASE_URL"),
        help="Explicit target DSN, or QUOTA_BASELINE_DATABASE_URL; no application DB default",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        help="Reviewed read-only provider/maintenance manifest JSON",
    )
    parser.add_argument(
        "--baseline-kind",
        choices=("legacy", "fresh-capacity"),
        default="legacy",
        help="Explicit importer; fresh conversion requires fresh-capacity",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Commit imports/readiness; default rolls everything back",
    )
    mode.add_argument(
        "--inventory",
        action="store_true",
        help="Read-only desired identity map and source digest",
    )
    mode.add_argument(
        "--check-ready",
        action="store_true",
        help="Read-only readiness gate; never enables quotas",
    )
    mode.add_argument(
        "--preflight",
        action="store_true",
        help="Read-only schema/deployment gate; success is not permission to enable quotas",
    )
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error(
            "An explicit --database-url or QUOTA_BASELINE_DATABASE_URL is required"
        )
    return args


def database_engine(url):
    from sqlalchemy import create_engine

    return create_engine(url)


def backend_root():
    parent = Path(__file__).resolve().parents[1]
    for root in (parent / "src/backend", parent):
        if (root / "app").is_dir() and (root / "alembic").is_dir():
            return root
    raise RuntimeError("Cannot locate packaged backend")


def _backend():
    root = str(backend_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    # Import registrations only. No SessionLocal connection, provider calls,
    # background threads or application startup/shutdown hooks are invoked.
    import app.models  # noqa: F401
    from app.services import quota_baseline

    return quota_baseline


def main(argv=None):
    args = arguments(argv)
    from sqlalchemy.orm import Session

    engine = None
    try:
        engine = database_engine(args.database_url)
        with Session(engine) as db:
            # Inspect schema before importing ORM models or starting the app.
            report = preflight(db) if args.preflight else _selected_execute(db, args)
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        if args.preflight:
            return 0 if report["deployment_ready"] else 2
        return 0 if report.get("ready", args.inventory) else 2
    except Exception as error:  # noqa: BLE001 -- scrub DSNs from all CLI failures
        # DB/DSN/JSON exceptions may contain connection credentials or input
        # values. Diagnostics expose the exception class, never its raw text.
        print(
            json.dumps(
                {
                    "ready": False,
                    "error": type(error).__name__,
                    "message": "Baseline failed; transaction rolled back. Check private operator diagnostics.",
                }
            )
        )
        return 2
    finally:
        if engine is not None:
            engine.dispose()


def _execute(db, args, baseline):
    if args.inventory:
        return baseline.inventory(db)
    if args.check_ready:
        return baseline.check_ready(db)
    evidence = json.loads(args.evidence.read_text()) if args.evidence else None
    return baseline.run(db, evidence=evidence, apply=args.apply)


def _fresh_receipt_present(db):
    from sqlalchemy import inspect, text

    if "quota_readiness" not in inspect(db.connection()).get_table_names():
        return False
    return (
        db.execute(
            text("SELECT id FROM quota_readiness WHERE id = :identity"),
            {"identity": "unified-groups-v1"},
        ).first()
        is not None
    )


def _selected_backend(kind):
    baseline = _backend()
    if kind == "fresh-capacity":
        from app.services import quota_capacity_baseline

        return quota_capacity_baseline
    return baseline


def _selected_execute(db, args):
    fresh = _fresh_receipt_present(db)
    if fresh and args.baseline_kind != "fresh-capacity":
        return {
            "ready": False,
            "baseline_kind_required": "fresh-capacity",
            "baseline_version_required": "fresh-capacity-v1",
            "message": "Fresh conversion detected; select --baseline-kind fresh-capacity.",
        }
    if args.baseline_kind == "fresh-capacity":
        schema = _schema(db, fresh=True)
        if not schema["compatible"]:
            return {
                "ready": False,
                "schema": schema,
                "message": "Apply additive schema migrations before loading the fresh importer.",
            }
    return _execute(db, args, _selected_backend(args.baseline_kind))


def _schema(db, fresh=False):
    """Check the additive rollout boundary without loading application models."""
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect, text

    inspector = inspect(db.connection())
    tables = set(inspector.get_table_names())
    required = {
        "users": {"organization_id"},
        "managed_groups": {"is_organization", "cost_center_id"},
        "metering_intervals": {"quota_attribution", "quota_accrued_through"},
        "quota_policies": {"scope", "scope_id", "revision"},
        "quota_reservations": {"operation_id", "state", "dispatch_metadata"},
        "quota_allocations": {"operation_id", "state", "observed"},
        "quota_charges": {"source", "source_id", "amount", "provenance"},
        "quota_audits": {"action", "before", "after"},
        "quota_actions": {"operation_id", "state", "details"},
        "quota_readiness": {"id", "ready", "details", "baseline_version"},
    }
    if fresh:
        required.update(
            {
                "native_collection_domains": {
                    "id",
                    "enrollment_id",
                    "latest_report_id",
                    "conflicted",
                },
                "native_enrollments": {"id", "domain_id", "digest", "payload"},
                "native_project_roots": {
                    "key",
                    "domain_id",
                    "project_id",
                    "provider_id",
                    "enrollment_id",
                    "payload",
                },
                "native_retained_bindings": {"key", "root_key", "domain_id", "payload"},
                "native_storage_snapshots": {
                    "id",
                    "domain_id",
                    "enrollment_id",
                    "digest",
                    "accepted",
                    "payload",
                },
            }
        )
    missing = {}
    for name, columns in required.items():
        actual = (
            {column["name"] for column in inspector.get_columns(name)}
            if name in tables
            else set()
        )
        if columns - actual:
            missing[name] = sorted(columns - actual)
    expected = sorted(ScriptDirectory(str(backend_root() / "alembic")).get_heads())
    revisions = (
        sorted(db.execute(text("SELECT version_num FROM alembic_version")).scalars())
        if "alembic_version" in tables
        else []
    )
    return {
        "compatible": not missing and revisions == expected,
        "expected_revisions": expected,
        "actual_revisions": revisions,
        "missing_columns": missing,
    }


def _retained_operations(db, fresh=False, now=None):
    from datetime import UTC, datetime

    from app.models.quota import QuotaAllocation, QuotaReservation
    from app.schemas.quota import AccountingUnavailable

    result = []
    for allocation in db.query(QuotaAllocation).filter(
        QuotaAllocation.state != "released"
    ):
        operation = (
            db.query(QuotaReservation)
            .filter_by(operation_id=allocation.operation_id)
            .one_or_none()
        )
        try:
            _retained_storage(
                db, operation, allocation, fresh, now or datetime.now(UTC)
            )
        except (AccountingUnavailable, ValueError, TypeError, KeyError):
            result.append(
                {
                    "operation_id": allocation.operation_id,
                    "project_id": allocation.project_id,
                    "code": "physical_storage_reconciliation_required",
                }
            )
    return result


def _retained_storage(db, operation, allocation, fresh, now):
    from app.models.project import Project
    from app.schemas.quota import AccountingUnavailable
    from app.services import quota_native, quota_operations, quota_storage

    if operation is None:
        raise AccountingUnavailable("Missing original operation")
    adapter = quota_storage.storage_adapter(operation)
    rows = quota_operations.current_generation(
        quota_operations._rows(db, operation.project_id)
    )
    root = quota_storage.measurement_row(rows)
    if quota_storage.storage_adapter(root) != adapter:
        raise AccountingUnavailable("Generation adapters differ")
    metadata = root.dispatch_metadata or {}
    version = (
        quota_native.MEASUREMENT_VERSION
        if adapter == quota_native.ADAPTER
        else quota_storage.VERSION
    )
    if metadata.get("storage_version") != version:
        raise AccountingUnavailable("Storage evidence version is incompatible")
    if adapter == quota_native.ADAPTER:
        project = db.get(Project, operation.project_id)
        quota_storage._native_scope_evidence(db, root, now)
        if project is None:
            raise AccountingUnavailable("Retained native project is missing")
    elif fresh and metadata.get("kind") == "deploy":
        quota_storage.validate(
            metadata.get("storage_evidence", {}),
            operation.project_id,
            metadata["generation_id"],
            metadata["storage_host_id"],
            now,
        )


def preflight(db):
    """Read-only deployment gate, deliberately separate from baseline enablement."""
    from datetime import UTC, datetime

    report = {
        "mode": "preflight",
        "checked_at": datetime.now(UTC).isoformat(),
        "deployment_ready": False,
        "ready": False,
        "schema": _schema(db, fresh=_fresh_receipt_present(db)),
        "phase": "schema_required",
        "next_action": "Back up the target database; apply additive migrations before promoting new application images.",
    }
    if not report["schema"]["compatible"]:
        return report
    fresh = _fresh_receipt_present(db)
    kind = "fresh-capacity" if fresh else "legacy"
    baseline = _selected_backend(kind)
    enabled = _backend().accounting.quotas_enabled()
    report.update(
        {
            "enforcement_enabled": enabled,
            "deployment_ready": not enabled,
            "phase": "baseline_required",
            "baseline_version_required": baseline.VERSION,
            "baseline_kind_required": kind,
            "baseline_gate": baseline.check_ready(db),
            "inventory": baseline.inventory(db),
            "retained_operation_blockers": _retained_operations(db, fresh=fresh),
            "next_action": "Keep enforcement disabled; fence writers and collect independent evidence for baseline rehearsal.",
        }
    )
    if report["baseline_gate"]["ready"]:
        report["phase"] = "baseline_review_required"
        report[
            "next_action"
        ] = "Review fresh provider evidence and the check-ready gate before separately authorizing enablement."
    if report["retained_operation_blockers"]:
        report["phase"] = "generation_reconciliation_required"
        report[
            "next_action"
        ] = "Preserve original allocations and jobs; reconcile physical storage under maintenance before enablement."
    if enabled:
        report["phase"] = "enforcement_already_enabled"
        report[
            "next_action"
        ] = "Offline baseline import requires an approved maintenance boundary with enforcement disabled."
    return report


if __name__ == "__main__":
    raise SystemExit(main())
