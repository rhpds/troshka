"""Node allocatable CPU quantities must not overstate usable whole vCPUs."""

from types import SimpleNamespace

import pytest

from app.services.providers.kubevirt import _node_allocatable_vcpus_ram_mb


@pytest.mark.parametrize(
    "quantity,expected",
    [("255500m", 255), ("4", 4), ("4.9", 4), ("500m", 0), ("0", 0)],
)
def test_allocatable_cpu_quantities(quantity, expected):
    node = SimpleNamespace(
        status=SimpleNamespace(allocatable={"cpu": quantity, "memory": "2Gi"})
    )
    assert _node_allocatable_vcpus_ram_mb(node) == (expected, 2048)


@pytest.mark.parametrize(
    "quantity", ["invalid", "-1", "-500m", "NaN", "Infinity", "-Infinity"]
)
def test_allocatable_cpu_rejects_invalid_quantities(quantity):
    node = SimpleNamespace(
        status=SimpleNamespace(allocatable={"cpu": quantity, "memory": "2Gi"})
    )
    with pytest.raises(ValueError):
        _node_allocatable_vcpus_ram_mb(node)
