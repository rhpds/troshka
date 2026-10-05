"""Retail instance hourly catalog and host-rate apportionment."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from app.services.metering_math import RATE_KEYS

_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "instance_hourly_rates.json"
)
_OCPVIRT_SIZE = re.compile(r"^(\d+)c-(\d+)g$")
_PROVIDER_ALIAS = {"aws": "ec2", "ec2": "ec2", "gcp": "gcp", "azure": "azure"}


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    with _CATALOG_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def kubevirt_unit_rates() -> dict[str, float]:
    raw = load_catalog().get("kubevirt_unit_rates") or {}
    out = {key: 0.0 for key in RATE_KEYS}
    for key in RATE_KEYS:
        if raw.get(key) is not None:
            out[key] = float(raw[key])
    return out


def lookup_instance(provider_type: str, instance_type: str | None) -> dict | None:
    if not instance_type:
        return None
    catalog = load_catalog()
    key = _PROVIDER_ALIAS.get(provider_type, provider_type)
    family = (catalog.get("providers") or {}).get(key) or {}
    row = family.get(instance_type)
    return dict(row) if row else None


def apportion_hourly(
    hourly_usd: float,
    vcpus: float,
    ram_gib: float,
    fallback: dict[str, float] | None = None,
) -> dict[str, float]:
    """Split a host hourly price into per-vCPU and per-GiB rates.

    CPU vs RAM weights come from kubevirt unit rates so a memory-heavy
    instance puts more of the hourly onto RAM than a compute-heavy one.
    Disk / EIP / Ceph stay on the kubevirt (or passed-in) fallback.
    """
    base = dict(fallback or kubevirt_unit_rates())
    cpu_w = float(vcpus) * float(base.get("vcpu_hour") or 0.0)
    ram_w = float(ram_gib) * float(base.get("ram_gib_hour") or 0.0)
    weight = cpu_w + ram_w
    if hourly_usd <= 0 or weight <= 0:
        return base
    if vcpus > 0:
        base["vcpu_hour"] = (hourly_usd * (cpu_w / weight)) / float(vcpus)
    if ram_gib > 0:
        base["ram_gib_hour"] = (hourly_usd * (ram_w / weight)) / float(ram_gib)
    return base


def ocpvirt_host_hourly(instance_type: str | None) -> tuple[float, float, float] | None:
    match = _OCPVIRT_SIZE.match(instance_type or "")
    if not match:
        return None
    vcpus = float(match.group(1))
    ram_gib = float(match.group(2))
    kv = kubevirt_unit_rates()
    hourly = vcpus * kv["vcpu_hour"] + ram_gib * kv["ram_gib_hour"]
    return hourly, vcpus, ram_gib


def host_capacity(
    provider_type: str,
    instance_type: str | None,
    host_vcpus: float | None = None,
    host_ram_gib: float | None = None,
) -> tuple[float, float]:
    """Return (vcpus, ram_gib) for a host from SKU or reported capacity."""
    if provider_type == "ocpvirt":
        parsed = ocpvirt_host_hourly(instance_type)
        if parsed:
            _hourly, vcpus, ram_gib = parsed
            return vcpus, ram_gib
    row = lookup_instance(provider_type, instance_type)
    if row:
        return float(row.get("vcpus") or 0), float(row.get("ram_gib") or 0)
    return float(host_vcpus or 0), float(host_ram_gib or 0)


def rates_for_host(
    provider_type: str,
    instance_type: str | None,
    host_vcpus: float | None = None,
    host_ram_gib: float | None = None,
) -> dict[str, float] | None:
    """Unit rates for a hypervisor host, or None to use kubevirt fallback only."""
    if provider_type in ("kubevirt",):
        return kubevirt_unit_rates()
    if provider_type == "ocpvirt":
        parsed = ocpvirt_host_hourly(instance_type)
        if not parsed:
            return kubevirt_unit_rates()
        hourly, vcpus, ram_gib = parsed
        return apportion_hourly(hourly, vcpus, ram_gib)
    row = lookup_instance(provider_type, instance_type)
    if not row:
        return None
    vcpus = float(row.get("vcpus") or host_vcpus or 0)
    ram_gib = float(row.get("ram_gib") or host_ram_gib or 0)
    return apportion_hourly(float(row["hourly_usd"]), vcpus, ram_gib)
