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
from app.schemas.quota import AccountingUnavailable
from app.services.instance_rates import (
    host_capacity,
    kubevirt_unit_rates,
    load_catalog,
    rates_for_host,
)
from app.services.metering_math import (
    KIND_TO_RATE,
    RATE_KEYS,
    Desired,
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
from app.services.quota_accounting import quotas_enabled

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
    return dict.fromkeys(RATE_KEYS, 0.0)


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


def _host_provider_type(db: Session, host: Host | None) -> str:
    if host and host.provider_id:
        provider = db.get(Provider, host.provider_id)
        if provider:
            return provider.type or "unknown"
    return "unknown"


def _snapshot_disks(project: Project, topology: dict) -> list[dict]:
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
    return disks


def _configured_rate_keys(*maps: dict | None) -> set[str]:
    return {
        key
        for rates in maps
        if rates
        for key, value in rates.items()
        if value is not None
    }


def build_snapshot(db: Session, project: Project) -> dict | None:
    if not project.host_id or project.state == "draft":
        return None
    host = db.get(Host, project.host_id)
    provider_type = _host_provider_type(db, host)
    topology = project.deployed_topology or project.topology or {}
    from app.services.ws_pubsub import get_cached_vm_states

    cached = get_cached_vm_states(project.id) or {}
    live_states = cached.get("states") or {}
    disks = _snapshot_disks(project, topology)
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
        "rate_keys": list(
            _configured_rate_keys(
                load_catalog().get("kubevirt_unit_rates"),
                _typed_rate_row(provider_type, load_type_rates(db)),
                host.metering_rates if host else None,
            )
        ),
        "observed_at": cached.get("observed_at"),
        "project_state": project.state,
        "vms": _vm_nodes(topology, live_states),
        "disks": disks,
        "eips": eips,
        "ceph": _ceph_nodes(topology),
        "unsupported_nodes": [
            n.get("type")
            for n in topology.get("nodes", [])
            if n.get("type") not in {"vmNode", "storageNode", "networkNode"}
        ],
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
    interval = MeteringInterval(
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
    from app.models.user import User

    owner = db.get(User, project.owner_id) if project.owner_id else None
    if quotas_enabled() or (owner and owner.organization_id):
        from app.services.quota_accounting import attribution

        interval.quota_attribution = attribution(db, project)
    db.add(interval)


def _quota_snapshot(project: Project, snap: dict | None, now: datetime) -> dict:
    """Require explicit supported observations before mutating quota meters."""
    from app.services.metering_math import _state_running
    from app.services.quota_accounting import utc

    if snap is None or not project.host_id or snap.get("unsupported_nodes"):
        raise AccountingUnavailable("Missing or unsupported project metering map")
    if snap.get("project_state", project.state) in {"error", "stopping", "destroying"}:
        raise AccountingUnavailable("Project requires infrastructure reconciliation")
    if snap.get("provider_type") not in {
        "aws",
        "ec2",
        "gcp",
        "azure",
        "ocpvirt",
        "libvirt",
        "kubevirt",
    }:
        raise AccountingUnavailable("Unsupported metering provider")
    observed = snap.get("observed_at")
    if isinstance(observed, str):
        try:
            observed = datetime.fromisoformat(observed)
        except ValueError:
            observed = None
    if not isinstance(observed, datetime) or not 0 <= (
        utc(now) - utc(observed)
    ).total_seconds() <= max(30, poll_seconds()):
        raise AccountingUnavailable("Fresh metering observation unavailable")
    if str(snap.get("billing_mode") or "shared") == "dedicated":
        if not snap.get("host_vcpus") or not snap.get("host_ram_gib"):
            raise AccountingUnavailable("Dedicated capacity unavailable")
        return snap
    vms = []
    for vm in snap.get("vms") or []:
        running = _state_running(vm.get("live_state"))
        if running is None:
            raise AccountingUnavailable("Compute power observation unavailable")
        vms.append({**vm, "running": running, "live_state": running})
    # Ceph/general-disk overlap has no demonstrated billing interpretation yet.
    if snap.get("ceph"):
        raise AccountingUnavailable("Ceph pricing coverage needs verification")
    return {**snap, "vms": vms}


def _validate_quota_rates(
    snap: dict, desired: set[Desired], type_rates: dict, fallback: dict
) -> None:
    available = snap.get("rate_keys")
    if available is None:
        available = _configured_rate_keys(
            snap.get("host_rates"),
            _typed_rate_row(snap.get("provider_type", ""), type_rates),
            load_catalog().get("kubevirt_unit_rates"),
        )
    for kind, _rid, qty, _hid, ptype in desired:
        key = KIND_TO_RATE.get(kind)
        rate = resolve_rate(kind, ptype, snap.get("host_rates"), type_rates, fallback)
        if key not in available or not Decimal(str(rate)).is_finite() or rate < 0:
            raise AccountingUnavailable("Metering rate is unresolved or invalid")
        if not Decimal(str(qty)).is_finite() or qty < 0:
            raise AccountingUnavailable("Metering quantity is invalid")


def _reopen_changed_rates(
    open_list: list[MeteringInterval],
    desired: set[Desired],
    close_keys: set,
    to_open: list[Desired],
    host_rates: dict | None,
    type_rates: dict,
    fallback: dict,
) -> None:
    """Close and reopen current intervals whose stamped unit rate changed."""
    open_keys = {(kind, rid) for kind, rid, _qty, _h, _p in to_open}
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


def _lock_reconciliation(db: Session, project: Project, quota_mode: bool) -> Project:
    from app.services.quota_locks import lock_metering_project

    project, _, _ = lock_metering_project(db, project.id)
    if quota_mode:
        from app.services.quota_accounting import attribution

        attribution(db, project)
    return project


def reconcile_project(
    db: Session,
    project: Project,
    now: datetime | None = None,
    snapshot: dict | None = None,
    quota_refresh: bool = False,
) -> float:
    quota_mode = quotas_enabled() or quota_refresh
    if not metering_enabled() and not quota_mode:
        return 0.0
    if quota_mode and not metering_enabled():
        raise AccountingUnavailable("Metering is disabled")
    project = _lock_reconciliation(db, project, quota_mode)
    now = now or _now()
    snap = snapshot if snapshot is not None else build_snapshot(db, project)
    if quota_mode:
        snap = _quota_snapshot(project, snap, now)
    if snap is None:
        return live_spend_total(db, project, now)
    type_rates = load_type_rates(db)
    host_rates = snap.get("host_rates")
    fallback = kubevirt_unit_rates()
    desired = desired_resources(snap, quota_mode=quota_mode)
    if quota_mode:
        _validate_quota_rates(snap, desired, type_rates, fallback)
    open_list = _open_rows(db, project.id)
    open_tuples = [(row.kind, row.resource_id, _as_float(row.qty)) for row in open_list]
    to_close, to_open = diff_intervals(open_tuples, desired)
    close_keys = {(kind, rid) for kind, rid, _qty in to_close}
    _reopen_changed_rates(
        open_list, desired, close_keys, to_open, host_rates, type_rates, fallback
    )
    for row in open_list:
        if (row.kind, row.resource_id) in close_keys:
            _close_interval(row, now)
    for kind, rid, qty, hid, ptype in to_open:
        rate = resolve_rate(kind, ptype, host_rates, type_rates, fallback)
        _open_interval(db, project, kind, rid, qty, hid, ptype, rate, now)
    db.flush()
    if quota_mode:
        from app.services.quota_accounting import accrue, charge_rows

        accrue(db, project.id, now)
        return float(
            sum((r.amount for r in charge_rows(db, "project", project.id)), Decimal(0))
        )
    spend = live_spend_total(db, project, now)
    apply_budget(project, spend)
    return spend


def live_spend_total(
    db: Session, project: Project, now: datetime | None = None
) -> float:
    now = now or _now()
    if quotas_enabled():
        from app.services.quota_accounting import spend

        return float(spend(db, "project", project.id, now))
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
    if quotas_enabled():
        from app.services.quota_accounting import project_view

        return project_view(db, project, now)
    rows = db.query(MeteringInterval).filter_by(project_id=project.id).all()
    by_kind_map: dict[str, float] = dict.fromkeys(KIND_TO_RATE, 0.0)
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


def apply_budget(project: Project, spend: float) -> None:
    if quotas_enabled():
        return  # Durable shared evaluator owns every quota-mode spending decision.
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
    from app.services.quota_locks import lock_metering_project

    project, _, _ = lock_metering_project(db, project.id)
    if quotas_enabled():
        return _freeze_quota_invoice(db, project, now)
    reconcile_project(db, project, now=now)
    for row in _open_rows(db, project.id):
        _close_interval(row, now)
    db.flush()
    from app.services.quota_accounting import accrue

    accrue(db, project.id, now, attributed_only=True)
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


def _freeze_quota_invoice(
    db: Session, project: Project, now: datetime, *, refresh_live=True
) -> ProjectInvoice:
    from app.services.quota_accounting import accrue, charge_rows, refresh, summarize

    if refresh_live:
        refresh(db, "project", project.id, now)
    # A document snapshot does not stop live charging. Only confirmed cleanup
    # closes source intervals, immediately before taking the final projection.
    accrue(db, project.id, now, retained=not refresh_live)
    rows = charge_rows(db, "project", project.id, end=now)
    summary = summarize(rows, end=now)
    invoice = ProjectInvoice(
        project_id=project.id,
        project_name=project.name,
        owner_id=project.owner_id,
        total_usd=summary["total"],
        line_items=summary["line_items"],
        period_start=min((r.start_at for r in rows), default=now),
        period_end=now,
        currency="USD",
        finalized_at=now,
    )
    db.add(invoice)
    db.flush()
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


def _finalize_owner_statement(
    db: Session, owner_id: str | None, period_start: datetime, now: datetime
) -> MonthlyStatement | None:
    statement = (
        db.query(MonthlyStatement)
        .filter_by(owner_id=owner_id, period_start=period_start)
        .first()
    )
    if not statement or statement.status == "final":
        return statement
    items = statement.line_items or {}
    projects_lines = items.get("by_project") or []
    if not projects_lines:
        db.delete(statement)
        return None
    statement.total_usd = sum(float(p.get("subtotal") or 0) for p in projects_lines)
    statement.status = "final"
    statement.finalized_at = now
    return statement


def finalize_month(
    db: Session, year: int, month: int, now: datetime | None = None
) -> list[MonthlyStatement]:
    """Rebuild active-project lines and freeze statements for a calendar month."""
    if not metering_enabled():
        return []
    now = now or _now()
    if quotas_enabled():
        return _finalize_quota_month(db, year, month, now)
    period_start, period_end = month_bounds(year, month)
    if now < period_end:
        return []
    projects = (
        db.query(Project)
        .filter(Project.state != "draft", Project.host_id.isnot(None))
        .all()
    )
    from app.services.quota_locks import lock_metering_projects

    lock_metering_projects(db, [project.id for project in projects])
    owners: set[str | None] = set()
    for project in projects:
        if not project.owner_id:
            continue
        owners.add(project.owner_id)
        rows = (
            db.query(MeteringInterval)
            .filter_by(project_id=project.id)
            .populate_existing()
            .all()
        )
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
        statement = _finalize_owner_statement(db, owner_id, period_start, now)
        if statement:
            finalized.append(statement)
    db.flush()
    return finalized


def _finalize_quota_month(
    db: Session, year: int, month: int, now: datetime
) -> list[MonthlyStatement]:
    from app.models.quota import QuotaCharge
    from app.services.quota_accounting import (
        charge_rows,
        refresh,
        require_ready,
        summarize,
        utc,
    )

    require_ready(db)
    start, end = month_bounds(year, month)
    if utc(now) < end:
        return []
    from app.services.quota_locks import lock_metering_projects

    projects = lock_metering_projects(db, [pid for (pid,) in db.query(Project.id)])
    for project in projects:
        refresh(db, "project", project.id, now)
    owners = {
        owner
        for (owner,) in db.query(QuotaCharge.owner_id)
        .filter(QuotaCharge.end_at > start, QuotaCharge.start_at < end)
        .distinct()
        .all()
    }
    statements = []
    for owner in sorted(owners):
        statement = _get_or_create_statement(db, owner, year, month)
        rows = charge_rows(db, "user", owner, start, end)
        ids = sorted({row.project_id for row in rows})
        projects = []
        for pid in ids:
            project_rows = [row for row in rows if row.project_id == pid]
            summary = summarize(project_rows, start, end)
            projects.append(
                {
                    "project_id": pid,
                    "project_name": project_rows[0].project_name,
                    "subtotal": str(summary["total"]),
                    "by_kind": summary["line_items"]["by_kind"],
                }
            )
        statement.line_items = {"by_project": projects, "source": "quota_charges"}
        statement.total_usd = summarize(rows, start, end)["total"]
        statement.status = "final"
        statement.finalized_at = now
        statements.append(statement)
    db.flush()
    return statements


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
    from app.services.quota_locks import lock_metering_project

    lock_metering_project(db, project.id)
    if quotas_enabled():
        # Metadata deletion must not discard intervals after a failed freeze.
        freeze_invoice(db, project)
        db.flush()
        return
    if not metering_enabled():
        from app.services.quota_accounting import accrue

        accrue(db, project.id, _now(), attributed_only=True)
        return
    freeze_invoice(db, project)
    db.flush()


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
