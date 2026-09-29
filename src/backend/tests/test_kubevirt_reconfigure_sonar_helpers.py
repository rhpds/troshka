"""Unit tests for kubevirt_reconfigure helpers extracted for Sonar S3776."""

from unittest.mock import MagicMock, patch

import pytest


class TestStaticLeaseHelpers:
    def test_topology_node_data_map(self):
        from app.services.kubevirt_reconfigure import _topology_node_data_map

        nodes = [
            {"id": "vm-1", "data": {"name": "a"}},
            {"data": {"id": "vm-2", "name": "b"}},
        ]
        assert _topology_node_data_map(nodes)["vm-1"]["name"] == "a"
        assert _topology_node_data_map(nodes)["vm-2"]["name"] == "b"

    def test_vm_edge_endpoint_src_has_nics(self):
        from app.services.kubevirt_reconfigure import _vm_edge_endpoint

        node_map = {
            "vm1": {"nics": [{"id": "nic-eth0"}]},
            "net1": {"cidr": "10.0.0.0/24"},
        }
        edge = {
            "source": "vm1",
            "target": "net1",
            "sourceHandle": "nic-eth0-left",
        }
        vm_data, net_id, nic_id = _vm_edge_endpoint(edge, node_map)
        assert vm_data is node_map["vm1"]
        assert net_id == "net1"
        assert nic_id == "nic-eth0"

    def test_vm_edge_endpoint_tgt_has_nics(self):
        from app.services.kubevirt_reconfigure import _vm_edge_endpoint

        node_map = {
            "net1": {"cidr": "10.0.0.0/24"},
            "vm1": {"nics": [{"id": "nic-eth0"}]},
        }
        edge = {
            "source": "net1",
            "target": "vm1",
            "targetHandle": "nic-eth0-right",
        }
        vm_data, net_id, nic_id = _vm_edge_endpoint(edge, node_map)
        assert vm_data is node_map["vm1"]
        assert net_id == "net1"
        assert nic_id == "nic-eth0"

    def test_vm_edge_endpoint_no_nics(self):
        from app.services.kubevirt_reconfigure import _vm_edge_endpoint

        assert _vm_edge_endpoint(
            {"source": "a", "target": "b"}, {"a": {}, "b": {}}
        ) == (None, "", "")

    def test_lease_from_vm_nic(self):
        from app.services.kubevirt_reconfigure import _lease_from_vm_nic

        vm = {
            "name": "worker-0",
            "nics": [
                {"id": "nic-other", "mac": "aa", "ip": "1.1.1.1"},
                {"id": "nic-eth0", "mac": "52:54:00:11:22:33", "ip": "10.0.0.10"},
            ],
        }
        lease = _lease_from_vm_nic(vm, "nic-eth0")
        assert lease == {
            "mac": "52:54:00:11:22:33",
            "ip": "10.0.0.10",
            "hostname": "worker-0",
        }
        assert _lease_from_vm_nic(vm, "missing") is None
        assert (
            _lease_from_vm_nic({"nics": [{"id": "x", "mac": "", "ip": ""}]}, "x")
            is None
        )

    def test_append_reservation_leases(self):
        from app.services.kubevirt_reconfigure import _append_reservation_leases

        leases: list[dict] = []
        reserved: set[str] = set()
        _append_reservation_leases(
            leases,
            reserved,
            [
                {"ip": "10.0.0.100", "mac": "aa:bb", "name": "vip"},
                {"ip": "", "mac": "", "hostname": "empty"},
            ],
        )
        assert reserved == {"10.0.0.100"}
        assert leases[0]["hostname"] == "vip"
        assert leases[1]["hostname"] == "empty"


class TestCephReservationHelpers:
    def test_ceph_ips_and_hostname(self):
        from app.services.kubevirt_reconfigure import (
            _ceph_hostname_for_index,
            _ceph_ips_for_node,
        )

        assert _ceph_ips_for_node({"labIp": "10.0.0.4", "osdIps": ["10.0.0.5"]}) == [
            "10.0.0.4",
            "10.0.0.5",
        ]
        assert _ceph_ips_for_node({}) == [""]
        assert _ceph_hostname_for_index(0) == "ceph-mon"
        assert _ceph_hostname_for_index(2) == "ceph-osd-1"

    def test_ceph_reservations_skips_wrong_net_and_used(self):
        from app.services.kubevirt_reconfigure import _ceph_reservations

        nodes = [
            {"type": "vmNode", "data": {}},
            {
                "type": "cephClusterNode",
                "data": {
                    "networkRef": "other",
                    "labIp": "10.0.0.4",
                    "osdIps": ["10.0.0.5"],
                },
            },
            {
                "type": "cephClusterNode",
                "data": {
                    "networkRef": "net1",
                    "labIp": "10.0.0.4",
                    "osdIps": ["10.0.0.5", ""],
                },
            },
        ]
        reserved = {"10.0.0.4"}
        out = _ceph_reservations("net1", nodes, reserved)
        assert [r["ip"] for r in out] == ["10.0.0.5"]
        assert out[0]["hostname"] == "ceph-osd-0"
        assert "10.0.0.5" in reserved


class TestNetworkChangeHelpers:
    def test_delete_removed_skips_gateway(self):
        from app.services.kubevirt_reconfigure import _delete_removed_kubevirt_networks

        api = MagicMock()
        _delete_removed_kubevirt_networks(
            api,
            "ns",
            "project01",
            [
                {"id": "gw1", "data": {"subtype": "gateway"}},
                {"id": "net-abcd1234", "data": {}},
            ],
        )
        api.delete_namespaced_custom_object.assert_called_once()
        assert (
            api.delete_namespaced_custom_object.call_args.kwargs["name"]
            == "net-net-abcd"
        )

    def test_delete_removed_swallows_errors(self):
        from app.services.kubevirt_reconfigure import _delete_removed_kubevirt_networks

        api = MagicMock()
        api.delete_namespaced_custom_object.side_effect = RuntimeError("gone")
        _delete_removed_kubevirt_networks(api, "ns", "p1", [{"id": "n1", "data": {}}])

    @patch("app.services.kubevirt_reconfigure._create_troshkanetwork_cr")
    def test_create_added_sets_gateway_changed(self, mock_create):
        from app.services.kubevirt_reconfigure import _create_added_kubevirt_networks

        api = MagicMock()
        pending: list[str] = []
        errors: list[str] = []
        current = {
            "nodes": [
                {
                    "id": "net-lab01",
                    "type": "networkNode",
                    "data": {
                        "id": "net-lab01",
                        "cidr": "10.0.0.0/24",
                        "externalAccess": True,
                    },
                }
            ],
            "edges": [],
        }
        changed = _create_added_kubevirt_networks(
            api,
            "ns",
            "project01xx",
            current,
            [current["nodes"][0]],
            [],
            {"troshka-project": "project0"},
            pending,
            errors,
        )
        assert changed is True
        assert pending
        assert errors == []
        mock_create.assert_called_once()

    @patch("app.services.kubevirt_reconfigure._create_troshkanetwork_cr")
    def test_create_added_records_errors(self, mock_create):
        from app.services.kubevirt_reconfigure import _create_added_kubevirt_networks

        mock_create.side_effect = RuntimeError("boom")
        pending: list[str] = []
        errors: list[str] = []
        node = {
            "id": "net-lab01",
            "type": "networkNode",
            "data": {"id": "net-lab01", "cidr": "10.0.0.0/24"},
        }
        _create_added_kubevirt_networks(
            MagicMock(),
            "ns",
            "project01xx",
            {"nodes": [node], "edges": []},
            [node],
            [],
            {},
            pending,
            errors,
        )
        assert pending == []
        assert errors and "Failed to add network" in errors[0]

    def test_patch_changed_updates_spec(self):
        from app.services.kubevirt_reconfigure import _patch_changed_kubevirt_networks

        api = MagicMock()
        api.get_namespaced_custom_object.return_value = {"spec": {}}
        pending: list[str] = []
        errors: list[str] = []
        net = {
            "id": "net-lab01",
            "type": "networkNode",
            "data": {"id": "net-lab01", "cidr": "10.0.0.0/24", "mtu": 9000},
        }
        deployed_net = {
            "id": "net-lab01",
            "type": "networkNode",
            "data": {"id": "net-lab01", "cidr": "10.0.0.0/24"},
        }
        _patch_changed_kubevirt_networks(
            api,
            "ns",
            "project01xx",
            {"nodes": [net], "edges": []},
            {"nodes": [deployed_net], "edges": []},
            pending,
            errors,
        )
        api.replace_namespaced_custom_object.assert_called_once()
        assert pending
        assert errors == []


class TestCephChangeHelpers:
    def test_is_not_found_error(self):
        from app.services.kubevirt_reconfigure import _is_not_found_error

        assert _is_not_found_error(Exception("404 Not Found"))
        err = Exception("nope")
        err.status = 404  # type: ignore[attr-defined]
        assert _is_not_found_error(err)
        assert not _is_not_found_error(Exception("500"))

    def test_delete_troshka_ceph_ignores_404(self):
        from app.services.kubevirt_reconfigure import _delete_troshka_ceph_cr

        api = MagicMock()
        api.delete_namespaced_custom_object.side_effect = Exception("404")
        errors: list[str] = []
        _delete_troshka_ceph_cr(api, "ns", "project01", "troshka-ceph", errors)
        assert errors == []

    def test_delete_troshka_ceph_records_other_errors(self):
        from app.services.kubevirt_reconfigure import _delete_troshka_ceph_cr

        api = MagicMock()
        api.delete_namespaced_custom_object.side_effect = Exception("500 boom")
        errors: list[str] = []
        _delete_troshka_ceph_cr(api, "ns", "project01", "troshka-ceph", errors)
        assert errors and "Failed to remove Ceph" in errors[0]

    def test_upsert_patches_when_exists(self):
        from app.services.kubevirt_reconfigure import _upsert_troshka_ceph_cr

        api = MagicMock()
        errors: list[str] = []
        _upsert_troshka_ceph_cr(
            api, "ns", "project01", "troshka-ceph", {"spec": {}}, {"a": 1}, errors
        )
        api.patch_namespaced_custom_object.assert_called_once()
        api.create_namespaced_custom_object.assert_not_called()
        assert errors == []

    def test_upsert_creates_on_404(self):
        from app.services.kubevirt_reconfigure import _upsert_troshka_ceph_cr

        api = MagicMock()
        api.get_namespaced_custom_object.side_effect = Exception("404")
        errors: list[str] = []
        body = {"kind": "TroshkaCeph"}
        _upsert_troshka_ceph_cr(
            api, "ns", "project01", "troshka-ceph", body, {"a": 1}, errors
        )
        api.create_namespaced_custom_object.assert_called_once_with(
            group=api.create_namespaced_custom_object.call_args.kwargs["group"],
            version=api.create_namespaced_custom_object.call_args.kwargs["version"],
            namespace="ns",
            plural="troshkancephs",
            body=body,
        )
        assert errors == []

    def test_upsert_create_failure_recorded(self):
        from app.services.kubevirt_reconfigure import _upsert_troshka_ceph_cr

        api = MagicMock()
        api.get_namespaced_custom_object.side_effect = Exception("404")
        api.create_namespaced_custom_object.side_effect = Exception("create failed")
        errors: list[str] = []
        _upsert_troshka_ceph_cr(api, "ns", "project01", "troshka-ceph", {}, {}, errors)
        assert errors and "Failed to add Ceph" in errors[0]


class TestNicNetworkHelpers:
    def test_nic_network_ref_from_edge(self):
        from app.services.kubevirt_reconfigure import (
            _nic_network_ref_from_edge,
            _resolve_nic_networks,
        )

        net = {"type": "networkNode"}
        ctr = {"type": "containerNode"}
        edge = {"targetHandle": "nic-eth0-left", "sourceHandle": "nic-eth0-right"}
        assert _nic_network_ref_from_edge(net, ctr, edge, "net-aaaa1111", "c1") == (
            "nic-eth0",
            "net-net-aaaa",
        )
        assert _nic_network_ref_from_edge(ctr, net, edge, "c1", "net-bbbb2222") == (
            "nic-eth0",
            "net-net-bbbb",
        )
        assert _nic_network_ref_from_edge(net, net, edge, "a", "b") is None

        topo = {
            "nodes": [
                {"id": "net-lab01xx", "type": "networkNode"},
                {"id": "ctr-1", "type": "containerNode"},
            ],
            "edges": [
                {
                    "source": "net-lab01xx",
                    "target": "ctr-1",
                    "targetHandle": "nic-eth0-left",
                }
            ],
        }
        assert _resolve_nic_networks(topo) == {"nic-eth0": "net-net-lab0"}


class TestShowroomInfraHelpers:
    def test_collect_used_nic_ips(self):
        from app.services.kubevirt_reconfigure import _collect_used_nic_ips

        topo = {
            "nodes": [
                {"data": {"nics": [{"ip": "10.0.0.10"}, {"ip": ""}]}},
                {"data": {"nics": [{"ip": "10.0.0.11"}]}},
            ]
        }
        assert _collect_used_nic_ips(topo) == {"10.0.0.10", "10.0.0.11"}

    def test_append_missing_showroom_infra_nics(self):
        from app.services.kubevirt_reconfigure import (
            _append_missing_showroom_infra_nics,
        )

        used = {"10.0.0.9"}
        new_nics: list[dict] = []
        _append_missing_showroom_infra_nics(
            [("net-aaaa1111", "10.0.0.0/24"), ("net-bbbb2222", "10.1.0.0/24")],
            {"net-net-aaaa"},
            used,
            new_nics,
        )
        # first net already present; second gets an infra nic
        assert len(new_nics) == 1
        assert new_nics[0]["networkRef"] == "net-net-bbbb"
        assert new_nics[0]["ip"]
        assert new_nics[0]["ip"] in used

    def test_resolve_showroom_dns_by_name(self):
        from app.services.kubevirt_reconfigure import _resolve_showroom_dns_nameserver

        ctr = {"dnsNetwork": "lab"}
        topo = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {"name": "lab", "cidr": "10.0.0.0/24"},
                }
            ]
        }
        _resolve_showroom_dns_nameserver(topo, ctr, [("x", "10.0.0.0/24")])
        assert ctr["dnsNameserver"] == "10.0.0.2"

    def test_resolve_showroom_dns_fallback_dns_flag(self):
        from app.services.kubevirt_reconfigure import _resolve_showroom_dns_nameserver

        ctr: dict = {}
        topo = {
            "nodes": [
                {
                    "type": "networkNode",
                    "data": {"name": "other", "cidr": "10.1.0.0/24", "dns": True},
                }
            ]
        }
        _resolve_showroom_dns_nameserver(topo, ctr, [])
        assert ctr["dnsNameserver"] == "10.1.0.2"

    def test_resolve_showroom_dns_lab_nets_fallback(self):
        from app.services.kubevirt_reconfigure import _resolve_showroom_dns_nameserver

        ctr: dict = {}
        _resolve_showroom_dns_nameserver(
            {"nodes": [{"type": "vmNode", "data": {}}]},
            ctr,
            [("net1", "192.168.1.0/24")],
        )
        assert ctr["dnsNameserver"] == "192.168.1.2"

    def test_enrich_showroom_infra_networks_early_exit(self):
        from app.services.kubevirt_reconfigure import _enrich_showroom_infra_networks

        ctr = {"infraNetworking": False, "nics": [{"id": "n1"}]}
        _enrich_showroom_infra_networks({"nodes": []}, ctr)
        assert ctr["nics"] == [{"id": "n1"}]


class TestDiskPvcHelpers:
    def test_register_disk_pvcs_from_mounts(self):
        from app.services.kubevirt_reconfigure import _register_disk_pvcs_from_mounts

        disk_pvcs: dict[str, str] = {}
        seen: set[str] = set()
        _register_disk_pvcs_from_mounts(
            [
                {"diskNodeId": "disk-aaaa1111"},
                {"diskNodeId": ""},
                {"diskNodeId": "disk-aaaa1111"},
                {"diskNodeId": "disk-bbbb2222"},
            ],
            "showroom-xyz",
            disk_pvcs,
            seen,
        )
        assert disk_pvcs == {
            "disk-aaaa1111": "pod-showroom-disk-disk-aaa",
            "disk-bbbb2222": "pod-showroom-disk-disk-bbb",
        }

    def test_container_disk_pvcs_from_all_sources(self):
        from app.services.kubevirt_reconfigure import _container_disk_pvcs

        ctr = {
            "id": "showroom1",
            "mounts": [{"diskNodeId": "d1"}],
            "initContainers": [{"mounts": [{"diskNodeId": "d2"}]}],
            "podContainers": [{"mounts": [{"diskNodeId": "d3"}, {"diskNodeId": "d1"}]}],
        }
        pvcs = _container_disk_pvcs(ctr)
        assert set(pvcs) == {"d1", "d2", "d3"}
        assert all(v.startswith("pod-showroom-disk-") for v in pvcs.values())


class TestShowroomPodHelpers:
    def test_add_showroom_volume_mount_dedupes(self):
        from app.services.kubevirt_reconfigure import _add_showroom_volume_mount

        volumes: list[dict] = []
        mounts: list[dict] = []
        seen_vols: set[str] = set()
        seen_mounts: set[tuple[str, str]] = set()
        disk_pvcs = {"disk-aaaa": "pvc-1"}
        mount = {"diskNodeId": "disk-aaaa", "mountPath": "/data"}
        _add_showroom_volume_mount(
            mount, disk_pvcs, volumes, mounts, seen_vols, seen_mounts
        )
        _add_showroom_volume_mount(
            mount, disk_pvcs, volumes, mounts, seen_vols, seen_mounts
        )
        assert len(volumes) == 1
        assert len(mounts) == 1
        _add_showroom_volume_mount(
            {"diskNodeId": "", "mountPath": "/x"},
            disk_pvcs,
            volumes,
            mounts,
            seen_vols,
            seen_mounts,
        )
        assert len(volumes) == 1

    def test_collect_showroom_volumes_and_mounts(self):
        from app.services.kubevirt_reconfigure import (
            _collect_showroom_volumes_and_mounts,
        )

        ctr = {
            "mounts": [{"diskNodeId": "d1", "mountPath": "/a"}],
            "initContainers": [{"mounts": [{"diskNodeId": "d2", "mountPath": "/b"}]}],
            "podContainers": [{"mounts": [{"diskNodeId": "d1", "mountPath": "/c"}]}],
        }
        disk_pvcs = {"d1": "pvc-d1", "d2": "pvc-d2"}
        volumes, mounts = _collect_showroom_volumes_and_mounts(ctr, disk_pvcs)
        assert len(volumes) == 2
        assert {m["mountPath"] for m in mounts} == {"/a", "/b", "/c"}

    def test_build_showroom_init_and_containers(self):
        from app.services.kubevirt_reconfigure import (
            _build_showroom_containers,
            _build_showroom_init_containers,
            _container_ports_spec,
        )

        assert _container_ports_spec(
            [{"container_port": 80}, {"containerPort": 443}, {"port": 8080}]
        ) == [
            {"containerPort": 80, "protocol": "TCP"},
            {"containerPort": 443, "protocol": "TCP"},
            {"containerPort": 8080, "protocol": "TCP"},
        ]

        ctr = {
            "nics": [],
            "image": "fallback:latest",
            "initContainers": [
                {
                    "name": "init-0",
                    "image": "init:1",
                    "envVars": [{"key": "A", "value": "1"}],
                    "command": "echo hi",
                }
            ],
            "podContainers": [
                {
                    "name": "app",
                    "image": "app:1",
                    "ports": [{"port": 8080}],
                    "securityContext": {"runAsUser": 1000},
                    "command": ["app", "run"],
                }
            ],
        }
        vms = [{"name": "disk-x", "mountPath": "/data"}]
        inits = _build_showroom_init_containers(ctr, vms)
        assert inits[-1]["name"] == "init-0"
        assert inits[-1]["volumeMounts"] == vms
        assert inits[-1]["command"] == ["/bin/sh", "-c", "echo hi"]

        containers = _build_showroom_containers(ctr, vms)
        assert containers[0]["name"] == "app"
        assert containers[0]["ports"][0]["containerPort"] == 8080
        assert containers[0]["securityContext"]["runAsUser"] == 1000

        empty = _build_showroom_containers({"image": "solo:1", "podContainers": []}, [])
        assert empty == [{"name": "main", "image": "solo:1"}]

    @patch(
        "app.services.ocp.ops_pod_scaffold.lab_pod_dns_config",
        return_value={"nameservers": ["10.0.0.2"]},
    )
    def test_assemble_showroom_pod_body(self, _mock_dns):
        from app.services.kubevirt_reconfigure import (
            _NET_ANNOTATION,
            _assemble_showroom_pod_body,
        )

        body = _assemble_showroom_pod_body(
            "ns1",
            "pod-showroo",
            "showroo",
            {
                "dnsNameserver": "10.0.0.2",
                "nics": [{"networkRef": "net-lab01xx"}],
            },
            [],
            [{"name": "main", "image": "x"}],
            [{"name": "vol1"}],
            {"net-lab01xx": "nad-lab"},
            {"uid": "owner"},
        )
        assert body["metadata"]["name"] == "pod-showroo"
        assert body["spec"]["dnsPolicy"] == "None"
        assert body["spec"]["volumes"] == [{"name": "vol1"}]
        assert body["metadata"]["annotations"][_NET_ANNOTATION] == "nad-lab"

    def test_create_showroom_pod_ignores_409(self):
        from kubernetes.client.exceptions import ApiException

        from app.services.kubevirt_reconfigure import _create_showroom_pod

        core = MagicMock()
        core.create_namespaced_pod.side_effect = ApiException(status=409)
        _create_showroom_pod(
            core,
            "ns",
            {"id": "showroom1", "image": "x", "nics": [], "podContainers": []},
            {},
            {"uid": "o"},
        )

    def test_create_showroom_pod_raises_non_409(self):
        from kubernetes.client.exceptions import ApiException

        from app.services.kubevirt_reconfigure import _create_showroom_pod

        core = MagicMock()
        core.create_namespaced_pod.side_effect = ApiException(status=500)
        with pytest.raises(ApiException):
            _create_showroom_pod(
                core,
                "ns",
                {"id": "showroom1", "image": "x", "nics": [], "podContainers": []},
                {},
                {"uid": "o"},
            )
