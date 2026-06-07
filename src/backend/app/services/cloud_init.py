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
    from passlib.hash import sha512_crypt

    lines = ["#cloud-config"]

    hostname = vm_data.get("ciHostname") or vm_data.get("name", "localhost")
    lines.append(f"hostname: {hostname}")
    lines.append(f"fqdn: {hostname}")

    # SSH keys — injected for all users
    ssh_keys = vm_data.get("ciSshKeys", [])
    ssh_key = vm_data.get("ciSshKey", "").strip()
    all_keys = [k.strip() for k in ssh_keys if k.strip()] if ssh_keys else ([ssh_key] if ssh_key else [])
    if all_keys:
        lines.append("ssh_authorized_keys:")
        for key in all_keys:
            lines.append(f"  - {key}")

    # Passwords
    root_pw = vm_data.get("ciRootPassword", "")
    cloud_user_pw = vm_data.get("ciCloudUserPassword", "")
    root_hash = sha512_crypt.using(rounds=5000).hash(root_pw) if root_pw else None
    cloud_user_hash = sha512_crypt.using(rounds=5000).hash(cloud_user_pw) if cloud_user_pw else None

    chpasswd_users = []
    if root_hash:
        chpasswd_users.append({"name": "root", "password": root_hash, "type": "hash"})
    if cloud_user_hash:
        chpasswd_users.append({"name": "cloud-user", "password": cloud_user_hash, "type": "hash"})

    if chpasswd_users:
        lines.append("chpasswd:")
        lines.append("  expire: false")
        lines.append("  users:")
        for u in chpasswd_users:
            lines.append(f"    - name: {u['name']}")
            lines.append(f"      password: {u['password']}")
            lines.append(f"      type: {u['type']}")
        lines.append("ssh_pwauth: true")

    # Users
    lines.append("disable_root: false")
    cloud_user_sudo = vm_data.get("ciCloudUserSudo", True)
    lines.append("users:")
    lines.append("  - default")
    if root_hash:
        lines.append("  - name: root")
        lines.append("    lock_passwd: false")
    lines.append("  - name: cloud-user")
    lines.append("    lock_passwd: false")
    if all_keys:
        lines.append("    ssh_authorized_keys:")
        for key in all_keys:
            lines.append(f"      - {key}")
    if cloud_user_sudo:
        lines.append("    sudo: ALL=(ALL) NOPASSWD:ALL")
        lines.append("    groups: wheel")

    # Eject seed ISO after boot
    lines.append("runcmd:")
    lines.append("  - eject /dev/sr0 2>/dev/null || true")
    lines.append("  - eject /dev/sr1 2>/dev/null || true")

    # Custom user-data — validate YAML before appending
    custom = vm_data.get("ciUserData", "").strip()
    if custom:
        import yaml
        try:
            parsed = yaml.safe_load(custom)
            if isinstance(parsed, dict):
                for line in custom.split("\n"):
                    if line.strip().startswith("#cloud-config"):
                        continue
                    lines.append(line)
            elif parsed is not None:
                logger.warning("Custom user-data is not a YAML mapping, skipping: %s", repr(custom)[:100])
        except yaml.YAMLError as e:
            logger.warning("Invalid YAML in custom user-data, skipping: %s", e)

    return "\n".join(lines)


def generate_metadata(vm_name: str, mac: str = "") -> str:
    """Generate cloud-init meta-data JSON for a VM."""
    import uuid
    return json.dumps({
        "instance-id": f"{vm_name}-{uuid.uuid4().hex[:8]}",
        "local-hostname": vm_name,
    })


def generate_seed_iso_script(project_id: str, topology: dict) -> str:
    """Generate a script to create NoCloud seed ISOs for each VM with cloud-init enabled."""
    nodes = topology.get("nodes", [])
    vm_dir = f"/var/lib/troshka/vms/{project_id}"
    lines = ["#!/bin/bash", f"mkdir -p {vm_dir}", ""]

    for node in nodes:
        if node.get("type") != "vmNode":
            continue
        data = node.get("data", {})
        if not data.get("cloudInit"):
            continue

        node_id = node["id"]
        vm_label = data.get("name", "vm")
        from app.services.deploy_service import _vm_domain_name
        vm_name = _vm_domain_name(project_id, node_id)
        userdata = generate_userdata(data)
        metadata = generate_metadata(vm_label)

        seed_dir = f"/var/lib/troshka/tmp/seed-{node_id[:8]}"
        seed_iso = f"{vm_dir}/{node_id[:8]}-seed.iso"

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
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])

    vm_configs = {}
    for node in nodes:
        if node.get("type") != "vmNode":
            continue
        data = node.get("data", {})
        if not data.get("cloudInit"):
            continue

        vm_label = data.get("name", "vm")
        userdata = generate_userdata(data)
        metadata = generate_metadata(vm_label)

        # Find MAC addresses for this VM
        for nic in data.get("nics", []):
            mac = nic.get("mac", "").lower()
            if mac:
                vm_configs[mac] = {
                    "vm_name": vm_label,
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
