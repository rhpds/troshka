"""Tests for template workload chain helpers."""

from app.services.workloads.template_workloads import normalize_workload_roles


def test_normalize_workload_roles_strings_and_maps():
    assert normalize_workload_roles(
        [
            "rhpds.demo_workloads.role_a",
            {"name": "rhpds.demo_workloads.role_b"},
            {"role": "rhpds.demo_workloads.role_c"},
            "",
            {},
        ]
    ) == [
        "rhpds.demo_workloads.role_a",
        "rhpds.demo_workloads.role_b",
        "rhpds.demo_workloads.role_c",
    ]
