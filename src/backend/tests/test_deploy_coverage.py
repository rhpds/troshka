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
