"""Unit tests for template_loader helpers extracted for Sonar S3776."""

from unittest.mock import MagicMock, patch

import pytest

from app.services.template_loader import (
    _append_boundary_edges_for_cluster,
    _append_nic_network_id,
    _apply_topology_cluster_optionals,
    _apply_vm_machine_and_bus,
    _apply_vm_pxe_and_serial,
    _apply_vm_role_tags,
    _apply_vm_smbios_uuid,
    _assemble_topology_result,
    _build_one_topology_cluster,
    _cluster_boundary_anchor_edge,
    _collect_hidden_node_ids,
    _collect_iso_item_ids,
    _copy_ocp_export_fields,
    _edge_matches_controller_storage,
    _ensure_showroom_dns_network,
    _export_ocp_cluster_flags_and_disks,
    _export_one_ocp_cluster,
    _export_standalone_ocp_vm_flags,
    _export_vm_bmc_fields,
    _export_vm_tags_if_custom,
    _library_ids_from_storage_nodes,
    _network_ids_from_cluster_members,
    _node_id_to_name_map,
    _partition_container_nodes,
    _showroom_resolver_ips,
    _storage_node_for_controller,
    _vm_icon_for_os,
    build_topology_clusters,
)


class TestBuildTopologyClusterHelpers:
    def test_apply_optionals_distribution_and_disks(self):
        entry = {"distribution": "okd-scos", "networks": ["priv"]}
        cluster_obj = {}
        normalized = {
            "controlPlaneDisks": [{"sizeGb": 120}],
            "workerDisks": [{"sizeGb": 100}],
            "networkIds": ["n1"],
        }
        with patch(
            "app.services.ocp.client_mirror.normalize_distribution",
            return_value="okd-scos",
        ):
            _apply_topology_cluster_optionals(entry, cluster_obj, normalized, 2)
        assert cluster_obj["ocpDistribution"] == "okd-scos"
        assert cluster_obj["installWorkers"] == 2
        assert cluster_obj["controlPlaneDisks"] == [{"sizeGb": 120}]
        assert cluster_obj["workerDisks"] == [{"sizeGb": 100}]
        assert cluster_obj["networkIds"] == ["n1"]
        assert cluster_obj["_networkNames"] == ["priv"]

    def test_build_one_topology_cluster_sno_defaults(self):
        entry = {"name": "hub", "type": "sno"}
        with patch(
            "app.services.template_loader._count_cluster_roles", return_value=(1, 0)
        ):
            obj = _build_one_topology_cluster(entry, {}, True)
        assert obj["id"] == "hub"
        assert obj["type"] == "sno"
        assert obj["controlPlane"] == 1
        assert obj["recert"] is True
        assert obj["monitorHealth"] is True

    def test_build_topology_clusters_multi(self):
        with patch(
            "app.services.template_loader._build_one_topology_cluster",
            side_effect=lambda e, *_: {"name": e["name"]},
        ):
            out = build_topology_clusters([{"name": "a"}, {"name": "b"}], vms_def=None)
        assert out == [{"name": "a"}, {"name": "b"}]


class TestClusterBoundaryHelpers:
    def test_anchor_edge_top_vs_bottom(self):
        top = _cluster_boundary_anchor_edge("net1", "cluster-c1", False)
        assert top["sourceHandle"] == "bottom"
        assert top["targetHandle"] == "cluster-net-top"
        assert top["type"] == "clusterAnchor"
        bot = _cluster_boundary_anchor_edge("net1", "cluster-c1", True)
        assert bot["sourceHandle"] == "top"
        assert bot["targetHandle"] == "cluster-net-bottom"

    def test_append_boundary_edges_skips_missing_and_duplicates(self):
        c = {"id": "c1", "networkIds": ["net1", "missing", "net1"]}
        node_by_id = {
            "cluster-c1": {"id": "cluster-c1", "data": {}},
            "net1": {"id": "net1", "data": {"networkType": "private"}},
        }
        node_ids = set(node_by_id)
        linked = set()
        edges = []
        _append_boundary_edges_for_cluster(c, node_by_id, node_ids, linked, edges)
        assert len(edges) == 1
        assert ("net1", "cluster-c1") in linked
        # second call is a no-op (already linked)
        _append_boundary_edges_for_cluster(c, node_by_id, node_ids, linked, edges)
        assert len(edges) == 1

    def test_append_boundary_skips_missing_boundary(self):
        edges = []
        _append_boundary_edges_for_cluster(
            {"id": "gone", "networkIds": ["n"]}, {}, set(), set(), edges
        )
        assert edges == []


class TestVmOptionalFieldHelpers:
    def test_smbios_uuid_valid_and_invalid(self):
        data = {}
        uid = "550e8400-e29b-41d4-a716-446655440000"
        _apply_vm_smbios_uuid("vm1", {"uuid": uid}, data)
        assert data["smbiosUuid"] == uid
        with pytest.raises(ValueError, match="invalid uuid"):
            _apply_vm_smbios_uuid("vm1", {"uuid": "not-a-uuid"}, {})

    def test_smbios_uuid_absent_is_noop(self):
        data = {}
        _apply_vm_smbios_uuid("vm1", {}, data)
        assert data == {}

    def test_pxe_and_serial_aliases(self):
        data = {}
        _apply_vm_pxe_and_serial(
            {
                "pxe_boot_iso_id": "iso1",
                "pxe_boot_iso_name": "boot.iso",
                "serial_exec": "IOS",
                "headless": True,
            },
            data,
        )
        assert data["pxeBootIsoId"] == "iso1"
        assert data["pxeBootIsoName"] == "boot.iso"
        assert data["serialExecType"] == "ios"
        assert data["headless"] is True

    def test_machine_and_bus_aliases(self):
        data = {}
        _apply_vm_machine_and_bus({"machineType": "q35", "legacyRootBus": True}, data)
        assert data["machineType"] == "q35"
        assert data["legacyRootBus"] is True

    def test_role_tags_defaults(self):
        for role, group in (
            ("control-plane", "controllers"),
            ("worker", "workers"),
            ("bastion", "bastions,showroom"),
        ):
            data = {}
            _apply_vm_role_tags({}, data, role)
            assert data["tags"] == {"AnsibleGroup": group}

    def test_role_tags_explicit_override(self):
        data = {}
        _apply_vm_role_tags({"tags": {"env": "lab"}}, data, "worker")
        assert data["tags"] == {"env": "lab"}


class TestVmIconAndInferNetworks:
    def test_vm_icon_for_os(self):
        assert _vm_icon_for_os("blank") == "\U0001f4e6"
        assert _vm_icon_for_os("rhcos") == "\U0001f5a5"

    def test_append_nic_network_id_skips_bmc_and_dupes(self):
        ids = []
        nets_def = {"priv": {"type": "private"}, "bmc": {"type": "bmc"}}
        net_ids = {"priv": "nid-priv", "bmc": "nid-bmc"}
        _append_nic_network_id({"network": "bmc"}, nets_def, net_ids, ids)
        assert ids == []
        _append_nic_network_id({"network": "priv"}, nets_def, net_ids, ids)
        _append_nic_network_id({"network": "priv"}, nets_def, net_ids, ids)
        assert ids == ["nid-priv"]

    def test_network_ids_from_cluster_members(self):
        vms_def = {
            "cp-0": {"nics": [{"network": "priv"}, {"network": "bmc"}]},
            "other": {"nics": [{"network": "priv2"}]},
        }
        ids = _network_ids_from_cluster_members(
            "c1",
            vms_def,
            {"cp-0": "c1", "other": "c2"},
            {"priv": "n1", "bmc": "nb", "priv2": "n2"},
            {"priv": {"type": "private"}, "bmc": {"type": "bmc"}, "priv2": {}},
        )
        assert ids == ["n1"]


class TestShowroomAndAssembleHelpers:
    def test_showroom_resolver_ips_uses_dns_network(self):
        showroom_cfg = {"dns_network": "dnsnet"}
        nets_def = {"dnsnet": {"cidr": "10.0.0.0/24", "dns_server_ip": "10.0.0.53"}}
        with patch(
            "app.services.showroom_scaffold.dns_network_resolver_ips",
            return_value=["10.0.0.53"],
        ) as mock_dns:
            out = _showroom_resolver_ips(showroom_cfg, nets_def, "gw")
        assert out == ["10.0.0.53"]
        mock_dns.assert_called_once_with("10.0.0.0/24", "10.0.0.53")

    def test_ensure_showroom_dns_network(self):
        meta = {}
        node = {"data": {}}
        with patch(
            "app.services.template_loader._default_dns_network_name",
            return_value="dnsnet",
        ):
            _ensure_showroom_dns_network(meta, node, {}, None)
        assert meta["dns_network"] == "dnsnet"
        assert node["data"]["dnsNetwork"] == "dnsnet"

    def test_ensure_showroom_dns_noop_when_set(self):
        meta = {"dns_network": "already"}
        node = {"data": {}}
        _ensure_showroom_dns_network(meta, node, {}, None)
        assert meta["dns_network"] == "already"
        assert "dnsNetwork" not in node["data"]

    def test_collect_hidden_node_ids(self):
        assert _collect_hidden_node_ids(
            {"hidden_nodes": ["a", "missing", "b"]},
            {"a": "id-a", "b": "id-b"},
        ) == ["id-a", "id-b"]

    def test_assemble_topology_result_optionals(self):
        result = _assemble_topology_result(
            [],
            [],
            [],
            [],
            [],
            [],
            {"enabled": True},
            {
                "placement": {"requires_kubevirt": True},
                "workloads": ["w"],
                "requirements_content": "req",
                "workloadsDone": True,
            },
        )
        assert result["showroom"] == {"enabled": True}
        assert result["placement"]["requires_kubevirt"] is True
        assert result["workloads"] == ["w"]
        assert result["requirements_content"] == "req"
        assert result["workloadsDone"] is True

    def test_assemble_falls_back_to_tmpl_showroom(self):
        result = _assemble_topology_result(
            [], [], [], [], [], [], None, {"showroom": {"enabled": False}}
        )
        assert result["showroom"] == {"enabled": False}


class TestExportHelpers:
    def test_standalone_ocp_flags_skipped_for_cluster_member(self):
        out = {}
        _export_standalone_ocp_vm_flags(
            {
                "clusterId": "c1",
                "recertEnabled": True,
                "ocpMonitor": True,
                "configureBastionBrowser": True,
            },
            out,
        )
        assert out == {}

    def test_standalone_ocp_flags_for_non_member(self):
        out = {}
        _export_standalone_ocp_vm_flags(
            {
                "recertEnabled": True,
                "ocpMonitor": True,
                "configureBastionBrowser": True,
            },
            out,
        )
        assert out == {
            "recert": True,
            "ocp_monitor": True,
            "configure_bastion_browser": True,
        }

    def test_export_vm_bmc_fields(self):
        out = {"role": "worker"}
        _export_vm_bmc_fields({"bmcEnabled": True, "bmcIp": "10.0.0.9"}, out)
        assert out["bmc"] is True
        assert out["bmc_ip"] == "10.0.0.9"
        out2 = {"role": "control-plane"}
        _export_vm_bmc_fields({"bmcEnabled": True}, out2)
        assert "bmc" not in out2

    def test_export_vm_tags_if_custom(self):
        out = {}
        _export_vm_tags_if_custom({"tags": {"AnsibleGroup": "controllers"}}, out)
        assert out == {}
        _export_vm_tags_if_custom({"tags": {"env": "lab"}}, out)
        assert out["tags"] == {"env": "lab"}

    def test_copy_ocp_export_fields_skips_none(self):
        entry = {}
        _copy_ocp_export_fields({"name": "hub", "apiVip": None, "workers": 2}, entry)
        assert entry["name"] == "hub"
        assert entry["workers"] == 2
        assert "api_vip" not in entry

    def test_export_ocp_cluster_flags_and_disks(self):
        entry = {}
        _export_ocp_cluster_flags_and_disks(
            {
                "recert": True,
                "monitorHealth": True,
                "configureBastionBrowser": True,
                "controlPlaneDisks": [{"sizeGb": 120}],
                "workerDisks": [{"sizeGb": 100}],
            },
            entry,
        )
        assert entry["recert"] is True
        assert entry["ocp_monitor"] is True
        assert entry["configure_bastion_browser"] is True
        assert entry["control_plane_disks"] == [{"sizeGb": 120}]
        assert entry["worker_disks"] == [{"sizeGb": 100}]

    def test_export_one_ocp_cluster_networks(self):
        cluster = {
            "name": "hub",
            "type": "sno",
            "controlPlane": 1,
            "workers": 0,
            "networkIds": ["nid1", "nid2"],
            "recert": True,
        }
        entry = _export_one_ocp_cluster(cluster, {"nid1": "priv", "nid2": "mgmt"})
        assert entry["name"] == "hub"
        assert entry["networks"] == ["priv", "mgmt"]
        assert entry["recert"] is True


class TestStorageAndIsoHelpers:
    def test_edge_matches_controller_storage_both_directions(self):
        storage_ids = {"s1"}
        fwd = {
            "target": "vm1",
            "targetHandle": "disk-dc1",
            "source": "s1",
        }
        assert _edge_matches_controller_storage(fwd, "vm1", "dc1", storage_ids) == "s1"
        rev = {
            "source": "vm1",
            "sourceHandle": "disk-dc1",
            "target": "s1",
        }
        assert _edge_matches_controller_storage(rev, "vm1", "dc1", storage_ids) == "s1"
        assert (
            _edge_matches_controller_storage(fwd, "vm1", "other", storage_ids) is None
        )

    def test_storage_node_for_controller(self):
        assert _storage_node_for_controller("vm1", "", {}) == ""
        topo = {
            "nodes": [{"id": "s1", "type": "storageNode"}],
            "edges": [
                {
                    "target": "vm1",
                    "targetHandle": "ctrl-dc9",
                    "source": "s1",
                }
            ],
        }
        assert _storage_node_for_controller("vm1", "dc9", topo) == "s1"

    def test_library_ids_from_storage_nodes(self):
        nodes = [
            {"type": "storageNode", "data": {"libraryItemId": "a"}},
            {"type": "storageNode", "data": {}},
            {"type": "vmNode", "data": {"libraryItemId": "x"}},
        ]
        assert _library_ids_from_storage_nodes(nodes) == ["a"]

    def test_collect_iso_item_ids_mocks_db(self):
        assert _collect_iso_item_ids([], None) == set()
        db = MagicMock()
        row = MagicMock(id="iso1", format="iso")
        db.query.return_value.filter.return_value.all.return_value = [
            row,
            MagicMock(id="qcow", format="qcow2"),
        ]
        nodes = [{"type": "storageNode", "data": {"libraryItemId": "iso1"}}]
        with patch("app.models.library.LibraryItem"):
            assert _collect_iso_item_ids(nodes, db) == {"iso1"}


class TestExportIndexHelpers:
    def test_partition_container_nodes(self):
        nodes = [
            {"type": "containerNode", "data": {"isShowroom": False, "name": "a"}},
            {"type": "containerNode", "data": {"isShowroom": True, "name": "sr"}},
            {"type": "vmNode", "data": {}},
        ]
        containers, showrooms = _partition_container_nodes(nodes)
        assert len(containers) == 1 and containers[0]["data"]["name"] == "a"
        assert len(showrooms) == 1 and showrooms[0]["data"]["name"] == "sr"

    def test_node_id_to_name_map(self):
        nodes = [
            {"id": "1", "data": {"name": "vm1"}},
            {"id": "2", "data": {"label": "net1"}},
            {"id": "abcdef12", "data": {}},
        ]
        assert _node_id_to_name_map(nodes) == {
            "1": "vm1",
            "2": "net1",
            "abcdef12": "abcdef12",
        }

    def test_index_topology_for_export_mocks_db_iso_lookup(self):
        from app.services.template_loader import _index_topology_for_export

        topology = {
            "nodes": [
                {
                    "id": "n1",
                    "type": "networkNode",
                    "data": {"name": "priv", "subtype": "private"},
                },
                {
                    "id": "gw",
                    "type": "networkNode",
                    "data": {"subtype": "gateway", "name": "gw"},
                },
                {"id": "v1", "type": "vmNode", "data": {"name": "vm1"}},
                {
                    "id": "s1",
                    "type": "storageNode",
                    "data": {"libraryItemId": "lib-iso"},
                },
            ],
            "edges": [{"target": "v1", "source": "n1"}],
        }
        db = MagicMock()
        with patch(
            "app.services.template_loader._collect_iso_item_ids",
            return_value={"lib-iso"},
        ) as mock_iso:
            (
                nodes,
                edges,
                iso_ids,
                net_nodes,
                vm_nodes,
                edge_by_target,
                net_names,
            ) = _index_topology_for_export(topology, db)
        mock_iso.assert_called_once_with(nodes, db)
        assert iso_ids == {"lib-iso"}
        assert "n1" in net_nodes and "gw" in net_nodes
        assert "gw" not in net_names  # gateway skipped
        assert net_names["n1"] == "priv"
        assert len(vm_nodes) == 1
        assert edge_by_target["v1"][0]["source"] == "n1"


class TestBastionIpCollectors:
    def test_collects_nics_vips_and_dns(self):
        from app.services.template_loader import (
            _collect_dns_record_ips,
            _collect_nic_ips_on_network,
            _collect_ocp_vip_ips,
            _collect_used_host_ips,
        )

        resolved = {
            "vms": {
                "a": {
                    "nics": [
                        {"network": "cluster", "ip": "10.0.0.10"},
                        {"network": "bmc", "ip": "192.168.100.10"},
                        {"network": "cluster"},
                    ]
                }
            },
            "ocp": {"api_vip": "10.0.0.2", "ingress_vip": "10.0.0.3"},
            "networks": {
                "cluster": {
                    "dns_records": [{"ip": "10.0.0.4"}, {"name": "empty"}],
                }
            },
        }
        assert _collect_nic_ips_on_network(resolved, "cluster") == {"10.0.0.10"}
        assert _collect_ocp_vip_ips(resolved) == {"10.0.0.2", "10.0.0.3"}
        assert _collect_dns_record_ips(resolved, "cluster") == {"10.0.0.4"}
        assert _collect_used_host_ips(resolved, "cluster") == {
            "10.0.0.10",
            "10.0.0.2",
            "10.0.0.3",
            "10.0.0.4",
        }

    def test_pick_bastion_uses_fallback_cidr(self):
        from app.services.template_loader import _pick_bastion_cluster_ip

        ip = _pick_bastion_cluster_ip({"vms": {}, "networks": {}}, "cluster")
        assert ip.endswith(".50")
