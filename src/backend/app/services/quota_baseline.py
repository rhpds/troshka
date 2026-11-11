"""Operator-reviewed, offline quota baseline. Never contacts/mutates a provider.

The maintenance attestation is essential: PostgreSQL table locks prevent database
writes during import but cannot fence already dispatched provider work. Keep all
writers stopped until deliberate enablement. A manifest is evidence supplied by
an operator, not live verification performed by this module.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import PurePosixPath

from pydantic import ValidationError
from sqlalchemy import select, text

from app.core.database import Base
from app.models.elastic_ip import ElasticIp
from app.models.host import Host
from app.models.managed_group import ManagedGroup
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
from app.schemas.quota import AccountingUnavailable, Resources
from app.services import metering_service as metering
from app.services import quota_accounting as accounting
from app.services import quota_admission as admission
from app.services import quota_operations as operations
from app.services.metering_math import desired_resources
from app.services.quota_locks import lock_rows
from app.services.quota_resources import topology_resources

DEFAULT_ORGANIZATION_ID = "dc591874-9d97-4bf5-ab31-581bb95cc645"
VERSION = "operator-baseline-v2-allocated"
# Include all source, identity and resource tables; omit derived quota rows so
# the same manifest can be replayed idempotently. Values never leave the digest.
SOURCE_TABLES = tuple(
    sorted(name for name in Base.metadata.tables if not name.startswith("quota_"))
)


def _hash(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def inventory_digest(db):
    """Fingerprint every persisted source column, including membership and rates."""
    body = {}
    for name in SOURCE_TABLES:
        table = Base.metadata.tables[name]
        rows = [dict(row) for row in db.execute(select(table)).mappings()]
        if name == "metering_intervals":
            for row in rows:
                row.pop("quota_attribution", None)
                row.pop("quota_accrued_through", None)
        body[name] = sorted(
            rows, key=lambda row: json.dumps(row, sort_keys=True, default=str)
        )
    return _hash(body)


def inventory(db):
    """Read-only collection aid; maps are desired identities, never observations."""
    entries = []
    for project in db.query(Project).order_by(Project.id):
        topology = project.deployed_topology or project.topology or {}
        entry = {
            "project_id": project.id,
            "state": project.state,
            "host_id": project.host_id,
            "desired_digest": operations.digest(topology),
        }
        try:
            operations.supported(db, project, deployed=True)
            entry["allowed_resources"] = operations.resource_map(project, topology)
        except (AccountingUnavailable, ValueError, KeyError) as error:
            entry["blocker"] = str(error)
        entries.append(entry)
    return {"version": 1, "inventory_digest": inventory_digest(db), "projects": entries}


def _block(report, code, identity, message):
    item = {"code": code, "identity": identity, "message": str(message)}
    if item not in report["blockers"]:
        report["blockers"].append(item)


def _boundary(db):
    """Fail immediately if another database writer holds a conflicting lock."""
    if db.get_bind().dialect.name == "postgresql":
        names = sorted(Base.metadata.tables)
        quoted = ", ".join('"' + name + '"' for name in names)
        db.execute(text(f"LOCK TABLE {quoted} IN EXCLUSIVE MODE NOWAIT"))
    elif db.get_bind().dialect.name == "sqlite":
        # sqlite3 legacy transaction mode does not BEGIN for SELECT/SAVEPOINT.
        # Explicit BEGIN keeps released savepoints inside the dry-run rollback.
        connection = db.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")


def _operation_ids(db, evidence):
    identities = {value for (value,) in db.query(QuotaReservation.operation_id)}
    identities.update(f"baseline:{value}" for (value,) in db.query(Project.id))
    identities.update(item.get("operation_id") for item in evidence.get("inflight", []))
    return identities


def _lock_baseline(db, evidence):
    # Never wait for an external operation advisory while holding table locks.
    # Discover/prelock first, then NOWAIT table locks, then verify the complete
    # operation/project set so a concurrent insertion cannot escape this batch.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
    identities = _operation_ids(db, evidence)
    admission.lock_operations(db, identities)
    _boundary(db)
    if _operation_ids(db, evidence) != identities:
        raise AccountingUnavailable(
            "Operation inventory changed; rollback and repeat the maintenance review"
        )
    lock_rows(
        db,
        user_ids=[value for (value,) in db.query(User.id)],
        organization_ids=[value for (value,) in db.query(ManagedGroup.id)],
        project_ids=[value for (value,) in db.query(Project.id)],
    )


def _maintenance(report, evidence, digest, now):
    maintenance = evidence.get("maintenance") or {}
    if evidence.get("version") != 1 or evidence.get("inventory_digest") != digest:
        _block(
            report,
            "inventory_changed",
            "baseline",
            "Manifest version or source inventory digest does not match",
        )
    if not (
        maintenance.get("writers_stopped") is True
        and maintenance.get("history_complete") is True
        and maintenance.get("evidence")
    ):
        _block(
            report,
            "maintenance_required",
            "baseline",
            "Maintenance and complete-history review attestation required",
        )
    try:
        since = _timestamp(maintenance["since"])
        if since > now:
            raise ValueError("Maintenance boundary is in the future")
        return since
    except (KeyError, TypeError, ValueError) as error:
        _block(
            report,
            "maintenance_required",
            "baseline",
            f"Invalid maintenance boundary: {error}",
        )
        return None


def _timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Explicit UTC offset required")
    return accounting.utc(result)


def _default_identity(db, project_id, project_name, owner_id):
    default = db.get(ManagedGroup, DEFAULT_ORGANIZATION_ID)
    owner = db.get(User, owner_id) if owner_id else None
    if not default or not default.is_organization or not owner:
        raise AccountingUnavailable(
            "Historical identity requires migration default organization and known owner"
        )
    return {
        "project_id": project_id,
        "project_name": project_name,
        "owner_id": owner_id,
        "owner_name": owner.display_name or owner.email,
        "organization_id": default.id,
        "organization_name": default.name,
    }


def _validate_intervals(rows, now):
    previous = {}
    for row in sorted(rows, key=lambda r: accounting.utc(r.started_at)):
        start = accounting.utc(row.started_at)
        end = accounting.utc(row.ended_at) if row.ended_at else now
        if start > now or end < start:
            raise AccountingUnavailable("Invalid historical coverage timestamps")
        key = row.kind, row.resource_id
        if key in previous and previous[key] > start:
            raise AccountingUnavailable(
                "Overlapping detailed coverage for the same resource"
            )
        previous[key] = end


def _history_project(db, project, now):
    rows = db.query(MeteringInterval).filter_by(project_id=project.id).all()
    _validate_intervals(rows, now)
    migrated = set()
    for row in rows:
        _validate_cursor(db, row, now)
        if row.quota_attribution is None:
            row.quota_attribution = _default_identity(
                db, project.id, project.name, project.owner_id
            )
            migrated.add(row.id)
    accounting.accrue(db, project.id, now)
    for charge in db.query(QuotaCharge).filter(
        QuotaCharge.source_id.in_(migrated), QuotaCharge.source == "interval"
    ):
        charge.provenance = {
            **charge.provenance,
            "migration_attribution": True,
            "attribution_basis": "migration_default_not_historical_employer",
        }


def _validate_cursor(db, interval, now):
    start = accounting.utc(interval.started_at)
    end = accounting.utc(interval.ended_at) if interval.ended_at else now
    summaries = accounting.charge_rows(db, "project", interval.project_id, start, end)
    if any(row.source.startswith("legacy:") for row in summaries):
        raise AccountingUnavailable(
            "Detailed coverage overlaps already imported historical summary"
        )
    rows = (
        db.query(QuotaCharge)
        .filter_by(source="interval", source_id=interval.id)
        .order_by(QuotaCharge.start_at)
        .all()
    )
    cursor = accounting.utc(interval.quota_accrued_through or interval.started_at)
    if not start <= cursor <= end:
        raise AccountingUnavailable("Historical cursor is outside source coverage")
    position = start
    for row in rows:
        if (
            accounting.utc(row.start_at) != position
            or accounting.utc(row.end_at) > cursor
        ):
            raise AccountingUnavailable(
                "Historical cursor charge coverage has a gap or overlap"
            )
        if row.quantity != interval.qty or row.rate != interval.unit_rate:
            raise AccountingUnavailable(
                "Historical cursor rate/quantity differs from source stamp"
            )
        position = accounting.utc(row.end_at)
    if position != cursor:
        raise AccountingUnavailable("Historical cursor lacks complete charge coverage")


def _run_step(db, report, code, identity, function):
    """Preserve useful disjoint imports; one ambiguous source stays unapplied."""
    try:
        with db.begin_nested():
            function()
            db.flush()
    except ValidationError:
        _block(
            report,
            code,
            identity,
            "Invalid manifest resource vector; exact nonnegative integral quantities required",
        )
    except (
        AccountingUnavailable,
        ValueError,
        KeyError,
        TypeError,
        ArithmeticError,
    ) as error:
        _block(report, code, identity, error)


def _covered(db, project_id, start, end, amount):
    rows = accounting.charge_rows(db, "project", project_id, start, end)
    if not rows:
        return False
    cursor = start
    for row in rows:
        if accounting.utc(row.start_at) > cursor:
            raise AccountingUnavailable(
                "Historical coverage has a gap; summary cannot be added or prorated"
            )
        cursor = max(cursor, accounting.utc(row.end_at))
    if cursor < end:
        raise AccountingUnavailable(
            "Historical coverage is partial; summary needs manual reconciliation"
        )
    # Legacy documents were stored to six decimal places; compare at that same
    # precision instead of inventing sub-micro-dollar disagreement or residuals.
    total = accounting.summarize(rows, start, end)["total"]
    if total.quantize(Decimal("0.000001")) != amount.quantize(Decimal("0.000001")):
        raise AccountingUnavailable(
            "Historical coverage total conflicts with retained document"
        )
    return True


def _summary(db, *, source, source_id, project_id, name, owner_id, start, end, amount):
    if start is None or end is None:
        raise AccountingUnavailable(
            "Historical summary needs explicit coverage boundaries"
        )
    start, end = accounting.utc(start), accounting.utc(end)
    amount = Decimal(str(amount))
    if not amount.is_finite() or amount < 0 or end <= start:
        raise AccountingUnavailable("Invalid historical coverage amount or timestamps")
    if _covered(db, project_id, start, end, amount):
        return
    identity = _default_identity(db, project_id, name, owner_id)
    accounting.import_legacy_charge(
        db,
        project_id=project_id,
        source=source,
        source_id=source_id,
        start=start,
        end=end,
        amount=amount,
        identity=identity,
    )


def _statement(db, statement):
    if statement.currency != "USD":
        raise AccountingUnavailable(
            "Unsupported historical statement currency; no currency conversion is inferred"
        )
    lines = (statement.line_items or {}).get("by_project", [])
    if (
        sum((Decimal(str(row["subtotal"])) for row in lines), Decimal(0))
        != statement.total_usd
    ):
        raise AccountingUnavailable(
            "Statement project detail does not explain its total"
        )
    seen = set()
    for line in lines:
        project_id = line["project_id"]
        if project_id in seen:
            raise AccountingUnavailable("Duplicate statement project coverage")
        seen.add(project_id)
        _summary(
            db,
            source="statement",
            source_id=f"{statement.id}:{project_id}",
            project_id=project_id,
            name=line["project_name"],
            owner_id=statement.owner_id,
            start=statement.period_start,
            end=statement.period_end,
            amount=line["subtotal"],
        )


def _invoice(db, invoice):
    if invoice.currency != "USD":
        raise AccountingUnavailable(
            "Unsupported historical invoice currency; no currency conversion is inferred"
        )
    _summary(
        db,
        source="invoice",
        source_id=invoice.id,
        project_id=invoice.project_id,
        name=invoice.project_name,
        owner_id=invoice.owner_id,
        start=invoice.period_start,
        end=invoice.period_end,
        amount=invoice.total_usd,
    )


def _history(db, report, now):
    for project in db.query(Project).order_by(Project.id):
        _run_step(
            db,
            report,
            "historical_coverage",
            project.id,
            lambda p=project: _history_project(db, p, now),
        )
    for invoice in db.query(ProjectInvoice).order_by(
        ProjectInvoice.period_end, ProjectInvoice.id
    ):
        _run_step(
            db,
            report,
            "invoice_coverage",
            invoice.id,
            lambda i=invoice: _invoice(db, i),
        )
    for statement in db.query(MonthlyStatement).order_by(
        MonthlyStatement.period_start, MonthlyStatement.id
    ):
        _run_step(
            db,
            report,
            "statement_coverage",
            statement.id,
            lambda s=statement: _statement(db, s),
        )


def _evidence(project, entry, now, since):
    allowed = {
        "host_id",
        "observed_at",
        "source",
        "observation_id",
        "resources",
        "states",
        "jobs_checked_at",
        "jobs",
        "worker_fenced_at",
        "fence_evidence",
        "storage",
    }
    if set(entry) - allowed:
        raise AccountingUnavailable(
            "Unknown observation fields; rate/resource overrides are not accepted"
        )
    if (
        not entry.get("source")
        or not entry.get("observation_id")
        or entry.get("host_id") != project.host_id
    ):
        raise AccountingUnavailable("Provider observation identity/host/source missing")
    observed = _timestamp(entry["observed_at"])
    if not 0 <= (now - observed).total_seconds() <= max(30, metering.poll_seconds()):
        raise AccountingUnavailable("Provider observation is stale or in the future")
    fence = _timestamp(entry["worker_fenced_at"])
    jobs_at = _timestamp(entry["jobs_checked_at"])
    if (
        not since
        or not since <= fence <= jobs_at <= observed
        or not entry.get("fence_evidence")
    ):
        raise AccountingUnavailable(
            "Worker fence and original job inventory evidence required"
        )
    if entry.get("jobs") != []:
        raise AccountingUnavailable(
            "Pending, unknown or ambiguous provider jobs require reconciliation"
        )
    return observed, fence


def _resource_evidence(expected, observed):
    if observed.get("vms") != expected["vms"]:
        raise AccountingUnavailable(
            "Observed VM resource identities/capacities do not match"
        )
    disks = observed.get("disks", {})
    if any(
        path not in expected["disks"] or size != expected["disks"][path]
        for path, size in disks.items()
    ):
        raise AccountingUnavailable(
            "Observed disk resource path/virtual capacity does not match"
        )
    expected_names = {PurePosixPath(path).name for path in expected["disks"]}
    actual_names = {PurePosixPath(path).name for path in disks}
    if actual_names != expected_names or len(disks) != len(expected_names):
        raise AccountingUnavailable("Missing or duplicate disk resource identity")


def _live_snapshot(db, project, topology, entry, now):
    if not metering.metering_enabled():
        raise AccountingUnavailable(
            "Metering must be enabled before accounting readiness"
        )
    host = db.get(Host, project.host_id)
    provider = db.get(Provider, host.provider_id)
    rates = metering.compose_host_rates(
        host, provider.type, metering.load_type_rates(db)
    )
    keys = metering._configured_rate_keys(
        metering.load_catalog().get("kubevirt_unit_rates"),
        metering._typed_rate_row(provider.type, metering.load_type_rates(db)),
        host.metering_rates,
    )
    snap = {
        "host_id": host.id,
        "provider_type": provider.type,
        "billing_mode": "shared",
        "project_state": project.state,
        "host_rates": rates,
        "rate_keys": list(keys),
        "observed_at": entry["observed_at"],
        "vms": metering._vm_nodes(topology, entry["states"]),
        "disks": metering._topology_disks(topology),
        "eips": [],
        "ceph": [],
    }
    snap = metering._quota_snapshot(project, snap, now)
    desired = desired_resources(snap, quota_mode=True)
    # A stopped environment still needs known compute prices before a restart.
    future = {
        **snap,
        "vms": [{**vm, "running": True, "live_state": True} for vm in snap["vms"]],
    }
    metering._validate_quota_rates(
        snap,
        desired_resources(future, quota_mode=True),
        metering.load_type_rates(db),
        metering.kubevirt_unit_rates(),
    )
    actual = {
        (r.kind, r.resource_id, float(r.qty), r.host_id, r.provider_type)
        for r in db.query(MeteringInterval).filter_by(
            project_id=project.id, ended_at=None
        )
    }
    if actual != desired:
        raise AccountingUnavailable(
            "Open metering coverage differs from verified resource observation; reconcile manually"
        )


def _import_live(db, project, entry, now, since):
    operations.supported(db, project, deployed=True)
    if project.state not in {"active", "stopped"}:
        raise AccountingUnavailable(
            "Transitional/failed project requires original worker and provider job reconciliation"
        )
    if not project.host_id:
        raise AccountingUnavailable(
            "Existing allocation needs a verified single shared host"
        )
    observed, fence = _evidence(project, entry, now, since)
    topology = project.deployed_topology or project.topology or {}
    if operations.digest(topology) != operations.digest(project.topology or {}):
        raise AccountingUnavailable(
            "Desired/deployed resource digest differs; reconcile before enablement"
        )
    sizes = operations.virtual_sizes(db, topology)
    mapping = operations.resource_map(project, topology)
    _resource_evidence(mapping, entry["resources"])
    power = operations._whole_project_power(project, entry)
    if power is None:
        raise AccountingUnavailable(
            "Mixed/unknown whole-project observation requires reconciliation"
        )
    _live_snapshot(db, project, topology, entry, now)
    target = topology_resources(
        topology, state="stopped" if power == "off" else "active", virtual_sizes=sizes
    )
    operation_id = f"baseline:{project.id}"
    from app.services import quota_storage

    provisioned = target.storage_bytes
    storage = entry.get("storage", {})
    target.storage_bytes = quota_storage.validate(
        storage, project.id, operation_id, project.host_id, now
    )
    quota_storage.require_disks(mapping, storage)
    storage_time = _timestamp(storage["observed_at"])
    if not fence < storage_time <= observed:
        raise AccountingUnavailable(
            "Physical storage must be observed after the worker fence and before the baseline observation"
        )
    rows = db.query(QuotaReservation).filter_by(project_id=project.id).all()
    if any(row.operation_id != operation_id for row in rows):
        raise AccountingUnavailable(
            "Existing generation or in-flight jobs need provider reconciliation"
        )
    row = rows[0] if rows else None
    if row is None:
        identity = accounting.attribution(db, project)
        row = QuotaReservation(
            operation_id=operation_id,
            project_id=project.id,
            owner_id=identity["owner_id"],
            organization_id=identity["organization_id"],
            requested=target.model_dump(),
            observed=Resources().model_dump(),
            state="reconciling",
            dispatch_metadata={
                "kind": "deploy",
                "generation_id": operation_id,
                "desired_digest": operations.digest(topology),
                "virtual_sizes": sizes,
                "resources": mapping,
                "calls": {},
                "known_empty": False,
                "worker_finished": True,
                "worker_finished_at": fence.isoformat(),
                "storage_version": quota_storage.VERSION,
                "storage_host_id": project.host_id,
                "storage_evidence": storage,
                "storage_measured_at": storage["observed_at"],
                "provisioned_storage_bytes": provisioned,
                "baseline_evidence": entry,
                "provenance": VERSION,
            },
        )
        db.add(row)
        db.flush()
    elif row.dispatch_metadata.get("baseline_evidence") != entry:
        raise AccountingUnavailable(
            "Baseline evidence changed; use generation-aware reconciliation"
        )
    admission.confirm(db, operation_id, target, entry["observation_id"], observed)
    _confirmed_baseline(db, row, target, observed)


def _confirmed_baseline(db, row, target, observed):
    allocation = (
        db.query(QuotaAllocation).filter_by(operation_id=row.operation_id).one_or_none()
    )
    if (
        allocation is None
        or row.state != "confirmed"
        or row.observed != target.model_dump()
        or allocation.observed != row.observed
        or allocation.observation_id != row.observation_id
    ):
        raise AccountingUnavailable(
            "Imported allocation/reservation is missing or inconsistent; reconcile original generation"
        )
    if accounting.utc(allocation.observed_at) != observed:
        raise AccountingUnavailable(
            "Imported allocation observation differs; reconcile original generation"
        )


def _inflight(db, item):
    if set(item) - {"project_id", "operation_id", "requested", "provider_jobs"}:
        raise AccountingUnavailable(
            "Unexpected in-flight fields; include identities/status only, never provider output"
        )
    for job in item.get("provider_jobs", []):
        if (
            set(job) - {"id", "status", "host_id"}
            or not job.get("id")
            or not job.get("status")
        ):
            raise AccountingUnavailable(
                "Original provider jobs require identities/status only"
            )
    project = db.get(Project, item["project_id"])
    if project is None or not item.get("provider_jobs"):
        raise AccountingUnavailable(
            "In-flight import requires retained project and original provider job identities"
        )
    identity = accounting.attribution(db, project)
    vector = Resources.model_validate(item["requested"]).model_dump()
    row = (
        db.query(QuotaReservation).filter_by(operation_id=item["operation_id"]).first()
    )
    if row is None:
        db.add(
            QuotaReservation(
                operation_id=item["operation_id"],
                project_id=project.id,
                owner_id=identity["owner_id"],
                organization_id=identity["organization_id"],
                requested=vector,
                observed=Resources().model_dump(),
                state="reconciling",
                dispatch_metadata={
                    "kind": "legacy_unknown",
                    "generation_id": item["operation_id"],
                    "worker_finished": False,
                    "provider_jobs": item["provider_jobs"],
                    "provenance": VERSION,
                },
            )
        )
    elif row.project_id != project.id or row.requested != vector:
        raise AccountingUnavailable("Conflicting retained in-flight operation identity")


def _live(db, report, evidence, now, since):
    for eip in db.query(ElasticIp).filter(
        ElasticIp.state.in_(("associated", "allocated"))
    ):
        _block(
            report,
            "unsupported_eip",
            eip.id,
            "Retained EIP allocation needs verified ownership and a quota adapter",
        )
    for item in evidence.get("inflight", []):
        _run_step(
            db,
            report,
            "inflight_mapping",
            item.get("operation_id"),
            lambda i=item: _inflight(db, i),
        )
        _block(
            report,
            "inflight_unresolved",
            item.get("operation_id"),
            "Original provider jobs must be reconciled; no replacement dispatch",
        )
    for project in db.query(Project).order_by(Project.id):
        if project.state == "draft" and not project.host_id and not project.disks:
            if (
                db.query(MeteringInterval)
                .filter_by(project_id=project.id, ended_at=None)
                .first()
            ):
                _block(
                    report,
                    "open_draft_coverage",
                    project.id,
                    "Draft has unverified open metering",
                )
            continue
        entry = evidence.get("projects", {}).get(project.id, {})
        _run_step(
            db,
            report,
            "live_mapping",
            project.id,
            lambda p=project, e=entry: _import_live(db, p, e, now, since),
        )
    for row in db.query(QuotaReservation).filter(
        QuotaReservation.state.in_(admission.PENDING)
    ):
        _block(
            report,
            "inflight_unresolved",
            row.operation_id,
            "Reservation remains unresolved; preserve capacity",
        )
    retained = (
        db.query(QuotaAllocation).filter(QuotaAllocation.state != "released").all()
    )
    for row in retained:
        if db.get(Project, row.project_id) is None:
            _block(
                report,
                "orphan_allocation",
                row.operation_id,
                "Deleted project has retained infrastructure; verify cleanup",
            )


def _cutover(db, now):
    """Past unknown history uses default; future uses authoritative membership."""
    for project in db.query(Project).order_by(Project.id):
        rows = (
            db.query(MeteringInterval)
            .filter_by(project_id=project.id, ended_at=None)
            .all()
        )
        if not rows:
            continue
        identity = accounting.attribution(db, project)
        if any(
            row.quota_attribution["organization_id"] != identity["organization_id"]
            for row in rows
        ):
            accounting.split_attribution(
                db, project.id, now, identity["organization_id"]
            )


def _manifest_digest(db, evidence, actual):
    previous = db.get(QuotaReadiness, "baseline")
    details = previous.details if previous else {}
    if details.get("post_import_digest") == actual and details.get(
        "inventory_digest"
    ) == evidence.get("inventory_digest"):
        return evidence["inventory_digest"]
    return actual


def check_ready(db):
    """Read-only preflight. Disabled writers invalidate a reviewed source digest."""
    try:
        accounting.require_ready(db)
    except AccountingUnavailable as error:
        return {"ready": False, "message": str(error)}
    marker = db.get(QuotaReadiness, "baseline")
    if marker.baseline_version != VERSION:
        return {
            "ready": False,
            "message": "A reviewed versioned baseline import is required",
        }
    if not accounting.quotas_enabled() and marker.details.get(
        "post_import_digest"
    ) != inventory_digest(db):
        return {
            "ready": False,
            "message": "Source inventory changed while enforcement was disabled; repeat maintenance review/import",
        }
    return {"ready": True, "baseline": marker.details}


def run(db, *, evidence=None, now=None, apply=False):
    """Own this dedicated session's transaction. Default rehearses then rolls back.

    Apply may persist useful disjoint history with ready=false. No configuration
    is changed. Caller must never pass a session containing unrelated writes.
    """
    evidence, now = evidence or {}, accounting.utc(now or datetime.now(UTC))
    report = {
        "mode": "apply" if apply else "dry-run",
        "ready": False,
        "accounting_ready": False,
        "blockers": [],
        "version": VERSION,
        "checked_at": now.isoformat(),
    }
    try:
        _lock_baseline(db, evidence)
        actual_digest = inventory_digest(db)
        report["inventory_digest"] = _manifest_digest(db, evidence, actual_digest)
        since = _maintenance(report, evidence, report["inventory_digest"], now)
        if accounting.quotas_enabled():
            raise AccountingUnavailable(
                "Baseline import requires hierarchy enforcement disabled and writers stopped"
            )
        for user in db.query(User):
            if user.quota_overrides:
                _block(
                    report,
                    "legacy_quota_override",
                    user.id,
                    "Preserved legacy override needs explicit manual mapping",
                )
        _history(db, report, now)
        _live(db, report, evidence, now, since)
        report["ready"] = report["accounting_ready"] = not report["blockers"]
        if report["ready"]:
            _cutover(db, now)
        report["post_import_digest"] = inventory_digest(db)
        report["charge_count"] = db.query(QuotaCharge).count()
        report["allocation_count"] = db.query(QuotaAllocation).count()
        marker = db.get(QuotaReadiness, "baseline")
        if marker is None:
            marker = QuotaReadiness(id="baseline")
            db.add(marker)
        marker.ready, marker.details = report["ready"], dict(report)
        marker.checked_at, marker.baseline_version = now, VERSION
        db.flush()
        if apply:
            db.commit()
        else:
            db.rollback()
        return report
    except BaseException:
        db.rollback()
        raise
