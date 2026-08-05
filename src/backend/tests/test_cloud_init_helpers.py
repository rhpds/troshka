"""Tests for cloud_init.py — generate_userdata, generate_metadata, generate_seed_iso_script."""

from unittest.mock import patch

from app.services.cloud_init import (
    generate_metadata,
    generate_seed_iso_script,
    generate_userdata,
)

# ---------------------------------------------------------------------------
# generate_userdata
# ---------------------------------------------------------------------------


def test_generate_userdata_minimal():
    """Minimal VM data produces valid cloud-config with defaults."""
    result = generate_userdata({"name": "test-vm"})
    assert result.startswith("#cloud-config")
    assert "hostname: test-vm" in result
    assert "fqdn: test-vm" in result
    assert "qemu-guest-agent" in result
    assert "disable_root: false" in result


def test_generate_userdata_hostname_fallback():
    """ciHostname takes precedence over name."""
    result = generate_userdata({"name": "vm1", "ciHostname": "custom-host"})
    assert "hostname: custom-host" in result
    assert "fqdn: custom-host" in result


def test_generate_userdata_ssh_keys_list():
    """SSH keys from ciSshKeys list are injected."""
    result = generate_userdata(
        {"name": "vm", "ciSshKeys": ["ssh-rsa AAAA key1", "ssh-ed25519 BBBB key2"]}
    )
    assert "ssh_authorized_keys:" in result
    assert "ssh-rsa AAAA key1" in result
    assert "ssh-ed25519 BBBB key2" in result


def test_generate_userdata_ssh_key_single():
    """Single ciSshKey string is injected when ciSshKeys list is empty."""
    result = generate_userdata({"name": "vm", "ciSshKey": "ssh-rsa AAAA single"})
    assert "ssh-rsa AAAA single" in result


def test_generate_userdata_root_password():
    """Root password triggers chpasswd and user entry."""
    result = generate_userdata({"name": "vm", "ciRootPassword": "secret123"})
    assert "ssh_pwauth: true" in result
    assert "chpasswd:" in result
    assert "- name: root" in result
    assert "lock_passwd: false" in result
    # Hash should be present (SHA-512)
    assert "$6$" in result


def test_generate_userdata_cloud_user_password():
    """Cloud-user password produces hash in chpasswd section."""
    result = generate_userdata({"name": "vm", "ciCloudUserPassword": "userpass"})
    assert "- name: cloud-user" in result
    assert "ssh_pwauth: true" in result
    assert "$6$" in result


def test_generate_userdata_both_passwords():
    """Both root and cloud-user passwords appear."""
    result = generate_userdata(
        {"name": "vm", "ciRootPassword": "r00t", "ciCloudUserPassword": "usr"}
    )
    lines = result.split("\n")
    # Both users should appear in chpasswd section
    chpasswd_start = next(i for i, l in enumerate(lines) if l.strip() == "chpasswd:")
    chpasswd_section = "\n".join(lines[chpasswd_start : chpasswd_start + 10])
    assert "cloud-user" in chpasswd_section
    assert "root" in chpasswd_section


def test_generate_userdata_no_sudo():
    """ciCloudUserSudo=False omits sudo/wheel."""
    result = generate_userdata({"name": "vm", "ciCloudUserSudo": False})
    assert "sudo: ALL=(ALL) NOPASSWD:ALL" not in result
    assert "groups: wheel" not in result


def test_generate_userdata_with_sudo():
    """Default ciCloudUserSudo=True includes sudo/wheel."""
    result = generate_userdata({"name": "vm"})
    assert "sudo: ALL=(ALL) NOPASSWD:ALL" in result
    assert "groups: wheel" in result


def test_generate_userdata_packages():
    """Custom packages are appended alongside qemu-guest-agent."""
    result = generate_userdata({"name": "vm", "ciPackages": ["vim", "curl"]})
    assert "- qemu-guest-agent" in result
    assert "- vim" in result
    assert "- curl" in result


def test_generate_userdata_packages_dedup_qga():
    """qemu-guest-agent is not duplicated if user includes it."""
    result = generate_userdata(
        {"name": "vm", "ciPackages": ["qemu-guest-agent", "vim"]}
    )
    lines = [l.strip() for l in result.split("\n")]
    assert lines.count("- qemu-guest-agent") == 1


def test_generate_userdata_packages_malicious_rejected():
    """Package names with shell metacharacters are filtered out."""
    result = generate_userdata(
        {"name": "vm", "ciPackages": ["vim", "foo;rm -rf /", "bar$(evil)"]}
    )
    assert "- vim" in result
    assert "foo;rm" not in result
    assert "bar$(evil)" not in result


def test_generate_userdata_gateway_ip_chrony():
    """Gateway IP injects chrony config."""
    result = generate_userdata({"name": "vm", "gateway_ip": "10.0.0.1"})
    assert "chrony" in result
    assert "server 10.0.0.1 iburst prefer" in result
    assert "makestep 1 -1" in result
    assert "systemctl restart chronyd" in result


def test_generate_userdata_guest_exec_enabled():
    """Default guestExecEnabled=True adds qemu-ga runcmd."""
    result = generate_userdata({"name": "vm"})
    assert "guest-exec" in result
    assert "qemu-guest-agent" in result


def test_generate_userdata_guest_exec_disabled():
    """guestExecEnabled=False skips qemu-ga runcmd."""
    result = generate_userdata({"name": "vm", "guestExecEnabled": False})
    # The qemu-ga runcmd line should be absent
    assert "guest-exec,guest-exec-status" not in result


def test_generate_userdata_custom_userdata_runcmd():
    """Custom user-data runcmd items are appended to the runcmd section."""
    custom = "runcmd:\n  - echo hello\n  - echo world"
    result = generate_userdata({"name": "vm", "ciUserData": custom})
    assert "echo hello" in result
    assert "echo world" in result


def test_generate_userdata_custom_userdata_toplevel():
    """Custom user-data top-level keys appear outside runcmd."""
    custom = "timezone: America/New_York"
    result = generate_userdata({"name": "vm", "ciUserData": custom})
    assert "timezone: America/New_York" in result


def test_generate_userdata_exec_key_bootcmd():
    """SSH key with troshka-exec in comment is injected in bootcmd."""
    result = generate_userdata(
        {"name": "vm", "ciSshKeys": ["ssh-rsa AAAA troshka-exec"]}
    )
    assert "troshka-exec" in result
    # Should appear in bootcmd section
    assert "sed -i '/troshka-exec/d'" in result


def test_generate_userdata_ssh_deletekeys_false():
    """ssh_deletekeys is always false (pattern deploy safety)."""
    result = generate_userdata({"name": "vm"})
    assert "ssh_deletekeys: false" in result


def test_generate_userdata_eject_cidata():
    """Seed ISO eject command is in runcmd."""
    result = generate_userdata({"name": "vm"})
    assert "eject" in result
    assert "cidata" in result


def test_generate_userdata_is_valid_yaml():
    """Generated output is valid YAML."""
    import yaml

    result = generate_userdata(
        {
            "name": "test",
            "ciRootPassword": "pass",
            "ciCloudUserPassword": "pass",
            "ciSshKeys": ["ssh-rsa AAAA key1"],
            "ciPackages": ["vim"],
            "gateway_ip": "10.0.0.1",
        }
    )
    parsed = yaml.safe_load(result)
    assert isinstance(parsed, dict)
    assert parsed["hostname"] == "test"


def test_generate_userdata_sshd_restart_limit():
    """sshd restart limit is in bootcmd."""
    result = generate_userdata({"name": "vm"})
    assert "StartLimitBurst=20" in result


def test_generate_userdata_password_auth():
    """PasswordAuthentication yes is in bootcmd."""
    result = generate_userdata({"name": "vm"})
    assert "PasswordAuthentication yes" in result


# ---------------------------------------------------------------------------
# generate_metadata
# ---------------------------------------------------------------------------


def test_generate_metadata_basic():
    """Metadata contains instance-id and local-hostname."""
    import json

    result = generate_metadata("my-vm")
    data = json.loads(result)
    assert data["local-hostname"] == "my-vm"
    assert "my-vm-" in data["instance-id"]


def test_generate_metadata_unique_instance_id():
    """Each call generates a unique instance-id."""
    import json

    r1 = json.loads(generate_metadata("vm"))
    r2 = json.loads(generate_metadata("vm"))
    assert r1["instance-id"] != r2["instance-id"]


# ---------------------------------------------------------------------------
# generate_seed_iso_script
# ---------------------------------------------------------------------------


def test_generate_seed_iso_script_empty_topology():
    """Empty topology with no cloud-init VMs produces only header (no genisoimage)."""
    result = generate_seed_iso_script("proj-123", {"nodes": []})
    assert "genisoimage" not in result


def test_generate_seed_iso_script_no_cloud_init():
    """VMs without cloudInit enabled produce no ISO commands."""
    topo = {
        "nodes": [
            {
                "id": "vm-1",
                "type": "vmNode",
                "data": {"name": "test", "cloudInit": False},
            }
        ]
    }
    result = generate_seed_iso_script("proj-123", topo)
    assert "genisoimage" not in result


def test_generate_seed_iso_script_non_vm_nodes():
    """Non-vmNode nodes produce no ISO commands."""
    topo = {
        "nodes": [
            {
                "id": "net-1",
                "type": "networkNode",
                "data": {"name": "net", "cloudInit": True},
            }
        ]
    }
    result = generate_seed_iso_script("proj-123", topo)
    assert "genisoimage" not in result


@patch("app.services.deploy_topology._vm_domain_name", return_value="troshka-proj-vm1")
def test_generate_seed_iso_script_with_cloud_init(mock_domain):
    """VM with cloudInit produces seed ISO script."""
    topo = {
        "nodes": [
            {
                "id": "vm-1234-abcd",
                "type": "vmNode",
                "data": {"name": "bastion", "cloudInit": True},
            }
        ]
    }
    result = generate_seed_iso_script("proj-1234", topo)
    assert "#!/bin/bash" in result
    assert "mkdir -p /var/lib/troshka/vms/proj-1234" in result
    assert "#cloud-config" in result
    assert "genisoimage" in result
    assert "vm-1234-" in result  # Short ID in seed dir


@patch("app.services.deploy_topology._vm_domain_name", return_value="troshka-proj-vm1")
def test_generate_seed_iso_script_multiple_vms(mock_domain):
    """Multiple cloud-init VMs each get a seed section."""
    topo = {
        "nodes": [
            {
                "id": "vm-aaaa",
                "type": "vmNode",
                "data": {"name": "vm1", "cloudInit": True},
            },
            {
                "id": "vm-bbbb",
                "type": "vmNode",
                "data": {"name": "vm2", "cloudInit": True},
            },
        ]
    }
    result = generate_seed_iso_script("proj-1234", topo)
    assert result.count("genisoimage") == 2
    assert "vm-aaaa" in result
    assert "vm-bbbb" in result
