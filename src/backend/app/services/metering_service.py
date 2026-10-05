"""Persist metering intervals, budgets, and invoices."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.config import config
from app.models.elastic_ip import ElasticIp
from app.models.host import Host
from app.models.metering import MeteringInterval, MeteringRateDefault, ProjectInvoice
from app.models.project import Project
from app.models.provider import Provider
from app.services.metering_math import (
    KIND_TO_RATE,
    RATE_KEYS,
    desired_resources,
    diff_intervals,
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


def merge_rate_maps(stored: dict[str, dict]) -> dict[str, dict]:
    types = {
        "aws",
        "gcp",
        "azure",
        "kubevirt",
        "ocpvirt",
        "libvirt",
        "unknown",
    }
    types.update(stored.keys())
    out = {}
    for ptype in sorted(types):
        merged = empty_rate_map()
        merged.update(stored.get(ptype) or {})
        out[ptype] = merged
    return out


def _vm_nodes(topology: dict | None) -> list[dict]:
    vms = []
    for node in (topology or {}).get("nodes") or []:
        if node.get("type") != "vmNode":
            continue
        data = node.get("data") or {}
        ram = data.get("ram_gib")
        if ram is None:
            ram = data.get("ram") or 0
        vms.append(
            {
                "id": node.get("id"),
                "running": data.get("running"),
                "state": data.get("state") or data.get("power"),
                "vcpus": data.get("vcpus") or 0,
                "ram_gib": ram,
                "host_id": data.get("hostId") or data.get("host_id"),
            }
        )
    return vms


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
    disks = [
        {
            "id": disk.id,
            "size_gib": disk.size_gb,
            "host_id": project.host_id,
        }
        for disk in (project.disks or [])
    ]
    eips = [
        {"id": eip.id, "host_id": eip.host_id or project.host_id}
        for eip in db.query(ElasticIp).filter_by(project_id=project.id).all()
        if eip.state in ("associated", "allocated")
    ]
    return {
        "provider_type": provider_type,
        "host_id": project.host_id,
        "host_rates": (host.metering_rates if host else None),
        "vms": _vm_nodes(topology),
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
    desired = desired_resources(snap)
    open_list = _open_rows(db, project.id)
    open_tuples = [(row.kind, row.resource_id, _as_float(row.qty)) for row in open_list]
    to_close, to_open = diff_intervals(open_tuples, desired)
    close_keys = {(kind, rid) for kind, rid, _qty in to_close}
    for row in open_list:
        if (row.kind, row.resource_id) in close_keys:
            _close_interval(row, now)
    for kind, rid, qty, hid, ptype in to_open:
        rate = resolve_rate(kind, ptype, host_rates, type_rates)
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
    by_kind: dict[str, float] = {kind: 0.0 for kind in KIND_TO_RATE}
    payload = []
    for row in rows:
        item = {
            "qty": _as_float(row.qty),
            "unit_rate": _as_float(row.unit_rate),
            "started_at": row.started_at,
            "ended_at": row.ended_at,
        }
        payload.append(item)
        by_kind[row.kind] = by_kind.get(row.kind, 0.0) + spend_total([item], now)
    total = spend_total(payload, now)
    return {
        "total_usd": total,
        "by_kind": by_kind,
        "budget_usd": (
            _as_float(project.budget_usd) if project.budget_usd is not None else None
        ),
        "budget_warned": bool(project.budget_warned),
        "budget_stopped": bool(project.budget_stopped),
        "currency": "USD",
    }


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
    starts: list[datetime] = []
    for row in rows:
        starts.append(row.started_at)
        bucket = by_kind.setdefault(
            row.kind,
            {"kind": row.kind, "qty_hours": 0.0, "subtotal": 0.0, "hosts": []},
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
        if row.host_id and row.host_id not in bucket["hosts"]:
            bucket["hosts"].append(row.host_id)
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
    return invoice


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
            finally:
                session.close()

    threading.Thread(target=_loop, daemon=True, name="metering-poll").start()
