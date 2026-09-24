"""Tests for template workload chain helpers."""

from types import SimpleNamespace

from app.services.workloads.template_workloads import (
    _project_workload_ready,
    mark_run_once_roles_done,
    next_auto_workload_role,
    normalize_workload_roles,
    resolve_template_workload_chain,
    run_once_roles,
    workloads_done_set,
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


def test_run_once_roles_from_mappings():
    assert run_once_roles(
        [
            "always.rerun",
            {"role": "once.a", "runOnce": True},
            {"name": "once.b", "runOnce": True},
            {"role": "not.once", "runOnce": False},
        ]
    ) == {"once.a", "once.b"}


def test_next_auto_skips_run_once_when_in_workloads_done():
    workloads = [
        {"role": "role.one", "runOnce": True},
        {"role": "role.two", "runOnce": True},
        "role.three",
    ]
    assert (
        next_auto_workload_role(
            workloads,
            succeeded=set(),
            done={"role.one", "role.two"},
        )
        == "role.three"
    )
    # Non-runOnce roles only skip via succeeded (not workloadsDone).
    assert (
        next_auto_workload_role(
            workloads,
            succeeded={"role.three"},
            done={"role.one", "role.two"},
        )
        is None
    )


def test_next_auto_runs_run_once_when_not_done():
    workloads = [{"role": "role.one", "runOnce": True}, "role.two"]
    assert next_auto_workload_role(workloads, succeeded=set(), done=set()) == "role.one"


def test_mark_run_once_roles_done_stamps_topology():
    topo = {
        "workloads": [
            {"role": "a.once", "runOnce": True},
            "b.always",
            {"role": "c.once", "runOnce": True},
        ]
    }
    marked = mark_run_once_roles_done(topo)
    assert marked == {"a.once", "c.once"}
    assert set(topo["workloadsDone"]) == {"a.once", "c.once"}
    # Idempotent merge
    topo["workloadsDone"] = ["a.once", "extra"]
    mark_run_once_roles_done(topo)
    assert set(topo["workloadsDone"]) == {"a.once", "c.once", "extra"}


def test_workloads_done_set():
    assert workloads_done_set({"workloadsDone": ["a", "b", ""]}) == {"a", "b"}
    assert workloads_done_set({}) == set()


def test_project_workload_ready_accepts_milestone_or_ocp_ready():
    assert (
        _project_workload_ready(
            SimpleNamespace(
                ocp_control_plane_usable_at=None,
                ocp_status="monitoring",
                deployed_topology={},
                topology={},
            )
        )
        is False
    )
    assert (
        _project_workload_ready(
            SimpleNamespace(
                ocp_control_plane_usable_at=None,
                ocp_status="ready",
                deployed_topology={},
                topology={},
            )
        )
        is True
    )
    assert (
        _project_workload_ready(
            SimpleNamespace(
                ocp_control_plane_usable_at="set",
                ocp_status="monitoring",
                deployed_topology={},
                topology={},
            )
        )
        is True
    )


def test_project_workload_ready_requires_all_clusters_ready():
    """Multi-cluster: milestone alone is not enough if a sibling is not ready."""
    topo = {
        "clusters": [
            {"id": "source", "name": "source", "ocpInstallStatus": "ready"},
            {
                "id": "destination",
                "name": "destination",
                "ocpInstallStatus": "monitoring",
            },
        ]
    }
    assert (
        _project_workload_ready(
            SimpleNamespace(
                ocp_control_plane_usable_at="set",
                ocp_status="monitoring",
                deployed_topology=topo,
                topology={},
            )
        )
        is False
    )
    topo["clusters"][1]["ocpInstallStatus"] = "ready"
    assert (
        _project_workload_ready(
            SimpleNamespace(
                ocp_control_plane_usable_at="set",
                ocp_status="ready",
                deployed_topology=topo,
                topology={},
            )
        )
        is True
    )


def test_all_ocp_clusters_ready_helper():
    from app.services.workloads.template_workloads import _all_ocp_clusters_ready

    assert _all_ocp_clusters_ready({}) is True
    assert _all_ocp_clusters_ready({"clusters": []}) is True
    # No statuses stamped yet → don't block (legacy / VM-adjacent)
    assert _all_ocp_clusters_ready({"clusters": [{"id": "a"}, {"id": "b"}]}) is True
    assert (
        _all_ocp_clusters_ready(
            {
                "clusters": [
                    {"id": "a", "ocpInstallStatus": "ready"},
                    {"id": "b", "ocpInstallStatus": "ready"},
                ]
            }
        )
        is True
    )
    assert (
        _all_ocp_clusters_ready(
            {
                "clusters": [
                    {"id": "a", "ocpInstallStatus": "ready"},
                    {"id": "b", "ocpInstallStatus": "monitoring"},
                ]
            }
        )
        is False
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
