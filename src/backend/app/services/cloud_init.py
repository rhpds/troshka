"""
Cloud-init metadata service.

Generates user-data and meta-data for VMs, and creates a lightweight
HTTP metadata service script to run on the host bridge.
"""
import json
import logging

logger = logging.getLogger(__name__)


def generate_userdata(vm_data: dict) -> str:
    """Generate cloud-init user-data YAML for a VM."""
    lines = ["#cloud-config"]

    hostname = vm_data.get("ciHostname") or vm_data.get("name", "localhost")
    lines.append(f"hostname: {hostname}")
    lines.append(f"fqdn: {hostname}")

    ssh_keys = vm_data.get("ciSshKeys", [])
    ssh_key = vm_data.get("ciSshKey", "").strip()
    if ssh_keys:
        lines.append("ssh_authorized_keys:")
        for key in ssh_keys:
            if key.strip():
                lines.append(f"  - {key.strip()}")
    elif ssh_key:
        lines.append("ssh_authorized_keys:")
        lines.append(f"  - {ssh_key}")

    root_pw = vm_data.get("ciRootPassword", "")
    if root_pw:
        from passlib.hash import sha512_crypt
        pw_hash = sha512_crypt.using(rounds=5000).hash(root_pw)
        lines.append("chpasswd:")
        lines.append("  expire: false")
        lines.append("  users:")
        lines.append(f"    - name: root")
        lines.append(f"      password: {pw_hash}")
        lines.append("ssh_pwauth: true")

    lines.append("disable_root: false")
    lines.append("users:")
    lines.append("  - default")
    if root_pw:
        lines.append("  - name: root")
        lines.append(f"    hashed_passwd: {pw_hash}")
        lines.append("    lock_passwd: false")

    lines.append("runcmd:")
    lines.append("  - eject /dev/sr0 2>/dev/null || true")
    lines.append("  - eject /dev/sr1 2>/dev/null || true")

    custom = vm_data.get("ciUserData", "").strip()
    if custom:
        for line in custom.split("\n"):
            if line.strip().startswith("#cloud-config"):
                continue
            lines.append(line)

    return "\n".join(lines)


def generate_metadata(vm_name: str, mac: str = "") -> str:
    """Generate cloud-init meta-data JSON for a VM."""
    return json.dumps({
        "instance-id": vm_name,
        "local-hostname": vm_name,
    })


def generate_seed_iso_script(project_id: str, topology: dict) -> str:
    """Generate a script to create NoCloud seed ISOs for each VM with cloud-init enabled."""
    prefix = f"troshka-{project_id[:8]}"
    nodes = topology.get("nodes", [])
    lines = ["#!/bin/bash", ""]

    for node in nodes:
        if node.get("type") != "vmNode":
            continue
        data = node.get("data", {})
        if not data.get("cloudInit"):
            continue

        vm_name = f"{prefix}-{data.get('name', 'vm')}"
        userdata = generate_userdata(data)
        metadata = generate_metadata(vm_name)

        seed_dir = f"/tmp/troshka-seed-{vm_name}"
        seed_iso = f"/var/lib/troshka/vms/{vm_name}-seed.iso"

        lines.append(f"mkdir -p {seed_dir}")
        lines.append(f"cat > {seed_dir}/user-data << 'USERDATA'")
        lines.append(userdata)
        lines.append("USERDATA")
        lines.append(f"cat > {seed_dir}/meta-data << 'METADATA'")
        lines.append(metadata)
        lines.append("METADATA")
        lines.append(f"genisoimage -output {seed_iso} -volid cidata -joliet -rock {seed_dir}/user-data {seed_dir}/meta-data 2>/dev/null || mkisofs -output {seed_iso} -volid cidata -joliet -rock {seed_dir}/user-data {seed_dir}/meta-data")
        lines.append(f"rm -rf {seed_dir}")
        lines.append(f'echo "Seed ISO created for {vm_name}"')
        lines.append("")

    if len(lines) <= 2:
        return ""
    return "\n".join(lines)


def generate_metadata_service_script(project_id: str, topology: dict, vni_map: dict) -> str:
    """Generate a Python HTTP metadata service that runs on the host bridge.

    The service listens on 169.254.169.254:80 and serves per-VM
    user-data and meta-data based on the requesting IP (mapped via DHCP lease).
    """
    prefix = f"troshka-{project_id[:8]}"
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])

    # Build VM data map: MAC → user-data
    vm_configs = {}
    for node in nodes:
        if node.get("type") != "vmNode":
            continue
        data = node.get("data", {})
        if not data.get("cloudInit"):
            continue

        vm_name = f"{prefix}-{data.get('name', 'vm')}"
        userdata = generate_userdata(data)
        metadata = generate_metadata(vm_name)

        # Find MAC addresses for this VM
        for nic in data.get("nics", []):
            mac = nic.get("mac", "").lower()
            if mac:
                vm_configs[mac] = {
                    "vm_name": vm_name,
                    "userdata": userdata,
                    "metadata": metadata,
                }

    if not vm_configs:
        return ""

    # Find bridge names from VNI map
    bridges = [f"br-{vni}" for vni in vni_map.values()]

    configs_json = json.dumps(vm_configs)

    return f"""#!/bin/bash
# Troshka cloud-init metadata service for project {project_id[:8]}
# Serves user-data/meta-data on 169.254.169.254 via bridge IP

# Kill any existing metadata service for this project
pkill -9 -f "metadata-{project_id[:8]}.py" 2>/dev/null || true
sleep 1
# Also kill anything on port 80 of 169.254.169.254
fuser -k 80/tcp 2>/dev/null || true
sleep 1

# Add route for metadata IP on each bridge
for br in {' '.join(bridges)}; do
  ip addr add 169.254.169.254/32 dev $br 2>/dev/null || true
done

# Write the metadata service script
cat > /opt/troshka-agent/metadata-{project_id[:8]}.py << 'METAEOF'
import http.server
import json
import subprocess
import sys

CONFIGS = {configs_json}

def get_mac_for_ip(ip):
    \"\"\"Look up MAC address from IP via ARP table.\"\"\"
    try:
        result = subprocess.run(["ip", "neigh", "show", ip], capture_output=True, text=True)
        for line in result.stdout.strip().split("\\n"):
            parts = line.split()
            if len(parts) >= 5 and parts[0] == ip:
                return parts[4].lower()
    except Exception:
        pass
    return None

class MetadataHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        client_ip = self.client_address[0]
        mac = get_mac_for_ip(client_ip)

        config = CONFIGS.get(mac, {{}})

        meta = json.loads(config.get("metadata", "{{}}"))
        vm_name = config.get("vm_name", "troshka-vm")

        if self.path in ("/latest/user-data", "/latest/user-data/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/yaml")
            self.end_headers()
            self.wfile.write(config.get("userdata", "").encode())
        elif self.path in ("/latest/meta-data/", "/latest/meta-data"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ami-id\\ninstance-id\\nlocal-hostname\\nhostname\\ninstance-type\\n")
        elif self.path == "/latest/meta-data/instance-id":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(meta.get("instance-id", vm_name).encode())
        elif self.path in ("/latest/meta-data/local-hostname", "/latest/meta-data/hostname"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(meta.get("local-hostname", vm_name).encode())
        elif self.path == "/latest/meta-data/ami-id":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"troshka-image")
        elif self.path == "/latest/meta-data/instance-type":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"troshka.nested")
        elif self.path in ("/", "/latest", "/latest/"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"latest\\n")
        else:
            self.send_response(200)
            self.end_headers()

import socketserver
socketserver.TCPServer.allow_reuse_address = True
server = http.server.HTTPServer(("169.254.169.254", 80), MetadataHandler)
print(f"Metadata service running on 169.254.169.254:80")
server.serve_forever()
METAEOF

# Start the metadata service in background
nohup python3 /opt/troshka-agent/metadata-{project_id[:8]}.py > /var/log/troshka-metadata-{project_id[:8]}.log 2>&1 &
echo "Metadata service started (PID $!)"
"""
