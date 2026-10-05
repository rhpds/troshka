from datetime import UTC, datetime, timedelta

from app.models.host import Host
from app.services.instance_rates import (
    apportion_hourly,
    kubevirt_unit_rates,
    lookup_instance,
    ocpvirt_host_hourly,
    rates_for_host,
)
from app.services.metering_math import interval_cost, resolve_rate
from app.services.metering_service import compose_host_rates


def test_catalog_has_supported_host_skus():
    assert lookup_instance("ec2", "m8i.xlarge")["hourly_usd"] == 0.2117
    assert lookup_instance("aws", "r8i.metal-48xl")["bare_metal"] is True
    assert lookup_instance("gcp", "n2-highmem-8")["vcpus"] == 8
    assert lookup_instance("azure", "Standard_E8s_v5")["hourly_usd"] == 0.504


def test_apportion_quarter_vm_is_quarter_of_host_hourly():
    kv = kubevirt_unit_rates()
    rates = apportion_hourly(1.1114, 16, 128, kv)
    start = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    cpu = interval_cost(4, rates["vcpu_hour"], start, end)
    ram = interval_cost(32, rates["ram_gib_hour"], start, end)
    assert abs((cpu + ram) - (1.1114 * 0.25)) < 1e-9


def test_metal_host_hourly_is_higher_than_nested_sku():
    nested = lookup_instance("ec2", "m8i.xlarge")["hourly_usd"]
    metal = lookup_instance("ec2", "m8i.metal-48xl")["hourly_usd"]
    assert metal > nested


def test_ocpvirt_size_uses_kubevirt_unit_rates():
    kv = kubevirt_unit_rates()
    rates = rates_for_host("ocpvirt", "8c-32g")
    assert abs(rates["vcpu_hour"] - kv["vcpu_hour"]) < 1e-9
    assert abs(rates["ram_gib_hour"] - kv["ram_gib_hour"]) < 1e-9
    hourly, vcpus, ram = ocpvirt_host_hourly("8c-32g")
    assert vcpus == 8
    assert ram == 32
    assert abs(hourly - (8 * kv["vcpu_hour"] + 32 * kv["ram_gib_hour"])) < 1e-9


def test_unknown_sku_falls_back_to_kubevirt_with_nested_factor():
    kv = kubevirt_unit_rates()
    host = Host(instance_type="no-such-size", total_vcpus=4, total_ram_mb=8192)
    rates = compose_host_rates(host, "libvirt", {})
    assert abs(rates["vcpu_hour"] - kv["vcpu_hour"] * 0.5) < 1e-9


def test_instance_catalog_beats_type_default():
    host = Host(instance_type="m8i.xlarge", total_vcpus=4, total_ram_mb=16384)
    rates = compose_host_rates(host, "ec2", {"ec2": {"vcpu_hour": 9.99}})
    derived = rates_for_host("ec2", "m8i.xlarge")
    assert abs(rates["vcpu_hour"] - derived["vcpu_hour"] * 0.5) < 1e-9
    assert rates["vcpu_hour"] != 9.99


def test_host_override_beats_catalog():
    host = Host(
        instance_type="m8i.xlarge",
        total_vcpus=4,
        total_ram_mb=16384,
        metering_rates={"vcpu_hour": 0.01},
    )
    rates = compose_host_rates(host, "ec2", {})
    assert rates["vcpu_hour"] == 0.01


def test_kubevirt_skips_nested_factor():
    kv = kubevirt_unit_rates()
    host = Host(instance_type="pool", total_vcpus=8, total_ram_mb=32768)
    rates = compose_host_rates(host, "kubevirt", {})
    assert abs(rates["vcpu_hour"] - kv["vcpu_hour"]) < 1e-9


def test_dedicated_skips_nested_factor():
    kv = kubevirt_unit_rates()
    host = Host(
        instance_type="8c-32g",
        billing_mode="dedicated",
        total_vcpus=8,
        total_ram_mb=32768,
    )
    rates = compose_host_rates(host, "ocpvirt", {})
    assert abs(rates["vcpu_hour"] - kv["vcpu_hour"]) < 1e-9


def test_resolve_rate_kubevirt_fallback():
    kv = kubevirt_unit_rates()
    assert resolve_rate("vcpu", "libvirt", None, {}, fallback=kv) == kv["vcpu_hour"]
