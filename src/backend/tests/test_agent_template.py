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
