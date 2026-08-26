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

    @patch("app.services.deploy_topology._find_vm_disks")
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
        assert spec["nics"] == [
            {"id": "nic-1", "mac": "00:11:22:33:44:55", "model": "virtio"}
        ]
        assert spec["bootOrder"] == ["disk-1"]
        assert spec["cloudInit"] == {"userData": "", "networkConfig": ""}

    @patch("app.services.deploy_topology._find_vm_disks")
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

    @patch("app.services.deploy_topology._find_vm_disks")
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

    @patch("app.services.deploy_topology._find_vm_disks")
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

    @patch("app.services.deploy_topology._find_vm_disks")
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

    @patch("app.services.deploy_topology._find_vm_disks")
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

    @patch("app.services.deploy_topology._find_vm_disks")
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

    @patch("app.services.deploy_topology._find_vm_disks")
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

    @patch("app.services.deploy_topology._find_vm_disks")
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


# ---------------------------------------------------------------------------
# _enrich_project_response
# ---------------------------------------------------------------------------
import datetime

import pytest
from fastapi import HTTPException

from app.services.troshkad_client import TroshkadError


class TestEnrichProjectResponse:
    def _call(self, p, hosts_by_id, provs_by_id, owners_by_id):
        from app.api.projects import _enrich_project_response

        return _enrich_project_response(p, hosts_by_id, provs_by_id, owners_by_id)

    def _make_project(self, **overrides):
        now = datetime.datetime.now(datetime.UTC)
        defaults = dict(
            id="proj-1",
            name="test",
            description=None,
            owner_id="owner-1",
            owner_email=None,
            provider_id=None,
            host_type="ec2",
            state="draft",
            public_token=None,
            guest_permission="view",
            auto_stop_minutes=None,
            auto_stop_expires_at=None,
            auto_delete_minutes=None,
            auto_stopped=False,
            lifetime_expires_at=None,
            poweroff_mode="acpi",
            host_id=None,
            host_instance_id=None,
            host_ip=None,
            host_provider_name=None,
            host_provider_type=None,
            topology=None,
            deployed_topology=None,
            deploy_error=None,
            deploy_progress=None,
            tags=None,
            guid=None,
            clock_target=None,
            guest_exec_enabled=True,
            ocp_status=None,
            ocp_status_detail=None,
            ocp_install_elapsed=None,
            deploy_started_at=None,
            created_at=now,
            updated_at=now,
        )
        defaults.update(overrides)
        m = MagicMock(spec=[])
        for k, v in defaults.items():
            setattr(m, k, v)
        return m

    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_basic_with_owner(self, mock_dp):
        owner = MagicMock()
        owner.email = "alice@example.com"
        p = self._make_project()
        resp = self._call(p, {}, {}, {"owner-1": owner})
        assert resp.owner_email == "alice@example.com"

    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_with_host_and_provider(self, mock_dp):
        host = MagicMock()
        host.instance_id = "i-abc123"
        host.ip_address = "10.0.0.1"
        host.provider_id = "prov-1"
        prov = MagicMock()
        prov.name = "AWS East"
        prov.type = "ec2"
        p = self._make_project(host_id="host-1")
        resp = self._call(p, {"host-1": host}, {"prov-1": prov}, {})
        assert resp.host_instance_id == "i-abc123"
        assert resp.host_ip == "10.0.0.1"
        assert resp.host_provider_name == "AWS East"
        assert resp.host_provider_type == "ec2"

    @patch("app.services.deploy_service._get_deploy_progress_data")
    def test_deploying_state_with_live_progress(self, mock_dp):
        mock_dp.return_value = {"step": "downloading", "pct": 42}
        p = self._make_project(state="deploying")
        resp = self._call(p, {}, {}, {})
        assert resp.deploy_progress == {"step": "downloading", "pct": 42}

    @patch("app.core.redis.get_job_info")
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_deploying_state_with_queued_job(self, mock_dp, mock_job):
        mock_job.return_value = {
            "status": "queued",
            "queue_position": 2,
            "queue_length": 5,
        }
        p = self._make_project(state="deploying")
        resp = self._call(p, {}, {}, {})
        assert resp.deploy_progress["step"] == "queued"
        assert resp.deploy_progress["queue_position"] == 2

    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_no_owner_in_dict(self, mock_dp):
        p = self._make_project()
        resp = self._call(p, {}, {}, {})
        assert resp.owner_email is None


# ---------------------------------------------------------------------------
# _recompute_auto_stop_timer
# ---------------------------------------------------------------------------
class TestRecomputeAutoStopTimer:
    def _call(self, project, fields):
        from app.api.projects import _recompute_auto_stop_timer

        return _recompute_auto_stop_timer(project, fields)

    def test_none_clears_fields(self):
        proj = MagicMock()
        proj.auto_stop_started_at = datetime.datetime.now(datetime.UTC)
        proj.auto_stop_expires_at = datetime.datetime.now(datetime.UTC)
        proj.auto_stop_warned = True
        self._call(proj, {"auto_stop_minutes": None})
        assert proj.auto_stop_started_at is None
        assert proj.auto_stop_expires_at is None
        assert proj.auto_stop_warned is False

    def test_active_project_sets_started_at(self):
        proj = MagicMock()
        proj.state = "active"
        proj.auto_stop_started_at = None
        proj.auto_stop_minutes = 60
        self._call(proj, {"auto_stop_minutes": 60})
        assert proj.auto_stop_started_at is not None
        assert proj.auto_stop_expires_at is not None

    def test_non_active_project_no_started_at(self):
        proj = MagicMock()
        proj.state = "draft"
        proj.auto_stop_started_at = None
        proj.auto_stop_minutes = 60
        self._call(proj, {"auto_stop_minutes": 60})
        # started_at not set for non-active
        assert proj.auto_stop_expires_at is None or proj.auto_stop_started_at is None

    def test_existing_started_at_calculates_expires(self):
        started = datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        proj = MagicMock()
        proj.state = "active"
        proj.auto_stop_started_at = started
        proj.auto_stop_minutes = 120
        self._call(proj, {"auto_stop_minutes": 120})
        expected = started + datetime.timedelta(minutes=120)
        assert proj.auto_stop_expires_at == expected

    def test_always_clears_warned(self):
        proj = MagicMock()
        proj.state = "active"
        proj.auto_stop_started_at = datetime.datetime.now(datetime.UTC)
        proj.auto_stop_minutes = 30
        proj.auto_stop_warned = True
        self._call(proj, {"auto_stop_minutes": 30})
        assert proj.auto_stop_warned is False


# ---------------------------------------------------------------------------
# _recompute_auto_delete_timer
# ---------------------------------------------------------------------------
class TestRecomputeAutoDeleteTimer:
    def _call(self, project, fields):
        from app.api.projects import _recompute_auto_delete_timer

        return _recompute_auto_delete_timer(project, fields)

    def test_none_clears_fields(self):
        proj = MagicMock()
        proj.auto_delete_started_at = datetime.datetime.now(datetime.UTC)
        proj.lifetime_expires_at = datetime.datetime.now(datetime.UTC)
        proj.auto_delete_warned = True
        self._call(proj, {"auto_delete_minutes": None})
        assert proj.auto_delete_started_at is None
        assert proj.lifetime_expires_at is None
        assert proj.auto_delete_warned is False

    def test_non_draft_sets_started_at(self):
        proj = MagicMock()
        proj.state = "active"
        proj.auto_delete_started_at = None
        proj.auto_delete_minutes = 1440
        self._call(proj, {"auto_delete_minutes": 1440})
        assert proj.auto_delete_started_at is not None

    def test_draft_does_not_set_started_at(self):
        proj = MagicMock()
        proj.state = "draft"
        proj.auto_delete_started_at = None
        proj.auto_delete_minutes = 1440
        self._call(proj, {"auto_delete_minutes": 1440})
        # For draft state, started_at stays None
        # So lifetime_expires_at also stays unset
        assert proj.auto_delete_started_at is None

    def test_existing_started_at_calculates_expires(self):
        started = datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC)
        proj = MagicMock()
        proj.state = "active"
        proj.auto_delete_started_at = started
        proj.auto_delete_minutes = 60
        self._call(proj, {"auto_delete_minutes": 60})
        expected = started + datetime.timedelta(minutes=60)
        assert proj.lifetime_expires_at == expected

    def test_always_clears_warned(self):
        proj = MagicMock()
        proj.state = "deploying"
        proj.auto_delete_started_at = datetime.datetime.now(datetime.UTC)
        proj.auto_delete_minutes = 30
        proj.auto_delete_warned = True
        self._call(proj, {"auto_delete_minutes": 30})
        assert proj.auto_delete_warned is False


# ---------------------------------------------------------------------------
# _apply_password_mode
# ---------------------------------------------------------------------------
class TestApplyPasswordMode:
    def _call(self, result, mode, custom=""):
        from app.api.projects import _apply_password_mode

        return _apply_password_mode(result, mode, custom)

    def test_current_no_op(self):
        result = {
            "networks": {"net-1": {"bmc_password": "old"}},
            "vms": {"vm-1": {"cloud_user_password": "old"}},
        }
        self._call(result, "current")
        assert result["networks"]["net-1"]["bmc_password"] == "old"
        assert result["vms"]["vm-1"]["cloud_user_password"] == "old"

    def test_none_removes_passwords(self):
        result = {
            "networks": {"net-1": {"bmc_password": "old", "cidr": "10.0.0.0/24"}},
            "vms": {"vm-1": {"cloud_user_password": "old", "name": "test"}},
        }
        self._call(result, "none")
        assert "bmc_password" not in result["networks"]["net-1"]
        assert "cloud_user_password" not in result["vms"]["vm-1"]
        # Non-password keys remain
        assert result["networks"]["net-1"]["cidr"] == "10.0.0.0/24"

    def test_custom_replaces_passwords(self):
        result = {
            "networks": {"net-1": {"bmc_password": "old"}},
            "vms": {"vm-1": {"cloud_user_password": "old"}},
        }
        self._call(result, "custom", "newpass123")
        assert result["networks"]["net-1"]["bmc_password"] == "newpass123"
        assert result["vms"]["vm-1"]["cloud_user_password"] == "newpass123"

    def test_custom_only_replaces_existing_passwords(self):
        result = {
            "networks": {"net-1": {"cidr": "10.0.0.0/24"}},
            "vms": {"vm-1": {"name": "test"}},
        }
        self._call(result, "custom", "newpass")
        # No bmc_password key added where it didn't exist
        assert "bmc_password" not in result["networks"]["net-1"]
        assert "cloud_user_password" not in result["vms"]["vm-1"]

    def test_empty_result_no_error(self):
        result = {}
        self._call(result, "none")
        # Should not raise


# ---------------------------------------------------------------------------
# _build_pull_through_config
# ---------------------------------------------------------------------------
class TestBuildPullThroughConfig:
    def _call(self, registry_url):
        from app.api.projects import _build_pull_through_config

        return _build_pull_through_config(registry_url)

    def test_basic_config(self):
        result = self._call("https://mirror.example.com")
        assert result["enabled"] is True
        assert result["url"] == "https://mirror.example.com"
        assert result["orgs"]["registry.redhat.io"] == "registry_redhat_io"
        assert result["orgs"]["quay.io"] == "quay_io"

    def test_different_url(self):
        result = self._call("https://quay-proxy.internal.corp")
        assert result["url"] == "https://quay-proxy.internal.corp"

    def test_has_exactly_two_orgs(self):
        result = self._call("http://test")
        assert len(result["orgs"]) == 2


# ---------------------------------------------------------------------------
# _enforce_single_bastion_browser
# ---------------------------------------------------------------------------
class TestEnforceSingleBastionBrowser:
    def _call(self, topology):
        from app.api.projects import _enforce_single_bastion_browser

        return _enforce_single_bastion_browser(topology)

    def test_no_bastion_browser_ok(self):
        topology = {
            "nodes": [
                {"type": "vmNode", "data": {"name": "vm1"}},
                {"type": "vmNode", "data": {"name": "vm2"}},
            ]
        }
        self._call(topology)  # Should not raise

    def test_one_bastion_browser_ok(self):
        topology = {
            "nodes": [
                {"type": "vmNode", "data": {"configureBastionBrowser": True}},
                {"type": "vmNode", "data": {"name": "vm2"}},
            ]
        }
        self._call(topology)  # Should not raise

    def test_two_bastion_browsers_raises(self):
        topology = {
            "nodes": [
                {"type": "vmNode", "data": {"configureBastionBrowser": True}},
                {"type": "vmNode", "data": {"configureBastionBrowser": True}},
            ]
        }
        with pytest.raises(HTTPException) as exc_info:
            self._call(topology)
        assert exc_info.value.status_code == 400

    def test_empty_topology_ok(self):
        self._call({})  # No raise

    def test_none_topology_ok(self):
        self._call(None)  # No raise


# ---------------------------------------------------------------------------
# _find_gateway_node
# ---------------------------------------------------------------------------
class TestFindGatewayNode:
    def _call(self, topology):
        from app.api.projects import _find_gateway_node

        return _find_gateway_node(topology)

    def test_found(self):
        gw = {
            "type": "networkNode",
            "data": {"subtype": "gateway", "gatewayMode": "nat-portforward"},
        }
        topology = {"nodes": [{"type": "vmNode", "data": {}}, gw]}
        assert self._call(topology) is gw

    def test_not_found_wrong_mode(self):
        topology = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {"subtype": "gateway", "gatewayMode": "nat-only"},
                }
            ]
        }
        assert self._call(topology) is None

    def test_not_found_wrong_subtype(self):
        topology = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {"subtype": "vxlan", "gatewayMode": "nat-portforward"},
                }
            ]
        }
        assert self._call(topology) is None

    def test_empty_topology(self):
        assert self._call({"nodes": []}) is None

    def test_no_nodes_key(self):
        assert self._call({}) is None


# ---------------------------------------------------------------------------
# _accumulate_disk_info
# ---------------------------------------------------------------------------
class TestAccumulateDiskInfo:
    def _make_result(self):
        return {
            "disk_list": [],
            "any_disk_changed": False,
            "files_to_remove": [],
            "needs_library_download": False,
            "disks_to_create": [],
            "disks_to_resize": [],
        }

    def _call(self, info, result):
        from app.api.projects import _accumulate_disk_info

        return _accumulate_disk_info(info, result)

    def test_basic_accumulation(self):
        result = self._make_result()
        info = {
            "path": "/vms/disk.qcow2",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 20,
            "backing_file": None,
            "image_changed": False,
            "size_grew": False,
            "is_new": False,
            "is_library": False,
        }
        self._call(info, result)
        assert len(result["disk_list"]) == 1
        assert result["disk_list"][0]["path"] == "/vms/disk.qcow2"
        assert result["any_disk_changed"] is False
        assert len(result["disks_to_create"]) == 0

    def test_image_changed_flags(self):
        result = self._make_result()
        info = {
            "path": "/vms/disk.qcow2",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 20,
            "backing_file": "/cache/img.qcow2",
            "image_changed": True,
            "size_grew": False,
            "is_new": False,
            "is_library": False,
        }
        self._call(info, result)
        assert result["any_disk_changed"] is True
        assert "/vms/disk.qcow2" in result["files_to_remove"]

    def test_size_grew_adds_resize(self):
        result = self._make_result()
        info = {
            "path": "/vms/disk.qcow2",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 50,
            "backing_file": None,
            "image_changed": False,
            "size_grew": True,
            "is_new": False,
            "is_library": False,
        }
        self._call(info, result)
        assert result["any_disk_changed"] is True
        assert len(result["disks_to_resize"]) == 1
        assert result["disks_to_resize"][0]["new_size_gb"] == 50
        assert len(result["disks_to_create"]) == 0

    def test_is_library_sets_needs_download(self):
        result = self._make_result()
        info = {
            "path": "/vms/disk.qcow2",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 20,
            "backing_file": "/cache/lib.qcow2",
            "image_changed": False,
            "size_grew": False,
            "is_new": False,
            "is_library": True,
        }
        self._call(info, result)
        assert result["needs_library_download"] is True

    def test_is_new_sets_changed(self):
        result = self._make_result()
        info = {
            "path": "/vms/newdisk.qcow2",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 10,
            "backing_file": None,
            "image_changed": False,
            "size_grew": False,
            "is_new": True,
            "is_library": False,
        }
        self._call(info, result)
        assert result["any_disk_changed"] is True

    def test_size_grew_with_image_changed_no_resize(self):
        """When image_changed AND size_grew, resize is skipped (new image is correct size)."""
        result = self._make_result()
        info = {
            "path": "/vms/disk.qcow2",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 50,
            "backing_file": "/cache/img.qcow2",
            "image_changed": True,
            "size_grew": True,
            "is_new": False,
            "is_library": False,
        }
        self._call(info, result)
        assert result["any_disk_changed"] is True
        assert len(result["disks_to_resize"]) == 0


# ---------------------------------------------------------------------------
# _broadcast_vm_states
# ---------------------------------------------------------------------------
class TestBroadcastVmStates:
    @patch("app.api.projects.notify_project")
    @patch("app.services.troshkad_client.get_all_vm_states")
    @patch("app.services.deploy_topology._vm_domain_name")
    def test_maps_running_and_shut_off(self, mock_dom, mock_states, mock_notify):
        mock_dom.side_effect = lambda pid, nid: f"troshka-{nid}"
        mock_states.return_value = {
            "troshka-vm1": "running",
            "troshka-vm2": "shut_off",
        }
        current = {
            "nodes": [
                {"id": "vm1", "type": "vmNode"},
                {"id": "vm2", "type": "vmNode"},
            ]
        }
        from app.api.projects import _broadcast_vm_states

        _broadcast_vm_states(MagicMock(), "proj-1", current)
        call_args = mock_notify.call_args[0]
        states = call_args[1]["states"]
        assert states["vm1"] == "running"
        assert states["vm2"] == "stopped"

    @patch("app.api.projects.notify_project")
    @patch("app.services.troshkad_client.get_all_vm_states")
    @patch("app.services.deploy_topology._vm_domain_name")
    def test_unknown_passes_through(self, mock_dom, mock_states, mock_notify):
        mock_dom.return_value = "troshka-vm1"
        mock_states.return_value = {"troshka-vm1": "paused"}
        current = {"nodes": [{"id": "vm1", "type": "vmNode"}]}
        from app.api.projects import _broadcast_vm_states

        _broadcast_vm_states(MagicMock(), "proj-1", current)
        states = mock_notify.call_args[0][1]["states"]
        assert states["vm1"] == "paused"

    @patch("app.api.projects.notify_project")
    @patch("app.services.troshkad_client.get_all_vm_states")
    def test_exception_silently_caught(self, mock_states, mock_notify):
        mock_states.side_effect = Exception("connection refused")
        from app.api.projects import _broadcast_vm_states

        # Should not raise
        _broadcast_vm_states(MagicMock(), "proj-1", {"nodes": []})
        mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# _reconfigure_bmc
# ---------------------------------------------------------------------------
class TestReconfigureBmc:
    def _call(self, h, p_id, deployed, bmc_config, errors):
        from app.api.projects import _reconfigure_bmc

        return _reconfigure_bmc(h, p_id, deployed, bmc_config, errors)

    @patch("app.services.deploy_service._setup_bmc_via_troshkad")
    @patch("app.services.deploy_service._teardown_bmc_via_troshkad")
    def test_teardown_only(self, mock_teardown, mock_setup):
        deployed = {
            "nodes": [
                {"type": "networkNode", "data": {"networkType": "bmc"}},
            ]
        }
        errors = []
        self._call(MagicMock(), "proj-1", deployed, None, errors)
        mock_teardown.assert_called_once()
        mock_setup.assert_not_called()
        assert len(errors) == 0

    @patch("app.services.deploy_service._setup_bmc_via_troshkad", return_value=True)
    @patch("app.services.deploy_service._teardown_bmc_via_troshkad")
    def test_setup_only(self, mock_teardown, mock_setup):
        deployed = {"nodes": [{"type": "vmNode", "data": {}}]}
        errors = []
        self._call(MagicMock(), "proj-1", deployed, {"some": "config"}, errors)
        mock_teardown.assert_not_called()
        mock_setup.assert_called_once()
        assert len(errors) == 0

    @patch("app.services.deploy_service._setup_bmc_via_troshkad", return_value=True)
    @patch("app.services.deploy_service._teardown_bmc_via_troshkad")
    def test_teardown_and_setup(self, mock_teardown, mock_setup):
        deployed = {"nodes": [{"type": "networkNode", "data": {"networkType": "bmc"}}]}
        errors = []
        self._call(MagicMock(), "proj-1", deployed, {"bmc": True}, errors)
        mock_teardown.assert_called_once()
        mock_setup.assert_called_once()

    @patch(
        "app.services.deploy_service._setup_bmc_via_troshkad",
        return_value="some error message",
    )
    @patch("app.services.deploy_service._teardown_bmc_via_troshkad")
    def test_setup_failure_appends_error(self, mock_teardown, mock_setup):
        deployed = {"nodes": []}
        errors = []
        self._call(MagicMock(), "proj-1", deployed, {"bmc": True}, errors)
        assert len(errors) == 1
        assert "BMC setup failed" in errors[0]

    @patch(
        "app.services.deploy_service._setup_bmc_via_troshkad",
        side_effect=Exception("boom"),
    )
    @patch("app.services.deploy_service._teardown_bmc_via_troshkad")
    def test_setup_exception_appends_error(self, mock_teardown, mock_setup):
        deployed = {"nodes": []}
        errors = []
        self._call(MagicMock(), "proj-1", deployed, {"bmc": True}, errors)
        assert len(errors) == 1
        assert "BMC setup failed" in errors[0]


# ---------------------------------------------------------------------------
# _finalize_kubevirt_reconfigure
# ---------------------------------------------------------------------------
class TestFinalizeKubevirtReconfigure:
    def _call(self, proj, s, p_id, current, copy_module, notify_fn):
        from app.api.projects import _finalize_kubevirt_reconfigure

        return _finalize_kubevirt_reconfigure(
            proj, s, p_id, current, copy_module, notify_fn
        )

    def test_strips_transient_fields_and_commits(self):
        import copy as real_copy

        proj = MagicMock()
        session = MagicMock()
        notify = MagicMock()
        current = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {
                        "name": "vm1",
                        "resolvedS3Path": "s3://bucket/path",
                        "presignedUrl": "https://presigned",
                        "ciGeneratedUserData": "#cloud-config\n...",
                    },
                },
                {"type": "networkNode", "data": {"cidr": "10.0.0.0/24"}},
            ]
        }
        self._call(proj, session, "proj-1", current, real_copy, notify)

        # deployed_topology and topology should have s3/presigned/ci stripped
        saved_topo = proj.deployed_topology
        for node in saved_topo["nodes"]:
            assert "resolvedS3Path" not in node.get("data", {})
            assert "presignedUrl" not in node.get("data", {})
            assert "ciGeneratedUserData" not in node.get("data", {})
        assert proj.state == "active"
        assert proj.deploy_error is None
        session.commit.assert_called_once()
        notify.assert_called_once_with(
            "proj-1", {"type": "project-state", "state": "active"}
        )

    def test_does_not_mutate_original_topology(self):
        import copy as real_copy

        proj = MagicMock()
        session = MagicMock()
        notify = MagicMock()
        current = {
            "nodes": [
                {"type": "vmNode", "data": {"resolvedS3Path": "keep-me"}},
            ]
        }
        self._call(proj, session, "proj-1", current, real_copy, notify)
        # Original should still have the field
        assert current["nodes"][0]["data"]["resolvedS3Path"] == "keep-me"

    def test_empty_nodes(self):
        import copy as real_copy

        proj = MagicMock()
        session = MagicMock()
        notify = MagicMock()
        self._call(proj, session, "proj-1", {"nodes": []}, real_copy, notify)
        assert proj.state == "active"
        session.commit.assert_called_once()


# ---------------------------------------------------------------------------
# _build_redeploy_vm_data
# ---------------------------------------------------------------------------
class TestBuildRedeployVmData:
    def _call(self, vm_node):
        from app.api.projects import _build_redeploy_vm_data

        return _build_redeploy_vm_data(vm_node)

    def test_basic_extraction(self):
        vm_node = {
            "id": "vm-abc",
            "data": {
                "name": "my-vm",
                "vcpus": 4,
                "ram": 16,
                "cloudInit": True,
                "bootDevices": ["disk-1"],
                "firmware": "uefi",
                "secureBoot": True,
            },
        }
        result = self._call(vm_node)
        assert result["node_id"] == "vm-abc"
        assert result["name"] == "my-vm"
        assert result["vcpus"] == 4
        assert result["ram_gb"] == 16
        assert result["cloud_init"] is True
        assert result["boot_devices"] == ["disk-1"]
        assert result["firmware"] == "uefi"
        assert result["secure_boot"] is True

    def test_defaults(self):
        vm_node = {"id": "vm-min", "data": {}}
        result = self._call(vm_node)
        assert result["node_id"] == "vm-min"
        assert result["name"] == "vm"
        assert result["vcpus"] == 2
        assert result["ram_gb"] == 4
        assert result["cloud_init"] is False
        assert result["boot_devices"] is None
        assert result["firmware"] == "bios"
        assert result["secure_boot"] is False

    def test_missing_data_key(self):
        vm_node = {"id": "vm-no-data"}
        result = self._call(vm_node)
        assert result["node_id"] == "vm-no-data"
        assert result["name"] == "vm"
        assert result["vcpus"] == 2

    def test_partial_data(self):
        vm_node = {"id": "vm-partial", "data": {"name": "custom", "vcpus": 8}}
        result = self._call(vm_node)
        assert result["name"] == "custom"
        assert result["vcpus"] == 8
        assert result["ram_gb"] == 4  # default
        assert result["firmware"] == "bios"  # default


# ---------------------------------------------------------------------------
# _set_redeploy_progress
# ---------------------------------------------------------------------------
class TestSetRedeployProgress:
    def _call(self, dom, data):
        from app.api.projects import _set_redeploy_progress

        return _set_redeploy_progress(dom, data)

    @patch("app.core.redis.set_progress")
    def test_calls_set_progress_with_prefix(self, mock_set):
        self._call("troshka-abc-def", {"step": "downloading", "pct": 50})
        mock_set.assert_called_once_with(
            "redeploy:troshka-abc-def", {"step": "downloading", "pct": 50}
        )

    @patch("app.core.redis.set_progress")
    def test_empty_data_dict(self, mock_set):
        self._call("dom-1", {})
        mock_set.assert_called_once_with("redeploy:dom-1", {})

    @patch("app.core.redis.set_progress")
    def test_complex_data(self, mock_set):
        data = {"step": "creating_vm", "pct": 75, "detail": "disk resize"}
        self._call("dom-xyz", data)
        mock_set.assert_called_once_with("redeploy:dom-xyz", data)

    @patch("app.core.redis.set_progress")
    def test_empty_domain_string(self, mock_set):
        self._call("", {"step": "done"})
        mock_set.assert_called_once_with("redeploy:", {"step": "done"})


# ---------------------------------------------------------------------------
# _get_redeploy_progress
# ---------------------------------------------------------------------------
class TestGetRedeployProgress:
    def _call(self, dom):
        from app.api.projects import _get_redeploy_progress

        return _get_redeploy_progress(dom)

    @patch("app.core.redis.get_progress")
    def test_returns_progress_data(self, mock_get):
        mock_get.return_value = {"step": "downloading", "pct": 42}
        result = self._call("troshka-abc-def")
        assert result == {"step": "downloading", "pct": 42}
        mock_get.assert_called_once_with("redeploy:troshka-abc-def")

    @patch("app.core.redis.get_progress")
    def test_returns_none_when_no_progress(self, mock_get):
        mock_get.return_value = None
        result = self._call("dom-1")
        assert result is None
        mock_get.assert_called_once_with("redeploy:dom-1")

    @patch("app.core.redis.get_progress")
    def test_prefix_applied_correctly(self, mock_get):
        mock_get.return_value = {}
        self._call("my-domain")
        mock_get.assert_called_once_with("redeploy:my-domain")

    @patch("app.core.redis.get_progress")
    def test_empty_domain(self, mock_get):
        mock_get.return_value = {"step": "idle"}
        result = self._call("")
        mock_get.assert_called_once_with("redeploy:")
        assert result == {"step": "idle"}


# ---------------------------------------------------------------------------
# _delete_redeploy_progress
# ---------------------------------------------------------------------------
class TestDeleteRedeployProgress:
    def _call(self, dom):
        from app.api.projects import _delete_redeploy_progress

        return _delete_redeploy_progress(dom)

    @patch("app.core.redis.delete_progress")
    def test_calls_delete_with_prefix(self, mock_del):
        self._call("troshka-abc-def")
        mock_del.assert_called_once_with("redeploy:troshka-abc-def")

    @patch("app.core.redis.delete_progress")
    def test_empty_domain(self, mock_del):
        self._call("")
        mock_del.assert_called_once_with("redeploy:")

    @patch("app.core.redis.delete_progress")
    def test_domain_with_special_chars(self, mock_del):
        self._call("troshka-12345678-abcdef01")
        mock_del.assert_called_once_with("redeploy:troshka-12345678-abcdef01")


# ---------------------------------------------------------------------------
# _check_library_items_ready
# ---------------------------------------------------------------------------
class TestCheckLibraryItemsReady:
    def _call(self, topology, db):
        from app.api.projects import _check_library_items_ready

        return _check_library_items_ready(topology, db)

    def test_no_storage_nodes_passes(self):
        topology = {
            "nodes": [
                {"type": "vmNode", "data": {"name": "vm1"}},
                {"type": "networkNode", "data": {"cidr": "10.0.0.0/24"}},
            ]
        }
        db = MagicMock()
        self._call(topology, db)  # Should not raise
        db.query.assert_not_called()

    def test_storage_node_without_library_id_passes(self):
        topology = {
            "nodes": [
                {"type": "storageNode", "data": {"name": "blank-disk", "sizeGb": 20}},
            ]
        }
        db = MagicMock()
        self._call(topology, db)  # Should not raise
        db.query.assert_not_called()

    def test_library_item_not_found_raises(self):
        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {"name": "rhel-disk", "libraryItemId": "lib-missing"},
                },
            ]
        }
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        with pytest.raises(HTTPException) as exc_info:
            self._call(topology, db)
        assert exc_info.value.status_code == 400
        assert "not found" in exc_info.value.detail

    def test_library_item_not_ready_raises(self):
        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {"name": "rhel-disk", "libraryItemId": "lib-1"},
                },
            ]
        }
        lib_item = MagicMock()
        lib_item.state = "uploading"
        lib_item.name = "RHEL 9.4"
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = lib_item
        with pytest.raises(HTTPException) as exc_info:
            self._call(topology, db)
        assert exc_info.value.status_code == 400
        assert "uploading" in exc_info.value.detail

    def test_library_item_ready_passes(self):
        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {"name": "rhel-disk", "libraryItemId": "lib-1"},
                },
            ]
        }
        lib_item = MagicMock()
        lib_item.state = "ready"
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = lib_item
        self._call(topology, db)  # Should not raise

    def test_multiple_storage_nodes_all_ready(self):
        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {"name": "disk-a", "libraryItemId": "lib-a"},
                },
                {
                    "type": "storageNode",
                    "data": {"name": "disk-b", "libraryItemId": "lib-b"},
                },
            ]
        }
        lib_a = MagicMock()
        lib_a.state = "ready"
        lib_b = MagicMock()
        lib_b.state = "ready"
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [
            lib_a,
            lib_b,
        ]
        self._call(topology, db)  # Should not raise

    def test_second_library_item_not_ready_raises(self):
        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {"name": "disk-a", "libraryItemId": "lib-a"},
                },
                {
                    "type": "storageNode",
                    "data": {"name": "disk-b", "libraryItemId": "lib-b"},
                },
            ]
        }
        lib_a = MagicMock()
        lib_a.state = "ready"
        lib_b = MagicMock()
        lib_b.state = "error"
        lib_b.name = "Windows 11"
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [
            lib_a,
            lib_b,
        ]
        with pytest.raises(HTTPException) as exc_info:
            self._call(topology, db)
        assert exc_info.value.status_code == 400
        assert "error" in exc_info.value.detail

    def test_empty_topology_passes(self):
        self._call({"nodes": []}, MagicMock())  # Should not raise


# ---------------------------------------------------------------------------
# _domain_name
# ---------------------------------------------------------------------------
class TestDomainName:
    def _call(self, project_id, vm_id):
        from app.api.projects import _domain_name

        return _domain_name(project_id, vm_id)

    @patch("app.services.deploy_topology._vm_domain_name")
    def test_delegates_to_deploy_service(self, mock_dom):
        mock_dom.return_value = "troshka-abcdef01-12345678"
        result = self._call("proj-abcdef01", "vm-12345678")
        assert result == "troshka-abcdef01-12345678"
        mock_dom.assert_called_once_with("proj-abcdef01", "vm-12345678")

    @patch("app.services.deploy_topology._vm_domain_name")
    def test_passes_through_return_value(self, mock_dom):
        mock_dom.return_value = "troshka-aaa-bbb"
        assert self._call("aaa", "bbb") == "troshka-aaa-bbb"

    @patch("app.services.deploy_topology._vm_domain_name")
    def test_empty_ids(self, mock_dom):
        mock_dom.return_value = "troshka--"
        result = self._call("", "")
        mock_dom.assert_called_once_with("", "")
        assert result == "troshka--"


# ---------------------------------------------------------------------------
# _resolve_vm_ssh_params
# ---------------------------------------------------------------------------
class TestResolveVmSshParams:
    def _call(self, project, vm_id):
        from app.api.projects import _resolve_vm_ssh_params

        return _resolve_vm_ssh_params(project, vm_id)

    def test_vm_not_found_raises_404(self):
        project = MagicMock()
        project.topology = {"nodes": [{"id": "vm-1", "data": {}}]}
        with pytest.raises(HTTPException) as exc_info:
            self._call(project, "vm-nonexistent")
        assert exc_info.value.status_code == 404
        assert "vm-nonexistent" in exc_info.value.detail

    def test_returns_vm_node_and_ip(self):
        vm_node = {
            "id": "vm-abc",
            "data": {
                "nics": [
                    {"id": "nic-1", "ip": "10.0.0.5"},
                    {"id": "nic-2", "ip": "10.0.1.5"},
                ],
                "ciCloudUserPassword": "secret123",
            },
        }
        project = MagicMock()
        project.topology = {"nodes": [vm_node]}
        result_node, result_ip, result_password = self._call(project, "vm-abc")
        assert result_node is vm_node
        assert result_ip == "10.0.0.5"  # First NIC with IP
        assert result_password == "secret123"

    def test_no_nics_returns_empty_ip(self):
        vm_node = {"id": "vm-no-nic", "data": {}}
        project = MagicMock()
        project.topology = {"nodes": [vm_node]}
        result_node, result_ip, result_password = self._call(project, "vm-no-nic")
        assert result_node is vm_node
        assert result_ip == ""
        assert result_password == ""

    def test_nics_without_ip_returns_empty(self):
        vm_node = {
            "id": "vm-nics",
            "data": {
                "nics": [
                    {"id": "nic-1"},
                    {"id": "nic-2", "ip": ""},
                ],
            },
        }
        project = MagicMock()
        project.topology = {"nodes": [vm_node]}
        _, result_ip, _ = self._call(project, "vm-nics")
        assert result_ip == ""

    def test_none_topology_raises_404(self):
        project = MagicMock()
        project.topology = None
        with pytest.raises(HTTPException) as exc_info:
            self._call(project, "vm-1")
        assert exc_info.value.status_code == 404

    def test_empty_topology_raises_404(self):
        project = MagicMock()
        project.topology = {}
        with pytest.raises(HTTPException) as exc_info:
            self._call(project, "vm-1")
        assert exc_info.value.status_code == 404

    def test_no_password_returns_empty_string(self):
        vm_node = {
            "id": "vm-nopw",
            "data": {"nics": [{"id": "nic-1", "ip": "10.0.0.2"}]},
        }
        project = MagicMock()
        project.topology = {"nodes": [vm_node]}
        _, _, result_password = self._call(project, "vm-nopw")
        assert result_password == ""


# ---------------------------------------------------------------------------
# _resolve_provider_type
# ---------------------------------------------------------------------------
class TestResolveProviderType:
    def _call(self, project):
        from app.api.projects import _resolve_provider_type

        return _resolve_provider_type(project)

    def test_no_host_id_returns_none(self):
        project = MagicMock()
        project.host_id = None
        assert self._call(project) is None

    def test_no_session_returns_none(self):
        # Use a plain object so object.__getattribute__ works predictably
        class FakeProject:
            pass

        proj = FakeProject()
        proj.host_id = "host-1"
        sa_state = MagicMock()
        sa_state.session = None
        proj._sa_instance_state = sa_state
        assert self._call(proj) is None

    def test_host_not_found_returns_none(self):
        class FakeProject:
            pass

        proj = FakeProject()
        proj.host_id = "host-1"
        mock_session = MagicMock()
        mock_session.get.return_value = None
        sa_state = MagicMock()
        sa_state.session = mock_session
        proj._sa_instance_state = sa_state
        assert self._call(proj) is None

    def test_host_no_provider_id_returns_none(self):
        class FakeProject:
            pass

        proj = FakeProject()
        proj.host_id = "host-1"
        mock_host = MagicMock()
        mock_host.provider_id = None
        mock_session = MagicMock()
        mock_session.get.return_value = mock_host
        sa_state = MagicMock()
        sa_state.session = mock_session
        proj._sa_instance_state = sa_state
        assert self._call(proj) is None

    def test_provider_found_returns_type(self):
        class FakeProject:
            pass

        proj = FakeProject()
        proj.host_id = "host-1"
        mock_host = MagicMock()
        mock_host.provider_id = "prov-1"
        mock_provider = MagicMock()
        mock_provider.type = "ec2"
        mock_session = MagicMock()

        def fake_get(model, id_val):
            from app.models.host import Host
            from app.models.provider import Provider

            if model is Host:
                return mock_host
            if model is Provider:
                return mock_provider
            return None

        mock_session.get.side_effect = fake_get
        sa_state = MagicMock()
        sa_state.session = mock_session
        proj._sa_instance_state = sa_state
        assert self._call(proj) == "ec2"

    def test_provider_not_found_returns_none(self):
        class FakeProject:
            pass

        proj = FakeProject()
        proj.host_id = "host-1"
        mock_host = MagicMock()
        mock_host.provider_id = "prov-missing"
        mock_session = MagicMock()

        def fake_get(model, id_val):
            from app.models.host import Host

            if model is Host:
                return mock_host
            return None  # Provider not found

        mock_session.get.side_effect = fake_get
        sa_state = MagicMock()
        sa_state.session = mock_session
        proj._sa_instance_state = sa_state
        assert self._call(proj) is None


# ---------------------------------------------------------------------------
# _build_destroy_context (additional coverage)
# ---------------------------------------------------------------------------
class TestBuildDestroyContextExtended:
    def _call(self, project):
        from app.api.projects import _build_destroy_context

        return _build_destroy_context(project)

    def test_includes_dns_fields(self):
        project = MagicMock()
        project.id = "proj-dns"
        project.host_id = "host-dns"
        project.vni_map = {}
        project.deployed_topology = {"nodes": []}
        project.topology = None
        project.dns_provider_id = "dns-prov-1"
        project.domain = "lab.dns.example.com"
        ctx = self._call(project)
        assert ctx["dns_provider_id"] == "dns-prov-1"
        assert ctx["domain"] == "lab.dns.example.com"

    def test_vni_map_none_defaults_to_empty_dict(self):
        project = MagicMock()
        project.id = "proj-vni"
        project.host_id = None
        project.vni_map = None
        project.deployed_topology = None
        project.topology = None
        project.dns_provider_id = None
        project.domain = None
        ctx = self._call(project)
        assert ctx["vni_map"] == {}

    def test_deployed_topology_preferred_over_topology(self):
        project = MagicMock()
        project.id = "proj-pref"
        project.host_id = "h"
        project.vni_map = {}
        project.deployed_topology = {"nodes": [{"id": "deployed-node"}]}
        project.topology = {"nodes": [{"id": "draft-node"}]}
        project.dns_provider_id = None
        project.domain = None
        ctx = self._call(project)
        assert ctx["topology"]["nodes"][0]["id"] == "deployed-node"

    def test_nested_vni_map_deep_copied(self):
        original_vni = {"net-a": 100, "net-b": 200}
        project = MagicMock()
        project.id = "proj-deep"
        project.host_id = "h"
        project.vni_map = original_vni
        project.deployed_topology = {"nodes": []}
        project.topology = None
        project.dns_provider_id = None
        project.domain = None
        ctx = self._call(project)
        ctx["vni_map"]["net-c"] = 300
        assert "net-c" not in original_vni


# ---------------------------------------------------------------------------
# _find_changed_kubevirt_vms
# ---------------------------------------------------------------------------
class TestFindChangedKubevirtVms:
    def _call(self, current, deployed):
        from app.api.projects import _find_changed_kubevirt_vms

        return _find_changed_kubevirt_vms(current, deployed)

    def test_no_changes(self):
        topo = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {"name": "vm1", "vcpus": 4}},
            ]
        }
        # Same data in both
        result = self._call(topo, topo)
        assert result == []

    def test_data_changed(self):
        current = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {"vcpus": 8}},
            ]
        }
        deployed = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {"vcpus": 4}},
            ]
        }
        result = self._call(current, deployed)
        assert result == ["vm-1"]

    def test_new_vm_not_in_result(self):
        """VMs that only exist in current (added) are NOT returned."""
        current = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {"vcpus": 4}},
                {"id": "vm-2", "type": "vmNode", "data": {"vcpus": 2}},
            ]
        }
        deployed = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {"vcpus": 4}},
            ]
        }
        result = self._call(current, deployed)
        assert "vm-2" not in result

    def test_non_vm_nodes_ignored(self):
        current = {
            "nodes": [
                {"id": "net-1", "type": "networkNode", "data": {"cidr": "10.0.0.0/24"}},
            ]
        }
        deployed = {
            "nodes": [
                {"id": "net-1", "type": "networkNode", "data": {"cidr": "10.0.1.0/24"}},
            ]
        }
        result = self._call(current, deployed)
        assert result == []

    def test_empty_topologies(self):
        result = self._call({}, {})
        assert result == []

    def test_multiple_changed_vms(self):
        current = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {"vcpus": 8}},
                {"id": "vm-2", "type": "vmNode", "data": {"ram": 32}},
                {"id": "vm-3", "type": "vmNode", "data": {"name": "same"}},
            ]
        }
        deployed = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {"vcpus": 4}},
                {"id": "vm-2", "type": "vmNode", "data": {"ram": 16}},
                {"id": "vm-3", "type": "vmNode", "data": {"name": "same"}},
            ]
        }
        result = self._call(current, deployed)
        assert sorted(result) == ["vm-1", "vm-2"]


# ---------------------------------------------------------------------------
# _get_deployed_disk_info
# ---------------------------------------------------------------------------
class TestGetDeployedDiskInfo:
    def _call(self, vm_node_id, deployed):
        from app.api.projects import _get_deployed_disk_info

        return _get_deployed_disk_info(vm_node_id, deployed)

    @patch("app.api.projects._find_vm_disks")
    def test_vm_not_in_deployed(self, mock_find):
        deployed = {"nodes": [{"id": "other-vm"}]}
        libs, sizes = self._call("vm-missing", deployed)
        assert libs == {}
        assert sizes == {}
        mock_find.assert_not_called()

    @patch("app.api.projects._find_vm_disks")
    def test_vm_found_with_disks(self, mock_find):
        mock_find.return_value = [
            {"node_id": "d1", "library_item_id": "lib-a", "size_gb": 20},
            {"node_id": "d2", "size_gb": 50},
        ]
        deployed = {"nodes": [{"id": "vm-1"}]}
        libs, sizes = self._call("vm-1", deployed)
        assert libs == {"d1": "lib-a", "d2": None}
        assert sizes == {"d1": 20, "d2": 50}

    @patch("app.api.projects._find_vm_disks")
    def test_empty_deployed(self, mock_find):
        libs, sizes = self._call("vm-1", {"nodes": []})
        assert libs == {}
        assert sizes == {}


# ---------------------------------------------------------------------------
# _resolve_disk_backing
# ---------------------------------------------------------------------------
class TestResolveDiskBacking:
    def _call(self, d, pool=None):
        from app.api.projects import _resolve_disk_backing

        return _resolve_disk_backing(d, pool)

    @patch("app.services.deploy_topology._image_cache_path")
    def test_library_source(self, mock_cache_path):
        mock_cache_path.return_value = "/var/lib/troshka/images/lib-1.qcow2"
        d = {"source": "library", "library_item_id": "lib-1", "format": "qcow2"}
        backing, is_lib = self._call(d)
        assert backing == "/var/lib/troshka/images/lib-1.qcow2"
        assert is_lib is True
        mock_cache_path.assert_called_once_with("lib-1", "qcow2", pool=None)

    def test_pattern_source(self):
        d = {
            "source": "pattern",
            "patternId": "pat-abc",
            "patternDiskId": "disk-123",
            "format": "qcow2",
        }
        backing, is_lib = self._call(d)
        assert backing == "/var/lib/troshka/cache/patterns/pat-abc/disk-123.qcow2"
        assert is_lib is False

    def test_blank_source(self):
        d = {"source": "blank"}
        backing, is_lib = self._call(d)
        assert backing is None
        assert is_lib is False

    def test_library_without_id(self):
        d = {"source": "library", "library_item_id": None}
        backing, is_lib = self._call(d)
        assert backing is None
        assert is_lib is False

    def test_pattern_without_id(self):
        d = {"source": "pattern", "patternId": None}
        backing, is_lib = self._call(d)
        assert backing is None
        assert is_lib is False


# ---------------------------------------------------------------------------
# _classify_single_disk
# ---------------------------------------------------------------------------
class TestClassifySingleDisk:
    def _call(self, d, p_id, vm_node_id, dep_disk_libs, dep_disk_sizes, pool=None):
        from app.api.projects import _classify_single_disk

        return _classify_single_disk(
            d, p_id, vm_node_id, dep_disk_libs, dep_disk_sizes, pool
        )

    @patch("app.api.projects._resolve_disk_backing", return_value=(None, False))
    @patch("app.api.projects._disk_path", return_value="/vms/p/vm/d1.qcow2")
    def test_new_disk(self, mock_path, mock_backing):
        d = {
            "node_id": "d1",
            "size_gb": 20,
            "format": "qcow2",
            "bus": "virtio",
        }
        info = self._call(d, "proj-1", "vm-1", {}, {})
        assert info["is_new"] is True
        assert info["image_changed"] is False
        assert info["size_grew"] is False
        assert info["path"] == "/vms/p/vm/d1.qcow2"

    @patch("app.api.projects._resolve_disk_backing", return_value=(None, False))
    @patch("app.api.projects._disk_path", return_value="/vms/p/vm/d1.qcow2")
    def test_image_changed(self, mock_path, mock_backing):
        d = {
            "node_id": "d1",
            "size_gb": 20,
            "format": "qcow2",
            "bus": "virtio",
            "library_item_id": "lib-new",
        }
        dep_libs = {"d1": "lib-old"}
        dep_sizes = {"d1": 20}
        info = self._call(d, "proj-1", "vm-1", dep_libs, dep_sizes)
        # image_changed is a truthy value (not necessarily boolean True)
        assert info["image_changed"]
        assert info["is_new"] is False

    @patch("app.api.projects._resolve_disk_backing", return_value=(None, False))
    @patch("app.api.projects._disk_path", return_value="/vms/p/vm/d1.qcow2")
    def test_size_grew(self, mock_path, mock_backing):
        d = {
            "node_id": "d1",
            "size_gb": 50,
            "format": "qcow2",
            "bus": "virtio",
        }
        dep_libs = {"d1": None}
        dep_sizes = {"d1": 20}
        info = self._call(d, "proj-1", "vm-1", dep_libs, dep_sizes)
        assert info["size_grew"] is True
        assert info["image_changed"] is False

    @patch("app.api.projects._resolve_disk_backing", return_value=(None, False))
    @patch("app.api.projects._disk_path", return_value="/vms/p/vm/d1.qcow2")
    def test_no_change(self, mock_path, mock_backing):
        d = {
            "node_id": "d1",
            "size_gb": 20,
            "format": "qcow2",
            "bus": "virtio",
        }
        dep_libs = {"d1": None}
        dep_sizes = {"d1": 20}
        info = self._call(d, "proj-1", "vm-1", dep_libs, dep_sizes)
        assert info["image_changed"] is False
        assert info["size_grew"] is False
        assert info["is_new"] is False


# ---------------------------------------------------------------------------
# _get_project_and_host
# ---------------------------------------------------------------------------
class TestGetProjectAndHost:
    def _call(self, project_id, user, db, check_disk=False):
        from app.api.projects import _get_project_and_host

        return _get_project_and_host(project_id, user, db, check_disk)

    def test_project_not_found(self):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        user = MagicMock()
        with pytest.raises(HTTPException) as exc_info:
            self._call("proj-1", user, db)
        assert exc_info.value.status_code == 404

    def test_access_denied_not_owner(self):
        project = MagicMock()
        project.owner_id = "other-user"
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        user = MagicMock()
        user.id = "my-user"
        user.role = "user"
        with pytest.raises(HTTPException) as exc_info:
            self._call("proj-1", user, db)
        assert exc_info.value.status_code == 403

    def test_admin_bypasses_ownership(self):
        project = MagicMock()
        project.owner_id = "other-user"
        project.state = "active"
        project.host_id = "host-1"
        host = MagicMock()
        host.host_type = "ec2"
        host.private_key = "key"
        host.ip_address = "10.0.0.1"
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]
        user = MagicMock()
        user.id = "admin-user"
        user.role = "admin"
        result_project, result_host = self._call("proj-1", user, db)
        assert result_project is project
        assert result_host is host

    def test_wrong_state_raises_409(self):
        project = MagicMock()
        project.owner_id = "user-1"
        project.state = "deploying"
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        user = MagicMock()
        user.id = "user-1"
        with pytest.raises(HTTPException) as exc_info:
            self._call("proj-1", user, db)
        assert exc_info.value.status_code == 409
        assert "deploying" in exc_info.value.detail

    def test_host_not_found_raises_503(self):
        project = MagicMock()
        project.owner_id = "user-1"
        project.state = "active"
        project.host_id = "host-1"
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [project, None]
        user = MagicMock()
        user.id = "user-1"
        with pytest.raises(HTTPException) as exc_info:
            self._call("proj-1", user, db)
        assert exc_info.value.status_code == 503

    def test_kubevirt_cluster_skips_key_check(self):
        """KubeVirt cluster hosts don't need private_key or ip_address."""
        project = MagicMock()
        project.owner_id = "user-1"
        project.state = "active"
        project.host_id = "host-1"
        host = MagicMock()
        host.host_type = "kubevirt-cluster"
        host.private_key = None
        host.ip_address = None
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]
        user = MagicMock()
        user.id = "user-1"
        result_project, result_host = self._call("proj-1", user, db)
        assert result_host is host

    def test_ec2_host_no_key_raises_503(self):
        """EC2 hosts without private_key raise 503."""
        project = MagicMock()
        project.owner_id = "user-1"
        project.state = "active"
        project.host_id = "host-1"
        host = MagicMock()
        host.host_type = "ec2"
        host.private_key = None
        host.ip_address = "10.0.0.1"
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]
        user = MagicMock()
        user.id = "user-1"
        with pytest.raises(HTTPException) as exc_info:
            self._call("proj-1", user, db)
        assert exc_info.value.status_code == 503

    @patch("app.services.troshkad_client.check_disk_usage")
    def test_check_disk_high_usage_raises_507(self, mock_disk):
        mock_disk.return_value = {"used_pct": 95, "free_bytes": 2 * 1024**3}
        project = MagicMock()
        project.owner_id = "user-1"
        project.state = "active"
        project.host_id = "host-1"
        host = MagicMock()
        host.host_type = "ec2"
        host.private_key = "key"
        host.ip_address = "10.0.0.1"
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]
        user = MagicMock()
        user.id = "user-1"
        with pytest.raises(HTTPException) as exc_info:
            self._call("proj-1", user, db, check_disk=True)
        assert exc_info.value.status_code == 507


# ---------------------------------------------------------------------------
# _project_response_dict
# ---------------------------------------------------------------------------
class TestProjectResponseDict:
    def _call(self, project):
        from app.api.projects import _project_response_dict

        return _project_response_dict(project)

    @patch("app.services.ws_pubsub.get_cached_vm_states", return_value=None)
    @patch("app.api.projects._resolve_provider_type", return_value=None)
    @patch("app.api.projects._resolve_deploy_progress", return_value=None)
    def test_basic_fields(self, mock_dp, mock_prov, mock_states):
        project = MagicMock()
        project.id = "proj-1"
        project.name = "test"
        project.description = "desc"
        project.owner_id = "owner-1"
        project.provider_id = None
        project.host_type = "ec2"
        project.host_id = None
        project.guid = "abc"
        project.state = "draft"
        project.public_token = None
        project.guest_permission = "view"
        project.topology = None
        project.deployed_topology = None
        project.vni_map = None
        project.deploy_error = None
        project.ocp_status = None
        project.ocp_install_elapsed = None
        project.tags = None
        project.auto_stop_minutes = None
        project.auto_stop_expires_at = None
        project.auto_delete_minutes = None
        project.auto_stopped = False
        project.lifetime_expires_at = None
        project.poweroff_mode = "acpi"
        project.clock_target = None
        project.guest_exec_enabled = True
        project.created_at = "2025-01-01"
        project.updated_at = "2025-01-01"
        result = self._call(project)
        assert result["id"] == "proj-1"
        assert result["name"] == "test"
        assert result["state"] == "draft"
        assert "deploy_progress" not in result
        assert "bmc" not in result
        assert "provider_type" not in result

    @patch("app.services.ws_pubsub.get_cached_vm_states")
    @patch("app.api.projects._resolve_provider_type", return_value="ec2")
    @patch("app.api.projects._resolve_deploy_progress")
    def test_with_bmc_and_provider(self, mock_dp, mock_prov, mock_states):
        mock_dp.return_value = {"step": "downloading"}
        mock_states.return_value = {"vm1": "running"}
        project = MagicMock()
        project.id = "proj-2"
        project.name = "t"
        project.description = None
        project.owner_id = "o"
        project.provider_id = "prov-1"
        project.host_type = "ec2"
        project.host_id = "h-1"
        project.guid = None
        project.state = "deploying"
        project.public_token = None
        project.guest_permission = "view"
        project.topology = None
        project.deployed_topology = {"bmc": {"vms": {}}}
        project.vni_map = None
        project.deploy_error = None
        project.ocp_status = None
        project.ocp_install_elapsed = None
        project.tags = None
        project.auto_stop_minutes = None
        project.auto_stop_expires_at = None
        project.auto_delete_minutes = None
        project.auto_stopped = False
        project.lifetime_expires_at = None
        project.poweroff_mode = "acpi"
        project.clock_target = None
        project.guest_exec_enabled = True
        project.created_at = "2025-01-01"
        project.updated_at = "2025-01-01"
        result = self._call(project)
        assert result["deploy_progress"] == {"step": "downloading"}
        assert result["bmc"] == {"vms": {}}
        assert result["provider_type"] == "ec2"
        assert result["vm_states"] == {"vm1": "running"}

    @patch("app.services.ws_pubsub.get_cached_vm_states", return_value=None)
    @patch("app.api.projects._resolve_provider_type", return_value=None)
    @patch("app.api.projects._resolve_deploy_progress", return_value=None)
    def test_datetime_serialization(self, mock_dp, mock_prov, mock_states):
        project = MagicMock()
        project.id = "p"
        project.name = "t"
        project.description = None
        project.owner_id = "o"
        project.provider_id = None
        project.host_type = "ec2"
        project.host_id = None
        project.guid = None
        project.state = "active"
        project.public_token = None
        project.guest_permission = "view"
        project.topology = None
        project.deployed_topology = {}
        project.vni_map = None
        project.deploy_error = None
        project.ocp_status = None
        project.ocp_install_elapsed = None
        project.tags = None
        project.auto_stop_minutes = 60
        project.auto_stop_expires_at = datetime.datetime(
            2025, 6, 1, tzinfo=datetime.UTC
        )
        project.auto_delete_minutes = None
        project.auto_stopped = False
        project.lifetime_expires_at = None
        project.poweroff_mode = "acpi"
        project.clock_target = datetime.datetime(2025, 1, 15, tzinfo=datetime.UTC)
        project.guest_exec_enabled = True
        project.created_at = "2025-01-01"
        project.updated_at = "2025-01-01"
        result = self._call(project)
        assert result["auto_stop_expires_at"] == "2025-06-01T00:00:00+00:00"
        assert result["clock_target"] == "2025-01-15T00:00:00+00:00"
        assert result["lifetime_expires_at"] is None


# ---------------------------------------------------------------------------
# _cleanup_old_vm_files
# ---------------------------------------------------------------------------
class TestCleanupOldVmFiles:
    def _call(self, h, p_id, target_vm_id, topology):
        from app.api.projects import _cleanup_old_vm_files

        return _cleanup_old_vm_files(h, p_id, target_vm_id, topology)

    @patch("app.api.projects._seed_path", return_value="/vms/seed.iso")
    @patch("app.api.projects._disk_path", return_value="/vms/disk.qcow2")
    @patch("app.api.projects._find_vm_disks")
    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="job-1")
    def test_removes_disks_and_seed(
        self, mock_start, mock_wait, mock_find, mock_disk_path, mock_seed
    ):
        mock_find.return_value = [
            {"node_id": "d1", "format": "qcow2"},
            {"node_id": "d2", "format": "iso"},  # ISOs are skipped
        ]
        host = MagicMock()
        self._call(host, "proj-1", "vm-1", {"nodes": []})
        mock_start.assert_called_once()
        # Should have disk path + seed path, but not the ISO
        call_args = mock_start.call_args[0]
        paths = call_args[2]["paths"]
        assert "/vms/disk.qcow2" in paths
        assert "/vms/seed.iso" in paths
        assert len(paths) == 2  # 1 disk (not iso) + 1 seed

    @patch("app.api.projects._seed_path", return_value="/vms/seed.iso")
    @patch("app.api.projects._find_vm_disks", return_value=[])
    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="job-1")
    def test_no_disks_only_seed(self, mock_start, mock_wait, mock_find, mock_seed):
        host = MagicMock()
        self._call(host, "proj-1", "vm-1", {})
        call_args = mock_start.call_args[0]
        paths = call_args[2]["paths"]
        assert paths == ["/vms/seed.iso"]

    @patch("app.api.projects._seed_path", return_value="/vms/seed.iso")
    @patch("app.api.projects._find_vm_disks", return_value=[])
    @patch("app.api.projects.start_job", side_effect=TroshkadError("fail"))
    def test_troshkad_error_logged_not_raised(self, mock_start, mock_find, mock_seed):
        """TroshkadError during cleanup is logged but does not raise."""
        host = MagicMock()
        # Should not raise
        self._call(host, "proj-1", "vm-1", {})


# ---------------------------------------------------------------------------
# _detect_disk_changes
# ---------------------------------------------------------------------------
class TestDetectDiskChanges:
    def _call(self, p_id, vm_node_id, vm_disks, deployed, pool=None):
        from app.api.projects import _detect_disk_changes

        return _detect_disk_changes(p_id, vm_node_id, vm_disks, deployed, pool)

    @patch("app.api.projects._find_vm_disks", return_value=[])
    def test_no_disks_returns_empty(self, mock_find):
        result = self._call("p", "vm-1", [], {"nodes": []})
        assert result["disk_list"] == []
        assert result["any_disk_changed"] is False
        assert result["needs_library_download"] is False

    @patch("app.api.projects._classify_single_disk")
    @patch("app.api.projects._find_vm_disks", return_value=[])
    @patch("app.services.deploy_topology._image_cache_path")
    def test_iso_disk_adds_to_cdrom(
        self, mock_cache, mock_find_deployed, mock_classify
    ):
        mock_cache.return_value = "/cache/iso.iso"
        vm_disks = [
            {
                "node_id": "d1",
                "format": "iso",
                "library_item_id": "lib-iso",
                "bus": "sata",
            },
        ]
        result = self._call("p", "vm-1", vm_disks, {"nodes": []})
        assert "/cache/iso.iso" in result["cdrom_list"]
        assert len(result["disk_list"]) == 0
        mock_classify.assert_not_called()

    @patch("app.api.projects._accumulate_disk_info")
    @patch("app.api.projects._classify_single_disk")
    @patch("app.api.projects._find_vm_disks", return_value=[])
    def test_normal_disk_classifies_and_accumulates(
        self, mock_find_deployed, mock_classify, mock_accum
    ):
        mock_classify.return_value = {
            "path": "/vms/d1.qcow2",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 20,
            "backing_file": None,
            "image_changed": False,
            "size_grew": False,
            "is_new": True,
            "is_library": False,
        }
        vm_disks = [
            {"node_id": "d1", "format": "qcow2", "bus": "virtio", "size_gb": 20},
        ]
        self._call("p", "vm-1", vm_disks, {"nodes": []})
        mock_classify.assert_called_once()
        mock_accum.assert_called_once()


# ---------------------------------------------------------------------------
# Endpoint tests — cover deploy, stop, start, force-stop, extend-timer,
# vm-states, vm start/stop/forcestop/restart/console/ready/status
# These exercise the big uncovered blocks at lines 963-1249 and 1372-1846.
# ---------------------------------------------------------------------------
from contextlib import contextmanager

from fastapi.testclient import TestClient


def _mock_user(role="user", user_id="user-1"):
    u = MagicMock()
    u.id = user_id
    u.role = role
    u.email = "test@example.com"
    return u


def _ep_project(**overrides):
    defaults = dict(
        id="proj-1",
        name="test",
        owner_id="user-1",
        state="draft",
        topology=None,
        deployed_topology=None,
        host_id=None,
        host_assignments=None,
        vni_map=None,
        deploy_error=None,
        deploy_started_at=None,
        deploy_progress=None,
        auto_stop_expires_at=None,
        auto_stop_warned=False,
        lifetime_expires_at=None,
        auto_delete_warned=False,
        provider_id=None,
        host_type="ec2",
        guid=None,
        public_token=None,
        guest_permission="view",
        ocp_status=None,
        ocp_install_elapsed=None,
        tags=None,
        auto_stop_minutes=None,
        auto_delete_minutes=None,
        auto_stopped=False,
        poweroff_mode="acpi",
        clock_target=None,
        guest_exec_enabled=True,
        created_at=datetime.datetime.now(datetime.UTC),
        updated_at=datetime.datetime.now(datetime.UTC),
        description=None,
        dns_provider_id=None,
        domain=None,
    )
    defaults.update(overrides)
    m = MagicMock()
    for k, v in defaults.items():
        setattr(m, k, v)
    return m


def _ep_host(**overrides):
    defaults = dict(
        id="host-1",
        ip_address="10.0.0.1",
        private_key="key",
        agent_token="token",
        host_type="ec2",
        state="active",
        agent_status="connected",
        console_domain="console.example.com",
        provider_id="prov-1",
        instance_id="i-abc",
        private_ip="10.0.0.2",
    )
    defaults.update(overrides)
    m = MagicMock()
    for k, v in defaults.items():
        setattr(m, k, v)
    return m


@contextmanager
def _override_deps(user=None, db=None):
    """Temporarily override FastAPI deps for get_current_user and get_db."""
    from app.core.auth import get_current_user
    from app.core.database import get_db
    from app.main import app

    if user is None:
        user = _mock_user()
    if db is None:
        db = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# extend_timer endpoint — lines 963-986
# ---------------------------------------------------------------------------
class TestExtendTimerEndpoint:
    """Tests for POST /projects/{id}/extend-timer."""

    def test_extend_auto_stop(self):
        now = datetime.datetime.now(datetime.UTC)
        project = _ep_project(auto_stop_expires_at=now, auto_stop_warned=True)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with patch(
            "app.api.projects._project_response_dict", return_value={"id": "proj-1"}
        ):
            with _override_deps(db=db) as client:
                resp = client.post(
                    "/api/v1/projects/proj-1/extend-timer",
                    json={"timer": "auto_stop", "add_minutes": 30},
                )
        assert resp.status_code == 200
        assert project.auto_stop_warned is False

    def test_extend_auto_stop_not_active_returns_400(self):
        project = _ep_project(auto_stop_expires_at=None)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.post(
                "/api/v1/projects/proj-1/extend-timer",
                json={"timer": "auto_stop", "add_minutes": 30},
            )
        assert resp.status_code == 400

    def test_extend_auto_delete(self):
        now = datetime.datetime.now(datetime.UTC)
        project = _ep_project(lifetime_expires_at=now, auto_delete_warned=True)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with patch(
            "app.api.projects._project_response_dict", return_value={"id": "proj-1"}
        ):
            with _override_deps(db=db) as client:
                resp = client.post(
                    "/api/v1/projects/proj-1/extend-timer",
                    json={"timer": "auto_delete", "add_minutes": 60},
                )
        assert resp.status_code == 200
        assert project.auto_delete_warned is False

    def test_extend_auto_delete_not_active_returns_400(self):
        project = _ep_project(lifetime_expires_at=None)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.post(
                "/api/v1/projects/proj-1/extend-timer",
                json={"timer": "auto_delete", "add_minutes": 60},
            )
        assert resp.status_code == 400

    def test_extend_invalid_timer_returns_400(self):
        project = _ep_project()
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.post(
                "/api/v1/projects/proj-1/extend-timer",
                json={"timer": "invalid", "add_minutes": 30},
            )
        assert resp.status_code == 400

    def test_extend_timer_project_not_found(self):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        with _override_deps(db=db) as client:
            resp = client.post(
                "/api/v1/projects/proj-1/extend-timer",
                json={"timer": "auto_stop", "add_minutes": 30},
            )
        assert resp.status_code == 404

    def test_extend_timer_access_denied(self):
        project = _ep_project(owner_id="owner-1")
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        user = _mock_user(user_id="other-user")
        with _override_deps(user=user, db=db) as client:
            resp = client.post(
                "/api/v1/projects/proj-1/extend-timer",
                json={"timer": "auto_stop", "add_minutes": 30},
            )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# deploy_project endpoint — lines 1000-1130
# ---------------------------------------------------------------------------
class TestDeployProjectEndpoint:
    """Tests for POST /projects/{id}/deploy."""

    def test_deploy_project_not_found(self):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        with _override_deps(db=db) as client:
            resp = client.post("/api/v1/projects/proj-1/deploy")
        assert resp.status_code == 404

    def test_deploy_wrong_state(self):
        project = _ep_project(state="active")
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.post("/api/v1/projects/proj-1/deploy")
        assert resp.status_code == 409

    def test_deploy_no_topology(self):
        project = _ep_project(state="draft", topology=None)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.post("/api/v1/projects/proj-1/deploy")
        assert resp.status_code == 400

    @patch("app.api.projects._check_library_items_ready")
    @patch(
        "app.api.projects.calculate_project_requirements", return_value={"vm_count": 0}
    )
    @patch("app.services.deploy_topology.validate_topology_ips", return_value=[])
    @patch("app.services.deploy_topology.validate_topology_names", return_value=[])
    def test_deploy_no_vms(self, mock_vn, mock_vi, mock_reqs, mock_lib):
        topo = {"nodes": [], "edges": []}
        project = _ep_project(state="draft", topology=topo)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.post("/api/v1/projects/proj-1/deploy")
        assert resp.status_code == 400
        assert "no VMs" in resp.json()["detail"]

    @patch("app.api.projects._check_library_items_ready")
    @patch(
        "app.api.projects.calculate_project_requirements", return_value={"vm_count": 1}
    )
    @patch("app.services.deploy_topology.validate_topology_ips", return_value=[])
    @patch(
        "app.services.deploy_topology.validate_topology_names",
        return_value=["dup name"],
    )
    def test_deploy_topology_errors(self, mock_vn, mock_vi, mock_reqs, mock_lib):
        topo = {"nodes": [{"type": "vmNode", "data": {}}], "edges": []}
        project = _ep_project(state="draft", topology=topo)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.post("/api/v1/projects/proj-1/deploy")
        assert resp.status_code == 400
        assert "Topology has errors" in resp.json()["detail"]

    @patch("app.api.projects._check_library_items_ready")
    @patch(
        "app.api.projects.calculate_project_requirements", return_value={"vm_count": 1}
    )
    @patch("app.services.deploy_topology.validate_topology_ips", return_value=[])
    @patch("app.services.deploy_topology.validate_topology_names", return_value=[])
    def test_deploy_non_admin_cannot_select_pool(
        self, mock_vn, mock_vi, mock_reqs, mock_lib
    ):
        topo = {"nodes": [{"type": "vmNode", "data": {}}], "edges": []}
        project = _ep_project(state="draft", topology=topo)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(user=_mock_user(role="user"), db=db) as client:
            resp = client.post("/api/v1/projects/proj-1/deploy?storage_pool_id=pool-1")
        assert resp.status_code == 403

    @patch("app.core.redis.enqueue_job")
    @patch("app.services.troshkad_client.check_disk_usage", return_value=None)
    @patch("app.api.projects.place_project")
    @patch("app.api.projects._check_library_items_ready")
    @patch(
        "app.api.projects.calculate_project_requirements", return_value={"vm_count": 1}
    )
    @patch("app.services.deploy_topology.validate_topology_ips", return_value=[])
    @patch("app.services.deploy_topology.validate_topology_names", return_value=[])
    def test_deploy_success(
        self, mock_vn, mock_vi, mock_reqs, mock_lib, mock_place, mock_disk, mock_enqueue
    ):
        topo = {"nodes": [{"type": "vmNode", "data": {}}], "edges": []}
        project = _ep_project(state="draft", topology=topo)
        host = _ep_host()
        mock_place.return_value = {
            "host_id": "host-1",
            "host_ip": "10.0.0.1",
            "requirements": {"vm_count": 1},
            "vni_map": {"net-1": 100},
        }
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [project, host]
        with _override_deps(db=db) as client:
            resp = client.post("/api/v1/projects/proj-1/deploy")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deploying"

    @patch(
        "app.api.projects.place_project", return_value={"error": "No hosts available"}
    )
    @patch("app.api.projects._check_library_items_ready")
    @patch(
        "app.api.projects.calculate_project_requirements", return_value={"vm_count": 1}
    )
    @patch("app.services.deploy_topology.validate_topology_ips", return_value=[])
    @patch("app.services.deploy_topology.validate_topology_names", return_value=[])
    def test_deploy_placement_error(
        self, mock_vn, mock_vi, mock_reqs, mock_lib, mock_place
    ):
        topo = {"nodes": [{"type": "vmNode", "data": {}}], "edges": []}
        project = _ep_project(state="draft", topology=topo)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.post("/api/v1/projects/proj-1/deploy")
        assert resp.status_code == 503

    @patch("app.api.projects._check_library_items_ready")
    @patch(
        "app.api.projects.calculate_project_requirements", return_value={"vm_count": 1}
    )
    @patch("app.services.deploy_topology.validate_topology_ips", return_value=[])
    @patch("app.services.deploy_topology.validate_topology_names", return_value=[])
    def test_deploy_bmc_no_connected_vms(self, mock_vn, mock_vi, mock_reqs, mock_lib):
        bmc_net = {
            "id": "bmc-net",
            "type": "networkNode",
            "data": {"networkType": "bmc"},
        }
        topo = {"nodes": [{"type": "vmNode", "data": {}}, bmc_net], "edges": []}
        project = _ep_project(state="draft", topology=topo)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.post("/api/v1/projects/proj-1/deploy")
        assert resp.status_code == 400
        assert "BMC network" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# stop_project endpoint — lines 1139-1159
# ---------------------------------------------------------------------------
class TestStopProjectEndpoint:
    """Tests for POST /projects/{id}/stop."""

    @patch("app.api.projects.notify_project")
    @patch("app.core.redis.enqueue_job")
    def test_stop_success(self, mock_enqueue, mock_notify):
        project = _ep_project(state="active")
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.post("/api/v1/projects/proj-1/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopping"
        assert project.state == "stopping"

    def test_stop_wrong_state(self):
        project = _ep_project(state="draft")
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.post("/api/v1/projects/proj-1/stop")
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# start_project endpoint — lines 1222-1249
# ---------------------------------------------------------------------------
class TestStartProjectEndpoint:
    """Tests for POST /projects/{id}/start."""

    @patch("app.api.projects.notify_project")
    @patch("app.core.redis.enqueue_job")
    def test_start_success(self, mock_enqueue, mock_notify):
        project = _ep_project(state="stopped")
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.post("/api/v1/projects/proj-1/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "starting"
        assert project.state == "starting"

    @patch("app.api.projects.notify_project")
    @patch("app.core.redis.enqueue_job")
    def test_start_from_error(self, mock_enqueue, mock_notify):
        project = _ep_project(state="error")
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.post("/api/v1/projects/proj-1/start")
        assert resp.status_code == 200

    def test_start_wrong_state(self):
        project = _ep_project(state="deploying")
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.post("/api/v1/projects/proj-1/start")
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# get_all_vm_states endpoint — lines 1372-1390
# ---------------------------------------------------------------------------
class TestGetAllVmStatesEndpoint:
    """Tests for GET /projects/{id}/vm-states."""

    def test_no_host_returns_empty(self):
        project = _ep_project(state="active", host_id=None)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.get("/api/v1/projects/proj-1/vm-states")
        assert resp.status_code == 200
        assert resp.json() == {"states": {}}

    @patch("app.services.ws_pubsub.get_cached_vm_states")
    def test_cached_states_returned(self, mock_cached):
        project = _ep_project(state="active", host_id="host-1")
        mock_cached.return_value = {
            "states": {"vm-1": "running"},
            "container_states": {},
        }
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.get("/api/v1/projects/proj-1/vm-states")
        assert resp.status_code == 200
        assert resp.json()["states"]["vm-1"] == "running"

    @patch("app.services.ws_pubsub.get_cached_vm_states", return_value=None)
    def test_no_cached_states_returns_empty(self, mock_cached):
        project = _ep_project(state="active", host_id="host-1")
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.get("/api/v1/projects/proj-1/vm-states")
        assert resp.status_code == 200
        assert resp.json()["states"] == {}


# ---------------------------------------------------------------------------
# get_deploy_status endpoint — lines 782-790
# ---------------------------------------------------------------------------
class TestGetDeployStatusEndpoint:
    """Tests for GET /projects/{id}/deploy-status."""

    @patch("app.services.deploy_service.get_deploy_progress")
    def test_deploy_progress_success(self, mock_progress):
        project = _ep_project(state="deploying")
        mock_progress.return_value = {"step": "downloading", "pct": 42}
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.get("/api/v1/projects/proj-1/deploy-progress")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "deploying"
        assert data["progress"]["pct"] == 42

    def test_deploy_progress_not_found(self):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        with _override_deps(db=db) as client:
            resp = client.get("/api/v1/projects/proj-1/deploy-progress")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# list_kubeconfigs / get_kubeconfig — lines 793-868
# ---------------------------------------------------------------------------
class TestKubeconfigEndpoints:
    """Tests for kubeconfig listing and download."""

    def test_list_kubeconfigs(self):
        topo = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"label": "bastion", "ocpKubeconfig": "kc-data"},
                },
                {"id": "vm-2", "type": "vmNode", "data": {"label": "worker"}},
                {"id": "net-1", "type": "networkNode", "data": {}},
            ]
        }
        project = _ep_project(state="active", deployed_topology=topo)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.get("/api/v1/projects/proj-1/kubeconfigs")
        assert resp.status_code == 200
        configs = resp.json()
        assert len(configs) == 1
        assert configs[0]["vm_name"] == "bastion"

    def test_get_kubeconfig_download(self):
        topo = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"label": "sno", "ocpKubeconfig": "apiVersion: v1"},
                },
            ]
        }
        project = _ep_project(state="active", deployed_topology=topo)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.get("/api/v1/projects/proj-1/kubeconfig?vm=sno")
        assert resp.status_code == 200
        assert resp.text == "apiVersion: v1"
        assert "kubeconfig-sno.yaml" in resp.headers.get("content-disposition", "")

    def test_get_kubeconfig_not_found(self):
        topo = {"nodes": [{"id": "vm-1", "type": "vmNode", "data": {}}]}
        project = _ep_project(state="active", deployed_topology=topo)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = project
        with _override_deps(db=db) as client:
            resp = client.get("/api/v1/projects/proj-1/kubeconfig")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# force_stop_project — lines 1162-1219
# ---------------------------------------------------------------------------
class TestForceStopProjectEndpoint:
    """Tests for POST /projects/{id}/force-stop."""

    @patch("app.api.projects.notify_project")
    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="job-1")
    @patch(
        "app.services.deploy_topology._vm_domain_name", return_value="troshka-abc-def"
    )
    def test_force_stop_troshkad_host(
        self, mock_dom, mock_start, mock_wait, mock_notify
    ):
        host = _ep_host()
        topo = {"nodes": [{"id": "vm-1", "type": "vmNode", "data": {}}]}
        project = _ep_project(state="active", host_id="host-1", deployed_topology=topo)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [project, host]
        with _override_deps(db=db) as client:
            resp = client.post("/api/v1/projects/proj-1/force-stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"
        assert project.state == "stopped"

    def test_force_stop_no_host(self):
        project = _ep_project(state="active", host_id="host-1")
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [project, None]
        with _override_deps(db=db) as client:
            resp = client.post("/api/v1/projects/proj-1/force-stop")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# stop_vm, get_vm_status, forcestop_vm, restart_vm, get_vm_console, vm_ready
# — lines 1580-1846
# ---------------------------------------------------------------------------
class TestVmOperationEndpoints:
    """Tests for individual VM operation endpoints."""

    @patch("app.api.projects.notify_project")
    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="job-1")
    @patch("app.api.projects._domain_name", return_value="troshka-p-vm")
    @patch("app.api.projects._get_project_and_host")
    def test_stop_vm_success(
        self, mock_gph, mock_dom, mock_start, mock_wait, mock_notify
    ):
        project = _ep_project(state="active", host_id="host-1")
        host = _ep_host()
        mock_gph.return_value = (project, host)
        with _override_deps() as client:
            resp = client.post("/api/v1/projects/proj-1/vms/vm-1/stop")
        assert resp.status_code == 200
        assert resp.json()["action"] == "stop"
        assert resp.json()["success"] is True

    @patch("app.api.projects.start_job", side_effect=TroshkadError("fail"))
    @patch("app.api.projects._domain_name", return_value="troshka-p-vm")
    @patch("app.api.projects._get_project_and_host")
    def test_stop_vm_troshkad_error(self, mock_gph, mock_dom, mock_start):
        project = _ep_project(state="active", host_id="host-1")
        host = _ep_host()
        mock_gph.return_value = (project, host)
        with _override_deps() as client:
            resp = client.post("/api/v1/projects/proj-1/vms/vm-1/stop")
        assert resp.status_code == 200
        assert resp.json()["success"] is False

    @patch("app.api.projects.troshkad_get_vm_state")
    @patch("app.api.projects._domain_name", return_value="troshka-p-vm")
    @patch("app.api.projects._get_project_and_host")
    def test_get_vm_status(self, mock_gph, mock_dom, mock_state):
        project = _ep_project(state="active", host_id="host-1")
        host = _ep_host()
        mock_gph.return_value = (project, host)
        mock_state.return_value = {"state": "running", "boot_devs": ["hd"]}
        with _override_deps() as client:
            resp = client.get("/api/v1/projects/proj-1/vms/vm-1/status")
        assert resp.status_code == 200
        assert resp.json()["state"] == "running"

    @patch("app.services.ws_pubsub.get_cached_vm_states")
    @patch("app.api.projects._get_project_and_host")
    def test_get_vm_status_kubevirt(self, mock_gph, mock_cached):
        project = _ep_project(state="active", host_id="host-1")
        host = _ep_host(host_type="kubevirt-cluster")
        mock_gph.return_value = (project, host)
        mock_cached.return_value = {"states": {"vm-1": "running"}}
        with _override_deps() as client:
            resp = client.get("/api/v1/projects/proj-1/vms/vm-1/status")
        assert resp.status_code == 200
        assert resp.json()["state"] == "running"

    @patch("app.api.projects.notify_project")
    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="job-1")
    @patch("app.api.projects._domain_name", return_value="troshka-p-vm")
    @patch("app.api.projects._get_project_and_host")
    def test_forcestop_vm_success(
        self, mock_gph, mock_dom, mock_start, mock_wait, mock_notify
    ):
        project = _ep_project(state="active", host_id="host-1")
        host = _ep_host()
        mock_gph.return_value = (project, host)
        with _override_deps() as client:
            resp = client.post("/api/v1/projects/proj-1/vms/vm-1/forcestop")
        assert resp.status_code == 200
        assert resp.json()["action"] == "forcestop"
        assert resp.json()["success"] is True

    @patch("app.api.projects.start_job", side_effect=TroshkadError("fail"))
    @patch("app.api.projects._domain_name", return_value="troshka-p-vm")
    @patch("app.api.projects._get_project_and_host")
    def test_forcestop_vm_troshkad_error(self, mock_gph, mock_dom, mock_start):
        project = _ep_project(state="active", host_id="host-1")
        host = _ep_host()
        mock_gph.return_value = (project, host)
        with _override_deps() as client:
            resp = client.post("/api/v1/projects/proj-1/vms/vm-1/forcestop")
        assert resp.status_code == 200
        assert resp.json()["success"] is False

    @patch("app.api.projects.notify_project")
    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="job-1")
    @patch("app.api.projects._domain_name", return_value="troshka-p-vm")
    @patch("app.api.projects._get_project_and_host")
    def test_restart_vm_success(
        self, mock_gph, mock_dom, mock_start, mock_wait, mock_notify
    ):
        project = _ep_project(state="active", host_id="host-1")
        host = _ep_host()
        mock_gph.return_value = (project, host)
        with _override_deps() as client:
            resp = client.post("/api/v1/projects/proj-1/vms/vm-1/restart")
        assert resp.status_code == 200
        assert resp.json()["action"] == "restart"
        assert resp.json()["success"] is True

    @patch("app.api.projects.start_job", side_effect=TroshkadError("fail"))
    @patch("app.api.projects._domain_name", return_value="troshka-p-vm")
    @patch("app.api.projects._get_project_and_host")
    def test_restart_vm_troshkad_error(self, mock_gph, mock_dom, mock_start):
        project = _ep_project(state="active", host_id="host-1")
        host = _ep_host()
        mock_gph.return_value = (project, host)
        with _override_deps() as client:
            resp = client.post("/api/v1/projects/proj-1/vms/vm-1/restart")
        assert resp.status_code == 200
        assert resp.json()["success"] is False

    @patch("app.services.console_dns.sign_console_jwt", return_value="test-jwt")
    @patch("app.api.projects.troshkad_get_vnc_port", return_value=5900)
    @patch("app.api.projects._domain_name", return_value="troshka-p-vm")
    @patch("app.api.projects._get_project_and_host")
    def test_get_vm_console(self, mock_gph, mock_dom, mock_vnc, mock_jwt):
        project = _ep_project(state="active", host_id="host-1")
        host = _ep_host()
        mock_gph.return_value = (project, host)
        with _override_deps() as client:
            resp = client.get("/api/v1/projects/proj-1/vms/vm-1/console")
        assert resp.status_code == 200
        assert resp.json()["ws_url"] == "wss://console.example.com/ws/test-jwt"

    @patch("app.api.projects.troshkad_get_vnc_port", return_value=None)
    @patch("app.api.projects._domain_name", return_value="troshka-p-vm")
    @patch("app.api.projects._get_project_and_host")
    def test_get_vm_console_no_vnc(self, mock_gph, mock_dom, mock_vnc):
        project = _ep_project(state="active", host_id="host-1")
        host = _ep_host()
        mock_gph.return_value = (project, host)
        with _override_deps() as client:
            resp = client.get("/api/v1/projects/proj-1/vms/vm-1/console")
        assert resp.status_code == 200
        assert "error" in resp.json()

    @patch("app.api.projects.troshkad_get_vnc_port", return_value=5900)
    @patch("app.api.projects._domain_name", return_value="troshka-p-vm")
    @patch("app.api.projects._get_project_and_host")
    def test_get_vm_console_no_console_domain(self, mock_gph, mock_dom, mock_vnc):
        project = _ep_project(state="active", host_id="host-1")
        host = _ep_host(console_domain=None)
        mock_gph.return_value = (project, host)
        with _override_deps() as client:
            resp = client.get("/api/v1/projects/proj-1/vms/vm-1/console")
        assert resp.status_code == 200
        assert "error" in resp.json()

    @patch(
        "app.api.projects.wait_for_job",
        return_value={"status": "completed", "result": {"output": "ok"}},
    )
    @patch("app.api.projects.start_job", return_value="job-1")
    @patch("app.api.projects._get_project_and_host")
    def test_vm_ready_success(self, mock_gph, mock_start, mock_wait):
        topo = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "nics": [{"id": "nic-1", "ip": "10.0.0.5"}],
                        "ciCloudUserPassword": "secret",
                    },
                }
            ]
        }
        project = _ep_project(state="active", host_id="host-1", topology=topo)
        host = _ep_host()
        mock_gph.return_value = (project, host)
        with _override_deps() as client:
            resp = client.get("/api/v1/projects/proj-1/vms/vm-1/ready")
        assert resp.status_code == 200

    @patch("app.api.projects._get_project_and_host")
    def test_vm_ready_wrong_state(self, mock_gph):
        topo = {"nodes": [{"id": "vm-1", "type": "vmNode", "data": {}}]}
        project = _ep_project(state="reconfiguring", host_id="host-1", topology=topo)
        host = _ep_host()
        mock_gph.return_value = (project, host)
        with _override_deps() as client:
            resp = client.get("/api/v1/projects/proj-1/vms/vm-1/ready")
        assert resp.status_code == 200
        assert resp.json()["ready"] is False

    @patch("app.api.projects._get_project_and_host")
    def test_vm_ready_no_ip(self, mock_gph):
        topo = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"nics": [], "ciCloudUserPassword": "pw"},
                }
            ]
        }
        project = _ep_project(state="active", host_id="host-1", topology=topo)
        host = _ep_host()
        mock_gph.return_value = (project, host)
        with _override_deps() as client:
            resp = client.get("/api/v1/projects/proj-1/vms/vm-1/ready")
        assert resp.status_code == 200
        assert resp.json()["ready"] is False
        assert resp.json()["reason"] == "no IP"

    @patch("app.api.projects._get_project_and_host")
    def test_vm_ready_no_password(self, mock_gph):
        topo = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"nics": [{"id": "nic-1", "ip": "10.0.0.5"}]},
                }
            ]
        }
        project = _ep_project(state="active", host_id="host-1", topology=topo)
        host = _ep_host()
        mock_gph.return_value = (project, host)
        with _override_deps() as client:
            resp = client.get("/api/v1/projects/proj-1/vms/vm-1/ready")
        assert resp.status_code == 200
        assert resp.json()["ready"] is False
        assert resp.json()["reason"] == "no password"

    @patch("app.api.projects._get_project_and_host")
    def test_vm_ready_vm_not_found(self, mock_gph):
        topo = {"nodes": [{"id": "vm-2", "type": "vmNode", "data": {}}]}
        project = _ep_project(state="active", host_id="host-1", topology=topo)
        host = _ep_host()
        mock_gph.return_value = (project, host)
        with _override_deps() as client:
            resp = client.get("/api/v1/projects/proj-1/vms/vm-1/ready")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
# _build_storage_mode_kwargs  (hosts.py helper)
# ═══════════════════════════════════════════════════════════════════════


class TestBuildStorageModeKwargs:
    """Tests for _build_storage_mode_kwargs in app/api/hosts.py."""

    def _call(self, host, session, nfs_kwargs):
        from app.api.hosts import _build_storage_mode_kwargs

        return _build_storage_mode_kwargs(host, session, nfs_kwargs)

    def test_local_mode_when_no_nfs_server(self):
        h = MagicMock()
        h.storage_pool_id = None
        h.ip_address = "10.0.0.1"
        s = MagicMock()
        mode, ca, cert, key = self._call(h, s, {})
        assert mode == "local"
        assert ca == ""
        assert cert == ""
        assert key == ""

    def test_local_mode_when_nfs_server_empty(self):
        h = MagicMock()
        h.storage_pool_id = None
        h.ip_address = "10.0.0.1"
        s = MagicMock()
        mode, ca, cert, key = self._call(h, s, {"nfs_server": ""})
        assert mode == "local"

    def test_shared_mode_no_pool_id(self):
        """Shared mode but no storage_pool_id -> no TLS certs."""
        h = MagicMock()
        h.storage_pool_id = None
        h.ip_address = "10.0.0.1"
        s = MagicMock()
        mode, ca, cert, key = self._call(h, s, {"nfs_server": "nfs.example.com"})
        assert mode == "shared"
        assert ca == ""
        assert cert == ""
        assert key == ""

    def test_shared_mode_no_ip(self):
        """Shared mode with pool but no ip_address -> no TLS certs."""
        h = MagicMock()
        h.storage_pool_id = "pool-1"
        h.ip_address = ""
        s = MagicMock()
        mode, ca, cert, key = self._call(h, s, {"nfs_server": "nfs.example.com"})
        assert mode == "shared"
        assert ca == ""
        assert cert == ""
        assert key == ""

    @patch("app.services.storage_pool_service.sign_host_cert", autospec=True)
    def test_shared_mode_with_pool_and_certs(self, mock_sign):
        """Shared mode with pool that has CA -> signs host cert."""
        mock_sign.return_value = ("cert-data", "key-data")

        pool = MagicMock()
        pool.ca_cert = "ca-cert-pem"
        pool.ca_key = "ca-key-pem"

        h = MagicMock()
        h.storage_pool_id = "pool-1"
        h.ip_address = "10.0.0.1"
        h.private_ip = "172.16.0.1"

        s = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = pool

        mode, ca, cert, key = self._call(h, s, {"nfs_server": "nfs.example.com"})
        assert mode == "shared"
        assert ca == "ca-cert-pem"
        assert cert == "cert-data"
        assert key == "key-data"
        mock_sign.assert_called_once_with(
            "ca-cert-pem", "ca-key-pem", "10.0.0.1", "172.16.0.1"
        )

    def test_shared_mode_pool_missing_ca(self):
        """Pool exists but has no CA cert/key -> no TLS certs."""
        pool = MagicMock()
        pool.ca_cert = ""
        pool.ca_key = ""

        h = MagicMock()
        h.storage_pool_id = "pool-1"
        h.ip_address = "10.0.0.1"
        h.private_ip = "172.16.0.1"

        s = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = pool

        mode, ca, cert, key = self._call(h, s, {"nfs_server": "nfs.example.com"})
        assert mode == "shared"
        assert ca == ""
        assert cert == ""
        assert key == ""

    def test_shared_mode_pool_not_found(self):
        """Pool ID set but pool not in DB -> no TLS certs."""
        h = MagicMock()
        h.storage_pool_id = "pool-gone"
        h.ip_address = "10.0.0.1"

        s = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = None

        mode, ca, cert, key = self._call(h, s, {"nfs_server": "nfs.example.com"})
        assert mode == "shared"
        assert ca == ""
        assert cert == ""
        assert key == ""


# ═══════════════════════════════════════════════════════════════════════
# _update_console_dns_for_new_ip  (hosts.py helper)
# ═══════════════════════════════════════════════════════════════════════


class TestUpdateConsoleDnsForNewIp:
    """Tests for _update_console_dns_for_new_ip in app/api/hosts.py."""

    def _call(self, h, s, old_ip, new_ip):
        from app.api.hosts import _update_console_dns_for_new_ip

        return _update_console_dns_for_new_ip(h, s, old_ip, new_ip)

    def test_returns_early_no_console_domain(self):
        """No console_domain -> skip DNS update."""
        h = MagicMock()
        h.console_domain = ""
        s = MagicMock()
        self._call(h, s, "1.2.3.4", "5.6.7.8")
        s.query.assert_not_called()

    def test_returns_early_same_ip(self):
        """Same old/new IP -> skip DNS update."""
        h = MagicMock()
        h.console_domain = "host.console.example.com"
        s = MagicMock()
        self._call(h, s, "1.2.3.4", "1.2.3.4")
        s.query.assert_not_called()

    def test_returns_early_empty_new_ip(self):
        """Empty new_ip -> skip DNS update."""
        h = MagicMock()
        h.console_domain = "host.console.example.com"
        s = MagicMock()
        self._call(h, s, "1.2.3.4", "")
        s.query.assert_not_called()

    @patch("app.services.providers.get_provider_driver")
    def test_updates_dns_when_ip_changes(self, mock_get_driver):
        """New IP + console_domain -> creates DNS record."""
        mock_driver = MagicMock()
        mock_get_driver.return_value = mock_driver

        provider = MagicMock()
        h = MagicMock()
        h.console_domain = "host.console.example.com"
        h.provider_id = "prov-1"
        h.id = "host-1234-5678"

        s = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = provider

        self._call(h, s, "1.2.3.4", "5.6.7.8")
        mock_driver.create_console_record.assert_called_once_with(
            provider, h, "host.console.example.com", "5.6.7.8"
        )

    @patch("app.services.providers.get_provider_driver")
    def test_handles_driver_exception(self, mock_get_driver):
        """Exception in driver -> logs warning, does not raise."""
        mock_get_driver.side_effect = Exception("DNS fail")

        provider = MagicMock()
        h = MagicMock()
        h.console_domain = "host.console.example.com"
        h.provider_id = "prov-1"
        h.id = "host-1234-5678"

        s = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = provider

        # Should not raise
        self._call(h, s, "1.2.3.4", "5.6.7.8")

    def test_no_provider_found(self):
        """Provider not in DB -> no crash, no driver call."""
        h = MagicMock()
        h.console_domain = "host.console.example.com"
        h.provider_id = "prov-gone"
        h.id = "host-1234-5678"

        s = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = None

        # Should not raise
        self._call(h, s, "1.2.3.4", "5.6.7.8")

    @patch("app.services.providers.get_provider_driver")
    def test_updates_dns_old_ip_none(self, mock_get_driver):
        """old_ip is None (first assignment) -> updates DNS."""
        mock_driver = MagicMock()
        mock_get_driver.return_value = mock_driver

        provider = MagicMock()
        h = MagicMock()
        h.console_domain = "host.console.example.com"
        h.provider_id = "prov-1"
        h.id = "host-1234-5678"

        s = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = provider

        self._call(h, s, None, "5.6.7.8")
        mock_driver.create_console_record.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# Provisioning validation in add_host (hosts.py)
# ═══════════════════════════════════════════════════════════════════════


class TestProvisionHostValidation:
    """Tests for validation logic in the add_host (provision) endpoint."""

    def _call(self, body_dict, provider=None, pool=None):
        """Call the add_host endpoint via direct function invocation with mocks."""
        from app.api.hosts import ProvisionRequest, add_host

        body = ProvisionRequest(**body_dict)
        user = MagicMock()
        user.role = "admin"
        db = MagicMock()

        # Mock provider query
        db.query.return_value.filter_by.return_value.first.return_value = provider
        # Mock db.get for pool
        db.get.return_value = pool

        if provider:
            provider.get_credentials = MagicMock()

        try:
            return add_host(body=body, user=user, db=db)
        except HTTPException as e:
            return e

    def test_provider_not_found(self):
        result = self._call({"provider_id": "prov-missing"})
        assert isinstance(result, HTTPException)
        assert result.status_code == 404
        assert "Provider not found" in result.detail

    def test_provider_not_active(self):
        prov = MagicMock()
        prov.state = "provisioning"
        prov.type = "ec2"
        result = self._call({"provider_id": "prov-1"}, provider=prov)
        assert isinstance(result, HTTPException)
        assert result.status_code == 400
        assert "not active" in result.detail

    def test_ec2_no_image(self):
        prov = MagicMock()
        prov.state = "active"
        prov.type = "ec2"
        prov.default_image = ""
        prov.vpc_id = "vpc-123"
        prov.subnet_id = "subnet-123"
        result = self._call({"provider_id": "prov-1"}, provider=prov)
        assert isinstance(result, HTTPException)
        assert result.status_code == 400
        assert "image" in result.detail.lower()

    def test_ec2_no_vpc(self):
        prov = MagicMock()
        prov.state = "active"
        prov.type = "ec2"
        prov.default_image = "ami-123"
        prov.vpc_id = ""
        prov.subnet_id = ""
        result = self._call({"provider_id": "prov-1"}, provider=prov)
        assert isinstance(result, HTTPException)
        assert result.status_code == 400
        assert "VPC" in result.detail


# ═══════════════════════════════════════════════════════════════════════
# _apply_pool_nfs_config (hosts.py)
# ═══════════════════════════════════════════════════════════════════════


class TestApplyPoolNfsConfig:
    """Tests for _apply_pool_nfs_config in app/api/hosts.py."""

    def _call(self, cfg, pool):
        from app.api.hosts import _apply_pool_nfs_config

        return _apply_pool_nfs_config(cfg, pool)

    def _make_cfg(self):
        from app.services.agent_deployer import AgentDeployConfig

        return AgentDeployConfig()

    def test_fsx_pool(self):
        pool = MagicMock()
        pool.mode = "shared-fsx"
        pool.fsx_dns_name = "fsx.us-east-1.amazonaws.com"
        cfg = self._make_cfg()
        self._call(cfg, pool)
        assert cfg.nfs_server == "fsx.us-east-1.amazonaws.com"
        assert cfg.nfs_path == "/fsx"

    def test_byo_pool_with_path_and_port(self):
        pool = MagicMock()
        pool.mode = "shared-byo"
        pool.nfs_endpoint = "10.0.0.5:/exports/data"
        pool.nfs_port = 2049
        cfg = self._make_cfg()
        self._call(cfg, pool)
        assert cfg.nfs_server == "10.0.0.5"
        assert cfg.nfs_path == "/exports/data"
        assert cfg.nfs_port == 2049

    def test_byo_pool_without_path(self):
        pool = MagicMock()
        pool.mode = "shared-byo"
        pool.nfs_endpoint = "10.0.0.5"
        pool.nfs_port = 0
        cfg = self._make_cfg()
        self._call(cfg, pool)
        assert cfg.nfs_server == "10.0.0.5"
        assert cfg.nfs_path == "/"
        assert cfg.nfs_port == 0

    def test_ceph_nfs_pool(self):
        pool = MagicMock()
        pool.mode = "shared-ceph-nfs"
        pool.nfs_endpoint = "ceph-nfs.local:/cephfs"
        pool.nfs_port = 0
        cfg = self._make_cfg()
        self._call(cfg, pool)
        assert cfg.nfs_server == "ceph-nfs.local"
        assert cfg.nfs_path == "/cephfs"

    def test_fsx_pool_no_dns_name(self):
        pool = MagicMock()
        pool.mode = "shared-fsx"
        pool.fsx_dns_name = ""
        pool.nfs_endpoint = ""
        pool.nfs_port = 0
        cfg = self._make_cfg()
        self._call(cfg, pool)
        assert cfg.nfs_server == ""

    def test_local_pool_noop(self):
        pool = MagicMock()
        pool.mode = "local"
        pool.fsx_dns_name = ""
        pool.nfs_endpoint = ""
        kwargs = {}
        self._call(kwargs, pool)
        assert "nfs_server" not in kwargs
