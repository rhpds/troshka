import pytest

from app.services.ocp.agent_template import _collect_ocp_mounts
from app.services.template_loader import generate_topology_from_template


def _template(disks=None):
    return {
        "ocp": [
            {
                "name": "safe",
                "type": "sno",
                "control_plane_disks": [
                    {"size_gb": 120, "bootable": True},
                    {"size_gb": 250, "ocp_mount": "/var/lib/containers"},
                ],
                "nodes": {
                    "cp": {
                        "role": "control-plane",
                        **({"disks": disks} if disks is not None else {}),
                    }
                },
            }
        ],
    }


@pytest.mark.parametrize(
    "disks",
    [
        [{"ocp_mount": "/var/lib/containers"}],
        [{}, {"bootable": True}],
    ],
)
def test_inherited_boot_disk_mount_is_rejected(disks):
    exception_template = _template(disks)
    with pytest.raises(ValueError, match="boot.*disk|disk.*boot"):
        generate_topology_from_template(exception_template)


def test_first_disk_fallback_cannot_be_formatted_as_extra_mount():
    template = {
        "vms": {
            "cp": {
                "os": "rhcos",
                "disks": [
                    {
                        "size_gb": 120,
                        "bootable": False,
                        "ocp_mount": "/var/lib/containers",
                    },
                    {"size_gb": 250},
                ],
            }
        }
    }
    with pytest.raises(ValueError, match="boot.*disk|disk.*boot"):
        generate_topology_from_template(template)


@pytest.mark.parametrize("boot_devices", [[], ["data"], ["root", "data"]])
def test_canvas_mount_on_root_or_boot_order_disk_is_rejected(boot_devices):
    topology = _canvas_topology(boot_devices)
    if not boot_devices:
        topology["nodes"][1]["data"]["ocpMount"] = "/var/lib/containers"
    with pytest.raises(ValueError, match="boot.*disk|disk.*boot"):
        _collect_ocp_mounts(topology)


def _canvas_topology(boot_devices):
    return {
        "nodes": [
            {
                "id": "vm",
                "type": "vmNode",
                "data": {
                    "name": "cp",
                    "os": "rhcos",
                    "bootDevices": boot_devices,
                    "diskControllers": [
                        {"id": "dp-root", "bus": "virtio"},
                        {"id": "dp-data", "bus": "virtio"},
                    ],
                },
            },
            {
                "id": "root",
                "type": "storageNode",
                "data": {"name": "root", "format": "qcow2"},
            },
            {
                "id": "data",
                "type": "storageNode",
                "data": {
                    "name": "data",
                    "format": "qcow2",
                    "ocpMount": "/var/lib/containers",
                },
            },
        ],
        "edges": [
            {"source": "root", "target": "vm", "targetHandle": "dp-root-left"},
            {"source": "data", "target": "vm", "targetHandle": "dp-data-left"},
        ],
    }


def test_extra_disk_mount_is_allowed_after_boot_disk():
    topology = generate_topology_from_template(_template())
    assert _collect_ocp_mounts(topology) == [
        {"device": "/dev/vdb", "mount": "/var/lib/containers"}
    ]


def test_canvas_mount_device_follows_actual_boot_disk_order():
    topology = _canvas_topology(["data"])
    topology["nodes"][2]["data"].pop("ocpMount")
    topology["nodes"][1]["data"]["ocpMount"] = "/srv/custom"
    assert _collect_ocp_mounts(topology) == [
        {"device": "/dev/vdb", "mount": "/srv/custom"}
    ]


def test_canvas_mount_validation_precedes_install_config_changes():
    import copy

    from app.services.ocp.agent_template import (
        _customize_one_cluster,
        customize_topology,
    )

    topology = _canvas_topology(["data"])
    before = copy.deepcopy(topology)
    with pytest.raises(ValueError, match="boot.*disk|disk.*boot"):
        customize_topology(topology, "test", {"common_password": "example"})
    assert topology == before
    with pytest.raises(ValueError, match="boot.*disk|disk.*boot"):
        _customize_one_cluster(topology, {"id": "safe"}, {}, False)
    assert topology == before


def _mount_manifests(topology):
    import base64
    import re

    import yaml

    from app.services.ocp.agent_template import _generate_ocp_mount_script

    script = _generate_ocp_mount_script(topology)
    return [
        (path, yaml.safe_load(base64.b64decode(encoded)))
        for encoded, path in re.findall(r"echo '([^']+)' \| base64 -d > (\S+)", script)
    ]


@pytest.mark.parametrize("workers", [0, 2])
def test_mount_machineconfigs_target_unique_master_and_worker_roles(workers):
    template = _template()
    cluster = template["ocp"][0]
    cluster["workers"] = workers
    cluster["worker_disks"] = cluster["control_plane_disks"]
    manifests = _mount_manifests(generate_topology_from_template(template))
    expected = {"master", "worker"} if workers else {"master"}
    assert {
        manifest["metadata"]["labels"]["machineconfiguration.openshift.io/role"]
        for _, manifest in manifests
    } == expected
    assert len(manifests) == len(expected)
    assert len({path for path, _ in manifests}) == len(expected)
    assert len({manifest["metadata"]["name"] for _, manifest in manifests}) == len(
        expected
    )


def test_conflicting_devices_for_mount_within_role_are_rejected():
    template = _template()
    cluster = template["ocp"][0]
    cluster["type"] = "compact"
    cluster["nodes"]["custom"] = {
        "role": "control-plane",
        "disks": [
            {"size_gb": 120, "bootable": True},
            {"size_gb": 100, "ocp_mount": ""},
            {"size_gb": 250, "ocp_mount": "/var/lib/containers"},
        ],
    }
    topology = generate_topology_from_template(template)
    with pytest.raises(ValueError, match="Conflicting.*mount"):
        _mount_manifests(topology)


def test_mount_on_one_bus_cannot_format_another_same_role_vm_root():
    import copy

    topology = _canvas_topology(["root"])
    topology["nodes"][0]["data"]["clusterRole"] = "control-plane"
    topology["nodes"][0]["data"]["diskControllers"][1]["bus"] = "sata"
    other = copy.deepcopy(topology["nodes"][0])
    other["id"] = "other"
    other["data"]["bootDevices"] = ["other-root"]
    other["data"]["diskControllers"] = [{"id": "dp-other", "bus": "sata"}]
    topology["nodes"].extend(
        [
            other,
            {"id": "other-root", "type": "storageNode", "data": {"format": "qcow2"}},
        ]
    )
    topology["edges"].append(
        {"source": "other-root", "target": "other", "targetHandle": "dp-other-left"}
    )
    with pytest.raises(ValueError, match="boot.*disk|disk.*boot"):
        _mount_manifests(topology)


def test_same_device_on_different_role_root_does_not_block_safe_mount():
    import copy

    topology = _canvas_topology(["root"])
    topology["nodes"][0]["data"]["clusterRole"] = "worker"
    topology["nodes"][0]["data"]["diskControllers"][1]["bus"] = "sata"
    other = copy.deepcopy(topology["nodes"][0])
    other["id"] = "other"
    other["data"]["clusterRole"] = "control-plane"
    other["data"]["bootDevices"] = ["other-root"]
    other["data"]["diskControllers"] = [{"id": "dp-other", "bus": "sata"}]
    topology["nodes"].extend(
        [
            other,
            {"id": "other-root", "type": "storageNode", "data": {"format": "qcow2"}},
        ]
    )
    topology["edges"].append(
        {"source": "other-root", "target": "other", "targetHandle": "dp-other-left"}
    )
    manifests = _mount_manifests(topology)
    assert (
        manifests[0][1]["metadata"]["labels"]["machineconfiguration.openshift.io/role"]
        == "worker"
    )


@pytest.mark.parametrize("workers", [0, 2])
def test_ops_pod_delivers_role_mount_manifests_before_image_creation(workers):
    from app.services.ocp.ops_pod_install import build_ops_pod_install_script

    template = _template()
    cluster = template["ocp"][0]
    cluster["workers"] = workers
    cluster["worker_disks"] = cluster["control_plane_disks"]
    topology = generate_topology_from_template(template)
    script = build_ops_pod_install_script(
        topology["clusters"], {}, "4.22", "/workdir", topology=topology
    )
    expected_roles = ["master", "worker"] if workers else ["master"]
    for role in expected_roles:
        path = f"/workdir/safe/openshift/98-{role}-var-lib-containers.yaml"
        assert path in script
        assert script.index(path) < script.index("agent create image")
    assert "/home/cloud-user/ocp-install/openshift" not in script


def test_ops_pod_mount_manifests_are_isolated_per_cluster():
    import copy

    from app.services.ocp.ops_pod_install import build_ops_pod_install_script

    template = _template()
    other = copy.deepcopy(template["ocp"][0])
    other["name"] = "other"
    other["nodes"] = {"other-cp": {"role": "control-plane"}}
    other["control_plane_disks"][1]["ocp_mount"] = "/srv/other"
    template["ocp"].append(other)
    topology = generate_topology_from_template(template)
    script = build_ops_pod_install_script(
        topology["clusters"], {}, "4.22", "/workdir", topology=topology
    )
    assert "/workdir/safe/openshift/98-master-var-lib-containers.yaml" in script
    assert "/workdir/other/openshift/98-master-srv-other.yaml" in script
    assert "/workdir/other/openshift/98-master-var-lib-containers.yaml" not in script
    assert "/workdir/safe/openshift/98-master-srv-other.yaml" not in script


def test_role_mount_safety_validation_is_scoped_to_each_cluster():
    import copy

    from app.services.ocp.agent_template import _validate_topology_ocp_mounts

    template = _template()
    other = copy.deepcopy(template["ocp"][0])
    other["name"] = "other"
    other["nodes"] = {
        "other-cp": {
            "role": "control-plane",
            "disks": [
                {"size_gb": 120, "bootable": True},
                {"size_gb": 100, "ocp_mount": ""},
                {"size_gb": 250, "ocp_mount": "/var/lib/containers"},
            ],
        }
    }
    template["ocp"].append(other)
    _validate_topology_ocp_mounts(generate_topology_from_template(template))


def test_actual_deploy_runner_delivers_worker_and_master_mount_manifests():
    import subprocess

    from app.services.deploy_service import _build_ops_pod_runner_script

    template = _template()
    cluster = template["ocp"][0]
    cluster["workers"] = 1
    cluster["worker_disks"] = cluster["control_plane_disks"]
    topology = generate_topology_from_template(template)
    script = _build_ops_pod_runner_script(
        topology, topology["clusters"], "4.22", "/workdir"
    )
    assert "/workdir/safe/openshift/98-master-var-lib-containers.yaml" in script
    assert "/workdir/safe/openshift/98-worker-var-lib-containers.yaml" in script
    result = subprocess.run(
        ["bash", "-n"], input=script, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
