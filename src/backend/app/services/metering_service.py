"""Persist metering intervals, budgets, and invoices."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.config import config
from app.models.elastic_ip import ElasticIp
from app.models.host import Host
from app.models.metering import (
    MeteringInterval,
    MeteringRateDefault,
    MonthlyStatement,
    ProjectInvoice,
)
from app.models.project import Project
from app.models.provider import Provider
from app.services.instance_rates import (
    host_capacity,
    kubevirt_unit_rates,
    rates_for_host,
)
from app.services.metering_math import (
    KIND_TO_RATE,
    RATE_KEYS,
    covered_seconds,
    desired_resources,
    diff_intervals,
    month_bounds,
    months_spanned,
    period_usage,
    previous_month,
    resolve_rate,
    spend_total,
)

logger = logging.getLogger(__name__)


def metering_enabled() -> bool:
    metering = getattr(config, "metering", None)
    if metering is None:
        return True
    return bool(getattr(metering, "enabled", True))


def warn_ratio() -> float:
    metering = getattr(config, "metering", None)
    if metering is None:
        return 0.8
    return float(getattr(metering, "warn_ratio", 0.8))


def poll_seconds() -> int:
    metering = getattr(config, "metering", None)
    if metering is None:
        return 120
    return int(getattr(metering, "poll_seconds", 120))


def nested_factor() -> float:
    """CPU/RAM discount for nested (non-kubevirt, non-dedicated) hosts."""
    metering = getattr(config, "metering", None)
    if metering is None:
        return 0.5
    return float(getattr(metering, "nested_factor", 0.5))


def _now() -> datetime:
    return datetime.now(UTC)


def _as_float(value) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def load_type_rates(db: Session) -> dict[str, dict]:
    rows = db.query(MeteringRateDefault).all()
    return {row.provider_type: dict(row.rates or {}) for row in rows}


def empty_rate_map() -> dict[str, float]:
    return {key: 0.0 for key in RATE_KEYS}


def _typed_rate_row(provider_type: str, type_rates: dict[str, dict]) -> dict:
    typed = type_rates.get(provider_type) or {}
    if typed:
        return typed
    if provider_type == "ec2":
        return type_rates.get("aws") or {}
    if provider_type == "aws":
        return type_rates.get("ec2") or {}
    return {}


def _apply_rate_map(out: dict[str, float], rates: dict | None) -> None:
    if not rates:
        return
    for key, value in rates.items():
        if key in RATE_KEYS and value is not None:
            out[key] = float(value)


def merge_rate_maps(stored: dict[str, dict]) -> dict[str, dict]:
    types = {
        "ec2",
        "aws",
        "gcp",
        "azure",
        "kubevirt",
        "ocpvirt",
        "libvirt",
        "unknown",
    }
    types.update(stored.keys())
    fallback = kubevirt_unit_rates()
    out = {}
    for ptype in sorted(types):
        merged = dict(fallback)
        merged.update(stored.get(ptype) or {})
        out[ptype] = merged
    return out


def compose_host_rates(
    host: Host | None,
    provider_type: str,
    type_rates: dict[str, dict],
) -> dict[str, float]:
    """host override > nested factor > instance catalog > type default > kubevirt."""
    out = dict(kubevirt_unit_rates())
    _apply_rate_map(out, _typed_rate_row(provider_type, type_rates))
    instance_type = host.instance_type if host else None
    host_vcpus = float(host.total_vcpus) if host and host.total_vcpus else None
    host_ram = None
    if host and host.total_ram_mb:
        host_ram = float(host.total_ram_mb) / 1024.0
    derived = rates_for_host(provider_type, instance_type, host_vcpus, host_ram)
    if derived:
        out.update(derived)
    billing_mode = (
        str(getattr(host, "billing_mode", None) or "shared") if host else "shared"
    )
    if billing_mode != "dedicated" and provider_type != "kubevirt":
        factor = nested_factor()
        out["vcpu_hour"] = float(out.get("vcpu_hour") or 0.0) * factor
        out["ram_gib_hour"] = float(out.get("ram_gib_hour") or 0.0) * factor
    _apply_rate_map(out, host.metering_rates if host else None)
    return out


def _start_auto_map(topology: dict | None) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for entry in (topology or {}).get("startOrder") or []:
        vid = entry.get("vmId")
        if vid and "autoStart" in entry:
            out[str(vid)] = entry.get("autoStart") is not False
    return out


def _vm_nodes(topology: dict | None, live_states: dict | None = None) -> list[dict]:
    live_states = live_states or {}
    auto_map = _start_auto_map(topology)
    vms = []
    for node in (topology or {}).get("nodes") or []:
        if node.get("type") != "vmNode":
            continue
        data = node.get("data") or {}
        ram = data.get("ram_gib")
        if ram is None:
            ram = data.get("ram") or 0
        nid = node.get("id")
        vms.append(
            {
                "id": nid,
                "running": data.get("running"),
                "state": data.get("state") or data.get("power"),
                "live_state": live_states.get(nid),
                "vcpus": data.get("vcpus") or 0,
                "ram_gib": ram,
                "host_id": data.get("hostId") or data.get("host_id"),
                "auto_start": auto_map.get(str(nid)),
                "power_on_at_deploy": data.get("powerOnAtDeploy"),
                "defer_ocp_install": data.get("deferOcpInstall"),
            }
        )
    return vms


def _topology_disks(topology: dict | None) -> list[dict]:
    items = []
    for node in (topology or {}).get("nodes") or []:
        if node.get("type") != "storageNode":
            continue
        data = node.get("data") or {}
        fmt = str(data.get("format") or "").lower()
        if fmt == "iso" or data.get("bootableIso"):
            continue
        size = data.get("size_gib") or data.get("sizeGb") or data.get("size") or 0
        items.append(
            {
                "id": node.get("id"),
                "size_gib": size,
                "host_id": data.get("hostId") or data.get("host_id"),
            }
        )
    return items


def _ceph_nodes(topology: dict | None) -> list[dict]:
    items = []
    for node in (topology or {}).get("nodes") or []:
        if node.get("type") != "storageNode":
            continue
        data = node.get("data") or {}
        if not (
            data.get("ceph")
            or data.get("kind") == "ceph"
            or "ceph" in str(data.get("backend") or "").lower()
        ):
            continue
        size = data.get("size_gib") or data.get("sizeGb") or data.get("size") or 0
        items.append(
            {
                "id": node.get("id"),
                "size_gib": size,
                "host_id": data.get("hostId") or data.get("host_id"),
            }
        )
    return items


def build_snapshot(db: Session, project: Project) -> dict | None:
    if not project.host_id or project.state == "draft":
        return None
    host = db.get(Host, project.host_id)
    provider_type = "unknown"
    if host and host.provider_id:
        provider = db.get(Provider, host.provider_id)
        if provider:
            provider_type = provider.type or "unknown"
    topology = project.deployed_topology or project.topology or {}
    from app.services.ws_pubsub import get_cached_vm_states

    cached = get_cached_vm_states(project.id) or {}
    live_states = cached.get("states") or {}
    disks = [
        {
            "id": disk.id,
            "size_gib": disk.size_gb,
            "host_id": project.host_id,
        }
        for disk in (project.disks or [])
    ]
    seen = {item["id"] for item in disks}
    for item in _topology_disks(topology):
        if item["id"] not in seen:
            disks.append(item)
            seen.add(item["id"])
    eips = [
        {"id": eip.id, "host_id": eip.host_id or project.host_id}
        for eip in db.query(ElasticIp).filter_by(project_id=project.id).all()
        if eip.state in ("associated", "allocated")
    ]
    host_vcpus = float(host.total_vcpus) if host and host.total_vcpus else None
    host_ram = None
    if host and host.total_ram_mb:
        host_ram = float(host.total_ram_mb) / 1024.0
    capacity = host_capacity(
        provider_type,
        host.instance_type if host else None,
        host_vcpus,
        host_ram,
    )
    billing_mode = (
        str(getattr(host, "billing_mode", None) or "shared") if host else "shared"
    )
    return {
        "provider_type": provider_type,
        "host_id": project.host_id,
        "billing_mode": billing_mode,
        "host_vcpus": capacity[0],
        "host_ram_gib": capacity[1],
        "host_rates": compose_host_rates(host, provider_type, load_type_rates(db)),
        "project_state": project.state,
        "vms": _vm_nodes(topology, live_states),
        "disks": disks,
        "eips": eips,
        "ceph": _ceph_nodes(topology),
    }


def _open_rows(db: Session, project_id: str) -> list[MeteringInterval]:
    return (
        db.query(MeteringInterval)
        .filter(
            MeteringInterval.project_id == project_id,
            MeteringInterval.ended_at.is_(None),
        )
        .all()
    )


def _close_interval(row: MeteringInterval, now: datetime) -> None:
    row.ended_at = now


def _open_interval(
    db: Session,
    project: Project,
    kind: str,
    resource_id: str,
    qty: float,
    host_id: str | None,
    provider_type: str,
    unit_rate: float,
    now: datetime,
) -> None:
    db.add(
        MeteringInterval(
            project_id=project.id,
            kind=kind,
            resource_id=resource_id,
            qty=qty,
            unit_rate=unit_rate,
            host_id=host_id,
            provider_type=provider_type,
            started_at=now,
            ended_at=None,
        )
    )


def reconcile_project(
    db: Session,
    project: Project,
    now: datetime | None = None,
    snapshot: dict | None = None,
) -> float:
    if not metering_enabled():
        return 0.0
    now = now or _now()
    snap = snapshot if snapshot is not None else build_snapshot(db, project)
    if snap is None:
        return live_spend_total(db, project, now)
    type_rates = load_type_rates(db)
    host_rates = snap.get("host_rates")
    fallback = kubevirt_unit_rates()
    desired = desired_resources(snap)
    open_list = _open_rows(db, project.id)
    open_tuples = [(row.kind, row.resource_id, _as_float(row.qty)) for row in open_list]
    to_close, to_open = diff_intervals(open_tuples, desired)
    close_keys = {(kind, rid) for kind, rid, _qty in to_close}
    open_keys = {(kind, rid) for kind, rid, _qty, _h, _p in to_open}
    # Re-open when catalog/nested-factor rate drifted (rates are stamped at open).
    desired_map = {(k, r): (q, h, p) for k, r, q, h, p in desired}
    for row in open_list:
        key = (row.kind, row.resource_id)
        if key in close_keys or key not in desired_map:
            continue
        qty, hid, ptype = desired_map[key]
        rate = resolve_rate(row.kind, ptype, host_rates, type_rates, fallback)
        # Numeric(18,6) stamps; tolerate sub-micro rounding so disk doesn't churn.
        if abs(_as_float(row.unit_rate) - rate) <= 1e-6:
            continue
        close_keys.add(key)
        if key not in open_keys:
            to_open.append((row.kind, row.resource_id, qty, hid, ptype))
            open_keys.add(key)
    for row in open_list:
        if (row.kind, row.resource_id) in close_keys:
            _close_interval(row, now)
    for kind, rid, qty, hid, ptype in to_open:
        rate = resolve_rate(kind, ptype, host_rates, type_rates, fallback)
        _open_interval(db, project, kind, rid, qty, hid, ptype, rate, now)
    db.flush()
    spend = live_spend_total(db, project, now)
    apply_budget(db, project, spend)
    return spend


def live_spend_total(
    db: Session, project: Project, now: datetime | None = None
) -> float:
    now = now or _now()
    rows = db.query(MeteringInterval).filter_by(project_id=project.id).all()
    payload = [
        {
            "qty": _as_float(row.qty),
            "unit_rate": _as_float(row.unit_rate),
            "started_at": row.started_at,
            "ended_at": row.ended_at,
        }
        for row in rows
    ]
    return spend_total(payload, now)


def live_spend(db: Session, project: Project, now: datetime | None = None) -> dict:
    now = now or _now()
    rows = db.query(MeteringInterval).filter_by(project_id=project.id).all()
    by_kind_map: dict[str, float] = {kind: 0.0 for kind in KIND_TO_RATE}
    by_kind_lines: dict[str, dict] = {}
    by_kind_spans: dict[str, list[dict]] = {}
    payload = []
    for row in rows:
        item = {
            "qty": _as_float(row.qty),
            "unit_rate": _as_float(row.unit_rate),
            "started_at": row.started_at,
            "ended_at": row.ended_at,
        }
        payload.append(item)
        cost = spend_total([item], now)
        by_kind_map[row.kind] = by_kind_map.get(row.kind, 0.0) + cost
        hours = spend_total(
            [
                {
                    "qty": 1.0,
                    "unit_rate": 1.0,
                    "started_at": row.started_at,
                    "ended_at": row.ended_at or now,
                }
            ],
            now,
        )
        bucket = by_kind_lines.setdefault(
            row.kind,
            {
                "kind": row.kind,
                "hours": 0.0,
                "qty_hours": 0.0,
                "subtotal": 0.0,
                "unit_rate": 0.0,
                "hosts": [],
            },
        )
        # qty_hours stays additive for rate math; Hours is wall-clock union.
        bucket["qty_hours"] += _as_float(row.qty) * hours
        bucket["subtotal"] += cost
        by_kind_spans.setdefault(row.kind, []).append(
            {"started_at": row.started_at, "ended_at": row.ended_at or now}
        )
        if row.host_id and row.host_id not in bucket["hosts"]:
            bucket["hosts"].append(row.host_id)
    total = spend_total(payload, now)
    since, until = _running_window(project, rows, now)
    lines = sorted(by_kind_lines.values(), key=lambda row: row["kind"])
    for item in lines:
        spans = by_kind_spans.get(item["kind"], [])
        item["hours"] = covered_seconds(spans, now) / 3600.0
        qh = float(item["qty_hours"])
        item["unit_rate"] = float(item["subtotal"]) / qh if qh > 0 else 0.0
    return {
        "total_usd": total,
        "by_kind": by_kind_map,
        "line_items": {"by_kind": lines},
        "budget_usd": (
            _as_float(project.budget_usd) if project.budget_usd is not None else None
        ),
        "budget_warned": bool(project.budget_warned),
        "budget_stopped": bool(project.budget_stopped),
        "currency": "USD",
        "running_seconds": covered_seconds(payload, now),
        "running_since": since.isoformat() if since else None,
        "running_until": until.isoformat() if until else None,
    }


def _running_window(
    project: Project, rows: list, now: datetime
) -> tuple[datetime | None, datetime | None]:
    if rows:
        since = min(row.started_at for row in rows)
    else:
        since = project.deploy_started_at
    # Open intervals (including dedicated host while project is stopped) keep the clock live.
    if any(row.ended_at is None for row in rows):
        return since, None
    if rows:
        return since, max(row.ended_at for row in rows)
    if project.state in ("stopped", "stopping"):
        return since, now
    return since, None


def apply_budget(db: Session, project: Project, spend: float) -> None:
    budget = project.budget_usd
    if budget is None:
        return
    cap = _as_float(budget)
    if cap <= 0:
        return
    if spend >= warn_ratio() * cap and not project.budget_warned:
        project.budget_warned = True
        _notify_budget(project.id, "budget_warning", spend, cap)
    if spend >= cap and project.state == "active" and not project.budget_stopped:
        project.budget_stopped = True
        from app.services.project_timer import _spawn_stop

        _spawn_stop(project.id)
        _notify_budget(project.id, "budget_stop", spend, cap)


def _notify_budget(project_id: str, kind: str, spend: float, budget: float) -> None:
    try:
        from app.services.ws_pubsub import notify_project

        notify_project(
            project_id,
            {
                "type": kind,
                "spend_usd": spend,
                "budget_usd": budget,
            },
        )
    except Exception:
        logger.warning("Metering notify failed for %s", project_id[:8])


def freeze_invoice(
    db: Session, project: Project, now: datetime | None = None
) -> ProjectInvoice:
    now = now or _now()
    reconcile_project(db, project, now=now)
    for row in _open_rows(db, project.id):
        _close_interval(row, now)
    db.flush()
    rows = db.query(MeteringInterval).filter_by(project_id=project.id).all()
    by_kind: dict[str, dict] = {}
    by_kind_spans: dict[str, list[dict]] = {}
    starts: list[datetime] = []
    for row in rows:
        starts.append(row.started_at)
        bucket = by_kind.setdefault(
            row.kind,
            {
                "kind": row.kind,
                "hours": 0.0,
                "qty_hours": 0.0,
                "subtotal": 0.0,
                "unit_rate": 0.0,
                "hosts": [],
            },
        )
        hours = spend_total(
            [
                {
                    "qty": 1.0,
                    "unit_rate": 1.0,
                    "started_at": row.started_at,
                    "ended_at": row.ended_at or now,
                }
            ],
            now,
        )
        cost = spend_total(
            [
                {
                    "qty": _as_float(row.qty),
                    "unit_rate": _as_float(row.unit_rate),
                    "started_at": row.started_at,
                    "ended_at": row.ended_at or now,
                }
            ],
            now,
        )
        bucket["qty_hours"] += _as_float(row.qty) * hours
        bucket["subtotal"] += cost
        by_kind_spans.setdefault(row.kind, []).append(
            {"started_at": row.started_at, "ended_at": row.ended_at or now}
        )
        if row.host_id and row.host_id not in bucket["hosts"]:
            bucket["hosts"].append(row.host_id)
    for item in by_kind.values():
        spans = by_kind_spans.get(item["kind"], [])
        item["hours"] = covered_seconds(spans, now) / 3600.0
        qh = float(item["qty_hours"])
        item["unit_rate"] = float(item["subtotal"]) / qh if qh > 0 else 0.0
    total = sum(item["subtotal"] for item in by_kind.values())
    invoice = ProjectInvoice(
        project_id=project.id,
        project_name=project.name,
        owner_id=project.owner_id,
        total_usd=total,
        line_items={"by_kind": list(by_kind.values())},
        period_start=min(starts) if starts else now,
        period_end=now,
        currency="USD",
        finalized_at=now,
    )
    db.add(invoice)
    db.flush()
    _contribute_intervals_to_months(db, project, rows, now)
    return invoice


def _interval_payload(rows: list, now: datetime) -> list[dict]:
    return [
        {
            "kind": row.kind,
            "qty": _as_float(row.qty),
            "unit_rate": _as_float(row.unit_rate),
            "started_at": row.started_at,
            "ended_at": row.ended_at or now,
        }
        for row in rows
    ]


def _get_or_create_statement(
    db: Session, owner_id: str | None, year: int, month: int
) -> MonthlyStatement:
    period_start, period_end = month_bounds(year, month)
    row = (
        db.query(MonthlyStatement)
        .filter_by(owner_id=owner_id, period_start=period_start)
        .first()
    )
    if row:
        return row
    row = MonthlyStatement(
        owner_id=owner_id,
        period_start=period_start,
        period_end=period_end,
        total_usd=0,
        line_items={"by_project": []},
        currency="USD",
        status="draft",
    )
    db.add(row)
    db.flush()
    return row


def _set_project_month_lines(
    statement: MonthlyStatement,
    project_id: str,
    project_name: str,
    subtotal: float,
    by_kind: list[dict],
) -> None:
    items = dict(statement.line_items or {})
    projects = [
        p for p in (items.get("by_project") or []) if p.get("project_id") != project_id
    ]
    if subtotal > 0 or by_kind:
        projects.append(
            {
                "project_id": project_id,
                "project_name": project_name,
                "subtotal": subtotal,
                "by_kind": by_kind,
            }
        )
    projects.sort(
        key=lambda p: (p.get("project_name") or "", p.get("project_id") or "")
    )
    items["by_project"] = projects
    statement.line_items = items
    statement.total_usd = sum(float(p.get("subtotal") or 0) for p in projects)


def _contribute_intervals_to_months(
    db: Session, project: Project, rows: list, now: datetime
) -> None:
    if not rows or not project.owner_id:
        return
    payload = _interval_payload(rows, now)
    earliest = min(row.started_at for row in rows)
    for year, month in months_spanned(earliest, now):
        period_start, period_end = month_bounds(year, month)
        statement = _get_or_create_statement(db, project.owner_id, year, month)
        if statement.status == "final":
            continue
        subtotal, by_kind = period_usage(payload, period_start, period_end, now)
        _set_project_month_lines(statement, project.id, project.name, subtotal, by_kind)


def finalize_month(
    db: Session, year: int, month: int, now: datetime | None = None
) -> list[MonthlyStatement]:
    """Rebuild active-project lines and freeze statements for a calendar month."""
    if not metering_enabled():
        return []
    now = now or _now()
    period_start, period_end = month_bounds(year, month)
    if now < period_end:
        return []
    projects = (
        db.query(Project)
        .filter(Project.state != "draft", Project.host_id.isnot(None))
        .all()
    )
    owners: set[str | None] = set()
    for project in projects:
        if not project.owner_id:
            continue
        owners.add(project.owner_id)
        rows = db.query(MeteringInterval).filter_by(project_id=project.id).all()
        if not rows:
            continue
        payload = _interval_payload(rows, now)
        subtotal, by_kind = period_usage(payload, period_start, period_end, now)
        statement = _get_or_create_statement(db, project.owner_id, year, month)
        if statement.status == "final":
            continue
        _set_project_month_lines(statement, project.id, project.name, subtotal, by_kind)
    # Also finalize any draft statements that only have destroyed-project lines.
    drafts = (
        db.query(MonthlyStatement)
        .filter(
            MonthlyStatement.period_start == period_start,
            MonthlyStatement.status == "draft",
        )
        .all()
    )
    for statement in drafts:
        owners.add(statement.owner_id)
    finalized: list[MonthlyStatement] = []
    for owner_id in owners:
        statement = (
            db.query(MonthlyStatement)
            .filter_by(owner_id=owner_id, period_start=period_start)
            .first()
        )
        if not statement:
            continue
        if statement.status == "final":
            finalized.append(statement)
            continue
        items = statement.line_items or {}
        projects_lines = items.get("by_project") or []
        if not projects_lines:
            db.delete(statement)
            continue
        statement.total_usd = sum(float(p.get("subtotal") or 0) for p in projects_lines)
        statement.status = "final"
        statement.finalized_at = now
        finalized.append(statement)
    db.flush()
    return finalized


def maybe_finalize_previous_month(db: Session, now: datetime | None = None) -> None:
    now = now or _now()
    year, month = previous_month(now)
    try:
        finalize_month(db, year, month, now=now)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Monthly statement finalize failed for %s-%02d", year, month)


def reconcile_all_projects(db: Session) -> None:
    if not metering_enabled():
        return
    projects = (
        db.query(Project)
        .filter(Project.state != "draft", Project.host_id.isnot(None))
        .all()
    )
    for project in projects:
        try:
            reconcile_project(db, project)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Metering reconcile failed for %s", project.id[:8])


def touch_metering(db: Session, project: Project) -> None:
    if not metering_enabled() or not project.host_id:
        return
    try:
        reconcile_project(db, project)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Metering touch failed for %s", project.id[:8])


def safe_freeze_invoice(db: Session, project: Project) -> None:
    if not metering_enabled():
        return
    try:
        freeze_invoice(db, project)
        db.flush()
    except Exception:
        logger.exception("Metering invoice freeze failed for %s", project.id[:8])


_poll_started = False


def start_metering_poll() -> None:
    global _poll_started
    if _poll_started or not metering_enabled():
        return
    _poll_started = True
    import threading
    import time

    from app.core.database import SessionLocal

    interval = max(30, poll_seconds())

    def _loop():
        while True:
            time.sleep(interval)
            session = SessionLocal()
            try:
                reconcile_all_projects(session)
                maybe_finalize_previous_month(session)
            finally:
                session.close()

    threading.Thread(target=_loop, daemon=True, name="metering-poll").start()
