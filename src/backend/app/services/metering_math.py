"""Pure metering math: rates, spend, desired resources, interval diffs."""

from __future__ import annotations

from calendar import monthrange
from datetime import UTC, datetime, timedelta

RATE_KEYS = (
    "vcpu_hour",
    "ram_gib_hour",
    "disk_gib_hour",
    "eip_hour",
    "ceph_gib_hour",
)

KIND_TO_RATE = {
    "vcpu": "vcpu_hour",
    "ram": "ram_gib_hour",
    "disk": "disk_gib_hour",
    "eip": "eip_hour",
    "ceph": "ceph_gib_hour",
}

Desired = tuple[str, str, float, str | None, str]
OpenRow = tuple[str, str, float]


def resolve_rate(
    kind: str,
    provider_type: str,
    host_rates: dict | None,
    type_rates: dict[str, dict],
    fallback: dict | None = None,
) -> float:
    key = KIND_TO_RATE.get(kind)
    if not key:
        return 0.0
    if host_rates and host_rates.get(key) is not None:
        return float(host_rates[key])
    typed = type_rates.get(provider_type) or {}
    if key in typed and typed[key] is not None:
        return float(typed[key])
    if fallback and fallback.get(key) is not None:
        return float(fallback[key])
    return 0.0


def hours_between(start: datetime, end: datetime) -> float:
    start_u = start if start.tzinfo else start.replace(tzinfo=UTC)
    end_u = end if end.tzinfo else end.replace(tzinfo=UTC)
    secs = (end_u - start_u).total_seconds()
    if secs <= 0:
        return 0.0
    return secs / 3600.0


def interval_cost(
    qty: float, unit_rate: float, start: datetime, end: datetime
) -> float:
    return float(qty) * float(unit_rate) * hours_between(start, end)


def spend_total(rows: list[dict], now: datetime) -> float:
    total = 0.0
    for row in rows:
        end = row.get("ended_at") or now
        total += interval_cost(row["qty"], row["unit_rate"], row["started_at"], end)
    return total


def covered_seconds(rows: list[dict], now: datetime) -> float:
    spans = []
    for row in rows:
        start = row.get("started_at")
        if not start:
            continue
        end = row.get("ended_at") or now
        spans.append((start, end))
    if not spans:
        return 0.0
    spans.sort(key=lambda pair: pair[0])
    merged = [spans[0]]
    for start, end in spans[1:]:
        last_s, last_e = merged[-1]
        if start <= last_e:
            merged[-1] = (last_s, max(last_e, end))
        else:
            merged.append((start, end))
    return sum(hours_between(start, end) * 3600.0 for start, end in merged)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month + 1, 1, tzinfo=UTC)
    return start, end


def previous_month(now: datetime) -> tuple[int, int]:
    now_u = _aware(now)
    if now_u.month == 1:
        return now_u.year - 1, 12
    return now_u.year, now_u.month - 1


def months_spanned(start: datetime, end: datetime) -> list[tuple[int, int]]:
    """Inclusive calendar months (UTC) from start through end."""
    cur = _aware(start).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last = _aware(end)
    out: list[tuple[int, int]] = []
    while cur <= last:
        out.append((cur.year, cur.month))
        days = monthrange(cur.year, cur.month)[1]
        cur = cur + timedelta(days=days)
        cur = cur.replace(day=1)
    return out


def period_usage(
    rows: list[dict], period_start: datetime, period_end: datetime, now: datetime
) -> tuple[float, list[dict]]:
    """Cost and by-kind lines for intervals overlapping [period_start, period_end)."""
    p0, p1 = _aware(period_start), _aware(period_end)
    now_u = _aware(now)
    by_kind: dict[str, dict] = {}
    total = 0.0
    for row in rows:
        start = row.get("started_at")
        if not start:
            continue
        end = row.get("ended_at") or now_u
        start_u, end_u = _aware(start), _aware(end)
        clip_s = max(start_u, p0)
        clip_e = min(end_u, p1)
        if clip_e <= clip_s:
            continue
        qty = float(row.get("qty") or 0)
        rate = float(row.get("unit_rate") or 0)
        hours = hours_between(clip_s, clip_e)
        cost = qty * rate * hours
        total += cost
        kind = str(row.get("kind") or "unknown")
        bucket = by_kind.setdefault(
            kind,
            {
                "kind": kind,
                "hours": 0.0,
                "qty_hours": 0.0,
                "subtotal": 0.0,
                "unit_rate": 0.0,
            },
        )
        bucket["hours"] += hours
        bucket["qty_hours"] += qty * hours
        bucket["subtotal"] += cost
    lines = sorted(by_kind.values(), key=lambda item: item["kind"])
    for item in lines:
        qh = float(item["qty_hours"])
        item["unit_rate"] = float(item["subtotal"]) / qh if qh > 0 else 0.0
    return total, lines


_RUNNING_STATES = frozenset({"running", "active", "on"})
_STOPPED_STATES = frozenset({"stopped", "shutoff", "off", "halted", "paused"})


def _state_running(value) -> bool | None:
    if value is True:
        return True
    if value is False:
        return False
    token = str(value or "").lower()
    if token in _RUNNING_STATES:
        return True
    if token in _STOPPED_STATES:
        return False
    return None


def _intended_off(vm: dict) -> bool:
    if vm.get("auto_start") is False:
        return True
    if vm.get("power_on_at_deploy") is False:
        return True
    return bool(vm.get("defer_ocp_install"))


def _vm_running(vm: dict, project_state: str = "") -> bool:
    for key in ("live_state", "state", "power"):
        known = _state_running(vm.get(key))
        if known is not None:
            return known
    known = _state_running(vm.get("running"))
    if known is not None:
        return known
    if _intended_off(vm):
        return False
    return project_state in ("active", "starting")


def desired_resources(snapshot: dict) -> set[Desired]:
    provider_type = snapshot.get("provider_type") or "unknown"
    default_host = snapshot.get("host_id")
    project_state = str(snapshot.get("project_state") or "")
    billing_mode = str(snapshot.get("billing_mode") or "shared")
    # Error freezes the meter for shared and dedicated alike.
    if project_state == "error":
        return set()
    if billing_mode == "dedicated":
        return _dedicated_host_resources(snapshot, provider_type, default_host)
    if project_state in ("stopped", "stopping"):
        return set()
    out: set[Desired] = set()
    for vm in snapshot.get("vms") or []:
        hid = vm.get("host_id") or default_host
        vid = str(vm["id"])
        if _vm_running(vm, project_state):
            out.add(("vcpu", vid, float(vm.get("vcpus") or 0), hid, provider_type))
            out.add(("ram", vid, float(vm.get("ram_gib") or 0), hid, provider_type))
    _add_persistent(
        out, snapshot.get("disks"), "disk", "size_gib", default_host, provider_type
    )
    _add_eips(out, snapshot.get("eips"), default_host, provider_type)
    _add_persistent(
        out, snapshot.get("ceph"), "ceph", "size_gib", default_host, provider_type
    )
    return out


def _dedicated_host_resources(
    snapshot: dict, provider_type: str, default_host: str | None
) -> set[Desired]:
    """Full host CPU+RAM while the project owns a dedicated host (power-independent)."""
    out: set[Desired] = set()
    hid = default_host
    rid = f"host:{hid}" if hid else "host"
    vcpus = float(snapshot.get("host_vcpus") or 0)
    ram = float(snapshot.get("host_ram_gib") or 0)
    if vcpus > 0:
        out.add(("vcpu", rid, vcpus, hid, provider_type))
    if ram > 0:
        out.add(("ram", rid, ram, hid, provider_type))
    return out


def _add_persistent(
    out: set[Desired],
    items: list | None,
    kind: str,
    qty_key: str,
    default_host: str | None,
    provider_type: str,
) -> None:
    for item in items or []:
        hid = item.get("host_id") or default_host
        out.add(
            (
                kind,
                str(item["id"]),
                float(item.get(qty_key) or 0),
                hid,
                provider_type,
            )
        )


def _add_eips(
    out: set[Desired],
    items: list | None,
    default_host: str | None,
    provider_type: str,
) -> None:
    for item in items or []:
        hid = item.get("host_id") or default_host
        out.add(("eip", str(item["id"]), 1.0, hid, provider_type))


def diff_intervals(
    open_rows: list[OpenRow], desired: set[Desired]
) -> tuple[list[OpenRow], list[Desired]]:
    open_map = {(kind, rid): qty for kind, rid, qty in open_rows}
    desired_map = {(k, r): (q, h, p) for k, r, q, h, p in desired}
    to_close: list[OpenRow] = []
    to_open: list[Desired] = []
    for key, qty in open_map.items():
        if key not in desired_map or abs(desired_map[key][0] - qty) > 1e-9:
            to_close.append((key[0], key[1], qty))
    for key, (qty, hid, ptype) in desired_map.items():
        if key not in open_map or abs(open_map[key] - qty) > 1e-9:
            to_open.append((key[0], key[1], qty, hid, ptype))
    return to_close, to_open
