"""Pure metering math: rates, spend, desired resources, interval diffs."""

from __future__ import annotations

from datetime import UTC, datetime

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
) -> float:
    key = KIND_TO_RATE.get(kind)
    if not key:
        return 0.0
    if host_rates and host_rates.get(key) is not None:
        return float(host_rates[key])
    typed = type_rates.get(provider_type) or {}
    if typed.get(key) is not None:
        return float(typed[key])
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


def _vm_running(vm: dict) -> bool:
    if vm.get("running") is True:
        return True
    state = str(vm.get("state") or vm.get("power") or "").lower()
    return state in ("running", "active", "on")


def desired_resources(snapshot: dict) -> set[Desired]:
    provider_type = snapshot.get("provider_type") or "unknown"
    default_host = snapshot.get("host_id")
    out: set[Desired] = set()
    for vm in snapshot.get("vms") or []:
        hid = vm.get("host_id") or default_host
        vid = str(vm["id"])
        if _vm_running(vm):
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
