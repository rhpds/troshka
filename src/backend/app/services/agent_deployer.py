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
if ! which virsh &>/dev/null || ! which virt-install &>/dev/null || ! which nc &>/dev/null; then
    echo "Installing prerequisites..."
    dnf install -y qemu-kvm libvirt libvirt-client virt-install \
        python3 python3-libvirt dnsmasq nftables xorriso nmap-ncat || true
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

# Deploy libvirt qemu hook for namespace tap migration
mkdir -p /etc/libvirt/hooks
cat > /etc/libvirt/hooks/qemu << 'HOOKEOF'
#!/bin/bash
DOMAIN=$1
ACTION=$2
if [ "$ACTION" = "started" ]; then
    PID=$(echo "$DOMAIN" | sed -n 's/^troshka-\([a-f0-9]*\)-.*/\1/p')
    [ -z "$PID" ] && exit 0
    NS="troshka-$PID"
    ip netns list 2>/dev/null | grep -q "^$NS " || exit 0
    BRIDGE=$(ip netns exec "$NS" ip -o link show type bridge 2>/dev/null | awk -F': ' '{print $2}' | head -1)
    [ -z "$BRIDGE" ] && exit 0
    for TAP in $(virsh domiflist "$DOMAIN" 2>/dev/null | awk 'NR>2 && NF>0 {print $1}'); do
        ip link set "$TAP" netns "$NS" 2>/dev/null
        ip netns exec "$NS" ip link set "$TAP" master "$BRIDGE" 2>/dev/null
        ip netns exec "$NS" ip link set "$TAP" up 2>/dev/null
    done
fi
HOOKEOF
chmod +x /etc/libvirt/hooks/qemu

# Mount dedicated storage volume if present and not already mounted
if [ -b /dev/nvme1n1 ] && ! mountpoint -q /var/lib/troshka; then
    echo "Mounting dedicated storage volume..."
    blkid /dev/nvme1n1 || mkfs.xfs /dev/nvme1n1
    mkdir -p /var/lib/troshka
    mount /dev/nvme1n1 /var/lib/troshka
    grep -q '/var/lib/troshka' /etc/fstab || \
        echo '/dev/nvme1n1 /var/lib/troshka xfs defaults,nofail 0 2' >> /etc/fstab
    echo "Storage volume mounted at /var/lib/troshka"
elif mountpoint -q /var/lib/troshka; then
    echo "Storage volume already mounted at /var/lib/troshka"
else
    echo "WARNING: No dedicated storage volume found — using root filesystem"
fi

# Create directories
mkdir -p /var/lib/troshka/images /var/lib/troshka/vms /var/lib/troshka/tmp /etc/troshka-agent /opt/troshka-agent

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

# ── Install troshkad daemon ──
echo "Installing troshkad daemon..."
mkdir -p /opt/troshka/tls

# Generate TLS cert if not present
if [ ! -f /opt/troshka/tls/server.crt ]; then
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
        -nodes -days 3650 -subj "/CN=troshkad" \
        -keyout /opt/troshka/tls/server.key -out /opt/troshka/tls/server.crt 2>/dev/null
    chmod 600 /opt/troshka/tls/server.key
    echo "troshkad: TLS certificate generated"
else
    echo "troshkad: TLS certificate already exists"
fi

# Generate config with token if not present
if [ ! -f /opt/troshka/troshkad.conf ]; then
    TROSHKAD_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    cat > /opt/troshka/troshkad.conf << TROSHKADCFG
{{
  "port": 31337,
  "token": "$TROSHKAD_TOKEN",
  "tls_cert": "/opt/troshka/tls/server.crt",
  "tls_key": "/opt/troshka/tls/server.key",
  "host_id": "{host_id}",
  "max_concurrent_jobs": 4,
  "drain_timeout_seconds": 300
}}
TROSHKADCFG
    chmod 600 /opt/troshka/troshkad.conf
    echo "troshkad: config generated"
else
    echo "troshkad: config already exists"
fi

# Write systemd unit
cat > /etc/systemd/system/troshkad.service << 'SYSTEMDEOF'
[Unit]
Description=Troshka Host Agent Daemon
After=network.target libvirtd.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/troshka/troshkad.py
WorkingDirectory=/opt/troshka
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SYSTEMDEOF

systemctl daemon-reload
systemctl enable --now troshkad

# Open firewall port
if which firewall-cmd &>/dev/null; then
    firewall-cmd --add-port=31337/tcp --permanent 2>/dev/null || true
    firewall-cmd --reload 2>/dev/null || true
fi

echo "troshkad: service started"

# Output credentials for backend to capture
CERT_FP=$(openssl x509 -in /opt/troshka/tls/server.crt -noout -fingerprint -sha256 2>/dev/null | sed 's/.*=//')
TOKEN=$(python3 -c "import json; print(json.load(open('/opt/troshka/troshkad.conf'))['token'])")
echo "TROSHKAD_CREDENTIALS:$TOKEN:$CERT_FP"

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
                 "-o", "IdentitiesOnly=yes",
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
            "-o", "IdentitiesOnly=yes",
            "-i", key_path,
        ]

        logger.info("Deploying agent to %s", host_ip)

        # SCP troshkad.py to host
        troshkad_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
            "troshkad",
            "troshkad.py"
        )
        if os.path.exists(troshkad_path):
            logger.info("Copying troshkad.py to %s", host_ip)
            scp_result = subprocess.run(
                ["scp", *ssh_opts, troshkad_path, f"ec2-user@{host_ip}:/tmp/troshkad.py"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if scp_result.returncode != 0:
                logger.warning("SCP troshkad.py failed: %s", scp_result.stderr)
            else:
                subprocess.run(
                    ["ssh", *ssh_opts, f"ec2-user@{host_ip}", "sudo", "mv", "/tmp/troshkad.py", "/opt/troshka/troshkad.py"],
                    capture_output=True,
                    timeout=30,
                )
        else:
            logger.warning("troshkad.py not found at %s", troshkad_path)

        # Run install script
        result = subprocess.run(
            ["ssh", *ssh_opts, f"ec2-user@{host_ip}", "sudo", "bash", "-s"],
            input=script,
            capture_output=True,
            text=True,
            timeout=300,
        )

        output = result.stdout + result.stderr
        success = result.returncode == 0

        # Extract troshkad credentials from output
        troshkad_credentials = {}
        for line in output.splitlines():
            if line.startswith("TROSHKAD_CREDENTIALS:"):
                rest = line.split(":", 1)[1]
                troshkad_credentials = {
                    "token": rest[:64],
                    "fingerprint": rest[65:]
                }
                break

        logger.info("Agent deploy %s on %s (exit %d)", "succeeded" if success else "failed", host_ip, result.returncode)

        return {
            "success": success,
            "exit_code": result.returncode,
            "output": output,
            "troshkad_credentials": troshkad_credentials,
        }
    finally:
        os.unlink(key_path)
