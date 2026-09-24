"""Tests for template workload chain helpers."""

from types import SimpleNamespace

from app.services.workloads.template_workloads import (
    _project_workload_ready,
    normalize_workload_roles,
    resolve_template_workload_chain,
)


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


def test_project_workload_ready_accepts_milestone_or_ocp_ready():
    assert (
        _project_workload_ready(
            SimpleNamespace(ocp_control_plane_usable_at=None, ocp_status="monitoring")
        )
        is False
    )
    assert (
        _project_workload_ready(
            SimpleNamespace(ocp_control_plane_usable_at=None, ocp_status="ready")
        )
        is True
    )
    assert (
        _project_workload_ready(
            SimpleNamespace(ocp_control_plane_usable_at="set", ocp_status="monitoring")
        )
        is True
    )


def test_resolve_chain_prefers_deployed_workloads():
    roles = ["rhpds.demo_workloads.troshka_workload_cclm_operators"]
    project = SimpleNamespace(
        deployed_topology={
            "workloads": roles,
            "requirements_content": {"collections": []},
            "clusters": [{"id": "source-1", "name": "source"}],
        },
        topology={"workloads": ["other.role"]},
    )
    got_roles, req, topo = resolve_template_workload_chain(project)
    assert got_roles == roles
    assert req == {"collections": []}
    assert topo["clusters"][0]["id"] == "source-1"


def test_resolve_chain_falls_back_to_editable_topology():
    roles = ["rhpds.demo_workloads.troshka_workload_cclm_operators"]
    project = SimpleNamespace(
        deployed_topology={"clusters": [{"id": "c1", "name": "source"}]},
        topology={
            "workloads": roles,
            "requirements_content": {"collections": [{"name": "x"}]},
        },
    )
    got_roles, req, topo = resolve_template_workload_chain(project)
    assert got_roles == roles
    assert req == {"collections": [{"name": "x"}]}
    assert topo["clusters"][0]["id"] == "c1"
