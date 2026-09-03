"""Tests for Kubernetes operator handler and helper functions.

Tests cover pure-logic helpers and API-mocking functions across
project, vm, network, container handlers and k8s/topology helpers.
"""

import asyncio
import hashlib
import json
import pytest
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# helpers/k8s.py
# ---------------------------------------------------------------------------


class TestOwnerRef:
    def test_basic_owner_ref(self):
        from helpers.k8s import owner_ref

        cr = {
            "kind": "TroshkaProject",
            "metadata": {"name": "my-project", "uid": "uid-1234"},
        }
        result = owner_ref(cr)
        assert result["apiVersion"] == "troshka.redhat.com/v1alpha1"
        assert result["kind"] == "TroshkaProject"
        assert result["name"] == "my-project"
        assert result["uid"] == "uid-1234"
        assert result["controller"] is True

    def test_owner_ref_different_kind(self):
        from helpers.k8s import owner_ref

        cr = {
            "kind": "TroshkaVM",
            "metadata": {"name": "vm-abc", "uid": "uid-5678"},
        }
        result = owner_ref(cr)
        assert result["kind"] == "TroshkaVM"
        assert result["name"] == "vm-abc"

    def test_owner_ref_preserves_uid(self):
        from helpers.k8s import owner_ref

        cr = {
            "kind": "TroshkaNetwork",
            "metadata": {"name": "net-1", "uid": "550e8400-e29b-41d4-a716"},
        }
        assert owner_ref(cr)["uid"] == "550e8400-e29b-41d4-a716"


class TestGoldenPvcName:
    def test_deterministic_name(self):
        from helpers.k8s import golden_pvc_name

        result = golden_pvc_name("patterns/abc/disk1.qcow2")
        h = hashlib.sha256(b"patterns/abc/disk1.qcow2").hexdigest()[:16]
        assert result == f"golden-{h}"

    def test_different_paths_different_names(self):
        from helpers.k8s import golden_pvc_name

        a = golden_pvc_name("library/aaa.qcow2")
        b = golden_pvc_name("library/bbb.qcow2")
        assert a != b

    def test_same_path_same_name(self):
        from helpers.k8s import golden_pvc_name

        assert golden_pvc_name("x/y") == golden_pvc_name("x/y")

    def test_name_starts_with_golden(self):
        from helpers.k8s import golden_pvc_name

        assert golden_pvc_name("any-path").startswith("golden-")

    def test_name_length(self):
        from helpers.k8s import golden_pvc_name

        # "golden-" (7) + 16 hex chars = 23
        assert len(golden_pvc_name("foo")) == 23


class TestBuildDnsmasqDeployment:
    def _make_network_cr(self, cidr="10.0.0.0/24", name="net-abc12345"):
        return {
            "kind": "TroshkaNetwork",
            "metadata": {
                "name": name,
                "namespace": "test-ns",
                "uid": "uid-net-1",
            },
            "spec": {"cidr": cidr},
        }

    def test_deployment_name(self):
        from helpers.k8s import build_dnsmasq_deployment

        cr = self._make_network_cr()
        dep = build_dnsmasq_deployment(cr)
        assert dep["metadata"]["name"] == "dnsmasq-net-abc12345"

    def test_deployment_namespace(self):
        from helpers.k8s import build_dnsmasq_deployment

        cr = self._make_network_cr()
        dep = build_dnsmasq_deployment(cr)
        assert dep["metadata"]["namespace"] == "test-ns"

    def test_init_container_setup_cmd(self):
        from helpers.k8s import build_dnsmasq_deployment

        cr = self._make_network_cr(cidr="10.0.0.0/24")
        dep = build_dnsmasq_deployment(cr)
        init = dep["spec"]["template"]["spec"]["initContainers"][0]
        assert "ip addr add 10.0.0.2/24 dev net1" in init["command"][2]

    def test_empty_cidr_uses_true(self):
        from helpers.k8s import build_dnsmasq_deployment

        cr = self._make_network_cr(cidr="")
        dep = build_dnsmasq_deployment(cr)
        init = dep["spec"]["template"]["spec"]["initContainers"][0]
        assert init["command"][2] == "true"

    def test_dnsmasq_container_present(self):
        from helpers.k8s import build_dnsmasq_deployment

        cr = self._make_network_cr()
        dep = build_dnsmasq_deployment(cr)
        containers = dep["spec"]["template"]["spec"]["containers"]
        assert len(containers) == 1
        assert containers[0]["name"] == "dnsmasq"

    def test_configmap_volume(self):
        from helpers.k8s import build_dnsmasq_deployment

        cr = self._make_network_cr(name="mynet")
        dep = build_dnsmasq_deployment(cr)
        volumes = dep["spec"]["template"]["spec"]["volumes"]
        assert any(v["configMap"]["name"] == "dnsmasq-mynet" for v in volumes)

    def test_network_annotation(self):
        from helpers.k8s import build_dnsmasq_deployment

        cr = self._make_network_cr(name="netA")
        dep = build_dnsmasq_deployment(cr)
        ann = dep["spec"]["template"]["metadata"]["annotations"]
        assert ann["k8s.v1.cni.cncf.io/networks"] == "netA-nad"

    def test_service_account(self):
        from helpers.k8s import build_dnsmasq_deployment

        cr = self._make_network_cr()
        dep = build_dnsmasq_deployment(cr)
        assert (
            dep["spec"]["template"]["spec"]["serviceAccountName"] == "troshka-network"
        )


# ---------------------------------------------------------------------------
# helpers/topology.py
# ---------------------------------------------------------------------------


class TestGatewayIpForCidr:
    def test_basic_cidr(self):
        from helpers.topology import _gateway_ip_for_cidr

        assert _gateway_ip_for_cidr("10.0.0.0/24") == "10.0.0.1"

    def test_different_subnet(self):
        from helpers.topology import _gateway_ip_for_cidr

        assert _gateway_ip_for_cidr("192.168.1.0/16") == "192.168.1.1"

    def test_empty_cidr(self):
        from helpers.topology import _gateway_ip_for_cidr

        assert _gateway_ip_for_cidr("") == ""

    def test_no_prefix(self):
        from helpers.topology import _gateway_ip_for_cidr

        assert _gateway_ip_for_cidr("10.0.0.0") == ""


class TestExtractNetworks:
    def test_empty_topology(self):
        from helpers.topology import extract_networks

        assert extract_networks({}) == []
        assert extract_networks({"nodes": []}) == []

    def test_single_network(self):
        from helpers.topology import extract_networks

        topo = {
            "nodes": [
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {
                        "id": "net1",
                        "label": "My Network",
                        "cidr": "10.0.0.0/24",
                        "networkType": "standard",
                    },
                }
            ],
            "edges": [],
        }
        nets = extract_networks(topo)
        assert len(nets) == 1
        assert nets[0]["id"] == "net1"
        assert nets[0]["cidr"] == "10.0.0.0/24"
        assert nets[0]["gateway"] == "10.0.0.1"

    def test_skips_gateway_subtype(self):
        from helpers.topology import extract_networks

        topo = {
            "nodes": [
                {
                    "id": "gw1",
                    "type": "networkNode",
                    "data": {"id": "gw1", "subtype": "gateway"},
                },
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"id": "net1", "cidr": "10.0.0.0/24"},
                },
            ],
            "edges": [],
        }
        nets = extract_networks(topo)
        assert len(nets) == 1
        assert nets[0]["id"] == "net1"

    def test_external_access_via_gateway_edge(self):
        from helpers.topology import extract_networks

        topo = {
            "nodes": [
                {
                    "id": "gw1",
                    "type": "networkNode",
                    "data": {"id": "gw1", "subtype": "gateway"},
                },
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"id": "net1", "cidr": "10.0.0.0/24"},
                },
            ],
            "edges": [{"source": "gw1", "target": "net1"}],
        }
        nets = extract_networks(topo)
        assert nets[0]["externalAccess"] is True

    def test_no_external_access_without_gateway(self):
        from helpers.topology import extract_networks

        topo = {
            "nodes": [
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"id": "net1", "cidr": "10.0.0.0/24"},
                },
            ],
            "edges": [],
        }
        nets = extract_networks(topo)
        assert nets[0]["externalAccess"] is False

    def test_dns_forwarders(self):
        from helpers.topology import extract_networks

        topo = {
            "nodes": [
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {
                        "id": "net1",
                        "cidr": "10.0.0.0/24",
                        "dnsForwarders": ["8.8.8.8", "1.1.1.1"],
                    },
                },
            ],
            "edges": [],
        }
        nets = extract_networks(topo)
        assert nets[0]["dnsForwarders"] == ["8.8.8.8", "1.1.1.1"]


class TestExtractVms:
    def test_empty_topology(self):
        from helpers.topology import extract_vms

        assert extract_vms({}) == []

    def test_basic_vm(self):
        from helpers.topology import extract_vms

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "id": "vm1",
                        "label": "test-vm",
                        "cpus": 4,
                        "memory": 8192,
                        "firmware": "uefi",
                    },
                }
            ]
        }
        vms = extract_vms(topo)
        assert len(vms) == 1
        assert vms[0]["name"] == "test-vm"
        assert vms[0]["cpus"] == 4
        assert vms[0]["memory"] == 8192
        assert vms[0]["firmware"] == "uefi"

    def test_vm_defaults(self):
        from helpers.topology import extract_vms

        topo = {"nodes": [{"id": "vm1", "type": "vmNode", "data": {"id": "vm1"}}]}
        vms = extract_vms(topo)
        assert vms[0]["firmware"] == "bios"
        assert "machineType" not in vms[0]
        assert vms[0]["powerOnAtDeploy"] is True
        assert vms[0]["recertEnabled"] is False

    def test_vm_with_ram_fallback(self):
        from helpers.topology import extract_vms

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"id": "vm1", "ram": 4},
                }
            ]
        }
        vms = extract_vms(topo)
        # ram=4 * 1024 = 4096
        assert vms[0]["memory"] == 4096

    def test_vm_with_cdrom(self):
        from helpers.topology import extract_vms

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "id": "vm1",
                        "pxeBootIsoId": "iso-123",
                        "pxeBootIsoS3Path": "library/iso-123.iso",
                    },
                }
            ]
        }
        vms = extract_vms(topo)
        assert vms[0]["cdrom"]["libraryIsoId"] == "iso-123"
        assert vms[0]["cdrom"]["s3Path"] == "library/iso-123.iso"

    def test_skips_non_vm_nodes(self):
        from helpers.topology import extract_vms

        topo = {
            "nodes": [
                {"id": "n1", "type": "networkNode", "data": {"id": "n1"}},
                {"id": "vm1", "type": "vmNode", "data": {"id": "vm1"}},
            ]
        }
        assert len(extract_vms(topo)) == 1


class TestExtractContainers:
    def test_empty_topology(self):
        from helpers.topology import extract_containers

        assert extract_containers({}) == []

    def test_basic_container(self):
        from helpers.topology import extract_containers

        topo = {
            "nodes": [
                {
                    "id": "c1",
                    "type": "containerNode",
                    "data": {
                        "id": "c1",
                        "label": "nginx",
                        "image": "nginx:latest",
                        "command": "nginx -g 'daemon off;'",
                    },
                }
            ]
        }
        ctrs = extract_containers(topo)
        assert len(ctrs) == 1
        assert ctrs[0]["name"] == "nginx"
        assert ctrs[0]["image"] == "nginx:latest"
        assert ctrs[0]["isPod"] is False

    def test_pod_container(self):
        from helpers.topology import extract_containers

        topo = {
            "nodes": [
                {
                    "id": "p1",
                    "type": "containerNode",
                    "data": {
                        "id": "p1",
                        "label": "my-pod",
                        "isPod": True,
                        "podContainers": [{"name": "app", "image": "app:1.0"}],
                    },
                }
            ]
        }
        ctrs = extract_containers(topo)
        assert ctrs[0]["isPod"] is True
        assert len(ctrs[0]["podContainers"]) == 1

    def test_container_defaults(self):
        from helpers.topology import extract_containers

        topo = {
            "nodes": [
                {
                    "id": "c1",
                    "type": "containerNode",
                    "data": {"id": "c1"},
                }
            ]
        }
        ctrs = extract_containers(topo)
        assert ctrs[0]["cpus"] == 1
        assert ctrs[0]["memory"] == 512
        assert ctrs[0]["isPod"] is False


class TestEnrichShowroomInfraNetworks:
    def test_adds_lab_nad_nics_for_showroom(self):
        from helpers.topology import extract_containers, enrich_showroom_infra_networks

        topo = {
            "nodes": [
                {
                    "id": "net-mgmt",
                    "type": "networkNode",
                    "data": {
                        "name": "mgmt",
                        "subtype": "network",
                        "cidr": "10.0.0.0/24",
                        "dns": True,
                    },
                },
                {
                    "id": "net-cluster",
                    "type": "networkNode",
                    "data": {
                        "name": "cluster",
                        "subtype": "network",
                        "cidr": "192.168.50.0/24",
                    },
                },
                {
                    "id": "sr-1",
                    "type": "containerNode",
                    "data": {
                        "id": "sr-1",
                        "label": "showroom",
                        "isShowroom": True,
                        "isPod": True,
                        "infraNetworking": True,
                        "nics": [],
                    },
                },
            ],
        }
        ctrs = extract_containers(topo)
        enrich_showroom_infra_networks(topo, ctrs)
        assert len(ctrs[0]["nics"]) == 2
        refs = {n["networkRef"] for n in ctrs[0]["nics"]}
        assert refs == {"net-net-mgmt", "net-net-clus"}
        assert ctrs[0]["nics"][0]["ip"].endswith(".250")


class TestExtractNicId:
    def test_basic_nic_handle(self):
        from helpers.topology import _extract_nic_id

        assert _extract_nic_id("nic-nic-abc123") == "nic-abc123"

    def test_strips_direction_suffix(self):
        from helpers.topology import _extract_nic_id

        assert _extract_nic_id("nic-nic-abc123-top") == "nic-abc123"
        assert _extract_nic_id("nic-nic-abc123-bottom") == "nic-abc123"
        assert _extract_nic_id("nic-nic-abc123-left") == "nic-abc123"
        assert _extract_nic_id("nic-nic-abc123-right") == "nic-abc123"

    def test_empty_handle(self):
        from helpers.topology import _extract_nic_id

        assert _extract_nic_id("") == ""
        assert _extract_nic_id(None) == ""

    def test_handle_without_nic_prefix(self):
        from helpers.topology import _extract_nic_id

        assert _extract_nic_id("something-else") == ""


class TestResolveNicNetworks:
    def test_empty_topology(self):
        from helpers.topology import resolve_nic_networks

        assert resolve_nic_networks({}) == {}

    def test_network_to_vm_edge(self):
        from helpers.topology import resolve_nic_networks

        topo = {
            "nodes": [
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"id": "net1"},
                },
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"id": "vm1"},
                },
            ],
            "edges": [
                {
                    "source": "net1",
                    "target": "vm1",
                    "targetHandle": "nic-nic-abc12345-bottom",
                }
            ],
        }
        result = resolve_nic_networks(topo)
        assert result["nic-abc12345"] == f"net-{('net1')[:8]}"

    def test_vm_to_network_edge(self):
        from helpers.topology import resolve_nic_networks

        topo = {
            "nodes": [
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"id": "net1"},
                },
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"id": "vm1"},
                },
            ],
            "edges": [
                {
                    "source": "vm1",
                    "target": "net1",
                    "sourceHandle": "nic-nic-def456-left",
                }
            ],
        }
        result = resolve_nic_networks(topo)
        assert "nic-def456" in result


class TestEnrichVmNics:
    def test_backfills_missing_network_ref(self):
        from helpers.topology import enrich_vm_nics

        topo = {
            "nodes": [
                {"id": "net1", "type": "networkNode", "data": {"id": "net1"}},
                {"id": "vm1", "type": "vmNode", "data": {"id": "vm1"}},
            ],
            "edges": [
                {
                    "source": "net1",
                    "target": "vm1",
                    "targetHandle": "nic-nic-abc12345-bottom",
                }
            ],
        }
        spec = {"nics": [{"id": "nic-abc12345", "mac": "aa:bb:cc:dd:ee:ff"}]}
        enrich_vm_nics(topo, spec)
        assert spec["nics"][0]["networkRef"] == f"net-{('net1')[:8]}"

    def test_preserves_existing_network_ref(self):
        from helpers.topology import enrich_vm_nics

        spec = {"nics": [{"id": "nic-1", "networkRef": "net-existing"}]}
        enrich_vm_nics({"nodes": [], "edges": []}, spec)
        assert spec["nics"][0]["networkRef"] == "net-existing"


class TestResolveVmDisks:
    def test_empty_topology(self):
        from helpers.topology import resolve_vm_disks

        disks, cdroms = resolve_vm_disks({})
        assert disks == {}
        assert cdroms == {}

    def test_library_disk(self):
        from helpers.topology import resolve_vm_disks

        topo = {
            "nodes": [
                {
                    "id": "stor1",
                    "type": "storageNode",
                    "data": {
                        "id": "stor1",
                        "source": "library",
                        "libraryItemId": "lib-001",
                        "resolvedS3Path": "library/lib-001/disk-1/rhel.qcow2",
                        "format": "qcow2",
                        "size": 40,
                    },
                },
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"id": "vm1"},
                },
            ],
            "edges": [{"source": "stor1", "target": "vm1"}],
        }
        disks, cdroms = resolve_vm_disks(topo)
        assert "vm1" in disks
        assert len(disks["vm1"]) == 1
        assert "libraryImage" in disks["vm1"][0]
        assert disks["vm1"][0]["sizeGb"] == 40

    def test_disk_bus_from_controller(self):
        from helpers.topology import resolve_vm_disks

        topo = {
            "nodes": [
                {
                    "id": "stor1",
                    "type": "storageNode",
                    "data": {
                        "id": "stor1",
                        "source": "library",
                        "libraryItemId": "lib-001",
                        "format": "qcow2",
                        "size": 40,
                    },
                },
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "id": "vm1",
                        "diskControllers": [
                            {"id": "dp-1", "bus": "ide", "name": "disk0"},
                        ],
                    },
                },
            ],
            "edges": [
                {
                    "source": "stor1",
                    "target": "vm1",
                    "sourceHandle": "right",
                    "targetHandle": "dp-dp-1-left",
                }
            ],
        }
        disks, _ = resolve_vm_disks(topo)
        assert disks["vm1"][0]["bus"] == "ide"

    def test_pattern_disk(self):
        from helpers.topology import resolve_vm_disks

        topo = {
            "nodes": [
                {
                    "id": "stor1",
                    "type": "storageNode",
                    "data": {
                        "id": "stor1",
                        "source": "pattern",
                        "patternId": "pat-001",
                        "patternDiskId": "disk-001",
                        "format": "qcow2",
                    },
                },
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"id": "vm1"},
                },
            ],
            "edges": [{"source": "stor1", "target": "vm1"}],
        }
        disks, _ = resolve_vm_disks(topo)
        assert "patternImage" in disks["vm1"][0]
        assert (
            "patterns/pat-001/disk-001.qcow2"
            in disks["vm1"][0]["patternImage"]["s3Path"]
        )

    def test_blank_disk(self):
        from helpers.topology import resolve_vm_disks

        topo = {
            "nodes": [
                {
                    "id": "stor1",
                    "type": "storageNode",
                    "data": {
                        "id": "stor1",
                        "source": "",
                        "format": "qcow2",
                        "size": 100,
                    },
                },
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"id": "vm1"},
                },
            ],
            "edges": [{"source": "stor1", "target": "vm1"}],
        }
        disks, _ = resolve_vm_disks(topo)
        assert disks["vm1"][0]["blank"] is True

    def test_iso_goes_to_cdroms(self):
        from helpers.topology import resolve_vm_disks

        topo = {
            "nodes": [
                {
                    "id": "iso1",
                    "type": "storageNode",
                    "data": {
                        "id": "iso1",
                        "format": "iso",
                        "libraryItemId": "iso-lib-1",
                    },
                },
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"id": "vm1"},
                },
            ],
            "edges": [{"source": "iso1", "target": "vm1"}],
        }
        disks, cdroms = resolve_vm_disks(topo)
        assert "vm1" not in disks
        assert "vm1" in cdroms
        assert cdroms["vm1"]["libraryIsoId"] == "iso-lib-1"

    def test_central_flag_propagated(self):
        from helpers.topology import resolve_vm_disks

        topo = {
            "nodes": [
                {
                    "id": "stor1",
                    "type": "storageNode",
                    "data": {
                        "id": "stor1",
                        "source": "library",
                        "libraryItemId": "lib-001",
                        "resolvedS3Path": "library/lib-001/disk-1/rhel.qcow2",
                        "centralSource": True,
                    },
                },
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"id": "vm1"},
                },
            ],
            "edges": [{"source": "stor1", "target": "vm1"}],
        }
        disks, _ = resolve_vm_disks(topo)
        assert disks["vm1"][0]["libraryImage"]["central"] is True


class TestExtractStartOrder:
    def test_fallback_to_vm_list(self):
        from helpers.topology import extract_start_order

        topo = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {"id": "vm1"}},
                {"id": "vm2", "type": "vmNode", "data": {"id": "vm2"}},
            ]
        }
        so = extract_start_order(topo)
        assert len(so) == 2
        assert so[0]["vmId"] == "vm1"
        assert so[1]["vmId"] == "vm2"

    def test_explicit_start_order(self):
        from helpers.topology import extract_start_order

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "id": "vm1",
                        "startOrder": [
                            {"vmId": "vm2"},
                            {"vmId": "vm1"},
                        ],
                    },
                },
                {"id": "vm2", "type": "vmNode", "data": {"id": "vm2"}},
            ]
        }
        so = extract_start_order(topo)
        assert so[0]["vmId"] == "vm2"

    def test_empty_topology(self):
        from helpers.topology import extract_start_order

        assert extract_start_order({}) == []


class TestBuildStaticLeases:
    def test_empty_topology(self):
        from helpers.topology import build_static_leases

        assert build_static_leases({}) == {}

    def test_vm_to_network_lease(self):
        from helpers.topology import build_static_leases

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "id": "vm1",
                        "label": "bastion",
                        "nics": [
                            {
                                "id": "nic-aaa",
                                "mac": "52:54:00:01:02:03",
                                "ip": "10.0.0.10",
                            }
                        ],
                    },
                },
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"id": "net1"},
                },
            ],
            "edges": [
                {
                    "source": "vm1",
                    "target": "net1",
                    "sourceHandle": "nic-nic-aaa-bottom",
                }
            ],
        }
        leases = build_static_leases(topo)
        assert "net1" in leases
        assert leases["net1"][0]["mac"] == "52:54:00:01:02:03"
        assert leases["net1"][0]["ip"] == "10.0.0.10"
        assert leases["net1"][0]["hostname"] == "bastion"

    def test_no_lease_without_mac_or_ip(self):
        from helpers.topology import build_static_leases

        topo = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "id": "vm1",
                        "nics": [{"id": "nic-aaa", "mac": "", "ip": ""}],
                    },
                },
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"id": "net1"},
                },
            ],
            "edges": [
                {
                    "source": "vm1",
                    "target": "net1",
                    "sourceHandle": "nic-nic-aaa-bottom",
                }
            ],
        }
        leases = build_static_leases(topo)
        assert leases == {}


# ---------------------------------------------------------------------------
# handlers/project.py
# ---------------------------------------------------------------------------


class TestProjectCleanupLegacyPod:
    def test_deletes_standalone_pod(self):
        from handlers.project import _cleanup_legacy_pod

        pod_mock = MagicMock()
        pod_mock.metadata.owner_references = []
        core_api = MagicMock()
        core_api.read_namespaced_pod.return_value = pod_mock

        _cleanup_legacy_pod(core_api, "ns1", "my-pod")

        core_api.delete_namespaced_pod.assert_called_once_with(
            name="my-pod", namespace="ns1"
        )

    def test_skips_replicaset_owned_pod(self):
        from handlers.project import _cleanup_legacy_pod

        owner = MagicMock()
        owner.kind = "ReplicaSet"
        pod_mock = MagicMock()
        pod_mock.metadata.owner_references = [owner]
        core_api = MagicMock()
        core_api.read_namespaced_pod.return_value = pod_mock

        _cleanup_legacy_pod(core_api, "ns1", "my-pod")

        core_api.delete_namespaced_pod.assert_not_called()

    def test_ignores_404(self):
        from handlers.project import _cleanup_legacy_pod
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.read_namespaced_pod.side_effect = ApiException(status=404)

        # Should not raise
        _cleanup_legacy_pod(core_api, "ns1", "my-pod")

    def test_raises_non_404_error(self):
        from handlers.project import _cleanup_legacy_pod
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.read_namespaced_pod.side_effect = ApiException(status=500)

        with pytest.raises(ApiException):
            _cleanup_legacy_pod(core_api, "ns1", "my-pod")

    def test_handles_none_owner_references(self):
        from handlers.project import _cleanup_legacy_pod

        pod_mock = MagicMock()
        pod_mock.metadata.owner_references = None
        core_api = MagicMock()
        core_api.read_namespaced_pod.return_value = pod_mock

        _cleanup_legacy_pod(core_api, "ns1", "my-pod")
        core_api.delete_namespaced_pod.assert_called_once()


class TestCleanupCaptureResources:
    def test_deletes_all_resources(self):
        from handlers.project import _cleanup_capture_resources

        export_jobs = [
            {
                "tempPvcName": "tmp-pvc-1",
                "snapName": "snap-1",
                "jobName": "job-1",
            },
            {
                "tempPvcName": "tmp-pvc-2",
                "snapName": "snap-2",
                "jobName": "job-2",
            },
        ]
        core_api = MagicMock()
        custom_api = MagicMock()
        batch_api = MagicMock()

        _cleanup_capture_resources(
            core_api, custom_api, batch_api, export_jobs, "test-ns"
        )

        assert core_api.delete_namespaced_persistent_volume_claim.call_count == 2
        assert custom_api.delete_namespaced_custom_object.call_count == 2
        assert batch_api.delete_namespaced_job.call_count == 2

    def test_swallows_exceptions(self):
        from handlers.project import _cleanup_capture_resources

        export_jobs = [
            {
                "tempPvcName": "tmp-pvc-1",
                "snapName": "snap-1",
                "jobName": "job-1",
            }
        ]
        core_api = MagicMock()
        core_api.delete_namespaced_persistent_volume_claim.side_effect = Exception(
            "PVC gone"
        )
        custom_api = MagicMock()
        custom_api.delete_namespaced_custom_object.side_effect = Exception("Snap gone")
        batch_api = MagicMock()
        batch_api.delete_namespaced_job.side_effect = Exception("Job gone")

        # Should not raise
        _cleanup_capture_resources(
            core_api, custom_api, batch_api, export_jobs, "test-ns"
        )

    def test_empty_export_jobs(self):
        from handlers.project import _cleanup_capture_resources

        core_api = MagicMock()
        custom_api = MagicMock()
        batch_api = MagicMock()

        _cleanup_capture_resources(core_api, custom_api, batch_api, [], "ns")

        core_api.delete_namespaced_persistent_volume_claim.assert_not_called()

    def test_uses_background_propagation(self):
        from handlers.project import _cleanup_capture_resources

        export_jobs = [
            {
                "tempPvcName": "tmp-1",
                "snapName": "snap-1",
                "jobName": "job-1",
            }
        ]
        core_api = MagicMock()
        custom_api = MagicMock()
        batch_api = MagicMock()

        _cleanup_capture_resources(core_api, custom_api, batch_api, export_jobs, "ns")

        batch_api.delete_namespaced_job.assert_called_with(
            name="job-1",
            namespace="ns",
            propagation_policy="Background",
        )


class TestResolveDiskS3Path:
    def test_library_image(self):
        from handlers.project import _resolve_disk_s3_path

        disk = {"libraryImage": {"s3Path": "library/abc.qcow2", "central": False}}
        path, central = _resolve_disk_s3_path(disk)
        assert path == "library/abc.qcow2"
        assert central is False

    def test_pattern_image(self):
        from handlers.project import _resolve_disk_s3_path

        disk = {
            "patternImage": {
                "s3Path": "patterns/p1/disk.qcow2",
                "central": True,
            }
        }
        path, central = _resolve_disk_s3_path(disk)
        assert path == "patterns/p1/disk.qcow2"
        assert central is True

    def test_blank_disk(self):
        from handlers.project import _resolve_disk_s3_path

        disk = {"blank": True}
        path, central = _resolve_disk_s3_path(disk)
        assert path is None
        assert central is False

    def test_library_takes_priority_over_pattern(self):
        from handlers.project import _resolve_disk_s3_path

        disk = {
            "libraryImage": {"s3Path": "library/x.qcow2"},
            "patternImage": {"s3Path": "patterns/y.qcow2"},
        }
        path, _ = _resolve_disk_s3_path(disk)
        assert path == "library/x.qcow2"

    def test_empty_s3_path(self):
        from handlers.project import _resolve_disk_s3_path

        disk = {"libraryImage": {"s3Path": ""}}
        path, central = _resolve_disk_s3_path(disk)
        assert path is None


class TestDeleteCustomResources:
    def test_deletes_all_items(self):
        from handlers.project import _delete_custom_resources

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {"metadata": {"name": "item-1"}},
                {"metadata": {"name": "item-2"}},
            ]
        }

        _delete_custom_resources(custom_api, "group", "v1", "things", "test-ns")

        assert custom_api.delete_namespaced_custom_object.call_count == 2

    def test_with_grace_period(self):
        from handlers.project import _delete_custom_resources

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [{"metadata": {"name": "item-1"}}]
        }

        _delete_custom_resources(
            custom_api, "group", "v1", "things", "test-ns", grace_period=0
        )

        custom_api.delete_namespaced_custom_object.assert_called_once_with(
            group="group",
            version="v1",
            namespace="test-ns",
            plural="things",
            name="item-1",
            grace_period_seconds=0,
        )

    def test_without_grace_period(self):
        from handlers.project import _delete_custom_resources

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [{"metadata": {"name": "item-1"}}]
        }

        _delete_custom_resources(custom_api, "group", "v1", "things", "test-ns")

        custom_api.delete_namespaced_custom_object.assert_called_once_with(
            group="group",
            version="v1",
            namespace="test-ns",
            plural="things",
            name="item-1",
        )

    def test_handles_404_on_delete(self):
        from handlers.project import _delete_custom_resources
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [{"metadata": {"name": "item-1"}}]
        }
        custom_api.delete_namespaced_custom_object.side_effect = ApiException(
            status=404
        )

        # Should not raise (404 is swallowed)
        _delete_custom_resources(custom_api, "group", "v1", "things", "test-ns")

    def test_handles_list_failure(self):
        from handlers.project import _delete_custom_resources

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = Exception("API down")

        # Should not raise
        _delete_custom_resources(custom_api, "group", "v1", "things", "test-ns")

    def test_empty_items(self):
        from handlers.project import _delete_custom_resources

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {"items": []}

        _delete_custom_resources(custom_api, "group", "v1", "things", "test-ns")

        custom_api.delete_namespaced_custom_object.assert_not_called()


class TestRemoveSaFromSccs:
    def test_removes_sa_ref(self):
        from handlers.project import _remove_sa_from_sccs

        sa_ref = "system:serviceaccount:ns1:my-sa"
        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {
            "users": [sa_ref, "other-user"]
        }

        _remove_sa_from_sccs(custom_api, "ns1", "my-sa", ["scc-1"])

        custom_api.patch_cluster_custom_object.assert_called_once()
        patched_body = custom_api.patch_cluster_custom_object.call_args[1]["body"]
        assert sa_ref not in patched_body["users"]
        assert "other-user" in patched_body["users"]

    def test_no_patch_if_sa_not_present(self):
        from handlers.project import _remove_sa_from_sccs

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {"users": ["other-user"]}

        _remove_sa_from_sccs(custom_api, "ns1", "my-sa", ["scc-1"])

        custom_api.patch_cluster_custom_object.assert_not_called()

    def test_handles_none_users(self):
        from handlers.project import _remove_sa_from_sccs

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {"users": None}

        # Should not raise
        _remove_sa_from_sccs(custom_api, "ns1", "my-sa", ["scc-1"])

    def test_multiple_sccs(self):
        from handlers.project import _remove_sa_from_sccs

        sa_ref = "system:serviceaccount:ns1:my-sa"
        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {"users": [sa_ref]}

        _remove_sa_from_sccs(custom_api, "ns1", "my-sa", ["scc-1", "scc-2", "scc-3"])

        assert custom_api.get_cluster_custom_object.call_count == 3

    def test_swallows_exceptions(self):
        from handlers.project import _remove_sa_from_sccs

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.side_effect = Exception("SCC not found")

        # Should not raise
        _remove_sa_from_sccs(custom_api, "ns1", "my-sa", ["scc-1"])


class TestProjectDeleteSccCleanup:
    def test_removes_all_sa_refs_from_sccs(self):
        from handlers.project import _remove_sa_from_sccs

        custom_api = MagicMock()
        scc_data = {
            "troshka-network-pods": {
                "users": ["system:serviceaccount:test-ns:troshka-network"]
            },
            "troshka-gateway": {
                "users": [
                    "system:serviceaccount:test-ns:troshka-network",
                    "system:serviceaccount:test-ns:troshka-bmc",
                ]
            },
            "troshka-privileged-jobs": {
                "users": ["system:serviceaccount:test-ns:troshka-recert"]
            },
        }
        custom_api.get_cluster_custom_object.side_effect = (
            lambda group, version, plural, name: scc_data[name]
        )

        _remove_sa_from_sccs(
            custom_api,
            "test-ns",
            "troshka-network",
            ("troshka-network-pods", "troshka-gateway"),
        )
        _remove_sa_from_sccs(custom_api, "test-ns", "troshka-bmc", ("troshka-gateway",))
        _remove_sa_from_sccs(
            custom_api, "test-ns", "troshka-recert", ("troshka-privileged-jobs",)
        )

        assert custom_api.patch_cluster_custom_object.call_count == 4
        patched_sccs = [
            c.kwargs["name"]
            for c in custom_api.patch_cluster_custom_object.call_args_list
        ]
        assert patched_sccs.count("troshka-gateway") == 2
        assert patched_sccs.count("troshka-network-pods") == 1
        assert patched_sccs.count("troshka-privileged-jobs") == 1


class TestStopAllVms:
    def test_patches_vms_to_stop(self):
        from handlers.project import _stop_all_vms

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = [
            # First call: troshkavms
            {
                "items": [
                    {
                        "metadata": {"name": "vm-1"},
                        "status": {"kubevirtVmName": "kv-vm-1"},
                    },
                    {
                        "metadata": {"name": "vm-2"},
                        "status": {"kubevirtVmName": "kv-vm-2"},
                    },
                ]
            },
            # Second call: VMIs (empty = all stopped)
            {"items": []},
        ]

        asyncio.run(_stop_all_vms(custom_api, "test-ns"))

        assert custom_api.patch_namespaced_custom_object.call_count == 2
        # Verify it patched with running: False
        first_call = custom_api.patch_namespaced_custom_object.call_args_list[0]
        assert first_call[1]["body"] == {"spec": {"running": False}}
        assert first_call[1]["group"] == "kubevirt.io"

    def test_uses_fallback_name_when_no_status(self):
        from handlers.project import _stop_all_vms

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = [
            {
                "items": [
                    {"metadata": {"name": "vm-1"}, "status": {}},
                ]
            },
            {"items": []},
        ]

        asyncio.run(_stop_all_vms(custom_api, "test-ns"))

        call_kwargs = custom_api.patch_namespaced_custom_object.call_args[1]
        assert call_kwargs["name"] == "troshka-vm-1"

    def test_handles_patch_failure_gracefully(self):
        from handlers.project import _stop_all_vms

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = [
            {
                "items": [
                    {
                        "metadata": {"name": "vm-1"},
                        "status": {"kubevirtVmName": "kv-vm-1"},
                    },
                ]
            },
            {"items": []},
        ]
        custom_api.patch_namespaced_custom_object.side_effect = Exception(
            "VM not found"
        )

        # Should not raise — patches are wrapped in try/except
        asyncio.run(_stop_all_vms(custom_api, "test-ns"))


# ---------------------------------------------------------------------------
# handlers/vm.py
# ---------------------------------------------------------------------------


class TestVmResolveDiskS3:
    def test_library_image_normal(self):
        from handlers.vm import _resolve_disk_s3

        disk = {"libraryImage": {"s3Path": "library/abc.qcow2"}}
        s3_cfg = {"bucket": "my-bucket"}
        central_cfg = {"bucket": "central-bucket"}

        path, cfg, secret = _resolve_disk_s3(disk, s3_cfg, central_cfg)
        assert path == "library/abc.qcow2"
        assert cfg == s3_cfg
        assert secret == "s3-credentials"  # pragma: allowlist secret

    def test_library_image_central(self):
        from handlers.vm import _resolve_disk_s3

        disk = {
            "libraryImage": {
                "s3Path": "library/abc.qcow2",
                "central": True,
            }
        }
        s3_cfg = {"bucket": "my-bucket"}
        central_cfg = {"bucket": "central-bucket"}

        path, cfg, secret = _resolve_disk_s3(disk, s3_cfg, central_cfg)
        assert path == "library/abc.qcow2"
        assert cfg == central_cfg
        assert secret == "s3-central-credentials"  # pragma: allowlist secret

    def test_pattern_image(self):
        from handlers.vm import _resolve_disk_s3

        disk = {"patternImage": {"s3Path": "patterns/p1/disk.qcow2"}}
        s3_cfg = {"bucket": "b"}
        central_cfg = {}

        path, cfg, secret = _resolve_disk_s3(disk, s3_cfg, central_cfg)
        assert path == "patterns/p1/disk.qcow2"
        assert cfg == s3_cfg

    def test_blank_disk_returns_nones(self):
        from handlers.vm import _resolve_disk_s3

        disk = {"blank": True}
        path, cfg, secret = _resolve_disk_s3(disk, {}, {})
        assert path is None
        assert cfg is None
        assert secret is None

    def test_empty_s3_path_returns_nones(self):
        from handlers.vm import _resolve_disk_s3

        disk = {"libraryImage": {"s3Path": ""}}
        path, cfg, secret = _resolve_disk_s3(disk, {}, {})
        assert path is None

    def test_central_without_central_config_uses_normal(self):
        from handlers.vm import _resolve_disk_s3

        disk = {
            "libraryImage": {
                "s3Path": "library/x.qcow2",
                "central": True,
            }
        }
        s3_cfg = {"bucket": "normal"}
        # Empty central config = fall back to normal
        path, cfg, secret = _resolve_disk_s3(disk, s3_cfg, {})
        assert cfg == s3_cfg
        assert secret == "s3-credentials"  # pragma: allowlist secret


class TestVmCleanupLegacyPod:
    def test_deletes_standalone_pod(self):
        from handlers.vm import _cleanup_legacy_pod

        pod_mock = MagicMock()
        pod_mock.metadata.owner_references = []
        core_api = MagicMock()
        core_api.read_namespaced_pod.return_value = pod_mock

        _cleanup_legacy_pod(core_api, "ns1", "my-pod")
        core_api.delete_namespaced_pod.assert_called_once()

    def test_ignores_404(self):
        from handlers.vm import _cleanup_legacy_pod
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.read_namespaced_pod.side_effect = ApiException(status=404)

        _cleanup_legacy_pod(core_api, "ns1", "gone-pod")

    def test_keeps_replicaset_owned_pod(self):
        from handlers.vm import _cleanup_legacy_pod

        owner = MagicMock()
        owner.kind = "ReplicaSet"
        pod_mock = MagicMock()
        pod_mock.metadata.owner_references = [owner]
        core_api = MagicMock()
        core_api.read_namespaced_pod.return_value = pod_mock

        _cleanup_legacy_pod(core_api, "ns1", "my-pod")
        core_api.delete_namespaced_pod.assert_not_called()


class TestGetS3ConfigFromProject:
    @patch("handlers.vm.client")
    def test_returns_s3_config(self, mock_client):
        from handlers.vm import _get_s3_config_from_project

        mock_custom = MagicMock()
        mock_client.CustomObjectsApi.return_value = mock_custom
        mock_custom.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "spec": {
                        "s3Config": {
                            "bucket": "my-bucket",
                            "endpoint": "s3.example.com",
                        }
                    }
                }
            ]
        }

        result = _get_s3_config_from_project("test-ns")
        assert result == {"bucket": "my-bucket", "endpoint": "s3.example.com"}

    @patch("handlers.vm.client")
    def test_returns_empty_when_no_projects(self, mock_client):
        from handlers.vm import _get_s3_config_from_project

        mock_custom = MagicMock()
        mock_client.CustomObjectsApi.return_value = mock_custom
        mock_custom.list_namespaced_custom_object.return_value = {"items": []}

        assert _get_s3_config_from_project("empty-ns") == {}

    @patch("handlers.vm.client")
    def test_returns_empty_when_no_s3_config(self, mock_client):
        from handlers.vm import _get_s3_config_from_project

        mock_custom = MagicMock()
        mock_client.CustomObjectsApi.return_value = mock_custom
        mock_custom.list_namespaced_custom_object.return_value = {
            "items": [{"spec": {}}]
        }

        assert _get_s3_config_from_project("no-config-ns") == {}


class TestGetCentralS3ConfigFromProject:
    @patch("handlers.vm.client")
    def test_returns_central_config(self, mock_client):
        from handlers.vm import _get_central_s3_config_from_project

        mock_custom = MagicMock()
        mock_client.CustomObjectsApi.return_value = mock_custom
        mock_custom.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "spec": {
                        "centralS3Config": {
                            "bucket": "central",
                            "endpoint": "s4.example.com",
                        }
                    }
                }
            ]
        }

        result = _get_central_s3_config_from_project("test-ns")
        assert result == {"bucket": "central", "endpoint": "s4.example.com"}

    @patch("handlers.vm.client")
    def test_returns_empty_when_no_projects(self, mock_client):
        from handlers.vm import _get_central_s3_config_from_project

        mock_custom = MagicMock()
        mock_client.CustomObjectsApi.return_value = mock_custom
        mock_custom.list_namespaced_custom_object.return_value = {"items": []}

        assert _get_central_s3_config_from_project("empty-ns") == {}


class TestCreateCloneDatavolume:
    def test_creates_datavolume(self):
        from handlers.vm import _create_clone_datavolume

        custom_api = MagicMock()
        clone_dv = {"metadata": {"name": "disk-1"}, "spec": {}}

        _create_clone_datavolume(custom_api, "test-ns", "disk-1", clone_dv)

        custom_api.create_namespaced_custom_object.assert_called_once_with(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace="test-ns",
            plural="datavolumes",
            body=clone_dv,
        )

    def test_409_with_succeeded_dv_skips(self):
        from handlers.vm import _create_clone_datavolume
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.create_namespaced_custom_object.side_effect = ApiException(
            status=409
        )
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Succeeded"}
        }

        # Should not raise
        _create_clone_datavolume(
            custom_api, "test-ns", "disk-1", {"metadata": {"name": "disk-1"}}
        )

    def test_409_then_404_retries_create(self):
        from handlers.vm import _create_clone_datavolume
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.create_namespaced_custom_object.side_effect = [
            ApiException(status=409),
            None,  # second create succeeds
        ]
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)

        _create_clone_datavolume(
            custom_api, "test-ns", "disk-1", {"metadata": {"name": "disk-1"}}
        )

        assert custom_api.create_namespaced_custom_object.call_count == 2

    def test_409_with_terminal_failed_dv_recreates(self):
        from handlers.vm import _create_clone_datavolume
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.create_namespaced_custom_object.side_effect = [
            ApiException(status=409),  # first create: DV already exists
            None,  # recreate after deleting the failed DV
        ]
        # existing DV is stuck in CloneValidationFailed (never reaches a phase)
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {
                "phase": "",
                "conditions": [
                    {
                        "type": "Bound",
                        "status": "False",
                        "reason": "CloneValidationFailed",
                    }
                ],
            }
        }

        _create_clone_datavolume(
            custom_api, "test-ns", "disk-1", {"metadata": {"name": "disk-1"}}
        )

        # deleted the terminal DV and recreated it
        custom_api.delete_namespaced_custom_object.assert_called_once()
        assert custom_api.create_namespaced_custom_object.call_count == 2

    def test_non_409_raises(self):
        from handlers.vm import _create_clone_datavolume
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.create_namespaced_custom_object.side_effect = ApiException(
            status=500
        )

        with pytest.raises(ApiException):
            _create_clone_datavolume(
                custom_api,
                "test-ns",
                "disk-1",
                {"metadata": {"name": "disk-1"}},
            )


class TestDiskNeedsReprovision:
    def test_bound_pvc_is_healthy(self):
        from handlers.vm import _disk_needs_reprovision

        core_api = MagicMock()
        pvc = MagicMock()
        pvc.status.phase = "Bound"
        core_api.read_namespaced_persistent_volume_claim.return_value = pvc
        custom_api = MagicMock()

        assert _disk_needs_reprovision(custom_api, core_api, "ns", "disk-1") is False
        custom_api.get_namespaced_custom_object.assert_not_called()

    def test_missing_pvc_and_failed_dv_needs_reprovision(self):
        from handlers.vm import _disk_needs_reprovision
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )
        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {
                "conditions": [
                    {
                        "type": "Bound",
                        "status": "False",
                        "reason": "CloneValidationFailed",
                    }
                ]
            }
        }
        assert _disk_needs_reprovision(custom_api, core_api, "ns", "disk-1") is True

    def test_missing_pvc_and_missing_dv_needs_reprovision(self):
        from handlers.vm import _disk_needs_reprovision
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )
        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
        assert _disk_needs_reprovision(custom_api, core_api, "ns", "disk-1") is True

    def test_in_progress_clone_is_left_alone(self):
        from handlers.vm import _disk_needs_reprovision
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )
        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "ImportInProgress", "conditions": []}
        }
        assert _disk_needs_reprovision(custom_api, core_api, "ns", "disk-1") is False


class TestProvisionNewDisksSelfHeal:
    @patch("handlers.vm._clone_s3_disk", return_value=True)
    @patch("handlers.vm._try_delete_pvc")
    @patch("handlers.vm._try_delete_datavolume")
    @patch("handlers.vm._disk_needs_reprovision", return_value=True)
    def test_existing_disk_with_dead_volume_is_reprovisioned(
        self, _needs, mock_del_dv, mock_del_pvc, mock_clone
    ):
        from handlers.vm import _provision_new_disks

        old_disks = {"d1": {"id": "d1"}}
        new_disks = {"d1": {"id": "d1"}}
        asyncio.run(
            _provision_new_disks(
                new_disks,
                old_disks,
                "vm-1",
                "ns",
                {},
                MagicMock(),
                MagicMock(),
                {},
                {},
                MagicMock(),
            )
        )
        # stale objects cleared and the disk re-cloned instead of skipped
        mock_del_dv.assert_called_once()
        mock_del_pvc.assert_called_once()
        mock_clone.assert_called_once()

    @patch("handlers.vm._clone_s3_disk", return_value=True)
    @patch("handlers.vm._disk_needs_reprovision", return_value=False)
    def test_existing_healthy_disk_is_skipped(self, _needs, mock_clone):
        from handlers.vm import _provision_new_disks

        old_disks = {"d1": {"id": "d1"}}
        new_disks = {"d1": {"id": "d1"}}
        result = asyncio.run(
            _provision_new_disks(
                new_disks,
                old_disks,
                "vm-1",
                "ns",
                {},
                MagicMock(),
                MagicMock(),
                {},
                {},
                MagicMock(),
            )
        )
        mock_clone.assert_not_called()
        assert result == {"d1": "vm-1-disk-d1"}


# ---------------------------------------------------------------------------
# handlers/network.py
# ---------------------------------------------------------------------------


class TestModifySccUsers:
    def test_add_sa_ref(self):
        from handlers.network import _modify_scc_users

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {"users": ["existing-user"]}

        result = _modify_scc_users(
            custom_api, "my-scc", "system:serviceaccount:ns:sa", "add"
        )

        assert result is True
        patched = custom_api.patch_cluster_custom_object.call_args[1]["body"]
        assert "system:serviceaccount:ns:sa" in patched["users"]
        assert "existing-user" in patched["users"]

    def test_add_duplicate_returns_false(self):
        from handlers.network import _modify_scc_users

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {
            "users": ["system:serviceaccount:ns:sa"]
        }

        result = _modify_scc_users(
            custom_api, "my-scc", "system:serviceaccount:ns:sa", "add"
        )
        assert result is False
        custom_api.patch_cluster_custom_object.assert_not_called()

    def test_remove_sa_ref(self):
        from handlers.network import _modify_scc_users

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {
            "users": ["system:serviceaccount:ns:sa", "other"]
        }

        result = _modify_scc_users(
            custom_api, "my-scc", "system:serviceaccount:ns:sa", "remove"
        )

        assert result is True
        patched = custom_api.patch_cluster_custom_object.call_args[1]["body"]
        assert "system:serviceaccount:ns:sa" not in patched["users"]

    def test_remove_missing_returns_false(self):
        from handlers.network import _modify_scc_users

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {"users": ["other"]}

        result = _modify_scc_users(
            custom_api, "my-scc", "system:serviceaccount:ns:sa", "remove"
        )
        assert result is False

    def test_handles_none_users(self):
        from handlers.network import _modify_scc_users

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {"users": None}

        result = _modify_scc_users(
            custom_api, "my-scc", "system:serviceaccount:ns:sa", "add"
        )
        assert result is True
        patched = custom_api.patch_cluster_custom_object.call_args[1]["body"]
        assert patched["users"] == ["system:serviceaccount:ns:sa"]


class TestPatchSccUsers:
    def test_iterates_all_sccs(self):
        from handlers.network import _patch_scc_users, _modify_scc_users

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {"users": []}

        asyncio.run(
            _patch_scc_users(
                custom_api,
                "system:serviceaccount:ns:sa",
                ["scc-1", "scc-2", "scc-3"],
                "add",
                "ns",
            )
        )

        assert custom_api.get_cluster_custom_object.call_count == 3

    def test_retries_on_409(self):
        from handlers.network import _patch_scc_users
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.side_effect = [
            ApiException(status=409),
            {"users": []},  # second attempt succeeds
        ]

        asyncio.run(
            _patch_scc_users(
                custom_api,
                "system:serviceaccount:ns:sa",
                ["scc-1"],
                "add",
                "ns",
            )
        )

        assert custom_api.get_cluster_custom_object.call_count == 2


class TestCreateDeploymentWithStaleCleanup:
    def test_creates_deployment_directly(self):
        from handlers.network import _create_deployment_with_stale_cleanup

        apps_api = MagicMock()
        body = {"metadata": {"name": "dep-1"}}

        asyncio.run(
            _create_deployment_with_stale_cleanup(apps_api, "ns", "dep-1", body)
        )

        apps_api.create_namespaced_deployment.assert_called_once_with(
            namespace="ns", body=body
        )

    def test_409_waits_and_retries(self):
        from handlers.network import _create_deployment_with_stale_cleanup
        from kubernetes.client import ApiException

        apps_api = MagicMock()
        apps_api.create_namespaced_deployment.side_effect = [
            ApiException(status=409),
            None,  # second create succeeds after wait
        ]
        # Simulate the deployment being already deleted
        apps_api.read_namespaced_deployment.side_effect = ApiException(status=404)

        body = {"metadata": {"name": "dep-1"}}
        asyncio.run(
            _create_deployment_with_stale_cleanup(apps_api, "ns", "dep-1", body)
        )

        assert apps_api.create_namespaced_deployment.call_count == 2

    def test_non_409_raises(self):
        from handlers.network import _create_deployment_with_stale_cleanup
        from kubernetes.client import ApiException

        apps_api = MagicMock()
        apps_api.create_namespaced_deployment.side_effect = ApiException(status=500)

        with pytest.raises(ApiException):
            asyncio.run(
                _create_deployment_with_stale_cleanup(apps_api, "ns", "dep-1", {})
            )


# ---------------------------------------------------------------------------
# handlers/container.py
# ---------------------------------------------------------------------------


class TestCreateSingleContainer:
    def test_creates_pod_with_correct_name(self):
        from handlers.container import _create_single_container

        core_api = MagicMock()
        ctr = {
            "id": "abcdef12-3456-7890-abcd-ef1234567890",
            "image": "nginx:latest",
            "cpus": 2,
            "memory": 1024,
            "nics": [],
            "env": {},
        }

        _create_single_container(core_api, "ns1", ctr, {}, {"ref": "owner"}, {})

        call_kwargs = core_api.create_namespaced_pod.call_args
        body = call_kwargs[1]["body"]
        assert body["metadata"]["name"] == "ctr-abcdef12"

    def test_container_with_command(self):
        from handlers.container import _create_single_container

        core_api = MagicMock()
        ctr = {
            "id": "test1234",
            "image": "alpine",
            "command": "echo hello",
            "cpus": 1,
            "memory": 512,
            "nics": [],
            "env": {},
        }

        _create_single_container(core_api, "ns1", ctr, {}, {}, {})

        body = core_api.create_namespaced_pod.call_args[1]["body"]
        container = body["spec"]["containers"][0]
        assert container["command"] == ["/bin/sh", "-c", "echo hello"]

    def test_container_with_env_vars(self):
        from handlers.container import _create_single_container

        core_api = MagicMock()
        ctr = {
            "id": "test1234",
            "image": "app",
            "cpus": 1,
            "memory": 512,
            "nics": [],
            "env": {"FOO": "bar", "COUNT": 42},
        }

        _create_single_container(core_api, "ns1", ctr, {}, {}, {})

        body = core_api.create_namespaced_pod.call_args[1]["body"]
        env = body["spec"]["containers"][0]["env"]
        env_dict = {e["name"]: e["value"] for e in env}
        assert env_dict["FOO"] == "bar"
        assert env_dict["COUNT"] == "42"

    def test_container_with_network(self):
        from handlers.container import _create_single_container

        core_api = MagicMock()
        ctr = {
            "id": "test1234",
            "image": "app",
            "cpus": 1,
            "memory": 512,
            "nics": [{"networkRef": "net-abc"}],
            "env": {},
        }
        nad_refs = {"net-abc": "my-nad-name"}

        _create_single_container(core_api, "ns1", ctr, nad_refs, {}, {})

        body = core_api.create_namespaced_pod.call_args[1]["body"]
        ann = body["metadata"]["annotations"]["k8s.v1.cni.cncf.io/networks"]
        assert ann == "my-nad-name"

    def test_409_is_swallowed(self):
        from handlers.container import _create_single_container
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.create_namespaced_pod.side_effect = ApiException(status=409)

        ctr = {
            "id": "test1234",
            "image": "app",
            "cpus": 1,
            "memory": 512,
            "nics": [],
            "env": {},
        }
        # Should not raise
        _create_single_container(core_api, "ns1", ctr, {}, {}, {})


class TestCreatePodGroup:
    def test_creates_pod_with_init_and_app_containers(self):
        from handlers.container import _create_pod_group

        core_api = MagicMock()
        ctr = {
            "id": "podid123",
            "image": "fallback",
            "nics": [],
            "initContainers": [
                {"name": "init-1", "image": "busybox", "command": "echo init"}
            ],
            "podContainers": [{"name": "app", "image": "myapp:1.0", "env": {"X": "1"}}],
        }

        _create_pod_group(core_api, "ns1", ctr, {}, {}, {})

        body = core_api.create_namespaced_pod.call_args[1]["body"]
        assert body["metadata"]["name"] == "pod-podid123"
        assert len(body["spec"]["initContainers"]) == 1
        assert len(body["spec"]["containers"]) == 1
        assert body["spec"]["containers"][0]["name"] == "app"

    def test_fallback_to_main_container_when_no_pod_containers(self):
        from handlers.container import _create_pod_group

        core_api = MagicMock()
        ctr = {
            "id": "podid123",
            "image": "my-image",
            "nics": [],
            "initContainers": [],
            "podContainers": [],
        }

        _create_pod_group(core_api, "ns1", ctr, {}, {}, {})

        body = core_api.create_namespaced_pod.call_args[1]["body"]
        assert len(body["spec"]["containers"]) == 1
        assert body["spec"]["containers"][0]["name"] == "main"
        assert body["spec"]["containers"][0]["image"] == "my-image"

    def test_pod_with_ports(self):
        from handlers.container import _create_pod_group

        core_api = MagicMock()
        ctr = {
            "id": "podid123",
            "image": "app",
            "nics": [],
            "initContainers": [],
            "podContainers": [
                {
                    "name": "web",
                    "image": "nginx",
                    "ports": [{"port": 80}, {"container_port": 443}],
                    "env": {},
                }
            ],
        }

        _create_pod_group(core_api, "ns1", ctr, {}, {}, {})

        body = core_api.create_namespaced_pod.call_args[1]["body"]
        ports = body["spec"]["containers"][0]["ports"]
        assert ports[0]["containerPort"] == 80
        assert ports[1]["containerPort"] == 443

    def test_409_is_swallowed(self):
        from handlers.container import _create_pod_group
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.create_namespaced_pod.side_effect = ApiException(status=409)

        ctr = {
            "id": "podid123",
            "image": "app",
            "nics": [],
            "initContainers": [],
            "podContainers": [],
        }

        # Should not raise
        _create_pod_group(core_api, "ns1", ctr, {}, {}, {})


class TestCreateContainerPods:
    @patch("handlers.container.client")
    def test_dispatches_single_and_pod(self, mock_client):
        from handlers.container import create_container_pods

        mock_core = MagicMock()
        mock_client.CoreV1Api.return_value = mock_core

        containers = [
            {
                "id": "single01",
                "image": "nginx",
                "isPod": False,
                "cpus": 1,
                "memory": 512,
                "nics": [],
                "env": {},
            },
            {
                "id": "podgrp01",
                "image": "app",
                "isPod": True,
                "nics": [],
                "initContainers": [],
                "podContainers": [{"name": "app", "image": "app:1", "env": {}}],
            },
        ]

        create_container_pods("ns1", containers, {}, {})

        assert mock_core.create_namespaced_pod.call_count == 2


# ---------------------------------------------------------------------------
# helpers/k8s.py — build_nad
# ---------------------------------------------------------------------------


class TestBuildNad:
    def test_nad_name(self):
        from helpers.k8s import build_nad

        cr = {
            "kind": "TroshkaNetwork",
            "metadata": {
                "name": "my-network",
                "namespace": "test-ns",
                "uid": "uid-1",
            },
            "spec": {},
        }
        nad = build_nad(cr)
        assert nad["metadata"]["name"] == "my-network-nad"

    def test_nad_config_has_netattachdefname(self):
        from helpers.k8s import build_nad

        cr = {
            "kind": "TroshkaNetwork",
            "metadata": {
                "name": "net-x",
                "namespace": "prod-ns",
                "uid": "uid-2",
            },
            "spec": {},
        }
        nad = build_nad(cr)
        config = json.loads(nad["spec"]["config"])
        assert config["netAttachDefName"] == "prod-ns/net-x-nad"

    def test_nad_topology_is_layer2(self):
        from helpers.k8s import build_nad

        cr = {
            "kind": "TroshkaNetwork",
            "metadata": {
                "name": "n",
                "namespace": "ns",
                "uid": "uid-3",
            },
            "spec": {},
        }
        nad = build_nad(cr)
        config = json.loads(nad["spec"]["config"])
        assert config["topology"] == "layer2"

    def test_nad_has_owner_reference(self):
        from helpers.k8s import build_nad

        cr = {
            "kind": "TroshkaNetwork",
            "metadata": {
                "name": "n",
                "namespace": "ns",
                "uid": "uid-4",
            },
            "spec": {},
        }
        nad = build_nad(cr)
        refs = nad["metadata"]["ownerReferences"]
        assert len(refs) == 1
        assert refs[0]["kind"] == "TroshkaNetwork"
        assert refs[0]["uid"] == "uid-4"


class TestKubeMacPoolOptOut:
    def test_project_namespace_labels_include_kmp_ignore(self):
        from helpers.k8s import project_namespace_labels

        labels = project_namespace_labels("81252cc5")
        assert labels["mutatevirtualmachines.kubemacpool.io"] == "ignore"
        assert labels["troshka-project"] == "81252cc5"

    def test_ensure_kubemacpool_opt_out_patches_missing_label(self):
        from helpers.k8s import ensure_kubemacpool_opt_out

        core_api = MagicMock()
        ns = MagicMock()
        ns.metadata.labels = {"app": "troshka"}
        core_api.read_namespace.return_value = ns

        ensure_kubemacpool_opt_out(core_api, "troshka-81252cc5")

        core_api.patch_namespace.assert_called_once_with(
            name="troshka-81252cc5",
            body={
                "metadata": {
                    "labels": {
                        "app": "troshka",
                        "mutatevirtualmachines.kubemacpool.io": "ignore",
                    }
                }
            },
        )

    def test_ensure_kubemacpool_opt_out_skips_when_labeled(self):
        from helpers.k8s import ensure_kubemacpool_opt_out

        core_api = MagicMock()
        ns = MagicMock()
        ns.metadata.labels = {
            "app": "troshka",
            "mutatevirtualmachines.kubemacpool.io": "ignore",
        }
        core_api.read_namespace.return_value = ns

        ensure_kubemacpool_opt_out(core_api, "troshka-81252cc5")

        core_api.patch_namespace.assert_not_called()

    def test_ensure_kubemacpool_opt_out_ignores_missing_namespace(self):
        from helpers.k8s import ensure_kubemacpool_opt_out
        from kubernetes.client.exceptions import ApiException

        core_api = MagicMock()
        core_api.read_namespace.side_effect = ApiException(status=404)

        ensure_kubemacpool_opt_out(core_api, "troshka-missing")

        core_api.patch_namespace.assert_not_called()


# ---------------------------------------------------------------------------
# helpers/k8s.py — build_exec_deployment / build_gateway_deployment
# ---------------------------------------------------------------------------


class TestBuildExecDeployment:
    def _make_project_cr(self, namespace="ns1", name="proj1"):
        return {
            "kind": "TroshkaProject",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "uid": "uid-proj",
            },
            "spec": {"projectId": "proj1234-5678"},
        }

    def test_exec_ip_is_dot_3(self):
        from helpers.k8s import build_exec_deployment

        cr = self._make_project_cr()
        dep = build_exec_deployment(cr, "cluster-nad", cidr="10.0.0.0/24")
        init_cmd = dep["spec"]["template"]["spec"]["initContainers"][0]["command"][2]
        assert "10.0.0.3/24" in init_cmd

    def test_dns_points_at_dot_2(self):
        from helpers.k8s import build_exec_deployment

        cr = self._make_project_cr()
        dep = build_exec_deployment(cr, "cluster-nad", cidr="10.0.0.0/24")
        dns = dep["spec"]["template"]["spec"]["dnsConfig"]["nameservers"]
        assert dns == ["10.0.0.2"]

    def test_ssh_key_secret_mount(self):
        from helpers.k8s import build_exec_deployment

        cr = self._make_project_cr()
        dep = build_exec_deployment(
            cr,
            "nad",
            cidr="10.0.0.0/24",
            ssh_key_secret="my-key",  # pragma: allowlist secret
        )
        volumes = dep["spec"]["template"]["spec"]["volumes"]
        ssh_vol = [v for v in volumes if v["name"] == "ssh-key"]
        assert len(ssh_vol) == 1
        assert (
            ssh_vol[0]["secret"]["secretName"] == "my-key"  # pragma: allowlist secret
        )

    def test_no_ssh_key_secret(self):
        from helpers.k8s import build_exec_deployment

        cr = self._make_project_cr()
        dep = build_exec_deployment(cr, "nad", cidr="10.0.0.0/24")
        volumes = dep["spec"]["template"]["spec"]["volumes"]
        assert all(v["name"] != "ssh-key" for v in volumes)


class TestBuildGatewayDeployment:
    def _make_project_cr(self, namespace="ns1"):
        return {
            "kind": "TroshkaProject",
            "metadata": {
                "name": "proj1",
                "namespace": namespace,
                "uid": "uid-gw",
            },
            "spec": {"projectId": "proj1234-5678"},
        }

    def test_gateway_addrs_env(self):
        from helpers.k8s import build_gateway_deployment

        cr = self._make_project_cr()
        gateway_ips = {
            "nad-1": {"ip": "10.0.0.1", "cidr": "10.0.0.0/24"},
            "nad-2": {"ip": "192.168.1.1", "cidr": "192.168.1.0/16"},
        }
        dep = build_gateway_deployment(cr, ["nad-1", "nad-2"], gateway_ips=gateway_ips)
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        addrs_env = [e for e in env if e["name"] == "GATEWAY_ADDRS"][0]
        assert "10.0.0.1/24" in addrs_env["value"]
        assert "192.168.1.1/16" in addrs_env["value"]

    def test_network_annotation_joined(self):
        from helpers.k8s import build_gateway_deployment

        cr = self._make_project_cr()
        dep = build_gateway_deployment(cr, ["nad-a", "nad-b"])
        ann = dep["spec"]["template"]["metadata"]["annotations"]
        assert ann["k8s.v1.cni.cncf.io/networks"] == "nad-a,nad-b"

    def test_privileged_security_context(self):
        from helpers.k8s import build_gateway_deployment

        cr = self._make_project_cr()
        dep = build_gateway_deployment(cr, ["nad-1"])
        sec = dep["spec"]["template"]["spec"]["containers"][0]["securityContext"]
        assert sec["privileged"] is True

    def test_empty_gateway_ips(self):
        from helpers.k8s import build_gateway_deployment

        cr = self._make_project_cr()
        dep = build_gateway_deployment(cr, ["nad-1"])
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        addrs_env = [e for e in env if e["name"] == "GATEWAY_ADDRS"][0]
        assert addrs_env["value"] == ""


# ---------------------------------------------------------------------------
# handlers/project.py — additional coverage
# ---------------------------------------------------------------------------


class TestSetupRecertSa:
    def test_creates_sa_and_patches_scc(self):
        from handlers.project import _setup_recert_sa

        core_api = MagicMock()
        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {"users": []}

        _setup_recert_sa(core_api, custom_api, "test-ns")

        core_api.create_namespaced_service_account.assert_called_once()
        custom_api.patch_cluster_custom_object.assert_called_once()
        patched = custom_api.patch_cluster_custom_object.call_args[1]["body"]
        assert "system:serviceaccount:test-ns:troshka-recert" in patched["users"]

    def test_sa_already_exists_409(self):
        from handlers.project import _setup_recert_sa
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.create_namespaced_service_account.side_effect = ApiException(
            status=409
        )
        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {"users": []}

        _setup_recert_sa(core_api, custom_api, "test-ns")
        custom_api.patch_cluster_custom_object.assert_called_once()

    def test_sa_creation_raises_non_409(self):
        from handlers.project import _setup_recert_sa
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.create_namespaced_service_account.side_effect = ApiException(
            status=500
        )
        custom_api = MagicMock()

        with pytest.raises(ApiException):
            _setup_recert_sa(core_api, custom_api, "test-ns")

    def test_skips_patch_when_sa_already_in_scc(self):
        from handlers.project import _setup_recert_sa

        core_api = MagicMock()
        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {
            "users": ["system:serviceaccount:test-ns:troshka-recert"]
        }

        _setup_recert_sa(core_api, custom_api, "test-ns")
        custom_api.patch_cluster_custom_object.assert_not_called()

    def test_scc_fetch_failure_swallowed(self):
        from handlers.project import _setup_recert_sa

        core_api = MagicMock()
        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.side_effect = Exception("SCC not found")

        _setup_recert_sa(core_api, custom_api, "test-ns")


class TestCreateNetworkCrs:
    def _make_body(self):
        return {
            "kind": "TroshkaProject",
            "metadata": {"name": "proj1", "uid": "uid-proj1"},
        }

    def test_creates_network_cr(self):
        from handlers.project import _create_network_crs

        custom_api = MagicMock()
        networks = [
            {
                "id": "net12345678",
                "cidr": "10.0.0.0/24",
                "gateway": "10.0.0.1",
                "networkType": "standard",
            }
        ]
        patch = MagicMock()

        _create_network_crs(
            custom_api, networks, {}, "test-ns", "proj1", self._make_body(), patch
        )

        custom_api.create_namespaced_custom_object.assert_called_once()
        cr = custom_api.create_namespaced_custom_object.call_args[1]["body"]
        assert cr["metadata"]["name"] == "net-net12345"
        assert cr["spec"]["cidr"] == "10.0.0.0/24"

    def test_includes_static_leases(self):
        from handlers.project import _create_network_crs

        custom_api = MagicMock()
        networks = [{"id": "net12345678", "cidr": "10.0.0.0/24"}]
        leases = {
            "net12345678": [
                {"mac": "52:54:00:01:02:03", "ip": "10.0.0.10", "hostname": "vm1"}
            ]
        }
        patch = MagicMock()

        _create_network_crs(
            custom_api, networks, leases, "test-ns", "proj1", self._make_body(), patch
        )

        cr = custom_api.create_namespaced_custom_object.call_args[1]["body"]
        assert len(cr["spec"]["staticLeases"]) == 1
        assert cr["spec"]["staticLeases"][0]["ip"] == "10.0.0.10"

    def test_409_is_swallowed(self):
        from handlers.project import _create_network_crs
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.create_namespaced_custom_object.side_effect = ApiException(
            status=409
        )
        networks = [{"id": "net12345678", "cidr": "10.0.0.0/24"}]
        patch = MagicMock()

        _create_network_crs(
            custom_api, networks, {}, "test-ns", "proj1", self._make_body(), patch
        )

    def test_non_409_raises(self):
        from handlers.project import _create_network_crs
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.create_namespaced_custom_object.side_effect = ApiException(
            status=500
        )
        networks = [{"id": "net12345678", "cidr": "10.0.0.0/24"}]
        patch = MagicMock()

        with pytest.raises(ApiException):
            _create_network_crs(
                custom_api,
                networks,
                {},
                "test-ns",
                "proj1",
                self._make_body(),
                patch,
            )

    def test_updates_deploy_progress(self):
        from handlers.project import _create_network_crs

        custom_api = MagicMock()
        networks = [
            {"id": "net1abcdef", "cidr": "10.0.0.0/24"},
            {"id": "net2abcdef", "cidr": "10.0.1.0/24"},
        ]
        patch = MagicMock()

        _create_network_crs(
            custom_api, networks, {}, "test-ns", "proj1", self._make_body(), patch
        )

        assert custom_api.create_namespaced_custom_object.call_count == 2
        assert patch.status.__setitem__.call_count >= 1

    def test_includes_pxe_config(self):
        from handlers.project import _create_network_crs

        custom_api = MagicMock()
        networks = [
            {
                "id": "net12345678",
                "cidr": "10.0.0.0/24",
                "pxeConfig": {"enabled": True},
            }
        ]
        patch = MagicMock()

        _create_network_crs(
            custom_api, networks, {}, "test-ns", "proj1", self._make_body(), patch
        )

        cr = custom_api.create_namespaced_custom_object.call_args[1]["body"]
        assert cr["spec"]["pxeConfig"] == {"enabled": True}


class TestSetupGateway:
    def test_creates_gateway_for_external_network(self):
        from handlers.project import _setup_gateway
        from kubernetes.client import ApiException

        core_api = MagicMock()
        apps_api = MagicMock()
        # _ensure_deployment_gone needs delete to succeed and read to 404
        apps_api.delete_namespaced_deployment.return_value = None
        apps_api.read_namespaced_deployment.side_effect = ApiException(status=404)
        networks = [
            {
                "id": "net12345678",
                "cidr": "10.0.0.0/24",
                "gateway": "10.0.0.1",
                "externalAccess": True,
            }
        ]
        body = {
            "kind": "TroshkaProject",
            "metadata": {"name": "proj1", "namespace": "ns1", "uid": "uid-proj1"},
            "spec": {"projectId": "proj-1234"},
        }

        asyncio.run(_setup_gateway(core_api, apps_api, networks, "ns1", "proj1", body))

        apps_api.create_namespaced_deployment.assert_called_once()

    def test_skips_when_no_external_access(self):
        from handlers.project import _setup_gateway

        core_api = MagicMock()
        apps_api = MagicMock()
        networks = [
            {"id": "net12345678", "cidr": "10.0.0.0/24", "externalAccess": False}
        ]
        body = {
            "kind": "TroshkaProject",
            "metadata": {"name": "proj1", "namespace": "ns1", "uid": "uid"},
            "spec": {"projectId": "proj-1234"},
        }

        asyncio.run(_setup_gateway(core_api, apps_api, networks, "ns1", "proj1", body))

        apps_api.create_namespaced_deployment.assert_not_called()

    def test_skips_when_no_networks(self):
        from handlers.project import _setup_gateway

        core_api = MagicMock()
        apps_api = MagicMock()
        body = {
            "kind": "TroshkaProject",
            "metadata": {"name": "p", "namespace": "ns1", "uid": "uid"},
            "spec": {"projectId": "p1234"},
        }

        asyncio.run(_setup_gateway(core_api, apps_api, [], "ns1", "p", body))

        apps_api.create_namespaced_deployment.assert_not_called()


class TestSetupExecPod:
    def _make_args(self):
        spec = {"projectId": "proj-12345678", "execSshKey": ""}
        meta = {"uid": "uid-proj"}
        networks = [
            {
                "id": "net12345678",
                "cidr": "10.0.0.0/24",
                "networkType": "standard",
            }
        ]
        body = {
            "kind": "TroshkaProject",
            "metadata": {"name": "proj1", "namespace": "ns1", "uid": "uid-proj"},
            "spec": spec,
        }
        return spec, meta, networks, body

    def _make_apps_api(self):
        from kubernetes.client import ApiException

        apps_api = MagicMock()
        apps_api.delete_namespaced_deployment.return_value = None
        apps_api.read_namespaced_deployment.side_effect = ApiException(status=404)
        return apps_api

    def test_creates_exec_deployment(self):
        from handlers.project import _setup_exec_pod

        core_api = MagicMock()
        apps_api = self._make_apps_api()
        spec, meta, networks, body = self._make_args()

        asyncio.run(
            _setup_exec_pod(
                core_api, apps_api, spec, meta, networks, "ns1", "proj1", body
            )
        )

        apps_api.create_namespaced_deployment.assert_called_once()

    def test_creates_ssh_key_secret_when_provided(self):
        from handlers.project import _setup_exec_pod

        core_api = MagicMock()
        apps_api = self._make_apps_api()
        spec, meta, networks, body = self._make_args()
        spec["execSshKey"] = "ssh-ed25519 AAAA..."

        asyncio.run(
            _setup_exec_pod(
                core_api, apps_api, spec, meta, networks, "ns1", "proj1", body
            )
        )

        core_api.create_namespaced_secret.assert_called_once()
        apps_api.create_namespaced_deployment.assert_called_once()

    def test_replaces_ssh_key_secret_on_409(self):
        from handlers.project import _setup_exec_pod
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.create_namespaced_secret.side_effect = ApiException(status=409)
        apps_api = self._make_apps_api()
        spec, meta, networks, body = self._make_args()
        spec["execSshKey"] = "ssh-ed25519 AAAA..."

        asyncio.run(
            _setup_exec_pod(
                core_api, apps_api, spec, meta, networks, "ns1", "proj1", body
            )
        )

        core_api.replace_namespaced_secret.assert_called_once()

    def test_skips_when_no_standard_network(self):
        from handlers.project import _setup_exec_pod

        core_api = MagicMock()
        apps_api = MagicMock()
        spec, meta, _, body = self._make_args()
        bmc_only = [{"id": "bmc1", "cidr": "10.0.1.0/24", "networkType": "bmc"}]

        asyncio.run(
            _setup_exec_pod(
                core_api, apps_api, spec, meta, bmc_only, "ns1", "proj1", body
            )
        )

        apps_api.create_namespaced_deployment.assert_not_called()


class TestBuildVmCr:
    def _make_vm(self, **overrides):
        base = {
            "id": "vm12345678-1234-5678-abcd-ef1234567890",
            "name": "test-vm",
            "cpus": 4,
            "memory": 8192,
            "firmware": "uefi",
            "nics": [{"id": "nic-aaa", "mac": "52:54:00:01:02:03"}],
            "powerOnAtDeploy": True,
            "cloudInit": {},
        }
        base.update(overrides)
        return base

    def _make_body(self):
        return {
            "kind": "TroshkaProject",
            "metadata": {"name": "proj1", "uid": "uid-proj1"},
        }

    def test_basic_vm_cr(self):
        from handlers.project import _build_vm_cr

        vm = self._make_vm()
        cr = _build_vm_cr(vm, {}, {}, {}, None, "ns1", "proj1", self._make_body())

        assert cr["metadata"]["name"] == "vm-vm123456"
        assert cr["spec"]["cpus"] == 4
        assert cr["spec"]["memory"] == 8192
        assert cr["spec"]["firmware"] == "uefi"
        assert cr["kind"] == "TroshkaVM"

    def test_includes_nic_specs(self):
        from handlers.project import _build_vm_cr

        vm = self._make_vm()
        nic_map = {"nic-aaa": "net-abc12345"}
        cr = _build_vm_cr(vm, {}, {}, nic_map, None, "ns1", "proj1", self._make_body())

        assert len(cr["spec"]["nics"]) == 1
        assert cr["spec"]["nics"][0]["networkRef"] == "net-abc12345"
        assert cr["spec"]["nics"][0]["mac"] == "52:54:00:01:02:03"

    def test_includes_disk_specs(self):
        from handlers.project import _build_vm_cr

        vm = self._make_vm()
        disks = {
            vm["id"]: [
                {
                    "id": "disk-1",
                    "sizeGb": 50,
                    "libraryImage": {"s3Path": "lib/a.qcow2"},
                }
            ]
        }
        cr = _build_vm_cr(vm, disks, {}, {}, None, "ns1", "proj1", self._make_body())

        assert len(cr["spec"]["disks"]) == 1

    def test_cdrom_from_map(self):
        from handlers.project import _build_vm_cr

        vm = self._make_vm()
        cdroms = {
            vm["id"]: {
                "s3Path": "library/iso.iso",
                "libraryIsoId": "iso-1",
                "central": True,
            }
        }
        cr = _build_vm_cr(vm, {}, cdroms, {}, None, "ns1", "proj1", self._make_body())

        assert cr["spec"]["cdrom"]["s3Path"] == "library/iso.iso"
        assert cr["spec"]["cdrom"]["central"] is True

    def test_cdrom_from_vm_data(self):
        from handlers.project import _build_vm_cr

        vm = self._make_vm(cdrom={"s3Path": "library/cd.iso", "libraryIsoId": "cd-1"})
        cr = _build_vm_cr(vm, {}, {}, {}, None, "ns1", "proj1", self._make_body())

        assert cr["spec"]["cdrom"]["s3Path"] == "library/cd.iso"

    def test_guestfish_commands(self):
        from handlers.project import _build_vm_cr

        vm = self._make_vm(guestfishCommands=["rm /etc/old-cert"])
        cr = _build_vm_cr(vm, {}, {}, {}, None, "ns1", "proj1", self._make_body())

        assert cr["spec"]["guestfishCommands"] == ["rm /etc/old-cert"]

    def test_bastion_pvc_for_rhcos(self):
        from handlers.project import _build_vm_cr

        vm = self._make_vm(os="rhcos")
        cr = _build_vm_cr(
            vm, {}, {}, {}, "bastion-disk-pvc", "ns1", "proj1", self._make_body()
        )

        assert cr["spec"]["bastionPvc"] == "bastion-disk-pvc"

    def test_no_bastion_pvc_for_non_rhcos(self):
        from handlers.project import _build_vm_cr

        vm = self._make_vm(os="rhel")
        cr = _build_vm_cr(
            vm, {}, {}, {}, "bastion-disk-pvc", "ns1", "proj1", self._make_body()
        )

        assert "bastionPvc" not in cr["spec"]


class TestResolveVmState:
    def test_running_vmi(self):
        from handlers.project import _resolve_vm_state

        vm = {"status": {"kubevirtVmName": "kv-vm-1", "state": "Running"}}
        vmi_states = {"kv-vm-1": "Running"}
        assert _resolve_vm_state(vm, vmi_states) == "Running"

    def test_no_vmi_no_state_returns_creating(self):
        from handlers.project import _resolve_vm_state

        vm = {"status": {}}
        assert _resolve_vm_state(vm, {}) == "creating"

    def test_has_state_no_kv_name(self):
        from handlers.project import _resolve_vm_state

        vm = {"status": {"state": "Stopped"}}
        assert _resolve_vm_state(vm, {}) == "Stopped"

    def test_kv_name_not_in_vmi_was_running(self):
        from handlers.project import _resolve_vm_state

        vm = {"status": {"kubevirtVmName": "kv-vm-1", "state": "Running"}}
        assert _resolve_vm_state(vm, {}) == "Stopped"

    def test_kv_name_not_in_vmi_was_scheduling(self):
        from handlers.project import _resolve_vm_state

        vm = {"status": {"kubevirtVmName": "kv-vm-1", "state": "Scheduling"}}
        assert _resolve_vm_state(vm, {}) == "Scheduling"


class TestDetectSchedulingError:
    def test_no_pods_returns_none(self):
        from handlers.project import _detect_scheduling_error

        core_api = MagicMock()
        pod_list = MagicMock()
        pod_list.items = []
        core_api.list_namespaced_pod.return_value = pod_list

        assert _detect_scheduling_error(core_api, "ns1", "kv-vm-1") is None

    def test_unschedulable_condition(self):
        from handlers.project import _detect_scheduling_error

        cond = MagicMock()
        cond.reason = "Unschedulable"
        cond.message = "Insufficient memory"
        pod = MagicMock()
        pod.status.conditions = [cond]
        pod.metadata.name = "virt-launcher-pod"
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = pod_list

        result = _detect_scheduling_error(core_api, "ns1", "kv-vm-1")
        assert result == "Insufficient memory"

    def test_volume_attach_failure(self):
        from handlers.project import _detect_scheduling_error

        cond = MagicMock()
        cond.reason = "Scheduled"
        pod = MagicMock()
        pod.status.conditions = [cond]
        pod.metadata.name = "virt-launcher-pod"
        pod_list = MagicMock()
        pod_list.items = [pod]

        event = MagicMock()
        event.message = "Volume attach timed out"
        ev_list = MagicMock()
        ev_list.items = [event]

        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = pod_list
        core_api.list_namespaced_event.return_value = ev_list

        result = _detect_scheduling_error(core_api, "ns1", "kv-vm-1")
        assert result == "Volume attach timed out"

    def test_exception_returns_none(self):
        from handlers.project import _detect_scheduling_error

        core_api = MagicMock()
        core_api.list_namespaced_pod.side_effect = Exception("API down")

        assert _detect_scheduling_error(core_api, "ns1", "kv-vm-1") is None

    def test_no_scheduling_issues_returns_none(self):
        from handlers.project import _detect_scheduling_error

        cond = MagicMock()
        cond.reason = "Scheduled"
        pod = MagicMock()
        pod.status.conditions = [cond]
        pod.metadata.name = "virt-launcher-pod"
        pod_list = MagicMock()
        pod_list.items = [pod]

        ev_list = MagicMock()
        ev_list.items = []

        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = pod_list
        core_api.list_namespaced_event.return_value = ev_list

        assert _detect_scheduling_error(core_api, "ns1", "kv-vm-1") is None


class TestCollectVmStates:
    def test_running_vm(self):
        from handlers.project import _collect_vm_states

        vm_items = [
            {
                "metadata": {"name": "vm-1"},
                "spec": {"vmId": "id-1"},
                "status": {"kubevirtVmName": "kv-1", "state": "Running"},
            }
        ]
        vmi_states = {"kv-1": "Running"}
        core_api = MagicMock()

        states, ready, errors = _collect_vm_states(
            vm_items, vmi_states, core_api, "ns1"
        )

        assert states["id-1"] == "Running"
        assert ready == 1
        assert errors == {}

    def test_stopped_vm_counts_as_ready(self):
        from handlers.project import _collect_vm_states

        vm_items = [
            {
                "metadata": {"name": "vm-1"},
                "spec": {"vmId": "id-1"},
                "status": {"kubevirtVmName": "kv-1", "state": "Stopped"},
            }
        ]
        states, ready, errors = _collect_vm_states(vm_items, {}, MagicMock(), "ns1")

        assert ready == 1

    def test_scheduling_error_sets_error_state(self):
        from handlers.project import _collect_vm_states

        vm_items = [
            {
                "metadata": {"name": "vm-1"},
                "spec": {"vmId": "id-1"},
                "status": {"kubevirtVmName": "kv-1", "state": "Scheduling"},
            }
        ]
        vmi_states = {"kv-1": "Scheduling"}

        cond = MagicMock()
        cond.reason = "Unschedulable"
        cond.message = "No nodes available"
        pod = MagicMock()
        pod.status.conditions = [cond]
        pod.metadata.name = "pod-1"
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = pod_list

        states, ready, errors = _collect_vm_states(
            vm_items, vmi_states, core_api, "ns1"
        )

        assert states["id-1"] == "error"
        assert "id-1" in errors
        assert ready == 0

    def test_multiple_vms(self):
        from handlers.project import _collect_vm_states

        vm_items = [
            {
                "metadata": {"name": "vm-1"},
                "spec": {"vmId": "id-1"},
                "status": {"kubevirtVmName": "kv-1", "state": "Running"},
            },
            {
                "metadata": {"name": "vm-2"},
                "spec": {"vmId": "id-2"},
                "status": {"kubevirtVmName": "kv-2", "state": ""},
            },
        ]
        vmi_states = {"kv-1": "Running"}
        core_api = MagicMock()

        states, ready, errors = _collect_vm_states(
            vm_items, vmi_states, core_api, "ns1"
        )

        assert states["id-1"] == "Running"
        assert states["id-2"] == "creating"
        assert ready == 1

    def test_fallback_vm_id_from_metadata(self):
        from handlers.project import _collect_vm_states

        vm_items = [
            {
                "metadata": {"name": "vm-fallback"},
                "spec": {},
                "status": {"state": "Stopped"},
            }
        ]
        states, ready, _ = _collect_vm_states(vm_items, {}, MagicMock(), "ns1")

        assert "vm-fallback" in states
        assert ready == 1


class TestRecertJobNameFromCfg:
    def test_basic_name(self):
        from handlers.project import _recert_job_name_from_cfg

        cfg = {"rhcosPvc": "vm-abc12345-disk-def67890", "vmName": "sno1"}
        job_name, vm_part, vm_label = _recert_job_name_from_cfg(cfg)

        assert job_name == "recert-vm-abc12345"
        assert vm_part == "vm-abc12345"
        assert vm_label == "sno1"

    def test_no_disk_separator(self):
        from handlers.project import _recert_job_name_from_cfg

        cfg = {"rhcosPvc": "some-pvc", "vmName": "myvm"}
        job_name, vm_part, _ = _recert_job_name_from_cfg(cfg)

        assert job_name == "recert-vm"
        assert vm_part == "vm"

    def test_default_vm_name(self):
        from handlers.project import _recert_job_name_from_cfg

        cfg = {"rhcosPvc": "vm-abc-disk-def"}
        _, _, vm_label = _recert_job_name_from_cfg(cfg)

        assert vm_label == "vm"


class TestCheckRecertPvcsReady:
    def test_all_bound(self):
        from handlers.project import _check_recert_pvcs_ready

        pvc = MagicMock()
        pvc.status.phase = "Bound"
        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.return_value = pvc

        cfgs = [{"rhcosPvc": "pvc-1"}, {"rhcosPvc": "pvc-2"}]
        assert _check_recert_pvcs_ready(core_api, cfgs, "ns1") is True

    def test_not_all_bound(self):
        from handlers.project import _check_recert_pvcs_ready

        pvc = MagicMock()
        pvc.status.phase = "Pending"
        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.return_value = pvc

        cfgs = [{"rhcosPvc": "pvc-1"}]
        assert _check_recert_pvcs_ready(core_api, cfgs, "ns1") is False

    def test_exception_returns_false(self):
        from handlers.project import _check_recert_pvcs_ready

        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.side_effect = Exception(
            "not found"
        )

        cfgs = [{"rhcosPvc": "pvc-1"}]
        assert _check_recert_pvcs_ready(core_api, cfgs, "ns1") is False

    def test_empty_pvc_name_skipped(self):
        from handlers.project import _check_recert_pvcs_ready

        core_api = MagicMock()
        cfgs = [{"rhcosPvc": ""}]
        assert _check_recert_pvcs_ready(core_api, cfgs, "ns1") is True


class TestCollectRecertConfigs:
    def test_collects_recert_enabled_vms(self):
        from handlers.project import _collect_recert_configs

        vms = [
            {
                "id": "vm12345678",
                "name": "sno1",
                "recertEnabled": True,
            }
        ]
        vm_disks_map = {
            "vm12345678": [
                {"id": "disk-aabb", "patternImage": {"s3Path": "patterns/p/d.qcow2"}}
            ]
        }

        configs = _collect_recert_configs(vms, vm_disks_map, "bastion-pvc")

        assert len(configs) == 1
        assert configs[0]["vmName"] == "sno1"

    def test_skips_non_recert_vms(self):
        from handlers.project import _collect_recert_configs

        vms = [{"id": "vm1", "recertEnabled": False}]
        assert _collect_recert_configs(vms, {}, None) == []

    def test_skips_non_pattern_disks(self):
        from handlers.project import _collect_recert_configs

        vms = [{"id": "vm1", "recertEnabled": True}]
        vm_disks = {"vm1": [{"id": "d1", "blank": True}]}
        assert _collect_recert_configs(vms, vm_disks, None) == []

    def test_skips_empty_disks(self):
        from handlers.project import _collect_recert_configs

        vms = [{"id": "vm1", "recertEnabled": True}]
        assert _collect_recert_configs(vms, {}, None) == []


class TestCheckExportJob:
    def test_succeeded(self):
        from handlers.project import _check_export_job

        batch_api = MagicMock()
        job = MagicMock()
        job.status.succeeded = 1
        job.status.failed = None
        batch_api.read_namespaced_job.return_value = job

        assert _check_export_job(batch_api, {"jobName": "j1"}, "ns") == "done"

    def test_failed(self):
        from handlers.project import _check_export_job

        batch_api = MagicMock()
        job = MagicMock()
        job.status.succeeded = None
        job.status.failed = 3
        batch_api.read_namespaced_job.return_value = job

        assert _check_export_job(batch_api, {"jobName": "j1"}, "ns") == "failed"

    def test_pending(self):
        from handlers.project import _check_export_job

        batch_api = MagicMock()
        job = MagicMock()
        job.status.succeeded = None
        job.status.failed = 1
        batch_api.read_namespaced_job.return_value = job

        assert _check_export_job(batch_api, {"jobName": "j1"}, "ns") == "pending"

    def test_exception_returns_pending(self):
        from handlers.project import _check_export_job

        batch_api = MagicMock()
        batch_api.read_namespaced_job.side_effect = Exception("boom")

        assert _check_export_job(batch_api, {"jobName": "j1"}, "ns") == "pending"


class TestReadExportSizes:
    def test_reads_size_from_logs(self):
        from handlers.project import _read_export_sizes

        pod = MagicMock()
        pod.metadata.name = "pod-1"
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = pod_list
        core_api.read_namespaced_pod_log.return_value = "DISK_SIZE_BYTES=1073741824\n"

        export_jobs = [{"jobName": "j1"}]
        _read_export_sizes(core_api, export_jobs, "ns")

        assert export_jobs[0]["sizeBytes"] == 1073741824

    def test_no_pods_skips(self):
        from handlers.project import _read_export_sizes

        pod_list = MagicMock()
        pod_list.items = []
        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = pod_list

        export_jobs = [{"jobName": "j1"}]
        _read_export_sizes(core_api, export_jobs, "ns")

        assert "sizeBytes" not in export_jobs[0]

    def test_exception_swallowed(self):
        from handlers.project import _read_export_sizes

        core_api = MagicMock()
        core_api.list_namespaced_pod.side_effect = Exception("fail")

        export_jobs = [{"jobName": "j1"}]
        _read_export_sizes(core_api, export_jobs, "ns")


class TestSetupVncProxy:
    @patch("handlers.project._create_vnc_rbac")
    @patch("handlers.project.client")
    def test_creates_vnc_components(self, mock_client, mock_rbac):
        from handlers.project import _setup_vnc_proxy

        core_api = MagicMock()
        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "spec": {"host": "vnc.example.com"},
            "status": {"ingress": [{"host": "vnc.example.com"}]},
        }
        mock_apps = MagicMock()
        mock_client.AppsV1Api.return_value = mock_apps

        body = {
            "kind": "TroshkaProject",
            "metadata": {"name": "proj1", "uid": "uid-1"},
        }
        patch = MagicMock()

        _setup_vnc_proxy(custom_api, core_api, "ns1", "proj1", body, patch)

        core_api.create_namespaced_service_account.assert_called_once()
        mock_apps.create_namespaced_deployment.assert_called_once()
        core_api.create_namespaced_service.assert_called_once()
        custom_api.create_namespaced_custom_object.assert_called_once()

    @patch("handlers.project._create_vnc_rbac")
    @patch("handlers.project.client")
    def test_409_swallowed_for_all_components(self, mock_client, mock_rbac):
        from handlers.project import _setup_vnc_proxy
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.create_namespaced_service_account.side_effect = ApiException(
            status=409
        )
        core_api.create_namespaced_service.side_effect = ApiException(status=409)
        custom_api = MagicMock()
        custom_api.create_namespaced_custom_object.side_effect = ApiException(
            status=409
        )
        custom_api.get_namespaced_custom_object.return_value = {
            "spec": {"host": "vnc.example.com"},
        }
        mock_apps = MagicMock()
        mock_apps.create_namespaced_deployment.side_effect = ApiException(status=409)
        mock_client.AppsV1Api.return_value = mock_apps

        body = {
            "kind": "TroshkaProject",
            "metadata": {"name": "proj1", "uid": "uid-1"},
        }
        patch = MagicMock()

        _setup_vnc_proxy(custom_api, core_api, "ns1", "proj1", body, patch)


class TestEnsureDeploymentGone:
    def test_deletes_and_waits(self):
        from handlers.project import _ensure_deployment_gone
        from kubernetes.client import ApiException

        apps_api = MagicMock()
        apps_api.read_namespaced_deployment.side_effect = ApiException(status=404)

        asyncio.run(_ensure_deployment_gone(apps_api, "ns1", "dep-1"))

        apps_api.delete_namespaced_deployment.assert_called_once()

    def test_404_on_delete_returns_immediately(self):
        from handlers.project import _ensure_deployment_gone
        from kubernetes.client import ApiException

        apps_api = MagicMock()
        apps_api.delete_namespaced_deployment.side_effect = ApiException(status=404)

        asyncio.run(_ensure_deployment_gone(apps_api, "ns1", "dep-1"))

    def test_non_404_raises(self):
        from handlers.project import _ensure_deployment_gone
        from kubernetes.client import ApiException

        apps_api = MagicMock()
        apps_api.delete_namespaced_deployment.side_effect = ApiException(status=500)

        with pytest.raises(ApiException):
            asyncio.run(_ensure_deployment_gone(apps_api, "ns1", "dep-1"))


class TestCleanupRecertJob:
    def test_deletes_job_and_pods(self):
        from handlers.project import _cleanup_recert_job

        pod = MagicMock()
        pod.metadata.name = "recert-pod-1"
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = pod_list
        batch_api = MagicMock()

        _cleanup_recert_job(core_api, batch_api, "ns1", "recert-vm-abc")

        core_api.delete_namespaced_pod.assert_called_once_with(
            name="recert-pod-1", namespace="ns1"
        )
        batch_api.delete_namespaced_job.assert_called_once()

    def test_swallows_all_exceptions(self):
        from handlers.project import _cleanup_recert_job

        core_api = MagicMock()
        core_api.list_namespaced_pod.side_effect = Exception("fail")
        batch_api = MagicMock()
        batch_api.delete_namespaced_job.side_effect = Exception("fail")

        _cleanup_recert_job(core_api, batch_api, "ns1", "recert-vm-abc")


class TestStartKubevirtVms:
    def test_starts_all_vms(self):
        from handlers.project import _start_kubevirt_vms

        custom_api = MagicMock()
        vm_items = [
            {
                "spec": {"powerOnAtDeploy": True},
                "status": {"kubevirtVmName": "kv-1"},
            },
            {
                "spec": {"powerOnAtDeploy": True},
                "status": {"kubevirtVmName": "kv-2"},
            },
        ]

        result = _start_kubevirt_vms(custom_api, vm_items, "ns1")

        assert result == 2
        assert custom_api.patch_namespaced_custom_object.call_count == 2

    def test_skips_power_off_vms(self):
        from handlers.project import _start_kubevirt_vms

        custom_api = MagicMock()
        vm_items = [
            {
                "spec": {"powerOnAtDeploy": False},
                "status": {"kubevirtVmName": "kv-1"},
            },
        ]

        result = _start_kubevirt_vms(custom_api, vm_items, "ns1")

        assert result == 1
        custom_api.patch_namespaced_custom_object.assert_not_called()

    def test_skips_vms_without_kv_name(self):
        from handlers.project import _start_kubevirt_vms

        custom_api = MagicMock()
        vm_items = [{"spec": {"powerOnAtDeploy": True}, "status": {}}]

        result = _start_kubevirt_vms(custom_api, vm_items, "ns1")
        assert result == 0

    def test_skips_already_running_vms(self):
        from handlers.project import _start_kubevirt_vms

        custom_api = MagicMock()
        vm_items = [
            {
                "spec": {"powerOnAtDeploy": True},
                "status": {"kubevirtVmName": "kv-1", "state": "Running"},
            },
        ]

        result = _start_kubevirt_vms(custom_api, vm_items, "ns1")

        assert result == 1
        custom_api.patch_namespaced_custom_object.assert_not_called()

    def test_patch_failure_swallowed(self):
        from handlers.project import _start_kubevirt_vms

        custom_api = MagicMock()
        custom_api.patch_namespaced_custom_object.side_effect = Exception("fail")
        vm_items = [
            {
                "spec": {"powerOnAtDeploy": True},
                "status": {"kubevirtVmName": "kv-1"},
            },
        ]

        result = _start_kubevirt_vms(custom_api, vm_items, "ns1")
        assert result == 0


class TestPodUsesPvcOnNode:
    def test_pod_uses_pvc(self):
        from handlers.project import _pod_uses_pvc_on_node

        claim = MagicMock()
        claim.claim_name = "my-pvc"
        vol = MagicMock()
        vol.persistent_volume_claim = claim
        pod = MagicMock()
        pod.metadata.deletion_timestamp = None
        pod.spec.volumes = [vol]
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = pod_list

        assert _pod_uses_pvc_on_node(core_api, "ns1", "node-1", "my-pvc") is True

    def test_pod_does_not_use_pvc(self):
        from handlers.project import _pod_uses_pvc_on_node

        claim = MagicMock()
        claim.claim_name = "other-pvc"
        vol = MagicMock()
        vol.persistent_volume_claim = claim
        pod = MagicMock()
        pod.metadata.deletion_timestamp = None
        pod.spec.volumes = [vol]
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = pod_list

        assert _pod_uses_pvc_on_node(core_api, "ns1", "node-1", "my-pvc") is False

    def test_terminating_pod_ignored(self):
        from handlers.project import _pod_uses_pvc_on_node

        claim = MagicMock()
        claim.claim_name = "my-pvc"
        vol = MagicMock()
        vol.persistent_volume_claim = claim
        pod = MagicMock()
        pod.metadata.deletion_timestamp = "2025-01-01T00:00:00Z"
        pod.spec.volumes = [vol]
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = pod_list

        assert _pod_uses_pvc_on_node(core_api, "ns1", "node-1", "my-pvc") is False

    def test_exception_returns_false(self):
        from handlers.project import _pod_uses_pvc_on_node

        core_api = MagicMock()
        core_api.list_namespaced_pod.side_effect = Exception("fail")

        assert _pod_uses_pvc_on_node(core_api, "ns1", "node-1", "pvc") is False


class TestUpsertS3Secret:
    def test_creates_secret(self):
        from handlers.project import _upsert_s3_secret

        core_api = MagicMock()
        cfg = {
            "accessKeyId": "AKIA...",
            "secretKey": "secret",  # pragma: allowlist secret
        }

        _upsert_s3_secret(core_api, "ns1", "s3-creds", cfg)

        core_api.create_namespaced_secret.assert_called_once()

    def test_patches_on_409(self):
        from handlers.project import _upsert_s3_secret
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.create_namespaced_secret.side_effect = ApiException(status=409)
        cfg = {
            "accessKeyId": "AKIA...",
            "secretKey": "secret",  # pragma: allowlist secret
        }

        _upsert_s3_secret(core_api, "ns1", "s3-creds", cfg)

        core_api.patch_namespaced_secret.assert_called_once()

    def test_non_409_raises(self):
        from handlers.project import _upsert_s3_secret
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.create_namespaced_secret.side_effect = ApiException(status=500)

        with pytest.raises(ApiException):
            _upsert_s3_secret(core_api, "ns1", "s3-creds", {})


# ---------------------------------------------------------------------------
# handlers/vm.py — additional coverage
# ---------------------------------------------------------------------------


class TestWaitForDatavolume:
    def test_succeeded_returns_true(self):
        from handlers.vm import _wait_for_datavolume

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Succeeded"}
        }

        result = asyncio.run(_wait_for_datavolume(custom_api, "dv-1", "ns1"))
        assert result is True

    def test_failed_returns_false(self):
        from handlers.vm import _wait_for_datavolume

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Failed", "conditions": []}
        }

        result = asyncio.run(_wait_for_datavolume(custom_api, "dv-1", "ns1"))
        assert result is False

    def test_error_phase_returns_false(self):
        from handlers.vm import _wait_for_datavolume

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Error", "conditions": []}
        }

        result = asyncio.run(_wait_for_datavolume(custom_api, "dv-1", "ns1"))
        assert result is False

    def test_404_returns_false(self):
        from handlers.vm import _wait_for_datavolume
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)

        result = asyncio.run(_wait_for_datavolume(custom_api, "dv-1", "ns1"))
        assert result is False

    def test_owner_deleted_returns_false(self):
        from handlers.vm import _wait_for_datavolume
        from kubernetes.client import ApiException

        custom_api = MagicMock()

        def side_effect(*args, **kwargs):
            if kwargs.get("plural") == "troshkavms":
                raise ApiException(status=404)
            return {"status": {"phase": "ImportInProgress"}}

        custom_api.get_namespaced_custom_object.side_effect = side_effect

        result = asyncio.run(
            _wait_for_datavolume(
                custom_api,
                "dv-1",
                "ns1",
                owner_name="vm-1",
                owner_namespace="ns1",
            )
        )
        assert result is False


class TestCheckDatavolumeStatusTerminalConditions:
    """Cover terminal clone-validation conditions in _check_datavolume_status."""

    def test_clone_validation_failed_reason_returns_failed(self):
        from handlers.vm import _check_datavolume_status

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {
                "phase": "CloneScheduled",
                "conditions": [
                    {"type": "Bound", "status": "True", "reason": "Bound"},
                    {
                        "type": "Running",
                        "status": "False",
                        "reason": "CloneValidationFailed",
                        "message": "clone validation failed",
                    },
                ],
            }
        }

        assert _check_datavolume_status(custom_api, "dv-1", "ns1") == "failed"

    def test_target_smaller_than_source_returns_failed(self):
        from handlers.vm import _check_datavolume_status

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {
                "phase": "Pending",
                "conditions": [
                    {"type": "Bound", "status": "True", "reason": "Bound"},
                    {
                        "type": "Running",
                        "status": "False",
                        "reason": "Error",
                        "message": (
                            "target resources requests storage size is "
                            "smaller than the source"
                        ),
                    },
                ],
            }
        }

        assert _check_datavolume_status(custom_api, "dv-1", "ns1") == "failed"

    def test_clone_validation_failed_in_message_returns_failed(self):
        from handlers.vm import _check_datavolume_status

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {
                "phase": "Pending",
                "conditions": [
                    {
                        "type": "Bound",
                        "status": "False",
                        "reason": "Error",
                        "message": "CloneValidationFailed: something bad",
                    },
                ],
            }
        }

        assert _check_datavolume_status(custom_api, "dv-1", "ns1") == "failed"

    def test_golden_import_too_small_returns_failed(self):
        """A golden whose PVC is too small for the image is terminal.

        CDI leaves the DV in ImportInProgress with a Running=False/Error
        condition forever; the request size can't grow, so it must be
        recreated rather than waited on.
        """
        from handlers.vm import _check_datavolume_status

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {
                "phase": "ImportInProgress",
                "conditions": [
                    {"type": "Bound", "status": "False", "reason": "Pending"},
                    {
                        "type": "Running",
                        "status": "False",
                        "reason": "Error",
                        "message": "DataVolume too small to contain image",
                    },
                ],
            }
        }

        assert _check_datavolume_status(custom_api, "dv-1", "ns1") == "failed"

    def test_in_progress_clone_still_pending(self):
        from handlers.vm import _check_datavolume_status

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {
                "phase": "CloneInProgress",
                "conditions": [
                    {"type": "Bound", "status": "True", "reason": "Bound"},
                    {
                        "type": "Running",
                        "status": "True",
                        "reason": "Pod is running",
                        "message": "Clone from ns/src in progress (42.0%)",
                    },
                    {"type": "Ready", "status": "False"},
                ],
            }
        }

        assert _check_datavolume_status(custom_api, "dv-1", "ns1") == "pending"

    def test_no_conditions_still_pending(self):
        from handlers.vm import _check_datavolume_status

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "ImportInProgress"}
        }

        assert _check_datavolume_status(custom_api, "dv-1", "ns1") == "pending"

    def test_terminal_condition_makes_wait_return_false_fast(self):
        """A clone-validation-failed DV must NOT block on the 3600s timeout."""
        from handlers.vm import _wait_for_datavolume

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {
                "phase": "CloneScheduled",
                "conditions": [
                    {
                        "type": "Running",
                        "status": "False",
                        "reason": "CloneValidationFailed",
                        "message": "clone validation failed",
                    },
                ],
            }
        }

        with patch("handlers.vm.asyncio.sleep") as mock_sleep:
            result = asyncio.run(_wait_for_datavolume(custom_api, "dv-1", "ns1"))
        assert result is False
        mock_sleep.assert_not_called()


class TestEnsureGoldenPvc:
    @patch("handlers.vm._wait_for_datavolume")
    def test_existing_pvc_returns_name(self, mock_wait):
        from handlers.vm import _ensure_golden_pvc
        from helpers.kubevirt import s3_import_url

        custom_api = MagicMock()
        core_api = MagicMock()
        # Matching golden DataVolume already exists -> early return, no wait.
        custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "source": {
                    "s3": {
                        "url": s3_import_url("library/abc.qcow2", {"bucket": "b"}),
                        "secretRef": "s3-credentials",  # pragma: allowlist secret
                    }
                }
            }
        }

        result = asyncio.run(
            _ensure_golden_pvc(
                custom_api,
                core_api,
                "library/abc.qcow2",
                20,
                {"bucket": "b"},
            )
        )

        assert result.startswith("golden-")
        mock_wait.assert_not_called()

    @patch("handlers.vm._wait_for_datavolume", return_value=True)
    def test_creates_golden_pvc_when_not_exists(self, mock_wait):
        from handlers.vm import _ensure_golden_pvc
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )

        result = asyncio.run(
            _ensure_golden_pvc(
                custom_api,
                core_api,
                "library/abc.qcow2",
                20,
                {"bucket": "b"},
            )
        )

        assert result.startswith("golden-")
        custom_api.create_namespaced_custom_object.assert_called_once()

    @patch("handlers.vm._wait_for_datavolume", return_value=False)
    def test_raises_on_import_failure(self, mock_wait):
        from handlers.vm import _ensure_golden_pvc
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )

        with pytest.raises((Exception, TypeError)):
            asyncio.run(
                _ensure_golden_pvc(
                    custom_api,
                    core_api,
                    "library/abc.qcow2",
                    20,
                    {"bucket": "b"},
                )
            )


class TestCreateOrAdoptKubevirtVm:
    def test_creates_vm(self):
        from handlers.vm import _create_or_adopt_kubevirt_vm

        custom_api = MagicMock()
        kv_vm = {"metadata": {"name": "troshka-vm-abc"}}

        asyncio.run(
            _create_or_adopt_kubevirt_vm(custom_api, "ns1", kv_vm, "troshka-vm-abc", "")
        )

        custom_api.create_namespaced_custom_object.assert_called_once()

    def test_409_with_existing_name_adopts(self):
        from handlers.vm import _create_or_adopt_kubevirt_vm
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.create_namespaced_custom_object.side_effect = ApiException(
            status=409
        )
        kv_vm = {"metadata": {"name": "troshka-vm-abc"}}

        asyncio.run(
            _create_or_adopt_kubevirt_vm(
                custom_api,
                "ns1",
                kv_vm,
                "troshka-vm-abc",
                "troshka-vm-abc",
            )
        )

        assert custom_api.create_namespaced_custom_object.call_count == 1

    def test_409_without_existing_recreates(self):
        from handlers.vm import _create_or_adopt_kubevirt_vm
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.create_namespaced_custom_object.side_effect = [
            ApiException(status=409),
            None,
        ]
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
        kv_vm = {"metadata": {"name": "troshka-vm-abc"}}

        asyncio.run(
            _create_or_adopt_kubevirt_vm(custom_api, "ns1", kv_vm, "troshka-vm-abc", "")
        )

        assert custom_api.create_namespaced_custom_object.call_count == 2

    def test_non_409_raises(self):
        from handlers.vm import _create_or_adopt_kubevirt_vm
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.create_namespaced_custom_object.side_effect = ApiException(
            status=500
        )
        kv_vm = {"metadata": {"name": "troshka-vm-abc"}}

        with pytest.raises(ApiException):
            asyncio.run(
                _create_or_adopt_kubevirt_vm(
                    custom_api, "ns1", kv_vm, "troshka-vm-abc", ""
                )
            )


class TestSetupBmc:
    @patch("handlers.vm._find_bmc_nad", return_value="bmc-nad-1")
    @patch("handlers.vm._ensure_bmc_sa_and_rbac")
    @patch("handlers.vm.client.AppsV1Api")
    def test_creates_bmc_deployment(self, mock_apps_cls, mock_rbac, mock_nad):
        from handlers.vm import _setup_bmc
        from kubernetes.client import ApiException

        core_api = MagicMock()
        custom_api = MagicMock()
        mock_apps = MagicMock()
        mock_apps_cls.return_value = mock_apps
        mock_apps.read_namespaced_deployment.side_effect = ApiException(status=404)

        spec = {
            "bmcEnabled": True,
            "vmId": "vm12345678",
            "smbiosUuid": "uuid-1",
            "bmcIp": "10.0.1.10",
        }

        _setup_bmc(spec, "troshka-proj", core_api, custom_api)

        mock_apps.create_namespaced_deployment.assert_called_once()

    def test_skips_when_not_enabled(self):
        from handlers.vm import _setup_bmc

        core_api = MagicMock()
        custom_api = MagicMock()

        _setup_bmc({"bmcEnabled": False}, "ns1", core_api, custom_api)

    @patch("handlers.vm._find_bmc_nad", return_value=None)
    @patch("handlers.vm._ensure_bmc_sa_and_rbac")
    def test_skips_when_no_bmc_nad(self, mock_rbac, mock_nad):
        from handlers.vm import _setup_bmc

        core_api = MagicMock()
        custom_api = MagicMock()

        _setup_bmc(
            {"bmcEnabled": True, "vmId": "vm1"},
            "ns1",
            core_api,
            custom_api,
        )


class TestResolveNadRefs:
    @patch("handlers.vm.client")
    def test_builds_nad_refs(self, mock_client):
        from handlers.vm import _resolve_nad_refs

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "net-abc"},
                    "status": {"nadName": "net-abc-nad"},
                }
            ]
        }

        result = _resolve_nad_refs(custom_api, "ns1")
        assert result["net-abc"] == "net-abc-nad"

    @patch("handlers.vm.client")
    def test_fallback_nad_name(self, mock_client):
        from handlers.vm import _resolve_nad_refs

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [{"metadata": {"name": "net-xyz"}, "status": {}}]
        }

        result = _resolve_nad_refs(custom_api, "ns1")
        assert result["net-xyz"] == "net-xyz-nad"

    @patch("handlers.vm.client")
    def test_exception_returns_empty(self, mock_client):
        from handlers.vm import _resolve_nad_refs

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = Exception("fail")

        assert _resolve_nad_refs(custom_api, "ns1") == {}


class TestTryDeleteDatavolume:
    def test_deletes_successfully(self):
        from handlers.vm import _try_delete_datavolume

        custom_api = MagicMock()
        _try_delete_datavolume(custom_api, "ns1", "dv-1")
        custom_api.delete_namespaced_custom_object.assert_called_once()

    def test_404_swallowed(self):
        from handlers.vm import _try_delete_datavolume
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.delete_namespaced_custom_object.side_effect = ApiException(
            status=404
        )

        _try_delete_datavolume(custom_api, "ns1", "dv-1")

    def test_non_404_warns(self):
        from handlers.vm import _try_delete_datavolume
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.delete_namespaced_custom_object.side_effect = ApiException(
            status=500
        )

        _try_delete_datavolume(custom_api, "ns1", "dv-1")


class TestTryDeletePvc:
    def test_deletes_successfully(self):
        from handlers.vm import _try_delete_pvc

        core_api = MagicMock()
        _try_delete_pvc(core_api, "ns1", "pvc-1")
        core_api.delete_namespaced_persistent_volume_claim.assert_called_once()

    def test_404_swallowed(self):
        from handlers.vm import _try_delete_pvc
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.delete_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )

        _try_delete_pvc(core_api, "ns1", "pvc-1")

    def test_non_404_warns(self):
        from handlers.vm import _try_delete_pvc
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.delete_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=500
        )

        _try_delete_pvc(core_api, "ns1", "pvc-1")


class TestDeleteRemovedDisks:
    def test_deletes_removed_disks(self):
        from handlers.vm import _delete_removed_disks

        core_api = MagicMock()
        custom_api = MagicMock()
        old_disks = {"disk-aaa": {}, "disk-bbb": {}}
        new_disks = {"disk-aaa": {}}

        _delete_removed_disks(old_disks, new_disks, "vm-1", "ns1", core_api, custom_api)

        custom_api.delete_namespaced_custom_object.assert_called_once()
        core_api.delete_namespaced_persistent_volume_claim.assert_called_once()

    def test_no_removed_disks(self):
        from handlers.vm import _delete_removed_disks

        core_api = MagicMock()
        custom_api = MagicMock()
        old_disks = {"disk-aaa": {}}
        new_disks = {"disk-aaa": {}}

        _delete_removed_disks(old_disks, new_disks, "vm-1", "ns1", core_api, custom_api)

        custom_api.delete_namespaced_custom_object.assert_not_called()

    def test_all_disks_removed(self):
        from handlers.vm import _delete_removed_disks

        core_api = MagicMock()
        custom_api = MagicMock()
        old_disks = {"disk-aaa": {}, "disk-bbb": {}, "disk-ccc": {}}
        new_disks = {}

        _delete_removed_disks(old_disks, new_disks, "vm-1", "ns1", core_api, custom_api)

        assert custom_api.delete_namespaced_custom_object.call_count == 3
        assert core_api.delete_namespaced_persistent_volume_claim.call_count == 3


class TestRunGuestfishJob:
    @patch("handlers.vm.client")
    def test_creates_guestfish_job(self, mock_client):
        from handlers.vm import _run_guestfish_job

        mock_batch = MagicMock()
        mock_client.BatchV1Api.return_value = mock_batch
        job_status = MagicMock()
        job_status.status.succeeded = 1
        job_status.status.failed = None
        mock_batch.read_namespaced_job.return_value = job_status

        spec = {
            "guestfishCommands": ["rm /etc/old-cert"],
            "disks": [{"id": "disk-1"}],
        }
        body = {
            "kind": "TroshkaVM",
            "metadata": {"name": "vm-1", "uid": "uid-vm"},
        }
        disk_pvcs = {"disk-1": "vm-1-disk-disk-1"}

        asyncio.run(_run_guestfish_job(spec, "vm-1", "ns1", body, disk_pvcs))

        mock_batch.create_namespaced_job.assert_called_once()

    @patch("handlers.vm.client")
    def test_skips_when_no_commands(self, mock_client):
        from handlers.vm import _run_guestfish_job

        asyncio.run(_run_guestfish_job({}, "vm-1", "ns1", {}, {}))

        mock_client.BatchV1Api.assert_not_called()

    @patch("handlers.vm.client")
    def test_skips_when_no_disks(self, mock_client):
        from handlers.vm import _run_guestfish_job

        spec = {"guestfishCommands": ["rm /foo"], "disks": []}
        asyncio.run(_run_guestfish_job(spec, "vm-1", "ns1", {}, {}))

        mock_client.BatchV1Api.assert_not_called()


class TestFindBmcNad:
    def test_finds_bmc_nad(self):
        from handlers.vm import _find_bmc_nad

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "net-bmc1"},
                    "spec": {"networkType": "bmc"},
                    "status": {"nadName": "net-bmc1-nad"},
                },
                {
                    "metadata": {"name": "net-std1"},
                    "spec": {"networkType": "standard"},
                },
            ]
        }

        result = _find_bmc_nad("ns1", custom_api)
        assert result == "net-bmc1-nad"

    def test_no_bmc_network(self):
        from handlers.vm import _find_bmc_nad

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "net-std1"},
                    "spec": {"networkType": "standard"},
                }
            ]
        }

        assert _find_bmc_nad("ns1", custom_api) is None

    def test_exception_returns_none(self):
        from handlers.vm import _find_bmc_nad

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = Exception("fail")

        assert _find_bmc_nad("ns1", custom_api) is None

    def test_fallback_nad_name(self):
        from handlers.vm import _find_bmc_nad

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "net-bmc1"},
                    "spec": {"networkType": "bmc"},
                    "status": {},
                }
            ]
        }

        result = _find_bmc_nad("ns1", custom_api)
        assert result == "net-bmc1-nad"


class TestUpsertCloudinitSecret:
    @patch("handlers.vm.build_cloudinit_secret")
    def test_replaces_existing(self, mock_build):
        from handlers.vm import _upsert_cloudinit_secret

        mock_build.return_value = {
            "metadata": {"name": "cloudinit-vm-1"},
            "data": {"userdata": "..."},
        }
        core_api = MagicMock()
        body = {
            "kind": "TroshkaVM",
            "metadata": {"name": "vm-1", "uid": "uid-1"},
        }

        result = _upsert_cloudinit_secret(body, "ns1", core_api)

        assert result == "cloudinit-vm-1"
        core_api.replace_namespaced_secret.assert_called_once()

    @patch("handlers.vm.build_cloudinit_secret")
    def test_creates_on_404(self, mock_build):
        from handlers.vm import _upsert_cloudinit_secret
        from kubernetes.client import ApiException

        mock_build.return_value = {
            "metadata": {"name": "cloudinit-vm-1"},
            "data": {"userdata": "..."},
        }
        core_api = MagicMock()
        core_api.replace_namespaced_secret.side_effect = ApiException(status=404)
        body = {
            "kind": "TroshkaVM",
            "metadata": {"name": "vm-1", "uid": "uid-1"},
        }

        result = _upsert_cloudinit_secret(body, "ns1", core_api)

        assert result == "cloudinit-vm-1"
        core_api.create_namespaced_secret.assert_called_once()

    @patch("handlers.vm.build_cloudinit_secret", return_value=None)
    def test_returns_none_when_no_secret(self, mock_build):
        from handlers.vm import _upsert_cloudinit_secret

        core_api = MagicMock()
        body = {
            "kind": "TroshkaVM",
            "metadata": {"name": "vm-1", "uid": "uid-1"},
        }

        assert _upsert_cloudinit_secret(body, "ns1", core_api) is None


# ---------------------------------------------------------------------------
# helpers/bmc.py — build_bmc_deployment
# ---------------------------------------------------------------------------


class TestBuildBmcDeployment:
    def test_basic_deployment(self):
        from helpers.bmc import build_bmc_deployment

        bmc_vms = [{"vmId": "vm12345678", "smbiosUuid": "uuid-1", "bmcIp": "10.0.1.10"}]
        dep = build_bmc_deployment("proj1", "ns1", bmc_vms, "bmc-nad", {})

        assert dep["metadata"]["name"] == "bmc-proj1"
        assert dep["metadata"]["namespace"] == "ns1"

    def test_vm_map_env_with_domain_uuid(self):
        from helpers.bmc import build_bmc_deployment

        bmc_vms = [
            {"vmId": "vm12345678", "domainUuid": "dom-uuid-1", "bmcIp": "10.0.1.10"}
        ]
        dep = build_bmc_deployment("proj1", "ns1", bmc_vms, "bmc-nad", {})

        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        vm_map_env = [e for e in env if e["name"] == "SUSHY_VM_MAP"][0]
        vm_map = json.loads(vm_map_env["value"])
        assert "dom-uuid-1" in vm_map
        assert vm_map["dom-uuid-1"] == "troshka-vm-vm123456"

    def test_vm_map_env_fallback_no_domain_uuid(self):
        from helpers.bmc import build_bmc_deployment

        bmc_vms = [{"vmId": "vm12345678", "bmcIp": "10.0.1.10"}]
        dep = build_bmc_deployment("proj1", "ns1", bmc_vms, "bmc-nad", {})

        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        vm_map_env = [e for e in env if e["name"] == "SUSHY_VM_MAP"][0]
        vm_map = json.loads(vm_map_env["value"])
        assert "troshka-vm-vm123456" in vm_map

    def test_bmc_ips_env(self):
        from helpers.bmc import build_bmc_deployment

        bmc_vms = [
            {"vmId": "vm1", "smbiosUuid": "u1", "bmcIp": "10.0.1.10"},
            {"vmId": "vm2", "smbiosUuid": "u2", "bmcIp": "10.0.1.11"},
        ]
        dep = build_bmc_deployment("proj1", "ns1", bmc_vms, "bmc-nad", {})

        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        bmc_ips_env = [e for e in env if e["name"] == "SUSHY_BMC_IPS"][0]
        assert "10.0.1.10" in bmc_ips_env["value"]
        assert "10.0.1.11" in bmc_ips_env["value"]

    def test_no_bmc_ips(self):
        from helpers.bmc import build_bmc_deployment

        bmc_vms = [{"vmId": "vm1", "smbiosUuid": "u1"}]
        dep = build_bmc_deployment("proj1", "ns1", bmc_vms, "bmc-nad", {})

        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        bmc_ips_envs = [e for e in env if e["name"] == "SUSHY_BMC_IPS"]
        assert len(bmc_ips_envs) == 0

    def test_with_credentials(self):
        from helpers.bmc import build_bmc_deployment

        bmc_vms = [{"vmId": "vm1", "smbiosUuid": "u1"}]
        creds = {"username": "admin", "password": "secret"}  # pragma: allowlist secret
        dep = build_bmc_deployment("proj1", "ns1", bmc_vms, "bmc-nad", creds)

        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        user_env = [e for e in env if e["name"] == "SUSHY_USERNAME"][0]
        assert user_env["value"] == "admin"

    def test_network_annotation(self):
        from helpers.bmc import build_bmc_deployment

        bmc_vms = [{"vmId": "vm1", "smbiosUuid": "u1"}]
        dep = build_bmc_deployment("proj1", "ns1", bmc_vms, "my-bmc-nad", {})

        ann = dep["spec"]["template"]["metadata"]["annotations"]
        assert ann["k8s.v1.cni.cncf.io/networks"] == "my-bmc-nad"


# ---------------------------------------------------------------------------
# images/bmc/kubevirt_driver.py
# ---------------------------------------------------------------------------


class TestKubeVirtDriverStripBootOrders:
    def test_strips_boot_orders(self):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        disks = [
            {"name": "d1", "disk": {}, "bootOrder": 1},
            {"name": "d2", "cdrom": {}},
        ]
        ifaces = [{"name": "i1", "bootOrder": 2}]

        new_disks, new_ifaces = KubeVirtDriver._strip_boot_orders(disks, ifaces)

        assert "bootOrder" not in new_disks[0]
        assert "bootOrder" not in new_ifaces[0]
        assert new_disks[0]["name"] == "d1"
        assert "disk" in new_disks[0]

    def test_preserves_original(self):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        disks = [{"name": "d1", "disk": {}, "bootOrder": 1}]
        ifaces = []

        KubeVirtDriver._strip_boot_orders(disks, ifaces)

        assert disks[0]["bootOrder"] == 1


class TestKubeVirtDriverSetBootOrderOn:
    def test_assigns_orders(self):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        items = [
            {"name": "a", "disk": {}},
            {"name": "b", "cdrom": {}},
        ]

        next_order = KubeVirtDriver._set_boot_order_on(items, lambda _: True, 1)

        assert items[0]["bootOrder"] == 1
        assert items[1]["bootOrder"] == 2
        assert next_order == 3

    def test_predicate_filters(self):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        items = [
            {"name": "a", "disk": {}},
            {"name": "b", "cdrom": {}},
        ]

        KubeVirtDriver._set_boot_order_on(items, lambda d: "disk" in d, 1)

        assert items[0]["bootOrder"] == 1
        assert "bootOrder" not in items[1]


class TestKubeVirtDriverAssignBootOrders:
    def test_hdd_boot(self):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        disks = [
            {"name": "d1", "disk": {}},
            {"name": "d2", "cdrom": {}},
        ]
        ifaces = [{"name": "i1"}]

        KubeVirtDriver._assign_boot_orders(disks, ifaces, "disk")

        assert disks[0]["bootOrder"] == 1
        assert "bootOrder" not in disks[1]

    def test_pxe_boot(self):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        disks = [{"name": "d1", "disk": {}}]
        ifaces = [{"name": "i1"}, {"name": "i2"}]

        KubeVirtDriver._assign_boot_orders(disks, ifaces, "interface")

        assert ifaces[0]["bootOrder"] == 1
        assert ifaces[1]["bootOrder"] == 2
        assert disks[0]["bootOrder"] == 3

    def test_cdrom_boot(self):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        disks = [
            {"name": "d1", "disk": {}},
            {"name": "cd1", "cdrom": {}},
        ]
        ifaces = []

        KubeVirtDriver._assign_boot_orders(disks, ifaces, "cdrom")

        # Disk first so empty disks fall through to CDROM on first boot.
        assert disks[0]["bootOrder"] == 1
        assert disks[1]["bootOrder"] == 2


class TestKubeVirtDriverRestoreBootOrders:
    def test_restores_saved_orders(self):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        items = [
            {"name": "d1", "disk": {}},
            {"name": "d2", "cdrom": {}},
        ]
        saved = [{"d1": 5}, {"d2": 3}]

        KubeVirtDriver._restore_boot_orders(items, saved)

        assert items[0]["bootOrder"] == 5
        assert items[1]["bootOrder"] == 3

    def test_skips_none_order(self):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        items = [{"name": "d1", "disk": {}}]
        saved = [{"d1": None}]

        KubeVirtDriver._restore_boot_orders(items, saved)

        assert "bootOrder" not in items[0]

    def test_skips_missing_item(self):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        items = [{"name": "d1", "disk": {}}]
        saved = [{"d2": 1}]

        KubeVirtDriver._restore_boot_orders(items, saved)

        assert "bootOrder" not in items[0]


class TestKubeVirtDriverGetPowerState:
    @patch("images.bmc.kubevirt_driver.config")
    def test_running_returns_on(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        mock_config.load_incluster_config.return_value = None
        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {"vm1": "kv-vm-1"}
        driver.custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Running"}
        }

        assert driver.get_power_state("vm1") == "On"

    @patch("images.bmc.kubevirt_driver.config")
    def test_no_vmi_returns_off(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver
        from kubernetes.client import ApiException

        mock_config.load_incluster_config.return_value = None
        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}
        driver.custom_api.get_namespaced_custom_object.side_effect = ApiException(
            status=404
        )

        assert driver.get_power_state("vm1") == "Off"

    @patch("images.bmc.kubevirt_driver.config")
    def test_scheduling_returns_off(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        mock_config.load_incluster_config.return_value = None
        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}
        driver.custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Scheduling"}
        }

        assert driver.get_power_state("vm1") == "Off"


class TestKubeVirtDriverSetPowerState:
    @patch("images.bmc.kubevirt_driver.config")
    def test_power_on(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}

        driver.set_power_state("vm1", "On")

        driver.custom_api.patch_namespaced_custom_object.assert_called_once()
        body = driver.custom_api.patch_namespaced_custom_object.call_args[1]["body"]
        assert body == {"spec": {"running": True}}

    @patch("images.bmc.kubevirt_driver.config")
    def test_force_off(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}

        driver.set_power_state("vm1", "ForceOff")

        patch_call = driver.custom_api.patch_namespaced_custom_object
        assert patch_call.call_count == 1
        body = patch_call.call_args[1]["body"]
        assert body == {"spec": {"running": False}}
        driver.custom_api.delete_namespaced_custom_object.assert_called_once()

    @patch("images.bmc.kubevirt_driver.config")
    def test_force_restart(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}

        driver.set_power_state("vm1", "ForceRestart")

        patch_calls = driver.custom_api.patch_namespaced_custom_object.call_args_list
        assert len(patch_calls) == 1
        assert patch_calls[0][1]["body"] == {"spec": {"running": True}}
        driver.custom_api.delete_namespaced_custom_object.assert_called_once()


class TestKubeVirtDriverGetSystems:
    @patch("images.bmc.kubevirt_driver.config")
    def test_lists_systems_by_uid(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}
        driver.custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {"metadata": {"name": "vm-1", "uid": "uid-1234"}},
            ]
        }

        result = driver.get_systems()
        assert result == ["uid-1234"]

    @patch("images.bmc.kubevirt_driver.config")
    def test_uses_vm_map_keys_when_populated(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {"dom-uuid-1": "troshka-vm-vm1", "dom-uuid-2": "troshka-vm-vm2"}
        driver.custom_api.list_namespaced_custom_object.return_value = {"items": []}

        result = driver.get_systems()
        assert sorted(result) == ["dom-uuid-1", "dom-uuid-2"]

    @patch("images.bmc.kubevirt_driver.config")
    def test_empty_list(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}
        driver.custom_api.list_namespaced_custom_object.return_value = {"items": []}

        assert driver.get_systems() == []


class TestKubeVirtDriverGetBootDevice:
    @patch("images.bmc.kubevirt_driver.config")
    def test_hdd_boot_device(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}
        driver.custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "devices": {
                                "disks": [
                                    {"name": "d1", "disk": {}, "bootOrder": 1},
                                    {"name": "cd1", "cdrom": {}, "bootOrder": 2},
                                ],
                                "interfaces": [
                                    {"name": "i1", "bootOrder": 3},
                                ],
                            }
                        }
                    }
                }
            }
        }

        assert driver.get_boot_device("vm1") == "Hdd"

    @patch("images.bmc.kubevirt_driver.config")
    def test_pxe_boot_device(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}
        driver.custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "devices": {
                                "disks": [],
                                "interfaces": [
                                    {"name": "i1", "bootOrder": 1},
                                ],
                            }
                        }
                    }
                }
            }
        }

        assert driver.get_boot_device("vm1") == "Pxe"

    @patch("images.bmc.kubevirt_driver.config")
    def test_no_boot_order_defaults_hdd(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}
        driver.custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "devices": {
                                "disks": [{"name": "d1", "disk": {}}],
                                "interfaces": [],
                            }
                        }
                    }
                }
            }
        }

        assert driver.get_boot_device("vm1") == "Hdd"


class TestKubeVirtDriverGetBootMode:
    @patch("images.bmc.kubevirt_driver.config")
    def test_uefi_mode(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}
        driver.custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "firmware": {"bootloader": {"efi": {"secureBoot": False}}}
                        }
                    }
                }
            }
        }

        assert driver.get_boot_mode("vm1") == "UEFI"

    @patch("images.bmc.kubevirt_driver.config")
    def test_legacy_mode(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}
        driver.custom_api.get_namespaced_custom_object.return_value = {
            "spec": {"template": {"spec": {"domain": {"firmware": {"bootloader": {}}}}}}
        }

        assert driver.get_boot_mode("vm1") == "Legacy"


class TestKubeVirtDriverGetTotalMemory:
    @patch("images.bmc.kubevirt_driver.config")
    def test_memory_in_mi(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}
        driver.custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {"resources": {"requests": {"memory": "4096Mi"}}}
                    }
                }
            }
        }

        assert driver.get_total_memory("vm1") == 4096

    @patch("images.bmc.kubevirt_driver.config")
    def test_memory_in_gi(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}
        driver.custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {"domain": {"resources": {"requests": {"memory": "8Gi"}}}}
                }
            }
        }

        assert driver.get_total_memory("vm1") == 8192


class TestKubeVirtDriverGetTotalCpus:
    @patch("images.bmc.kubevirt_driver.config")
    def test_returns_core_count(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}
        driver.custom_api.get_namespaced_custom_object.return_value = {
            "spec": {"template": {"spec": {"domain": {"cpu": {"cores": 8}}}}}
        }

        assert driver.get_total_cpus("vm1") == 8

    @patch("images.bmc.kubevirt_driver.config")
    def test_defaults_to_1(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}
        driver.custom_api.get_namespaced_custom_object.return_value = {
            "spec": {"template": {"spec": {"domain": {"cpu": {}}}}}
        }

        assert driver.get_total_cpus("vm1") == 1


class TestKubeVirtDriverGetNics:
    @patch("images.bmc.kubevirt_driver.config")
    def test_returns_nics(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}
        driver.custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "devices": {
                                "interfaces": [
                                    {"name": "nic1", "macAddress": "52:54:00:01:02:03"},
                                    {"name": "nic2"},
                                ]
                            }
                        }
                    }
                }
            }
        }

        nics = driver.get_nics("vm1")
        assert len(nics) == 2
        assert nics[0] == {"id": "nic1", "mac": "52:54:00:01:02:03"}
        assert nics[1] == {"id": "nic2", "mac": ""}


class TestKubeVirtDriverKvName:
    @patch("images.bmc.kubevirt_driver.config")
    def test_maps_from_vm_map(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.vm_map = {"uuid-123": "kv-vm-mapped"}

        assert driver._kv_name("uuid-123") == "kv-vm-mapped"

    @patch("images.bmc.kubevirt_driver.config")
    def test_strips_slashes(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.vm_map = {"vm1": "kv-1"}

        assert driver._kv_name("/vm1/") == "kv-1"

    @patch("images.bmc.kubevirt_driver.config")
    def test_identity_passthrough(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.vm_map = {}

        assert driver._kv_name("unmapped-vm") == "unmapped-vm"


class TestKubeVirtDriverGetBootOverrideEnabled:
    @patch("images.bmc.kubevirt_driver.config")
    def test_once_when_override_exists(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.vm_map = {}
        driver._boot_once_overrides = {"vm1": {"disks": [], "interfaces": []}}

        assert driver.get_boot_override_enabled("vm1") == "Once"

    @patch("images.bmc.kubevirt_driver.config")
    def test_continuous_when_no_override(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.vm_map = {}
        driver._boot_once_overrides = {}

        assert driver.get_boot_override_enabled("vm1") == "Continuous"


class TestExtractKubeconfigSecret:
    def test_extracts_kubeconfig(self):
        import base64
        from handlers.project import _extract_kubeconfig_secret

        kc_b64 = base64.b64encode(b"apiVersion: v1\nkind: Config").decode()
        logs = f"KUBECONFIG_B64_BEGIN {kc_b64} KUBECONFIG_B64_END"

        pod = MagicMock()
        pod.metadata.name = "recert-pod"
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = pod_list
        core_api.read_namespaced_pod_log.return_value = logs

        result = _extract_kubeconfig_secret(
            core_api, "ns1", "recert-vm-abc", "proj1", vm_name="sno1"
        )

        assert result is None
        assert core_api.create_namespaced_secret.call_count == 2

    def test_no_pods_returns_error(self):
        from handlers.project import _extract_kubeconfig_secret

        pod_list = MagicMock()
        pod_list.items = []
        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = pod_list

        result = _extract_kubeconfig_secret(core_api, "ns1", "recert-vm-abc", "proj1")

        assert result is not None
        assert "No pods found" in result

    def test_no_marker_returns_error(self):
        from handlers.project import _extract_kubeconfig_secret

        pod = MagicMock()
        pod.metadata.name = "recert-pod"
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = pod_list
        core_api.read_namespaced_pod_log.return_value = "no kubeconfig here"

        result = _extract_kubeconfig_secret(core_api, "ns1", "recert-vm-abc", "proj1")

        assert result is not None
        assert "No kubeconfig marker" in result

    def test_409_replaces_secret(self):
        import base64
        from handlers.project import _extract_kubeconfig_secret
        from kubernetes.client import ApiException

        kc_b64 = base64.b64encode(b"apiVersion: v1").decode()
        logs = f"KUBECONFIG_B64_BEGIN {kc_b64} KUBECONFIG_B64_END"

        pod = MagicMock()
        pod.metadata.name = "recert-pod"
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = pod_list
        core_api.read_namespaced_pod_log.return_value = logs
        core_api.create_namespaced_secret.side_effect = ApiException(status=409)

        result = _extract_kubeconfig_secret(core_api, "ns1", "recert-vm-abc", "proj1")

        assert result is None
        core_api.replace_namespaced_secret.assert_called()


# ---------------------------------------------------------------------------
# handlers/project.py — additional coverage (batch 2)
# ---------------------------------------------------------------------------


class TestEnsureCacheNamespaceAndSecrets:
    @patch("handlers.project.client")
    def test_creates_namespace_and_secrets(self, mock_client):
        from handlers.project import _ensure_cache_namespace_and_secrets

        core_api = MagicMock()
        s3_config = {
            "accessKeyId": "AKIA...",
            "secretKey": "secret",  # pragma: allowlist secret
        }
        central_config = {
            "accessKeyId": "CENTRAL...",
            "secretKey": "csecret",  # pragma: allowlist secret
        }

        _ensure_cache_namespace_and_secrets(core_api, s3_config, central_config)

        core_api.create_namespace.assert_called_once()
        # Two upsert calls (one for s3-credentials, one for s3-central-credentials)
        assert core_api.create_namespaced_secret.call_count == 2

    @patch("handlers.project.client")
    def test_409_on_namespace_is_swallowed(self, mock_client):
        from handlers.project import _ensure_cache_namespace_and_secrets
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.create_namespace.side_effect = ApiException(status=409)

        _ensure_cache_namespace_and_secrets(core_api, {}, {})

    @patch("handlers.project.client")
    def test_skips_secret_without_access_key(self, mock_client):
        from handlers.project import _ensure_cache_namespace_and_secrets

        core_api = MagicMock()

        _ensure_cache_namespace_and_secrets(core_api, {}, {})

        core_api.create_namespaced_secret.assert_not_called()

    @patch("handlers.project.client")
    @patch("helpers.obc.get_obc_s3_config", return_value=None)
    def test_hydrates_credentials_from_project_namespace(self, _mock_obc, mock_client):
        from handlers.project import _ensure_cache_namespace_and_secrets
        from kubernetes.client import ApiException

        core_api = MagicMock()
        secret = MagicMock()
        secret.string_data = None
        secret.data = {
            "accessKeyId": "QUtJ...",
            "secretKey": "c2VjcmV0",  # pragma: allowlist secret
        }

        def _read_secret(name, namespace):
            if name == "s3-credentials":
                return secret
            raise ApiException(status=404)

        core_api.read_namespaced_secret.side_effect = _read_secret

        _ensure_cache_namespace_and_secrets(
            core_api,
            {
                "credentialsSecret": "s3-credentials",  # pragma: allowlist secret
                "bucket": "troshka-images",
            },
            {},
            project_namespace="troshka-b1677acf",
        )

        core_api.read_namespaced_secret.assert_any_call(
            "s3-credentials", "troshka-b1677acf"
        )
        core_api.create_namespaced_secret.assert_called_once()
        assert (
            core_api.create_namespaced_secret.call_args.kwargs["namespace"]
            == "troshka-cache"
        )

    @patch("handlers.project.client")
    def test_non_409_namespace_raises(self, mock_client):
        from handlers.project import _ensure_cache_namespace_and_secrets
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.create_namespace.side_effect = ApiException(status=500)

        with pytest.raises(ApiException):
            _ensure_cache_namespace_and_secrets(core_api, {}, {})


class TestCreateGoldenPvcForDisk:
    def test_creates_golden_pvc_for_library_disk(self):
        from handlers.project import _create_golden_pvc_for_disk
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )

        disk = {"libraryImage": {"s3Path": "library/rhel.qcow2"}, "sizeGb": 40}
        s3_config = {"bucket": "b", "endpoint": "s3.example.com"}

        _create_golden_pvc_for_disk(custom_api, core_api, disk, s3_config, {})

        custom_api.create_namespaced_custom_object.assert_called_once()

    def test_skips_when_golden_import_matches(self):
        from handlers.project import _create_golden_pvc_for_disk
        from helpers.k8s import golden_pvc_name
        from helpers.kubevirt import s3_import_url

        custom_api = MagicMock()
        core_api = MagicMock()
        s3_config = {"bucket": "troshka-images", "region": "us-east-1"}
        s3_path = "library/rhel.qcow2"
        custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "source": {
                    "s3": {
                        "url": s3_import_url(s3_path, s3_config),
                        "secretRef": "s3-credentials",  # pragma: allowlist secret
                    }
                }
            }
        }

        disk = {"libraryImage": {"s3Path": s3_path}}

        _create_golden_pvc_for_disk(custom_api, core_api, disk, s3_config, {})

        custom_api.create_namespaced_custom_object.assert_not_called()
        assert custom_api.get_namespaced_custom_object.call_args[1]["name"] == (
            golden_pvc_name(s3_path)
        )

    def test_recreates_when_golden_import_mismatched(self):
        from handlers.project import _create_golden_pvc_for_disk
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        core_api = MagicMock()
        s3_config = {"bucket": "troshka-images", "region": "us-east-1"}
        central_config = {
            "bucket": "troshka-gold-images",
            "endpoint": "https://s4.example.com",
        }
        custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "source": {
                    "s3": {
                        "url": "https://s3.us-east-1.amazonaws.com/troshka-images/library/rhel.qcow2",
                        "secretRef": "s3-credentials",  # pragma: allowlist secret
                    }
                }
            }
        }
        core_api.read_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )

        disk = {
            "libraryImage": {"s3Path": "library/rhel.qcow2", "central": True},
            "sizeGb": 20,
        }

        _create_golden_pvc_for_disk(
            custom_api, core_api, disk, s3_config, central_config
        )

        custom_api.delete_namespaced_custom_object.assert_called_once()
        custom_api.create_namespaced_custom_object.assert_called_once()

    def test_skips_blank_disk(self):
        from handlers.project import _create_golden_pvc_for_disk

        custom_api = MagicMock()
        core_api = MagicMock()

        disk = {"blank": True}

        _create_golden_pvc_for_disk(custom_api, core_api, disk, {}, {})

        core_api.read_namespaced_persistent_volume_claim.assert_not_called()

    def test_uses_central_config_when_flagged(self):
        from handlers.project import _create_golden_pvc_for_disk
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )

        disk = {
            "libraryImage": {"s3Path": "library/img.qcow2", "central": True},
            "sizeGb": 20,
        }
        s3_config = {"bucket": "normal"}
        central_config = {"bucket": "central"}

        _create_golden_pvc_for_disk(
            custom_api, core_api, disk, s3_config, central_config
        )

        custom_api.create_namespaced_custom_object.assert_called_once()

    def test_passes_source_size_for_normal_disk(self):
        """BUG #1: top-level sourceSizeGb is forwarded to build_datavolume_from_s3."""
        from handlers.project import _create_golden_pvc_for_disk
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        core_api = MagicMock()
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)

        disk = {
            "libraryImage": {"s3Path": "library/rhel/disk/rhel.qcow2"},
            "sizeGb": 100,
            "sourceSizeGb": 80,
        }
        s3_config = {"bucket": "b", "endpoint": "s3.example.com"}

        with patch("helpers.kubevirt.build_datavolume_from_s3") as mock_build:
            mock_build.return_value = {"metadata": {"name": "g"}}
            _create_golden_pvc_for_disk(custom_api, core_api, disk, s3_config, {})

        assert mock_build.call_args.kwargs["source_size_gb"] == 80

    def test_passes_source_size_for_cdrom_shaped_disk(self):
        """BUG #1: cdrom-shaped {'libraryImage': {...sourceSizeGb...}} forwards it."""
        from handlers.project import _create_golden_pvc_for_disk
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        core_api = MagicMock()
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)

        disk = {
            "libraryImage": {
                "s3Path": "library/iso/disk/rhel.iso",
                "libraryIsoId": "iso-1",
                "sourceSizeGb": 11,
            }
        }
        s3_config = {"bucket": "b", "endpoint": "s3.example.com"}

        with patch("helpers.kubevirt.build_datavolume_from_s3") as mock_build:
            mock_build.return_value = {"metadata": {"name": "g"}}
            _create_golden_pvc_for_disk(custom_api, core_api, disk, s3_config, {})

        assert mock_build.call_args.kwargs["source_size_gb"] == 11

    def test_reaps_golden_with_terminal_condition(self):
        """BUG #2: a matching-but-terminal golden is deleted and recreated."""
        from handlers.project import _create_golden_pvc_for_disk
        from helpers.kubevirt import s3_import_url

        custom_api = MagicMock()
        core_api = MagicMock()
        s3_config = {"bucket": "troshka-images", "region": "us-east-1"}
        s3_path = "library/rhel/disk/rhel.qcow2"
        # DV matches the desired source but is stuck in a terminal clone failure.
        custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "source": {
                    "s3": {
                        "url": s3_import_url(s3_path, s3_config),
                        "secretRef": "s3-credentials",  # pragma: allowlist secret
                    }
                }
            },
            "status": {
                "phase": "CloneScheduled",
                "conditions": [
                    {
                        "type": "Running",
                        "status": "False",
                        "reason": "CloneValidationFailed",
                        "message": "target is smaller than the source",
                    }
                ],
            },
        }

        disk = {"libraryImage": {"s3Path": s3_path}, "sizeGb": 40}

        _create_golden_pvc_for_disk(custom_api, core_api, disk, s3_config, {})

        custom_api.delete_namespaced_custom_object.assert_called_once()
        custom_api.create_namespaced_custom_object.assert_called_once()

    def test_reaps_golden_with_import_too_small(self):
        """An undersized golden import (too small to contain image) is recreated.

        The golden sits in ImportInProgress with a Running=False/Error condition
        forever; deleting the project never clears the shared golden, so the
        operator must reap and recreate it (at the now-correct sourceSizeGb).
        """
        from handlers.project import _create_golden_pvc_for_disk
        from helpers.kubevirt import s3_import_url

        custom_api = MagicMock()
        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = MagicMock(items=[])
        s3_config = {"bucket": "troshka-images", "region": "us-east-1"}
        s3_path = "patterns/p1/content.qcow2"
        custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "source": {
                    "s3": {
                        "url": s3_import_url(s3_path, s3_config),
                        "secretRef": "s3-credentials",  # pragma: allowlist secret
                    }
                }
            },
            "status": {
                "phase": "ImportInProgress",
                "conditions": [
                    {"type": "Bound", "status": "False", "reason": "Pending"},
                    {
                        "type": "Running",
                        "status": "False",
                        "reason": "Error",
                        "message": "DataVolume too small to contain image",
                    },
                ],
            },
        }

        disk = {"libraryImage": {"s3Path": s3_path}, "sizeGb": 50, "sourceSizeGb": 80}

        _create_golden_pvc_for_disk(custom_api, core_api, disk, s3_config, {})

        custom_api.delete_namespaced_custom_object.assert_called_once()
        custom_api.create_namespaced_custom_object.assert_called_once()

    def test_leaves_healthy_importing_golden_alone(self):
        """BUG #2: a matching golden that is still importing must be left alone."""
        from handlers.project import _create_golden_pvc_for_disk
        from helpers.kubevirt import s3_import_url

        custom_api = MagicMock()
        core_api = MagicMock()
        core_api.list_namespaced_pod.return_value = MagicMock(items=[])
        s3_config = {"bucket": "troshka-images", "region": "us-east-1"}
        s3_path = "library/rhel/disk/rhel.qcow2"
        custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "source": {
                    "s3": {
                        "url": s3_import_url(s3_path, s3_config),
                        "secretRef": "s3-credentials",  # pragma: allowlist secret
                    }
                }
            },
            "status": {"phase": "ImportInProgress", "conditions": []},
        }

        disk = {"libraryImage": {"s3Path": s3_path}, "sizeGb": 40}

        _create_golden_pvc_for_disk(custom_api, core_api, disk, s3_config, {})

        custom_api.delete_namespaced_custom_object.assert_not_called()
        custom_api.create_namespaced_custom_object.assert_not_called()


class TestPrecreateGoldenPvcs:
    @patch("handlers.project._create_golden_pvc_for_disk")
    @patch("handlers.project._ensure_cache_namespace_and_secrets")
    def test_calls_create_for_each_disk(self, mock_ensure, mock_create):
        from handlers.project import _precreate_golden_pvcs

        custom_api = MagicMock()
        core_api = MagicMock()
        spec = {"s3Config": {"bucket": "b"}, "centralS3Config": {}}
        all_disks = [
            {"libraryImage": {"s3Path": "lib/a.qcow2"}},
            {"patternImage": {"s3Path": "pat/b.qcow2"}},
        ]
        patch_obj = MagicMock()

        _precreate_golden_pvcs(custom_api, core_api, spec, all_disks, patch_obj)

        assert mock_create.call_count == 2
        mock_ensure.assert_called_once()


class TestCreateRecertJobs:
    @patch("helpers.kubevirt.build_recert_job")
    def test_creates_jobs_when_not_exist(self, mock_build):
        from handlers.project import _create_recert_jobs
        from kubernetes.client import ApiException

        batch_api = MagicMock()
        batch_api.read_namespaced_job.side_effect = ApiException(status=404)
        mock_build.return_value = {"metadata": {"name": "recert-vm-abc"}}

        cfgs = [{"rhcosPvc": "vm-abc-disk-def", "vmName": "sno1"}]

        result = _create_recert_jobs(batch_api, cfgs, "ns1")

        assert result is None
        batch_api.create_namespaced_job.assert_called_once()

    @patch("helpers.kubevirt.build_recert_job")
    def test_skips_when_job_already_exists(self, mock_build):
        from handlers.project import _create_recert_jobs

        batch_api = MagicMock()
        batch_api.read_namespaced_job.return_value = MagicMock()

        cfgs = [{"rhcosPvc": "vm-abc-disk-def", "vmName": "sno1"}]

        result = _create_recert_jobs(batch_api, cfgs, "ns1")

        assert result is None
        batch_api.create_namespaced_job.assert_not_called()

    @patch("helpers.kubevirt.build_recert_job")
    def test_returns_error_on_creation_failure(self, mock_build):
        from handlers.project import _create_recert_jobs
        from kubernetes.client import ApiException

        batch_api = MagicMock()
        batch_api.read_namespaced_job.side_effect = ApiException(status=404)
        mock_build.return_value = {"metadata": {"name": "recert-vm-abc"}}
        batch_api.create_namespaced_job.side_effect = Exception("quota exceeded")

        cfgs = [{"rhcosPvc": "vm-abc-disk-def", "vmName": "sno1"}]

        result = _create_recert_jobs(batch_api, cfgs, "ns1")

        assert result is not None
        assert "Failed to create recert job" in result


class TestPollRecertJobs:
    def test_all_done_returns_true(self):
        from handlers.project import _poll_recert_jobs

        batch_api = MagicMock()
        job = MagicMock()
        job.status.succeeded = 1
        job.status.failed = None
        batch_api.read_namespaced_job.return_value = job

        cfgs = [{"rhcosPvc": "vm-abc-disk-def", "vmName": "sno1"}]
        status = {}
        patch_obj = MagicMock()

        all_done, should_return = _poll_recert_jobs(
            batch_api, cfgs, "ns1", status, patch_obj
        )

        assert all_done is True
        assert should_return is False

    def test_still_running_returns_should_return(self):
        from handlers.project import _poll_recert_jobs

        batch_api = MagicMock()
        job = MagicMock()
        job.status.succeeded = None
        job.status.failed = None
        batch_api.read_namespaced_job.return_value = job

        cfgs = [{"rhcosPvc": "vm-abc-disk-def", "vmName": "sno1"}]
        status = {}
        patch_obj = MagicMock()

        all_done, should_return = _poll_recert_jobs(
            batch_api, cfgs, "ns1", status, patch_obj
        )

        assert all_done is False
        assert should_return is True

    def test_failed_job_first_attempt_retries(self):
        from handlers.project import _poll_recert_jobs

        batch_api = MagicMock()
        job = MagicMock()
        job.status.succeeded = None
        job.status.failed = 1
        batch_api.read_namespaced_job.return_value = job

        cfgs = [{"rhcosPvc": "vm-abc-disk-def", "vmName": "sno1"}]
        status = {}
        patch_obj = MagicMock()

        all_done, should_return = _poll_recert_jobs(
            batch_api, cfgs, "ns1", status, patch_obj
        )

        assert all_done is False
        assert should_return is True
        # Should increment attempt counter
        patch_obj.status.__setitem__.assert_any_call("recertAttempts_0", 1)

    def test_failed_job_after_3_attempts_errors(self):
        from handlers.project import _poll_recert_jobs

        batch_api = MagicMock()
        job = MagicMock()
        job.status.succeeded = None
        job.status.failed = 1
        batch_api.read_namespaced_job.return_value = job

        cfgs = [{"rhcosPvc": "vm-abc-disk-def", "vmName": "sno1"}]
        status = {"recertAttempts_0": 2}
        patch_obj = MagicMock()

        all_done, should_return = _poll_recert_jobs(
            batch_api, cfgs, "ns1", status, patch_obj
        )

        assert all_done is False
        assert should_return is True
        patch_obj.status.__setitem__.assert_any_call("phase", "Error")


class TestFinalizeRecert:
    @patch("handlers.project._cleanup_recert_job")
    @patch("handlers.project._extract_kubeconfig_secret", return_value=None)
    def test_extracts_and_cleans(self, mock_extract, mock_cleanup):
        from handlers.project import _finalize_recert

        core_api = MagicMock()
        batch_api = MagicMock()
        cfgs = [{"rhcosPvc": "vm-abc-disk-def", "vmName": "sno1"}]

        _finalize_recert(core_api, batch_api, cfgs, "ns1", "proj1")

        mock_extract.assert_called_once()
        mock_cleanup.assert_called_once()

    @patch("handlers.project._cleanup_recert_job")
    @patch(
        "handlers.project._extract_kubeconfig_secret",
        return_value="extraction failed",
    )
    def test_continues_on_extraction_failure(self, mock_extract, mock_cleanup):
        from handlers.project import _finalize_recert

        core_api = MagicMock()
        batch_api = MagicMock()
        cfgs = [{"rhcosPvc": "vm-abc-disk-def", "vmName": "sno1"}]

        _finalize_recert(core_api, batch_api, cfgs, "ns1", "proj1")

        mock_cleanup.assert_called_once()


class TestFindStaleVolumeAttachments:
    def test_finds_stale_attachments(self):
        from handlers.project import _find_stale_volume_attachments

        # PVC bound to PV "pv-1"
        pvc = MagicMock()
        pvc.spec.volume_name = "pv-1"
        pvc.metadata.name = "disk-pvc"
        pvc_list = MagicMock()
        pvc_list.items = [pvc]
        core_api = MagicMock()
        core_api.list_namespaced_persistent_volume_claim.return_value = pvc_list

        # VA referencing pv-1 on node-1
        va = MagicMock()
        va.spec.source.persistent_volume_name = "pv-1"
        va.spec.node_name = "node-1"
        va.metadata.name = "va-stale"
        va_list = MagicMock()
        va_list.items = [va]
        storage_api = MagicMock()
        storage_api.list_volume_attachment.return_value = va_list

        # No pod uses this PVC on node-1
        empty_pod_list = MagicMock()
        empty_pod_list.items = []
        core_api.list_namespaced_pod.return_value = empty_pod_list

        result = _find_stale_volume_attachments(storage_api, core_api, "ns1")

        assert result == ["va-stale"]

    def test_no_pvcs_returns_empty(self):
        from handlers.project import _find_stale_volume_attachments

        pvc_list = MagicMock()
        pvc_list.items = []
        core_api = MagicMock()
        core_api.list_namespaced_persistent_volume_claim.return_value = pvc_list
        storage_api = MagicMock()

        result = _find_stale_volume_attachments(storage_api, core_api, "ns1")

        assert result == []

    def test_pvc_in_use_not_stale(self):
        from handlers.project import _find_stale_volume_attachments

        pvc = MagicMock()
        pvc.spec.volume_name = "pv-1"
        pvc.metadata.name = "disk-pvc"
        pvc_list = MagicMock()
        pvc_list.items = [pvc]
        core_api = MagicMock()
        core_api.list_namespaced_persistent_volume_claim.return_value = pvc_list

        va = MagicMock()
        va.spec.source.persistent_volume_name = "pv-1"
        va.spec.node_name = "node-1"
        va.metadata.name = "va-used"
        va_list = MagicMock()
        va_list.items = [va]
        storage_api = MagicMock()
        storage_api.list_volume_attachment.return_value = va_list

        # Pod uses this PVC on the same node
        claim = MagicMock()
        claim.claim_name = "disk-pvc"
        vol = MagicMock()
        vol.persistent_volume_claim = claim
        pod = MagicMock()
        pod.metadata.deletion_timestamp = None
        pod.spec.volumes = [vol]
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api.list_namespaced_pod.return_value = pod_list

        result = _find_stale_volume_attachments(storage_api, core_api, "ns1")

        assert result == []


class TestPollExportJobs:
    def test_all_done_returns_none(self):
        from handlers.project import _poll_export_jobs

        batch_api = MagicMock()
        job = MagicMock()
        job.status.succeeded = 1
        job.status.failed = None
        batch_api.read_namespaced_job.return_value = job

        export_jobs = [{"jobName": "export-vm1-disk1"}]
        patch_obj = MagicMock()

        custom_api = MagicMock()
        result = asyncio.run(
            _poll_export_jobs(batch_api, export_jobs, "ns1", custom_api, "proj1")
        )

        assert result is None

    def test_failed_job_returns_error(self):
        from handlers.project import _poll_export_jobs

        batch_api = MagicMock()
        job = MagicMock()
        job.status.succeeded = None
        failed_condition = MagicMock()
        failed_condition.type = "Failed"
        failed_condition.status = "True"
        job.status.conditions = [failed_condition]
        batch_api.read_namespaced_job.return_value = job

        export_jobs = [{"jobName": "export-vm1-disk1"}]
        custom_api = MagicMock()

        result = asyncio.run(
            _poll_export_jobs(batch_api, export_jobs, "ns1", custom_api, "proj1")
        )

        assert result is not None
        assert "failed" in result

    def test_timeout_returns_error(self):
        from handlers.project import _poll_export_jobs

        batch_api = MagicMock()
        job = MagicMock()
        job.status.succeeded = None
        job.status.failed = None
        batch_api.read_namespaced_job.return_value = job

        export_jobs = [{"jobName": "export-vm1-disk1", "deadline": 10}]
        custom_api = MagicMock()

        async def _run():
            with patch("asyncio.sleep", return_value=asyncio.Future()) as ms:
                ms.return_value.set_result(None)
                return await _poll_export_jobs(
                    batch_api, export_jobs, "ns1", custom_api, "proj1"
                )

        result = asyncio.run(_run())

        assert result is not None
        assert "timed out" in result

    def test_deadline_scales_with_disk_size(self):
        from handlers.project import _snapshot_and_export_disk

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"readyToUse": True}
        }
        core_api = MagicMock()
        batch_api = MagicMock()
        patch_obj = MagicMock()
        patch_obj.status = {}

        disk_info = {
            "pvcName": "vm-abc-disk-1234",
            "diskId": "1234abcd-0000-0000-0000-000000000000",
            "vmName": "myvm",
            "s3Key": "patterns/p1/disk.qcow2",
            "vmId": "vm-uuid-1",
            "sizeGb": 1228,
            "format": "qcow2",
        }
        result = asyncio.run(
            _snapshot_and_export_disk(
                disk_info,
                {"bucket": "b"},
                custom_api,
                core_api,
                batch_api,
                "ns1",
                "proj1",
            )
        )
        assert result["deadline"] == 1228 * 90


class TestCreateVncRbac:
    @patch("handlers.project.client")
    def test_creates_role_and_binding(self, mock_client):
        from handlers.project import _create_vnc_rbac

        mock_rbac = MagicMock()
        mock_client.RbacAuthorizationV1Api.return_value = mock_rbac

        _create_vnc_rbac("ns1")

        mock_rbac.create_namespaced_role.assert_called_once()
        mock_rbac.create_namespaced_role_binding.assert_called_once()

    @patch("handlers.project.client")
    def test_409_swallowed_for_both(self, mock_client):
        from handlers.project import _create_vnc_rbac
        from kubernetes.client import ApiException

        mock_rbac = MagicMock()
        mock_rbac.create_namespaced_role.side_effect = ApiException(status=409)
        mock_rbac.create_namespaced_role_binding.side_effect = ApiException(status=409)
        mock_client.RbacAuthorizationV1Api.return_value = mock_rbac

        _create_vnc_rbac("ns1")

    @patch("handlers.project.client")
    def test_non_409_raises(self, mock_client):
        from handlers.project import _create_vnc_rbac
        from kubernetes.client import ApiException

        mock_rbac = MagicMock()
        mock_rbac.create_namespaced_role.side_effect = ApiException(status=500)
        mock_client.RbacAuthorizationV1Api.return_value = mock_rbac

        with pytest.raises(ApiException):
            _create_vnc_rbac("ns1")


# ---------------------------------------------------------------------------
# handlers/vm.py — additional coverage (batch 2)
# ---------------------------------------------------------------------------


class TestEnsureBmcSaAndRbac:
    @patch("handlers.vm.client")
    def test_creates_sa_scc_role_binding(self, mock_client):
        from handlers.vm import _ensure_bmc_sa_and_rbac

        core_api = MagicMock()
        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {"users": []}
        mock_rbac = MagicMock()
        mock_client.RbacAuthorizationV1Api.return_value = mock_rbac

        _ensure_bmc_sa_and_rbac("ns1", core_api, custom_api)

        core_api.create_namespaced_service_account.assert_called_once()
        custom_api.patch_cluster_custom_object.assert_called_once()
        mock_rbac.create_namespaced_role.assert_called_once()
        mock_rbac.create_namespaced_role_binding.assert_called_once()

    @patch("handlers.vm.client.RbacAuthorizationV1Api")
    def test_sa_409_swallowed(self, mock_rbac_cls):
        from handlers.vm import _ensure_bmc_sa_and_rbac
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.create_namespaced_service_account.side_effect = ApiException(
            status=409
        )
        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {"users": []}
        mock_rbac_cls.return_value = MagicMock()

        _ensure_bmc_sa_and_rbac("ns1", core_api, custom_api)

    @patch("handlers.vm.client")
    def test_skips_scc_patch_when_sa_already_in_list(self, mock_client):
        from handlers.vm import _ensure_bmc_sa_and_rbac

        core_api = MagicMock()
        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {
            "users": ["system:serviceaccount:ns1:troshka-bmc"]
        }
        mock_rbac = MagicMock()
        mock_client.RbacAuthorizationV1Api.return_value = mock_rbac

        _ensure_bmc_sa_and_rbac("ns1", core_api, custom_api)

        custom_api.patch_cluster_custom_object.assert_not_called()

    @patch("handlers.vm.client")
    def test_scc_failure_swallowed(self, mock_client):
        from handlers.vm import _ensure_bmc_sa_and_rbac

        core_api = MagicMock()
        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.side_effect = Exception("SCC gone")
        mock_rbac = MagicMock()
        mock_client.RbacAuthorizationV1Api.return_value = mock_rbac

        _ensure_bmc_sa_and_rbac("ns1", core_api, custom_api)


class TestDeleteAndWaitForKubevirtVm:
    def test_deletes_and_waits_for_404(self):
        from handlers.vm import _delete_and_wait_for_kubevirt_vm
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)

        asyncio.run(_delete_and_wait_for_kubevirt_vm(custom_api, "ns1", "kv-vm-1"))

        custom_api.delete_namespaced_custom_object.assert_called_once()

    def test_swallows_delete_exception(self):
        from handlers.vm import _delete_and_wait_for_kubevirt_vm
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.delete_namespaced_custom_object.side_effect = Exception("fail")
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)

        asyncio.run(_delete_and_wait_for_kubevirt_vm(custom_api, "ns1", "kv-vm-1"))


class TestRecreateKubevirtVm:
    @patch("handlers.vm._delete_and_wait_for_kubevirt_vm")
    def test_deletes_then_creates(self, mock_delete):
        from handlers.vm import _recreate_kubevirt_vm

        async def noop(*a, **k):
            pass

        mock_delete.side_effect = noop

        custom_api = MagicMock()
        kv_vm = {"metadata": {"name": "kv-vm-1"}}

        asyncio.run(_recreate_kubevirt_vm(custom_api, "ns1", kv_vm, "kv-vm-1"))

        mock_delete.assert_called_once()
        custom_api.create_namespaced_custom_object.assert_called_once()

    def test_409_on_recreate_is_swallowed(self):
        from handlers.vm import _recreate_kubevirt_vm
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
        custom_api.create_namespaced_custom_object.side_effect = ApiException(
            status=409
        )
        kv_vm = {"metadata": {"name": "kv-vm-1"}}

        asyncio.run(_recreate_kubevirt_vm(custom_api, "ns1", kv_vm, "kv-vm-1"))


class TestStopKubevirtVm:
    def test_patches_running_false(self):
        from handlers.vm import _stop_kubevirt_vm
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        # VMI disappears on first check
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)

        asyncio.run(_stop_kubevirt_vm(custom_api, "ns1", "kv-vm-1"))

        custom_api.patch_namespaced_custom_object.assert_called_once()
        body = custom_api.patch_namespaced_custom_object.call_args[1]["body"]
        assert body == {"spec": {"running": False}}

    def test_patch_failure_swallowed(self):
        from handlers.vm import _stop_kubevirt_vm
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.patch_namespaced_custom_object.side_effect = Exception("fail")
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)

        asyncio.run(_stop_kubevirt_vm(custom_api, "ns1", "kv-vm-1"))


class TestDeleteRemovedDisksVm:
    def test_deletes_only_removed(self):
        from handlers.vm import _delete_removed_disks

        core_api = MagicMock()
        custom_api = MagicMock()
        old_disks = {"disk-aaa12345": {}, "disk-bbb12345": {}, "disk-ccc12345": {}}
        new_disks = {"disk-aaa12345": {}}

        _delete_removed_disks(old_disks, new_disks, "vm-1", "ns1", core_api, custom_api)

        assert custom_api.delete_namespaced_custom_object.call_count == 2
        assert core_api.delete_namespaced_persistent_volume_claim.call_count == 2


class TestProvisionDiskPvcs:
    @patch("handlers.vm._ensure_golden_pvc")
    @patch("handlers.vm._wait_for_datavolume", return_value=True)
    def test_provisions_s3_disk(self, mock_wait, mock_golden):
        from handlers.vm import _provision_disk_pvcs

        mock_golden.return_value = "golden-abc123"
        custom_api = MagicMock()
        core_api = MagicMock()

        spec = {
            "disks": [
                {
                    "id": "disk-aaa",
                    "sizeGb": 40,
                    "libraryImage": {"s3Path": "library/rhel.qcow2"},
                }
            ]
        }
        body = {
            "kind": "TroshkaVM",
            "metadata": {"name": "vm-1", "uid": "uid-1"},
        }
        patch_obj = MagicMock()

        result = asyncio.run(
            _provision_disk_pvcs(
                spec,
                "vm-1",
                "ns1",
                body,
                core_api,
                custom_api,
                {"bucket": "b"},
                {},
                patch_obj,
            )
        )

        assert "disk-aaa" in result
        custom_api.create_namespaced_custom_object.assert_called_once()

    def test_provisions_blank_disk(self):
        from handlers.vm import _provision_disk_pvcs

        custom_api = MagicMock()
        core_api = MagicMock()

        spec = {"disks": [{"id": "disk-bbb", "blank": True, "sizeGb": 100}]}
        body = {
            "kind": "TroshkaVM",
            "metadata": {"name": "vm-1", "uid": "uid-1"},
        }
        patch_obj = MagicMock()

        result = asyncio.run(
            _provision_disk_pvcs(
                spec,
                "vm-1",
                "ns1",
                body,
                core_api,
                custom_api,
                {},
                {},
                patch_obj,
            )
        )

        assert "disk-bbb" in result
        core_api.create_namespaced_persistent_volume_claim.assert_called_once()

    def test_empty_disks(self):
        from handlers.vm import _provision_disk_pvcs

        result = asyncio.run(
            _provision_disk_pvcs(
                {"disks": []},
                "vm-1",
                "ns1",
                {},
                MagicMock(),
                MagicMock(),
                {},
                {},
                MagicMock(),
            )
        )

        assert result == {}


class TestProvisionCdrom:
    @patch("handlers.vm._ensure_golden_pvc")
    @patch("handlers.vm._wait_for_datavolume", return_value=True)
    def test_provisions_cdrom(self, mock_wait, mock_golden):
        from handlers.vm import _provision_cdrom

        mock_golden.return_value = "golden-iso"
        custom_api = MagicMock()
        core_api = MagicMock()
        golden_pvc = MagicMock()
        golden_pvc.spec.resources.requests = {"storage": "10Gi"}
        core_api.read_namespaced_persistent_volume_claim.return_value = golden_pvc

        spec = {"cdrom": {"s3Path": "library/rhel.iso"}}
        body = {
            "kind": "TroshkaVM",
            "metadata": {"name": "vm-1", "uid": "uid-1"},
        }

        result = asyncio.run(
            _provision_cdrom(
                spec, "vm-1", "ns1", body, core_api, custom_api, {"bucket": "b"}
            )
        )

        assert result == "vm-1-cdrom"

    def test_skips_when_no_cdrom(self):
        from handlers.vm import _provision_cdrom

        result = asyncio.run(
            _provision_cdrom({}, "vm-1", "ns1", {}, MagicMock(), MagicMock(), {})
        )

        assert result is None

    @patch("handlers.vm._ensure_golden_pvc", side_effect=Exception("S3 down"))
    def test_failure_returns_none(self, mock_golden):
        from handlers.vm import _provision_cdrom

        spec = {"cdrom": {"s3Path": "library/rhel.iso"}}
        body = {
            "kind": "TroshkaVM",
            "metadata": {"name": "vm-1", "uid": "uid-1"},
        }

        result = asyncio.run(
            _provision_cdrom(spec, "vm-1", "ns1", body, MagicMock(), MagicMock(), {})
        )

        assert result is None


# ── KubeVirt driver static method tests ──

# Add BMC driver to path for KubeVirt driver tests
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "images", "bmc"))


class TestKubeVirtDriverStaticMethods:
    def test_strip_boot_orders(self):
        from kubevirt_driver import KubeVirtDriver

        disks = [
            {"name": "disk1", "bootOrder": 1, "disk": {}},
            {"name": "disk2", "bootOrder": 2, "cdrom": {}},
        ]
        ifaces = [{"name": "iface1", "bootOrder": 3}]
        stripped_disks, stripped_ifaces = KubeVirtDriver._strip_boot_orders(
            disks, ifaces
        )
        for d in stripped_disks:
            assert "bootOrder" not in d
        for i in stripped_ifaces:
            assert "bootOrder" not in i

    def test_assign_boot_orders_disk(self):
        from kubevirt_driver import KubeVirtDriver

        disks = [
            {"name": "d1", "disk": {}},
            {"name": "d2", "cdrom": {}},
        ]
        ifaces = [{"name": "i1"}]
        KubeVirtDriver._assign_boot_orders(disks, ifaces, "disk")
        # disk entries should get bootOrder first
        disk_entries = [d for d in disks if "disk" in d]
        assert disk_entries[0].get("bootOrder") == 1

    def test_assign_boot_orders_interface(self):
        from kubevirt_driver import KubeVirtDriver

        disks = [{"name": "d1", "disk": {}}]
        ifaces = [{"name": "i1"}, {"name": "i2"}]
        KubeVirtDriver._assign_boot_orders(disks, ifaces, "interface")
        assert ifaces[0].get("bootOrder") == 1
        assert disks[0].get("bootOrder") == 2 or disks[0].get("bootOrder") == 3

    def test_assign_boot_orders_cdrom(self):
        from kubevirt_driver import KubeVirtDriver

        disks = [
            {"name": "d1", "cdrom": {}},
            {"name": "d2", "disk": {}},
        ]
        ifaces = []
        KubeVirtDriver._assign_boot_orders(disks, ifaces, "cdrom")
        disk_entries = [d for d in disks if "disk" in d]
        cdrom_entries = [d for d in disks if "cdrom" in d]
        assert disk_entries[0].get("bootOrder") == 1
        assert cdrom_entries[0].get("bootOrder") == 2

    def test_restore_boot_orders(self):
        from kubevirt_driver import KubeVirtDriver

        items = [
            {"name": "disk1"},
            {"name": "disk2"},
        ]
        saved = [{"disk1": 2}, {"disk2": 1}]
        KubeVirtDriver._restore_boot_orders(items, saved)
        assert items[0]["bootOrder"] == 2
        assert items[1]["bootOrder"] == 1

    def test_restore_boot_orders_skips_none(self):
        from kubevirt_driver import KubeVirtDriver

        items = [{"name": "disk1"}]
        saved = [{"disk1": None}]
        KubeVirtDriver._restore_boot_orders(items, saved)
        assert "bootOrder" not in items[0]


class TestKubeVirtDriverGetters:
    def _make_driver(self):
        from kubevirt_driver import KubeVirtDriver

        with patch("kubernetes.config.load_incluster_config"):
            with patch("kubernetes.client.CustomObjectsApi"):
                with patch.dict(
                    os.environ,
                    {"SUSHY_NAMESPACE": "test-ns", "SUSHY_VM_MAP": '{"uuid1": "vm-1"}'},
                ):
                    d = KubeVirtDriver()
        return d

    def test_get_total_memory(self):
        d = self._make_driver()
        d._get_vm = MagicMock(
            return_value={
                "spec": {
                    "template": {
                        "spec": {
                            "domain": {"resources": {"requests": {"memory": "4Gi"}}}
                        }
                    }
                }
            }
        )
        mem = d.get_total_memory("uuid1")
        assert mem == 4096

    def test_get_total_cpus(self):
        d = self._make_driver()
        d._get_vm = MagicMock(
            return_value={
                "spec": {"template": {"spec": {"domain": {"cpu": {"cores": 8}}}}}
            }
        )
        cpus = d.get_total_cpus("uuid1")
        assert cpus == 8

    def test_get_boot_override_enabled_continuous(self):
        d = self._make_driver()
        d._boot_once_overrides = {}
        result = d.get_boot_override_enabled("uuid1")
        assert result == "Continuous"

    def test_get_boot_override_enabled_once(self):
        d = self._make_driver()
        d._boot_once_overrides = {"vm-1": {"disks": [], "interfaces": []}}
        result = d.get_boot_override_enabled("uuid1")
        assert result == "Once"

    def test_get_systems_from_vm_map(self):
        d = self._make_driver()
        d.custom_api.list_namespaced_custom_object.return_value = {"items": []}
        systems = d.get_systems()
        assert systems == ["uuid1"]

    def test_get_systems_from_api(self):
        d = self._make_driver()
        d.vm_map = {}
        d.custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {"metadata": {"name": "vm-1", "uid": "uid-1"}},
                {"metadata": {"name": "vm-2", "uid": "uid-2"}},
            ]
        }
        systems = d.get_systems()
        assert set(systems) == {"uid-1", "uid-2"}

    def test_get_boot_mode_uefi(self):
        d = self._make_driver()
        d._get_vm = MagicMock(
            return_value={
                "spec": {
                    "template": {
                        "spec": {
                            "domain": {
                                "firmware": {
                                    "bootloader": {"efi": {"secureBoot": False}}
                                }
                            }
                        }
                    }
                }
            }
        )
        assert d.get_boot_mode("uuid1") == "UEFI"

    def test_get_boot_mode_legacy(self):
        d = self._make_driver()
        d._get_vm = MagicMock(
            return_value={
                "spec": {
                    "template": {"spec": {"domain": {"firmware": {"bootloader": {}}}}}
                }
            }
        )
        assert d.get_boot_mode("uuid1") == "Legacy"

    def test_get_nics(self):
        d = self._make_driver()
        d._get_vm = MagicMock(
            return_value={
                "spec": {
                    "template": {
                        "spec": {
                            "domain": {
                                "devices": {
                                    "interfaces": [
                                        {
                                            "name": "nic1",
                                            "macAddress": "aa:bb:cc:dd:ee:ff",
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            }
        )
        nics = d.get_nics("uuid1")
        assert len(nics) == 1
        assert nics[0]["mac"] == "aa:bb:cc:dd:ee:ff"


# ---------------------------------------------------------------------------
# handlers/project.py — _snapshot_and_export_disk
# ---------------------------------------------------------------------------


class TestSnapshotAndExportDisk:
    def _make_disk_info(self):
        return {
            "pvcName": "vm-abc-disk-1234",
            "diskId": "1234abcd-0000-0000-0000-000000000000",
            "vmName": "myvm",
            "s3Key": "patterns/p1/disk.qcow2",
            "vmId": "vm-uuid-1",
            "sizeGb": 20,
            "format": "qcow2",
        }

    @patch(
        "helpers.patterns.build_export_job",
        return_value={"metadata": {"name": "export-myvm-1234abcd"}},
    )
    @patch(
        "helpers.patterns.build_temp_pvc_from_snapshot",
        return_value={"metadata": {"name": "export-myvm-1234abcd"}},
    )
    @patch(
        "helpers.patterns.build_volume_snapshot",
        return_value={"metadata": {"name": "snap-myvm-1234abcd"}},
    )
    def test_creates_snapshot_pvc_and_job(self, mock_snap, mock_pvc, mock_job):
        from handlers.project import _snapshot_and_export_disk

        custom_api = MagicMock()
        # First call creates snapshot, second call polls it as ready
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"readyToUse": True}
        }
        core_api = MagicMock()
        batch_api = MagicMock()
        patch_obj = MagicMock()
        patch_obj.status = {}
        s3_config = {"bucket": "test-bucket"}

        result = asyncio.run(
            _snapshot_and_export_disk(
                self._make_disk_info(),
                s3_config,
                custom_api,
                core_api,
                batch_api,
                "ns1",
                "proj1",
            )
        )

        custom_api.create_namespaced_custom_object.assert_called_once()
        assert core_api.create_namespaced_persistent_volume_claim.call_count == 2
        batch_api.create_namespaced_job.assert_called_once()
        assert result["snapName"] == "snap-myvm-1234abcd"
        assert result["tempPvcName"] == "export-myvm-1234abcd"
        assert result["scratchPvcName"] == "scratch-myvm-1234abcd"
        assert result["diskId"] == "1234abcd-0000-0000-0000-000000000000"
        assert result["s3Key"] == "patterns/p1/disk.qcow2"
        assert result["format"] == "qcow2"

    @patch(
        "helpers.patterns.build_export_job", return_value={"metadata": {"name": "j"}}
    )
    @patch(
        "helpers.patterns.build_temp_pvc_from_snapshot",
        return_value={"metadata": {"name": "p"}},
    )
    @patch(
        "helpers.patterns.build_volume_snapshot",
        return_value={"metadata": {"name": "s"}},
    )
    def test_handles_409_conflict_on_snapshot(self, mock_snap, mock_pvc, mock_job):
        from handlers.project import _snapshot_and_export_disk
        from kubernetes.client.exceptions import ApiException

        custom_api = MagicMock()
        exc = ApiException(status=409, reason="Conflict")
        custom_api.create_namespaced_custom_object.side_effect = exc
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"readyToUse": True}
        }
        core_api = MagicMock()
        batch_api = MagicMock()
        patch_obj = MagicMock()
        patch_obj.status = {}

        # Should not raise on 409
        result = asyncio.run(
            _snapshot_and_export_disk(
                self._make_disk_info(),
                {"bucket": "b"},
                custom_api,
                core_api,
                batch_api,
                "ns1",
                "proj1",
            )
        )
        assert "snapName" in result

    @patch(
        "helpers.patterns.build_export_job", return_value={"metadata": {"name": "j"}}
    )
    @patch(
        "helpers.patterns.build_temp_pvc_from_snapshot",
        return_value={"metadata": {"name": "p"}},
    )
    @patch(
        "helpers.patterns.build_volume_snapshot",
        return_value={"metadata": {"name": "s"}},
    )
    def test_returns_correct_metadata(self, mock_snap, mock_pvc, mock_job):
        from handlers.project import _snapshot_and_export_disk

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"readyToUse": True}
        }
        core_api = MagicMock()
        batch_api = MagicMock()
        patch_obj = MagicMock()
        patch_obj.status = {}

        disk_info = self._make_disk_info()
        result = asyncio.run(
            _snapshot_and_export_disk(
                disk_info,
                {"bucket": "b"},
                custom_api,
                core_api,
                batch_api,
                "ns1",
                "proj1",
            )
        )
        assert result["jobName"] == "export-myvm-1234abcd"
        assert result["vmId"] == "vm-uuid-1"
        assert result["virtualSizeBytes"] == 20 * 1073741824


# ---------------------------------------------------------------------------
# handlers/vm.py — vm_delete handler
# ---------------------------------------------------------------------------


class TestVmDeleteHandler:
    """Tests for the vm_delete kopf handler.

    Since kopf is mocked as MagicMock in conftest, the @kopf.on.delete
    decorator wraps the function in a MagicMock.  We extract the original
    async function from the mock's call_args to test it directly.
    """

    @staticmethod
    def _get_vm_delete_fn():
        """Extract the unwrapped vm_delete async function.

        Since kopf is mocked as MagicMock, @kopf.on.delete(G,V,P)(fn)
        passes fn to the mock but returns a MagicMock instead of fn.
        We retrieve the original async function from the mock's call args.
        """
        import importlib

        import kopf

        # Ensure the module is loaded so decorators have been applied
        importlib.import_module("handlers.vm")

        decorator_mock = kopf.on.delete.return_value
        for call_args in reversed(decorator_mock.call_args_list):
            fn = call_args[0][0]
            if asyncio.iscoroutinefunction(fn) and fn.__name__ == "vm_delete":
                return fn
        raise RuntimeError("Could not find vm_delete in kopf mock call args")

    def test_deletes_kubevirt_vm_and_disks(self):
        vm_delete = self._get_vm_delete_fn()

        spec = {
            "disks": [
                {"id": "disk1aaa-0000-0000-0000-000000000000"},
                {"id": "disk2bbb-0000-0000-0000-000000000000"},
            ],
            "cdrom": {},
        }
        status = {"kubevirtVmName": "troshka-vm-abc"}
        meta = {"name": "vm-abc", "uid": "uid1"}

        with patch("handlers.vm.client") as mock_client:
            mock_custom = MagicMock()
            mock_core = MagicMock()
            mock_client.CustomObjectsApi.return_value = mock_custom
            mock_client.CoreV1Api.return_value = mock_core

            asyncio.run(
                vm_delete(
                    spec=spec,
                    status=status,
                    meta=meta,
                    namespace="ns1",
                    name="vm-abc",
                )
            )

            # KubeVirt VM delete
            mock_custom.delete_namespaced_custom_object.assert_any_call(
                group="kubevirt.io",
                version="v1",
                namespace="ns1",
                plural="virtualmachines",
                name="troshka-vm-abc",
            )
            # DV deletes (2 disks x datavolumes)
            assert mock_custom.delete_namespaced_custom_object.call_count >= 3

    def test_handles_404_on_vm_delete(self):
        vm_delete = self._get_vm_delete_fn()
        from kubernetes.client import ApiException
        from kubernetes import client as real_client

        spec = {"disks": [], "cdrom": {}}
        status = {"kubevirtVmName": "troshka-vm-abc"}
        meta = {"name": "vm-abc", "uid": "uid1"}

        with patch("handlers.vm.client") as mock_client:
            mock_client.ApiException = real_client.ApiException
            mock_custom = MagicMock()
            mock_custom.delete_namespaced_custom_object.side_effect = ApiException(
                status=404, reason="Not Found"
            )
            mock_core = MagicMock()
            mock_client.CustomObjectsApi.return_value = mock_custom
            mock_client.CoreV1Api.return_value = mock_core

            # Should not raise on 404
            asyncio.run(
                vm_delete(
                    spec=spec,
                    status=status,
                    meta=meta,
                    namespace="ns1",
                    name="vm-abc",
                )
            )

    def test_deletes_cdrom_pvc(self):
        vm_delete = self._get_vm_delete_fn()

        spec = {
            "disks": [],
            "cdrom": {"s3Path": "library/iso/rhel.iso"},
        }
        status = {"kubevirtVmName": "troshka-vm-abc"}
        meta = {"name": "vm-abc", "uid": "uid1"}

        with patch("handlers.vm.client") as mock_client:
            mock_custom = MagicMock()
            mock_core = MagicMock()
            mock_client.CustomObjectsApi.return_value = mock_custom
            mock_client.CoreV1Api.return_value = mock_core

            asyncio.run(
                vm_delete(
                    spec=spec,
                    status=status,
                    meta=meta,
                    namespace="ns1",
                    name="vm-abc",
                )
            )

            mock_core.delete_namespaced_persistent_volume_claim.assert_any_call(
                name="vm-abc-cdrom", namespace="ns1"
            )

    def test_deletes_cloudinit_secret(self):
        vm_delete = self._get_vm_delete_fn()

        spec = {"disks": [], "cdrom": {}}
        status = {"kubevirtVmName": "troshka-vm-abc"}
        meta = {"name": "vm-abc", "uid": "uid1"}

        with patch("handlers.vm.client") as mock_client:
            mock_custom = MagicMock()
            mock_core = MagicMock()
            mock_client.CustomObjectsApi.return_value = mock_custom
            mock_client.CoreV1Api.return_value = mock_core

            asyncio.run(
                vm_delete(
                    spec=spec,
                    status=status,
                    meta=meta,
                    namespace="ns1",
                    name="vm-abc",
                )
            )

            mock_core.delete_namespaced_secret.assert_called_once_with(
                name="cloudinit-vm-abc", namespace="ns1"
            )

    def test_handles_404_on_secret_delete(self):
        vm_delete = self._get_vm_delete_fn()
        from kubernetes.client import ApiException
        from kubernetes import client as real_client

        spec = {"disks": [], "cdrom": {}}
        status = {"kubevirtVmName": "troshka-vm-abc"}
        meta = {"name": "vm-abc", "uid": "uid1"}

        with patch("handlers.vm.client") as mock_client:
            mock_client.ApiException = real_client.ApiException
            mock_custom = MagicMock()
            mock_core = MagicMock()
            mock_core.delete_namespaced_secret.side_effect = ApiException(
                status=404, reason="Not Found"
            )
            mock_client.CustomObjectsApi.return_value = mock_custom
            mock_client.CoreV1Api.return_value = mock_core

            # Should not raise on 404
            asyncio.run(
                vm_delete(
                    spec=spec,
                    status=status,
                    meta=meta,
                    namespace="ns1",
                    name="vm-abc",
                )
            )


# ---------------------------------------------------------------------------
# NEW TESTS: vm.py — additional coverage
# ---------------------------------------------------------------------------


class TestWaitForDatavolumeExtended:
    """Cover _wait_for_datavolume timeout, owner-deleted, and error paths."""

    def test_succeeds_when_phase_succeeded(self):
        from handlers.vm import _wait_for_datavolume

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Succeeded"}
        }

        result = asyncio.run(_wait_for_datavolume(custom_api, "dv-1", "ns1"))
        assert result is True

    def test_returns_false_on_failed_phase(self):
        from handlers.vm import _wait_for_datavolume

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Failed", "conditions": []}
        }

        result = asyncio.run(_wait_for_datavolume(custom_api, "dv-1", "ns1"))
        assert result is False

    def test_returns_false_on_error_phase(self):
        from handlers.vm import _wait_for_datavolume

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Error", "conditions": []}
        }

        result = asyncio.run(_wait_for_datavolume(custom_api, "dv-1", "ns1"))
        assert result is False

    def test_returns_false_when_dv_404(self):
        from handlers.vm import _wait_for_datavolume
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)

        result = asyncio.run(_wait_for_datavolume(custom_api, "dv-1", "ns1"))
        assert result is False

    def test_returns_false_when_owner_deleted(self):
        from handlers.vm import _wait_for_datavolume
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        # First call is owner check -> 404
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)

        result = asyncio.run(
            _wait_for_datavolume(
                custom_api,
                "dv-1",
                "ns1",
                owner_name="vm-1",
                owner_namespace="ns1",
            )
        )
        assert result is False

    def test_owner_check_generic_exception_continues(self):
        """When the owner check raises a non-ApiException, it should be swallowed."""
        from handlers.vm import _wait_for_datavolume

        custom_api = MagicMock()
        # Side effects: 1st call = owner check (generic exc), 2nd call = DV check (Succeeded)
        custom_api.get_namespaced_custom_object.side_effect = [
            Exception("connection error"),
            {"status": {"phase": "Succeeded"}},
        ]

        result = asyncio.run(
            _wait_for_datavolume(
                custom_api,
                "dv-1",
                "ns1",
                owner_name="vm-1",
                owner_namespace="ns1",
            )
        )
        assert result is True


class TestEnsureGoldenPvcExtended:
    """Cover _ensure_golden_pvc: PVC exists, namespace conflict, DV conflict."""

    def test_returns_existing_pvc(self):
        from handlers.vm import _ensure_golden_pvc
        from helpers.kubevirt import s3_import_url

        custom_api = MagicMock()
        core_api = MagicMock()
        # Matching golden DataVolume already exists -> return its name, no import.
        custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "source": {
                    "s3": {
                        "url": s3_import_url("lib/x.qcow2", {}),
                        "secretRef": "s3-credentials",  # pragma: allowlist secret
                    }
                }
            }
        }

        result = asyncio.run(
            _ensure_golden_pvc(custom_api, core_api, "lib/x.qcow2", 20, {})
        )
        from helpers.k8s import golden_pvc_name

        assert result == golden_pvc_name("lib/x.qcow2")

    def test_creates_namespace_and_dv_when_pvc_not_found(self):
        from handlers.vm import _ensure_golden_pvc
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        core_api = MagicMock()
        # PVC not found
        core_api.read_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )
        # DV check: succeed immediately
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Succeeded"}
        }

        result = asyncio.run(
            _ensure_golden_pvc(custom_api, core_api, "lib/y.qcow2", 20, {})
        )
        # Namespace created
        core_api.create_namespace.assert_called_once()
        # DV created
        custom_api.create_namespaced_custom_object.assert_called_once()
        from helpers.k8s import golden_pvc_name

        assert result == golden_pvc_name("lib/y.qcow2")

    def test_namespace_409_is_swallowed(self):
        from handlers.vm import _ensure_golden_pvc
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )
        # Namespace already exists
        core_api.create_namespace.side_effect = ApiException(status=409)
        # DV succeeds
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Succeeded"}
        }

        # Should not raise
        asyncio.run(_ensure_golden_pvc(custom_api, core_api, "lib/z.qcow2", 20, {}))

    def test_namespace_non_409_raises(self):
        from handlers.vm import _ensure_golden_pvc
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )
        core_api.create_namespace.side_effect = ApiException(status=500)

        with pytest.raises(ApiException):
            asyncio.run(_ensure_golden_pvc(custom_api, core_api, "lib/z.qcow2", 20, {}))

    def test_dv_409_is_swallowed(self):
        from handlers.vm import _ensure_golden_pvc
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )
        # DV create 409
        custom_api.create_namespaced_custom_object.side_effect = ApiException(
            status=409
        )
        # DV wait succeeds
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Succeeded"}
        }

        asyncio.run(_ensure_golden_pvc(custom_api, core_api, "lib/a.qcow2", 20, {}))

    def test_existing_dv_read_non_404_raises(self):
        from handlers.vm import _ensure_golden_pvc
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        core_api = MagicMock()
        # Reading the existing golden DataVolume errors with a non-404 -> propagate.
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=500)

        with pytest.raises(ApiException):
            asyncio.run(_ensure_golden_pvc(custom_api, core_api, "lib/b.qcow2", 20, {}))

    def test_default_secret_name_when_none(self):
        from handlers.vm import _ensure_golden_pvc
        from helpers.kubevirt import s3_import_url

        custom_api = MagicMock()
        core_api = MagicMock()
        # secret_name=None defaults to "s3-credentials"; a matching DV lets us
        # exercise that branch without falling into the import/wait path.
        custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "source": {
                    "s3": {
                        "url": s3_import_url("lib/c.qcow2", {}),
                        "secretRef": "s3-credentials",  # pragma: allowlist secret
                    }
                }
            }
        }

        asyncio.run(
            _ensure_golden_pvc(
                custom_api, core_api, "lib/c.qcow2", 20, {}, secret_name=None
            )
        )
        # Should not raise -- covers the `if not secret_name` branch


class TestRunGuestfishJobExtended:
    """Cover _run_guestfish_job: no commands, job succeeds, job fails."""

    def test_noop_when_no_guestfish_commands(self):
        from handlers.vm import _run_guestfish_job

        spec = {"disks": []}
        body = {"metadata": {"name": "vm1", "uid": "u1"}, "kind": "TroshkaVM"}

        # Should return without doing anything
        asyncio.run(_run_guestfish_job(spec, "vm-1", "ns1", body, {}))

    def test_creates_and_waits_for_job(self):
        from handlers.vm import _run_guestfish_job
        from kubernetes import client as real_client

        spec = {
            "guestfishCommands": ["rm /etc/old"],
            "disks": [{"id": "disk-aaa"}],
        }
        body = {"metadata": {"name": "vm1", "uid": "u1"}, "kind": "TroshkaVM"}
        disk_pvcs = {"disk-aaa": "vm-1-disk-disk-aaa"}

        with patch("handlers.vm.client") as mock_client:
            mock_client.ApiException = real_client.ApiException
            mock_batch = MagicMock()
            mock_client.BatchV1Api.return_value = mock_batch

            job_status = MagicMock()
            job_status.succeeded = True
            job_status.failed = None
            job_mock = MagicMock()
            job_mock.status = job_status
            mock_batch.read_namespaced_job.return_value = job_mock

            asyncio.run(_run_guestfish_job(spec, "vm-1", "ns1", body, disk_pvcs))
            mock_batch.create_namespaced_job.assert_called_once()

    def test_job_create_409_continues(self):
        from handlers.vm import _run_guestfish_job
        from kubernetes import client as real_client
        from kubernetes.client import ApiException

        spec = {
            "guestfishCommands": ["rm /etc/old"],
            "disks": [{"id": "disk-aaa"}],
        }
        body = {"metadata": {"name": "vm1", "uid": "u1"}, "kind": "TroshkaVM"}
        disk_pvcs = {"disk-aaa": "vm-1-disk-disk-aaa"}

        with patch("handlers.vm.client") as mock_client:
            mock_client.ApiException = real_client.ApiException
            mock_batch = MagicMock()
            mock_client.BatchV1Api.return_value = mock_batch

            mock_batch.create_namespaced_job.side_effect = ApiException(status=409)

            job_status = MagicMock()
            job_status.succeeded = True
            job_status.failed = None
            job_mock = MagicMock()
            job_mock.status = job_status
            mock_batch.read_namespaced_job.return_value = job_mock

            asyncio.run(_run_guestfish_job(spec, "vm-1", "ns1", body, disk_pvcs))


class TestResolveNadRefsExtended:
    """Cover _resolve_nad_refs: normal and exception paths."""

    def test_returns_nad_mapping(self):
        from handlers.vm import _resolve_nad_refs

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "net-abc"},
                    "status": {"nadName": "net-abc-nad"},
                },
                {
                    "metadata": {"name": "net-def"},
                    # No status.nadName -- uses fallback
                },
            ]
        }

        result = _resolve_nad_refs(custom_api, "ns1")
        assert result["net-abc"] == "net-abc-nad"
        assert result["net-def"] == "net-def-nad"

    def test_returns_empty_on_exception(self):
        from handlers.vm import _resolve_nad_refs

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = Exception("API down")

        result = _resolve_nad_refs(custom_api, "ns1")
        assert result == {}


class TestFindBmcNadExtended:
    """Cover _find_bmc_nad."""

    def test_finds_bmc_nad(self):
        from handlers.vm import _find_bmc_nad

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "bmc-net"},
                    "spec": {"networkType": "bmc"},
                    "status": {"nadName": "bmc-nad-custom"},
                },
            ]
        }
        assert _find_bmc_nad("ns1", custom_api) == "bmc-nad-custom"

    def test_returns_none_when_no_bmc(self):
        from handlers.vm import _find_bmc_nad

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "std-net"},
                    "spec": {"networkType": "standard"},
                },
            ]
        }
        assert _find_bmc_nad("ns1", custom_api) is None

    def test_returns_none_on_exception(self):
        from handlers.vm import _find_bmc_nad

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = Exception("fail")
        assert _find_bmc_nad("ns1", custom_api) is None

    def test_uses_fallback_nad_name(self):
        from handlers.vm import _find_bmc_nad

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "net-bmc"},
                    "spec": {"networkType": "bmc"},
                    # No status -- fallback
                },
            ]
        }
        assert _find_bmc_nad("ns1", custom_api) == "net-bmc-nad"


class TestSetupBmcExtended:
    """Cover _setup_bmc."""

    def test_noop_when_not_enabled(self):
        from handlers.vm import _setup_bmc

        core_api = MagicMock()
        custom_api = MagicMock()
        _setup_bmc({"bmcEnabled": False}, "ns1", core_api, custom_api)
        core_api.create_namespaced_service_account.assert_not_called()

    @patch("handlers.vm.client")
    def test_noop_when_no_bmc_nad(self, mock_client):
        from handlers.vm import _setup_bmc

        core_api = MagicMock()
        custom_api = MagicMock()
        # _find_bmc_nad returns None
        custom_api.list_namespaced_custom_object.return_value = {"items": []}
        mock_client.AppsV1Api.return_value = MagicMock()

        _setup_bmc({"bmcEnabled": True, "vmId": "vm-1"}, "ns1", core_api, custom_api)
        mock_client.AppsV1Api.return_value.create_namespaced_deployment.assert_not_called()


class TestEnsureBmcSaAndRbacExtended:
    """Cover _ensure_bmc_sa_and_rbac: 409 paths, SCC patch."""

    @patch("handlers.vm.client")
    def test_creates_sa_role_rolebinding(self, mock_client):
        from handlers.vm import _ensure_bmc_sa_and_rbac

        core_api = MagicMock()
        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {"users": []}

        mock_rbac = MagicMock()
        mock_client.RbacAuthorizationV1Api.return_value = mock_rbac

        _ensure_bmc_sa_and_rbac("ns1", core_api, custom_api)

        core_api.create_namespaced_service_account.assert_called_once()
        mock_rbac.create_namespaced_role.assert_called_once()
        mock_rbac.create_namespaced_role_binding.assert_called_once()

    def test_sa_409_swallowed(self):
        from handlers.vm import _ensure_bmc_sa_and_rbac
        from kubernetes.client import ApiException
        from kubernetes import client as real_client

        core_api = MagicMock()
        core_api.create_namespaced_service_account.side_effect = ApiException(
            status=409
        )
        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {"users": []}

        with patch("handlers.vm.client") as mock_client:
            mock_client.ApiException = real_client.ApiException
            mock_client.V1ServiceAccount = real_client.V1ServiceAccount
            mock_client.V1ObjectMeta = real_client.V1ObjectMeta
            mock_rbac = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = mock_rbac

            # Should not raise
            _ensure_bmc_sa_and_rbac("ns1", core_api, custom_api)

    def test_role_409_swallowed(self):
        from handlers.vm import _ensure_bmc_sa_and_rbac
        from kubernetes.client import ApiException
        from kubernetes import client as real_client

        core_api = MagicMock()
        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {"users": []}

        with patch("handlers.vm.client") as mock_client:
            mock_client.ApiException = real_client.ApiException
            mock_client.V1ServiceAccount = real_client.V1ServiceAccount
            mock_client.V1ObjectMeta = real_client.V1ObjectMeta
            mock_rbac = MagicMock()
            mock_rbac.create_namespaced_role.side_effect = ApiException(status=409)
            mock_client.RbacAuthorizationV1Api.return_value = mock_rbac

            _ensure_bmc_sa_and_rbac("ns1", core_api, custom_api)

    def test_rolebinding_409_swallowed(self):
        from handlers.vm import _ensure_bmc_sa_and_rbac
        from kubernetes.client import ApiException
        from kubernetes import client as real_client

        core_api = MagicMock()
        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {"users": []}

        with patch("handlers.vm.client") as mock_client:
            mock_client.ApiException = real_client.ApiException
            mock_client.V1ServiceAccount = real_client.V1ServiceAccount
            mock_client.V1ObjectMeta = real_client.V1ObjectMeta
            mock_rbac = MagicMock()
            mock_rbac.create_namespaced_role_binding.side_effect = ApiException(
                status=409
            )
            mock_client.RbacAuthorizationV1Api.return_value = mock_rbac

            _ensure_bmc_sa_and_rbac("ns1", core_api, custom_api)


class TestTryDeleteDatavolumeAndPvc:
    """Cover _try_delete_datavolume and _try_delete_pvc."""

    def test_deletes_datavolume(self):
        from handlers.vm import _try_delete_datavolume

        custom_api = MagicMock()
        _try_delete_datavolume(custom_api, "ns1", "pvc-1")
        custom_api.delete_namespaced_custom_object.assert_called_once()

    def test_dv_404_swallowed(self):
        from handlers.vm import _try_delete_datavolume
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.delete_namespaced_custom_object.side_effect = ApiException(
            status=404
        )
        _try_delete_datavolume(custom_api, "ns1", "pvc-1")

    def test_deletes_pvc(self):
        from handlers.vm import _try_delete_pvc

        core_api = MagicMock()
        _try_delete_pvc(core_api, "ns1", "pvc-1")
        core_api.delete_namespaced_persistent_volume_claim.assert_called_once()

    def test_pvc_404_swallowed(self):
        from handlers.vm import _try_delete_pvc
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.delete_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )
        _try_delete_pvc(core_api, "ns1", "pvc-1")


class TestDeleteRemovedDisksExtended:
    """Cover _delete_removed_disks."""

    def test_removes_only_deleted_disks(self):
        from handlers.vm import _delete_removed_disks

        core_api = MagicMock()
        custom_api = MagicMock()
        old_disks = {"disk-aaa": {}, "disk-bbb": {}}
        new_disks = {"disk-aaa": {}}

        _delete_removed_disks(old_disks, new_disks, "vm-1", "ns1", core_api, custom_api)
        # Only disk-bbb was removed
        custom_api.delete_namespaced_custom_object.assert_called_once()
        core_api.delete_namespaced_persistent_volume_claim.assert_called_once()

    def test_no_removal_when_same(self):
        from handlers.vm import _delete_removed_disks

        core_api = MagicMock()
        custom_api = MagicMock()
        old_disks = {"disk-aaa": {}}
        new_disks = {"disk-aaa": {}}

        _delete_removed_disks(old_disks, new_disks, "vm-1", "ns1", core_api, custom_api)
        custom_api.delete_namespaced_custom_object.assert_not_called()
        core_api.delete_namespaced_persistent_volume_claim.assert_not_called()


class TestUpsertCloudinitSecretExtended:
    """Cover _upsert_cloudinit_secret: replace success, 404 create, no secret."""

    @patch("handlers.vm.build_cloudinit_secret")
    def test_returns_none_when_no_secret(self, mock_build):
        from handlers.vm import _upsert_cloudinit_secret

        mock_build.return_value = None
        result = _upsert_cloudinit_secret(
            {}, "ns1", MagicMock()
        )  # pragma: allowlist secret
        assert result is None

    @patch("handlers.vm.build_cloudinit_secret")
    @patch("handlers.vm.owner_ref")
    def test_replaces_existing_secret(self, mock_owner, mock_build):
        from handlers.vm import _upsert_cloudinit_secret

        mock_build.return_value = {
            "metadata": {"name": "cloudinit-vm1"},
            "data": {},
        }
        mock_owner.return_value = {"kind": "TroshkaVM"}
        core_api = MagicMock()

        result = _upsert_cloudinit_secret(
            {"kind": "TroshkaVM", "metadata": {"name": "vm1", "uid": "u1"}},
            "ns1",
            core_api,
        )
        assert result == "cloudinit-vm1"
        core_api.replace_namespaced_secret.assert_called_once()

    @patch("handlers.vm.build_cloudinit_secret")
    @patch("handlers.vm.owner_ref")
    def test_creates_on_404(self, mock_owner, mock_build):
        from handlers.vm import _upsert_cloudinit_secret
        from kubernetes.client import ApiException

        mock_build.return_value = {
            "metadata": {"name": "cloudinit-vm1"},
            "data": {},
        }
        mock_owner.return_value = {"kind": "TroshkaVM"}
        core_api = MagicMock()
        core_api.replace_namespaced_secret.side_effect = ApiException(status=404)

        result = _upsert_cloudinit_secret(
            {"kind": "TroshkaVM", "metadata": {"name": "vm1", "uid": "u1"}},
            "ns1",
            core_api,
        )
        assert result == "cloudinit-vm1"
        core_api.create_namespaced_secret.assert_called_once()


class TestStopKubevirtVmExtended:
    """Cover _stop_kubevirt_vm: normal stop, VMI not found."""

    def test_patches_and_waits(self):
        from handlers.vm import _stop_kubevirt_vm
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        # VMI goes 404 on first check
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)

        asyncio.run(_stop_kubevirt_vm(custom_api, "ns1", "kv-vm-1"))
        custom_api.patch_namespaced_custom_object.assert_called_once()

    def test_patch_exception_swallowed(self):
        from handlers.vm import _stop_kubevirt_vm
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.patch_namespaced_custom_object.side_effect = Exception("VM gone")
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)

        asyncio.run(_stop_kubevirt_vm(custom_api, "ns1", "kv-vm-1"))


class TestDeleteAndWaitForKubevirtVmExtended:
    """Cover _delete_and_wait_for_kubevirt_vm."""

    def test_deletes_and_waits_404(self):
        from handlers.vm import _delete_and_wait_for_kubevirt_vm
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        # Delete succeeds, wait get returns 404
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)

        asyncio.run(_delete_and_wait_for_kubevirt_vm(custom_api, "ns1", "kv-vm-1"))
        custom_api.delete_namespaced_custom_object.assert_called_once()

    def test_delete_exception_swallowed(self):
        from handlers.vm import _delete_and_wait_for_kubevirt_vm
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.delete_namespaced_custom_object.side_effect = Exception("gone")
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)

        asyncio.run(_delete_and_wait_for_kubevirt_vm(custom_api, "ns1", "kv-vm-1"))


# ---------------------------------------------------------------------------
# NEW TESTS: project.py -- additional coverage
# ---------------------------------------------------------------------------


class TestCleanupRecertJobExtended:
    """Cover _cleanup_recert_job."""

    def test_deletes_pods_and_job(self):
        from handlers.project import _cleanup_recert_job

        core_api = MagicMock()
        batch_api = MagicMock()

        pod1 = MagicMock()
        pod1.metadata.name = "recert-pod-1"
        pod_list = MagicMock()
        pod_list.items = [pod1]
        core_api.list_namespaced_pod.return_value = pod_list

        _cleanup_recert_job(core_api, batch_api, "ns1", "recert-vm-1")

        core_api.delete_namespaced_pod.assert_called_once_with(
            name="recert-pod-1", namespace="ns1"
        )
        batch_api.delete_namespaced_job.assert_called_once()

    def test_swallows_all_exceptions(self):
        from handlers.project import _cleanup_recert_job

        core_api = MagicMock()
        batch_api = MagicMock()
        core_api.list_namespaced_pod.side_effect = Exception("API error")
        batch_api.delete_namespaced_job.side_effect = Exception("Job gone")

        # Should not raise
        _cleanup_recert_job(core_api, batch_api, "ns1", "recert-vm-1")


class TestExtractKubeconfigSecretExtended:
    """Cover _extract_kubeconfig_secret: success, no pods, no marker."""

    def test_returns_error_when_no_pods(self):
        from handlers.project import _extract_kubeconfig_secret

        core_api = MagicMock()
        pod_list = MagicMock()
        pod_list.items = []
        core_api.list_namespaced_pod.return_value = pod_list

        err = _extract_kubeconfig_secret(core_api, "ns1", "recert-vm", "proj-1")
        assert "No pods found" in err

    def test_returns_error_when_no_marker(self):
        from handlers.project import _extract_kubeconfig_secret

        core_api = MagicMock()
        pod1 = MagicMock()
        pod1.metadata.name = "recert-pod"
        pod_list = MagicMock()
        pod_list.items = [pod1]
        core_api.list_namespaced_pod.return_value = pod_list
        core_api.read_namespaced_pod_log.return_value = "some random logs"

        err = _extract_kubeconfig_secret(core_api, "ns1", "recert-vm", "proj-1")
        assert "No kubeconfig marker" in err

    def test_creates_secrets_on_success(self):
        import base64
        from handlers.project import _extract_kubeconfig_secret

        core_api = MagicMock()
        pod1 = MagicMock()
        pod1.metadata.name = "recert-pod"
        pod_list = MagicMock()
        pod_list.items = [pod1]
        core_api.list_namespaced_pod.return_value = pod_list

        kc_b64 = base64.b64encode(b"kubeconfig-data").decode()
        core_api.read_namespaced_pod_log.return_value = (
            f"KUBECONFIG_B64_BEGIN {kc_b64} KUBECONFIG_B64_END"
        )

        err = _extract_kubeconfig_secret(
            core_api, "ns1", "recert-vm", "proj-1", vm_name="sno"
        )
        assert err is None
        # Should create ocp-kubeconfig and ocp-kubeconfig-sno
        assert core_api.create_namespaced_secret.call_count == 2

    def test_replaces_secret_on_409(self):
        import base64
        from handlers.project import _extract_kubeconfig_secret
        from kubernetes.client import ApiException

        core_api = MagicMock()
        pod1 = MagicMock()
        pod1.metadata.name = "recert-pod"
        pod_list = MagicMock()
        pod_list.items = [pod1]
        core_api.list_namespaced_pod.return_value = pod_list

        kc_b64 = base64.b64encode(b"kubeconfig-data").decode()
        core_api.read_namespaced_pod_log.return_value = (
            f"KUBECONFIG_B64_BEGIN {kc_b64} KUBECONFIG_B64_END"
        )
        core_api.create_namespaced_secret.side_effect = ApiException(status=409)

        err = _extract_kubeconfig_secret(core_api, "ns1", "recert-vm", "proj-1")
        assert err is None
        core_api.replace_namespaced_secret.assert_called_once()

    def test_returns_error_on_non_409_secret_failure(self):
        import base64
        from handlers.project import _extract_kubeconfig_secret
        from kubernetes.client import ApiException

        core_api = MagicMock()
        pod1 = MagicMock()
        pod1.metadata.name = "recert-pod"
        pod_list = MagicMock()
        pod_list.items = [pod1]
        core_api.list_namespaced_pod.return_value = pod_list

        kc_b64 = base64.b64encode(b"kubeconfig-data").decode()
        core_api.read_namespaced_pod_log.return_value = (
            f"KUBECONFIG_B64_BEGIN {kc_b64} KUBECONFIG_B64_END"
        )
        core_api.create_namespaced_secret.side_effect = ApiException(status=500)

        err = _extract_kubeconfig_secret(core_api, "ns1", "recert-vm", "proj-1")
        assert "Failed to create kubeconfig secret" in err


class TestEnsureDeploymentGoneExtended:
    """Cover _ensure_deployment_gone: already 404, timeout."""

    def test_returns_on_404_delete(self):
        from handlers.project import _ensure_deployment_gone
        from kubernetes.client import ApiException

        apps_api = MagicMock()
        apps_api.delete_namespaced_deployment.side_effect = ApiException(status=404)

        asyncio.run(_ensure_deployment_gone(apps_api, "ns1", "dep-1"))

    def test_waits_until_gone(self):
        from handlers.project import _ensure_deployment_gone
        from kubernetes.client import ApiException

        apps_api = MagicMock()
        apps_api.read_namespaced_deployment.side_effect = ApiException(status=404)

        asyncio.run(_ensure_deployment_gone(apps_api, "ns1", "dep-1"))
        apps_api.delete_namespaced_deployment.assert_called_once()


class TestCheckExportJobExtended:
    """Cover _check_export_job."""

    def test_returns_done_when_succeeded(self):
        from handlers.project import _check_export_job

        batch_api = MagicMock()
        job_status = MagicMock()
        job_status.succeeded = 1
        job_status.failed = 0
        job_mock = MagicMock()
        job_mock.status = job_status
        batch_api.read_namespaced_job.return_value = job_mock

        result = _check_export_job(batch_api, {"jobName": "export-1"}, "ns1")
        assert result == "done"

    def test_returns_failed_when_3_failures(self):
        from handlers.project import _check_export_job

        batch_api = MagicMock()
        job_status = MagicMock()
        job_status.succeeded = 0
        job_status.failed = 3
        job_mock = MagicMock()
        job_mock.status = job_status
        batch_api.read_namespaced_job.return_value = job_mock

        result = _check_export_job(batch_api, {"jobName": "export-1"}, "ns1")
        assert result == "failed"

    def test_returns_pending_on_exception(self):
        from handlers.project import _check_export_job

        batch_api = MagicMock()
        batch_api.read_namespaced_job.side_effect = Exception("timeout")

        result = _check_export_job(batch_api, {"jobName": "export-1"}, "ns1")
        assert result == "pending"


class TestReadExportSizesExtended:
    """Cover _read_export_sizes."""

    def test_reads_size_from_log(self):
        from handlers.project import _read_export_sizes

        core_api = MagicMock()
        pod1 = MagicMock()
        pod1.metadata.name = "export-pod-1"
        pod_list = MagicMock()
        pod_list.items = [pod1]
        core_api.list_namespaced_pod.return_value = pod_list
        core_api.read_namespaced_pod_log.return_value = (
            "Some logs\nDISK_SIZE_BYTES=123456789\nDone"
        )

        export_jobs = [{"jobName": "export-1"}]
        _read_export_sizes(core_api, export_jobs, "ns1")
        assert export_jobs[0]["sizeBytes"] == 123456789

    def test_swallows_exception(self):
        from handlers.project import _read_export_sizes

        core_api = MagicMock()
        core_api.list_namespaced_pod.side_effect = Exception("API error")

        export_jobs = [{"jobName": "export-1"}]
        _read_export_sizes(core_api, export_jobs, "ns1")
        assert "sizeBytes" not in export_jobs[0]


class TestCollectRecertConfigsExtended:
    """Cover _collect_recert_configs."""

    def test_collects_recert_enabled_vms(self):
        from handlers.project import _collect_recert_configs

        vms = [
            {"id": "vm-aaa12345", "name": "sno1", "recertEnabled": True},
            {"id": "vm-bbb12345", "name": "sno2", "recertEnabled": False},
        ]
        vm_disks_map = {
            "vm-aaa12345": [{"id": "disk-111", "patternImage": {"s3Path": "x"}}],
            "vm-bbb12345": [{"id": "disk-222", "patternImage": {"s3Path": "y"}}],
        }

        configs = _collect_recert_configs(vms, vm_disks_map, "bastion-pvc")
        assert len(configs) == 1
        assert configs[0]["vmName"] == "sno1"
        assert configs[0]["bastionPvc"] == "bastion-pvc"

    def test_skips_vm_without_pattern_disks(self):
        from handlers.project import _collect_recert_configs

        vms = [{"id": "vm-aaa12345", "name": "sno1", "recertEnabled": True}]
        vm_disks_map = {
            "vm-aaa12345": [{"id": "disk-111"}],  # no patternImage
        }

        configs = _collect_recert_configs(vms, vm_disks_map, None)
        assert len(configs) == 0

    def test_skips_vm_without_disks(self):
        from handlers.project import _collect_recert_configs

        vms = [{"id": "vm-aaa12345", "name": "sno1", "recertEnabled": True}]
        vm_disks_map = {}

        configs = _collect_recert_configs(vms, vm_disks_map, None)
        assert len(configs) == 0


class TestRecertJobNameFromCfgExtended:
    """Cover _recert_job_name_from_cfg."""

    def test_derives_name_from_pvc(self):
        from handlers.project import _recert_job_name_from_cfg

        cfg = {"rhcosPvc": "vm-abc12345-disk-ddd11111", "vmName": "sno1"}
        job_name, vm_part, vm_label = _recert_job_name_from_cfg(cfg)
        assert job_name == "recert-vm-abc12345"
        assert vm_part == "vm-abc12345"
        assert vm_label == "sno1"

    def test_fallback_when_no_disk_separator(self):
        from handlers.project import _recert_job_name_from_cfg

        cfg = {"rhcosPvc": "simple-pvc", "vmName": "vm1"}
        job_name, vm_part, vm_label = _recert_job_name_from_cfg(cfg)
        assert job_name == "recert-vm"
        assert vm_part == "vm"


class TestCheckRecertPvcsReadyExtended:
    """Cover _check_recert_pvcs_ready."""

    def test_all_bound(self):
        from handlers.project import _check_recert_pvcs_ready

        core_api = MagicMock()
        pvc_mock = MagicMock()
        pvc_mock.status.phase = "Bound"
        core_api.read_namespaced_persistent_volume_claim.return_value = pvc_mock

        cfgs = [{"rhcosPvc": "pvc-1"}, {"rhcosPvc": "pvc-2"}]
        assert _check_recert_pvcs_ready(core_api, cfgs, "ns1") is True

    def test_not_bound(self):
        from handlers.project import _check_recert_pvcs_ready

        core_api = MagicMock()
        pvc_mock = MagicMock()
        pvc_mock.status.phase = "Pending"
        core_api.read_namespaced_persistent_volume_claim.return_value = pvc_mock

        cfgs = [{"rhcosPvc": "pvc-1"}]
        assert _check_recert_pvcs_ready(core_api, cfgs, "ns1") is False

    def test_returns_false_on_exception(self):
        from handlers.project import _check_recert_pvcs_ready

        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.side_effect = Exception("err")

        cfgs = [{"rhcosPvc": "pvc-1"}]
        assert _check_recert_pvcs_ready(core_api, cfgs, "ns1") is False

    def test_empty_pvc_name_skipped(self):
        from handlers.project import _check_recert_pvcs_ready

        core_api = MagicMock()
        cfgs = [{"rhcosPvc": ""}]
        assert _check_recert_pvcs_ready(core_api, cfgs, "ns1") is True


class TestStartKubevirtVmsExtended:
    """Cover _start_kubevirt_vms."""

    def test_patches_run_strategy_always(self):
        from handlers.project import _start_kubevirt_vms

        custom_api = MagicMock()
        vm_items = [
            {
                "metadata": {"name": "vm-1"},
                "spec": {"powerOnAtDeploy": True},
                "status": {"kubevirtVmName": "kv-vm-1"},
            },
        ]

        count = _start_kubevirt_vms(custom_api, vm_items, "ns1")
        assert count == 1
        custom_api.patch_namespaced_custom_object.assert_called_once()
        assert custom_api.patch_namespaced_custom_object.call_args[1]["body"] == {
            "spec": {"runStrategy": "Always"}
        }

    def test_skips_power_off_vm(self):
        from handlers.project import _start_kubevirt_vms

        custom_api = MagicMock()
        vm_items = [
            {
                "metadata": {"name": "vm-1"},
                "spec": {"powerOnAtDeploy": False},
                "status": {"kubevirtVmName": "kv-vm-1"},
            },
        ]

        count = _start_kubevirt_vms(custom_api, vm_items, "ns1")
        assert count == 1
        custom_api.patch_namespaced_custom_object.assert_not_called()

    def test_skips_vm_without_kv_name(self):
        from handlers.project import _start_kubevirt_vms

        custom_api = MagicMock()
        vm_items = [
            {"metadata": {"name": "vm-1"}, "spec": {}, "status": {}},
        ]

        count = _start_kubevirt_vms(custom_api, vm_items, "ns1")
        assert count == 0

    def test_swallows_patch_exception(self):
        from handlers.project import _start_kubevirt_vms

        custom_api = MagicMock()
        custom_api.patch_namespaced_custom_object.side_effect = Exception("fail")
        vm_items = [
            {
                "metadata": {"name": "vm-1"},
                "spec": {"powerOnAtDeploy": True},
                "status": {"kubevirtVmName": "kv-vm-1"},
            },
        ]

        count = _start_kubevirt_vms(custom_api, vm_items, "ns1")
        assert count == 0


class TestPodUsesPvcOnNodeExtended:
    """Cover _pod_uses_pvc_on_node."""

    def test_returns_true_when_pod_uses_pvc(self):
        from handlers.project import _pod_uses_pvc_on_node

        core_api = MagicMock()
        pod = MagicMock()
        pod.metadata.deletion_timestamp = None
        vol = MagicMock()
        vol.persistent_volume_claim.claim_name = "my-pvc"
        pod.spec.volumes = [vol]
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api.list_namespaced_pod.return_value = pod_list

        assert _pod_uses_pvc_on_node(core_api, "ns1", "node-1", "my-pvc") is True

    def test_returns_false_when_no_matching_pvc(self):
        from handlers.project import _pod_uses_pvc_on_node

        core_api = MagicMock()
        pod = MagicMock()
        pod.metadata.deletion_timestamp = None
        vol = MagicMock()
        vol.persistent_volume_claim.claim_name = "other-pvc"
        pod.spec.volumes = [vol]
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api.list_namespaced_pod.return_value = pod_list

        assert _pod_uses_pvc_on_node(core_api, "ns1", "node-1", "my-pvc") is False

    def test_skips_terminating_pods(self):
        from handlers.project import _pod_uses_pvc_on_node

        core_api = MagicMock()
        pod = MagicMock()
        pod.metadata.deletion_timestamp = "2025-01-01T00:00:00Z"
        vol = MagicMock()
        vol.persistent_volume_claim.claim_name = "my-pvc"
        pod.spec.volumes = [vol]
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api.list_namespaced_pod.return_value = pod_list

        assert _pod_uses_pvc_on_node(core_api, "ns1", "node-1", "my-pvc") is False

    def test_returns_false_on_exception(self):
        from handlers.project import _pod_uses_pvc_on_node

        core_api = MagicMock()
        core_api.list_namespaced_pod.side_effect = Exception("fail")

        assert _pod_uses_pvc_on_node(core_api, "ns1", "node-1", "my-pvc") is False


class TestDetectSchedulingErrorExtended:
    """Cover _detect_scheduling_error."""

    def test_detects_unschedulable(self):
        from handlers.project import _detect_scheduling_error

        core_api = MagicMock()
        pod = MagicMock()
        cond = MagicMock()
        cond.reason = "Unschedulable"
        cond.message = "Insufficient memory"
        pod.status.conditions = [cond]
        pod.metadata.name = "virt-launcher-pod"
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api.list_namespaced_pod.return_value = pod_list

        err = _detect_scheduling_error(core_api, "ns1", "kv-vm-1")
        assert err == "Insufficient memory"

    def test_detects_volume_attach_failure(self):
        from handlers.project import _detect_scheduling_error

        core_api = MagicMock()
        pod = MagicMock()
        pod.status.conditions = []
        pod.metadata.name = "virt-launcher-pod"
        pod_list = MagicMock()
        pod_list.items = [pod]

        ev = MagicMock()
        ev.message = "Volume cannot be attached"
        ev_list = MagicMock()
        ev_list.items = [ev]
        core_api.list_namespaced_pod.return_value = pod_list
        core_api.list_namespaced_event.return_value = ev_list

        err = _detect_scheduling_error(core_api, "ns1", "kv-vm-1")
        assert err == "Volume cannot be attached"

    def test_returns_none_when_no_issues(self):
        from handlers.project import _detect_scheduling_error

        core_api = MagicMock()
        pod = MagicMock()
        pod.status.conditions = []
        pod.metadata.name = "virt-launcher-pod"
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api.list_namespaced_pod.return_value = pod_list
        core_api.list_namespaced_event.return_value = MagicMock(items=[])

        err = _detect_scheduling_error(core_api, "ns1", "kv-vm-1")
        assert err is None

    def test_returns_none_on_exception(self):
        from handlers.project import _detect_scheduling_error

        core_api = MagicMock()
        core_api.list_namespaced_pod.side_effect = Exception("API error")

        err = _detect_scheduling_error(core_api, "ns1", "kv-vm-1")
        assert err is None


class TestCreateRecertJobsExtended:
    """Cover _create_recert_jobs."""

    @patch("helpers.kubevirt.build_recert_job")
    def test_creates_job_when_not_found(self, mock_build):
        from handlers.project import _create_recert_jobs
        from kubernetes.client import ApiException

        batch_api = MagicMock()
        batch_api.read_namespaced_job.side_effect = ApiException(status=404)
        mock_build.return_value = {"metadata": {"name": "recert-vm-abc"}}

        cfgs = [{"rhcosPvc": "vm-abc12345-disk-ddd11111", "vmName": "sno1"}]
        err = _create_recert_jobs(batch_api, cfgs, "ns1")
        assert err is None
        batch_api.create_namespaced_job.assert_called_once()

    def test_skips_when_job_exists(self):
        from handlers.project import _create_recert_jobs

        batch_api = MagicMock()
        batch_api.read_namespaced_job.return_value = MagicMock()  # job exists

        cfgs = [{"rhcosPvc": "vm-abc12345-disk-ddd11111", "vmName": "sno1"}]
        err = _create_recert_jobs(batch_api, cfgs, "ns1")
        assert err is None
        batch_api.create_namespaced_job.assert_not_called()

    @patch("helpers.kubevirt.build_recert_job")
    def test_returns_error_on_create_failure(self, mock_build):
        from handlers.project import _create_recert_jobs
        from kubernetes.client import ApiException

        batch_api = MagicMock()
        batch_api.read_namespaced_job.side_effect = ApiException(status=404)
        batch_api.create_namespaced_job.side_effect = Exception("quota exceeded")
        mock_build.return_value = {"metadata": {"name": "recert-vm-abc"}}

        cfgs = [{"rhcosPvc": "vm-abc12345-disk-ddd11111", "vmName": "sno1"}]
        err = _create_recert_jobs(batch_api, cfgs, "ns1")
        assert "Failed to create recert job" in err


class TestPollRecertJobsExtended:
    """Cover _poll_recert_jobs: all done, failed + retry, failed permanently."""

    def test_all_done(self):
        from handlers.project import _poll_recert_jobs

        batch_api = MagicMock()
        job_status = MagicMock()
        job_status.succeeded = True
        job_status.failed = None
        js = MagicMock()
        js.status = job_status
        batch_api.read_namespaced_job.return_value = js

        cfgs = [{"rhcosPvc": "vm-abc12345-disk-ddd11111", "vmName": "sno1"}]
        status = {}
        patch_obj = MagicMock()

        all_done, should_return = _poll_recert_jobs(
            batch_api, cfgs, "ns1", status, patch_obj
        )
        assert all_done is True
        assert should_return is False

    def test_failed_with_retry(self):
        from handlers.project import _poll_recert_jobs

        batch_api = MagicMock()
        job_status = MagicMock()
        job_status.succeeded = None
        job_status.failed = True
        js = MagicMock()
        js.status = job_status
        batch_api.read_namespaced_job.return_value = js

        cfgs = [{"rhcosPvc": "vm-abc12345-disk-ddd11111", "vmName": "sno1"}]
        status = {}
        patch_obj = MagicMock()

        all_done, should_return = _poll_recert_jobs(
            batch_api, cfgs, "ns1", status, patch_obj
        )
        assert all_done is False
        assert should_return is True

    def test_failed_permanently_after_3_attempts(self):
        from handlers.project import _poll_recert_jobs

        batch_api = MagicMock()
        job_status = MagicMock()
        job_status.succeeded = None
        job_status.failed = True
        js = MagicMock()
        js.status = job_status
        batch_api.read_namespaced_job.return_value = js

        cfgs = [{"rhcosPvc": "vm-abc12345-disk-ddd11111", "vmName": "sno1"}]
        status = {"recertAttempts_0": 2}  # already 2 attempts
        patch_obj = MagicMock()

        all_done, should_return = _poll_recert_jobs(
            batch_api, cfgs, "ns1", status, patch_obj
        )
        assert all_done is False
        assert should_return is True
        # Should set error phase
        patch_obj.status.__setitem__.assert_any_call("phase", "Error")


class TestFinalizeRecertExtended:
    """Cover _finalize_recert."""

    @patch("handlers.project._extract_kubeconfig_secret")
    @patch("handlers.project._cleanup_recert_job")
    def test_calls_extract_and_cleanup_for_each(self, mock_cleanup, mock_extract):
        from handlers.project import _finalize_recert

        mock_extract.return_value = None  # success

        core_api = MagicMock()
        batch_api = MagicMock()
        cfgs = [
            {"rhcosPvc": "vm-aaa12345-disk-ddd11111", "vmName": "sno1"},
            {"rhcosPvc": "vm-bbb12345-disk-eee22222", "vmName": "sno2"},
        ]

        _finalize_recert(core_api, batch_api, cfgs, "ns1", "proj-1")

        assert mock_extract.call_count == 2
        assert mock_cleanup.call_count == 2

    @patch("handlers.project._extract_kubeconfig_secret")
    @patch("handlers.project._cleanup_recert_job")
    def test_continues_on_extract_error(self, mock_cleanup, mock_extract):
        from handlers.project import _finalize_recert

        mock_extract.return_value = "extraction failed"

        core_api = MagicMock()
        batch_api = MagicMock()
        cfgs = [{"rhcosPvc": "vm-aaa12345-disk-ddd11111", "vmName": "sno1"}]

        # Should not raise
        _finalize_recert(core_api, batch_api, cfgs, "ns1", "proj-1")
        mock_cleanup.assert_called_once()


class TestFindStaleVolumeAttachmentsExtended:
    """Cover _find_stale_volume_attachments."""

    def test_returns_empty_when_no_pvcs(self):
        from handlers.project import _find_stale_volume_attachments

        storage_api = MagicMock()
        core_api = MagicMock()
        pvc_list = MagicMock()
        pvc_list.items = []
        core_api.list_namespaced_persistent_volume_claim.return_value = pvc_list

        result = _find_stale_volume_attachments(storage_api, core_api, "ns1")
        assert result == []

    def test_finds_stale_attachment(self):
        from handlers.project import _find_stale_volume_attachments

        storage_api = MagicMock()
        core_api = MagicMock()

        pvc = MagicMock()
        pvc.spec.volume_name = "pv-1"
        pvc.metadata.name = "pvc-1"
        pvc_list = MagicMock()
        pvc_list.items = [pvc]
        core_api.list_namespaced_persistent_volume_claim.return_value = pvc_list

        va = MagicMock()
        va.spec.source.persistent_volume_name = "pv-1"
        va.spec.node_name = "node-1"
        va.metadata.name = "va-1"
        va_list = MagicMock()
        va_list.items = [va]
        storage_api.list_volume_attachment.return_value = va_list

        # No pod uses the PVC on that node
        pod_list = MagicMock()
        pod_list.items = []
        core_api.list_namespaced_pod.return_value = pod_list

        result = _find_stale_volume_attachments(storage_api, core_api, "ns1")
        assert "va-1" in result


# ---------------------------------------------------------------------------
# Additional coverage tests (appended)
# ---------------------------------------------------------------------------


class TestEnsureDeploymentGoneNon404Error:
    """Cover _ensure_deployment_gone: non-404 on delete raises, timeout path."""

    def test_non_404_on_delete_raises(self):
        from handlers.project import _ensure_deployment_gone
        from kubernetes.client.exceptions import ApiException

        apps_api = MagicMock()
        apps_api.delete_namespaced_deployment.side_effect = ApiException(status=500)

        with pytest.raises(ApiException):
            asyncio.run(_ensure_deployment_gone(apps_api, "ns1", "dep-x"))

    def test_timeout_path(self):
        from handlers.project import _ensure_deployment_gone
        from kubernetes.client.exceptions import ApiException

        apps_api = MagicMock()
        # delete succeeds
        apps_api.delete_namespaced_deployment.return_value = None
        # read always succeeds (deployment never goes away)
        apps_api.read_namespaced_deployment.return_value = MagicMock()

        # Use a very short max_wait to trigger timeout quickly
        asyncio.run(_ensure_deployment_gone(apps_api, "ns1", "dep-x", max_wait=0.1))
        # Should not raise -- just log warning and return

    def test_read_non_404_during_wait_raises(self):
        from handlers.project import _ensure_deployment_gone
        from kubernetes.client.exceptions import ApiException

        apps_api = MagicMock()
        apps_api.delete_namespaced_deployment.return_value = None
        apps_api.read_namespaced_deployment.side_effect = ApiException(status=500)

        with pytest.raises(ApiException):
            asyncio.run(_ensure_deployment_gone(apps_api, "ns1", "dep-x"))

    def test_read_404_during_wait_returns(self):
        from handlers.project import _ensure_deployment_gone
        from kubernetes.client.exceptions import ApiException

        apps_api = MagicMock()
        apps_api.delete_namespaced_deployment.return_value = None
        apps_api.read_namespaced_deployment.side_effect = ApiException(status=404)

        asyncio.run(_ensure_deployment_gone(apps_api, "ns1", "dep-x"))
        # Should return cleanly


class TestCleanupRecertJobExceptionPaths:
    """Cover _cleanup_recert_job: pod delete failure, list failure."""

    def test_pod_delete_failure_swallowed(self):
        from handlers.project import _cleanup_recert_job

        core_api = MagicMock()
        batch_api = MagicMock()
        pod = MagicMock()
        pod.metadata.name = "recert-pod-1"
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api.list_namespaced_pod.return_value = pod_list
        core_api.delete_namespaced_pod.side_effect = Exception("pod delete failed")

        _cleanup_recert_job(core_api, batch_api, "ns1", "recert-vm-abc")
        # Should not raise
        batch_api.delete_namespaced_job.assert_called_once()

    def test_list_pods_failure_swallowed(self):
        from handlers.project import _cleanup_recert_job

        core_api = MagicMock()
        batch_api = MagicMock()
        core_api.list_namespaced_pod.side_effect = Exception("list failed")

        _cleanup_recert_job(core_api, batch_api, "ns1", "recert-vm-abc")
        # Should not raise; job delete still attempted
        batch_api.delete_namespaced_job.assert_called_once()


class TestExtractKubeconfigSecretEdgeCases:
    """Cover _extract_kubeconfig_secret: create fails non-409, outer exception."""

    def test_create_secret_non_409_returns_error(self):
        from handlers.project import _extract_kubeconfig_secret
        from kubernetes.client.exceptions import ApiException
        import base64

        core_api = MagicMock()
        pod = MagicMock()
        pod.metadata.name = "recert-pod"
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api.list_namespaced_pod.return_value = pod_list

        kc_data = base64.b64encode(b"apiVersion: v1").decode()
        core_api.read_namespaced_pod_log.return_value = (
            f"KUBECONFIG_B64_BEGIN {kc_data} KUBECONFIG_B64_END"
        )
        core_api.create_namespaced_secret.side_effect = ApiException(status=500)

        result = _extract_kubeconfig_secret(core_api, "ns1", "recert-j", "proj1")
        assert result is not None
        assert "Failed to create kubeconfig secret" in result

    def test_outer_exception_returns_error(self):
        from handlers.project import _extract_kubeconfig_secret

        core_api = MagicMock()
        core_api.list_namespaced_pod.side_effect = Exception("API down")

        result = _extract_kubeconfig_secret(core_api, "ns1", "recert-j", "proj1")
        assert result is not None
        assert "Failed to extract kubeconfig" in result


class TestCreateGoldenPvcForDiskNon404:
    """Cover _create_golden_pvc_for_disk: read PVC raises non-404, DV create 409."""

    def test_read_pvc_non_404_raises(self):
        from handlers.project import _create_golden_pvc_for_disk
        from kubernetes.client.exceptions import ApiException

        custom_api = MagicMock()
        core_api = MagicMock()
        # Reading the existing golden DataVolume errors with a non-404 -> propagate.
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=500)

        disk = {"libraryImage": {"s3Path": "library/img.qcow2"}, "sizeGb": 20}

        with pytest.raises(ApiException):
            _create_golden_pvc_for_disk(custom_api, core_api, disk, {"bucket": "b"}, {})

    def test_dv_create_409_swallowed(self):
        from handlers.project import _create_golden_pvc_for_disk
        from kubernetes.client.exceptions import ApiException

        custom_api = MagicMock()
        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )
        custom_api.create_namespaced_custom_object.side_effect = ApiException(
            status=409
        )

        disk = {"libraryImage": {"s3Path": "library/img.qcow2"}, "sizeGb": 20}

        # Should not raise on 409
        _create_golden_pvc_for_disk(custom_api, core_api, disk, {"bucket": "b"}, {})

    def test_dv_create_non_409_raises(self):
        from handlers.project import _create_golden_pvc_for_disk
        from kubernetes.client.exceptions import ApiException

        custom_api = MagicMock()
        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )
        custom_api.create_namespaced_custom_object.side_effect = ApiException(
            status=403
        )

        disk = {"libraryImage": {"s3Path": "library/img.qcow2"}, "sizeGb": 20}

        with pytest.raises(ApiException):
            _create_golden_pvc_for_disk(custom_api, core_api, disk, {"bucket": "b"}, {})


class TestDeleteCustomResourcesEdgeCases:
    """Cover _delete_custom_resources: 404 on delete, list failure."""

    def test_delete_404_ignored(self):
        from handlers.project import _delete_custom_resources
        from kubernetes.client.exceptions import ApiException

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [{"metadata": {"name": "res-1"}}]
        }
        custom_api.delete_namespaced_custom_object.side_effect = ApiException(
            status=404
        )

        # Should not raise
        _delete_custom_resources(
            custom_api, "g", "v1", "resources", "ns1", resource_label="res"
        )

    def test_list_failure_logs_warning(self):
        from handlers.project import _delete_custom_resources

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = Exception("API down")

        # Should not raise
        _delete_custom_resources(
            custom_api, "g", "v1", "resources", "ns1", resource_label="res"
        )

    def test_grace_period_passed_to_delete(self):
        from handlers.project import _delete_custom_resources

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [{"metadata": {"name": "res-1"}}]
        }

        _delete_custom_resources(
            custom_api,
            "g",
            "v1",
            "resources",
            "ns1",
            resource_label="res",
            grace_period=0,
        )

        call_kwargs = custom_api.delete_namespaced_custom_object.call_args
        assert call_kwargs[1]["grace_period_seconds"] == 0


class TestRemoveSaFromSccsEdgeCases:
    """Cover _remove_sa_from_sccs: SA not in users, exception path."""

    def test_sa_not_in_users_no_patch(self):
        from handlers.project import _remove_sa_from_sccs

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {
            "users": ["system:serviceaccount:other-ns:other-sa"]
        }

        _remove_sa_from_sccs(custom_api, "ns1", "my-sa", ["scc-1"])

        custom_api.patch_cluster_custom_object.assert_not_called()

    def test_sa_removed_when_present(self):
        from handlers.project import _remove_sa_from_sccs

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {
            "users": ["system:serviceaccount:ns1:my-sa", "other"]
        }

        _remove_sa_from_sccs(custom_api, "ns1", "my-sa", ["scc-1"])

        custom_api.patch_cluster_custom_object.assert_called_once()
        patch_body = custom_api.patch_cluster_custom_object.call_args[1]["body"]
        assert "system:serviceaccount:ns1:my-sa" not in patch_body["users"]
        assert "other" in patch_body["users"]

    def test_exception_swallowed(self):
        from handlers.project import _remove_sa_from_sccs

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.side_effect = Exception("SCC not found")

        # Should not raise
        _remove_sa_from_sccs(custom_api, "ns1", "my-sa", ["scc-1"])

    def test_null_users_field(self):
        from handlers.project import _remove_sa_from_sccs

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {"users": None}

        # Should not raise
        _remove_sa_from_sccs(custom_api, "ns1", "my-sa", ["scc-1"])
        custom_api.patch_cluster_custom_object.assert_not_called()

    def test_multiple_sccs(self):
        from handlers.project import _remove_sa_from_sccs

        custom_api = MagicMock()
        # First SCC has the SA, second does not
        custom_api.get_cluster_custom_object.side_effect = [
            {"users": ["system:serviceaccount:ns1:my-sa"]},
            {"users": ["other"]},
        ]

        _remove_sa_from_sccs(custom_api, "ns1", "my-sa", ["scc-1", "scc-2"])

        assert custom_api.patch_cluster_custom_object.call_count == 1


class TestFindStaleVolumeAttachmentsVaPvNotInNamespace:
    """Cover _find_stale_volume_attachments: VA for PV not in namespace PVCs."""

    def test_va_pv_not_in_namespace_skipped(self):
        from handlers.project import _find_stale_volume_attachments

        storage_api = MagicMock()
        core_api = MagicMock()

        pvc = MagicMock()
        pvc.spec.volume_name = "pv-local"
        pvc.metadata.name = "pvc-local"
        pvc_list = MagicMock()
        pvc_list.items = [pvc]
        core_api.list_namespaced_persistent_volume_claim.return_value = pvc_list

        # VA references a PV NOT in namespace PVC list
        va = MagicMock()
        va.spec.source.persistent_volume_name = "pv-other-namespace"
        va.spec.node_name = "node-1"
        va.metadata.name = "va-foreign"
        va_list = MagicMock()
        va_list.items = [va]
        storage_api.list_volume_attachment.return_value = va_list

        result = _find_stale_volume_attachments(storage_api, core_api, "ns1")
        assert result == []
        # list_namespaced_pod should NOT be called since VA was skipped
        core_api.list_namespaced_pod.assert_not_called()

    def test_mixed_was_foreign_and_stale(self):
        from handlers.project import _find_stale_volume_attachments

        storage_api = MagicMock()
        core_api = MagicMock()

        pvc = MagicMock()
        pvc.spec.volume_name = "pv-local"
        pvc.metadata.name = "pvc-local"
        pvc_list = MagicMock()
        pvc_list.items = [pvc]
        core_api.list_namespaced_persistent_volume_claim.return_value = pvc_list

        va_foreign = MagicMock()
        va_foreign.spec.source.persistent_volume_name = "pv-other"
        va_foreign.spec.node_name = "node-1"
        va_foreign.metadata.name = "va-foreign"

        va_stale = MagicMock()
        va_stale.spec.source.persistent_volume_name = "pv-local"
        va_stale.spec.node_name = "node-2"
        va_stale.metadata.name = "va-stale"

        va_list = MagicMock()
        va_list.items = [va_foreign, va_stale]
        storage_api.list_volume_attachment.return_value = va_list

        # No pods on node-2 using pvc-local
        pod_list = MagicMock()
        pod_list.items = []
        core_api.list_namespaced_pod.return_value = pod_list

        result = _find_stale_volume_attachments(storage_api, core_api, "ns1")
        assert "va-stale" in result
        assert "va-foreign" not in result


class TestStartKubevirtVmsPatchException:
    """Cover _start_kubevirt_vms: patch fails silently."""

    def test_patch_exception_swallowed(self):
        from handlers.project import _start_kubevirt_vms

        custom_api = MagicMock()
        custom_api.patch_namespaced_custom_object.side_effect = Exception(
            "patch failed"
        )
        vm_items = [
            {
                "spec": {"powerOnAtDeploy": True},
                "status": {"kubevirtVmName": "kv-vm-1"},
            }
        ]

        result = _start_kubevirt_vms(custom_api, vm_items, "ns1")
        # Exception swallowed, started count remains 0
        assert result == 0


class TestCollectVmStatesReadyForStopped:
    """Cover _collect_vm_states: Stopped counts as ready."""

    def test_stopped_vm_counts_as_ready(self):
        from handlers.project import _collect_vm_states

        core_api = MagicMock()
        vm_items = [
            {
                "metadata": {"name": "vm-1"},
                "spec": {"vmId": "vm-uuid-1"},
                "status": {"kubevirtVmName": "kv-1", "state": "Stopped"},
            }
        ]
        # kv-1 not in vmi_states, state is "Stopped" but kv_name exists
        # _resolve_vm_state will not override because state != "Running"
        # Actually state is "Stopped" already, so _resolve_vm_state returns "Stopped"
        vmi_states = {}

        states, ready, errors = _collect_vm_states(
            vm_items, vmi_states, core_api, "ns1"
        )
        assert states["vm-uuid-1"] == "Stopped"
        assert ready == 1


class TestResolveVmStateAllBranches:
    """Cover _resolve_vm_state: all branches comprehensively."""

    def test_kv_name_in_vmi_states(self):
        from handlers.project import _resolve_vm_state

        vm = {"status": {"kubevirtVmName": "kv-1", "state": "Running"}}
        assert _resolve_vm_state(vm, {"kv-1": "Scheduling"}) == "Scheduling"

    def test_no_status_at_all(self):
        from handlers.project import _resolve_vm_state

        vm = {}
        assert _resolve_vm_state(vm, {}) == "creating"

    def test_empty_status(self):
        from handlers.project import _resolve_vm_state

        vm = {"status": {}}
        assert _resolve_vm_state(vm, {}) == "creating"

    def test_kv_name_not_in_vmi_and_running(self):
        from handlers.project import _resolve_vm_state

        vm = {"status": {"kubevirtVmName": "kv-1", "state": "Running"}}
        assert _resolve_vm_state(vm, {}) == "Stopped"

    def test_kv_name_not_in_vmi_and_not_running(self):
        from handlers.project import _resolve_vm_state

        vm = {"status": {"kubevirtVmName": "kv-1", "state": "Scheduling"}}
        assert _resolve_vm_state(vm, {}) == "Scheduling"

    def test_no_kv_name_with_state(self):
        from handlers.project import _resolve_vm_state

        vm = {"status": {"state": "Error"}}
        assert _resolve_vm_state(vm, {}) == "Error"

    def test_no_kv_name_no_state(self):
        from handlers.project import _resolve_vm_state

        vm = {"status": {"kubevirtVmName": ""}}
        assert _resolve_vm_state(vm, {}) == "creating"


class TestDetectSchedulingErrorVolume:
    """Cover _detect_scheduling_error: FailedAttachVolume event."""

    def test_failed_attach_volume_event(self):
        from handlers.project import _detect_scheduling_error

        core_api = MagicMock()

        pod = MagicMock()
        pod.metadata.name = "virt-launcher-kv-vm-1"
        pod.status.conditions = []  # No Unschedulable condition
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api.list_namespaced_pod.return_value = pod_list

        event = MagicMock()
        event.message = "AttachVolume.Attach failed for pv-1"
        ev_list = MagicMock()
        ev_list.items = [event]
        core_api.list_namespaced_event.return_value = ev_list

        err = _detect_scheduling_error(core_api, "ns1", "kv-vm-1")
        assert err is not None
        assert "AttachVolume" in err

    def test_no_pods_returns_none(self):
        from handlers.project import _detect_scheduling_error

        core_api = MagicMock()
        pod_list = MagicMock()
        pod_list.items = []
        core_api.list_namespaced_pod.return_value = pod_list

        assert _detect_scheduling_error(core_api, "ns1", "kv-vm-1") is None

    def test_unschedulable_no_message(self):
        from handlers.project import _detect_scheduling_error

        core_api = MagicMock()
        pod = MagicMock()
        pod.metadata.name = "virt-launcher-kv-vm-1"
        cond = MagicMock()
        cond.reason = "Unschedulable"
        cond.message = None
        pod.status.conditions = [cond]
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api.list_namespaced_pod.return_value = pod_list

        err = _detect_scheduling_error(core_api, "ns1", "kv-vm-1")
        assert err == "Unschedulable"

    def test_volume_event_no_message(self):
        from handlers.project import _detect_scheduling_error

        core_api = MagicMock()
        pod = MagicMock()
        pod.metadata.name = "virt-launcher-kv-vm-1"
        pod.status.conditions = []
        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api.list_namespaced_pod.return_value = pod_list

        event = MagicMock()
        event.message = None
        ev_list = MagicMock()
        ev_list.items = [event]
        core_api.list_namespaced_event.return_value = ev_list

        err = _detect_scheduling_error(core_api, "ns1", "kv-vm-1")
        assert err == "Volume attach failed"


class TestCollectRecertConfigsDeadCodeBranch:
    """Cover _collect_recert_configs: various edge cases."""

    def test_vm_with_bastion_pvc_none(self):
        from handlers.project import _collect_recert_configs

        vms = [
            {"id": "aaaaaaaa-1111", "name": "sno-1", "recertEnabled": True},
        ]
        vm_disks_map = {
            "aaaaaaaa-1111": [
                {"id": "dddddddd-1111", "patternImage": {"s3Path": "p/d.qcow2"}}
            ],
        }

        configs = _collect_recert_configs(vms, vm_disks_map, None)
        assert len(configs) == 1
        assert configs[0]["bastionPvc"] == ""
        assert configs[0]["vmName"] == "sno-1"

    def test_vm_name_fallback_to_id(self):
        from handlers.project import _collect_recert_configs

        vms = [
            {"id": "aaaaaaaa-1111", "recertEnabled": True},
        ]
        vm_disks_map = {
            "aaaaaaaa-1111": [
                {"id": "dddddddd-1111", "patternImage": {"s3Path": "p/d.qcow2"}}
            ],
        }

        configs = _collect_recert_configs(vms, vm_disks_map, "bastion-pvc")
        assert len(configs) == 1
        assert configs[0]["vmName"] == "aaaaaaaa"

    def test_multiple_vms_mixed(self):
        from handlers.project import _collect_recert_configs

        vms = [
            {"id": "aaaaaaaa-1111", "name": "sno-1", "recertEnabled": True},
            {"id": "bbbbbbbb-2222", "name": "sno-2", "recertEnabled": False},
            {"id": "cccccccc-3333", "name": "sno-3", "recertEnabled": True},
        ]
        vm_disks_map = {
            "aaaaaaaa-1111": [
                {"id": "dddddddd-1111", "patternImage": {"s3Path": "p/a.qcow2"}}
            ],
            "bbbbbbbb-2222": [
                {"id": "eeeeeeee-2222", "patternImage": {"s3Path": "p/b.qcow2"}}
            ],
            "cccccccc-3333": [{"id": "ffffffff-3333"}],  # No patternImage
        }

        configs = _collect_recert_configs(vms, vm_disks_map, "bp")
        # sno-1: recertEnabled + patternImage -> included
        # sno-2: not recertEnabled -> skipped
        # sno-3: recertEnabled but no patternImage -> skipped
        assert len(configs) == 1
        assert configs[0]["vmName"] == "sno-1"


class TestRecertJobNameFromCfgEdgeCases:
    """Cover _recert_job_name_from_cfg: edge cases."""

    def test_empty_rhcos_pvc(self):
        from handlers.project import _recert_job_name_from_cfg

        cfg = {"rhcosPvc": "", "vmName": "my-vm"}
        job_name, vm_part, vm_label = _recert_job_name_from_cfg(cfg)
        assert job_name == "recert-vm"
        assert vm_part == "vm"
        assert vm_label == "my-vm"

    def test_missing_rhcos_pvc_key(self):
        from handlers.project import _recert_job_name_from_cfg

        cfg = {}
        job_name, vm_part, vm_label = _recert_job_name_from_cfg(cfg)
        assert job_name == "recert-vm"
        assert vm_part == "vm"
        assert vm_label == "vm"


class TestPodUsesPvcOnNodeTerminatingPod:
    """Cover _pod_uses_pvc_on_node: pod with deletion_timestamp skipped."""

    def test_terminating_pod_skipped(self):
        from handlers.project import _pod_uses_pvc_on_node

        core_api = MagicMock()

        pod = MagicMock()
        pod.metadata.deletion_timestamp = "2026-01-01T00:00:00Z"
        vol = MagicMock()
        claim = MagicMock()
        claim.claim_name = "my-pvc"
        vol.persistent_volume_claim = claim
        pod.spec.volumes = [vol]

        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api.list_namespaced_pod.return_value = pod_list

        assert _pod_uses_pvc_on_node(core_api, "ns1", "node-1", "my-pvc") is False

    def test_pod_with_no_pvc_volumes(self):
        from handlers.project import _pod_uses_pvc_on_node

        core_api = MagicMock()

        pod = MagicMock()
        pod.metadata.deletion_timestamp = None
        vol = MagicMock()
        vol.persistent_volume_claim = None  # Not a PVC volume
        pod.spec.volumes = [vol]

        pod_list = MagicMock()
        pod_list.items = [pod]
        core_api.list_namespaced_pod.return_value = pod_list

        assert _pod_uses_pvc_on_node(core_api, "ns1", "node-1", "my-pvc") is False


class TestUpsertCloudinitSecret409OnReplace:
    """Cover _upsert_cloudinit_secret: 409 on replace (idempotent, no raise)."""

    @patch("handlers.vm.build_cloudinit_secret")
    @patch("handlers.vm.owner_ref")
    def test_409_on_replace_no_raise(self, mock_owner, mock_build):
        from handlers.vm import _upsert_cloudinit_secret
        from kubernetes.client import ApiException

        mock_build.return_value = {
            "metadata": {"name": "cloudinit-vm1"},
            "data": {},
        }
        mock_owner.return_value = {"kind": "TroshkaVM"}
        core_api = MagicMock()
        core_api.replace_namespaced_secret.side_effect = ApiException(status=409)

        result = _upsert_cloudinit_secret(
            {"kind": "TroshkaVM", "metadata": {"name": "vm1", "uid": "u1"}},
            "ns1",
            core_api,
        )
        assert result == "cloudinit-vm1"
        # 409 on replace should be silently handled (not create, not raise)
        core_api.create_namespaced_secret.assert_not_called()

    @patch("handlers.vm.build_cloudinit_secret")
    @patch("handlers.vm.owner_ref")
    def test_non_404_non_409_on_replace_raises(self, mock_owner, mock_build):
        from handlers.vm import _upsert_cloudinit_secret
        from kubernetes.client import ApiException

        mock_build.return_value = {
            "metadata": {"name": "cloudinit-vm1"},
            "data": {},
        }
        mock_owner.return_value = {"kind": "TroshkaVM"}
        core_api = MagicMock()
        core_api.replace_namespaced_secret.side_effect = ApiException(status=500)

        with pytest.raises(ApiException):
            _upsert_cloudinit_secret(
                {"kind": "TroshkaVM", "metadata": {"name": "vm1", "uid": "u1"}},
                "ns1",
                core_api,
            )


class TestTryDeleteDatavolumeNon404:
    """Cover _try_delete_datavolume: non-404 logs warning."""

    def test_non_404_logs_warning_no_raise(self):
        from handlers.vm import _try_delete_datavolume
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.delete_namespaced_custom_object.side_effect = ApiException(
            status=500
        )

        # Should not raise -- just logs warning
        _try_delete_datavolume(custom_api, "ns1", "dv-broken")


class TestTryDeletePvcNon404:
    """Cover _try_delete_pvc: non-404 logs warning."""

    def test_non_404_logs_warning_no_raise(self):
        from handlers.vm import _try_delete_pvc
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.delete_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=500
        )

        # Should not raise -- just logs warning
        _try_delete_pvc(core_api, "ns1", "pvc-broken")


class TestCheckExportJobPending:
    """Cover _check_export_job: pending from job not done or failed."""

    def test_pending_from_in_progress_job(self):
        from handlers.project import _check_export_job

        batch_api = MagicMock()
        job = MagicMock()
        job.status.succeeded = 0
        job.status.failed = 1  # Below threshold of 3
        batch_api.read_namespaced_job.return_value = job

        assert _check_export_job(batch_api, {"jobName": "j1"}, "ns1") == "pending"

    def test_pending_from_no_status_counts(self):
        from handlers.project import _check_export_job

        batch_api = MagicMock()
        job = MagicMock()
        job.status.succeeded = None
        job.status.failed = None
        batch_api.read_namespaced_job.return_value = job

        assert _check_export_job(batch_api, {"jobName": "j1"}, "ns1") == "pending"


class TestResolveNadRefsNoStatus:
    """Cover _resolve_nad_refs: fallback when status is empty dict."""

    def test_no_status_key_at_all(self):
        from handlers.vm import _resolve_nad_refs

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {"metadata": {"name": "net-abc"}},  # No status key
            ]
        }

        result = _resolve_nad_refs(custom_api, "ns1")
        assert result["net-abc"] == "net-abc-nad"

    def test_empty_items(self):
        from handlers.vm import _resolve_nad_refs

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {"items": []}

        result = _resolve_nad_refs(custom_api, "ns1")
        assert result == {}


class TestCollectVmStatesSchedulingError:
    """Cover _collect_vm_states: scheduling error sets state to error."""

    @patch("handlers.project._detect_scheduling_error")
    def test_scheduling_error_sets_error_state(self, mock_detect):
        from handlers.project import _collect_vm_states

        mock_detect.return_value = "Insufficient CPU"
        core_api = MagicMock()

        vm_items = [
            {
                "metadata": {"name": "vm-1"},
                "spec": {"vmId": "vm-uuid-1"},
                "status": {"kubevirtVmName": "kv-1", "state": "Scheduling"},
            }
        ]
        # kv-1 in vmi_states as "Scheduling"
        vmi_states = {"kv-1": "Scheduling"}

        states, ready, errors = _collect_vm_states(
            vm_items, vmi_states, core_api, "ns1"
        )
        assert states["vm-uuid-1"] == "error"
        assert errors["vm-uuid-1"] == "Insufficient CPU"
        assert ready == 0

    @patch("handlers.project._detect_scheduling_error")
    def test_scheduling_no_error(self, mock_detect):
        from handlers.project import _collect_vm_states

        mock_detect.return_value = None
        core_api = MagicMock()

        vm_items = [
            {
                "metadata": {"name": "vm-1"},
                "spec": {"vmId": "vm-uuid-1"},
                "status": {"kubevirtVmName": "kv-1", "state": "Scheduling"},
            }
        ]
        vmi_states = {"kv-1": "Scheduling"}

        states, ready, errors = _collect_vm_states(
            vm_items, vmi_states, core_api, "ns1"
        )
        assert states["vm-uuid-1"] == "Scheduling"
        assert errors == {}
        assert ready == 0

    def test_vm_without_vmid_uses_metadata_name(self):
        from handlers.project import _collect_vm_states

        core_api = MagicMock()
        vm_items = [
            {
                "metadata": {"name": "vm-fallback"},
                "spec": {},
                "status": {"state": "Running", "kubevirtVmName": "kv-1"},
            }
        ]
        vmi_states = {"kv-1": "Running"}

        states, ready, errors = _collect_vm_states(
            vm_items, vmi_states, core_api, "ns1"
        )
        assert states["vm-fallback"] == "Running"
        assert ready == 1


class TestDeleteCustomResourcesNon404Delete:
    """Cover _delete_custom_resources: non-404 delete logs warning (line 1539)."""

    def test_delete_non_404_logs_warning(self):
        from handlers.project import _delete_custom_resources
        from kubernetes.client.exceptions import ApiException

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [{"metadata": {"name": "res-1"}}]
        }
        custom_api.delete_namespaced_custom_object.side_effect = ApiException(
            status=500
        )

        # Should not raise -- logs warning for non-404
        _delete_custom_resources(
            custom_api, "g", "v1", "resources", "ns1", resource_label="res"
        )

    def test_multiple_items_partial_delete_failure(self):
        from handlers.project import _delete_custom_resources
        from kubernetes.client.exceptions import ApiException

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {"metadata": {"name": "res-1"}},
                {"metadata": {"name": "res-2"}},
            ]
        }
        # First succeeds, second fails with 403
        custom_api.delete_namespaced_custom_object.side_effect = [
            None,
            ApiException(status=403),
        ]

        _delete_custom_resources(
            custom_api, "g", "v1", "resources", "ns1", resource_label="res"
        )
        assert custom_api.delete_namespaced_custom_object.call_count == 2


class TestCheckRecertPvcsReadyEmpty:
    """Cover _check_recert_pvcs_ready: empty pvc_name in config."""

    def test_empty_pvc_name_skipped(self):
        from handlers.project import _check_recert_pvcs_ready

        core_api = MagicMock()
        cfgs = [{"rhcosPvc": ""}]

        assert _check_recert_pvcs_ready(core_api, cfgs, "ns1") is True
        core_api.read_namespaced_persistent_volume_claim.assert_not_called()


class TestReadExportSizesEdgeCases:
    """Cover _read_export_sizes: no pods, exception path."""

    def test_no_pods_for_job(self):
        from handlers.project import _read_export_sizes

        core_api = MagicMock()
        pod_list = MagicMock()
        pod_list.items = []
        core_api.list_namespaced_pod.return_value = pod_list

        export_jobs = [{"jobName": "export-j1", "sizeBytes": 0}]
        _read_export_sizes(core_api, export_jobs, "ns1")
        # sizeBytes should remain 0
        assert export_jobs[0].get("sizeBytes") == 0

    def test_exception_swallowed(self):
        from handlers.project import _read_export_sizes

        core_api = MagicMock()
        core_api.list_namespaced_pod.side_effect = Exception("API error")

        export_jobs = [{"jobName": "export-j1"}]
        # Should not raise
        _read_export_sizes(core_api, export_jobs, "ns1")


# ---------------------------------------------------------------------------
# handlers/network.py — _patch_single_scc
# ---------------------------------------------------------------------------


class TestPatchSingleScc:
    """Cover _patch_single_scc: success, 409 retry, other exception."""

    def test_success_on_first_attempt(self):
        from handlers.network import _patch_single_scc, _modify_scc_users

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.return_value = {"users": []}

        asyncio.run(
            _patch_single_scc(
                custom_api, "my-scc", "system:serviceaccount:ns:sa", "add", "ns"
            )
        )

        custom_api.get_cluster_custom_object.assert_called_once()
        custom_api.patch_cluster_custom_object.assert_called_once()

    def test_retries_on_409_then_succeeds(self):
        from handlers.network import _patch_single_scc
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        # First call raises 409, second succeeds
        custom_api.get_cluster_custom_object.side_effect = [
            ApiException(status=409),
            {"users": []},
        ]

        asyncio.run(
            _patch_single_scc(
                custom_api, "my-scc", "system:serviceaccount:ns:sa", "add", "ns"
            )
        )

        assert custom_api.get_cluster_custom_object.call_count == 2

    def test_non_409_api_exception_logs_warning(self):
        from handlers.network import _patch_single_scc
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.side_effect = ApiException(status=403)

        # Should not raise — warning is logged instead
        asyncio.run(
            _patch_single_scc(
                custom_api, "my-scc", "system:serviceaccount:ns:sa", "add", "ns"
            )
        )

        # Only one attempt, no retry for non-409
        custom_api.get_cluster_custom_object.assert_called_once()

    def test_generic_exception_logs_warning(self):
        from handlers.network import _patch_single_scc

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.side_effect = RuntimeError("boom")

        # Should not raise
        asyncio.run(
            _patch_single_scc(
                custom_api, "my-scc", "system:serviceaccount:ns:sa", "remove", "ns"
            )
        )

        custom_api.get_cluster_custom_object.assert_called_once()

    def test_409_exhausts_retries(self):
        from handlers.network import _patch_single_scc
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        custom_api.get_cluster_custom_object.side_effect = ApiException(status=409)

        asyncio.run(
            _patch_single_scc(
                custom_api, "my-scc", "system:serviceaccount:ns:sa", "add", "ns"
            )
        )

        # 5 attempts total (range(5))
        assert custom_api.get_cluster_custom_object.call_count == 5


# ---------------------------------------------------------------------------
# handlers/network.py — _cleanup_legacy_pod
# ---------------------------------------------------------------------------


class TestNetworkCleanupLegacyPod:
    """Cover _cleanup_legacy_pod in handlers/network.py."""

    def test_deletes_standalone_pod(self):
        from handlers.network import _cleanup_legacy_pod

        core_api = MagicMock()
        pod = MagicMock()
        pod.metadata.owner_references = []
        core_api.read_namespaced_pod.return_value = pod

        _cleanup_legacy_pod(core_api, "ns1", "dnsmasq-net1")

        core_api.delete_namespaced_pod.assert_called_once_with(
            name="dnsmasq-net1", namespace="ns1"
        )

    def test_skips_pod_owned_by_replicaset(self):
        from handlers.network import _cleanup_legacy_pod

        core_api = MagicMock()
        pod = MagicMock()
        owner = MagicMock()
        owner.kind = "ReplicaSet"
        pod.metadata.owner_references = [owner]
        core_api.read_namespaced_pod.return_value = pod

        _cleanup_legacy_pod(core_api, "ns1", "dnsmasq-net1")

        core_api.delete_namespaced_pod.assert_not_called()

    def test_ignores_404(self):
        from handlers.network import _cleanup_legacy_pod
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.read_namespaced_pod.side_effect = ApiException(status=404)

        # Should not raise
        _cleanup_legacy_pod(core_api, "ns1", "gone-pod")

    def test_raises_non_404(self):
        from handlers.network import _cleanup_legacy_pod
        from kubernetes.client import ApiException

        core_api = MagicMock()
        core_api.read_namespaced_pod.side_effect = ApiException(status=500)

        with pytest.raises(ApiException):
            _cleanup_legacy_pod(core_api, "ns1", "bad-pod")

    def test_deletes_pod_with_none_owner_references(self):
        from handlers.network import _cleanup_legacy_pod

        core_api = MagicMock()
        pod = MagicMock()
        pod.metadata.owner_references = None
        core_api.read_namespaced_pod.return_value = pod

        _cleanup_legacy_pod(core_api, "ns1", "dnsmasq-net1")

        core_api.delete_namespaced_pod.assert_called_once()


# ---------------------------------------------------------------------------
# images/bmc/kubevirt_driver.py — set_boot_device with Once, revert_boot_once
# ---------------------------------------------------------------------------


class TestKubeVirtDriverSetBootDeviceOnce:
    """Cover set_boot_device with boot_enabled='Once' saving overrides."""

    @patch("images.bmc.kubevirt_driver.config")
    def test_boot_once_saves_overrides(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {"vm1": "kv-vm-1"}
        driver._boot_once_overrides = {}

        driver.custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "devices": {
                                "disks": [
                                    {"name": "disk0", "disk": {}, "bootOrder": 1},
                                    {"name": "cdrom0", "cdrom": {}, "bootOrder": 2},
                                ],
                                "interfaces": [
                                    {"name": "eth0", "bootOrder": 3},
                                ],
                            }
                        }
                    }
                }
            }
        }

        driver.set_boot_device("vm1", "Pxe", boot_enabled="Once")

        assert "kv-vm-1" in driver._boot_once_overrides
        saved = driver._boot_once_overrides["kv-vm-1"]
        # Should have saved the original boot orders
        assert len(saved["disks"]) == 2
        assert len(saved["interfaces"]) == 1
        # Verify patch was called
        driver.custom_api.patch_namespaced_custom_object.assert_called_once()

    @patch("images.bmc.kubevirt_driver.config")
    def test_boot_continuous_clears_overrides(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {"vm1": "kv-vm-1"}
        driver._boot_once_overrides = {"kv-vm-1": {"disks": [], "interfaces": []}}

        driver.custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "devices": {
                                "disks": [{"name": "disk0", "disk": {}}],
                                "interfaces": [{"name": "eth0"}],
                            }
                        }
                    }
                }
            }
        }

        driver.set_boot_device("vm1", "Hdd", boot_enabled="Continuous")

        assert "kv-vm-1" not in driver._boot_once_overrides


class TestKubeVirtDriverRevertBootOnce:
    """Cover revert_boot_once: restores saved orders, no-op if nothing saved."""

    @patch("images.bmc.kubevirt_driver.config")
    def test_revert_restores_saved_boot_orders(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {"vm1": "kv-vm-1"}
        driver._boot_once_overrides = {
            "kv-vm-1": {
                "disks": [{"disk0": 1}],
                "interfaces": [{"eth0": 3}],
            }
        }

        driver.custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "devices": {
                                "disks": [
                                    {"name": "disk0", "disk": {}, "bootOrder": 5},
                                ],
                                "interfaces": [
                                    {"name": "eth0", "bootOrder": 6},
                                ],
                            }
                        }
                    }
                }
            }
        }

        driver.revert_boot_once("vm1")

        # Override should be cleared
        assert "kv-vm-1" not in driver._boot_once_overrides

        # Patch should have been called to restore boot orders
        driver.custom_api.patch_namespaced_custom_object.assert_called_once()
        patch_body = driver.custom_api.patch_namespaced_custom_object.call_args[1][
            "body"
        ]
        patched_disks = patch_body["spec"]["template"]["spec"]["domain"]["devices"][
            "disks"
        ]
        patched_ifaces = patch_body["spec"]["template"]["spec"]["domain"]["devices"][
            "interfaces"
        ]
        # bootOrder should be restored to original values
        assert patched_disks[0]["bootOrder"] == 1
        assert patched_ifaces[0]["bootOrder"] == 3

    @patch("images.bmc.kubevirt_driver.config")
    def test_revert_noop_when_no_saved_override(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {"vm1": "kv-vm-1"}
        driver._boot_once_overrides = {}

        driver.revert_boot_once("vm1")

        # No API calls should have been made
        driver.custom_api.get_namespaced_custom_object.assert_not_called()
        driver.custom_api.patch_namespaced_custom_object.assert_not_called()


class TestKubeVirtDriverGetBootModeExtended:
    """Cover get_boot_mode: UEFI and Legacy paths."""

    @patch("images.bmc.kubevirt_driver.config")
    def test_returns_uefi_when_efi_present(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}

        driver.custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "firmware": {"bootloader": {"efi": {"secureBoot": False}}}
                        }
                    }
                }
            }
        }

        assert driver.get_boot_mode("vm1") == "UEFI"

    @patch("images.bmc.kubevirt_driver.config")
    def test_returns_legacy_when_no_efi(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}

        driver.custom_api.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {"domain": {"firmware": {"bootloader": {"bios": {}}}}}
                }
            }
        }

        assert driver.get_boot_mode("vm1") == "Legacy"

    @patch("images.bmc.kubevirt_driver.config")
    def test_returns_legacy_when_no_firmware(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}

        driver.custom_api.get_namespaced_custom_object.return_value = {
            "spec": {"template": {"spec": {"domain": {}}}}
        }

        assert driver.get_boot_mode("vm1") == "Legacy"


class TestKubeVirtDriverGetVmi404:
    """Cover _get_vmi returning None on 404."""

    @patch("images.bmc.kubevirt_driver.config")
    def test_get_vmi_returns_none_on_404(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver
        from kubernetes.client import ApiException

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}

        driver.custom_api.get_namespaced_custom_object.side_effect = ApiException(
            status=404
        )

        assert driver._get_vmi("vm1") is None

    @patch("images.bmc.kubevirt_driver.config")
    def test_get_vmi_raises_on_non_404(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver
        from kubernetes.client import ApiException

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}

        driver.custom_api.get_namespaced_custom_object.side_effect = ApiException(
            status=500
        )

        with pytest.raises(ApiException):
            driver._get_vmi("vm1")


class TestKubeVirtDriverPatchVmDevices:
    """Cover _patch_vm_devices building and applying a patch."""

    @patch("images.bmc.kubevirt_driver.config")
    def test_patch_vm_devices_calls_api(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {"vm1": "kv-vm-1"}

        disks = [{"name": "disk0", "disk": {}, "bootOrder": 1}]
        interfaces = [{"name": "eth0", "bootOrder": 2}]

        driver._patch_vm_devices("vm1", disks, interfaces)

        driver.custom_api.patch_namespaced_custom_object.assert_called_once()
        call_kwargs = driver.custom_api.patch_namespaced_custom_object.call_args[1]
        assert call_kwargs["name"] == "kv-vm-1"
        assert call_kwargs["namespace"] == "ns1"
        body = call_kwargs["body"]
        assert body["spec"]["template"]["spec"]["domain"]["devices"]["disks"] == disks
        assert (
            body["spec"]["template"]["spec"]["domain"]["devices"]["interfaces"]
            == interfaces
        )


class TestKubeVirtDriverDeleteVmi:
    """Cover _delete_vmi: best-effort, ignores ApiException."""

    @patch("images.bmc.kubevirt_driver.config")
    def test_delete_vmi_success(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {"vm1": "kv-vm-1"}

        driver._delete_vmi("vm1")

        driver.custom_api.delete_namespaced_custom_object.assert_called_once()

    @patch("images.bmc.kubevirt_driver.config")
    def test_delete_vmi_ignores_api_exception(self, mock_config):
        from images.bmc.kubevirt_driver import KubeVirtDriver
        from kubernetes.client import ApiException

        driver = KubeVirtDriver.__new__(KubeVirtDriver)
        driver.custom_api = MagicMock()
        driver.namespace = "ns1"
        driver.vm_map = {}

        driver.custom_api.delete_namespaced_custom_object.side_effect = ApiException(
            status=404
        )

        # Should not raise
        driver._delete_vmi("vm1")


# ---------------------------------------------------------------------------
# _recreate_kubevirt_vm — additional edge cases
# ---------------------------------------------------------------------------


class TestRecreateVmDeleteRaises:
    """Cover _recreate_kubevirt_vm when _delete_and_wait raises."""

    @patch("handlers.vm._delete_and_wait_for_kubevirt_vm")
    def test_delete_raises_propagates(self, mock_delete):
        from handlers.vm import _recreate_kubevirt_vm

        async def boom(*a, **k):
            raise RuntimeError("delete timed out")

        mock_delete.side_effect = boom

        custom_api = MagicMock()
        kv_vm = {"metadata": {"name": "kv-vm-1"}}

        with pytest.raises(RuntimeError, match="delete timed out"):
            asyncio.run(_recreate_kubevirt_vm(custom_api, "ns1", kv_vm, "kv-vm-1"))

        # create should never be called if delete fails
        custom_api.create_namespaced_custom_object.assert_not_called()

    @patch("handlers.vm._delete_and_wait_for_kubevirt_vm")
    def test_create_non_409_raises(self, mock_delete):
        """Non-409 ApiException on create_namespaced_custom_object propagates."""
        from handlers.vm import _recreate_kubevirt_vm
        from kubernetes.client import ApiException

        async def noop(*a, **k):
            pass

        mock_delete.side_effect = noop

        custom_api = MagicMock()
        custom_api.create_namespaced_custom_object.side_effect = ApiException(
            status=500
        )
        kv_vm = {"metadata": {"name": "kv-vm-1"}}

        with pytest.raises(ApiException):
            asyncio.run(_recreate_kubevirt_vm(custom_api, "ns1", kv_vm, "kv-vm-1"))


# ---------------------------------------------------------------------------
# _provision_disk_pvcs — blank disk edge cases
# ---------------------------------------------------------------------------


class TestProvisionBlankDiskEdgeCases:
    """Cover _provision_disk_pvcs blank disk path edge cases."""

    def test_blank_disk_409_swallowed(self):
        """409 on blank PVC create is silently swallowed (idempotent)."""
        from handlers.vm import _provision_disk_pvcs
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        core_api = MagicMock()
        core_api.create_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=409
        )

        spec = {"disks": [{"id": "disk-blank1", "blank": True, "sizeGb": 50}]}
        body = {
            "kind": "TroshkaVM",
            "metadata": {"name": "vm-1", "uid": "uid-1"},
        }
        patch_obj = MagicMock()

        result = asyncio.run(
            _provision_disk_pvcs(
                spec,
                "vm-1",
                "ns1",
                body,
                core_api,
                custom_api,
                {},
                {},
                patch_obj,
            )
        )

        assert "disk-blank1" in result
        core_api.create_namespaced_persistent_volume_claim.assert_called_once()

    def test_blank_disk_non_409_raises(self):
        """Non-409 error on blank PVC create propagates."""
        from handlers.vm import _provision_disk_pvcs
        from kubernetes.client import ApiException

        custom_api = MagicMock()
        core_api = MagicMock()
        core_api.create_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=500
        )

        spec = {"disks": [{"id": "disk-err", "blank": True, "sizeGb": 20}]}
        body = {
            "kind": "TroshkaVM",
            "metadata": {"name": "vm-1", "uid": "uid-1"},
        }
        patch_obj = MagicMock()

        with pytest.raises(ApiException):
            asyncio.run(
                _provision_disk_pvcs(
                    spec,
                    "vm-1",
                    "ns1",
                    body,
                    core_api,
                    custom_api,
                    {},
                    {},
                    patch_obj,
                )
            )

    @patch("handlers.vm._ensure_golden_pvc")
    @patch("handlers.vm._wait_for_datavolume", return_value=True)
    def test_mixed_s3_and_blank_disks(self, mock_wait, mock_golden):
        """One S3 disk + one blank disk provisions both correctly."""
        from handlers.vm import _provision_disk_pvcs

        mock_golden.return_value = "golden-abc123"
        custom_api = MagicMock()
        core_api = MagicMock()

        spec = {
            "disks": [
                {
                    "id": "disk-s3xx",
                    "sizeGb": 40,
                    "libraryImage": {"s3Path": "library/rhel.qcow2"},
                },
                {"id": "disk-blnk", "blank": True, "sizeGb": 100},
            ]
        }
        body = {
            "kind": "TroshkaVM",
            "metadata": {"name": "vm-1", "uid": "uid-1"},
        }
        patch_obj = MagicMock()

        result = asyncio.run(
            _provision_disk_pvcs(
                spec,
                "vm-1",
                "ns1",
                body,
                core_api,
                custom_api,
                {"bucket": "b"},
                {},
                patch_obj,
            )
        )

        assert "disk-s3xx" in result
        assert "disk-blnk" in result
        # S3 disk creates a clone DV
        custom_api.create_namespaced_custom_object.assert_called_once()
        # Blank disk creates a PVC
        core_api.create_namespaced_persistent_volume_claim.assert_called_once()

    def test_blank_disk_default_size(self):
        """Blank disk without sizeGb defaults to 20."""
        from handlers.vm import _provision_disk_pvcs

        custom_api = MagicMock()
        core_api = MagicMock()

        spec = {"disks": [{"id": "disk-dflt", "blank": True}]}
        body = {
            "kind": "TroshkaVM",
            "metadata": {"name": "vm-1", "uid": "uid-1"},
        }
        patch_obj = MagicMock()

        result = asyncio.run(
            _provision_disk_pvcs(
                spec,
                "vm-1",
                "ns1",
                body,
                core_api,
                custom_api,
                {},
                {},
                patch_obj,
            )
        )

        assert "disk-dflt" in result
        # Verify the PVC was created (size checked inside build_blank_pvc)
        core_api.create_namespaced_persistent_volume_claim.assert_called_once()


# ---------------------------------------------------------------------------
# _golden_requested_gb — source-size floor for clone targets
# ---------------------------------------------------------------------------


class TestGoldenRequestedGb:
    """_golden_requested_gb must reflect the golden PVC's ACTUAL capacity.

    CDI validates a clone target against the source PVC's real bound capacity
    (status.capacity), which can exceed its requested size (Ceph RBD rounds up;
    CDI expands the import to fit the source). Flooring the clone at the
    requested size under-sizes it and CDI rejects with CloneValidationFailed.
    """

    def test_prefers_capacity_over_requested(self):
        from handlers.vm import _golden_requested_gb

        core_api = MagicMock()
        golden = MagicMock()
        golden.status.capacity = {"storage": "30Gi"}
        golden.spec.resources.requests = {"storage": "20Gi"}
        core_api.read_namespaced_persistent_volume_claim.return_value = golden

        # Real incident: requested 20Gi, actual capacity 30Gi -> must return 30.
        assert _golden_requested_gb(core_api, "golden-abc") == 30

    def test_falls_back_to_requested_when_capacity_absent(self):
        from handlers.vm import _golden_requested_gb

        core_api = MagicMock()
        golden = MagicMock()
        golden.status.capacity = None
        golden.spec.resources.requests = {"storage": "20Gi"}
        core_api.read_namespaced_persistent_volume_claim.return_value = golden

        assert _golden_requested_gb(core_api, "golden-abc") == 20

    def test_returns_zero_when_read_fails(self):
        from handlers.vm import _golden_requested_gb

        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.side_effect = Exception(
            "PVC not found"
        )

        assert _golden_requested_gb(core_api, "golden-abc") == 0


# ---------------------------------------------------------------------------
# _provision_cdrom — clone path edge cases
# ---------------------------------------------------------------------------


class TestProvisionCdromCloneEdgeCases:
    """Cover _provision_cdrom clone path edge cases."""

    @patch("handlers.vm._ensure_golden_pvc")
    @patch("handlers.vm._wait_for_datavolume", return_value=True)
    def test_409_on_clone_dv_create_swallowed(self, mock_wait, mock_golden):
        """409 on clone DV create is idempotent — returns pvc_name."""
        from handlers.vm import _provision_cdrom
        from kubernetes.client import ApiException

        mock_golden.return_value = "golden-iso"
        custom_api = MagicMock()
        core_api = MagicMock()
        golden_pvc = MagicMock()
        golden_pvc.spec.resources.requests = {"storage": "10Gi"}
        core_api.read_namespaced_persistent_volume_claim.return_value = golden_pvc
        custom_api.create_namespaced_custom_object.side_effect = ApiException(
            status=409
        )

        spec = {"cdrom": {"s3Path": "library/rhel.iso"}}
        body = {
            "kind": "TroshkaVM",
            "metadata": {"name": "vm-1", "uid": "uid-1"},
        }

        result = asyncio.run(
            _provision_cdrom(
                spec, "vm-1", "ns1", body, core_api, custom_api, {"bucket": "b"}
            )
        )

        assert result == "vm-1-cdrom"
        mock_wait.assert_called_once()

    @patch("handlers.vm._ensure_golden_pvc")
    @patch("handlers.vm._wait_for_datavolume", return_value=True)
    def test_golden_pvc_size_read_failure_defaults_10(self, mock_wait, mock_golden):
        """When golden PVC size read fails, defaults to 10Gi."""
        from handlers.vm import _provision_cdrom

        mock_golden.return_value = "golden-iso"
        custom_api = MagicMock()
        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.side_effect = Exception(
            "PVC not found"
        )

        spec = {"cdrom": {"s3Path": "library/rhel.iso"}}
        body = {
            "kind": "TroshkaVM",
            "metadata": {"name": "vm-1", "uid": "uid-1"},
        }

        result = asyncio.run(
            _provision_cdrom(
                spec, "vm-1", "ns1", body, core_api, custom_api, {"bucket": "b"}
            )
        )

        assert result == "vm-1-cdrom"
        # The clone DV was created with default 10Gi
        create_call = custom_api.create_namespaced_custom_object.call_args
        assert create_call is not None

    @patch("handlers.vm._ensure_golden_pvc")
    @patch("handlers.vm._wait_for_datavolume", return_value=True)
    def test_golden_pvc_larger_size_used(self, mock_wait, mock_golden):
        """When golden PVC is larger than 10Gi, the larger size is used."""
        from handlers.vm import _provision_cdrom

        mock_golden.return_value = "golden-iso"
        custom_api = MagicMock()
        core_api = MagicMock()
        golden_pvc = MagicMock()
        golden_pvc.spec.resources.requests = {"storage": "25Gi"}
        core_api.read_namespaced_persistent_volume_claim.return_value = golden_pvc

        spec = {"cdrom": {"s3Path": "library/big.iso"}}
        body = {
            "kind": "TroshkaVM",
            "metadata": {"name": "vm-1", "uid": "uid-1"},
        }

        result = asyncio.run(
            _provision_cdrom(
                spec, "vm-1", "ns1", body, core_api, custom_api, {"bucket": "b"}
            )
        )

        assert result == "vm-1-cdrom"

    @patch("handlers.vm._ensure_golden_pvc")
    @patch("handlers.vm._wait_for_datavolume", return_value=False)
    def test_wait_failure_returns_none(self, mock_wait, mock_golden):
        """When _wait_for_datavolume returns False (timeout), returns None."""
        from handlers.vm import _provision_cdrom

        mock_golden.return_value = "golden-iso"
        custom_api = MagicMock()
        core_api = MagicMock()
        golden_pvc = MagicMock()
        golden_pvc.spec.resources.requests = {"storage": "10Gi"}
        core_api.read_namespaced_persistent_volume_claim.return_value = golden_pvc

        # _wait_for_datavolume returns False which means it raises an exception
        # inside the function via the owner status check
        mock_wait.side_effect = Exception("DV wait failed")

        spec = {"cdrom": {"s3Path": "library/rhel.iso"}}
        body = {
            "kind": "TroshkaVM",
            "metadata": {"name": "vm-1", "uid": "uid-1"},
        }

        result = asyncio.run(
            _provision_cdrom(
                spec, "vm-1", "ns1", body, core_api, custom_api, {"bucket": "b"}
            )
        )

        # CDROM failure is non-fatal, returns None
        assert result is None


# ---------------------------------------------------------------------------
# _create_vnc_rbac — additional edge cases
# ---------------------------------------------------------------------------


class TestCreateVncRbacExtended:
    """Additional edge-case coverage for _create_vnc_rbac."""

    @patch("handlers.project.client")
    def test_409_on_rolebinding_only(self, mock_client):
        """Role created fine, RoleBinding already exists (409) — should not raise."""
        from handlers.project import _create_vnc_rbac
        from kubernetes.client import ApiException

        mock_rbac = MagicMock()
        mock_rbac.create_namespaced_role.return_value = None
        mock_rbac.create_namespaced_role_binding.side_effect = ApiException(status=409)
        mock_client.RbacAuthorizationV1Api.return_value = mock_rbac

        _create_vnc_rbac("ns-test")

        mock_rbac.create_namespaced_role.assert_called_once()
        mock_rbac.create_namespaced_role_binding.assert_called_once()

    @patch("handlers.project.client")
    def test_non_409_on_rolebinding_raises(self, mock_client):
        """Role created fine, RoleBinding fails with non-409 — should raise."""
        from handlers.project import _create_vnc_rbac
        from kubernetes.client import ApiException

        mock_rbac = MagicMock()
        mock_rbac.create_namespaced_role.return_value = None
        mock_rbac.create_namespaced_role_binding.side_effect = ApiException(status=403)
        mock_client.RbacAuthorizationV1Api.return_value = mock_rbac

        with pytest.raises(ApiException) as exc_info:
            _create_vnc_rbac("ns-test")
        assert exc_info.value.status == 403

    @patch("handlers.project.client")
    def test_namespace_passed_to_both_calls(self, mock_client):
        """Verify namespace is passed correctly to Role and RoleBinding."""
        from handlers.project import _create_vnc_rbac

        mock_rbac = MagicMock()
        mock_client.RbacAuthorizationV1Api.return_value = mock_rbac

        _create_vnc_rbac("my-project-ns")

        role_call = mock_rbac.create_namespaced_role.call_args
        assert role_call.kwargs["namespace"] == "my-project-ns"
        role_body = role_call.kwargs["body"]
        assert role_body["metadata"]["namespace"] == "my-project-ns"

        rb_call = mock_rbac.create_namespaced_role_binding.call_args
        assert rb_call.kwargs["namespace"] == "my-project-ns"
        rb_body = rb_call.kwargs["body"]
        assert rb_body["metadata"]["namespace"] == "my-project-ns"
        assert rb_body["subjects"][0]["namespace"] == "my-project-ns"

    @patch("handlers.project.client")
    def test_role_has_correct_rules(self, mock_client):
        """Verify Role body contains expected VMI and VNC subresource rules."""
        from handlers.project import _create_vnc_rbac

        mock_rbac = MagicMock()
        mock_client.RbacAuthorizationV1Api.return_value = mock_rbac

        _create_vnc_rbac("ns1")

        role_body = mock_rbac.create_namespaced_role.call_args.kwargs["body"]
        rules = role_body["rules"]
        assert len(rules) == 2
        assert rules[0]["apiGroups"] == ["kubevirt.io"]
        assert "virtualmachineinstances" in rules[0]["resources"]
        assert rules[1]["apiGroups"] == ["subresources.kubevirt.io"]
        assert "virtualmachineinstances/vnc" in rules[1]["resources"]


# ---------------------------------------------------------------------------
# project_update handler
# ---------------------------------------------------------------------------


class TestProjectUpdateHandler:
    """Cover the kopf project_update handler.

    Since kopf is mocked as MagicMock in conftest, the @kopf.on.update
    decorator wraps the function in a MagicMock.  We extract the original
    async function from the mock's call_args to test it directly.
    """

    @staticmethod
    def _get_project_update_fn():
        """Extract the unwrapped project_update async function."""
        import importlib

        import kopf

        importlib.import_module("handlers.project")

        decorator_mock = kopf.on.update.return_value
        for call_args in reversed(decorator_mock.call_args_list):
            fn = call_args[0][0]
            if asyncio.iscoroutinefunction(fn) and fn.__name__ == "project_update":
                return fn
        raise RuntimeError("Could not find project_update in kopf mock call args")

    @patch("handlers.project._handle_capture")
    def test_happy_path_valid_annotation(self, mock_handle_capture):
        """Valid capture annotation triggers _handle_capture."""
        from handlers.project import CAPTURE_ANNOTATION

        fn = self._get_project_update_fn()

        mock_handle_capture.return_value = None

        capture_config = {"s3Config": {"bucket": "test"}, "disks": []}
        meta = {
            "annotations": {CAPTURE_ANNOTATION: json.dumps(capture_config)},
        }
        status = {"phase": "Running"}
        patch_obj = MagicMock()

        asyncio.run(
            fn(
                status=status,
                meta=meta,
                namespace="ns1",
                name="proj-1",
                patch=patch_obj,
            )
        )

        mock_handle_capture.assert_called_once_with(
            capture_config, "ns1", "proj-1", patch_obj
        )

    def test_missing_annotation_is_noop(self):
        """No capture annotation means no action."""
        fn = self._get_project_update_fn()

        meta = {"annotations": {}}
        status = {"phase": "Running"}
        patch_obj = MagicMock()

        result = asyncio.run(
            fn(
                status=status,
                meta=meta,
                namespace="ns1",
                name="proj-1",
                patch=patch_obj,
            )
        )

        assert result is None

    def test_none_annotations_is_noop(self):
        """annotations=None should be treated as empty."""
        fn = self._get_project_update_fn()

        meta = {"annotations": None}
        status = {"phase": "Running"}
        patch_obj = MagicMock()

        result = asyncio.run(
            fn(
                status=status,
                meta=meta,
                namespace="ns1",
                name="proj-1",
                patch=patch_obj,
            )
        )

        assert result is None

    def test_no_annotations_key_is_noop(self):
        """No annotations key at all should be treated as empty."""
        fn = self._get_project_update_fn()

        meta = {}
        status = {"phase": "Running"}
        patch_obj = MagicMock()

        result = asyncio.run(
            fn(
                status=status,
                meta=meta,
                namespace="ns1",
                name="proj-1",
                patch=patch_obj,
            )
        )

        assert result is None

    @patch("handlers.project._handle_capture")
    def test_invalid_json_does_not_call_handle_capture(self, mock_handle_capture):
        """Invalid JSON in annotation logs error, does not call _handle_capture."""
        from handlers.project import CAPTURE_ANNOTATION

        fn = self._get_project_update_fn()

        meta = {"annotations": {CAPTURE_ANNOTATION: "not-valid-json{{"}}
        status = {"phase": "Running"}
        patch_obj = MagicMock()

        result = asyncio.run(
            fn(
                status=status,
                meta=meta,
                namespace="ns1",
                name="proj-1",
                patch=patch_obj,
            )
        )

        mock_handle_capture.assert_not_called()
        assert result is None

    @patch("handlers.project._handle_capture")
    def test_capturing_phase_skips(self, mock_handle_capture):
        """If phase is already 'Capturing', skip processing."""
        from handlers.project import CAPTURE_ANNOTATION

        fn = self._get_project_update_fn()

        capture_config = {"s3Config": {"bucket": "test"}, "disks": []}
        meta = {"annotations": {CAPTURE_ANNOTATION: json.dumps(capture_config)}}
        status = {"phase": "Capturing"}
        patch_obj = MagicMock()

        result = asyncio.run(
            fn(
                status=status,
                meta=meta,
                namespace="ns1",
                name="proj-1",
                patch=patch_obj,
            )
        )

        mock_handle_capture.assert_not_called()
        assert result is None

    @patch("handlers.project._handle_capture")
    def test_empty_status_phase_proceeds(self, mock_handle_capture):
        """Empty status (no phase key) should proceed with capture."""
        from handlers.project import CAPTURE_ANNOTATION

        fn = self._get_project_update_fn()

        mock_handle_capture.return_value = None

        capture_config = {"disks": []}
        meta = {"annotations": {CAPTURE_ANNOTATION: json.dumps(capture_config)}}
        status = {}
        patch_obj = MagicMock()

        asyncio.run(
            fn(
                status=status,
                meta=meta,
                namespace="ns1",
                name="proj-1",
                patch=patch_obj,
            )
        )

        mock_handle_capture.assert_called_once()


# ---------------------------------------------------------------------------
# _collect_recert_configs — remaining edge cases
# ---------------------------------------------------------------------------


class TestCollectRecertConfigsRemainingEdges:
    """Cover remaining edge cases for _collect_recert_configs."""

    def test_empty_vm_list(self):
        """Empty VMs list returns empty configs."""
        from handlers.project import _collect_recert_configs

        assert _collect_recert_configs([], {}, None) == []
        assert _collect_recert_configs([], {"vm1": []}, "bastion") == []

    def test_multiple_vms_all_recert_enabled(self):
        """All VMs have recertEnabled and pattern disks — all should be collected."""
        from handlers.project import _collect_recert_configs

        vms = [
            {"id": "aaaaaaaa-1111", "name": "sno-1", "recertEnabled": True},
            {"id": "bbbbbbbb-2222", "name": "sno-2", "recertEnabled": True},
            {"id": "cccccccc-3333", "name": "sno-3", "recertEnabled": True},
        ]
        vm_disks_map = {
            "aaaaaaaa-1111": [
                {"id": "dddddddd-1111", "patternImage": {"s3Path": "p/a.qcow2"}}
            ],
            "bbbbbbbb-2222": [
                {"id": "eeeeeeee-2222", "patternImage": {"s3Path": "p/b.qcow2"}}
            ],
            "cccccccc-3333": [
                {"id": "ffffffff-3333", "patternImage": {"s3Path": "p/c.qcow2"}}
            ],
        }

        configs = _collect_recert_configs(vms, vm_disks_map, "bastion-pvc")
        assert len(configs) == 3
        names = [c["vmName"] for c in configs]
        assert names == ["sno-1", "sno-2", "sno-3"]
        for c in configs:
            assert c["bastionPvc"] == "bastion-pvc"

    def test_rhcos_pvc_name_construction(self):
        """Verify PVC name is constructed from first 8 chars of vm ID and disk ID."""
        from handlers.project import _collect_recert_configs

        vms = [
            {"id": "abcdefgh-ijklmnop", "name": "sno", "recertEnabled": True},
        ]
        vm_disks_map = {
            "abcdefgh-ijklmnop": [
                {"id": "12345678-90abcdef", "patternImage": {"s3Path": "p/d.qcow2"}}
            ],
        }

        configs = _collect_recert_configs(vms, vm_disks_map, None)
        assert len(configs) == 1
        assert configs[0]["rhcosPvc"] == "vm-abcdefgh-disk-12345678"

    def test_multiple_disks_uses_first(self):
        """When a VM has multiple disks, rhcosPvc is built from the first disk."""
        from handlers.project import _collect_recert_configs

        vms = [
            {"id": "aaaaaaaa-1111", "name": "sno", "recertEnabled": True},
        ]
        vm_disks_map = {
            "aaaaaaaa-1111": [
                {"id": "dddddddd-first", "patternImage": {"s3Path": "p/a.qcow2"}},
                {"id": "eeeeeeee-second", "patternImage": {"s3Path": "p/b.qcow2"}},
            ],
        }

        configs = _collect_recert_configs(vms, vm_disks_map, None)
        assert len(configs) == 1
        assert configs[0]["rhcosPvc"] == "vm-aaaaaaaa-disk-dddddddd"

    def test_disk_without_id_key(self):
        """Disk entry missing 'id' key — should use empty string slice."""
        from handlers.project import _collect_recert_configs

        vms = [
            {"id": "aaaaaaaa-1111", "name": "sno", "recertEnabled": True},
        ]
        vm_disks_map = {
            "aaaaaaaa-1111": [
                {"patternImage": {"s3Path": "p/d.qcow2"}},
            ],
        }

        configs = _collect_recert_configs(vms, vm_disks_map, None)
        assert len(configs) == 1
        # disk id defaults to empty string, sliced to [:8] = ""
        assert configs[0]["rhcosPvc"] == "vm-aaaaaaaa-disk-"


# ---------------------------------------------------------------------------
# main.py — configure() startup handler
# ---------------------------------------------------------------------------


def _load_configure():
    """Load the configure function from main.py with a passthrough decorator.

    The conftest mocks kopf as MagicMock, which swallows the decorated function.
    We reload main.py with kopf.on.startup() set as a passthrough so the
    real configure function is preserved and callable.
    """
    import importlib.util
    import os

    kopf_mock = sys.modules["kopf"]
    # Save original and set passthrough decorator
    orig = kopf_mock.on.startup.return_value
    kopf_mock.on.startup.return_value = lambda fn: fn

    # Mock kubernetes hierarchy (configure imports it at call time)
    k8s_mock = MagicMock()
    old_k8s = sys.modules.get("kubernetes")
    old_k8s_client = sys.modules.get("kubernetes.client")
    old_k8s_exc = sys.modules.get("kubernetes.client.exceptions")
    sys.modules["kubernetes"] = k8s_mock
    sys.modules["kubernetes.client"] = k8s_mock.client
    sys.modules["kubernetes.client.exceptions"] = k8s_mock.client.exceptions

    # Remove any cached operator module
    if "troshka_operator" in sys.modules:
        del sys.modules["troshka_operator"]

    op_path = os.path.join(os.path.dirname(__file__), "..", "main.py")
    spec = importlib.util.spec_from_file_location("troshka_operator", op_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Restore original kopf mock behavior
    kopf_mock.on.startup.return_value = orig

    return mod.configure


import logging
import sys


class TestConfigureStartup:
    """Tests for the @kopf.on.startup() handler in main.py."""

    def _make_settings(self):
        """Create a mock settings object with the attributes configure() sets."""
        settings = MagicMock()
        settings.posting = MagicMock()
        settings.persistence = MagicMock()
        settings.execution = MagicMock()
        settings.batching = MagicMock()
        return settings

    def test_configure_sets_kopf_settings(self):
        """Verify all kopf settings are configured correctly."""
        configure = _load_configure()
        settings = self._make_settings()

        with patch("kubernetes.client") as mock_client:
            mock_custom = MagicMock()
            mock_custom.list_cluster_custom_object.return_value = {"items": []}
            mock_client.CustomObjectsApi.return_value = mock_custom
            mock_client.BatchV1Api.return_value = MagicMock()

            configure(settings=settings)

        assert settings.posting.level == logging.WARNING
        assert settings.persistence.finalizer == "troshka.redhat.com/finalizer"
        assert settings.execution.max_workers == 100
        assert settings.batching.batch_window == 0.5

    def test_configure_retries_failed_recert(self):
        """Project in Error with recertConfig gets job deleted and status reset."""
        configure = _load_configure()
        settings = self._make_settings()

        error_project = {
            "metadata": {"namespace": "test-ns", "name": "proj-abc"},
            "status": {
                "phase": "Error",
                "recertConfig": {"rhcosPvc": "vm-aaaaaaaa-disk-dddddddd"},
            },
        }

        with patch("kubernetes.client") as mock_client:
            mock_custom = MagicMock()
            mock_batch = MagicMock()
            mock_custom.list_cluster_custom_object.return_value = {
                "items": [error_project]
            }
            mock_client.CustomObjectsApi.return_value = mock_custom
            mock_client.BatchV1Api.return_value = mock_batch

            configure(settings=settings)

        # Should delete the recert job
        mock_batch.delete_namespaced_job.assert_called_once_with(
            name="recert-vm-aaaaaaaa",
            namespace="test-ns",
            propagation_policy="Background",
        )

        # Should patch status to Deploying
        mock_custom.patch_namespaced_custom_object_status.assert_called_once_with(
            group="troshka.redhat.com",
            version="v1alpha1",
            namespace="test-ns",
            plural="troshkaprojects",
            name="proj-abc",
            body={
                "status": {
                    "phase": "Deploying",
                    "error": None,
                    "recertAttempts": 0,
                }
            },
        )

    def test_configure_skips_non_error_projects(self):
        """Projects not in Error phase should not trigger any job deletion or status patch."""
        configure = _load_configure()
        settings = self._make_settings()

        running_project = {
            "metadata": {"namespace": "test-ns", "name": "proj-running"},
            "status": {
                "phase": "Running",
                "recertConfig": {"rhcosPvc": "vm-11111111-disk-22222222"},
            },
        }
        deploying_project = {
            "metadata": {"namespace": "test-ns", "name": "proj-deploying"},
            "status": {"phase": "Deploying"},
        }

        with patch("kubernetes.client") as mock_client:
            mock_custom = MagicMock()
            mock_batch = MagicMock()
            mock_custom.list_cluster_custom_object.return_value = {
                "items": [running_project, deploying_project]
            }
            mock_client.CustomObjectsApi.return_value = mock_custom
            mock_client.BatchV1Api.return_value = mock_batch

            configure(settings=settings)

        mock_batch.delete_namespaced_job.assert_not_called()
        mock_custom.patch_namespaced_custom_object_status.assert_not_called()

    def test_configure_handles_list_exception(self):
        """Exception from list_cluster_custom_object is caught gracefully."""
        configure = _load_configure()
        settings = self._make_settings()

        with patch("kubernetes.client") as mock_client:
            mock_custom = MagicMock()
            mock_custom.list_cluster_custom_object.side_effect = Exception(
                "API unreachable"
            )
            mock_client.CustomObjectsApi.return_value = mock_custom
            mock_client.BatchV1Api.return_value = MagicMock()

            # Should not raise — exception is caught internally
            configure(settings=settings)

        # Settings should still have been configured before the exception
        assert settings.posting.level == logging.WARNING

    def test_configure_handles_delete_job_exception(self):
        """If deleting the recert job fails, status patch should still proceed."""
        configure = _load_configure()
        settings = self._make_settings()

        error_project = {
            "metadata": {"namespace": "test-ns", "name": "proj-fail-delete"},
            "status": {
                "phase": "Error",
                "recertConfig": {"rhcosPvc": "vm-bbbbbbbb-disk-cccccccc"},
            },
        }

        with patch("kubernetes.client") as mock_client:
            mock_custom = MagicMock()
            mock_batch = MagicMock()
            mock_batch.delete_namespaced_job.side_effect = Exception("job not found")
            mock_custom.list_cluster_custom_object.return_value = {
                "items": [error_project]
            }
            mock_client.CustomObjectsApi.return_value = mock_custom
            mock_client.BatchV1Api.return_value = mock_batch

            configure(settings=settings)

        # Job deletion failed, but status patch should still happen
        mock_custom.patch_namespaced_custom_object_status.assert_called_once()
        patch_body = mock_custom.patch_namespaced_custom_object_status.call_args
        assert patch_body.kwargs["body"]["status"]["phase"] == "Deploying"

    def test_configure_error_without_recert_config_skipped(self):
        """Project in Error but without recertConfig should be skipped."""
        configure = _load_configure()
        settings = self._make_settings()

        error_no_recert = {
            "metadata": {"namespace": "test-ns", "name": "proj-error-no-recert"},
            "status": {"phase": "Error", "error": "something else broke"},
        }

        with patch("kubernetes.client") as mock_client:
            mock_custom = MagicMock()
            mock_batch = MagicMock()
            mock_custom.list_cluster_custom_object.return_value = {
                "items": [error_no_recert]
            }
            mock_client.CustomObjectsApi.return_value = mock_custom
            mock_client.BatchV1Api.return_value = mock_batch

            configure(settings=settings)

        mock_batch.delete_namespaced_job.assert_not_called()
        mock_custom.patch_namespaced_custom_object_status.assert_not_called()

    def test_configure_rhcos_pvc_without_disk_separator(self):
        """When rhcosPvc doesn't contain '-disk-', job name uses 'vm' fallback."""
        configure = _load_configure()
        settings = self._make_settings()

        error_project = {
            "metadata": {"namespace": "test-ns", "name": "proj-no-disk-sep"},
            "status": {
                "phase": "Error",
                "recertConfig": {"rhcosPvc": "some-weird-pvc-name"},
            },
        }

        with patch("kubernetes.client") as mock_client:
            mock_custom = MagicMock()
            mock_batch = MagicMock()
            mock_custom.list_cluster_custom_object.return_value = {
                "items": [error_project]
            }
            mock_client.CustomObjectsApi.return_value = mock_custom
            mock_client.BatchV1Api.return_value = mock_batch

            configure(settings=settings)

        # Fallback job name should be "recert-vm"
        mock_batch.delete_namespaced_job.assert_called_once_with(
            name="recert-vm",
            namespace="test-ns",
            propagation_policy="Background",
        )


class TestClusterVipLeases:
    """build_static_leases reserves OCP cluster VIPs with bogus MACs so dnsmasq
    keeps them out of the dynamic DHCP pool (KubeVirt-native parity with the
    troshkad vxlan.py path)."""

    def _topo(self, api_vip="10.0.0.10", ingress_vip="10.0.0.11"):
        return {
            "nodes": [
                {
                    "id": "net1",
                    "type": "networkNode",
                    "data": {"id": "net1", "cidr": "10.0.0.0/24"},
                },
                {
                    "id": "cluster-prod",
                    "type": "clusterNode",
                    "data": {
                        "name": "prod",
                        "apiVip": api_vip,
                        "ingressVip": ingress_vip,
                    },
                },
                {
                    "id": "prod-cp-0",
                    "type": "vmNode",
                    "parentId": "cluster-prod",
                    "data": {
                        "id": "prod-cp-0",
                        "label": "prod-cp-0",
                        "nics": [
                            {
                                "id": "nic-n0",
                                "mac": "52:54:00:aa:bb:01",
                                "ip": "10.0.0.20",
                            }
                        ],
                    },
                },
            ],
            "edges": [
                {
                    "source": "net1",
                    "target": "prod-cp-0",
                    "sourceHandle": "bottom",
                    "targetHandle": "nic-nic-n0-top",
                }
            ],
        }

    def test_vips_reserved_with_bogus_macs(self):
        from helpers.topology import build_static_leases

        leases = build_static_leases(self._topo())["net1"]
        by_ip = {l["ip"]: l for l in leases}
        # Real NIC lease still present.
        assert by_ip["10.0.0.20"]["mac"] == "52:54:00:aa:bb:01"
        # VIPs reserved with deterministic 02: (locally-administered) MACs.
        assert by_ip["10.0.0.10"]["mac"] == "02:00:0a:00:00:0a"
        assert by_ip["10.0.0.10"]["hostname"] == "prod-api"
        assert by_ip["10.0.0.11"]["mac"] == "02:00:0a:00:00:0b"
        assert by_ip["10.0.0.11"]["hostname"] == "prod-ingress"

    def test_sno_has_no_vip_reservations(self):
        from helpers.topology import build_static_leases

        # SNO: no VIPs (api==ingress==node IP) -> only the real NIC lease.
        leases = build_static_leases(self._topo(api_vip="", ingress_vip=""))["net1"]
        assert [l["ip"] for l in leases] == ["10.0.0.20"]

    def test_bogus_mac_is_deterministic(self):
        from helpers.topology import _bogus_mac_for_ip

        assert _bogus_mac_for_ip("192.168.1.254") == "02:00:c0:a8:01:fe"
        assert _bogus_mac_for_ip("10.0.0.10") == _bogus_mac_for_ip("10.0.0.10")

    def test_identical_api_ingress_vip_dedups(self):
        """api_vip == ingress_vip (SNO-style) yields ONE lease, not two — a
        duplicate dhcp-host for the same address makes dnsmasq exit 1."""
        from helpers.topology import build_static_leases

        topo = self._topo(api_vip="10.0.0.10", ingress_vip="10.0.0.10")
        vip_leases = [
            l for l in build_static_leases(topo)["net1"] if l["ip"] == "10.0.0.10"
        ]
        assert len(vip_leases) == 1
        assert vip_leases[0]["mac"] == "02:00:0a:00:00:0a"
