"""Unit tests for deploy_topology.py helpers extracted for Sonar S3776."""

from app.services.deploy_topology import (
    _add_node_used_ips,
    _apply_disk_image_source,
    _apply_library_disk_image,
    _apply_pattern_disk_image,
    _apply_showroom_gateway_mode,
    _bind_one_forward_eip,
    _bind_showroom_forwards_to_eip,
    _ceph_lab_ip_cidr_error,
    _ceph_lab_ip_conflict_error,
    _claim_cidr_infra_ips,
    _claim_cluster_node_vips,
    _claim_dns_records_and_lb,
    _claim_nic_ips_for_network,
    _claim_topology_cluster_vips,
    _claim_vip_keys,
    _cluster_infra_overlap_warnings,
    _collect_ips_on_network,
    _container_lab_network_entry,
    _cp_primary_ip,
    _desired_showroom_gateway_mode,
    _edge_container_nic_binding,
    _ensure_showroom_restrict_outbound,
    _filter_non_showroom_web_forwards,
    _find_control_plane_vm,
    _find_gateway_node,
    _is_control_plane_vm,
    _is_showroom_managed_forward,
    _keep_valid_osd_ips,
    _maybe_strip_unused_auto_external_ip,
    _merge_disk_storage_data,
    _network_containing_ip,
    _validate_showroom_dns_config,
    _vm_infra_overlap_warnings,
)


class TestClaimIpHelpers:
    def test_claim_cidr_infra_ips(self):
        claimed = {}
        _claim_cidr_infra_ips("10.0.0.0/24", claimed)
        assert claimed["10.0.0.1"] == "gateway"
        assert claimed["10.0.0.2"] == "dnsmasq"

    def test_claim_cidr_infra_ips_invalid(self):
        claimed = {}
        _claim_cidr_infra_ips("not-a-cidr", claimed)
        assert claimed == {}

    def test_claim_cidr_infra_ips_empty(self):
        claimed = {}
        _claim_cidr_infra_ips("", claimed)
        assert claimed == {}

    def test_claim_dns_records_and_lb(self):
        claimed = {}
        _claim_dns_records_and_lb(
            {
                "dnsRecords": [{"ip": "10.0.0.50", "name": "app"}],
                "lbIp": "10.0.0.100",
            },
            claimed,
        )
        assert claimed["10.0.0.50"] == "DNS record 'app'"
        assert claimed["10.0.0.100"] == "load balancer"

    def test_claim_nic_ips_for_network(self):
        topology = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "name": "bastion",
                        "nics": [{"id": "n1", "ip": "10.0.0.10"}],
                    },
                },
                {"id": "skip", "type": "storageNode", "data": {}},
            ]
        }
        claimed = {}
        _claim_nic_ips_for_network(topology, "net1", {"n1": "net1"}, claimed)
        assert claimed["10.0.0.10"] == "VM/container 'bastion'"

    def test_claim_vip_keys_snake_fallback(self):
        claimed = {}
        _claim_vip_keys({"api_vip": "10.0.0.20"}, "ocp", claimed, snake_fallback=True)
        assert claimed["10.0.0.20"] == "cluster 'ocp' API VIP"

    def test_claim_topology_and_cluster_node_vips(self):
        topology = {
            "clusters": [
                {
                    "name": "c1",
                    "networkIds": ["net1"],
                    "apiVip": "10.0.0.30",
                    "ingressVip": "10.0.0.31",
                }
            ],
            "nodes": [
                {
                    "type": "clusterNode",
                    "data": {"name": "c2", "apiVip": "10.0.0.40"},
                }
            ],
        }
        claimed = {}
        _claim_topology_cluster_vips(topology, "net1", claimed)
        _claim_cluster_node_vips(topology, claimed)
        assert claimed["10.0.0.30"].startswith("cluster 'c1'")
        assert claimed["10.0.0.40"].startswith("cluster 'c2'")

    def test_collect_ips_on_network_composes(self):
        topology = {
            "nodes": [
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"cidr": "192.168.1.0/24", "lbIp": "192.168.1.50"},
                }
            ],
            "clusters": [],
        }
        claimed = _collect_ips_on_network(topology, "net1")
        assert claimed["192.168.1.1"] == "gateway"
        assert claimed["192.168.1.50"] == "load balancer"
        assert _collect_ips_on_network(topology, "missing") == {}


class TestCephLabIpHelpers:
    def test_cidr_error_outside(self):
        err = _ceph_lab_ip_cidr_error("10.0.0.5", "192.168.0.0/24", "ceph", "lab")
        assert err and "outside" in err

    def test_cidr_error_invalid(self):
        err = _ceph_lab_ip_cidr_error("nope", "10.0.0.0/24", "ceph", "lab")
        assert err and "invalid" in err

    def test_cidr_ok(self):
        assert _ceph_lab_ip_cidr_error("10.0.0.4", "10.0.0.0/24", "ceph", "lab") is None

    def test_conflict_claimed(self):
        err = _ceph_lab_ip_conflict_error(
            "10.0.0.4", "ceph", "lab", {}, {"10.0.0.4": "gateway"}
        )
        assert err and "conflicts" in err

    def test_conflict_none(self):
        assert _ceph_lab_ip_conflict_error("10.0.0.4", "ceph", "lab", {}, {}) is None


class TestInfraOverlapHelpers:
    def test_vm_warning(self):
        node = {
            "id": "abcdefgh",
            "type": "vmNode",
            "data": {"name": "vm1", "nics": [{"ip": "10.0.0.1"}]},
        }
        warns = _vm_infra_overlap_warnings(node, {"10.0.0.1": "gateway"})
        assert len(warns) == 1
        assert "gateway" in warns[0]

    def test_cluster_warning(self):
        node = {
            "type": "clusterNode",
            "data": {"name": "ocp", "apiVip": "10.0.0.2"},
        }
        warns = _cluster_infra_overlap_warnings(node, {"10.0.0.2": "dnsmasq"})
        assert any("API VIP" in w for w in warns)


class TestShowroomHelpers:
    def test_find_gateway_node(self):
        topo = {
            "nodes": [
                {"id": "n1", "type": "networkNode", "data": {"subtype": "network"}},
                {"id": "gw", "type": "networkNode", "data": {"subtype": "gateway"}},
            ]
        }
        assert _find_gateway_node(topo)["id"] == "gw"
        assert _find_gateway_node({"nodes": []}) is None

    def test_validate_showroom_dns_no_name(self):
        errs = _validate_showroom_dns_config({"nodes": []})
        assert errs and "Enable DNS" in errs[0]

    def test_is_showroom_managed_forward(self):
        assert _is_showroom_managed_forward({"managedByShowroom": True})
        assert _is_showroom_managed_forward(
            {"extPort": "443", "intPort": "80", "intIp": "172.30.1.3"}
        )
        assert not _is_showroom_managed_forward(
            {"extPort": "6443", "intPort": "6443", "intIp": "10.0.0.1"}
        )

    def test_filter_non_showroom_web_forwards(self):
        merged = [
            {"extPort": "443", "intIp": "10.0.0.5"},
            {"extPort": "443", "intIp": "172.30.1.3"},
            {"extPort": "6443", "intIp": "10.0.0.5"},
            {"extPort": "80", "managedByShowroom": True},
        ]
        out = _filter_non_showroom_web_forwards(merged)
        assert len(out) == 3
        assert all(
            str(pf.get("extPort")) != "443"
            or pf.get("intIp", "").startswith("172.30.")
            or pf.get("managedByShowroom")
            for pf in out
        )

    def test_bind_forward_eip_route_web(self):
        pf = {"extPort": "443", "intPort": "80", "intIp": "172.30.1.3", "extIpId": "e1"}
        entry = _bind_one_forward_eip(pf, "eip-1", route_web=True)
        assert entry.get("managedByShowroom") is True
        assert "extIpId" not in entry

    def test_bind_forward_eip_cloud(self):
        pf = {"extPort": "22", "intPort": "22"}
        entry = _bind_one_forward_eip(pf, "eip-1", route_web=False)
        assert entry["extIpId"] == "eip-1"

    def test_bind_showroom_forwards_noop_without_eip(self):
        merged = [{"extPort": "22"}]
        assert _bind_showroom_forwards_to_eip(merged, "", False) is merged

    def test_desired_gateway_mode(self):
        assert _desired_showroom_gateway_mode([{"managedByShowroom": True}]) == (
            "nat-portforward"
        )
        assert _desired_showroom_gateway_mode([{"extPort": "22"}]) == "nat"

    def test_apply_gateway_mode(self):
        data = {"gatewayMode": "nat"}
        assert _apply_showroom_gateway_mode(data, [{"managedByShowroom": True}])
        assert data["gatewayMode"] == "nat-portforward"
        assert not _apply_showroom_gateway_mode(data, [{"managedByShowroom": True}])

    def test_ensure_restrict_outbound(self):
        data = {"outboundPolicy": "restrict", "outboundPorts": "80"}
        assert _ensure_showroom_restrict_outbound(data)
        assert "53" in data["outboundPorts"]
        assert "443" in data["outboundPorts"]
        assert not _ensure_showroom_restrict_outbound({"outboundPolicy": "allow-all"})

    def test_maybe_strip_unused_auto_eip(self):
        topo = {"externalIps": [{"id": "e1", "name": "IP-1", "ip": ""}]}
        assert _maybe_strip_unused_auto_external_ip(topo, [])
        assert topo["externalIps"] == []
        topo2 = {
            "externalIps": [{"id": "e1", "name": "IP-1", "ip": ""}],
        }
        assert not _maybe_strip_unused_auto_external_ip(topo2, [{"extIpId": "e1"}])


class TestClusterEgressHelpers:
    def test_is_control_plane_vm(self):
        assert _is_control_plane_vm(
            {"clusterId": "c1", "clusterRole": "control-plane"}, "c1"
        )
        assert _is_control_plane_vm(
            {"clusterId": "c1", "tags": {"AnsibleGroup": "controllers"}}, "c1"
        )
        assert not _is_control_plane_vm(
            {"clusterId": "c1", "clusterRole": "worker"}, "c1"
        )

    def test_find_control_plane_and_ip(self):
        topo = {
            "nodes": [
                {
                    "id": "cp",
                    "type": "vmNode",
                    "data": {
                        "clusterId": "c1",
                        "clusterRole": "control-plane",
                        "nics": [{"ip": "10.0.0.10"}],
                    },
                }
            ]
        }
        cp = _find_control_plane_vm("c1", topo)
        assert cp["id"] == "cp"
        assert _cp_primary_ip(cp) == "10.0.0.10"
        assert _find_control_plane_vm("other", topo) is None

    def test_network_containing_ip(self):
        import ipaddress

        nodes_by_id = {
            "net1": {
                "id": "net1",
                "type": "networkNode",
                "data": {"subtype": "network", "cidr": "10.0.0.0/24"},
            }
        }
        addr = ipaddress.ip_address("10.0.0.10")
        node = _network_containing_ip({"net1"}, nodes_by_id, addr)
        assert node["id"] == "net1"
        assert (
            _network_containing_ip(
                {"net1"}, nodes_by_id, ipaddress.ip_address("9.9.9.9")
            )
            is None
        )


class TestContainerNetworkHelpers:
    def test_edge_binding(self):
        edge = {
            "source": "ctr",
            "target": "net1",
            "sourceHandle": "nic-abc-left",
            "targetHandle": "",
        }
        nic_id, net_id = _edge_container_nic_binding(edge, "ctr")
        assert nic_id == "abc"
        assert net_id == "net1"
        assert _edge_container_nic_binding(edge, "other") == (None, None)

    def test_container_lab_network_entry(self):
        entry = _container_lab_network_entry(
            {"mac": "aa:bb", "model": "e1000", "ip": "10.0.0.5"},
            "nic1",
            42,
            {"cidr": "10.0.0.0/24", "dhcpGateway": "10.0.0.1"},
        )
        assert entry["bridge"] == "br-42"
        assert entry["nic_id"] == "nic1"
        assert entry["gateway"] == "10.0.0.1"


class TestUsedIpAndOsdHelpers:
    def test_add_node_used_ips(self):
        used: set[str] = set()
        _add_node_used_ips(
            {
                "type": "cephClusterNode",
                "data": {"nics": [{"ip": "10.0.0.5"}], "osdIps": ["10.0.0.200"]},
            },
            used,
        )
        assert "10.0.0.5" in used
        assert "10.0.0.200" in used
        _add_node_used_ips(
            {"type": "networkNode", "data": {"cidr": "10.0.0.0/24"}}, used
        )
        assert "10.0.0.1" in used

    def test_keep_valid_osd_ips(self):
        kept = _keep_valid_osd_ips(
            ["10.0.0.250", "10.0.0.1", "10.0.0.249", "10.0.0.250"],
            {"10.0.0.250", "10.0.0.249", "10.0.0.1"},
            {"10.0.0.1"},
            2,
        )
        assert kept == ["10.0.0.250", "10.0.0.249"]


class TestDiskSpecHelpers:
    def test_merge_disk_storage_data(self):
        topo = {
            "nodes": [
                {
                    "id": "s1",
                    "type": "storageNode",
                    "data": {"format": "qcow2", "size": 40},
                }
            ]
        }
        sd = _merge_disk_storage_data(
            {
                "node_id": "s1",
                "resolvedS3Path": "library/x.qcow2",
                "centralSource": True,
                "diskSource": "central",
            },
            topo,
        )
        assert sd["resolvedS3Path"] == "library/x.qcow2"
        assert sd["centralSource"] is True
        assert sd["diskSource"] == "central"

    def test_apply_pattern_and_library(self):
        spec: dict = {}
        assert _apply_pattern_disk_image(
            spec,
            {"patternId": "p1"},
            {"patternDiskId": "d1"},
            True,
            10,
        )
        assert "patternImage" in spec
        assert spec["sourceSizeGb"] == 10

        lib_spec: dict = {}
        assert _apply_library_disk_image(
            lib_spec, {"libraryItemId": "L1"}, {}, "qcow2", False, 0
        )
        assert lib_spec["libraryImage"]["s3Path"] == "library/L1.qcow2"
        assert "sourceSizeGb" not in lib_spec

    def test_apply_disk_image_source_blank(self):
        spec: dict = {}
        _apply_disk_image_source(spec, {}, {}, "blank", "qcow2", False)
        assert spec["blank"] is True

    def test_apply_disk_image_source_snapshot(self):
        spec: dict = {}
        _apply_disk_image_source(
            spec,
            {},
            {"resolvedS3Path": "snapshots/x.qcow2"},
            "snapshot",
            "qcow2",
            True,
        )
        assert spec["libraryImage"]["s3Path"] == "snapshots/x.qcow2"
