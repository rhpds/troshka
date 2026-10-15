import copy

import pytest

from app.services.template_loader import (
    build_topology_clusters,
    export_topology_to_template,
    generate_topology_from_template,
)


def _template():
    return {
        "networks": {"cluster": {"cidr": "10.20.0.0/24"}},
        "ocp": [
            {
                "name": "lab",
                "type": "sno",
                "workers": 1,
                "control_plane_cpu": 8,
                "control_plane_memory_gb": 32,
                "worker_memory_gb": 12,
                "networks": ["cluster"],
                "vm_defaults": {
                    "bmc": True,
                    "nested_virt": True,
                    "firmware": "bios",
                    "secure_boot": True,
                },
                "nodes": {
                    "custom-cp": {
                        "role": "control-plane",
                        "nested_virt": False,
                        "bmc": False,
                        "secure_boot": False,
                    }
                },
            }
        ],
    }


def _members(topology):
    return {
        n["data"]["name"]: n["data"] for n in topology["nodes"] if n["type"] == "vmNode"
    }


def test_cluster_nodes_and_vm_defaults_apply_to_explicit_and_generated_members():
    source = _template()
    before = copy.deepcopy(source)
    members = _members(generate_topology_from_template(source))
    assert set(members) == {"custom-cp", "lab-worker-0"}
    cp, worker = members["custom-cp"], members["lab-worker-0"]
    assert (cp["vcpus"], cp["ram"], worker["ram"]) == (8, 32, 12)
    assert cp["bmcEnabled"] is False
    assert cp["nestedVirt"] is False
    assert cp["secureBoot"] is False
    assert worker["bmcEnabled"] is True
    assert worker["nestedVirt"] is True
    assert worker["secureBoot"] is True
    assert cp["firmware"] == worker["firmware"] == "bios"
    assert len(cp["nics"]) == len(worker["nics"]) == 1
    assert source == before


def test_legacy_vm_overrides_and_unrelated_rhel_vm_are_preserved():
    source = _template()
    source["vms"] = {
        "legacy-worker": {
            "role": "worker",
            "vcpus": 3,
            "ram_gb": 6,
            "nics": [],
            "firmware": "uefi",
        },
        "unrelated": {"role": "worker", "os": "rhel", "nics": []},
    }
    members = _members(generate_topology_from_template(source))
    worker = members["legacy-worker"]
    assert (worker["vcpus"], worker["ram"], worker["nics"], worker["firmware"]) == (
        3,
        6,
        [],
        "uefi",
    )
    assert members["unrelated"]["vcpus"] == 2
    assert members["unrelated"]["ram"] == 4
    assert "nestedVirt" not in members["unrelated"]


@pytest.mark.parametrize("second_cluster", [False, True])
def test_duplicate_node_names_are_rejected(second_cluster):
    source = _template()
    if second_cluster:
        source["ocp"].append(
            {
                "name": "other",
                "type": "sno",
                "nodes": {"custom-cp": {"role": "control-plane"}},
            }
        )
    else:
        source["vms"] = {"custom-cp": {"role": "control-plane"}}
    with pytest.raises(ValueError, match="Duplicate VM name"):
        generate_topology_from_template(source)


@pytest.mark.parametrize("role", ["control_plane", "worker"])
def test_memory_units_accept_legacy_and_reject_conflicts(role):
    entry = {"name": "lab", f"{role}_memory_gb": 12, f"{role}_memory": 12288}
    cluster = build_topology_clusters([entry], {})[0]
    assert (
        cluster["controlPlaneMemory" if role == "control_plane" else "workerMemory"]
        == 12288
    )
    entry[f"{role}_memory"] = 8192
    with pytest.raises(ValueError, match="Conflicting"):
        build_topology_clusters([entry], {})


def test_false_bmc_and_nested_virt_survive_export():
    topology = generate_topology_from_template(_template())
    exported = export_topology_to_template(topology)
    cp = exported["vms"]["custom-cp"]
    assert cp["bmc"] is False
    assert cp["nested_virt"] is False
    assert cp["secure_boot"] is False
    imported = _members(generate_topology_from_template(exported))
    assert imported["custom-cp"]["bmcEnabled"] is False
    assert imported["custom-cp"]["nestedVirt"] is False
    assert imported["custom-cp"]["secureBoot"] is False


def test_vm_alias_overrides_shared_setting_and_template_requirement():
    source = _template()
    source["requires_kubevirt"] = True
    cp = source["ocp"][0]["nodes"]["custom-cp"]
    cp.pop("nested_virt")
    cp["nestedVirt"] = False
    members = _members(generate_topology_from_template(source))
    assert members["custom-cp"]["nestedVirt"] is False


def test_non_rhcos_role_tags_do_not_increase_inferred_cluster_workers():
    source = _template()
    source["ocp"][0].pop("workers")
    source["vms"] = {"unrelated": {"os": "rhel", "role": "worker"}}
    topology = generate_topology_from_template(source)
    assert topology["clusters"][0]["workers"] == 0
    assert set(_members(topology)) == {"custom-cp", "unrelated"}


def test_nodes_only_templates_are_listed(tmp_path):
    import yaml

    from app.services.template_loader import list_yaml_templates

    (tmp_path / "nodes.yaml").write_text(yaml.safe_dump(_template()))
    assert [entry["id"] for entry in list_yaml_templates(str(tmp_path))] == ["nodes"]


def test_bastion_ip_allocator_accounts_for_cluster_local_nodes():
    from app.services.template_loader import _collect_nic_ips_on_network

    source = _template()
    source["ocp"][0]["nodes"]["custom-cp"]["nics"] = [
        {"network": "cluster", "ip": "10.20.0.5"}
    ]
    assert _collect_nic_ips_on_network(source, "cluster") == {"10.20.0.5"}


def test_library_references_on_cluster_local_nodes_are_resolved(monkeypatch):
    from types import SimpleNamespace

    from app.api import projects

    visited = []
    monkeypatch.setattr(
        projects,
        "_resolve_vm_library_refs",
        lambda db, uid, name, cfg, missing: visited.append(name),
    )
    projects._resolve_template_library_items(
        None, SimpleNamespace(id="user"), {}, _template()["ocp"]
    )
    assert visited == ["custom-cp"]


def test_export_uses_gib_memory_and_preserves_shared_vm_defaults():
    exported = export_topology_to_template(generate_topology_from_template(_template()))
    cluster = exported["ocp"][0]
    assert cluster["control_plane_memory_gb"] == 32
    assert cluster["worker_memory_gb"] == 12
    assert "control_plane_memory" not in cluster
    assert cluster["vm_defaults"] == _template()["ocp"][0]["vm_defaults"]


def test_cluster_role_survives_export_when_tags_are_customized():
    from app.services.template_loader import _export_vm_role

    assert (
        _export_vm_role({"clusterRole": "control-plane", "tags": {}}) == "control-plane"
    )
    assert (
        _export_vm_role({"clusterRole": "worker", "tags": {"env": "lab"}}) == "worker"
    )


@pytest.mark.parametrize("mode,expected", [("none", None), ("custom", "replacement")])
def test_export_password_mode_covers_cluster_defaults_and_nodes(mode, expected):
    from app.api.projects import _apply_password_mode

    template = _template()
    defaults = template["ocp"][0]["vm_defaults"]
    node = template["ocp"][0]["nodes"]["custom-cp"]
    defaults["cloud_user_password"] = "shared-example"
    node["cloud_user_password"] = "node-example"
    _apply_password_mode(template, mode, "replacement")
    assert defaults.get("cloud_user_password") == expected
    assert node.get("cloud_user_password") == expected


@pytest.mark.parametrize(
    "setting,data_key",
    [
        ("cloud_init", "cloudInit"),
        ("minimal_cloud_init", "ciMinimalCloudInit"),
        ("userdata_only", "ciUserDataOnly"),
        ("separate_host", "separateHost"),
        ("legacy_root_bus", "legacyRootBus"),
        ("serial_only", "headless"),
        ("power_on", "powerOnAtDeploy"),
        ("serial_exec", "serialExecType"),
    ],
)
def test_boolean_vm_default_overrides_survive_roundtrip(setting, data_key):
    source = _template()
    source["ocp"][0]["vm_defaults"][setting] = True
    source["ocp"][0]["nodes"]["custom-cp"][setting] = False
    topology = generate_topology_from_template(source)
    assert _members(topology)["custom-cp"][data_key] in (False, "")
    exported = export_topology_to_template(topology)
    assert exported["vms"]["custom-cp"][setting] is False
    imported = generate_topology_from_template(exported)
    assert _members(imported)["custom-cp"][data_key] in (False, "")


@pytest.mark.parametrize("explicit_empty", [False, True])
def test_cluster_network_defaults_infer_from_nodes_unless_explicit(explicit_empty):
    source = _template()
    cluster = source["ocp"][0]
    cluster.pop("networks")
    source["networks"]["migration"] = {"cidr": "172.20.0.0/24"}
    cluster["nodes"]["custom-cp"]["nics"] = [
        {"network": "cluster", "ip": "10.20.0.10"},
        {"network": "migration", "ip": "172.20.0.10"},
    ]
    if explicit_empty:
        cluster["networks"] = []
    members = _members(generate_topology_from_template(source))
    assert len(members["custom-cp"]["nics"]) == 2
    assert len(members["lab-worker-0"]["nics"]) == (0 if explicit_empty else 2)


@pytest.mark.parametrize("role,value", [("worker", False), ("control-plane", True)])
def test_bmc_override_survives_default_role_value(role, value):
    source = _template()
    source["ocp"][0]["vm_defaults"]["bmc"] = not value
    source["ocp"][0]["nodes"] = {"custom": {"role": role, "bmc": value}}
    exported = export_topology_to_template(generate_topology_from_template(source))
    assert exported["vms"]["custom"]["bmc"] is value
    assert (
        _members(generate_topology_from_template(exported))["custom"]["bmcEnabled"]
        is value
    )


@pytest.mark.parametrize(
    "setting,shared,override,data_key",
    [
        ("packages", ["vim"], [], "ciPackages"),
        ("cloud_user_password", "example", "", "ciCloudUserPassword"),
        ("user_data", "example", "", "ciUserData"),
        ("network_config", "example", "", "ciNetworkConfig"),
        ("login_user", "example", "", "ciLoginUser"),
        ("affinity_group", "example", "", "affinityGroup"),
        ("machine_type", "q35", "", "machineType"),
        ("tags", {"custom": "example"}, {}, "tags"),
        ("serial_exec", "ios", "linux", "serialExecType"),
    ],
)
def test_empty_vm_setting_override_survives_roundtrip(
    setting, shared, override, data_key
):
    source = _template()
    source["ocp"][0]["vm_defaults"][setting] = shared
    source["ocp"][0]["nodes"]["custom-cp"][setting] = override
    topology = generate_topology_from_template(source)
    assert _members(topology)["custom-cp"][data_key] == override
    exported = export_topology_to_template(topology)
    assert exported["vms"]["custom-cp"][setting] == override
    imported = generate_topology_from_template(exported)
    assert _members(imported)["custom-cp"][data_key] == override


def test_serial_exec_true_uses_linux_profile():
    source = _template()
    source["ocp"][0]["vm_defaults"]["serial_exec"] = True
    members = _members(generate_topology_from_template(source))
    assert members["custom-cp"]["serialExecType"] == "linux"
    assert members["lab-worker-0"]["serialExecType"] == "linux"
