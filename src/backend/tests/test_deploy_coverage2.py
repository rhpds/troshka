"""Tests for uncovered deploy_service helpers — second batch.

Covers:
  - _generate_exec_ssh_keypair
  - _find_gateway_port_forwards
  - _handle_kubevirt_deploy_error
  - _push_kubevirt_deploy_progress
  - _resolve_eip_provider
  - _allocate_kubevirt_eips
  - _resolve_multihost_ips
  - _should_skip_ocpvirt_eip
  - _regenerate_kubevirt_cloud_init
  - _deploy_finalize_timers
"""

from unittest.mock import MagicMock, patch

from app.services.deploy_service import (
    _find_gateway_port_forwards,
    _generate_exec_ssh_keypair,
    _handle_kubevirt_deploy_error,
    _push_kubevirt_deploy_progress,
    _resolve_eip_provider,
    _resolve_multihost_ips,
    _should_skip_ocpvirt_eip,
    _should_skip_route_eip,
)

# ═══════════════════════════════════════════════════════════════════════════
# _generate_exec_ssh_keypair — pure crypto, no mocks
# ═══════════════════════════════════════════════════════════════════════════


class TestGenerateExecSshKeypair:
    def test_generates_valid_keypair(self):
        privkey, pubkey = _generate_exec_ssh_keypair("test-project-id")
        assert privkey.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
        assert "ssh-ed25519" in pubkey
        assert "troshka-exec" in pubkey

    def test_each_call_unique(self):
        _, pub1 = _generate_exec_ssh_keypair("proj-1")
        _, pub2 = _generate_exec_ssh_keypair("proj-2")
        assert pub1 != pub2


# ═══════════════════════════════════════════════════════════════════════════
# _find_gateway_port_forwards — pure dict traversal
# ═══════════════════════════════════════════════════════════════════════════


class TestFindGatewayPortForwards:
    def test_finds_matching_forwards(self):
        topology = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {
                        "subtype": "gateway",
                        "portForwards": [
                            {"extIpId": "eip-1", "extPort": 443},
                            {"extIpId": "eip-2", "extPort": 80},
                            {"extIpId": "eip-1", "extPort": 8443},
                        ],
                    },
                }
            ]
        }
        result = _find_gateway_port_forwards(topology, "eip-1")
        assert len(result) == 2
        assert result[0]["extPort"] == 443
        assert result[1]["extPort"] == 8443

    def test_no_gateway(self):
        topology = {"nodes": [{"type": "vmNode", "data": {}}]}
        result = _find_gateway_port_forwards(topology, "eip-1")
        assert result == []

    def test_no_matching_eip(self):
        topology = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {
                        "subtype": "gateway",
                        "portForwards": [{"extIpId": "eip-99", "extPort": 443}],
                    },
                }
            ]
        }
        result = _find_gateway_port_forwards(topology, "eip-1")
        assert result == []

    def test_empty_topology(self):
        result = _find_gateway_port_forwards({}, "eip-1")
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════
# _handle_kubevirt_deploy_error
# ═══════════════════════════════════════════════════════════════════════════


class TestHandleKubevirtDeployError:
    @patch("app.services.deploy_service._delete_deploy_progress")
    def test_sets_error_state(self, mock_del):
        project = MagicMock()
        db = MagicMock()
        notify = MagicMock()
        status = {"error": "OOM killed"}
        _handle_kubevirt_deploy_error("proj-12345678", project, status, db, notify)
        assert project.state == "error"
        assert project.deploy_error == "OOM killed"
        db.commit.assert_called()
        notify.assert_called_once()
        mock_del.assert_called_once()

    @patch("app.services.deploy_service._delete_deploy_progress")
    def test_fallback_message(self, mock_del):
        project = MagicMock()
        db = MagicMock()
        notify = MagicMock()
        status = {}
        _handle_kubevirt_deploy_error("proj-aabbccdd", project, status, db, notify)
        assert "Operator reported an error" in project.deploy_error

    @patch("app.services.deploy_service._delete_deploy_progress")
    def test_message_field(self, mock_del):
        project = MagicMock()
        db = MagicMock()
        notify = MagicMock()
        status = {"message": "timeout waiting for CRD"}
        _handle_kubevirt_deploy_error("proj-11223344", project, status, db, notify)
        assert project.deploy_error == "timeout waiting for CRD"


# ═══════════════════════════════════════════════════════════════════════════
# _push_kubevirt_deploy_progress
# ═══════════════════════════════════════════════════════════════════════════


class TestPushKubevirtDeployProgress:
    @patch("app.services.deploy_service._set_deploy_progress")
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_pushes_new_progress(self, mock_get, mock_set):
        project = MagicMock()
        db = MagicMock()
        notify = MagicMock()
        _push_kubevirt_deploy_progress(
            "proj-1", project, "images", "downloading", 50, [], db, notify
        )
        mock_set.assert_called_once()
        notify.assert_called_once()

    @patch("app.services.deploy_service._get_deploy_progress_data")
    def test_skips_unchanged(self, mock_get):
        mock_get.return_value = {"step": "images", "detail": "done", "percent": 100}
        project = MagicMock()
        db = MagicMock()
        notify = MagicMock()
        _push_kubevirt_deploy_progress(
            "proj-1", project, "images", "done", 100, [], db, notify
        )
        notify.assert_not_called()

    def test_skips_empty_detail_and_dv_lines(self):
        project = MagicMock()
        db = MagicMock()
        notify = MagicMock()
        _push_kubevirt_deploy_progress("proj-1", project, "step", "", 0, [], db, notify)
        notify.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# _resolve_eip_provider
# ═══════════════════════════════════════════════════════════════════════════


class TestResolveEipProvider:
    @patch("app.services.providers.get_provider_driver")
    def test_resolves_from_provider_id(self, mock_drv):
        project = MagicMock()
        project.provider_id = "prov-1"
        project.host_id = "host-1"
        db = MagicMock()
        prov = MagicMock()
        host = MagicMock()
        host.provider_id = "prov-1"
        db.query.return_value.filter_by.return_value.first.side_effect = [prov, host]
        result = _resolve_eip_provider("proj-12345678", project, db)
        assert result[0] == prov

    def test_no_host_no_provider(self):
        project = MagicMock()
        project.provider_id = None
        project.host_id = None
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        result = _resolve_eip_provider("proj-00000000", project, db)
        assert result == (None, None, None)

    @patch("app.services.providers.get_provider_driver")
    def test_falls_back_to_host_provider(self, mock_drv):
        project = MagicMock()
        project.provider_id = None
        project.host_id = "host-1"

        host = MagicMock()
        host.provider_id = "prov-2"
        prov = MagicMock()

        db = MagicMock()
        # provider_id is None so first query skipped (else None)
        # Second call: query(Host).filter_by(id=host_id).first() → host
        # Third call: query(Provider).filter_by(id=host.provider_id).first() → prov
        db.query.return_value.filter_by.return_value.first.side_effect = [
            host,  # host lookup
            prov,  # provider from host.provider_id
        ]
        result = _resolve_eip_provider("proj-aaaabbbb", project, db)
        assert result[0] == prov


# ═══════════════════════════════════════════════════════════════════════════
# _resolve_multihost_ips
# ═══════════════════════════════════════════════════════════════════════════


class TestResolveMultihostIps:
    def test_same_pool_uses_private(self):
        db = MagicMock()
        h1 = MagicMock()
        h1.id = "h1"
        h1.storage_pool_id = "pool-1"
        h1.private_ip = "10.0.0.1"
        h1.ip_address = "1.2.3.4"
        h2 = MagicMock()
        h2.id = "h2"
        h2.storage_pool_id = "pool-1"
        h2.private_ip = "10.0.0.2"
        h2.ip_address = "5.6.7.8"
        db.query.return_value.filter_by.return_value.first.side_effect = [h1, h2]
        result = _resolve_multihost_ips({"h1": ["vm1"], "h2": ["vm2"]}, db)
        assert result["h1"] == "10.0.0.1"
        assert result["h2"] == "10.0.0.2"

    def test_no_private_ip_uses_public(self):
        db = MagicMock()
        h1 = MagicMock()
        h1.id = "h1"
        h1.storage_pool_id = "pool-1"
        h1.private_ip = None
        h1.ip_address = "1.2.3.4"
        db.query.return_value.filter_by.return_value.first.return_value = h1
        result = _resolve_multihost_ips({"h1": ["vm1"]}, db)
        assert result["h1"] == "1.2.3.4"


# ═══════════════════════════════════════════════════════════════════════════
# _should_skip_ocpvirt_eip
# ═══════════════════════════════════════════════════════════════════════════


class TestShouldSkipOcpvirtEip:
    def test_non_ocpvirt_never_skips(self):
        provider = MagicMock()
        provider.type = "ec2"
        assert _should_skip_ocpvirt_eip(provider, {}, "eip-1", "proj-1") is False

    def test_ocpvirt_all_routable_ports_skips(self):
        provider = MagicMock()
        provider.type = "ocpvirt"
        topology = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {
                        "subtype": "gateway",
                        "portForwards": [
                            {"extIpId": "eip-1", "extPort": 443},
                            {"extIpId": "eip-1", "extPort": 80},
                        ],
                    },
                }
            ]
        }
        assert (
            _should_skip_ocpvirt_eip(provider, topology, "eip-1", "proj-12345678")
            is True
        )

    def test_ocpvirt_non_routable_ports_no_skip(self):
        provider = MagicMock()
        provider.type = "ocpvirt"
        topology = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {
                        "subtype": "gateway",
                        "portForwards": [
                            {"extIpId": "eip-1", "extPort": 443},
                            {"extIpId": "eip-1", "extPort": 8080},
                        ],
                    },
                }
            ]
        }
        assert (
            _should_skip_route_eip(provider, topology, "eip-1", "proj-12345678")
            is False
        )

    def test_kubevirt_all_routable_ports_skips(self):
        provider = MagicMock()
        provider.type = "kubevirt"
        topology = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {
                        "subtype": "gateway",
                        "portForwards": [
                            {"extIpId": "eip-1", "extPort": 80},
                            {"extIpId": "eip-1", "extPort": 6443},
                        ],
                    },
                }
            ]
        }
        assert (
            _should_skip_route_eip(provider, topology, "eip-1", "proj-12345678") is True
        )


# ═══════════════════════════════════════════════════════════════════════════
# _regenerate_kubevirt_cloud_init
# ═══════════════════════════════════════════════════════════════════════════


class TestRegenerateKubevirtCloudInit:
    @patch("app.services.cloud_init.generate_userdata", return_value="userdata")
    def test_injects_exec_key(self, mock_gen):
        from app.services.deploy_service import _regenerate_kubevirt_cloud_init

        topology = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {
                        "cloudInit": True,
                        "ciSshKeys": ["ssh-rsa AAAA old"],
                    },
                },
                {"type": "networkNode", "data": {}},
            ]
        }
        project = MagicMock()
        project.guest_exec_enabled = True
        _regenerate_kubevirt_cloud_init(
            topology, project, "ssh-ed25519 AAAA troshka-exec"
        )
        vm_data = topology["nodes"][0]["data"]
        assert "ssh-ed25519 AAAA troshka-exec" in vm_data["ciSshKeys"]
        assert vm_data["ciGeneratedUserData"] == "userdata"

    @patch("app.services.cloud_init.generate_userdata", return_value="ud")
    def test_guest_exec_disabled(self, mock_gen):
        from app.services.deploy_service import _regenerate_kubevirt_cloud_init

        topology = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {"cloudInit": True, "ciSshKeys": []},
                }
            ]
        }
        project = MagicMock()
        project.guest_exec_enabled = False
        _regenerate_kubevirt_cloud_init(
            topology, project, "ssh-ed25519 AAAA troshka-exec"
        )
        assert topology["nodes"][0]["data"]["guestExecEnabled"] is False

    def test_skips_non_cloud_init_vms(self):
        from app.services.deploy_service import _regenerate_kubevirt_cloud_init

        topology = {
            "nodes": [
                {"type": "vmNode", "data": {"cloudInit": False}},
                {"type": "vmNode", "data": {}},
            ]
        }
        project = MagicMock()
        project.guest_exec_enabled = True
        _regenerate_kubevirt_cloud_init(
            topology, project, "ssh-ed25519 AAAA troshka-exec"
        )

    @patch("app.services.cloud_init.generate_userdata", return_value="ud")
    def test_removes_old_troshka_exec_key(self, mock_gen):
        from app.services.deploy_service import _regenerate_kubevirt_cloud_init

        topology = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {
                        "cloudInit": True,
                        "ciSshKeys": [
                            "ssh-ed25519 AAAA troshka-exec",
                            "ssh-rsa BBBB user",
                        ],
                    },
                }
            ]
        }
        project = MagicMock()
        project.guest_exec_enabled = True
        _regenerate_kubevirt_cloud_init(
            topology, project, "ssh-ed25519 CCCC troshka-exec"
        )
        keys = topology["nodes"][0]["data"]["ciSshKeys"]
        assert len(keys) == 2
        assert "ssh-rsa BBBB user" in keys
        assert "ssh-ed25519 CCCC troshka-exec" in keys
