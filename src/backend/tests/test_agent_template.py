import yaml
from app.services.ocp.agent_template import _build_install_config


def _minimal_topology():
    return {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {
                    "name": "cp0",
                    "tags": {"AnsibleGroup": "controllers"},
                    "bmcEnabled": True,
                    "bmcIp": "192.168.50.10",
                    "nics": [
                        {"id": "nic1", "mac": "52:54:00:aa:bb:cc", "ip": "10.0.0.10"}
                    ],
                    "diskControllers": [],
                },
                "position": {"x": 0, "y": 0},
            },
            {
                "id": "net1",
                "type": "networkNode",
                "data": {
                    "subtype": "network",
                    "cidr": "10.0.0.0/24",
                    "networkType": "cluster",
                },
                "position": {"x": 0, "y": 0},
            },
        ],
        "edges": [],
    }


def test_build_install_config_with_pull_through():
    ptr = {
        "enabled": True,
        "url": "registry-quay.apps.example.com",
        "orgs": {
            "registry.redhat.io": "registry_redhat_io",
            "quay.io": "quay_io",
        },
    }
    ic = _build_install_config(
        _minimal_topology(),
        "ocp-sno",
        "sno",
        "sno.local",
        "10.0.0.10",
        "10.0.0.10",
        "password",
        '{"auths":{}}',
        "ssh-rsa AAAA",
        pull_through_registry=ptr,
    )
    parsed = yaml.safe_load(ic)
    assert "imageDigestMirrorSet" in parsed
    mirrors = parsed["imageDigestMirrorSet"]
    sources = [m["source"] for m in mirrors]
    assert "registry.redhat.io" in sources
    assert "quay.io" in sources
    rh = next(m for m in mirrors if m["source"] == "registry.redhat.io")
    assert "registry-quay.apps.example.com/registry_redhat_io" in rh["mirrors"]


def test_build_install_config_without_pull_through():
    ic = _build_install_config(
        _minimal_topology(),
        "ocp-sno",
        "sno",
        "sno.local",
        "10.0.0.10",
        "10.0.0.10",
        "password",
        '{"auths":{}}',
        "ssh-rsa AAAA",
    )
    assert "imageDigestMirrorSet" not in ic


def test_bastion_cloud_init_pull_through_registry():
    """When pull_through_registry is enabled, bastion cloud-init should contain registries.conf.d config."""
    from app.services.ocp.agent_template import customize_topology
    from app.services.template_loader import (
        resolve_inline_template,
        generate_topology_from_template,
    )

    tmpl = {
        "template_name": "test-ptr",
        "install_method": "agent",
        "networks": {
            "cluster": {"cidr": "10.0.0.0/24", "dhcp": True, "domain": "test.local"},
            "bmc": {"cidr": "192.168.50.0/24", "type": "bmc"},
        },
        "ocp": {
            "cluster_name": "test",
            "base_domain": "test.local",
            "api_vip": "10.0.0.2",
            "ingress_vip": "10.0.0.3",
        },
        "pull_through_registry": {
            "enabled": True,
            "url": "registry-quay.apps.example.com",
            "orgs": {"registry.redhat.io": "registry_redhat_io", "quay.io": "quay_io"},
        },
        "vms": {
            "bastion": {
                "role": "bastion",
                "vcpus": 2,
                "ram_gb": 4,
                "os": "rhel9",
                "disks": [{"size_gb": 50}],
                "nics": [
                    {"network": "cluster", "ip": "10.0.0.50"},
                    {"network": "bmc", "ip": "192.168.50.50"},
                ],
            },
        },
    }
    resolved = resolve_inline_template(tmpl)
    topo = generate_topology_from_template(resolved)
    config = {
        "cluster_name": "test",
        "base_domain": "test.local",
        "ocp_version": "4.20",
        "common_password": "testpass",
        "pull_secret_json": '{"auths":{}}',
        "ssh_pub_key": "ssh-rsa AAAA",
        "auto_install_ocp": True,
        "resolved": resolved,
    }
    customize_topology(topo, "test-ptr", config)

    bastion = next(
        n for n in topo["nodes"] if n.get("data", {}).get("name") == "bastion"
    )
    user_data = bastion["data"].get("ciUserData", "")
    assert "registries.conf.d/rhdp-cache.conf" in user_data
    assert "registry-quay.apps.example.com/registry_redhat_io" in user_data
    assert "registry-quay.apps.example.com/quay_io" in user_data
