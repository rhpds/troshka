"""
Agent deployer — installs the troshka agent on a remote host via SSH.

Uses the host's stored private key to connect and deploy.
"""

import logging
import os
import subprocess
import tempfile
import time

logger = logging.getLogger(__name__)


def get_provider_ssh_user(provider_type: str) -> str:
    """Return the SSH username for a given provider type."""
    if provider_type == "ec2":
        return "ec2-user"
    elif provider_type == "ocpvirt":
        return "cloud-user"
    elif provider_type in ("gcp", "azure"):
        return "troshka"
    raise ValueError(f"Unknown provider type: {provider_type}")


def get_provider_ssh_port(provider_type: str) -> int:
    """Return the SSH port for a given provider type."""
    if provider_type == "ocpvirt":
        from app.services.providers.ocpvirt import SSH_LB_PORT

        return SSH_LB_PORT
    return 22


def get_provider_data_disk(provider_type: str) -> str:
    """Return the data disk device path for a given provider type."""
    if provider_type in ("ec2", "ocpvirt"):
        return "sdf"
    elif provider_type == "gcp":
        return "/dev/sdb"
    elif provider_type == "azure":
        return "/dev/disk/azure/scsi1/lun0"
    raise ValueError(f"Unknown provider type: {provider_type}")


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
        python3 python3-libvirt dnsmasq nftables xorriso nmap-ncat sshpass || true
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

# Disable KSM — causes soft lockups with nested virtualization
echo 0 > /sys/kernel/mm/ksm/run 2>/dev/null || true
mkdir -p /etc/tmpfiles.d
cat > /etc/tmpfiles.d/ksm.conf << 'KSMEOF'
w /sys/kernel/mm/ksm/run - - - - 0
KSMEOF

# Allow SSH user to manage libvirt without polkit agent
usermod -aG libvirt {ssh_user}
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
# Troshka qemu hook — moves TAP interfaces into project namespace
# NOTE: Do NOT call virsh from this hook — it deadlocks virtqemud.
# Parse TAP and bridge names from the domain XML passed on stdin.
DOMAIN=$1
ACTION=$2
if [ "$ACTION" = "started" ]; then
    PID=$(echo "$DOMAIN" | sed -n 's/^troshka-\\([a-f0-9]*\\)-.*/\\1/p')
    [ -z "$PID" ] && exit 0
    NS="troshka-$PID"
    ip netns list 2>/dev/null | grep -q "^$NS " || exit 0
    XML=$(cat)
    # Extract all source bridges and target devs in order — they correspond 1:1
    BRIDGES=$(echo "$XML" | grep -oP "source bridge='\\K[^']+")
    TAPS=$(echo "$XML" | grep -oP "target dev='\\K(vnet|tap)[^']+")
    BRIDGE_ARR=($BRIDGES)
    TAP_ARR=($TAPS)
    for i in "${!TAP_ARR[@]}"; do
        TAP="${TAP_ARR[$i]}"
        BRIDGE="${BRIDGE_ARR[$i]}"
        [ -z "$TAP" ] && continue
        [ -z "$BRIDGE" ] && continue
        ip link set "$TAP" netns "$NS" 2>/dev/null
        ip netns exec "$NS" ip link set "$TAP" master "$BRIDGE" 2>/dev/null
        ip netns exec "$NS" ip link set "$TAP" up 2>/dev/null
    done
fi
HOOKEOF
chmod +x /etc/libvirt/hooks/qemu

# Restart libvirt to pick up hook changes
if systemctl is-active virtqemud &>/dev/null; then
    systemctl restart virtqemud
    systemctl start virtstoraged.socket virtnetworkd.socket 2>/dev/null || true
    echo "virtqemud restarted (modular sockets re-activated)"
elif systemctl is-active libvirtd &>/dev/null; then
    systemctl restart libvirtd
    echo "libvirtd restarted"
fi

# Ensure nvme-cli is installed for device detection
dnf install -y nvme-cli 2>/dev/null || true

# Detect NVMe device for a given /dev/sdX name via nvme id-ctrl
find_nvme_dev() {
    local target="$1"
    for dev in /dev/nvme*n1; do
        [ -b "$dev" ] || continue
        DEVNAME=$(nvme id-ctrl "$dev" -b 2>/dev/null | dd bs=1 skip=3072 count=32 2>/dev/null | tr -d '\\0 ')
        if [ "$DEVNAME" = "$target" ] || [ "$DEVNAME" = "/dev/$target" ]; then
            echo "$dev"; return
        fi
    done
}

# Mount dedicated storage volume if present and not already mounted
if mountpoint -q /var/lib/troshka; then
    echo "Storage volume already mounted at /var/lib/troshka"
else
    # Provider-specific device path: {data_disk_device}
    # EC2 uses NVMe translation (e.g., sdf -> /dev/nvme1n1)
    # GCP uses direct path (e.g., /dev/sdb)
    # Azure uses stable symlink (e.g., /dev/disk/azure/scsi1/lun0)
    if [[ "{data_disk_device}" == *"/"* ]]; then
        # Absolute path — use directly (GCP/Azure)
        DATA_DEV="{data_disk_device}"
    else
        # Logical name — translate via NVMe (EC2)
        DATA_DEV=$(find_nvme_dev {data_disk_device})
    fi
    if [ -n "$DATA_DEV" ] && [ -b "$DATA_DEV" ]; then
        echo "Mounting dedicated storage volume ($DATA_DEV)..."
        blkid "$DATA_DEV" || mkfs.xfs "$DATA_DEV"
        mkdir -p /var/lib/troshka
        mount "$DATA_DEV" /var/lib/troshka
        grep -q '/var/lib/troshka' /etc/fstab || \
            echo "$DATA_DEV /var/lib/troshka xfs defaults,nofail 0 2" >> /etc/fstab
        echo "Storage volume mounted at /var/lib/troshka"
    else
        echo "ERROR: No dedicated storage volume ({data_disk_device}) found — agent install cannot continue"
        exit 1
    fi
fi

# Detect and enable swap volume (/dev/sdg)
SWAP_DEV=$(find_nvme_dev sdg)
if [ -n "$SWAP_DEV" ]; then
    if swapon --show=NAME --noheadings | grep -q "$SWAP_DEV"; then
        echo "Swap already active on $SWAP_DEV"
    else
        echo "Setting up swap on $SWAP_DEV..."
        mkswap "$SWAP_DEV" 2>/dev/null || true
        swapon "$SWAP_DEV"
        grep -q "$SWAP_DEV" /etc/fstab || echo "$SWAP_DEV none swap defaults,nofail 0 0" >> /etc/fstab
        echo "Swap enabled on $SWAP_DEV ($(swapon --show=SIZE --noheadings --bytes "$SWAP_DEV" 2>/dev/null | awk '{printf "%.0f GB", $1/1024/1024/1024}'))"
    fi
else
    echo "WARNING: No swap volume found — memory overcommit may fail for large VMs"
fi

# Kernel tuning for VM memory overcommit
sysctl -w vm.overcommit_memory=1 >/dev/null
sysctl -w vm.swappiness=10 >/dev/null
cat > /etc/sysctl.d/99-troshka.conf << 'SYSCTLEOF'
vm.overcommit_memory = 1
vm.swappiness = 10
SYSCTLEOF
echo "Kernel tuning applied (overcommit=1, swappiness=10)"

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

# Deploy mTLS CA cert (for verifying backend client connections)
if [ -n "{agent_ca_cert_b64}" ]; then
    echo "{agent_ca_cert_b64}" | base64 -d > /opt/troshka/tls/ca.crt
    chmod 644 /opt/troshka/tls/ca.crt
    echo "troshkad: mTLS CA certificate installed"
fi

# Configure libvirt TLS for live migration (shared storage pools)
if [ "{storage_mode}" = "shared" ]; then
    mkdir -p /etc/pki/CA /etc/pki/libvirt/private

    # Install pool CA cert and host cert (signed by CA, injected by backend)
    if [ -n "{ca_cert_b64}" ]; then
        echo "{ca_cert_b64}" | base64 -d > /etc/pki/CA/cacert.pem
        echo "{host_cert_b64}" | base64 -d > /etc/pki/libvirt/servercert.pem
        echo "{host_key_b64}" | base64 -d > /etc/pki/libvirt/private/serverkey.pem
        # Client cert = same as server cert (for outgoing migration connections)
        cp /etc/pki/libvirt/servercert.pem /etc/pki/libvirt/clientcert.pem
        cp /etc/pki/libvirt/private/serverkey.pem /etc/pki/libvirt/private/clientkey.pem
    fi
    chmod 600 /etc/pki/libvirt/private/serverkey.pem /etc/pki/libvirt/private/clientkey.pem 2>/dev/null

    # Enable libvirt TLS listening with cert verification
    mkdir -p /etc/libvirt
    cat > /etc/libvirt/libvirtd.conf << 'LVEOF'
listen_tls = 1
listen_tcp = 0
LVEOF
    # For modular daemons (virtqemud)
    if systemctl list-unit-files virtproxyd.socket &>/dev/null; then
        systemctl enable --now virtproxyd-tls.socket 2>/dev/null || true
    fi
    systemctl restart virtqemud 2>/dev/null || systemctl restart libvirtd 2>/dev/null || true
    systemctl start virtstoraged.socket virtnetworkd.socket 2>/dev/null || true
    echo "troshkad: libvirt TLS configured with pool CA"
fi

# Ensure NFS mount for shared storage (idempotent — safe to run on every install/reinstall)
if [ "{storage_mode}" = "shared" ] && [ -n "{nfs_server}" ]; then
    # Create local dirs first (never on NFS)
    mkdir -p /var/lib/troshka/local /var/lib/troshka/seeds
    NFS_SRC="{nfs_server}:{nfs_path}"
    NFS_DST="/var/lib/troshka/shared"
    NFS_PORT="{nfs_port}"
    NFS_OPTS="nfsvers=4.1,nconnect=16,soft,timeo=50,retrans=3,_netdev"
    if [ -n "$NFS_PORT" ] && [ "$NFS_PORT" != "0" ]; then
        NFS_OPTS="port=$NFS_PORT,$NFS_OPTS"
    fi
    if mountpoint -q "$NFS_DST" 2>/dev/null; then
        # Check if existing mount is stale (stat with timeout)
        if ! timeout 5 stat "$NFS_DST" &>/dev/null; then
            echo "troshkad: NFS mount at $NFS_DST is stale, lazy unmounting..."
            umount -l "$NFS_DST" 2>/dev/null || true
            sleep 2
        else
            echo "troshkad: NFS already mounted at $NFS_DST"
        fi
    fi
    if ! mountpoint -q "$NFS_DST" 2>/dev/null; then
        mkdir -p "$NFS_DST"
        echo "troshkad: mounting NFS $NFS_SRC -> $NFS_DST (opts: $NFS_OPTS)"
        mount -t nfs -o "$NFS_OPTS" "$NFS_SRC" "$NFS_DST"
    fi
    grep -q "$NFS_DST" /etc/fstab || echo "$NFS_SRC $NFS_DST nfs4 $NFS_OPTS 0 0" >> /etc/fstab
    setsebool -P virt_use_nfs 1 2>/dev/null || true
fi

# Generate config with token if not present
if [ ! -f /opt/troshka/troshkad.conf ]; then
    TROSHKAD_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    python3 -c "
import json, sys
conf = {
    'port': 31337,
    'token': sys.argv[1],
    'tls_cert': '/opt/troshka/tls/server.crt',
    'tls_key': '/opt/troshka/tls/server.key',
    'host_id': '{host_id}',
    'max_concurrent_jobs': 16,
    'drain_timeout_seconds': 300,
    'storage_mode': '{storage_mode}',
    'shared_mount': '/var/lib/troshka/shared',
    'local_mount': '/var/lib/troshka/local',
}
json.dump(conf, open('/opt/troshka/troshkad.conf', 'w'), indent=2)
" "$TROSHKAD_TOKEN"
    chmod 600 /opt/troshka/troshkad.conf
    echo "troshkad: config generated"
else
    # Validate existing config is valid JSON, fix if corrupted
    python3 -c "import json; json.load(open('/opt/troshka/troshkad.conf'))" 2>/dev/null || {
        echo "troshkad: config corrupted, regenerating"
        TROSHKAD_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
        python3 -c "
import json, sys
conf = {
    'port': 31337,
    'token': sys.argv[1],
    'tls_cert': '/opt/troshka/tls/server.crt',
    'tls_key': '/opt/troshka/tls/server.key',
    'host_id': '{host_id}',
    'max_concurrent_jobs': 16,
    'drain_timeout_seconds': 300,
    'storage_mode': '{storage_mode}',
    'shared_mount': '/var/lib/troshka/shared',
    'local_mount': '/var/lib/troshka/local',
}
json.dump(conf, open('/opt/troshka/troshkad.conf', 'w'), indent=2)
" "$TROSHKAD_TOKEN"
        chmod 600 /opt/troshka/troshkad.conf
    }
    # Update storage_mode and client_ca in existing config
    python3 -c "
import json, os
conf = json.load(open('/opt/troshka/troshkad.conf'))
conf['storage_mode'] = '{storage_mode}'
conf['shared_mount'] = '/var/lib/troshka/shared'
conf['local_mount'] = '/var/lib/troshka/local'
if os.path.isfile('/opt/troshka/tls/ca.crt'):
    conf['client_ca'] = '/opt/troshka/tls/ca.crt'
json.dump(conf, open('/opt/troshka/troshkad.conf', 'w'), indent=2)
"
    echo "troshkad: config already exists, updated"
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
KillMode=process
TimeoutStopSec=130

[Install]
WantedBy=multi-user.target
SYSTEMDEOF

systemctl daemon-reload
systemctl enable troshkad
systemctl restart troshkad

# Open firewall port
if which firewall-cmd &>/dev/null; then
    firewall-cmd --add-port=31337/tcp --permanent 2>/dev/null || true
    firewall-cmd --reload 2>/dev/null || true
fi

echo "troshkad: service started"

# Write vncd systemd unit
cat > /etc/systemd/system/troshka-vncd.service << 'SYSTEMDEOF'
[Unit]
Description=Troshka VNC Console Proxy Daemon
After=network.target troshkad.service

[Service]
Type=simple
ExecStart=/opt/troshka/venv/bin/python3 /opt/troshka/troshka-vncd.py
WorkingDirectory=/opt/troshka
Restart=always
RestartSec=5
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
SYSTEMDEOF

# Console TLS via Let's Encrypt (only if console_domain is set)
CONSOLE_DOMAIN="{console_domain}"
VNCD_NO_TLS="{vncd_no_tls}"
if [ -n "$VNCD_NO_TLS" ]; then
    # OCP Virt: TLS terminated by OCP router, vncd runs plain on 8080
    sed -i 's|ExecStart=.*troshka-vncd.py.*|ExecStart=/opt/troshka/venv/bin/python3 /opt/troshka/troshka-vncd.py --no-tls|' \
        /etc/systemd/system/troshka-vncd.service
    systemctl daemon-reload
    systemctl enable troshka-vncd
    systemctl restart troshka-vncd
    echo "vncd: started in no-TLS mode (port 8080)"
elif [ -n "$CONSOLE_DOMAIN" ]; then
    echo "=== Setting up console TLS ==="
    /opt/troshka/venv/bin/pip install $PIP_ARGS certbot certbot-dns-route53
    /opt/troshka/venv/bin/certbot certonly --dns-route53 \
        -d "$CONSOLE_DOMAIN" \
        --non-interactive --agree-tos -m noreply@redhat.com \
        --preferred-challenges dns-01 2>&1 || echo "certbot: initial cert may have failed (will retry)"

    # Auto-renewal cron
    echo "0 3 * * * root /opt/troshka/venv/bin/certbot renew --quiet" > /etc/cron.d/certbot-renew

    # Store console_domain in troshkad config for vncd to find
    python3 -c "
import json
conf = json.load(open('/opt/troshka/troshkad.conf'))
conf['console_domain'] = '$CONSOLE_DOMAIN'
json.dump(conf, open('/opt/troshka/troshkad.conf', 'w'), indent=2)
"

    # Open port 443
    if which firewall-cmd &>/dev/null; then
        firewall-cmd --add-port=443/tcp --permanent 2>/dev/null || true
        firewall-cmd --reload 2>/dev/null || true
    fi

    systemctl daemon-reload
    systemctl enable troshka-vncd
    systemctl restart troshka-vncd
    echo "vncd: started with Let's Encrypt cert for $CONSOLE_DOMAIN"
else
    echo "vncd: no console_domain, skipping TLS setup"
fi

# BMC tools venv (sushy-tools for Redfish, virtualbmc for IPMI)
# Uses --system-site-packages to access the system python3-libvirt RPM
# (libvirt-devel is not available on RHEL 10 so libvirt-python can't compile from source)
echo "=== Setting up BMC tools venv ==="
rm -rf /opt/troshka/venv
python3 -m venv --system-site-packages /opt/troshka/venv
PIP_ARGS="--quiet"
# Check if libvirt is available from system site-packages; if so, skip libvirt-python
# (libvirt-devel is not available on RHEL 10 so it can't compile from source)
if /opt/troshka/venv/bin/python3 -c "import libvirt" 2>/dev/null; then
    echo "System libvirt module available, installing without libvirt-python"
    /opt/troshka/venv/bin/pip install $PIP_ARGS --no-deps sushy-tools
    # Install sushy-tools runtime deps (except libvirt-python which comes from system RPM)
    /opt/troshka/venv/bin/pip install $PIP_ARGS flask requests tenacity bcrypt webob pbr
    /opt/troshka/venv/bin/pip install $PIP_ARGS virtualbmc
else
    echo "No system libvirt, attempting full install"
    /opt/troshka/venv/bin/pip install $PIP_ARGS sushy-tools virtualbmc
fi
/opt/troshka/venv/bin/pip install $PIP_ARGS pexpect awscli websockets
echo "BMC venv ready at /opt/troshka/venv"

# Install oc CLI for direct OCP access (bastion-optional)
if ! command -v oc &>/dev/null; then
    echo "Installing oc CLI..."
    curl -sL https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz \\
        | tar xzf - -C /usr/local/bin oc kubectl
    echo "oc $(oc version --client 2>/dev/null | head -1) installed"
fi

# Output credentials for backend to capture (tab-separated to avoid colon ambiguity)
CERT_FP=$(openssl x509 -in /opt/troshka/tls/server.crt -noout -fingerprint -sha256 2>/dev/null | sed 's/.*=//')
TOKEN=$(python3 -c "import json; print(json.load(open('/opt/troshka/troshkad.conf'))['token'])")
echo "TROSHKAD_TOKEN=$TOKEN"
echo "TROSHKAD_FINGERPRINT=$CERT_FP"

# Report status
echo "=== Agent installation complete ==="
echo "Host ID: {host_id}"
echo "Libvirt: $(virsh version --daemon 2>/dev/null | head -1 || echo 'not running')"
echo "KVM: $(ls /dev/kvm 2>/dev/null && echo 'available' || echo 'NOT available')"
"""


def wait_for_ssh(
    host_ip: str,
    private_key: str,
    timeout: int = 300,
    port: int = 22,
    ssh_user: str = "ec2-user",
) -> bool:
    """Wait for SSH to become available on the host."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as kf:
        kf.write(private_key)
        key_path = kf.name
    os.chmod(key_path, 0o600)

    try:
        start = time.time()
        attempt = 0
        while time.time() - start < timeout:
            attempt += 1
            cmd = [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-i",
                key_path,
            ]
            if port != 22:
                cmd.extend(["-p", str(port)])
            cmd.extend([f"{ssh_user}@{host_ip}", "echo", "ssh-ready"])
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0 and "ssh-ready" in result.stdout:
                logger.info(
                    "SSH ready on %s after %ds (%d attempts)",
                    host_ip,
                    int(time.time() - start),
                    attempt,
                )
                return True
            elapsed = int(time.time() - start)
            err = (
                result.stderr.strip().split("\n")[-1]
                if result.stderr.strip()
                else f"exit {result.returncode}"
            )
            if attempt % 6 == 1:
                logger.info(
                    "Waiting for SSH on %s:%d (%ds elapsed, attempt %d: %s)",
                    host_ip,
                    port,
                    elapsed,
                    attempt,
                    err,
                )
            time.sleep(5)
        logger.warning(
            "SSH timeout on %s after %ds (%d attempts)", host_ip, timeout, attempt
        )
        return False
    finally:
        os.unlink(key_path)


PATTERN_BUFFER_INSTALL_SCRIPT = """#!/bin/bash
set -uo pipefail

echo "=== Troshka Pattern Buffer Agent Installer ==="
cloud-init status --wait 2>/dev/null || true

if ! which qemu-img &>/dev/null; then
    dnf install -y python3 python3-pip qemu-img nvme-cli sshpass || true
fi

mkdir -p /opt/troshka/tls /opt/troshka/venv /var/lib/troshka/local/tmp /etc/troshka-agent
python3 -m venv /opt/troshka/venv 2>/dev/null || true
/opt/troshka/venv/bin/pip install --quiet awscli pexpect 2>/dev/null || true

# TLS certs — self-signed for troshkad HTTPS
if [ ! -f /opt/troshka/tls/server.crt ]; then
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
        -keyout /opt/troshka/tls/server.key -out /opt/troshka/tls/server.crt \
        -days 3650 -nodes -subj "/CN=troshka-agent" 2>/dev/null
    chmod 600 /opt/troshka/tls/server.key
fi

# Deploy mTLS CA cert (for verifying backend client connections)
if [ -n "{agent_ca_cert_b64}" ]; then
    echo "{agent_ca_cert_b64}" | base64 -d > /opt/troshka/tls/ca.crt
    chmod 644 /opt/troshka/tls/ca.crt
fi

if [ ! -f /opt/troshka/troshkad.conf ]; then
    TROSHKAD_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    python3 -c "
import json, sys
json.dump({
    'token': sys.argv[1],
    'port': 31337,
    'tls_cert': '/opt/troshka/tls/server.crt',
    'tls_key': '/opt/troshka/tls/server.key',
    'storage_mode': '{storage_mode}',
    'local_mount': '/var/lib/troshka/local',
}, open('/opt/troshka/troshkad.conf', 'w'), indent=2)
" "$TROSHKAD_TOKEN"
fi

# Update client_ca in config if CA cert was deployed
python3 -c "
import json, os
conf = json.load(open('/opt/troshka/troshkad.conf'))
if os.path.isfile('/opt/troshka/tls/ca.crt'):
    conf['client_ca'] = '/opt/troshka/tls/ca.crt'
json.dump(conf, open('/opt/troshka/troshkad.conf', 'w'), indent=2)
"

echo "host_id: {host_id}" > /etc/troshka-agent/host-id

cp /tmp/troshkad.py /opt/troshka/troshkad.py
chmod +x /opt/troshka/troshkad.py

cat > /etc/systemd/system/troshkad.service << 'SVCEOF'
[Unit]
Description=Troshka Host Agent Daemon
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/troshka/troshkad.py
Restart=always
RestartSec=5
KillMode=process
TimeoutStopSec=130

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable --now troshkad

CERT_FP=$(openssl x509 -in /opt/troshka/tls/server.crt -noout -fingerprint -sha256 2>/dev/null | sed 's/.*=//')
TOKEN=$(python3 -c "import json; print(json.load(open('/opt/troshka/troshkad.conf'))['token'])")
echo "TROSHKAD_TOKEN=$TOKEN"
echo "TROSHKAD_FINGERPRINT=$CERT_FP"

echo "=== Pattern buffer agent installation complete ==="
"""


def deploy_agent(
    host_ip: str,
    private_key: str,
    host_id: str,
    api_url: str = "",
    storage_mode: str = "local",
    nfs_server: str = "",
    nfs_path: str = "",
    nfs_port: int = 0,
    ca_cert: str = "",
    host_cert: str = "",
    host_key: str = "",
    console_domain: str = "",
    vncd_no_tls: bool = False,
    host_type: str = "shared",
    ssh_port: int = 22,
    ssh_user: str = "ec2-user",
    data_disk_device: str = "sdf",
    agent_ca_cert: str = "",
) -> dict:
    """Deploy the troshka agent to a remote host via SSH."""
    import base64

    from app.core.config import config

    actual_api_url = api_url or getattr(config.app, "external_url", "")
    if not actual_api_url:
        logger.warning(
            "No external_url configured — agent will not be able to call back to the API"
        )
    base_script = (
        PATTERN_BUFFER_INSTALL_SCRIPT
        if host_type == "pattern_buffer"
        else AGENT_INSTALL_SCRIPT
    )
    script = (
        base_script.replace("{host_id}", host_id)
        .replace("{api_url}", actual_api_url)
        .replace("{storage_mode}", storage_mode)
        .replace("{nfs_server}", nfs_server)
        .replace("{nfs_path}", nfs_path)
        .replace("{nfs_port}", str(nfs_port))
        .replace(
            "{ca_cert_b64}",
            base64.b64encode(ca_cert.encode()).decode() if ca_cert else "",
        )
        .replace(
            "{host_cert_b64}",
            base64.b64encode(host_cert.encode()).decode() if host_cert else "",
        )
        .replace(
            "{host_key_b64}",
            base64.b64encode(host_key.encode()).decode() if host_key else "",
        )
        .replace("{console_domain}", console_domain)
        .replace("{vncd_no_tls}", "1" if vncd_no_tls else "")
        .replace("{ssh_user}", ssh_user)
        .replace("{data_disk_device}", data_disk_device)
        .replace(
            "{agent_ca_cert_b64}",
            base64.b64encode(agent_ca_cert.encode()).decode() if agent_ca_cert else "",
        )
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as kf:
        kf.write(private_key)
        key_path = kf.name
    os.chmod(key_path, 0o600)

    try:
        ssh_opts = [
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=30",
            "-o",
            "IdentitiesOnly=yes",
            "-i",
            key_path,
        ]
        ssh_port_opts = ["-p", str(ssh_port)] if ssh_port != 22 else []
        scp_port_opts = ["-P", str(ssh_port)] if ssh_port != 22 else []

        logger.info(
            "Deploying agent to %s (user=%s, port=%d)", host_ip, ssh_user, ssh_port
        )
        deploy_start = time.time()

        # SCP troshkad.py to host
        troshkad_path = os.path.join(
            os.path.dirname(
                os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
            ),
            "troshkad",
            "troshkad.py",
        )
        if os.path.exists(troshkad_path):
            logger.info("Copying troshkad.py to %s", host_ip)
            scp_result = subprocess.run(
                [
                    "scp",
                    *scp_port_opts,
                    *ssh_opts,
                    troshkad_path,
                    f"{ssh_user}@{host_ip}:/tmp/troshkad.py",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if scp_result.returncode != 0:
                logger.warning("SCP troshkad.py failed: %s", scp_result.stderr)
            else:
                subprocess.run(
                    [
                        "ssh",
                        *ssh_opts,
                        *ssh_port_opts,
                        f"{ssh_user}@{host_ip}",
                        "sudo",
                        "mkdir",
                        "-p",
                        "/opt/troshka",
                    ],
                    capture_output=True,
                    timeout=60,
                )
                subprocess.run(
                    [
                        "ssh",
                        *ssh_opts,
                        *ssh_port_opts,
                        f"{ssh_user}@{host_ip}",
                        "sudo",
                        "mv",
                        "/tmp/troshkad.py",
                        "/opt/troshka/troshkad.py",
                    ],
                    capture_output=True,
                    timeout=30,
                )
        else:
            logger.warning("troshkad.py not found at %s", troshkad_path)

        # SCP vncd.py to host
        vncd_path = os.path.join(
            os.path.dirname(
                os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
            ),
            "troshka-vncd",
            "troshka-vncd.py",
        )
        if os.path.exists(vncd_path):
            logger.info("Copying troshka-vncd.py to %s", host_ip)
            scp_result = subprocess.run(
                [
                    "scp",
                    *scp_port_opts,
                    *ssh_opts,
                    vncd_path,
                    f"{ssh_user}@{host_ip}:/tmp/troshka-vncd.py",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if scp_result.returncode != 0:
                logger.warning("SCP troshka-vncd.py failed: %s", scp_result.stderr)
            else:
                subprocess.run(
                    [
                        "ssh",
                        *ssh_opts,
                        *ssh_port_opts,
                        f"{ssh_user}@{host_ip}",
                        "sudo",
                        "mv",
                        "/tmp/troshka-vncd.py",
                        "/opt/troshka/troshka-vncd.py",
                    ],
                    capture_output=True,
                    timeout=30,
                )
        else:
            logger.warning("troshka-vncd.py not found at %s", vncd_path)

        # Copy utility scripts
        tools_dir = os.path.dirname(troshkad_path)
        infra_dir = os.path.join(os.path.dirname(os.path.dirname(tools_dir)), "infra")
        troshka_files = os.path.join(infra_dir, "troshka-fs-monitor.sh")
        if os.path.exists(troshka_files):
            subprocess.run(
                [
                    "scp",
                    *scp_port_opts,
                    *ssh_opts,
                    troshka_files,
                    f"{ssh_user}@{host_ip}:/tmp/troshka-fs-monitor.sh",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            subprocess.run(
                [
                    "ssh",
                    *ssh_opts,
                    *ssh_port_opts,
                    f"{ssh_user}@{host_ip}",
                    "sudo",
                    "mv",
                    "/tmp/troshka-fs-monitor.sh",
                    "/usr/local/bin/troshka-fs-monitor",
                    "&&",
                    "sudo",
                    "chmod",
                    "+x",
                    "/usr/local/bin/troshka-fs-monitor",
                ],
                capture_output=True,
                timeout=60,
            )

        # Run install script (sets up system config, qemu hook, restarts virtqemud)
        logger.info("Running install script on %s", host_ip)
        try:
            result = subprocess.run(
                [
                    "ssh",
                    *ssh_opts,
                    *ssh_port_opts,
                    f"{ssh_user}@{host_ip}",
                    "sudo",
                    "bash",
                    "-s",
                ],
                input=script,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired as te:
            stdout = te.stdout if isinstance(te.stdout, str) else ""
            stderr = te.stderr if isinstance(te.stderr, str) else ""
            partial = stdout + stderr
            last_lines = "\n".join(partial.strip().splitlines()[-15:])
            logger.error(
                "Install script timed out on %s after 300s. Last output:\n%s",
                host_ip,
                last_lines,
            )
            return {
                "success": False,
                "exit_code": -1,
                "output": partial,
                "troshkad_credentials": {},
            }

        output = result.stdout + result.stderr
        success = result.returncode == 0

        # Extract troshkad credentials from output
        troshkad_credentials = {}
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("TROSHKAD_TOKEN="):
                troshkad_credentials["token"] = line.split("=", 1)[1]
            elif line.startswith("TROSHKAD_FINGERPRINT="):
                troshkad_credentials["fingerprint"] = line.split("=", 1)[1]

        elapsed = int(time.time() - deploy_start)
        logger.info(
            "Agent deploy %s on %s (exit %d, %ds)",
            "succeeded" if success else "failed",
            host_ip,
            result.returncode,
            elapsed,
        )
        if not success:
            last_lines = "\n".join(output.strip().splitlines()[-10:])
            logger.warning("Agent deploy output (last 10 lines):\n%s", last_lines)

        return {
            "success": success,
            "exit_code": result.returncode,
            "output": output,
            "troshkad_credentials": troshkad_credentials,
        }
    finally:
        os.unlink(key_path)
