"""Unit tests for helpers extracted to satisfy Sonar S3776 (cognitive complexity)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from app.services.ceph_scaffold import (
    _ceph_cluster_edges,
    _ceph_edge_handles,
    _clamp_osd_capacity,
)
from app.services.ocp.cluster_topology_heal import (
    LEGACY_GHOST_CLUSTER_ID,
    LEGACY_GHOST_NODE_ID,
    _canvas_cluster_base,
    _cluster_entry_from_node,
    _heal_storage_node_membership,
    _heal_vm_node_membership,
    _resolve_ghost_member_cid,
    _should_drop_ghost,
)
from app.services.ocp.kubeconfig_merge import (
    _motd_context_lines,
    _motd_header_lines,
    _parse_merged_kubeconfig,
    _unique_context_name,
    merge_kubeconfigs,
)
from app.services.ocp.ops_pod_install import (
    PHASE_FAILED,
    PHASE_WAITING,
    _cluster_has_harvested_creds,
    _is_retryable_node_image_failure,
    _phase_from_input,
    _worker_join_active,
    _worker_join_phase,
)
from app.services.ocp.pull_through_registry import (
    PullThroughRegistryError,
    _assert_probe_ok,
    _auth_failure_message,
)
from app.services.project_ceph import _cluster_id_for_node, _edge_peer
from app.services.vxlan import (
    _bogus_mac_for_ip,
    _cluster_boundary_vips,
    _edge_peer_id,
    _is_gateway_network_node,
    _lb_backend_id_for_node,
    _members_on_network,
    _vip_reservations_for_cluster,
)
from app.services.workloads.inventory import InventoryError, _validate_vm_inventory_node


class TestVxlanHelpers:
    def test_bogus_mac_and_boundary_vips(self):
        assert _bogus_mac_for_ip("192.168.1.10").startswith("02:00:")
        boundary = {"data": {"apiVip": "10.0.0.5", "ingressVip": ""}}
        assert _cluster_boundary_vips(boundary) == [("api", "10.0.0.5")]

    def test_members_on_network_and_vip_reservations(self):
        members = {"vm1"}
        edges = [{"source": "net1", "target": "vm1"}]
        assert _members_on_network("net1", members, edges) is True
        assert _members_on_network("net2", members, edges) is False
        reserved = _vip_reservations_for_cluster(
            "c", [("api", "10.0.0.1"), ("ingress", "10.0.0.1")], set()
        )
        assert len(reserved) == 1
        assert reserved[0]["name"] == "c-api"

    def test_lb_backend_and_gateway_helpers(self):
        assert _edge_peer_id({"source": "a", "target": "b"}, "a") == "b"
        assert _edge_peer_id({"source": "a", "target": "b"}, "c") is None
        assert _lb_backend_id_for_node({"type": "vmNode"}, "vm1") == "vm1"
        assert (
            _lb_backend_id_for_node(
                {"type": "containerNode", "data": {"isShowroom": True}}, "x"
            )
            is None
        )
        assert (
            _lb_backend_id_for_node(
                {"type": "containerNode", "data": {"isPod": True}}, "p1"
            )
            == "p1"
        )
        assert _is_gateway_network_node(
            {"type": "networkNode", "data": {"subtype": "gateway"}}
        )
        assert not _is_gateway_network_node(
            {"type": "networkNode", "data": {"subtype": "lan"}}
        )


class TestClusterTopologyHealHelpers:
    def test_should_drop_ghost(self):
        ghost = {
            "id": LEGACY_GHOST_CLUSTER_ID,
            "nodeId": LEGACY_GHOST_NODE_ID,
            "baseDomain": "ocp.local",
        }
        real = {"id": "c1", "nodeId": "n1", "baseDomain": "lab"}
        assert _should_drop_ghost([ghost], [{"id": "d1"}]) is True
        assert _should_drop_ghost([ghost], []) is False
        assert _should_drop_ghost([ghost, real], []) is True
        assert _canvas_cluster_base([ghost, real], True) == [real]

    def test_cluster_entry_and_ghost_resolve(self):
        node = {"id": "cluster-x"}
        data = {"name": "X", "controlPlane": 3}
        entry = _cluster_entry_from_node(node, data, "cid", {"workers": 2})
        assert entry["controlPlane"] == 3
        assert entry["workers"] == 2
        sno = {"id": "sno1", "type": "sno"}
        assert (
            _resolve_ghost_member_cid({"name": "cp-0"}, [sno], {"sno1"}, None) == "sno1"
        )

    def test_heal_vm_and_storage(self):
        cluster = {"id": "c1", "nodeId": "box1"}
        by_id = {"c1": cluster}
        vm = {
            "id": "c1-cp-0",
            "type": "vmNode",
            "data": {"os": "rhcos", "clusterId": "c1"},
        }
        _heal_vm_node_membership(vm, by_id, [cluster], {"c1"})
        assert vm["parentId"] == "box1"
        disk = {
            "id": "c1-cp-0-disk-0",
            "type": "storageNode",
            "parentId": LEGACY_GHOST_NODE_ID,
        }
        _heal_storage_node_membership(disk, {"c1-cp-0": vm}, by_id, [cluster], {"c1"})
        assert disk["parentId"] == "box1"


class TestKubeconfigMergeHelpers:
    def test_unique_context_and_merge(self):
        seen = {"prod"}
        assert _unique_context_name("Prod", seen) == "prod-2"
        merged = merge_kubeconfigs(
            [
                (
                    "a",
                    yaml.safe_dump(
                        {
                            "apiVersion": "v1",
                            "kind": "Config",
                            "clusters": [
                                {"name": "c", "cluster": {"server": "https://a"}}
                            ],
                            "users": [{"name": "u", "user": {"token": "t"}}],
                            "contexts": [
                                {
                                    "name": "ctx",
                                    "context": {"cluster": "c", "user": "u"},
                                }
                            ],
                            "current-context": "ctx",
                        }
                    ),
                )
            ]
        )
        assert "current-context: a" in merged

    def test_motd_helpers(self):
        assert _parse_merged_kubeconfig("") is None
        assert _motd_header_lines([{}, {}])[0] == ""
        lines = _motd_context_lines(
            {"name": "a"},
            current="a",
            cluster_entries={"a": {"cluster": {"server": "https://x"}}},
            clusters_by_name={},
            kubeadmin_by_ctx={"a": "pw"},
            base_domain_by_ctx={},
        )
        assert any("Kubeadmin" in l for l in lines)
        assert any("* a" in l for l in lines)


class TestOpsPodInstallHelpers:
    def test_worker_join_and_phase(self):
        assert _worker_join_active("joining deferred worker foo")
        assert _worker_join_phase("worker join timed out") == PHASE_FAILED
        assert (
            _worker_join_phase("node-image create failed; retrying in 5s")
            == PHASE_WAITING
        )
        assert _is_retryable_node_image_failure(
            "node-image create failed; retrying in 5s"
        )
        assert _phase_from_input("waiting") in (
            "waiting",
            "creating-image",
            PHASE_WAITING,
        )

    def test_harvested_creds(self):
        topo = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {"clusterId": "c1", "ocpKubeconfig": "k"},
                }
            ]
        }
        assert _cluster_has_harvested_creds(topo, "c1") is True
        assert _cluster_has_harvested_creds(topo, "other") is False


class TestPullThroughHelpers:
    def test_assert_probe_ok(self):
        ok = SimpleNamespace(status=200)
        _assert_probe_ok(ok, "https://reg")
        with pytest.raises(PullThroughRegistryError):
            _assert_probe_ok(SimpleNamespace(status=500), "https://reg")
        assert "credentials invalid" in _auth_failure_message("https://r", 401, "nope")


class TestCephHelpers:
    def test_clamp_and_edges(self):
        assert _clamp_osd_capacity({"osdCount": 99})[0] == 6
        assert _ceph_edge_handles(0) == ("right", "ceph-left")
        assert _ceph_edge_handles(1) == ("left", "ceph-right")
        edges = _ceph_cluster_edges("ceph1", ["hub", "missing"], {"hub": "cluster-hub"})
        assert len(edges) == 1
        assert edges[0]["target"] == "cluster-hub"

    def test_project_ceph_edge_peer(self):
        assert _edge_peer({"source": "a", "target": "b"}, "a") == "b"
        assert _edge_peer({"source": "a", "target": "b"}, "z") == ""
        nodes = [{"id": "n1", "type": "clusterNode", "data": {"clusterId": "c1"}}]
        assert _cluster_id_for_node(nodes, "n1") == "c1"
        assert _cluster_id_for_node(nodes, "missing") is None


class TestInventoryHelpers:
    def test_validate_vm_inventory_node(self):
        with pytest.raises(InventoryError):
            _validate_vm_inventory_node({"data": {}})
        node = {
            "id": "b1",
            "data": {
                "name": "bastion",
                "nics": [{"ip": "10.0.0.1"}],
                "tags": {"AnsibleGroup": "bastions"},
            },
        }
        assert _validate_vm_inventory_node(node) == "b1"
        plain = {
            "id": "v1",
            "data": {
                "name": "app",
                "nics": [{"ip": "10.0.0.2"}],
                "tags": {"AnsibleGroup": "apps"},
            },
        }
        assert _validate_vm_inventory_node(plain) is None


class TestJoinDeferredHelpers:
    def test_apply_joined_flags(self):
        from app.services.ocp.join_deferred_workers import _apply_joined_worker_flags

        node = {"data": {"deferOcpInstall": True}}
        assert _apply_joined_worker_flags(node, {"powerOnAtDeploy": True}) is True
        assert node["data"]["deferOcpInstall"] is False
        assert node["data"]["powerOnAtDeploy"] is True
        assert _apply_joined_worker_flags(node, {}) is False


class TestPatternsRemapHelpers:
    def test_assign_and_stamp(self):
        from app.api.patterns import (
            _assign_cluster_ids,
            _remap_member_cluster_refs,
            _stamp_cluster_node_ids,
        )

        topo = {
            "clusters": [
                {
                    "id": "old",
                    "name": "Hub",
                    "nodeId": "n1",
                    "networkIds": ["net1"],
                }
            ],
            "nodes": [
                {"id": "n1-new", "type": "clusterNode", "data": {}},
                {"id": "vm1", "parentId": "n1", "data": {"clusterId": "old"}},
            ],
        }
        id_map = {"n1": "n1-new", "net1": "net1-new"}
        old_to_new = _assign_cluster_ids(topo, id_map)
        assert "old" in old_to_new
        _remap_member_cluster_refs(topo, old_to_new, id_map)
        assert topo["nodes"][1]["data"]["clusterId"] == old_to_new["old"]
        assert topo["nodes"][1]["parentId"] == "n1-new"
        _stamp_cluster_node_ids(topo)
        assert topo["nodes"][0]["data"]["clusterId"] == topo["clusters"][0]["id"]


class TestPlacementHelpers:
    def test_host_fits_requirements(self):
        from app.services.placement import _host_fits_requirements

        host = SimpleNamespace(
            used_vcpus=0,
            used_ram_mb=0,
            id="h1",
            uplink_mtu=9000,
        )
        with (
            patch("app.services.placement.get_allocatable", return_value=(16, 32768)),
            patch("app.services.placement._check_eip_capacity", return_value=True),
            patch(
                "app.services.placement._host_has_pattern_storage", return_value=True
            ),
            patch("app.services.placement._host_mtu_ok", return_value=True),
            patch("app.services.placement._get_inflight_deploys", return_value=0),
        ):
            cand = _host_fits_requirements(
                MagicMock(),
                host,
                required_vcpus=4,
                required_ram_mb=4096,
                required_eips=0,
                pattern_disk_ids=None,
                required_mtu=None,
            )
            assert cand[0] is host


class TestHealthPollerHelpers:
    def test_mark_stopped_and_read_power(self):
        from app.services import health_poller as hp

        host = SimpleNamespace(id="abcdef12", state="active", agent_status="connected")
        assert hp._mark_host_stopped(host, "stopped") is True
        assert host.state == "stopped"
        drv = MagicMock()
        drv.get_host_powerstate.return_value = "running"
        assert hp._read_cloud_power(drv, MagicMock(), "i-1", {}) == "running"


class TestRunServiceHelpers:
    def test_exit_code_from_statuses(self):
        from app.services.workloads.run_service import (
            _exit_code_from_container_statuses,
        )

        cs = SimpleNamespace(
            state=SimpleNamespace(terminated=SimpleNamespace(exit_code=0))
        )
        assert _exit_code_from_container_statuses([cs]) == 0
        assert _exit_code_from_container_statuses(None) is None


class TestMainHelpers:
    def test_project_has_active_rq_job(self):
        from app.main import _project_has_active_rq_job

        with patch("app.core.redis.is_redis_available", return_value=False):
            assert _project_has_active_rq_job("p1") is False
        with (
            patch("app.core.redis.is_redis_available", return_value=True),
            patch(
                "app.core.redis.get_job_info",
                return_value={"status": "started"},
            ),
        ):
            assert _project_has_active_rq_job("p1") is True


class TestAgentTemplateHelpers:
    def test_add_other_cluster_vips(self):
        import ipaddress

        from app.services.ocp.agent_template import _add_other_cluster_vips

        used: set[str] = set()
        net = ipaddress.ip_network("10.0.0.0/24")
        _add_other_cluster_vips(
            used,
            {
                "clusters": [
                    {"id": "c1"},
                    {"id": "c2", "apiVip": "10.0.0.50", "ingressVip": "10.0.0.51"},
                ]
            },
            {"id": "c1"},
            net,
        )
        assert "10.0.0.50" in used
        assert "10.0.0.51" in used


class TestGcHelpers:
    def test_orphan_pvs_dry_run_report(self):
        from app.services.gc_service import _orphan_pvs_dry_run_report

        report: dict = {}
        orphan = [{"pool": "p", "image": "img"}]
        _orphan_pvs_dry_run_report(report, orphan, True)
        assert report["kubevirt_orphan_pvs_action"]["dry_run"] is True
        assert report["kubevirt_rbd_reclaim"]["would_reclaim"] == [
            {"pool": "p", "image": "img"}
        ]


class TestProjectTimerHelpers:
    def test_started_deploy_is_zombie(self):
        from app.services.project_timer import _started_deploy_is_zombie

        project = SimpleNamespace(state="deploying", id="abc")
        assert _started_deploy_is_zombie("started", project, lambda _k: None) is True
        assert _started_deploy_is_zombie("queued", project, lambda _k: None) is False


class TestOpsPodHealHelpers:
    def test_strip_runtime_fields(self):
        from app.services.ocp.ops_pod_heal import _strip_ops_pod_runtime_fields

        body = {
            "metadata": {"name": "ops", "uid": "u1", "resourceVersion": "1"},
            "status": {"phase": "Pending"},
            "spec": {"nodeName": "n1", "containers": []},
        }
        _strip_ops_pod_runtime_fields(body)
        assert "uid" not in body["metadata"]
        assert "status" not in body
        assert "nodeName" not in body["spec"]
