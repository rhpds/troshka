"""Tests for extracted helper functions from projects.py and patterns.py."""

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# _resolve_deploy_progress
# ---------------------------------------------------------------------------
class TestResolveDeployProgress:
    def _call(self, project):
        from app.api.projects import _resolve_deploy_progress

        return _resolve_deploy_progress(project)

    def test_non_transitional_state_returns_none(self):
        project = MagicMock()
        project.state = "active"
        project.deploy_progress = {"step": "leftover"}
        assert self._call(project) is None

    def test_draft_state_returns_none(self):
        project = MagicMock()
        project.state = "draft"
        assert self._call(project) is None

    @patch("app.services.deploy_service._get_deploy_progress_data")
    def test_deploying_with_live_progress(self, mock_get):
        mock_get.return_value = {"step": "downloading", "pct": 42}
        project = MagicMock()
        project.state = "deploying"
        project.id = "proj-1"
        result = self._call(project)
        assert result == {"step": "downloading", "pct": 42}
        mock_get.assert_called_once_with("proj-1")

    @patch("app.core.redis.get_job_info")
    @patch("app.services.deploy_service._get_deploy_progress_data")
    def test_deploying_queued_job(self, mock_get_progress, mock_job_info):
        mock_get_progress.return_value = None
        mock_job_info.return_value = {
            "status": "queued",
            "queue_position": 3,
            "queue_length": 5,
        }
        project = MagicMock()
        project.state = "deploying"
        project.id = "proj-2"
        project.deploy_progress = None
        result = self._call(project)
        assert result == {"step": "queued", "detail": "#3 of 5"}

    @patch("app.services.deploy_service._get_deploy_progress_data")
    def test_stopping_with_stored_progress(self, mock_get):
        mock_get.return_value = None
        project = MagicMock()
        project.state = "stopping"
        project.id = "proj-3"
        project.deploy_progress = {"step": "stopping_vms"}
        result = self._call(project)
        assert result == {"step": "stopping_vms"}

    @patch("app.services.deploy_service._get_deploy_progress_data")
    def test_reconfiguring_no_progress(self, mock_get):
        mock_get.return_value = None
        project = MagicMock()
        project.state = "reconfiguring"
        project.id = "proj-4"
        project.deploy_progress = None
        result = self._call(project)
        assert result is None

    @patch("app.core.redis.get_job_info")
    @patch("app.services.deploy_service._get_deploy_progress_data")
    def test_deploying_queued_missing_position(self, mock_get_progress, mock_job_info):
        """Queue info without position/length uses '?' placeholders."""
        mock_get_progress.return_value = None
        mock_job_info.return_value = {"status": "queued"}
        project = MagicMock()
        project.state = "deploying"
        project.id = "proj-5"
        project.deploy_progress = None
        result = self._call(project)
        assert result == {"step": "queued", "detail": "#? of ?"}


# ---------------------------------------------------------------------------
# _find_kubeconfig_content
# ---------------------------------------------------------------------------
class TestFindKubeconfigContent:
    def _call(self, topo, vm=None):
        from app.api.projects import _find_kubeconfig_content

        return _find_kubeconfig_content(topo, vm)

    def test_empty_topology(self):
        assert self._call({}) is None

    def test_topology_no_nodes(self):
        assert self._call({"nodes": []}) is None

    def test_no_vm_nodes(self):
        topo = {
            "nodes": [
                {"type": "networkNode", "data": {"ocpKubeconfig": "should-ignore"}},
            ]
        }
        assert self._call(topo) is None

    def test_vm_with_kubeconfig_no_filter(self):
        topo = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {
                        "label": "bastion",
                        "ocpKubeconfig": "apiVersion: v1\nclusters: ...",
                    },
                },
            ]
        }
        result = self._call(topo)
        assert result == "apiVersion: v1\nclusters: ..."

    def test_vm_with_kubeconfig_filter_match(self):
        topo = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {"label": "ocp-master", "ocpKubeconfig": "master-kc"},
                },
                {
                    "type": "vmNode",
                    "data": {"label": "ocp-worker", "ocpKubeconfig": "worker-kc"},
                },
            ]
        }
        assert self._call(topo, vm="ocp-worker") == "worker-kc"

    def test_vm_filter_no_match(self):
        topo = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {"label": "bastion", "ocpKubeconfig": "kc-data"},
                },
            ]
        }
        assert self._call(topo, vm="nonexistent") is None

    def test_vm_without_kubeconfig(self):
        topo = {
            "nodes": [
                {"type": "vmNode", "data": {"label": "plain-vm"}},
            ]
        }
        assert self._call(topo) is None

    def test_name_fallback_when_no_label(self):
        """Uses data.name when data.label is absent."""
        topo = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {"name": "sno-1", "ocpKubeconfig": "sno-kc"},
                },
            ]
        }
        assert self._call(topo, vm="sno-1") == "sno-kc"

    def test_first_match_wins_without_filter(self):
        topo = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {"label": "vm-a", "ocpKubeconfig": "kc-a"},
                },
                {
                    "type": "vmNode",
                    "data": {"label": "vm-b", "ocpKubeconfig": "kc-b"},
                },
            ]
        }
        assert self._call(topo) == "kc-a"


# ---------------------------------------------------------------------------
# _build_destroy_context
# ---------------------------------------------------------------------------
class TestBuildDestroyContext:
    def _call(self, project):
        from app.api.projects import _build_destroy_context

        return _build_destroy_context(project)

    def test_basic_context(self):
        project = MagicMock()
        project.id = "proj-abc"
        project.host_id = "host-1"
        project.vni_map = {"net-1": 100}
        project.deployed_topology = {"nodes": [{"id": "n1"}]}
        project.topology = {"nodes": [{"id": "n2"}]}
        project.dns_provider_id = "dns-1"
        project.domain = "lab.example.com"

        ctx = self._call(project)
        assert ctx["project_id"] == "proj-abc"
        assert ctx["host_id"] == "host-1"
        assert ctx["vni_map"] == {"net-1": 100}
        # Should prefer deployed_topology over topology
        assert ctx["topology"] == {"nodes": [{"id": "n1"}]}
        assert ctx["dns_provider_id"] == "dns-1"
        assert ctx["domain"] == "lab.example.com"

    def test_falls_back_to_topology_when_no_deployed(self):
        project = MagicMock()
        project.id = "proj-def"
        project.host_id = "host-2"
        project.vni_map = None
        project.deployed_topology = None
        project.topology = {"nodes": [{"id": "n3"}]}
        project.dns_provider_id = None
        project.domain = None

        ctx = self._call(project)
        assert ctx["vni_map"] == {}
        assert ctx["topology"] == {"nodes": [{"id": "n3"}]}
        assert ctx["dns_provider_id"] is None
        assert ctx["domain"] is None

    def test_empty_when_both_topologies_none(self):
        project = MagicMock()
        project.id = "proj-ghi"
        project.host_id = None
        project.vni_map = None
        project.deployed_topology = None
        project.topology = None
        project.dns_provider_id = None
        project.domain = None

        ctx = self._call(project)
        assert ctx["topology"] == {}

    def test_deep_copy_isolation(self):
        """Mutating the returned context must not affect the project."""
        original_topo = {"nodes": [{"id": "n1", "data": {"label": "vm"}}]}
        project = MagicMock()
        project.id = "p"
        project.host_id = "h"
        project.vni_map = {"net": 1}
        project.deployed_topology = original_topo
        project.topology = None
        project.dns_provider_id = None
        project.domain = None

        ctx = self._call(project)
        ctx["topology"]["nodes"][0]["data"]["label"] = "MODIFIED"
        ctx["vni_map"]["net"] = 999

        # Original data should be unaffected
        assert original_topo["nodes"][0]["data"]["label"] == "vm"
        # vni_map is deepcopied too
        assert project.vni_map == {"net": 1}


# ---------------------------------------------------------------------------
# _apply_inject_vars (lives in patterns.py)
# ---------------------------------------------------------------------------
class TestApplyInjectVars:
    def _call(self, nodes, inject_vars):
        from app.api.patterns import _apply_inject_vars

        _apply_inject_vars(nodes, inject_vars)

    def test_targets_bastion_vm(self):
        nodes = [
            {
                "type": "vmNode",
                "data": {
                    "cloudInit": True,
                    "tags": {"AnsibleGroup": "bastions"},
                },
            },
            {
                "type": "vmNode",
                "data": {
                    "cloudInit": True,
                    "tags": {"AnsibleGroup": "workers"},
                },
            },
        ]
        inject = {"guid": "abc123"}
        self._call(nodes, inject)
        assert nodes[0]["data"]["ciInjectVars"] == {"guid": "abc123"}
        assert "ciInjectVars" not in nodes[1]["data"]

    def test_falls_back_to_cloud_init_vm(self):
        nodes = [
            {"type": "networkNode", "data": {}},
            {
                "type": "vmNode",
                "data": {"cloudInit": True, "tags": {"AnsibleGroup": "workers"}},
            },
        ]
        inject = {"key": "val"}
        self._call(nodes, inject)
        assert nodes[1]["data"]["ciInjectVars"] == {"key": "val"}

    def test_no_target_found(self):
        nodes = [
            {"type": "networkNode", "data": {}},
            {"type": "storageNode", "data": {}},
        ]
        inject = {"key": "val"}
        self._call(nodes, inject)
        # No crash, no injection
        for n in nodes:
            assert "ciInjectVars" not in n.get("data", {})

    def test_bastion_in_multi_group_tag(self):
        """Bastion detection works when AnsibleGroup has multiple groups."""
        nodes = [
            {
                "type": "vmNode",
                "data": {
                    "cloudInit": True,
                    "tags": {"AnsibleGroup": "utility, bastions, nfs"},
                },
            },
        ]
        inject = {"foo": "bar"}
        self._call(nodes, inject)
        assert nodes[0]["data"]["ciInjectVars"] == {"foo": "bar"}

    def test_bastion_preferred_over_cloud_init(self):
        """Even if a cloud-init VM appears first, bastion takes priority."""
        nodes = [
            {
                "type": "vmNode",
                "data": {"cloudInit": True, "tags": {"AnsibleGroup": "workers"}},
            },
            {
                "type": "vmNode",
                "data": {"cloudInit": True, "tags": {"AnsibleGroup": "bastions"}},
            },
        ]
        inject = {"x": "y"}
        self._call(nodes, inject)
        assert "ciInjectVars" not in nodes[0]["data"]
        assert nodes[1]["data"]["ciInjectVars"] == {"x": "y"}

    def test_empty_nodes_list(self):
        self._call([], {"key": "val"})  # should not raise

    def test_vm_without_cloud_init_not_targeted(self):
        """A vmNode without cloudInit is not targeted as fallback."""
        nodes = [
            {
                "type": "vmNode",
                "data": {"tags": {"AnsibleGroup": "workers"}},
            },
        ]
        inject = {"k": "v"}
        self._call(nodes, inject)
        assert "ciInjectVars" not in nodes[0]["data"]


# ---------------------------------------------------------------------------
# _build_kubevirt_vm_spec
# ---------------------------------------------------------------------------
class TestBuildKubevirtVmSpec:
    def _call(self, vm_id, vm, current):
        from app.api.projects import _build_kubevirt_vm_spec

        return _build_kubevirt_vm_spec(vm_id, vm, current)

    def _make_topology(self, vm_id, vm_data, storage_nodes=None, edges=None):
        """Build a minimal topology with a VM node and optional storage."""
        nodes = [{"id": vm_id, "type": "vmNode", "data": vm_data}]
        if storage_nodes:
            nodes.extend(storage_nodes)
        return {"nodes": nodes, "edges": edges or []}

    @patch("app.services.deploy_service._find_vm_disks")
    def test_basic_vm_spec(self, mock_find_disks):
        mock_find_disks.return_value = []
        vm_id = "vm-001"
        vm_data = {
            "id": vm_id,
            "label": "bastion",
            "nics": [{"id": "nic-1", "mac": "00:11:22:33:44:55"}],
            "bootDevices": ["disk-1"],
            "powerOnAtDeploy": True,
            "recertEnabled": False,
            "ocpMonitor": False,
            "configureBastionBrowser": False,
            "bmcEnabled": False,
            "domainUuid": "uuid-123",
        }
        current = self._make_topology(vm_id, vm_data)
        vm = {
            "name": "bastion",
            "vcpus": 4,
            "ram_gb": 16,
            "firmware": "uefi",
            "os": "rhel9",
        }

        spec = self._call(vm_id, vm, current)

        assert spec["vmId"] == vm_id
        assert spec["name"] == "bastion"
        assert spec["cpus"] == 4
        assert spec["memory"] == 16 * 1024
        assert spec["firmware"] == "uefi"
        assert spec["machineType"] == "q35"
        assert spec["smbiosUuid"] == "uuid-123"
        assert spec["os"] == "rhel9"
        assert spec["powerOnAtDeploy"] is True
        assert spec["recertEnabled"] is False
        assert spec["disks"] == []
        assert spec["nics"] == [{"id": "nic-1", "mac": "00:11:22:33:44:55"}]
        assert spec["bootOrder"] == ["disk-1"]
        assert spec["cloudInit"] == {"userData": "", "networkConfig": ""}

    @patch("app.services.deploy_service._find_vm_disks")
    def test_disk_from_pattern(self, mock_find_disks):
        mock_find_disks.return_value = [
            {
                "node_id": "disk-1",
                "size": 40,
                "format": "qcow2",
                "source": "pattern",
                "patternId": "pat-1",
                "resolvedS3Path": "patterns/pat-1/disk-1.qcow2",
                "centralSource": False,
            }
        ]
        vm_id = "vm-002"
        vm_data = {"id": vm_id, "nics": []}
        current = self._make_topology(vm_id, vm_data)
        vm = {"name": "ocp-master", "vcpus": 8, "ram_gb": 32}

        spec = self._call(vm_id, vm, current)

        assert len(spec["disks"]) == 1
        disk = spec["disks"][0]
        assert disk["id"] == "disk-1"
        assert disk["sizeGb"] == 40
        assert disk["bus"] == "virtio"
        assert disk["format"] == "qcow2"
        assert disk["patternImage"]["s3Path"] == "patterns/pat-1/disk-1.qcow2"
        assert disk["patternImage"]["central"] is False
        assert "blank" not in disk
        assert "libraryImage" not in disk

    @patch("app.services.deploy_service._find_vm_disks")
    def test_disk_from_library(self, mock_find_disks):
        mock_find_disks.return_value = [
            {
                "node_id": "disk-2",
                "size": 20,
                "format": "raw",
                "source": "library",
                "libraryItemId": "lib-1",
                "resolvedS3Path": "library/lib-1.raw",
                "centralSource": True,
            }
        ]
        vm_id = "vm-003"
        vm_data = {"id": vm_id, "nics": []}
        current = self._make_topology(vm_id, vm_data)
        vm = {"name": "sno", "vcpus": 16, "ram_gb": 64}

        spec = self._call(vm_id, vm, current)

        disk = spec["disks"][0]
        assert disk["libraryImage"]["s3Path"] == "library/lib-1.raw"
        assert disk["libraryImage"]["format"] == "raw"
        assert disk["libraryImage"]["central"] is True
        assert "patternImage" not in disk
        assert "blank" not in disk

    @patch("app.services.deploy_service._find_vm_disks")
    def test_blank_disk(self, mock_find_disks):
        mock_find_disks.return_value = [
            {
                "node_id": "disk-3",
                "size": 100,
                "format": "qcow2",
                "source": "blank",
            }
        ]
        vm_id = "vm-004"
        vm_data = {"id": vm_id, "nics": []}
        current = self._make_topology(vm_id, vm_data)
        vm = {"name": "target", "vcpus": 2, "ram_gb": 4}

        spec = self._call(vm_id, vm, current)

        disk = spec["disks"][0]
        assert disk["blank"] is True
        assert "patternImage" not in disk
        assert "libraryImage" not in disk

    @patch("app.services.deploy_service._find_vm_disks")
    def test_defaults_for_missing_vm_fields(self, mock_find_disks):
        mock_find_disks.return_value = []
        vm_id = "vm-005"
        vm_data = {"id": vm_id}
        current = self._make_topology(vm_id, vm_data)
        # vm dict missing optional keys
        vm = {"name": "minimal"}

        spec = self._call(vm_id, vm, current)

        assert spec["cpus"] == 2  # default vcpus
        assert spec["memory"] == 4 * 1024  # default 4 GB
        assert spec["firmware"] == "bios"  # default firmware
        assert spec["os"] == ""
        assert spec["powerOnAtDeploy"] is True
        assert spec["recertEnabled"] is False
        assert spec["ocpMonitor"] is False
        assert spec["bmcEnabled"] is False
        assert spec["smbiosUuid"] == ""
        assert spec["bootOrder"] == []
        assert spec["nics"] == []

    @patch("app.services.deploy_service._find_vm_disks")
    def test_cloud_init_user_data(self, mock_find_disks):
        mock_find_disks.return_value = []
        vm_id = "vm-006"
        vm_data = {
            "id": vm_id,
            "ciGeneratedUserData": "#cloud-config\nusers: ...",
            "ciNetworkConfig": "network:\n  version: 2",
            "nics": [],
        }
        current = self._make_topology(vm_id, vm_data)
        vm = {"name": "cloudy"}

        spec = self._call(vm_id, vm, current)

        assert spec["cloudInit"]["userData"] == "#cloud-config\nusers: ..."
        assert spec["cloudInit"]["networkConfig"] == "network:\n  version: 2"

    @patch("app.services.deploy_service._find_vm_disks")
    def test_cloud_init_falls_back_to_ci_user_data(self, mock_find_disks):
        mock_find_disks.return_value = []
        vm_id = "vm-007"
        vm_data = {
            "id": vm_id,
            "ciUserData": "#cloud-config\nfallback: true",
            "nics": [],
        }
        current = self._make_topology(vm_id, vm_data)
        vm = {"name": "fallback-vm"}

        spec = self._call(vm_id, vm, current)

        assert spec["cloudInit"]["userData"] == "#cloud-config\nfallback: true"

    @patch("app.services.deploy_service._find_vm_disks")
    def test_vm_id_not_in_topology_uses_vm_id_param(self, mock_find_disks):
        """When the vm_id is not found in topology nodes, spec falls back."""
        mock_find_disks.return_value = []
        vm_id = "vm-missing"
        current = {"nodes": [], "edges": []}
        vm = {"name": "ghost"}

        spec = self._call(vm_id, vm, current)

        # vmId falls back to the parameter since no node matched
        assert spec["vmId"] == "vm-missing"
        assert spec["name"] == "ghost"

    @patch("app.services.deploy_service._find_vm_disks")
    def test_multiple_disks(self, mock_find_disks):
        mock_find_disks.return_value = [
            {"node_id": "d1", "size": 20, "format": "qcow2", "source": "blank"},
            {
                "node_id": "d2",
                "size": 50,
                "format": "qcow2",
                "source": "pattern",
                "patternId": "p1",
                "resolvedS3Path": "patterns/p1/d2.qcow2",
                "centralSource": False,
            },
        ]
        vm_id = "vm-008"
        vm_data = {"id": vm_id, "nics": []}
        current = self._make_topology(vm_id, vm_data)
        vm = {"name": "multi-disk"}

        spec = self._call(vm_id, vm, current)

        assert len(spec["disks"]) == 2
        assert spec["disks"][0]["blank"] is True
        assert "patternImage" in spec["disks"][1]
