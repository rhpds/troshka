import yaml

from app.services.ocp.agent_template import _build_install_config_legacy


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
    ic = _build_install_config_legacy(
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
    assert "imageDigestSources" in parsed
    mirrors = parsed["imageDigestSources"]
    sources = [m["source"] for m in mirrors]
    assert "registry.redhat.io" in sources
    assert "quay.io" in sources
    rh = next(m for m in mirrors if m["source"] == "registry.redhat.io")
    assert "registry-quay.apps.example.com/registry_redhat_io" in rh["mirrors"]


def test_build_install_config_without_pull_through():
    ic = _build_install_config_legacy(
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
    assert "imageDigestSources" not in ic


def test_bastion_cloud_init_pull_through_registry():
    """When pull_through_registry is enabled, bastion cloud-init should contain registries.conf.d config."""
    from app.services.ocp.agent_template import customize_topology
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_inline_template,
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
        "install_via": "bastion",
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


def test_bastion_network_check_uses_tcp_not_icmp():
    """The install network-wait must not use ICMP ping (gateway egress may block
    icmp); it should test a TCP DNS reach to 8.8.8.8 instead."""
    from app.services.ocp.agent_template import customize_topology
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_inline_template,
    )

    tmpl = {
        "template_name": "test-net",
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
    customize_topology(
        topo,
        "test-net",
        {
            "cluster_name": "test",
            "base_domain": "test.local",
            "ocp_version": "4.20",
            "common_password": "testpass",
            "auto_install_ocp": True,
            "install_via": "bastion",
            "resolved": resolved,
        },
    )
    bastion = next(
        n for n in topo["nodes"] if n.get("data", {}).get("name") == "bastion"
    )
    user_data = bastion["data"].get("ciUserData", "")
    assert "ping -c1 -W2 8.8.8.8" not in user_data
    assert "/dev/tcp/8.8.8.8/53" in user_data


def test_build_install_script_uses_selenium_autologin_not_nss():
    from app.services.ocp.agent_template import _build_install_script

    script = _build_install_script(
        "4.22",
        auto_install=True,
        bmc_password="pass",
        bmc_ips_str="192.168.100.10",
        cluster_name="ocp",
        base_domain="ocp.local",
    )
    assert "PK11SDR_Encrypt" not in script
    assert "PWSAVEEOF" not in script
    assert "geckodriver" in script
    assert "ocp-autologin.py" in script
    assert "console-openshift-console.apps.ocp.ocp.local" in script


def test_redfish_insert_media_retries_bmc_readiness():
    """A not-yet-ready BMC must not crash the set -e subshell on the first node.

    The SYS_ID fetch (curl|python3) is unguarded, so under set -e/pipefail a
    single unreachable BMC in a multi-node loop killed the whole install. It now
    retries until the BMC answers and skips (continue) a BMC that never does.
    """
    from app.services.ocp.agent_template import _redfish_insert_media_cmd

    cmd = _redfish_insert_media_cmd("  ", "192.168.100.10 192.168.100.11")
    # Bounded readiness retry around the SYS_ID fetch.
    assert "for _try in $(seq 1" in cmd
    # set -e safe: the failing pipeline is guarded by && (not the last command in
    # the list), so its non-zero status does not abort the subshell.
    assert "SYS_ID=$(curl" in cmd
    assert '&& [ -n "$SYS_ID" ] && break' in cmd
    # A BMC that never becomes ready is skipped, not fatal.
    assert "continue" in cmd


def test_redfish_eject_media_retries_and_is_set_e_safe():
    """Eject MUST happen (a stuck ISO risks a node re-booting from media), so a
    transiently-unreachable BMC is retried, not skipped. The retry is also set -e
    safe (guarded && list) so it never aborts the subshell before the completion
    breadcrumb/sentinel — which previously made the pod exit 1, restart, and race
    the monitor's cred harvest + cluster-terminal kubeconfig injection (a 3+2's 5
    BMCs made this likely)."""
    from app.services.ocp.agent_template import _redfish_eject_media_cmd

    cmd = _redfish_eject_media_cmd("  ", "192.168.100.10 192.168.100.11")
    assert "for _try in $(seq 1" in cmd  # bounded readiness retry
    assert '&& [ -n "$SYS_ID" ] && break' in cmd  # set -e safe retry
    assert "VirtualMedia.EjectMedia" in cmd


def test_build_install_script_golden():
    """Full-output snapshot of the bastion agent-based installer.

    The Redfish/serve/wait-for/create-image command strings are now shared with
    the ops-pod runner (`ops_pod_install`) via helpers in `agent_template`. This
    golden guards the BASTION side: a future edit to any shared helper that would
    change the bastion's generated script must update the golden deliberately
    (regenerate `tests/golden/ocp_bastion_install_script.txt`), not slip through.
    """
    from pathlib import Path

    from app.services.ocp.agent_template import _build_install_script

    script = _build_install_script(
        "4.20",
        auto_install=True,
        bmc_password="pw",
        bmc_ips_str="192.168.100.10 192.168.100.11",
        cluster_name="ocp",
        base_domain="ocp.local",
    )
    golden = Path(__file__).parent / "golden" / "ocp_bastion_install_script.txt"
    assert script == golden.read_text()
