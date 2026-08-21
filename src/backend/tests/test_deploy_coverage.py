"""Tests for uncovered helper functions in deploy_service.py.

Focuses on newly extracted helpers: BMC, mesh, network, multihost,
container, EIP, LB, DNS, destroy, and various small utility functions.
"""

from unittest.mock import MagicMock, patch

from app.services.deploy_service import (
    _add_registry_creds,
    _auto_enable_recert_on_rhcos,
    _build_multihost_assignments,
    _build_vm_progress_items,
    _clean_stale_domain,
    _clean_stale_domains,
    _collect_gateway_sg_rules,
    _create_multihost_disks,
    _define_multihost_vms,
    _deploy_create_containers,
    _deploy_finalize_timers,
    _deploy_inject_gateway_ip,
    _deploy_pull_container_images,
    _deploy_setup_lb,
    _deploy_store_bmc_topology,
    _deploy_validate_bmc,
    _destroy_cleanup_dns,
    _destroy_cleanup_eips,
    _destroy_mesh_on_all_hosts,
    _destroy_remote_networks,
    _destroy_stop_all_vms,
    _detect_common_password,
    _detect_pattern_id,
    _detect_recert_from_pattern,
    _find_gateway_connected_network,
    _find_gateway_ip,
    _find_gateway_port_forwards,
    _find_lb_eip_private_ip,
    _get_connected_host,
    _handle_kubevirt_deploy_error,
    _inject_lb_port_forwards,
    _pull_pod_images,
    _pull_single_container_image,
    _push_kubevirt_deploy_progress,
    _push_mesh_config_to_peer,
    _resolve_multihost_ips,
    _resolve_recert_settings,
    _rollback_mesh,
    _setup_bmc_via_troshkad,
    _should_skip_ocpvirt_eip,
    _teardown_bmc_via_troshkad,
    _teardown_networks_via_troshkad,
)
from app.services.troshkad_client import TroshkadError

# ── Constants ──────────────────────────────────────────────────────────────

PROJECT_ID = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
HOST_ID = "host-0001"
HOST_ID_2 = "host-0002"


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_host(ip="10.0.0.1", host_type="ec2", provider_id="prov-1"):
    h = MagicMock()
    h.id = HOST_ID
    h.ip_address = ip
    h.host_type = host_type
    h.provider_id = provider_id
    h.storage_pool_id = None
    h.agent_status = "connected"
    return h


# ═══════════════════════════════════════════════════════════════════════════
# _setup_bmc_via_troshkad
# ═══════════════════════════════════════════════════════════════════════════


class TestSetupBmcViaTroshkad:
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    @patch("app.services.deploy_service._teardown_bmc_via_troshkad")
    def test_success(self, mock_teardown, mock_start, mock_wait):
        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}
        host = _make_host()
        bmc_config = {
            "bmc_network": {
                "cidr": "192.168.100.0/24",
                "bmcUsername": "admin",
                "bmcPassword": "pass123",
            },
            "vms": [{"domain_name": "vm1", "bmc_ip": "192.168.100.10"}],
            "dhcp_hosts": [],
        }
        result = _setup_bmc_via_troshkad(host, PROJECT_ID, bmc_config)
        assert result is True
        mock_teardown.assert_called_once()
        mock_start.assert_called_once()

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    @patch("app.services.deploy_service._teardown_bmc_via_troshkad")
    def test_failed(self, mock_teardown, mock_start, mock_wait):
        mock_start.return_value = "job-1"
        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "port conflict"},
        }
        host = _make_host()
        bmc_config = {
            "bmc_network": {"cidr": "10.0.0.0/24"},
            "vms": [],
            "dhcp_hosts": [],
        }
        result = _setup_bmc_via_troshkad(host, PROJECT_ID, bmc_config)
        assert result == "port conflict"


# ═══════════════════════════════════════════════════════════════════════════
# _teardown_bmc_via_troshkad
# ═══════════════════════════════════════════════════════════════════════════


class TestTeardownBmcViaTroshkad:
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_success(self, mock_start, mock_wait):
        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}
        host = _make_host()
        _teardown_bmc_via_troshkad(host, PROJECT_ID)
        mock_start.assert_called_once_with(
            host, "/bmc/teardown", {"project_id": PROJECT_ID}
        )

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_failed_logs_warning(self, mock_start, mock_wait):
        mock_start.return_value = "job-1"
        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "no bmc found"},
        }
        host = _make_host()
        # Should not raise, just log
        _teardown_bmc_via_troshkad(host, PROJECT_ID)


# ═══════════════════════════════════════════════════════════════════════════
# _push_mesh_config_to_peer
# ═══════════════════════════════════════════════════════════════════════════


class TestPushMeshConfigToPeer:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    @patch("app.services.deploy_service.get_peer_config_for_host")
    def test_success(self, mock_get_cfg, mock_start, mock_wait):
        mock_get_cfg.return_value = {"some": "config"}
        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}
        db = MagicMock()
        host = _make_host()
        db.query.return_value.filter_by.return_value.first.return_value = host
        peer = MagicMock()
        peer.host_id = HOST_ID
        result = _push_mesh_config_to_peer(db, PROJECT_ID, peer)
        assert result is None

    def test_no_host_id(self):
        db = MagicMock()
        peer = MagicMock()
        peer.host_id = None
        result = _push_mesh_config_to_peer(db, PROJECT_ID, peer)
        assert result == "Peer has no host_id"

    @patch("app.services.deploy_service.get_peer_config_for_host")
    def test_host_not_found(self, mock_get_cfg):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        peer = MagicMock()
        peer.host_id = "missing-host"
        result = _push_mesh_config_to_peer(db, PROJECT_ID, peer)
        assert "not found" in result

    @patch("app.services.deploy_service.start_job", side_effect=Exception("conn error"))
    @patch("app.services.deploy_service.get_peer_config_for_host")
    def test_exception(self, mock_get_cfg, mock_start):
        db = MagicMock()
        host = _make_host()
        db.query.return_value.filter_by.return_value.first.return_value = host
        peer = MagicMock()
        peer.host_id = HOST_ID
        result = _push_mesh_config_to_peer(db, PROJECT_ID, peer)
        assert "conn error" in result

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    @patch("app.services.deploy_service.get_peer_config_for_host")
    def test_job_failed(self, mock_get_cfg, mock_start, mock_wait):
        mock_get_cfg.return_value = {}
        mock_start.return_value = "job-1"
        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "wg failed"},
        }
        db = MagicMock()
        host = _make_host()
        db.query.return_value.filter_by.return_value.first.return_value = host
        peer = MagicMock()
        peer.host_id = HOST_ID
        result = _push_mesh_config_to_peer(db, PROJECT_ID, peer)
        assert "wg failed" in result


# ═══════════════════════════════════════════════════════════════════════════
# _rollback_mesh
# ═══════════════════════════════════════════════════════════════════════════


class TestRollbackMesh:
    @patch("app.services.deploy_service.delete_mesh_peers")
    @patch("app.services.deploy_service.troshkad_request")
    def test_rollback(self, mock_request, mock_delete):
        db = MagicMock()
        host = _make_host()
        db.query.return_value.filter_by.return_value.first.return_value = host
        peer = MagicMock()
        peer.host_id = HOST_ID
        _rollback_mesh(db, PROJECT_ID, [peer])
        mock_request.assert_called_once()
        mock_delete.assert_called_once_with(db, PROJECT_ID)

    @patch("app.services.deploy_service.delete_mesh_peers")
    @patch("app.services.deploy_service.troshkad_request", side_effect=Exception("err"))
    def test_rollback_ignores_errors(self, mock_request, mock_delete):
        db = MagicMock()
        host = _make_host()
        db.query.return_value.filter_by.return_value.first.return_value = host
        peer = MagicMock()
        peer.host_id = HOST_ID
        # Should not raise
        _rollback_mesh(db, PROJECT_ID, [peer])
        mock_delete.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# _teardown_networks_via_troshkad
# ═══════════════════════════════════════════════════════════════════════════


class TestTeardownNetworksViaTroshkad:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    def test_success(self, mock_start, mock_wait):
        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}
        host = _make_host()
        vni_map = {"net-1": 100, "net-2": 200}
        _teardown_networks_via_troshkad(host, PROJECT_ID, vni_map)
        args = mock_start.call_args
        assert args[0][1] == "/networks/full-teardown"
        assert set(args[0][2]["vni_list"]) == {100, 200}

    @patch("app.services.deploy_service.start_job", side_effect=TroshkadError("fail"))
    def test_troshkad_error_logged(self, mock_start):
        host = _make_host()
        # Should not raise
        _teardown_networks_via_troshkad(host, PROJECT_ID, {"n": 100})

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    def test_empty_vni_map(self, mock_start, mock_wait):
        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}
        host = _make_host()
        _teardown_networks_via_troshkad(host, PROJECT_ID, None)
        assert mock_start.call_args[0][2]["vni_list"] == []


# ═══════════════════════════════════════════════════════════════════════════
# _find_gateway_port_forwards
# ═══════════════════════════════════════════════════════════════════════════


class TestFindGatewayPortForwards:
    def test_returns_matching_forwards(self):
        topo = {
            "nodes": [
                {
                    "id": "gw1",
                    "data": {
                        "subtype": "gateway",
                        "portForwards": [
                            {"extPort": 443, "extIpId": "eip-1"},
                            {"extPort": 80, "extIpId": "eip-2"},
                            {"extPort": 8443, "extIpId": "eip-1"},
                        ],
                    },
                }
            ]
        }
        result = _find_gateway_port_forwards(topo, "eip-1")
        assert len(result) == 2
        assert result[0]["extPort"] == 443
        assert result[1]["extPort"] == 8443

    def test_no_gateway(self):
        topo = {"nodes": [{"id": "vm1", "data": {"subtype": "vm"}}]}
        assert _find_gateway_port_forwards(topo, "eip-1") == []

    def test_no_matching_eip(self):
        topo = {
            "nodes": [
                {
                    "id": "gw1",
                    "data": {
                        "subtype": "gateway",
                        "portForwards": [{"extPort": 443, "extIpId": "eip-other"}],
                    },
                }
            ]
        }
        assert _find_gateway_port_forwards(topo, "eip-1") == []


# ═══════════════════════════════════════════════════════════════════════════
# _find_lb_eip_private_ip
# ═══════════════════════════════════════════════════════════════════════════


class TestFindLbEipPrivateIp:
    def test_from_external_ips(self):
        lb = {"ext_ip_id": "eip-1"}
        gw = {"eip_private_ips": ["10.0.0.50"]}
        topo = {"externalIps": [{"id": "eip-1", "_private_ip": "10.0.0.99"}]}
        assert _find_lb_eip_private_ip(lb, gw, topo) == "10.0.0.99"

    def test_fallback_to_gw_private_ips(self):
        lb = {"ext_ip_id": "eip-1"}
        gw = {"eip_private_ips": ["10.0.0.50"]}
        topo = {"externalIps": []}
        assert _find_lb_eip_private_ip(lb, gw, topo) == "10.0.0.50"

    def test_empty_fallback(self):
        lb = {}
        gw = {}
        topo = {}
        assert _find_lb_eip_private_ip(lb, gw, topo) == ""


# ═══════════════════════════════════════════════════════════════════════════
# _inject_lb_port_forwards
# ═══════════════════════════════════════════════════════════════════════════


class TestInjectLbPortForwards:
    def test_no_lb(self):
        config = {}
        _inject_lb_port_forwards(config, {}, {})
        assert "port_forwards" not in config

    def test_lb_not_external(self):
        config = {"loadbalancer": {"frontends": [{"bindPort": 80}], "external": False}}
        _inject_lb_port_forwards(config, {}, {})
        # Should return early without modifying gateway
        assert "gateway" not in config

    def test_lb_with_existing_gateway(self):
        config = {
            "loadbalancer": {
                "frontends": [{"bindPort": 6443}],
                "external": True,
            },
            "gateway": {
                "mode": "nat-portforward",
                "port_forwards": [],
                "eip_private_ips": ["10.0.0.1"],
                "transit_ns_ip": "192.168.1.1",
            },
        }
        topo = {"externalIps": []}
        _inject_lb_port_forwards(config, topo, {})
        pfs = config["gateway"]["port_forwards"]
        assert len(pfs) == 1
        assert pfs[0]["extPort"] == 6443


# ═══════════════════════════════════════════════════════════════════════════
# _handle_kubevirt_deploy_error
# ═══════════════════════════════════════════════════════════════════════════


class TestHandleKubevirtDeployError:
    @patch("app.services.deploy_service._delete_deploy_progress")
    def test_sets_error_state(self, mock_del):
        project = MagicMock()
        db = MagicMock()
        mock_notify = MagicMock()
        status = {"error": "disk timeout"}
        _handle_kubevirt_deploy_error(PROJECT_ID, project, status, db, mock_notify)
        assert project.state == "error"
        assert project.deploy_error == "disk timeout"
        db.commit.assert_called_once()
        mock_notify.assert_called_once()

    @patch("app.services.deploy_service._delete_deploy_progress")
    def test_fallback_error_msg(self, mock_del):
        project = MagicMock()
        db = MagicMock()
        mock_notify = MagicMock()
        status = {}
        _handle_kubevirt_deploy_error(PROJECT_ID, project, status, db, mock_notify)
        assert project.deploy_error == "Operator reported an error"


# ═══════════════════════════════════════════════════════════════════════════
# _push_kubevirt_deploy_progress
# ═══════════════════════════════════════════════════════════════════════════


class TestPushKubevirtDeployProgress:
    @patch("app.services.deploy_service._set_deploy_progress")
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    def test_pushes_new_progress(self, mock_get, mock_set):
        project = MagicMock()
        db = MagicMock()
        mock_notify = MagicMock()
        _push_kubevirt_deploy_progress(
            PROJECT_ID, project, "importing", "disk 1", 50, ["line1"], db, mock_notify
        )
        mock_set.assert_called_once()
        mock_notify.assert_called_once()

    @patch("app.services.deploy_service._set_deploy_progress")
    @patch("app.services.deploy_service._get_deploy_progress_data")
    def test_skips_duplicate(self, mock_get, mock_set):
        mock_get.return_value = {"step": "importing", "detail": "disk 1", "percent": 50}
        project = MagicMock()
        db = MagicMock()
        mock_notify = MagicMock()
        _push_kubevirt_deploy_progress(
            PROJECT_ID, project, "importing", "disk 1", 50, ["line1"], db, mock_notify
        )
        mock_set.assert_not_called()
        mock_notify.assert_not_called()

    def test_no_detail_and_no_lines_returns_early(self):
        project = MagicMock()
        db = MagicMock()
        mock_notify = MagicMock()
        _push_kubevirt_deploy_progress(
            PROJECT_ID, project, "idle", "", 0, [], db, mock_notify
        )
        mock_notify.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# _build_multihost_assignments
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildMultihostAssignments:
    def test_builds_assignments(self):
        project = MagicMock()
        project.host_assignments = {"vm-1": "h1", "vm-2": "h1", "vm-3": "h2"}
        result = _build_multihost_assignments(project)
        assert result == {"h1": ["vm-1", "vm-2"], "h2": ["vm-3"]}

    def test_empty_returns_none(self):
        project = MagicMock()
        project.host_assignments = {}
        assert _build_multihost_assignments(project) is None

    def test_none_returns_none(self):
        project = MagicMock()
        project.host_assignments = None
        assert _build_multihost_assignments(project) is None


# ═══════════════════════════════════════════════════════════════════════════
# _resolve_multihost_ips
# ═══════════════════════════════════════════════════════════════════════════


class TestResolveMultihostIps:
    def test_same_pool_uses_private_ip(self):
        h1 = MagicMock()
        h1.id = "h1"
        h1.ip_address = "1.2.3.4"
        h1.private_ip = "10.0.0.1"
        h1.storage_pool_id = "pool-1"
        h2 = MagicMock()
        h2.id = "h2"
        h2.ip_address = "5.6.7.8"
        h2.private_ip = "10.0.0.2"
        h2.storage_pool_id = "pool-1"
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [h1, h2]
        result = _resolve_multihost_ips({"h1": [], "h2": []}, db)
        assert result == {"h1": "10.0.0.1", "h2": "10.0.0.2"}

    def test_different_pools_uses_public_ip(self):
        h1 = MagicMock()
        h1.id = "h1"
        h1.ip_address = "1.2.3.4"
        h1.private_ip = "10.0.0.1"
        h1.storage_pool_id = "pool-1"
        h2 = MagicMock()
        h2.id = "h2"
        h2.ip_address = "5.6.7.8"
        h2.private_ip = "10.0.0.2"
        h2.storage_pool_id = "pool-2"
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [h1, h2]
        result = _resolve_multihost_ips({"h1": [], "h2": []}, db)
        assert result == {"h1": "1.2.3.4", "h2": "5.6.7.8"}


# ═══════════════════════════════════════════════════════════════════════════
# _should_skip_ocpvirt_eip
# ═══════════════════════════════════════════════════════════════════════════


class TestShouldSkipOcpvirtEip:
    def test_non_ocpvirt_returns_false(self):
        provider = MagicMock()
        provider.type = "ec2"
        assert _should_skip_ocpvirt_eip(provider, {}, "eip-1", PROJECT_ID) is False

    def test_only_routable_ports_returns_true(self):
        provider = MagicMock()
        provider.type = "ocpvirt"
        topo = {
            "nodes": [
                {
                    "id": "gw1",
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
        assert _should_skip_ocpvirt_eip(provider, topo, "eip-1", PROJECT_ID) is True

    def test_non_routable_port_returns_false(self):
        provider = MagicMock()
        provider.type = "ocpvirt"
        topo = {
            "nodes": [
                {
                    "id": "gw1",
                    "data": {
                        "subtype": "gateway",
                        "portForwards": [
                            {"extIpId": "eip-1", "extPort": 8443},
                        ],
                    },
                }
            ]
        }
        assert _should_skip_ocpvirt_eip(provider, topo, "eip-1", PROJECT_ID) is False

    def test_no_matching_ports_returns_false(self):
        provider = MagicMock()
        provider.type = "ocpvirt"
        topo = {"nodes": []}
        assert _should_skip_ocpvirt_eip(provider, topo, "eip-1", PROJECT_ID) is False


# ═══════════════════════════════════════════════════════════════════════════
# _collect_gateway_sg_rules
# ═══════════════════════════════════════════════════════════════════════════


class TestCollectGatewaySgRules:
    def test_collects_rules(self):
        gw = {
            "data": {
                "gatewayMode": "nat-portforward",
                "portForwards": [
                    {"extPort": 443},
                    {"extPort": 80},
                ],
            }
        }
        rules = _collect_gateway_sg_rules(gw, PROJECT_ID)
        assert len(rules) == 2
        assert rules[0]["ext_port"] == 443

    def test_wrong_mode_returns_empty(self):
        gw = {"data": {"gatewayMode": "nat", "portForwards": [{"extPort": 443}]}}
        assert _collect_gateway_sg_rules(gw, PROJECT_ID) == []

    def test_none_gateway(self):
        assert _collect_gateway_sg_rules(None, PROJECT_ID) == []


# ═══════════════════════════════════════════════════════════════════════════
# _find_gateway_connected_network
# ═══════════════════════════════════════════════════════════════════════════


class TestFindGatewayConnectedNetwork:
    def test_finds_connected_network(self):
        topo = {
            "nodes": [
                {"id": "gw1", "type": "gatewayNode"},
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"cidr": "10.0.0.0/24"},
                },
            ],
            "edges": [{"source": "gw1", "target": "net1"}],
        }
        result = _find_gateway_connected_network(topo, "gw1")
        assert result["id"] == "net1"

    def test_no_edges(self):
        topo = {"nodes": [{"id": "gw1", "type": "gatewayNode"}], "edges": []}
        assert _find_gateway_connected_network(topo, "gw1") is None

    def test_target_not_network(self):
        topo = {
            "nodes": [
                {"id": "gw1", "type": "gatewayNode"},
                {"id": "vm1", "type": "vmNode"},
            ],
            "edges": [{"source": "gw1", "target": "vm1"}],
        }
        assert _find_gateway_connected_network(topo, "gw1") is None


# ═══════════════════════════════════════════════════════════════════════════
# _find_gateway_ip
# ═══════════════════════════════════════════════════════════════════════════


class TestFindGatewayIp:
    def test_finds_ip(self):
        topo = {
            "nodes": [
                {"id": "gw1", "type": "gatewayNode"},
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"cidr": "192.168.1.0/24"},
                },
            ],
            "edges": [{"source": "gw1", "target": "net1"}],
        }
        assert _find_gateway_ip(topo) == "192.168.1.1"

    def test_no_gateway(self):
        topo = {"nodes": [], "edges": []}
        assert _find_gateway_ip(topo) is None


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_inject_gateway_ip
# ═══════════════════════════════════════════════════════════════════════════


class TestDeployInjectGatewayIp:
    def test_injects_ip_into_cloud_init_vms(self):
        topo = {
            "nodes": [
                {"id": "gw1", "type": "gatewayNode"},
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"cidr": "10.0.0.0/24"},
                },
                {"id": "vm1", "type": "vmNode", "data": {"cloudInit": True}},
                {"id": "vm2", "type": "vmNode", "data": {"cloudInit": False}},
            ],
            "edges": [{"source": "gw1", "target": "net1"}],
        }
        _deploy_inject_gateway_ip(topo, PROJECT_ID)
        assert topo["nodes"][2]["data"]["gateway_ip"] == "10.0.0.1"
        assert "gateway_ip" not in topo["nodes"][3]["data"]


# ═══════════════════════════════════════════════════════════════════════════
# _detect_pattern_id
# ═══════════════════════════════════════════════════════════════════════════


class TestDetectPatternId:
    def test_finds_pattern_id(self):
        topo = {
            "nodes": [
                {
                    "id": "s1",
                    "type": "storageNode",
                    "data": {"patternId": "pat-123"},
                }
            ]
        }
        assert _detect_pattern_id(topo) == "pat-123"

    def test_no_pattern(self):
        topo = {"nodes": [{"id": "s1", "type": "storageNode", "data": {}}]}
        assert _detect_pattern_id(topo) is None

    def test_empty_topology(self):
        assert _detect_pattern_id({}) is None


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_validate_bmc
# ═══════════════════════════════════════════════════════════════════════════


class TestDeployValidateBmc:
    def test_no_bmc_network_returns_none(self):
        topo = {
            "nodes": [
                {"id": "n1", "type": "networkNode", "data": {"networkType": "data"}}
            ]
        }
        assert _deploy_validate_bmc(PROJECT_ID, topo) is None

    def test_missing_bmc_ip(self):
        topo = {
            "nodes": [
                {"id": "n1", "type": "networkNode", "data": {"networkType": "bmc"}},
                {
                    "id": "v1",
                    "type": "vmNode",
                    "data": {"bmcEnabled": True, "bmcIp": "", "name": "sno1"},
                },
            ]
        }
        result = _deploy_validate_bmc(PROJECT_ID, topo)
        assert "sno1" in result

    def test_all_bmc_ips_present(self):
        topo = {
            "nodes": [
                {"id": "n1", "type": "networkNode", "data": {"networkType": "bmc"}},
                {
                    "id": "v1",
                    "type": "vmNode",
                    "data": {"bmcEnabled": True, "bmcIp": "192.168.100.10"},
                },
            ]
        }
        assert _deploy_validate_bmc(PROJECT_ID, topo) is None


# ═══════════════════════════════════════════════════════════════════════════
# _detect_common_password
# ═══════════════════════════════════════════════════════════════════════════


class TestDetectCommonPassword:
    def test_finds_password(self):
        topo = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {
                        "cloudInit": True,
                        "ciCloudUserPassword": "secret123",
                    },
                }
            ]
        }
        assert _detect_common_password(topo) == "secret123"

    def test_no_password(self):
        topo = {"nodes": [{"type": "vmNode", "data": {"cloudInit": True}}]}
        assert _detect_common_password(topo) is None


# ═══════════════════════════════════════════════════════════════════════════
# _detect_recert_from_pattern
# ═══════════════════════════════════════════════════════════════════════════


class TestDetectRecertFromPattern:
    def test_pattern_with_recert(self):
        s = MagicMock()
        pat = MagicMock()
        pat.recert = True
        s.query.return_value.filter_by.return_value.first.return_value = pat
        topo = {"nodes": [{"type": "storageNode", "data": {"patternId": "pat-1"}}]}
        assert _detect_recert_from_pattern(s, topo) is True

    def test_no_pattern_id(self):
        s = MagicMock()
        topo = {"nodes": [{"type": "storageNode", "data": {}}]}
        assert _detect_recert_from_pattern(s, topo) is None


# ═══════════════════════════════════════════════════════════════════════════
# _resolve_recert_settings
# ═══════════════════════════════════════════════════════════════════════════


class TestResolveRecertSettings:
    def test_from_topology_markers(self):
        s = MagicMock()
        topo = {
            "_deploy_recert": True,
            "_deploy_common_password": "mypass",
            "nodes": [],
        }
        recert, pw = _resolve_recert_settings(s, topo)
        assert recert is True
        assert pw == "mypass"
        assert "_deploy_recert" not in topo
        assert "_deploy_common_password" not in topo

    @patch("app.services.deploy_service._detect_common_password", return_value="auto")
    @patch(
        "app.services.deploy_service._detect_recert_from_pattern", return_value=False
    )
    def test_from_detection(self, mock_recert, mock_pw):
        s = MagicMock()
        topo = {"nodes": []}
        recert, pw = _resolve_recert_settings(s, topo)
        assert recert is False
        assert pw == "auto"


# ═══════════════════════════════════════════════════════════════════════════
# _auto_enable_recert_on_rhcos
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoEnableRecertOnRhcos:
    def test_enables_on_rhcos(self):
        topo = {
            "nodes": [
                {"type": "vmNode", "data": {"os": "rhcos"}},
                {"type": "vmNode", "data": {"os": "rhel9"}},
            ]
        }
        _auto_enable_recert_on_rhcos(topo, True, PROJECT_ID)
        assert topo["nodes"][0]["data"]["recertEnabled"] is True
        assert "recertEnabled" not in topo["nodes"][1]["data"]

    def test_skips_if_already_has_recert(self):
        topo = {
            "nodes": [
                {"type": "vmNode", "data": {"os": "rhcos", "recertEnabled": True}},
            ]
        }
        _auto_enable_recert_on_rhcos(topo, True, PROJECT_ID)
        # Should not add a second time; the existing flag is preserved
        assert topo["nodes"][0]["data"]["recertEnabled"] is True

    def test_no_op_when_disabled(self):
        topo = {"nodes": [{"type": "vmNode", "data": {"os": "rhcos"}}]}
        _auto_enable_recert_on_rhcos(topo, False, PROJECT_ID)
        assert "recertEnabled" not in topo["nodes"][0]["data"]


# ═══════════════════════════════════════════════════════════════════════════
# _build_vm_progress_items
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildVmProgressItems:
    def test_items(self):
        vms = [
            {"node_id": "vm-1", "name": "bastion"},
            {"node_id": "vm-2", "name": "worker1"},
            {"node_id": "vm-3", "name": "worker2"},
        ]
        items = _build_vm_progress_items(vms, 1)
        assert items == [
            "bastion: defined",
            "worker1: defining...",
            "worker2: pending",
        ]


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_store_bmc_topology
# ═══════════════════════════════════════════════════════════════════════════


class TestDeployStoreBmcTopology:
    def test_stores_bmc_data(self):
        project = MagicMock()
        project.deployed_topology = {}
        topo = {
            "nodes": [
                {"id": "vm-1", "data": {"domainUuid": "uuid-1"}},
            ]
        }
        bmc_config = {
            "bmc_network": {"bmcUsername": "admin", "bmcPassword": "pass"},
            "vms": [
                {"node_id": "vm-1", "bmc_ip": "192.168.100.10", "domain_name": "dom1"}
            ],
        }
        _deploy_store_bmc_topology(project, topo, bmc_config)
        bmc = project.deployed_topology["bmc"]
        assert bmc["username"] == "admin"
        assert "vm-1" in bmc["vms"]
        assert "redfish_url" in bmc["vms"]["vm-1"]

    def test_no_bmc_config(self):
        project = MagicMock()
        project.deployed_topology = {}
        _deploy_store_bmc_topology(project, {}, None)
        assert "bmc" not in project.deployed_topology


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_finalize_timers
# ═══════════════════════════════════════════════════════════════════════════


class TestDeployFinalizeTimers:
    def test_sets_auto_stop(self):
        project = MagicMock()
        project.state = "active"
        project.auto_stop_minutes = 60
        project.auto_delete_minutes = None
        project.auto_delete_started_at = None
        _deploy_finalize_timers(project, True)
        assert project.auto_stop_started_at is not None
        assert project.auto_stop_expires_at is not None
        assert project.auto_stop_warned is False

    def test_sets_auto_delete(self):
        project = MagicMock()
        project.state = "active"
        project.auto_stop_minutes = None
        project.auto_delete_minutes = 120
        project.auto_delete_started_at = None
        _deploy_finalize_timers(project, True)
        assert project.auto_delete_started_at is not None
        assert project.lifetime_expires_at is not None

    def test_no_timers(self):
        project = MagicMock()
        project.state = "active"
        project.auto_stop_minutes = None
        project.auto_delete_minutes = None
        project.auto_delete_started_at = None
        _deploy_finalize_timers(project, True)
        # No assertions on times, just ensure no errors


# ═══════════════════════════════════════════════════════════════════════════
# _pull_pod_images
# ═══════════════════════════════════════════════════════════════════════════


class TestPullPodImages:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    @patch("app.services.deploy_service._add_registry_creds")
    def test_pulls_all_unique_images(self, mock_creds, mock_start, mock_wait):
        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}
        host = _make_host()
        ctr = {
            "init_containers": [{"image": "img1"}, {"image": "img2"}],
            "pod_containers": [{"image": "img2"}, {"image": "img3"}],
        }
        s = MagicMock()
        _pull_pod_images(host, ctr, s)
        # 3 unique images
        assert mock_start.call_count == 3


# ═══════════════════════════════════════════════════════════════════════════
# _pull_single_container_image
# ═══════════════════════════════════════════════════════════════════════════


class TestPullSingleContainerImage:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    @patch("app.services.deploy_service._add_registry_creds")
    def test_pulls_image(self, mock_creds, mock_start, mock_wait):
        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}
        host = _make_host()
        ctr = {"image": "quay.io/test/image:latest"}
        s = MagicMock()
        _pull_single_container_image(host, ctr, s)
        mock_start.assert_called_once()
        assert mock_start.call_args[0][1] == "/containers/pull"


# ═══════════════════════════════════════════════════════════════════════════
# _add_registry_creds
# ═══════════════════════════════════════════════════════════════════════════


class TestAddRegistryCreds:
    def test_no_cred_id(self):
        params = {"image": "test"}
        _add_registry_creds(params, {}, MagicMock())
        assert "registry" not in params

    @patch("app.core.encryption.decrypt", return_value="decrypted_pw")
    def test_adds_creds(self, mock_decrypt):
        cred = MagicMock()
        cred.registry_url = "quay.io"
        cred.username = "user"
        cred.password = "encrypted"
        s = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = cred
        params = {"image": "test"}
        ctr = {"registry_credential_id": "cred-1"}
        _add_registry_creds(params, ctr, s)
        assert params["registry"] == "quay.io"
        assert params["username"] == "user"
        assert params["password"] == "decrypted_pw"


# ═══════════════════════════════════════════════════════════════════════════
# _clean_stale_domain
# ═══════════════════════════════════════════════════════════════════════════


class TestCleanStaleDomain:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    def test_removes_stale_domain(self, mock_start, mock_wait):
        mock_start.side_effect = ["check-job", "destroy-job"]
        mock_wait.side_effect = [
            {"result": {"state": "shutoff"}},
            {"status": "completed"},
        ]
        host = _make_host()
        _clean_stale_domain(host, PROJECT_ID, "troshka-test-vm")
        assert mock_start.call_count == 2

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    def test_no_stale_domain(self, mock_start, mock_wait):
        mock_start.return_value = "check-job"
        mock_wait.return_value = {"result": {}}
        host = _make_host()
        _clean_stale_domain(host, PROJECT_ID, "troshka-test-vm")
        assert mock_start.call_count == 1

    @patch("app.services.deploy_service.start_job", side_effect=TroshkadError("err"))
    def test_troshkad_error(self, mock_start):
        host = _make_host()
        # Should not raise
        _clean_stale_domain(host, PROJECT_ID, "troshka-test-vm")


# ═══════════════════════════════════════════════════════════════════════════
# _clean_stale_domains (multihost)
# ═══════════════════════════════════════════════════════════════════════════


class TestCleanStaleDomains:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    @patch("app.services.deploy_service._update_deploy_progress")
    def test_cleans_stale_domains_for_host_vms(self, mock_prog, mock_start, mock_wait):
        mock_start.return_value = "j1"
        mock_wait.return_value = {"result": {}}  # no stale domain
        host = _make_host()
        host_vms = [{"node_id": "vm-node-1"}, {"node_id": "vm-node-2"}]
        _clean_stale_domains(host, PROJECT_ID, host_vms, "10.0.0.1")
        # One start_job per VM to check
        assert mock_start.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# _create_multihost_disks
# ═══════════════════════════════════════════════════════════════════════════


class TestCreateMultihostDisks:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service._create_vm_disks_via_troshkad")
    @patch("app.services.deploy_service._find_vm_disks")
    @patch("app.services.deploy_service._update_deploy_progress")
    def test_success(self, mock_prog, mock_find, mock_create, mock_wait):
        mock_find.return_value = [{"node_id": "d1", "format": "qcow2"}]
        mock_create.return_value = ["disk-j1"]
        mock_wait.return_value = {"status": "completed"}
        host = _make_host()
        host_vms = [{"node_id": "vm-1"}]
        result = _create_multihost_disks(
            host, PROJECT_ID, host_vms, {"nodes": []}, None, "label"
        )
        assert result is None

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service._create_vm_disks_via_troshkad")
    @patch("app.services.deploy_service._find_vm_disks")
    @patch("app.services.deploy_service._update_deploy_progress")
    def test_failure(self, mock_prog, mock_find, mock_create, mock_wait):
        mock_find.return_value = []
        mock_create.return_value = ["disk-j1"]
        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "no space"},
        }
        host = _make_host()
        host_vms = [{"node_id": "vm-1"}]
        result = _create_multihost_disks(
            host, PROJECT_ID, host_vms, {"nodes": []}, None, "label"
        )
        assert "no space" in result


# ═══════════════════════════════════════════════════════════════════════════
# _define_multihost_vms
# ═══════════════════════════════════════════════════════════════════════════


class TestDefineMultihostVms:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service._create_vm_via_troshkad")
    def test_success_with_uuid(self, mock_create, mock_wait):
        mock_create.return_value = "vm-job-1"
        mock_wait.return_value = {
            "status": "completed",
            "result": {"domain_uuid": "uuid-123"},
        }
        host = _make_host()
        topo = {"nodes": [{"id": "vm-1", "data": {}}]}
        host_vms = [{"node_id": "vm-1"}]
        result = _define_multihost_vms(
            host, PROJECT_ID, host_vms, topo, {}, None, None, "label"
        )
        assert result is None
        assert topo["nodes"][0]["data"]["domainUuid"] == "uuid-123"

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service._create_vm_via_troshkad")
    def test_failure(self, mock_create, mock_wait):
        mock_create.return_value = "vm-job-1"
        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "virt-install crash"},
        }
        host = _make_host()
        host_vms = [{"node_id": "vm-1"}]
        result = _define_multihost_vms(
            host, PROJECT_ID, host_vms, {"nodes": []}, {}, None, None, "label"
        )
        assert "virt-install crash" in result

    @patch("app.services.deploy_service._create_vm_via_troshkad", return_value=None)
    def test_no_job_id(self, mock_create):
        host = _make_host()
        host_vms = [{"node_id": "vm-1"}]
        result = _define_multihost_vms(
            host, PROJECT_ID, host_vms, {"nodes": []}, {}, None, None, "label"
        )
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_pull_container_images
# ═══════════════════════════════════════════════════════════════════════════


class TestDeployPullContainerImages:
    @patch("app.services.deploy_service._pull_single_container_image")
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._detect_pattern_id", return_value=None)
    @patch("app.services.deploy_service._is_pattern_deploy", return_value=False)
    @patch(
        "app.services.deploy_service._extract_containers",
        return_value=[{"node_id": "c1", "image": "nginx", "is_pod": False}],
    )
    def test_pulls_single_container(
        self, mock_extract, mock_pattern, mock_detect, mock_prog, mock_pull
    ):
        host = _make_host()
        s = MagicMock()
        _deploy_pull_container_images(host, PROJECT_ID, {"nodes": []}, s)
        mock_pull.assert_called_once()

    @patch("app.services.deploy_service._update_deploy_progress")
    @patch(
        "app.services.deploy_service._extract_containers",
        return_value=[],
    )
    def test_no_containers(self, mock_extract, mock_prog):
        host = _make_host()
        s = MagicMock()
        _deploy_pull_container_images(host, PROJECT_ID, {"nodes": []}, s)
        # Should return early

    @patch("app.services.deploy_service._pull_pod_images")
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._detect_pattern_id", return_value=None)
    @patch("app.services.deploy_service._is_pattern_deploy", return_value=False)
    @patch(
        "app.services.deploy_service._extract_containers",
        return_value=[{"node_id": "p1", "image": "", "is_pod": True}],
    )
    def test_pulls_pod(
        self, mock_extract, mock_pattern, mock_detect, mock_prog, mock_pull
    ):
        host = _make_host()
        s = MagicMock()
        _deploy_pull_container_images(host, PROJECT_ID, {"nodes": []}, s)
        mock_pull.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_create_containers
# ═══════════════════════════════════════════════════════════════════════════


class TestDeployCreateContainers:
    @patch("app.services.deploy_service._create_and_start_container")
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch(
        "app.services.deploy_service._extract_containers",
        return_value=[
            {"node_id": "c1", "image": "nginx", "is_pod": False, "name": "c1"}
        ],
    )
    def test_creates_unordered_container(self, mock_extract, mock_prog, mock_create):
        host = _make_host()
        topo = {"nodes": [], "startOrder": []}
        _deploy_create_containers(host, PROJECT_ID, topo, {}, None)
        mock_create.assert_called_once()

    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._extract_containers", return_value=[])
    def test_no_containers_returns_early(self, mock_extract, mock_prog):
        host = _make_host()
        _deploy_create_containers(host, PROJECT_ID, {"nodes": []}, {}, None)


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_setup_lb
# ═══════════════════════════════════════════════════════════════════════════


class TestDeploySetupLb:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch(
        "app.services.vxlan.build_host_network_config",
        return_value={
            "loadbalancer": {
                "frontends": [{"bindPort": 6443}],
                "backends": [{"port": 6443}],
                "lb_ip": "10.0.0.5",
            },
            "networks": [],
        },
    )
    def test_sets_up_lb(self, mock_build, mock_prog, mock_start, mock_wait):
        mock_start.return_value = "lb-job"
        mock_wait.return_value = {"status": "completed"}
        host = _make_host()
        result = _deploy_setup_lb(host, PROJECT_ID, {}, {})
        assert result is not None
        assert result["frontends"][0]["bindPort"] == 6443

    @patch(
        "app.services.vxlan.build_host_network_config",
        return_value={"loadbalancer": None, "networks": []},
    )
    def test_no_lb(self, mock_build):
        host = _make_host()
        result = _deploy_setup_lb(host, PROJECT_ID, {}, {})
        assert result is None

    @patch("app.services.deploy_service.start_job", side_effect=TroshkadError("fail"))
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch(
        "app.services.vxlan.build_host_network_config",
        return_value={
            "loadbalancer": {
                "frontends": [{"bindPort": 80}],
                "backends": [],
                "lb_ip": "10.0.0.5",
            },
            "networks": [],
        },
    )
    def test_lb_troshkad_error(self, mock_build, mock_prog, mock_start):
        host = _make_host()
        # Should not raise
        result = _deploy_setup_lb(host, PROJECT_ID, {}, {})
        assert result is not None


# ═══════════════════════════════════════════════════════════════════════════
# _get_connected_host
# ═══════════════════════════════════════════════════════════════════════════


class TestGetConnectedHost:
    def test_connected_host(self):
        host = _make_host()
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = host
        result = _get_connected_host(session, HOST_ID)
        assert result is host

    def test_disconnected_host(self):
        host = _make_host()
        host.agent_status = "disconnected"
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = host
        assert _get_connected_host(session, HOST_ID) is None

    def test_host_not_found(self):
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = None
        assert _get_connected_host(session, HOST_ID) is None


# ═══════════════════════════════════════════════════════════════════════════
# _destroy_stop_all_vms
# ═══════════════════════════════════════════════════════════════════════════


class TestDestroyStopAllVms:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    @patch("app.services.deploy_service._get_connected_host")
    def test_stops_vms(self, mock_get_host, mock_start, mock_wait):
        host = _make_host()
        mock_get_host.return_value = host
        mock_start.return_value = "stop-job"
        mock_wait.return_value = {"status": "completed"}
        session = MagicMock()
        _destroy_stop_all_vms(session, PROJECT_ID, {HOST_ID})
        mock_start.assert_called_once_with(
            host, "/vms/stop-all", {"project_id": PROJECT_ID}
        )

    @patch("app.services.deploy_service._get_connected_host", return_value=None)
    def test_host_not_connected(self, mock_get):
        session = MagicMock()
        # Should not raise
        _destroy_stop_all_vms(session, PROJECT_ID, {HOST_ID})

    @patch("app.services.deploy_service.start_job", side_effect=Exception("err"))
    @patch("app.services.deploy_service._get_connected_host")
    def test_exception_logged(self, mock_get, mock_start):
        mock_get.return_value = _make_host()
        session = MagicMock()
        _destroy_stop_all_vms(session, PROJECT_ID, {HOST_ID})


# ═══════════════════════════════════════════════════════════════════════════
# _destroy_remote_networks
# ═══════════════════════════════════════════════════════════════════════════


class TestDestroyRemoteNetworks:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    @patch("app.services.deploy_service._get_connected_host")
    def test_tears_down_remote_only(self, mock_get, mock_start, mock_wait):
        host = _make_host()
        mock_get.return_value = host
        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}
        session = MagicMock()
        _destroy_remote_networks(session, PROJECT_ID, {"h1", "h2"}, "h1", {"n": 100})
        # Only h2 should be torn down (h1 is network host)
        mock_start.assert_called_once()

    @patch("app.services.deploy_service._get_connected_host", return_value=None)
    def test_skips_disconnected(self, mock_get):
        session = MagicMock()
        _destroy_remote_networks(session, PROJECT_ID, {"h1", "h2"}, "h1", {"n": 100})


# ═══════════════════════════════════════════════════════════════════════════
# _destroy_mesh_on_all_hosts
# ═══════════════════════════════════════════════════════════════════════════


class TestDestroyMeshOnAllHosts:
    @patch("app.services.deploy_service.troshkad_request")
    @patch("app.services.deploy_service._get_connected_host")
    def test_tears_down_mesh(self, mock_get, mock_request):
        host = _make_host()
        mock_get.return_value = host
        session = MagicMock()
        _destroy_mesh_on_all_hosts(session, PROJECT_ID, {HOST_ID})
        mock_request.assert_called_once()

    @patch(
        "app.services.deploy_service.troshkad_request",
        side_effect=Exception("err"),
    )
    @patch("app.services.deploy_service._get_connected_host")
    def test_exception_logged(self, mock_get, mock_request):
        mock_get.return_value = _make_host()
        session = MagicMock()
        # Should not raise
        _destroy_mesh_on_all_hosts(session, PROJECT_ID, {HOST_ID})


# ═══════════════════════════════════════════════════════════════════════════
# _destroy_cleanup_dns
# ═══════════════════════════════════════════════════════════════════════════


class TestDestroyCleanupDns:
    def test_no_dns_provider(self):
        s = MagicMock()
        ctx = {"dns_provider_id": None}
        _destroy_cleanup_dns(s, ctx, PROJECT_ID)
        s.query.assert_not_called()

    @patch("app.services.dns_service.delete_dns_records")
    def test_deletes_records(self, mock_delete):
        s = MagicMock()
        dns_prov = MagicMock()
        dns_prov.type = "route53"
        dns_prov.config = {"zone_id": "Z123"}
        s.query.return_value.filter_by.return_value.first.return_value = dns_prov
        ctx = {
            "dns_provider_id": "dns-1",
            "topology": {
                "_dns_records": [{"name": "api.test.com", "value": "1.2.3.4"}]
            },
        }
        _destroy_cleanup_dns(s, ctx, PROJECT_ID)
        mock_delete.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# _destroy_cleanup_eips
# ═══════════════════════════════════════════════════════════════════════════


class TestDestroyCleanupEips:
    @patch("app.services.eip_service.release_eip")
    def test_releases_eips(self, mock_release):
        eip1 = MagicMock()
        eip1.public_ip = "1.2.3.4"
        eip2 = MagicMock()
        eip2.public_ip = "5.6.7.8"
        s = MagicMock()
        s.query.return_value.filter_by.return_value.all.return_value = [eip1, eip2]
        _destroy_cleanup_eips(s, PROJECT_ID)
        assert mock_release.call_count == 2

    @patch("app.services.eip_service.release_eip", side_effect=Exception("AWS err"))
    def test_continues_on_error(self, mock_release):
        eip = MagicMock()
        eip.public_ip = "1.2.3.4"
        s = MagicMock()
        s.query.return_value.filter_by.return_value.all.return_value = [eip]
        # Should not raise
        _destroy_cleanup_eips(s, PROJECT_ID)


# ═══════════════════════════════════════════════════════════════════════════
# _set_deploy_error
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import _set_deploy_error


class TestSetDeployError:
    def test_sets_state_and_commits(self):
        s = MagicMock()
        project = MagicMock()
        _set_deploy_error(s, project, "something broke")
        assert project.state == "error"
        assert project.deploy_error == "something broke"
        s.commit.assert_called_once()

    def test_empty_error_message(self):
        s = MagicMock()
        project = MagicMock()
        _set_deploy_error(s, project, "")
        assert project.state == "error"
        assert project.deploy_error == ""
        s.commit.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# _set_deploy_error_and_notify
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import _set_deploy_error_and_notify


class TestSetDeployErrorAndNotify:
    @patch("app.services.deploy_service.notify_project")
    def test_sets_state_and_notifies(self, mock_notify):
        s = MagicMock()
        project = MagicMock()
        _set_deploy_error_and_notify(s, PROJECT_ID, project, "disk full")
        assert project.state == "error"
        assert project.deploy_error == "disk full"
        s.commit.assert_called_once()
        mock_notify.assert_called_once_with(
            PROJECT_ID,
            {"type": "project-state", "state": "error", "deploy_error": "disk full"},
        )


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_resolve_host
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import _deploy_resolve_host


class TestDeployResolveHost:
    def test_existing_host_with_ip(self):
        s = MagicMock()
        host = _make_host()
        s.query.return_value.filter_by.return_value.first.return_value = host
        project = MagicMock()
        project.host_id = HOST_ID
        result_host, err = _deploy_resolve_host(s, project, PROJECT_ID)
        assert result_host is host
        assert err is None

    def test_existing_host_no_ip(self):
        s = MagicMock()
        host = _make_host()
        host.ip_address = None
        s.query.return_value.filter_by.return_value.first.return_value = host
        project = MagicMock()
        project.host_id = HOST_ID
        result_host, err = _deploy_resolve_host(s, project, PROJECT_ID)
        assert result_host is host
        assert err is not None
        assert "no IP address" in err

    def test_host_id_set_but_not_found(self):
        s = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = None
        project = MagicMock()
        project.host_id = "nonexistent"
        result_host, err = _deploy_resolve_host(s, project, PROJECT_ID)
        assert result_host is None
        assert err is not None
        assert "no longer exists" in err

    @patch("app.services.placement.find_available_host")
    @patch("app.services.placement.calculate_project_requirements")
    def test_auto_placement_success(self, mock_reqs, mock_find):
        s = MagicMock()
        host = _make_host()
        mock_reqs.return_value = {"total_vcpus": 4, "total_ram_mb": 8192}
        mock_find.return_value = host
        project = MagicMock()
        project.host_id = None
        project.topology = {"nodes": []}
        result_host, err = _deploy_resolve_host(s, project, PROJECT_ID)
        assert result_host is host
        assert err is None
        assert project.host_id == HOST_ID

    @patch(
        "app.services.placement.diagnose_placement_failure",
        return_value="Not enough RAM — need 512.0 GB",
    )
    @patch("app.services.placement.find_available_host")
    @patch("app.services.placement.calculate_project_requirements")
    def test_auto_placement_no_capacity(self, mock_reqs, mock_find, mock_diag):
        s = MagicMock()
        mock_reqs.return_value = {"total_vcpus": 64, "total_ram_mb": 524288}
        mock_find.return_value = None
        project = MagicMock()
        project.host_id = None
        project.topology = {"nodes": []}
        result_host, err = _deploy_resolve_host(s, project, PROJECT_ID)
        assert result_host is None
        assert err == "Not enough RAM — need 512.0 GB"


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_host_error_msg
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import _deploy_host_error_msg


class TestDeployHostErrorMsg:
    @patch(
        "app.services.placement.diagnose_placement_failure",
        return_value="Pattern disks are not available on any host",
    )
    def test_no_host_id_uses_placement_diagnostic(self, mock_diag):
        s = MagicMock()
        project = MagicMock()
        project.host_id = None
        project.topology = {}
        project.provider_id = None
        msg = _deploy_host_error_msg(s, project, None)
        assert msg == "Pattern disks are not available on any host"
        mock_diag.assert_called_once()

    def test_host_id_set_but_host_none(self):
        s = MagicMock()
        project = MagicMock()
        project.host_id = "some-id"
        msg = _deploy_host_error_msg(s, project, None)
        assert "no longer exists" in msg

    def test_host_exists_but_no_ip(self):
        s = MagicMock()
        project = MagicMock()
        project.host_id = HOST_ID
        host = _make_host()
        host.ip_address = None
        msg = _deploy_host_error_msg(s, project, host)
        assert "no IP address" in msg


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_init_context
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import _deploy_init_context


class TestDeployInitContext:
    def test_no_clock_target_existing_vni_map(self):
        s = MagicMock()
        project = MagicMock()
        project.clock_target = None
        project.topology = {"nodes": []}
        project.vni_map = {"net-1": 100}
        topo, offset, vni_map = _deploy_init_context(s, project, PROJECT_ID)
        assert offset is None
        assert vni_map == {"net-1": 100}
        assert topo == {"nodes": []}

    @patch("app.services.clock_service.compute_clock_offset")
    def test_with_clock_target(self, mock_offset):
        mock_offset.return_value = -3600
        s = MagicMock()
        project = MagicMock()
        project.clock_target = "2025-01-01T00:00:00Z"
        project.topology = {"nodes": []}
        project.vni_map = {"net-1": 100}
        topo, offset, vni_map = _deploy_init_context(s, project, PROJECT_ID)
        assert offset == -3600
        mock_offset.assert_called_once_with("2025-01-01T00:00:00Z")

    @patch("app.services.vxlan.allocate_vnis_for_project")
    def test_allocates_vnis_when_empty(self, mock_alloc):
        mock_alloc.return_value = {"net-1": 200}
        s = MagicMock()
        project = MagicMock()
        project.clock_target = None
        project.topology = {"nodes": []}
        project.vni_map = {}
        topo, offset, vni_map = _deploy_init_context(s, project, PROJECT_ID)
        assert vni_map == {"net-1": 200}
        assert project.vni_map == {"net-1": 200}
        s.commit.assert_called_once()

    def test_none_topology_defaults_to_empty_dict(self):
        s = MagicMock()
        project = MagicMock()
        project.clock_target = None
        project.topology = None
        project.vni_map = {"net-1": 100}
        topo, offset, vni_map = _deploy_init_context(s, project, PROJECT_ID)
        assert topo == {}


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_disable_guest_exec
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import _deploy_disable_guest_exec


class TestDeployDisableGuestExec:
    def test_enabled_does_nothing(self):
        project = MagicMock()
        project.guest_exec_enabled = True
        topology = {
            "nodes": [
                {"type": "vmNode", "data": {"cloudInit": True}},
            ]
        }
        _deploy_disable_guest_exec(project, topology)
        assert "guestExecEnabled" not in topology["nodes"][0]["data"]

    def test_disabled_marks_cloud_init_vms(self):
        project = MagicMock()
        project.guest_exec_enabled = False
        topology = {
            "nodes": [
                {"type": "vmNode", "data": {"cloudInit": True}},
                {"type": "vmNode", "data": {"cloudInit": False}},
                {"type": "networkNode", "data": {}},
            ]
        }
        _deploy_disable_guest_exec(project, topology)
        assert topology["nodes"][0]["data"]["guestExecEnabled"] is False
        # Non-cloud-init VM should not be touched
        assert "guestExecEnabled" not in topology["nodes"][1]["data"]

    def test_no_nodes(self):
        project = MagicMock()
        project.guest_exec_enabled = False
        topology = {"nodes": []}
        _deploy_disable_guest_exec(project, topology)  # should not raise


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_cache_images_and_pxe
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import _deploy_cache_images_and_pxe


class TestDeployCacheImagesAndPxe:
    @patch("app.services.deploy_service._setup_pxe_via_troshkad")
    @patch("app.services.deploy_service.cache_library_images")
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._checkpoint")
    def test_calls_cache_and_pxe(self, mock_cp, mock_prog, mock_cache, mock_pxe):
        host = _make_host()
        topology = {"nodes": []}
        vni_map = {"net-1": 100}
        s = MagicMock()
        _deploy_cache_images_and_pxe(host, PROJECT_ID, topology, vni_map, s)
        mock_cp.assert_called_once_with(s, PROJECT_ID, "images")
        mock_cache.assert_called_once()
        mock_pxe.assert_called_once_with(host, topology, vni_map, PROJECT_ID)


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_create_bmc_bridge
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import _deploy_create_bmc_bridge


class TestDeployCreateBmcBridge:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    @patch("app.services.deploy_service._extract_bmc_config")
    def test_creates_bridge_when_bmc_config_present(
        self, mock_extract, mock_start, mock_wait
    ):
        mock_extract.return_value = {
            "bmc_network": {"cidr": "192.168.100.0/24"},
            "vms": [{"bmc_ip": "192.168.100.10"}],
        }
        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}
        host = _make_host()
        _deploy_create_bmc_bridge(host, PROJECT_ID, {"nodes": []})
        mock_start.assert_called_once()
        call_args = mock_start.call_args[0]
        assert call_args[1] == "/bmc/create-bridge"
        assert call_args[2]["bmc_cidr"] == "192.168.100.0/24"
        assert call_args[2]["bmc_gateway_ip"] == "192.168.100.1"

    @patch("app.services.deploy_service._extract_bmc_config")
    def test_skips_when_no_bmc_config(self, mock_extract):
        mock_extract.return_value = None
        host = _make_host()
        _deploy_create_bmc_bridge(host, PROJECT_ID, {"nodes": []})
        # Should not raise, nothing to do


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_single_host_setup
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import _deploy_single_host_setup


class TestDeploySingleHostSetup:
    @patch("app.services.deploy_service._deploy_create_bmc_bridge")
    @patch("app.services.deploy_service._deploy_validate_bmc")
    @patch("app.services.deploy_service._deploy_pull_container_images")
    @patch("app.services.deploy_service._deploy_cache_images_and_pxe")
    @patch("app.services.deploy_service._project_deleted", return_value=False)
    @patch("app.services.deploy_service._deploy_create_ocpvirt_routes")
    @patch("app.services.deploy_service._deploy_disable_guest_exec")
    @patch("app.services.deploy_service._deploy_inject_gateway_ip")
    @patch("app.services.deploy_service._deploy_sync_sg_rules")
    @patch("app.services.deploy_service._deploy_setup_lb")
    @patch(
        "app.services.deploy_service._setup_networks_via_troshkad", return_value=True
    )
    @patch("app.services.deploy_service._get_network_lock")
    @patch("app.services.deploy_service._auto_assign_container_ips")
    @patch("app.services.deploy_service._deploy_allocate_eips")
    @patch("app.services.deploy_service._setup_metadata_via_troshkad")
    @patch("app.services.deploy_service._create_seed_isos_via_troshkad")
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._checkpoint")
    @patch("app.services.deploy_service._should_skip", return_value=False)
    def test_success_no_eips(
        self,
        mock_skip,
        mock_cp,
        mock_prog,
        mock_seeds,
        mock_meta,
        mock_alloc_eips,
        mock_auto_ips,
        mock_lock,
        mock_net,
        mock_lb,
        mock_sg,
        mock_gw_ip,
        mock_guest_exec,
        mock_routes,
        mock_deleted,
        mock_cache,
        mock_pull,
        mock_bmc_validate,
        mock_bmc_bridge,
    ):
        mock_bmc_validate.return_value = None
        mock_lb.return_value = None
        s = MagicMock()
        project = MagicMock()
        host = _make_host()
        topology = {"nodes": [], "externalIps": []}
        result = _deploy_single_host_setup(
            s, project, host, topology, {}, PROJECT_ID, None, None
        )
        assert result is not None
        assert result["lb_config"] is None
        assert result["external_ips"] == []
        # EIP allocation should not be called since no external IPs
        mock_alloc_eips.assert_not_called()

    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._set_deploy_error")
    @patch("app.services.deploy_service._get_network_lock")
    @patch("app.services.deploy_service._setup_networks_via_troshkad")
    @patch("app.services.deploy_service._auto_assign_container_ips")
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._checkpoint")
    @patch("app.services.deploy_service._should_skip", return_value=False)
    def test_network_failure_returns_none(
        self,
        mock_skip,
        mock_cp,
        mock_prog,
        mock_auto_ips,
        mock_net,
        mock_lock,
        mock_err,
        mock_del,
    ):
        mock_net.return_value = "nftables failed"
        s = MagicMock()
        project = MagicMock()
        host = _make_host()
        topology = {"nodes": [], "externalIps": []}
        result = _deploy_single_host_setup(
            s, project, host, topology, {}, PROJECT_ID, None, None
        )
        assert result is None
        mock_err.assert_called_once()

    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._set_deploy_error")
    @patch("app.services.deploy_service._deploy_allocate_eips")
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._checkpoint")
    @patch("app.services.deploy_service._should_skip", return_value=False)
    def test_eip_allocation_error(
        self, mock_skip, mock_cp, mock_prog, mock_alloc, mock_err, mock_del
    ):
        mock_alloc.return_value = "No EIPs available"
        s = MagicMock()
        project = MagicMock()
        host = _make_host()
        topology = {"nodes": [], "externalIps": [{"id": "eip-1"}]}
        result = _deploy_single_host_setup(
            s, project, host, topology, {}, PROJECT_ID, None, None
        )
        assert result is None
        mock_err.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_single_host_execute
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import _deploy_single_host_execute


class TestDeploySingleHostExecute:
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._deploy_complete_and_notify")
    @patch("app.services.deploy_service._deploy_start_vms", return_value=True)
    @patch("app.services.deploy_service._project_deleted", return_value=False)
    @patch("app.services.deploy_service._deploy_create_containers")
    @patch("app.services.deploy_service._deploy_setup_bmc")
    @patch("app.services.deploy_service._deploy_define_vms")
    @patch("app.services.deploy_service._deploy_handle_recert")
    @patch("app.services.deploy_service._deploy_create_disks")
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._checkpoint")
    def test_success_path(
        self,
        mock_cp,
        mock_prog,
        mock_disks,
        mock_recert,
        mock_define,
        mock_bmc,
        mock_containers,
        mock_deleted,
        mock_start,
        mock_complete,
        mock_del,
    ):
        mock_disks.return_value = [{"node_id": "vm-1", "domain_name": "dom1"}]
        mock_bmc.return_value = (None, None)  # no error, no bmc_config
        s = MagicMock()
        project = MagicMock()
        host = _make_host()
        topology = {"nodes": []}
        _deploy_single_host_execute(
            s, host, PROJECT_ID, project, topology, {}, None, None, None, True, None, []
        )
        mock_complete.assert_called_once()
        mock_start.assert_called_once()

    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._set_deploy_error")
    @patch("app.services.deploy_service._deploy_setup_bmc")
    @patch("app.services.deploy_service._deploy_define_vms")
    @patch("app.services.deploy_service._deploy_handle_recert")
    @patch("app.services.deploy_service._deploy_create_disks")
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._checkpoint")
    def test_bmc_error_returns_early(
        self,
        mock_cp,
        mock_prog,
        mock_disks,
        mock_recert,
        mock_define,
        mock_bmc,
        mock_err,
        mock_del,
    ):
        mock_disks.return_value = [{"node_id": "vm-1"}]
        mock_bmc.return_value = ("BMC setup failed", None)
        s = MagicMock()
        project = MagicMock()
        host = _make_host()
        _deploy_single_host_execute(
            s, host, PROJECT_ID, project, {}, {}, None, None, None, True, None, []
        )
        mock_err.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_complete_and_notify
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import _deploy_complete_and_notify


class TestDeployCompleteAndNotify:
    @patch("app.services.deploy_service._has_ocp_monitor", return_value=False)
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._deploy_store_bmc_topology")
    @patch("app.services.deploy_service._deploy_create_dns_records")
    @patch("app.services.deploy_service._deploy_finalize_timers")
    @patch("app.services.deploy_service.notify_project")
    def test_active_state_when_auto_start(
        self, mock_notify, mock_timers, mock_dns, mock_bmc, mock_del, mock_ocp
    ):
        s = MagicMock()
        project = MagicMock()
        project.auto_stop_expires_at = None
        project.lifetime_expires_at = None
        vms = [{"node_id": "vm-1"}, {"node_id": "vm-2"}]
        _deploy_complete_and_notify(
            s, PROJECT_ID, project, {}, vms, None, [], True, None
        )
        assert project.state == "active"
        assert project.deploy_error is None
        s.commit.assert_called_once()
        # Two notifications: project-state + vm-state
        assert mock_notify.call_count == 2

    @patch("app.services.deploy_service._has_ocp_monitor", return_value=False)
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._deploy_store_bmc_topology")
    @patch("app.services.deploy_service._deploy_create_dns_records")
    @patch("app.services.deploy_service._deploy_finalize_timers")
    @patch("app.services.deploy_service.notify_project")
    def test_stopped_state_when_no_auto_start(
        self, mock_notify, mock_timers, mock_dns, mock_bmc, mock_del, mock_ocp
    ):
        s = MagicMock()
        project = MagicMock()
        project.auto_stop_expires_at = None
        project.lifetime_expires_at = None
        _deploy_complete_and_notify(
            s, PROJECT_ID, project, {}, [], None, [], False, None
        )
        assert project.state == "stopped"

    @patch("app.services.deploy_service._has_ocp_monitor", return_value=True)
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._deploy_store_bmc_topology")
    @patch("app.services.deploy_service._deploy_create_dns_records")
    @patch("app.services.deploy_service._deploy_finalize_timers")
    @patch("app.services.deploy_service.notify_project")
    def test_ocp_monitor_enabled(
        self, mock_notify, mock_timers, mock_dns, mock_bmc, mock_del, mock_ocp
    ):
        s = MagicMock()
        project = MagicMock()
        project.auto_stop_expires_at = None
        project.lifetime_expires_at = None
        _deploy_complete_and_notify(
            s, PROJECT_ID, project, {}, [], None, [], True, None
        )
        assert project.ocp_status == "monitoring"
        assert project.ocp_status_detail is None
        assert project.ocp_install_elapsed is None
        # Two commits: one for state, one for ocp_monitor
        assert s.commit.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_handle_failure
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import _deploy_handle_failure


class TestDeployHandleFailure:
    @patch("app.services.deploy_service.notify_project")
    @patch("app.services.deploy_service._cleanup_stale_shared_cache")
    @patch("app.services.deploy_service._delete_deploy_progress")
    def test_sets_error_state_and_notifies(self, mock_del, mock_cache, mock_notify):
        s = MagicMock()
        project = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = project
        exc = RuntimeError("disk full")
        _deploy_handle_failure(s, PROJECT_ID, exc)
        assert project.state == "error"
        assert project.deploy_error == "disk full"
        mock_cache.assert_called_once_with(s, project)
        mock_notify.assert_called_once()
        s.commit.assert_called_once()
        mock_del.assert_called_once_with(PROJECT_ID)

    @patch("app.services.deploy_service._delete_deploy_progress")
    def test_project_not_found(self, mock_del):
        s = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = None
        _deploy_handle_failure(s, PROJECT_ID, RuntimeError("err"))
        # Should not raise, just delete progress
        mock_del.assert_called_once()

    @patch("app.services.deploy_service._delete_deploy_progress")
    def test_inner_exception_swallowed(self, mock_del):
        s = MagicMock()
        s.query.side_effect = Exception("DB gone")
        # Should not raise
        _deploy_handle_failure(s, PROJECT_ID, RuntimeError("orig"))
        mock_del.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# _setup_remote_host_network
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import _setup_remote_host_network


class TestSetupRemoteHostNetwork:
    def test_skips_network_host(self):
        result = _setup_remote_host_network(
            HOST_ID, HOST_ID, [], {}, [], {}, PROJECT_ID, MagicMock()
        )
        assert result is None

    def test_host_not_found(self):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        result = _setup_remote_host_network(
            HOST_ID, HOST_ID_2, [], {}, [], {HOST_ID: "10.0.0.1"}, PROJECT_ID, db
        )
        assert result is not None
        assert "not found" in result

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    def test_success(self, mock_start, mock_wait):
        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}
        db = MagicMock()
        host = _make_host()
        db.query.return_value.filter_by.return_value.first.return_value = host
        network_nodes = [{"id": "net-1", "type": "networkNode", "data": {}}]
        vni_map = {"net-1": 100}
        result = _setup_remote_host_network(
            HOST_ID,
            HOST_ID_2,
            network_nodes,
            vni_map,
            ["10.0.0.1", "10.0.0.2"],
            {HOST_ID: "10.0.0.1"},
            PROJECT_ID,
            db,
        )
        assert result is None

    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    def test_job_failed(self, mock_start, mock_wait):
        mock_start.return_value = "job-1"
        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "bridge failed"},
        }
        db = MagicMock()
        host = _make_host()
        db.query.return_value.filter_by.return_value.first.return_value = host
        result = _setup_remote_host_network(
            HOST_ID,
            HOST_ID_2,
            [{"id": "net-1"}],
            {"net-1": 100},
            ["10.0.0.1"],
            {HOST_ID: "10.0.0.1"},
            PROJECT_ID,
            db,
        )
        assert result is not None
        assert "bridge failed" in result

    @patch("app.services.deploy_service.start_job", side_effect=Exception("timeout"))
    def test_exception_returns_error(self, mock_start):
        db = MagicMock()
        host = _make_host()
        db.query.return_value.filter_by.return_value.first.return_value = host
        result = _setup_remote_host_network(
            HOST_ID,
            HOST_ID_2,
            [{"id": "net-1"}],
            {"net-1": 100},
            ["10.0.0.1"],
            {HOST_ID: "10.0.0.1"},
            PROJECT_ID,
            db,
        )
        assert result is not None
        assert "timeout" in result


# ═══════════════════════════════════════════════════════════════════════════
# _setup_remote_networks
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import _setup_remote_networks


class TestSetupRemoteNetworks:
    @patch("app.services.deploy_service._setup_remote_host_network", return_value=None)
    def test_success_all_hosts(self, mock_setup):
        db = MagicMock()
        peer1 = MagicMock()
        peer1.host_id = HOST_ID
        peer1.wg_address = "10.99.0.1/32"
        peer2 = MagicMock()
        peer2.host_id = HOST_ID_2
        peer2.wg_address = "10.99.0.2/32"
        db.query.return_value.filter_by.return_value.all.return_value = [peer1, peer2]
        project = MagicMock()
        project.id = PROJECT_ID
        project.mesh_network_host_id = HOST_ID
        host_assignments = {HOST_ID: ["vm-1"], HOST_ID_2: ["vm-2"]}
        topology = {
            "nodes": [
                {
                    "id": "net-1",
                    "type": "networkNode",
                    "data": {"networkType": "vxlan"},
                }
            ]
        }
        result = _setup_remote_networks(db, project, host_assignments, {}, topology)
        assert result is True

    @patch(
        "app.services.deploy_service._setup_remote_host_network",
        return_value="failed on remote",
    )
    def test_failure_returns_false(self, mock_setup):
        db = MagicMock()
        peer = MagicMock()
        peer.host_id = HOST_ID
        peer.wg_address = "10.99.0.1/32"
        db.query.return_value.filter_by.return_value.all.return_value = [peer]
        project = MagicMock()
        project.id = PROJECT_ID
        project.mesh_network_host_id = HOST_ID
        host_assignments = {HOST_ID: ["vm-1"]}
        topology = {"nodes": []}
        result = _setup_remote_networks(db, project, host_assignments, {}, topology)
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_multihost
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import _deploy_multihost


class TestDeployMultihost:
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._build_multihost_assignments")
    def test_no_host_assignments(self, mock_build, mock_del):
        mock_build.return_value = None
        db = MagicMock()
        project = MagicMock()
        _deploy_multihost(PROJECT_ID, project, db)
        assert project.state == "error"
        assert "No host assignments" in project.deploy_error
        db.commit.assert_called_once()

    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._setup_mesh")
    @patch("app.services.deploy_service._resolve_multihost_ips")
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._build_multihost_assignments")
    def test_mesh_setup_failure(
        self, mock_build, mock_prog, mock_ips, mock_mesh, mock_del
    ):
        mock_build.return_value = {HOST_ID: ["vm-1"]}
        mock_ips.return_value = {HOST_ID: "10.0.0.1"}
        mock_mesh.return_value = False
        db = MagicMock()
        project = MagicMock()
        project.topology = {"nodes": []}
        project.vni_map = {}
        _deploy_multihost(PROJECT_ID, project, db)
        assert project.state == "error"
        assert "Mesh setup failed" in project.deploy_error

    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._setup_remote_networks")
    @patch("app.services.deploy_service._get_network_lock")
    @patch("app.services.deploy_service._setup_networks_via_troshkad")
    @patch("app.services.deploy_service._setup_mesh")
    @patch("app.services.deploy_service._resolve_multihost_ips")
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._build_multihost_assignments")
    def test_network_setup_failure(
        self,
        mock_build,
        mock_prog,
        mock_ips,
        mock_mesh,
        mock_net,
        mock_lock,
        mock_remote,
        mock_del,
    ):
        mock_build.return_value = {HOST_ID: ["vm-1"]}
        mock_ips.return_value = {HOST_ID: "10.0.0.1"}
        mock_mesh.return_value = True
        mock_net.return_value = "nftables error"
        db = MagicMock()
        network_host = _make_host()
        db.query.return_value.filter_by.return_value.first.return_value = network_host
        project = MagicMock()
        project.topology = {"nodes": []}
        project.vni_map = {}
        project.mesh_network_host_id = HOST_ID
        _deploy_multihost(PROJECT_ID, project, db)
        assert project.state == "error"
        assert "Network setup failed" in project.deploy_error

    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._setup_remote_networks", return_value=False)
    @patch("app.services.deploy_service._get_network_lock")
    @patch(
        "app.services.deploy_service._setup_networks_via_troshkad", return_value=True
    )
    @patch("app.services.deploy_service._setup_mesh", return_value=True)
    @patch("app.services.deploy_service._resolve_multihost_ips")
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._build_multihost_assignments")
    def test_remote_network_failure(
        self,
        mock_build,
        mock_prog,
        mock_ips,
        mock_mesh,
        mock_net,
        mock_lock,
        mock_remote,
        mock_del,
    ):
        mock_build.return_value = {HOST_ID: ["vm-1"]}
        mock_ips.return_value = {HOST_ID: "10.0.0.1"}
        db = MagicMock()
        network_host = _make_host()
        db.query.return_value.filter_by.return_value.first.return_value = network_host
        project = MagicMock()
        project.topology = {"nodes": []}
        project.vni_map = {}
        project.mesh_network_host_id = HOST_ID
        _deploy_multihost(PROJECT_ID, project, db)
        assert project.state == "error"
        assert "Remote network setup failed" in project.deploy_error

    @patch("app.services.deploy_service.notify_project")
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._start_multihost_vms")
    @patch("app.services.deploy_service._deploy_vms_on_host", return_value=None)
    @patch("app.services.deploy_service._extract_vms")
    @patch("app.services.deploy_service._setup_remote_networks", return_value=True)
    @patch("app.services.deploy_service._get_network_lock")
    @patch(
        "app.services.deploy_service._setup_networks_via_troshkad", return_value=True
    )
    @patch("app.services.deploy_service._setup_mesh", return_value=True)
    @patch("app.services.deploy_service._resolve_multihost_ips")
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._build_multihost_assignments")
    def test_full_success(
        self,
        mock_build,
        mock_prog,
        mock_ips,
        mock_mesh,
        mock_net,
        mock_lock,
        mock_remote,
        mock_vms,
        mock_deploy_vms,
        mock_start,
        mock_del,
        mock_notify,
    ):
        mock_build.return_value = {HOST_ID: ["vm-1"]}
        mock_ips.return_value = {HOST_ID: "10.0.0.1"}
        mock_vms.return_value = [{"node_id": "vm-1"}]
        db = MagicMock()
        network_host = _make_host()
        db.query.return_value.filter_by.return_value.first.return_value = network_host
        project = MagicMock()
        project.id = PROJECT_ID
        project.topology = {"nodes": []}
        project.vni_map = {}
        project.mesh_network_host_id = HOST_ID
        project.host_assignments = {HOST_ID: "vm-1"}
        _deploy_multihost(PROJECT_ID, project, db)
        assert project.state == "active"
        assert project.deploy_error is None
        mock_notify.assert_called_once()

    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._setup_mesh", return_value=True)
    @patch("app.services.deploy_service._resolve_multihost_ips")
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._build_multihost_assignments")
    def test_network_host_not_found(
        self, mock_build, mock_prog, mock_ips, mock_mesh, mock_del
    ):
        mock_build.return_value = {HOST_ID: ["vm-1"]}
        mock_ips.return_value = {HOST_ID: "10.0.0.1"}
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        project = MagicMock()
        project.topology = {"nodes": []}
        project.vni_map = {}
        project.mesh_network_host_id = HOST_ID
        _deploy_multihost(PROJECT_ID, project, db)
        assert project.state == "error"
        assert "Network host not found" in project.deploy_error


# ═══════════════════════════════════════════════════════════════════════════
# cache_library_images — name fallback branch (lines 378-399)
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import cache_library_images


class TestCacheLibraryImagesNameFallback:
    @patch("app.services.deploy_service.start_job")
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service._get_host_pool", return_value=None)
    def test_name_fallback_resolves_item(self, mock_pool, mock_wait, mock_start):
        """When libraryItemId lookup fails, falls back to name-based lookup."""
        mock_start.return_value = "job-1"
        # stat says file exists (skip download)
        mock_wait.return_value = {"result": {"exists": True}}
        db = MagicMock()
        item = MagicMock()
        item.id = "resolved-item-id"
        item.name = "rhel9"
        item.s3_key = "library/rhel9.qcow2"
        item.size_bytes = 1024
        item.source = "local"
        item.source_provider_id = None
        # filter_by(id=...).first() returns None, filter(...name...).first() returns item
        db.query.return_value.filter_by.return_value.first.return_value = None
        db.query.return_value.filter.return_value.first.return_value = item
        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {
                        "libraryItemId": "bad-id-00",
                        "libraryItemName": "rhel9",
                        "format": "qcow2",
                    },
                }
            ]
        }
        host = _make_host()
        cache_library_images(topology, host, db)
        # The node's libraryItemId should be updated to the resolved ID
        assert topology["nodes"][0]["data"]["libraryItemId"] == "resolved-item-id"

    @patch("app.services.deploy_service._get_host_pool", return_value=None)
    def test_name_fallback_no_match_no_s3(self, mock_pool):
        """Name fallback returns None, item has no s3_key — no download needed."""
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        db.query.return_value.filter.return_value.first.return_value = None
        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {
                        "libraryItemId": "bad-id-00",
                        "libraryItemName": "nonexistent",
                        "format": "qcow2",
                    },
                }
            ]
        }
        host = _make_host()
        # Should not hang — no items to cache, returns early
        cache_library_images(topology, host, db)
        assert topology["nodes"][0]["data"]["libraryItemId"] == "bad-id-00"


# ═══════════════════════════════════════════════════════════════════════════
# _setup_mesh
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import _setup_mesh


class TestSetupMesh:
    @patch("app.services.deploy_service._push_mesh_config_to_peer", return_value=None)
    @patch("app.services.deploy_service.create_mesh_peers")
    def test_success(self, mock_create, mock_push):
        peer1 = MagicMock()
        peer2 = MagicMock()
        mock_create.return_value = [peer1, peer2]
        db = MagicMock()
        project = MagicMock()
        project.id = PROJECT_ID
        project.mesh_network_host_id = HOST_ID
        result = _setup_mesh(db, project, {HOST_ID: ["vm-1"]}, {HOST_ID: "10.0.0.1"})
        assert result is True
        assert mock_push.call_count == 2

    @patch("app.services.deploy_service._rollback_mesh")
    @patch(
        "app.services.deploy_service._push_mesh_config_to_peer",
        return_value="config push failed",
    )
    @patch("app.services.deploy_service.create_mesh_peers")
    def test_failure_rolls_back(self, mock_create, mock_push, mock_rollback):
        peer = MagicMock()
        mock_create.return_value = [peer]
        db = MagicMock()
        project = MagicMock()
        project.id = PROJECT_ID
        project.mesh_network_host_id = HOST_ID
        result = _setup_mesh(db, project, {HOST_ID: ["vm-1"]}, {HOST_ID: "10.0.0.1"})
        assert result is False
        mock_rollback.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# _cleanup_stale_shared_cache
# ═══════════════════════════════════════════════════════════════════════════

from app.services.deploy_service import (
    _allocate_single_eip,
    _cleanup_stale_shared_cache,
    _create_ordered_containers,
    _deploy_allocate_eips,
    _deploy_create_dns_records,
    _deploy_create_ocpvirt_routes,
    _deploy_handle_recert,
    _deploy_setup_bmc,
    _deploy_sync_sg_rules,
    _deploy_vms_on_host,
    _load_container_from_pattern,
    _start_multihost_vms,
)


class TestCleanupStaleSharedCache:
    def test_no_host_id(self):
        s = MagicMock()
        project = MagicMock()
        project.host_id = None
        _cleanup_stale_shared_cache(s, project)
        s.query.assert_not_called()

    def test_host_not_found(self):
        s = MagicMock()
        project = MagicMock()
        project.host_id = HOST_ID
        # Host query returns None
        s.query.return_value.filter_by.return_value.first.return_value = None
        _cleanup_stale_shared_cache(s, project)

    @patch("app.services.deploy_service._get_host_pool")
    def test_local_pool_skips(self, mock_pool):
        pool = MagicMock()
        pool.mode = "local"
        mock_pool.return_value = pool
        s = MagicMock()
        project = MagicMock()
        project.host_id = HOST_ID
        host = _make_host()
        s.query.return_value.filter_by.return_value.first.return_value = host
        _cleanup_stale_shared_cache(s, project)

    @patch("app.services.deploy_service._get_host_pool")
    def test_shared_pool_deletes_downloading_entries(self, mock_pool):
        pool = MagicMock()
        pool.id = "pool-1"
        pool.mode = "shared-fsx"
        mock_pool.return_value = pool
        entry1 = MagicMock()
        entry2 = MagicMock()
        s = MagicMock()
        project = MagicMock()
        project.host_id = HOST_ID
        host = _make_host()
        # First query is for Host lookup, second for SharedCacheEntry
        host_query = MagicMock()
        host_query.filter_by.return_value.first.return_value = host
        cache_query = MagicMock()
        cache_query.filter.return_value.all.return_value = [entry1, entry2]
        s.query.side_effect = [host_query, cache_query]
        _cleanup_stale_shared_cache(s, project)
        assert s.delete.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_setup_bmc (covers lines 3379-3406)
# ═══════════════════════════════════════════════════════════════════════════


class TestDeploySetupBmc:
    @patch("app.services.deploy_service._extract_bmc_config", return_value=None)
    def test_no_bmc_vms(self, mock_extract):
        topo = {"nodes": [{"type": "vmNode", "data": {"bmcEnabled": False}}]}
        err, cfg = _deploy_setup_bmc(_make_host(), PROJECT_ID, topo)
        assert err is None
        assert cfg is None

    @patch("app.services.deploy_service._extract_bmc_config", return_value=None)
    def test_bmc_vms_but_no_bmc_config(self, mock_extract):
        topo = {
            "nodes": [{"type": "vmNode", "data": {"bmcEnabled": True}}],
        }
        err, cfg = _deploy_setup_bmc(_make_host(), PROJECT_ID, topo)
        assert "no BMC network" in err
        assert cfg is None

    @patch("app.services.deploy_service._setup_bmc_via_troshkad", return_value=True)
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value={})
    @patch("app.services.deploy_service.notify_project")
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._extract_bmc_config")
    def test_bmc_success(
        self, mock_extract, mock_prog, mock_notify, mock_get, mock_bmc
    ):
        bmc = {"bmc_network": {"cidr": "10.0.0.0/24"}, "vms": [{"bmc_ip": "10.0.0.10"}]}
        mock_extract.return_value = bmc
        topo = {"nodes": [{"type": "vmNode", "data": {"bmcEnabled": True}}]}
        err, cfg = _deploy_setup_bmc(_make_host(), PROJECT_ID, topo)
        assert err is None
        assert cfg is bmc
        mock_bmc.assert_called_once()

    @patch(
        "app.services.deploy_service._setup_bmc_via_troshkad",
        return_value="port conflict",
    )
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value={})
    @patch("app.services.deploy_service.notify_project")
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._extract_bmc_config")
    def test_bmc_failure(
        self, mock_extract, mock_prog, mock_notify, mock_get, mock_bmc
    ):
        bmc = {"bmc_network": {}, "vms": [{"bmc_ip": "10.0.0.10"}]}
        mock_extract.return_value = bmc
        topo = {"nodes": [{"type": "vmNode", "data": {"bmcEnabled": True}}]}
        err, cfg = _deploy_setup_bmc(_make_host(), PROJECT_ID, topo)
        assert "port conflict" in err
        assert cfg is None


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_create_dns_records (covers lines 3513-3555)
# ═══════════════════════════════════════════════════════════════════════════


class TestDeployCreateDnsRecords:
    def test_no_dns_provider_id(self):
        s = MagicMock()
        project = MagicMock()
        project.dns_provider_id = None
        project.guid = "guid1"
        project.domain = "example.com"
        _deploy_create_dns_records(s, PROJECT_ID, project, {}, None, [])
        s.query.assert_not_called()

    def test_no_guid(self):
        s = MagicMock()
        project = MagicMock()
        project.dns_provider_id = "dns-1"
        project.guid = None
        project.domain = "example.com"
        _deploy_create_dns_records(s, PROJECT_ID, project, {}, None, [])
        s.query.assert_not_called()

    def test_no_lb_config(self):
        s = MagicMock()
        project = MagicMock()
        project.dns_provider_id = "dns-1"
        project.guid = "guid1"
        project.domain = "example.com"
        dns_prov = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = dns_prov
        _deploy_create_dns_records(s, PROJECT_ID, project, {}, None, [])

    @patch("app.services.dns_service.create_dns_records", return_value=[])
    @patch(
        "app.services.dns_service.resolve_dns_records",
        return_value=[{"name": "api.guid1.example.com", "value": "1.2.3.4"}],
    )
    @patch("app.services.deploy_service._update_deploy_progress")
    def test_creates_records(self, mock_prog, mock_resolve, mock_create):
        s = MagicMock()
        project = MagicMock()
        project.dns_provider_id = "dns-1"
        project.guid = "guid1"
        project.domain = "example.com"
        project.deployed_topology = {}
        dns_prov = MagicMock()
        dns_prov.type = "route53"
        dns_prov.config = {"zone_id": "Z123"}
        s.query.return_value.filter_by.return_value.first.return_value = dns_prov
        lb_config = {
            "dns_records": [{"name": "api.{guid}.{domain}", "type": "A"}],
            "dns_ttl": 60,
        }
        ext_ips = [{"ip": "1.2.3.4"}]
        _deploy_create_dns_records(s, PROJECT_ID, project, {}, lb_config, ext_ips)
        mock_resolve.assert_called_once()
        mock_create.assert_called_once()
        assert project.deployed_topology["_dns_records"] == [
            {"name": "api.guid1.example.com", "value": "1.2.3.4"}
        ]

    @patch("app.services.dns_service.create_dns_records", return_value=["err1"])
    @patch("app.services.dns_service.resolve_dns_records", return_value=[{"name": "a"}])
    @patch("app.services.deploy_service._update_deploy_progress")
    def test_logs_errors(self, mock_prog, mock_resolve, mock_create):
        s = MagicMock()
        project = MagicMock()
        project.dns_provider_id = "dns-1"
        project.guid = "g"
        project.domain = "d.com"
        project.deployed_topology = None
        dns_prov = MagicMock()
        dns_prov.type = "route53"
        dns_prov.config = {}
        s.query.return_value.filter_by.return_value.first.return_value = dns_prov
        lb_config = {"dns_records": [{"name": "x"}], "dns_ttl": 30}
        _deploy_create_dns_records(s, PROJECT_ID, project, {}, lb_config, [])
        # Should not raise even with errors


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_sync_sg_rules (covers lines 2926-2959)
# ═══════════════════════════════════════════════════════════════════════════


class TestDeploySyncSgRules:
    @patch("app.services.eip_service.sync_security_group_rules")
    def test_no_provider(self, mock_sync):
        s = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = None
        project = MagicMock()
        project.provider_id = None
        host = _make_host()
        host.provider_id = None
        _deploy_sync_sg_rules(s, PROJECT_ID, project, host, {}, None)
        mock_sync.assert_not_called()

    @patch("app.services.eip_service.sync_security_group_rules")
    def test_with_gateway_and_lb(self, mock_sync):
        s = MagicMock()
        provider = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = provider
        project = MagicMock()
        project.provider_id = "prov-1"
        host = _make_host()
        topology = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {
                        "subtype": "gateway",
                        "gatewayMode": "nat-portforward",
                        "portForwards": [{"extPort": 443}],
                    },
                }
            ]
        }
        lb_config = {
            "frontends": [{"bindPort": 6443}],
            "external": True,
        }
        _deploy_sync_sg_rules(s, PROJECT_ID, project, host, topology, lb_config)
        mock_sync.assert_called_once()
        rules = mock_sync.call_args[0][2]
        ports = {r["ext_port"] for r in rules}
        assert 443 in ports
        assert 6443 in ports

    @patch("app.services.eip_service.sync_security_group_rules")
    def test_fallback_to_host_provider(self, mock_sync):
        s = MagicMock()
        provider = MagicMock()
        # project.provider_id query returns None, host.provider_id query returns provider
        s.query.return_value.filter_by.return_value.first.side_effect = [None, provider]
        project = MagicMock()
        project.provider_id = "missing"
        host = _make_host()
        topology = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {
                        "subtype": "gateway",
                        "gatewayMode": "nat-portforward",
                        "portForwards": [{"extPort": 80}],
                    },
                }
            ]
        }
        _deploy_sync_sg_rules(s, PROJECT_ID, project, host, topology, None)
        mock_sync.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_allocate_eips (covers lines 2839-2867)
# ═══════════════════════════════════════════════════════════════════════════


class TestDeployAllocateEips:
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._checkpoint")
    def test_no_provider(self, mock_cp, mock_prog):
        s = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = None
        project = MagicMock()
        project.provider_id = None
        host = _make_host()
        host.provider_id = None
        result = _deploy_allocate_eips(
            s, PROJECT_ID, project, host, {}, [{"id": "eip-1"}]
        )
        assert "No provider" in result

    @patch("app.services.deploy_service._allocate_single_eip")
    @patch("app.services.deploy_service._should_skip_ocpvirt_eip", return_value=False)
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._checkpoint")
    def test_allocates_eips(self, mock_cp, mock_prog, mock_skip, mock_alloc):
        s = MagicMock()
        provider = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = provider
        project = MagicMock()
        project.provider_id = "prov-1"
        host = _make_host()
        ext_ips = [{"id": "eip-1"}, {"id": "eip-2"}]
        result = _deploy_allocate_eips(
            s, PROJECT_ID, project, host, {"nodes": []}, ext_ips
        )
        assert result is None
        assert mock_alloc.call_count == 2
        s.commit.assert_called_once()

    @patch("app.services.deploy_service._should_skip_ocpvirt_eip", return_value=True)
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._checkpoint")
    def test_skips_ocpvirt(self, mock_cp, mock_prog, mock_skip):
        s = MagicMock()
        provider = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = provider
        project = MagicMock()
        project.provider_id = "prov-1"
        host = _make_host()
        ext_ips = [{"id": "eip-1"}]
        result = _deploy_allocate_eips(
            s, PROJECT_ID, project, host, {"nodes": []}, ext_ips
        )
        assert result is None
        # _skip key should be cleaned up
        assert "_skip" not in ext_ips[0]


# ═══════════════════════════════════════════════════════════════════════════
# _allocate_single_eip — ec2/libvirt vs. transit-port providers
# ═══════════════════════════════════════════════════════════════════════════


class TestAllocateSingleEip:
    def _run(self, provider_type, mock_alloc, mock_assoc, mock_transit, mock_driver_fn):
        s = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = None

        provider = MagicMock()
        provider.type = provider_type
        host = _make_host(provider_id="prov-1")

        mock_eip = MagicMock()
        mock_eip.state = "allocated"
        mock_eip.public_ip = "192.168.124.198"
        mock_eip.private_ip = "192.168.124.198"
        mock_eip.port_map = None
        mock_alloc.return_value = mock_eip

        ext_ip = {"id": "eip-1"}
        topology = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {
                        "subtype": "gateway",
                        "gatewayMode": "nat-portforward",
                        "portForwards": [
                            {"extIpId": "eip-1", "extPort": "8080", "intIp": "10.0.0.5", "intPort": "80"}
                        ],
                    },
                }
            ]
        }

        _allocate_single_eip(s, provider, PROJECT_ID, host, ext_ip, topology)
        return ext_ip, mock_eip

    @patch("app.services.providers.get_provider_driver")
    @patch("app.services.eip_service.allocate_transit_ports")
    @patch("app.services.eip_service.associate_eip")
    @patch("app.services.eip_service.allocate_eip")
    def test_libvirt_skips_transit_ports(
        self, mock_alloc, mock_assoc, mock_transit, mock_driver_fn
    ):
        """Libvirt shares EC2's direct-private-IP DNAT path — no transit
        ports, no update_eip_ports call (which libvirt doesn't implement)."""
        ext_ip, _ = self._run(
            "libvirt", mock_alloc, mock_assoc, mock_transit, mock_driver_fn
        )
        mock_transit.assert_not_called()
        mock_driver_fn.return_value.update_eip_ports.assert_not_called()
        assert ext_ip["_private_ip"] == "192.168.124.198"
        assert "_transit_port_map" not in ext_ip

    @patch("app.services.providers.get_provider_driver")
    @patch("app.services.eip_service.allocate_transit_ports")
    @patch("app.services.eip_service.associate_eip")
    @patch("app.services.eip_service.allocate_eip")
    def test_ec2_skips_transit_ports(
        self, mock_alloc, mock_assoc, mock_transit, mock_driver_fn
    ):
        """Regression guard for the pre-existing EC2 direct-IP behavior."""
        self._run("ec2", mock_alloc, mock_assoc, mock_transit, mock_driver_fn)
        mock_transit.assert_not_called()
        mock_driver_fn.return_value.update_eip_ports.assert_not_called()

    @patch("app.services.providers.get_provider_driver")
    @patch("app.services.eip_service.allocate_transit_ports")
    @patch("app.services.eip_service.associate_eip")
    @patch("app.services.eip_service.allocate_eip")
    def test_gcp_still_allocates_transit_ports(
        self, mock_alloc, mock_assoc, mock_transit, mock_driver_fn
    ):
        """Regression guard: providers sharing one LB IP (gcp/azure/ocpvirt/
        kubevirt) must keep going through transit-port allocation."""
        mock_transit.return_value = {"8080": 40001}
        self._run("gcp", mock_alloc, mock_assoc, mock_transit, mock_driver_fn)
        mock_transit.assert_called_once()
        mock_driver_fn.return_value.update_eip_ports.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_handle_recert (covers lines 3283-3294)
# ═══════════════════════════════════════════════════════════════════════════


class TestDeployHandleRecert:
    @patch("app.services.deploy_service._is_ocp_topology", return_value=False)
    @patch("app.services.deploy_service._is_pattern_deploy", return_value=True)
    def test_skip_non_ocp(self, mock_pattern, mock_ocp):
        s = MagicMock()
        host = _make_host()
        _deploy_handle_recert(s, host, PROJECT_ID, {"nodes": []}, None)
        # Should return early, no recert logic

    @patch("app.services.deploy_service._is_pattern_deploy", return_value=False)
    def test_skip_non_pattern(self, mock_pattern):
        s = MagicMock()
        host = _make_host()
        _deploy_handle_recert(s, host, PROJECT_ID, {"nodes": []}, None)

    @patch("app.services.deploy_service._clean_kubelet_certs")
    @patch("app.services.deploy_service._auto_enable_recert_on_rhcos")
    @patch(
        "app.services.deploy_service._resolve_recert_settings",
        return_value=(True, "pass123"),
    )
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.deploy_service._is_ocp_topology", return_value=True)
    @patch("app.services.deploy_service._is_pattern_deploy", return_value=True)
    def test_runs_recert(
        self, mock_pattern, mock_ocp, mock_prog, mock_resolve, mock_auto, mock_clean
    ):
        s = MagicMock()
        host = _make_host()
        topo = {"nodes": []}
        _deploy_handle_recert(s, host, PROJECT_ID, topo, None)
        mock_clean.assert_called_once()
        mock_auto.assert_called_once_with(topo, True, PROJECT_ID)


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_create_ocpvirt_routes (covers lines 3050-3070)
# ═══════════════════════════════════════════════════════════════════════════


class TestDeployCreateOcpvirtRoutes:
    def test_no_host(self):
        s = MagicMock()
        _deploy_create_ocpvirt_routes(s, None, PROJECT_ID, {})
        s.query.assert_not_called()

    def test_non_ocpvirt_provider(self):
        s = MagicMock()
        provider = MagicMock()
        provider.type = "ec2"
        s.query.return_value.filter_by.return_value.first.return_value = provider
        host = _make_host()
        _deploy_create_ocpvirt_routes(s, host, PROJECT_ID, {"nodes": []})
        s.commit.assert_not_called()

    @patch("app.services.deploy_service._create_routes_for_gateway")
    @patch("app.services.providers.get_provider_driver")
    def test_creates_routes(self, mock_driver, mock_create):
        mock_create.return_value = [{"hostname": "test.apps.cluster.com", "port": 443}]
        s = MagicMock()
        provider = MagicMock()
        provider.type = "ocpvirt"
        s.query.return_value.filter_by.return_value.first.return_value = provider
        host = _make_host()
        topology = {
            "nodes": [
                {
                    "id": "gw1",
                    "data": {
                        "subtype": "gateway",
                        "portForwards": [{"extPort": 443}],
                    },
                }
            ]
        }
        _deploy_create_ocpvirt_routes(s, host, PROJECT_ID, topology)
        mock_create.assert_called_once()
        s.commit.assert_called_once()
        assert topology["nodes"][0]["data"]["externalEndpoints"] == [
            {"hostname": "test.apps.cluster.com", "port": 443}
        ]


# ═══════════════════════════════════════════════════════════════════════════
# _start_multihost_vms (covers lines 2643-2658)
# ═══════════════════════════════════════════════════════════════════════════


class TestStartMultihostVms:
    @patch("app.services.deploy_service._start_vms_via_troshkad", return_value=[])
    @patch("app.services.deploy_service._filter_topology_for_host")
    @patch("app.services.deploy_service._update_deploy_progress")
    def test_starts_vms_on_all_hosts(self, mock_prog, mock_filter, mock_start):
        mock_filter.return_value = {"nodes": []}
        db = MagicMock()
        host1 = _make_host()
        host1.id = HOST_ID
        host2 = _make_host()
        host2.id = HOST_ID_2
        db.query.return_value.filter_by.return_value.first.side_effect = [host1, host2]
        vm_sets = {HOST_ID: ["vm-1"], HOST_ID_2: ["vm-2"]}
        _start_multihost_vms(PROJECT_ID, vm_sets, {"nodes": []}, db)
        assert mock_start.call_count == 2

    @patch("app.services.deploy_service._start_vms_via_troshkad")
    @patch("app.services.deploy_service._filter_topology_for_host")
    @patch("app.services.deploy_service._update_deploy_progress")
    def test_skips_missing_host(self, mock_prog, mock_filter, mock_start):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        _start_multihost_vms(PROJECT_ID, {HOST_ID: ["vm-1"]}, {"nodes": []}, db)
        mock_start.assert_not_called()

    @patch(
        "app.services.deploy_service._start_vms_via_troshkad",
        return_value=[("sno1", "qemu error")],
    )
    @patch("app.services.deploy_service._filter_topology_for_host")
    @patch("app.services.deploy_service._update_deploy_progress")
    def test_logs_start_failures(self, mock_prog, mock_filter, mock_start):
        mock_filter.return_value = {"nodes": []}
        db = MagicMock()
        host = _make_host()
        db.query.return_value.filter_by.return_value.first.return_value = host
        _start_multihost_vms(PROJECT_ID, {HOST_ID: ["vm-1"]}, {"nodes": []}, db)
        # Should not raise, just log


# ═══════════════════════════════════════════════════════════════════════════
# _deploy_vms_on_host (covers lines 2542-2570)
# ═══════════════════════════════════════════════════════════════════════════


class TestDeployVmsOnHost:
    @patch("app.services.deploy_service._define_multihost_vms", return_value=None)
    @patch("app.services.deploy_service._clean_stale_domains")
    @patch("app.services.deploy_service._create_multihost_disks", return_value=None)
    @patch("app.services.deploy_service._create_seed_isos_via_troshkad")
    @patch("app.services.deploy_service._filter_topology_for_host")
    @patch("app.services.deploy_service.cache_library_images")
    @patch("app.services.deploy_service._get_host_pool", return_value=None)
    @patch("app.services.deploy_service._update_deploy_progress")
    def test_success(
        self,
        mock_prog,
        mock_pool,
        mock_cache,
        mock_filter,
        mock_seeds,
        mock_disks,
        mock_stale,
        mock_define,
    ):
        host = _make_host()
        project = MagicMock()
        project.clock_target = None
        host_vms = [{"node_id": "vm-1"}]
        topo = {"nodes": []}
        result = _deploy_vms_on_host(
            host, PROJECT_ID, project, host_vms, topo, {}, MagicMock()
        )
        assert result is None
        mock_cache.assert_called_once()
        mock_seeds.assert_called_once()

    @patch(
        "app.services.deploy_service._create_multihost_disks",
        return_value="disk error",
    )
    @patch("app.services.deploy_service._create_seed_isos_via_troshkad")
    @patch("app.services.deploy_service._filter_topology_for_host")
    @patch("app.services.deploy_service.cache_library_images")
    @patch("app.services.deploy_service._get_host_pool", return_value=None)
    @patch("app.services.deploy_service._update_deploy_progress")
    def test_disk_error(
        self, mock_prog, mock_pool, mock_cache, mock_filter, mock_seeds, mock_disks
    ):
        host = _make_host()
        project = MagicMock()
        project.clock_target = None
        result = _deploy_vms_on_host(
            host, PROJECT_ID, project, [{"node_id": "vm-1"}], {}, {}, MagicMock()
        )
        assert result == "disk error"

    @patch("app.services.deploy_service._define_multihost_vms", return_value=None)
    @patch("app.services.deploy_service._clean_stale_domains")
    @patch("app.services.deploy_service._create_multihost_disks", return_value=None)
    @patch("app.services.deploy_service._create_seed_isos_via_troshkad")
    @patch("app.services.deploy_service._filter_topology_for_host")
    @patch("app.services.deploy_service.cache_library_images")
    @patch("app.services.deploy_service._get_host_pool", return_value=None)
    @patch("app.services.deploy_service._update_deploy_progress")
    @patch("app.services.clock_service.compute_clock_offset", return_value=-3600)
    def test_with_clock_target(
        self,
        mock_offset,
        mock_prog,
        mock_pool,
        mock_cache,
        mock_filter,
        mock_seeds,
        mock_disks,
        mock_stale,
        mock_define,
    ):
        host = _make_host()
        project = MagicMock()
        project.clock_target = "2025-01-01T00:00:00Z"
        host_vms = [{"node_id": "vm-1"}]
        result = _deploy_vms_on_host(
            host, PROJECT_ID, project, host_vms, {}, {}, MagicMock()
        )
        assert result is None
        # clock_offset should be passed to _define_multihost_vms
        call_args = mock_define.call_args[0]
        assert call_args[6] == -3600


# ═══════════════════════════════════════════════════════════════════════════
# _create_ordered_containers (covers lines 3409-3431)
# ═══════════════════════════════════════════════════════════════════════════


class TestCreateOrderedContainers:
    @patch("app.services.deploy_service._create_and_start_container")
    def test_creates_ordered_containers(self, mock_create):
        host = _make_host()
        containers = [
            {"node_id": "c1", "image": "nginx", "is_pod": False},
            {"node_id": "c2", "image": "redis", "is_pod": False},
        ]
        start_order = [
            {"entryType": "container", "containerId": "c1", "delaySeconds": 0},
            {"entryType": "container", "containerId": "c2", "delaySeconds": 0},
        ]
        result = _create_ordered_containers(
            host, PROJECT_ID, containers, start_order, {}, {}, None
        )
        assert result == {"c1", "c2"}
        assert mock_create.call_count == 2

    @patch("app.services.deploy_service._create_and_start_pod")
    def test_creates_ordered_pods(self, mock_create):
        host = _make_host()
        containers = [{"node_id": "p1", "image": "", "is_pod": True}]
        start_order = [{"entryType": "container", "containerId": "p1"}]
        result = _create_ordered_containers(
            host, PROJECT_ID, containers, start_order, {}, {}, None
        )
        assert "p1" in result
        mock_create.assert_called_once()

    def test_no_matching_containers(self):
        host = _make_host()
        containers = [{"node_id": "c1", "image": "nginx", "is_pod": False}]
        start_order = [{"entryType": "container", "containerId": "nonexistent"}]
        result = _create_ordered_containers(
            host, PROJECT_ID, containers, start_order, {}, {}, None
        )
        assert result == set()

    def test_skips_non_container_entries(self):
        host = _make_host()
        containers = []
        start_order = [{"entryType": "vm", "vmId": "vm-1"}]
        result = _create_ordered_containers(
            host, PROJECT_ID, containers, start_order, {}, {}, None
        )
        assert result == set()


# ═══════════════════════════════════════════════════════════════════════════
# _load_container_from_pattern (covers lines 3145-3176)
# ═══════════════════════════════════════════════════════════════════════════


class TestLoadContainerFromPattern:
    @patch("app.services.deploy_service.wait_for_job")
    @patch("app.services.deploy_service.start_job")
    @patch("app.services.s3_storage._get_s3_config")
    @patch("app.services.s3_storage._bucket", return_value="troshka-images")
    def test_loads_image(self, mock_bucket, mock_creds, mock_start, mock_wait):
        mock_creds.return_value = {
            "access_key_id": "ak",
            "secret_access_key": "sk",
            "region": "us-east-1",
            "endpoint_url": "",
        }
        mock_start.side_effect = ["cache-job", "load-job"]
        mock_wait.side_effect = [
            {"status": "completed"},
            {"status": "completed"},
        ]
        host = _make_host()
        ctr = {"node_id": "container-1234-5678", "image": "quay.io/test/img:v1"}
        _load_container_from_pattern(host, PROJECT_ID, ctr, "pattern-123")
        assert mock_start.call_count == 2
        # First call: /images/cache to download the tar
        assert mock_start.call_args_list[0][0][1] == "/images/cache"
        # Second call: /containers/load-image to load it
        assert mock_start.call_args_list[1][0][1] == "/containers/load-image"
