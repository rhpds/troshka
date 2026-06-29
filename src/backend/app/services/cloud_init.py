"""
Cloud-init metadata service.

Generates user-data and meta-data for VMs, and creates a lightweight
HTTP metadata service script to run on the host bridge.
"""

import json
import logging

logger = logging.getLogger(__name__)


def _sha512_crypt(password: str, rounds: int = 5000) -> str:
    """SHA-512 crypt hash compatible with /etc/shadow."""
    from passlib.hash import sha512_crypt

    return sha512_crypt.using(rounds=rounds).hash(password)


def generate_userdata(vm_data: dict) -> str:
    """Generate cloud-init user-data YAML for a VM."""
    lines = ["#cloud-config"]

    hostname = vm_data.get("ciHostname") or vm_data.get("name", "localhost")
    lines.append(f"hostname: {hostname}")
    lines.append(f"fqdn: {hostname}")

    # SSH keys — injected for all users
    ssh_keys = vm_data.get("ciSshKeys", [])
    ssh_key = vm_data.get("ciSshKey", "").strip()
    all_keys = (
        [k.strip() for k in ssh_keys if k.strip()]
        if ssh_keys
        else ([ssh_key] if ssh_key else [])
    )
    if all_keys:
        lines.append("ssh_authorized_keys:")
        for key in all_keys:
            lines.append(f"  - {key}")

    # Passwords
    root_pw = vm_data.get("ciRootPassword", "")
    cloud_user_pw = vm_data.get("ciCloudUserPassword", "")
    root_hash = _sha512_crypt(root_pw) if root_pw else None
    cloud_user_hash = _sha512_crypt(cloud_user_pw) if cloud_user_pw else None

    if root_hash or cloud_user_hash:
        lines.append("ssh_pwauth: true")
        lines.append("chpasswd:")
        lines.append("  expire: false")
        lines.append("  users:")
        if cloud_user_hash:
            lines.append("    - name: cloud-user")
            lines.append(f"      password: {cloud_user_hash}")
            lines.append("      type: hash")
        if root_hash:
            lines.append("    - name: root")
            lines.append(f"      password: {root_hash}")
            lines.append("      type: hash")

    # Users — no 'default' entry (it locks passwords on RHEL 10)
    lines.append("disable_root: false")
    cloud_user_sudo = vm_data.get("ciCloudUserSudo", True)
    lines.append("users:")
    if root_hash:
        lines.append("  - name: root")
        lines.append("    lock_passwd: false")
    lines.append("  - name: cloud-user")
    lines.append("    lock_passwd: false")
    if cloud_user_hash:
        lines.append(f"    passwd: {cloud_user_hash}")
    if all_keys:
        lines.append("    ssh_authorized_keys:")
        for key in all_keys:
            lines.append(f"      - {key}")
    if cloud_user_sudo:
        lines.append("    sudo: ALL=(ALL) NOPASSWD:ALL")
        lines.append("    groups: wheel")

    # Packages
    import re

    _pkg_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+\-._]*$")
    ci_packages = [
        p for p in vm_data.get("ciPackages", []) if _pkg_re.fullmatch(str(p))
    ]
    all_packages = ["qemu-guest-agent"] + [
        p for p in ci_packages if p != "qemu-guest-agent"
    ]

    # Chrony NTP client config (points at gateway)
    gateway_ip = vm_data.get("gateway_ip")
    chrony_runcmd_lines = []
    if gateway_ip:
        if "chrony" not in all_packages:
            all_packages.append("chrony")
        chrony_runcmd_lines.append(
            f'  - printf "server {gateway_ip} iburst prefer\\nmakestep 1 -1\\ndriftfile /var/lib/chrony/drift\\n" > /etc/chrony.conf'
        )
        chrony_runcmd_lines.append("  - systemctl restart chronyd 2>/dev/null || true")

    if all_packages:
        lines.append("packages:")
        for pkg in all_packages:
            lines.append(f"  - {pkg}")

    # Custom user-data — split into top-level sections and runcmd items
    custom = vm_data.get("ciUserData", "").strip()
    custom_runcmd_lines = []
    if custom:
        in_runcmd = False
        for line in custom.split("\n"):
            stripped = line.strip()
            if stripped == "runcmd:":
                in_runcmd = True
                continue
            if in_runcmd:
                if line.startswith("  ") or line.startswith("\t"):
                    custom_runcmd_lines.append(line)
                elif stripped and not stripped.startswith("#"):
                    in_runcmd = False
                    lines.append(line)
            elif stripped and not stripped.startswith("#cloud-config"):
                lines.append(line)

    # runcmd — merged from base + custom
    lines.append("runcmd:")
    if root_hash or cloud_user_hash:
        lines.append(
            "  - sed -i 's/^PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config.d/50-cloud-init.conf 2>/dev/null; systemctl restart sshd 2>/dev/null || true"
        )
    lines.append(
        "  - for d in /dev/sr0 /dev/sr1; do blkid $d 2>/dev/null | grep -q cidata && eject $d 2>/dev/null; done || true"
    )
    lines.extend(chrony_runcmd_lines)
    lines.extend(custom_runcmd_lines)

    result = "\n".join(lines)

    # Validate the generated cloud-config is valid YAML with no duplicate keys
    import re

    import yaml

    try:
        parsed = yaml.safe_load(result)
        if not isinstance(parsed, dict):
            raise ValueError("Generated cloud-config is not a YAML mapping")
        elif "runcmd" in parsed and not isinstance(parsed["runcmd"], list):
            raise ValueError("Generated cloud-config runcmd is not a list")
    except (yaml.YAMLError, ValueError) as e:
        logger.error(
            "Generated cloud-config is invalid YAML: %s\n--- BEGIN ---\n%s\n--- END ---",
            e,
            result,
        )
        raise ValueError(f"Cloud-init user-data is invalid YAML: {e}")

    # Check for duplicate top-level keys (safe_load silently takes the last one)
    top_keys = re.findall(r"^([a-zA-Z_][a-zA-Z0-9_-]*):", result, re.MULTILINE)
    seen = set()
    for k in top_keys:
        if k in seen:
            logger.error("Generated cloud-config has duplicate top-level key: '%s'", k)
        seen.add(k)

    return result


def generate_metadata(vm_name: str, mac: str = "") -> str:
    """Generate cloud-init meta-data JSON for a VM."""
    import uuid

    return json.dumps(
        {
            "instance-id": f"{vm_name}-{uuid.uuid4().hex[:8]}",
            "local-hostname": vm_name,
        }
    )


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
        lines.append(
            f"genisoimage -output {seed_iso} -volid cidata -joliet -rock {seed_dir}/user-data {seed_dir}/meta-data 2>/dev/null || mkisofs -output {seed_iso} -volid cidata -joliet -rock {seed_dir}/user-data {seed_dir}/meta-data"
        )
        lines.append(f"rm -rf {seed_dir}")
        lines.append(f'echo "Seed ISO created for {vm_name}"')
        lines.append("")

    if len(lines) <= 2:
        return ""
    return "\n".join(lines)


def generate_metadata_service_script(
    project_id: str, topology: dict, vni_map: dict
) -> str:
    """Generate a Python HTTP metadata service that runs on the host bridge.

    The service listens on 169.254.169.254:80 and serves per-VM
    user-data and meta-data based on the requesting IP (mapped via DHCP lease).
    """
    nodes = topology.get("nodes", [])
    topology.get("edges", [])

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
