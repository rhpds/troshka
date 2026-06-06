"""
Agent deployer — installs the troshka agent on a remote host via SSH.

Uses the host's stored private key to connect and deploy.
"""
import logging
import subprocess
import tempfile
import os

logger = logging.getLogger(__name__)

AGENT_INSTALL_SCRIPT = """#!/bin/bash
set -uo pipefail

echo "=== Troshka Agent Installer ==="

# Wait for cloud-init to finish
echo "Waiting for cloud-init..."
cloud-init status --wait 2>/dev/null || true

# Verify nested virtualization
if grep -q vmx /proc/cpuinfo || grep -q svm /proc/cpuinfo; then
    echo "Nested virtualization: ENABLED"
else
    echo "WARNING: Nested virtualization NOT detected"
fi

# Ensure prerequisites — skip dnf if already installed
if ! which virsh &>/dev/null || ! which virt-install &>/dev/null; then
    echo "Installing prerequisites..."
    dnf install -y qemu-kvm libvirt libvirt-client libvirt-devel virt-install \
        python3 python3-libvirt dnsmasq nftables xorriso || true
else
    echo "Prerequisites already installed, skipping dnf"
fi

# Enable services (RHEL 10 uses modular daemons, RHEL 9 uses monolithic libvirtd)
if systemctl list-unit-files virtqemud.service &>/dev/null; then
    systemctl enable --now virtqemud.socket virtnetworkd.socket virtstoraged.socket
    echo "libvirt: modular daemons (RHEL 10+)"
else
    systemctl enable --now libvirtd
    echo "libvirt: monolithic daemon (RHEL 9)"
fi
systemctl enable --now nftables

# Enable KSM for RAM overcommit
echo 1 > /sys/kernel/mm/ksm/run 2>/dev/null || true
echo 1000 > /sys/kernel/mm/ksm/pages_to_scan 2>/dev/null || true

# Allow ec2-user to manage libvirt without polkit agent
usermod -aG libvirt ec2-user
cat > /etc/polkit-1/rules.d/50-libvirt.rules << 'POLKITEOF'
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.libvirt") == 0 && subject.isInGroup("libvirt")) {
        return polkit.Result.YES;
    }
});
POLKITEOF

# Create directories
mkdir -p /var/lib/troshka/images /var/lib/troshka/vms /etc/troshka-agent /opt/troshka-agent

# Write agent config
cat > /etc/troshka-agent/config.yaml << 'AGENTCFG'
api:
  url: "{api_url}"
host:
  id: "{host_id}"
storage:
  image_cache_dir: /var/lib/troshka/images
  image_cache_max_gb: 200
  vm_disk_dir: /var/lib/troshka/vms
libvirt:
  uri: "qemu:///system"
health:
  interval_seconds: 30
AGENTCFG

# Verify libvirt
virsh list --all > /dev/null 2>&1 && echo "libvirt: OK" || echo "libvirt: FAILED"

# Report status
echo "=== Agent installation complete ==="
echo "Host ID: {host_id}"
echo "Libvirt: $(virsh version --daemon 2>/dev/null | head -1 || echo 'not running')"
echo "KVM: $(ls /dev/kvm 2>/dev/null && echo 'available' || echo 'NOT available')"
"""


def wait_for_ssh(host_ip: str, private_key: str, timeout: int = 300) -> bool:
    """Wait for SSH to become available on the host."""
    import time

    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as kf:
        kf.write(private_key)
        key_path = kf.name
    os.chmod(key_path, 0o600)

    try:
        start = time.time()
        while time.time() - start < timeout:
            result = subprocess.run(
                ["ssh",
                 "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "ConnectTimeout=5",
                 "-o", "BatchMode=yes",
                 "-i", key_path,
                 f"ec2-user@{host_ip}", "echo", "ssh-ready"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and "ssh-ready" in result.stdout:
                logger.info("SSH ready on %s after %ds", host_ip, int(time.time() - start))
                return True
            time.sleep(5)
        logger.warning("SSH timeout on %s after %ds", host_ip, timeout)
        return False
    finally:
        os.unlink(key_path)


def deploy_agent(host_ip: str, private_key: str, host_id: str, api_url: str = "") -> dict:
    """Deploy the troshka agent to a remote host via SSH."""

    from app.core.config import config
    actual_api_url = api_url or getattr(config.app, "external_url", "")
    if not actual_api_url:
        logger.warning("No external_url configured — agent will not be able to call back to the API")
    script = AGENT_INSTALL_SCRIPT.replace("{host_id}", host_id).replace("{api_url}", actual_api_url)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as kf:
        kf.write(private_key)
        key_path = kf.name
    os.chmod(key_path, 0o600)

    try:
        ssh_opts = [
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=30",
            "-i", key_path,
        ]

        logger.info("Deploying agent to %s", host_ip)

        result = subprocess.run(
            ["ssh", *ssh_opts, f"ec2-user@{host_ip}", "sudo", "bash", "-s"],
            input=script,
            capture_output=True,
            text=True,
            timeout=300,
        )

        output = result.stdout + result.stderr
        success = result.returncode == 0

        logger.info("Agent deploy %s on %s (exit %d)", "succeeded" if success else "failed", host_ip, result.returncode)

        return {
            "success": success,
            "exit_code": result.returncode,
            "output": output,
        }
    finally:
        os.unlink(key_path)
