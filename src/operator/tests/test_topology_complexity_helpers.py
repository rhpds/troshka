"""Pure-helper unit tests for topology extractors (no cluster / k8s)."""

from helpers.topology import (
    _attach_disk_source,
    _build_disk_from_storage,
    _build_vm_entry,
    _ceph_ip_hostnames,
    _cluster_id_from_boundary,
    _dns_nameserver_for_showroom,
    _edge_other_end,
    _is_eligible_egress_network,
    _linked_cluster_ids,
    _pick_egress_from,
    _resolve_showroom_dns_cidr,
    _resolve_vm_firmware,
    _should_skip_showroom_enrich,
    cluster_egress_network_id,
    collect_container_disk_mounts,
    extract_vms,
)


def test_resolve_vm_firmware_uefi_secure():
    assert _resolve_vm_firmware({"firmware": "uefi", "secureBoot": True}) == (
        "uefi-secure"
    )
    assert _resolve_vm_firmware({"firmware": "uefi"}) == "uefi"
    assert _resolve_vm_firmware({}) == "bios"


def test_build_vm_entry_optional_flags_and_pxe_cdrom():
    node = {"id": "vm-1"}
    data = {
        "id": "vm-1",
        "label": "bastion",
        "headless": False,
        "legacyRootBus": True,
        "pxeBootIsoId": "iso-9",
        "pxeBootIsoS3Path": "library/iso-9.iso",
    }
    vm = _build_vm_entry(data, node)
    assert vm["headless"] is False
    assert vm["legacyRootBus"] is True
    assert vm["cdrom"] == {
        "libraryIsoId": "iso-9",
        "s3Path": "library/iso-9.iso",
    }


def test_extract_vms_skips_non_vm_nodes():
    topo = {
        "nodes": [
            {"id": "n1", "type": "networkNode", "data": {}},
            {
                "id": "v1",
                "type": "vmNode",
                "data": {"id": "v1", "label": "worker", "firmware": "bios"},
            },
        ]
    }
    vms = extract_vms(topo)
    assert len(vms) == 1
    assert vms[0]["name"] == "worker"


def test_collect_container_disk_mounts_dedupes_across_sidecar_lists():
    topo = {
        "nodes": [
            {
                "id": "disk-aaaaaaaa",
                "type": "storageNode",
                "data": {"size": 12},
            },
            {
                "id": "ctr-bbbbbbbb",
                "type": "containerNode",
                "data": {
                    "id": "ctr-bbbbbbbb",
                    "label": "app",
                    "mounts": [{"diskNodeId": "disk-aaaaaaaa"}],
                    "initContainers": [
                        {"mounts": [{"diskNodeId": "disk-aaaaaaaa"}]}
                    ],
                    "podContainers": [
                        {"mounts": [{"diskNodeId": "disk-aaaaaaaa"}]}
                    ],
                },
            },
        ]
    }
    mounts = collect_container_disk_mounts(topo)
    assert mounts == [("ctr-bbbbbbbb", "disk-aaaaaaaa", 12)]


def test_collect_container_disk_mounts_empty_when_no_containers():
    assert collect_container_disk_mounts({"nodes": []}) == []


def test_showroom_dns_explicit_name_wins_over_dns_flag():
    topo = {
        "nodes": [
            {
                "id": "net-a",
                "type": "networkNode",
                "data": {
                    "name": "lab-a",
                    "cidr": "10.1.0.0/24",
                    "dns": True,
                },
            },
            {
                "id": "net-b",
                "type": "networkNode",
                "data": {"name": "lab-b", "cidr": "10.2.0.0/24"},
            },
        ]
    }
    assert _resolve_showroom_dns_cidr(topo, "lab-b") == "10.2.0.0/24"
    assert _dns_nameserver_for_showroom(topo, {"dnsNetwork": "lab-b"}) == (
        "10.2.0.2"
    )


def test_showroom_dns_falls_back_to_dns_flag_then_first():
    topo = {
        "nodes": [
            {
                "id": "net-a",
                "type": "networkNode",
                "data": {"name": "lab-a", "cidr": "10.1.0.0/24"},
            },
            {
                "id": "net-b",
                "type": "networkNode",
                "data": {
                    "name": "lab-b",
                    "cidr": "10.2.0.0/24",
                    "dns": True,
                },
            },
        ]
    }
    assert _resolve_showroom_dns_cidr(topo, "") == "10.2.0.0/24"
    topo_no_dns = {
        "nodes": [
            {
                "id": "net-a",
                "type": "networkNode",
                "data": {"name": "lab-a", "cidr": "10.1.0.0/24"},
            }
        ]
    }
    assert _resolve_showroom_dns_cidr(topo_no_dns, "") == "10.1.0.0/24"


def test_should_skip_showroom_enrich():
    assert _should_skip_showroom_enrich({"name": "app"}) is True
    assert (
        _should_skip_showroom_enrich(
            {"isShowroom": True, "infraNetworking": False, "nics": [{"id": "n"}]}
        )
        is True
    )
    assert (
        _should_skip_showroom_enrich(
            {"isShowroom": True, "infraNetworking": True, "nics": [{"id": "n"}]}
        )
        is False
    )
    assert (
        _should_skip_showroom_enrich(
            {"isShowroom": True, "infraNetworking": False, "nics": []}
        )
        is False
    )


def test_cluster_egress_prefers_dns_then_any_gateway():
    topo = {
        "clusters": [
            {"id": "c1", "networkIds": ["net-plain", "net-dns"]},
        ],
        "nodes": [
            {
                "id": "gw",
                "type": "networkNode",
                "data": {"subtype": "gateway"},
            },
            {
                "id": "net-plain",
                "type": "networkNode",
                "data": {"id": "net-plain", "cidr": "10.0.0.0/24"},
            },
            {
                "id": "net-dns",
                "type": "networkNode",
                "data": {"id": "net-dns", "cidr": "10.1.0.0/24", "dns": True},
            },
            {
                "id": "net-migration",
                "type": "networkNode",
                "data": {
                    "id": "net-migration",
                    "networkType": "migration",
                    "cidr": "172.16.0.0/24",
                },
            },
        ],
        "edges": [
            {"source": "gw", "target": "net-plain"},
            {"source": "gw", "target": "net-dns"},
            {"source": "gw", "target": "net-migration"},
        ],
    }
    assert cluster_egress_network_id(topo["clusters"][0], topo) == "net-dns"
    assert cluster_egress_network_id(None, topo) == ""


def test_pick_egress_skips_ineligible():
    nodes_by_id = {
        "gw": {"id": "gw", "type": "networkNode", "data": {"subtype": "gateway"}},
        "mig": {
            "id": "mig",
            "type": "networkNode",
            "data": {"networkType": "migration"},
        },
        "ok": {"id": "ok", "type": "networkNode", "data": {"dns": True}},
    }
    assert _is_eligible_egress_network("gw", nodes_by_id) is False
    assert _is_eligible_egress_network("mig", nodes_by_id) is False
    assert _pick_egress_from(["gw", "mig", "ok"], nodes_by_id, dns_only=True) == "ok"


def test_build_disk_iso_and_blank_sources():
    iso = _build_disk_from_storage(
        {
            "format": "iso",
            "libraryItemId": "lib-1",
            "resolvedS3Path": "library/lib-1.iso",
            "centralSource": True,
            "sourceSizeGb": 2,
        },
        "stor-1",
    )
    assert iso["cdrom"]["s3Path"] == "library/lib-1.iso"
    assert iso["cdrom"]["sourceSizeGb"] == 2

    blank = _build_disk_from_storage({"format": "qcow2", "size": 10}, "stor-2")
    assert blank["disk"]["blank"] is True


def test_attach_disk_source_snapshot_without_path_is_blank():
    disk = {}
    _attach_disk_source(disk, {"source": "snapshot"}, "qcow2", False, "snapshot")
    assert disk["blank"] is True


def test_ceph_ip_hostnames_labels():
    pairs = list(
        _ceph_ip_hostnames(
            {"labIp": "10.0.0.4", "osdIps": ["10.0.0.254", "", "10.0.0.252"]}
        )
    )
    assert pairs == [
        ("10.0.0.4", "ceph-mon"),
        ("10.0.0.254", "ceph-osd-0"),
        ("10.0.0.252", "ceph-osd-2"),
    ]


def test_linked_cluster_ids_via_edges():
    nodes = [
        {"id": "ceph-1", "type": "cephClusterNode", "data": {}},
        {
            "id": "cl-1",
            "type": "clusterNode",
            "data": {"clusterId": "prod"},
        },
        {"id": "cl-2", "type": "clusterNode", "data": {"name": "dev"}},
        {"id": "vm-1", "type": "vmNode", "data": {}},
    ]
    edges = [
        {"source": "ceph-1", "target": "cl-1"},
        {"source": "cl-2", "target": "ceph-1"},
        {"source": "ceph-1", "target": "vm-1"},
    ]
    assert _linked_cluster_ids(nodes, edges, "ceph-1") == ["prod", "dev"]
    assert _edge_other_end({"source": "a", "target": "b"}, "a") == "b"
    assert _edge_other_end({"source": "a", "target": "b"}, "x") == ""
    assert _cluster_id_from_boundary({"data": {"clusterName": "x"}}) == "x"
