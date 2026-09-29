"""Unit tests for projects.py helpers extracted for Sonar S3776."""

import pytest
from fastapi import HTTPException

from app.api.projects import (
    _apply_cloud_init_credentials,
    _apply_password_mode,
    _apply_template_ocp_body_defaults,
    _cluster_access,
    _cluster_cp_member,
    _eip_pf_for_canvas,
    _empty_topo_diff,
    _exec_methods_for,
    _exec_password_from_body_or_vm,
    _exec_root_password,
    _exec_vm_ip,
    _normalize_exec_method,
    _persist_placement_and_enqueue,
    _reconfigure_diff,
    _require_reconfigure_host,
    _resolve_exec_params,
    _serial_type_from_ansible_groups,
    _template_project_description,
    _troshkad_added_vm_ready,
    _validate_bastion_bmc_ip,
    _vm_config_unchanged,
    _vm_reconfigure_needs_restart,
)


class TestClusterAccess:
    def test_empty_topo(self):
        assert _cluster_access({}, "c1") == {
            "kubeadmin_password": "",
            "kubeconfig_available": False,
            "vm_name": "",
        }

    def test_skips_workers(self):
        topo = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {
                        "clusterId": "c1",
                        "clusterRole": "worker",
                        "ocpKubeadminPassword": "x",
                    },
                }
            ]
        }
        assert _cluster_access(topo, "c1")["kubeadmin_password"] == ""

    def test_cp_with_creds(self):
        topo = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {
                        "clusterId": "c1",
                        "clusterRole": "master",
                        "label": "cp-0",
                        "ocpKubeadminPassword": "pw",
                        "ocpKubeconfig": "k",
                    },
                }
            ]
        }
        assert _cluster_access(topo, "c1") == {
            "kubeadmin_password": "pw",
            "kubeconfig_available": True,
            "vm_name": "cp-0",
        }

    def test_cp_member_helper(self):
        topo = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {
                        "clusterId": "c1",
                        "tags": {"AnsibleGroup": "workers"},
                    },
                },
                {
                    "type": "vmNode",
                    "data": {"clusterId": "c1", "clusterRole": "master", "name": "m"},
                },
            ]
        }
        d = _cluster_cp_member(topo, "c1")
        assert d and d.get("name") == "m"


class TestCloudInitAndPassword:
    def test_apply_cloud_init(self):
        topo = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {"cloudInit": True},
                },
                {"type": "networkNode", "data": {}},
            ]
        }
        _apply_cloud_init_credentials(topo, "secret", ["k1"], ["ssh-rsa A"])
        d = topo["nodes"][0]["data"]
        assert d["ciCloudUserPassword"] == "secret"
        assert d["ciSshKeyIds"] == ["k1"]
        assert d["ciSshKeys"] == ["ssh-rsa A"]

    def test_apply_password_mode_none(self):
        result = {
            "networks": {"n1": {"bmc_password": "x"}},
            "vms": {"v1": {"cloud_user_password": "y"}},
        }
        _apply_password_mode(result, "none")
        assert "bmc_password" not in result["networks"]["n1"]
        assert "cloud_user_password" not in result["vms"]["v1"]

    def test_apply_password_mode_custom(self):
        result = {
            "networks": {"n1": {"bmc_password": "x"}},
            "vms": {"v1": {"cloud_user_password": "y"}},
        }
        _apply_password_mode(result, "custom", "new")
        assert result["networks"]["n1"]["bmc_password"] == "new"
        assert result["vms"]["v1"]["cloud_user_password"] == "new"


class TestSerialAndVmConfig:
    def test_serial_groups(self):
        assert _serial_type_from_ansible_groups(["cisco_iosxe"]) == "ios"
        assert _serial_type_from_ansible_groups(["eos"]) == "eos"
        assert _serial_type_from_ansible_groups(["junos"]) == "junos"
        assert _serial_type_from_ansible_groups(["linux"]) == "linux"

    def test_troshkad_added_vm_ready(self):
        assert _troshkad_added_vm_ready(None, True) is False
        assert _troshkad_added_vm_ready("running", True) is True
        assert _troshkad_added_vm_ready("shut off", False) is True
        assert _troshkad_added_vm_ready("paused", False) is False

    def test_vm_config_unchanged(self):
        cfg = {
            "boot_devs": ["hd"],
            "vcpus": 2,
            "ram_mb": 4096,
            "disks": ["/d"],
            "cdroms": [],
            "nics": [],
        }
        vm = {"vcpus": 2, "ram_gb": 4}
        assert _vm_config_unchanged(cfg, ["hd"], vm, [], [], ["/d"], [])

    def test_needs_restart_disk_only(self):
        cfg = {
            "boot_devs": ["hd"],
            "vcpus": 2,
            "ram_mb": 4096,
            "disks": ["/old"],
            "cdroms": [],
        }
        vm = {"node_id": "v1", "vcpus": 2, "ram_gb": 4}
        assert (
            _vm_reconfigure_needs_restart(cfg, ["hd"], vm, [], [], ["/new"], [], set())
            is False
        )

    def test_needs_restart_cpu(self):
        cfg = {
            "boot_devs": ["hd"],
            "vcpus": 2,
            "ram_mb": 4096,
            "disks": ["/d"],
            "cdroms": [],
        }
        vm = {"node_id": "v1", "vcpus": 4, "ram_gb": 4}
        assert (
            _vm_reconfigure_needs_restart(cfg, ["hd"], vm, [], [], ["/d"], [], set())
            is True
        )


class TestEipPfAndDiff:
    def test_eip_pf_skips_route_web(self):
        pfs = [
            {"extIpId": "e1", "extPort": "443"},
            {"extIpId": "e1", "extPort": "8443"},
            {"extIpId": "e2", "extPort": "22"},
        ]
        assert _eip_pf_for_canvas(pfs, "e1", True) == [
            {"extIpId": "e1", "extPort": "8443"}
        ]

    def test_empty_topo_diff(self):
        d = _empty_topo_diff()
        assert d["added_vms"] == []
        assert d["has_changes"] is False


class TestTemplateHelpers:
    def test_ocp_body_defaults(self):
        body: dict = {}
        _apply_template_ocp_body_defaults(
            body, {"ocp": {"name": "lab", "base_domain": "example.com"}}
        )
        assert body["cluster_name"] == "lab"
        assert body["base_domain"] == "example.com"

    def test_bastion_bmc_ip_ok(self):
        assert _validate_bastion_bmc_ip("10.0.0.1") == "10.0.0.1"

    def test_bastion_bmc_ip_bad(self):
        with pytest.raises(HTTPException) as ei:
            _validate_bastion_bmc_ip("not-an-ip")
        assert ei.value.status_code == 400

    def test_template_description(self):
        desc = _template_project_description(
            {"cluster_name": "c", "base_domain": "d.local", "ocp_version": "4.16"},
            {"description": "Demo"},
        )
        assert "Demo" in desc
        assert "OCP 4.16" in desc
        assert "api.c.d.local" in desc


class TestExecParamHelpers:
    def test_password_from_vm(self):
        vm = {"data": {"ciCloudUserPassword": "from-vm"}}
        assert _exec_password_from_body_or_vm({}, vm) == "from-vm"
        assert _exec_password_from_body_or_vm({"password": "body"}, vm) == "body"

    def test_vm_ip_and_root(self):
        vm = {
            "data": {
                "ciRootPassword": "rootpw",
                "nics": [{"ip": ""}, {"ip": "1.2.3.4"}],
            }
        }
        assert _exec_vm_ip(vm) == "1.2.3.4"
        assert _exec_root_password(vm) == "rootpw"
        assert _exec_vm_ip(None) == ""

    def test_normalize_console_text(self):
        method, force_tty = _normalize_exec_method({"method": "console-text"})
        assert method == "console"
        assert force_tty is True

    def test_methods_auto_no_cloudinit(self):
        vm = {"data": {"cloudInit": False}}
        assert _exec_methods_for("auto", vm) == ["ssh", "console", "serial"]

    def test_resolve_exec_params_ssh(self):
        p = _resolve_exec_params({"use_ssh": True, "username": "u"}, None)
        assert p["method"] == "ssh"
        assert p["methods"] == ["ssh"]
        assert p["username"] == "u"


class TestReconfigureHelpers:
    def test_require_host_missing(self):
        with pytest.raises(HTTPException) as ei:
            _require_reconfigure_host(None)
        assert ei.value.status_code == 503

    def test_require_host_kubevirt_ok(self):
        class H:
            host_type = "kubevirt-cluster"
            private_key = None
            ip_address = None

        _require_reconfigure_host(H())

    def test_reconfigure_diff_empty(self):
        d = _reconfigure_diff({"nodes": []}, {})
        assert d == _empty_topo_diff()


class TestPersistPlacement:
    def test_single_host_response(self, monkeypatch):
        calls = {}

        class Proj:
            id = "p1"
            vni_map = None

        class Db:
            def commit(self):
                calls["commit"] = True

        monkeypatch.setattr(
            "app.api.projects._check_single_host_disk", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "app.core.redis.enqueue_job", lambda *a, **k: calls.setdefault("enq", True)
        )
        out = _persist_placement_and_enqueue(
            Proj(),
            {
                "host_id": "h1",
                "host_ip": "10.0.0.1",
                "requirements": {},
                "vni_map": {"n": 1},
            },
            "p1",
            Db(),
        )
        assert out["status"] == "deploying"
        assert out["host_id"] == "h1"
        assert calls.get("enq") and calls.get("commit")
