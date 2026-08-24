"""
Cloud-init metadata service.

Generates user-data and meta-data for VMs, and creates a lightweight
HTTP metadata service script to run on the host bridge.
"""

import json
import logging
import re

from passlib.hash import sha512_crypt as _sha512_crypt_impl  # type: ignore[attr-defined]

logger = logging.getLogger(__name__)


def _sha512_crypt(password: str, rounds: int = 5000) -> str:
    """SHA-512 crypt hash compatible with /etc/shadow."""
    return _sha512_crypt_impl.using(rounds=rounds).hash(password)


def _collect_ssh_keys(vm_data: dict) -> list[str]:
    """Collect and deduplicate SSH keys from vm_data."""
    ssh_keys = vm_data.get("ciSshKeys", [])
    ssh_key = vm_data.get("ciSshKey", "").strip()
    if ssh_keys:
        return [k.strip() for k in ssh_keys if k.strip()]
    if ssh_key:
        return [ssh_key]
    return []


def _build_ssh_keys_lines(all_keys: list[str]) -> list[str]:
    """Build top-level ssh_authorized_keys YAML lines."""
    if not all_keys:
        return []
    lines = ["ssh_authorized_keys:"]
    for key in all_keys:
        lines.append(f"  - {key}")
    return lines


def _login_user_name(vm_data: dict) -> str:
    """Primary login user for cloud-init (gold images may use aap, rhel, etc.)."""
    return vm_data.get("ciLoginUser") or "cloud-user"


def _build_password_lines(
    vm_data: dict,
) -> tuple[list[str], str | None, str | None]:
    """Build chpasswd section and return password hashes."""
    root_pw = vm_data.get("ciRootPassword", "")
    cloud_user_pw = vm_data.get("ciCloudUserPassword", "")
    login_user = _login_user_name(vm_data)
    root_hash = _sha512_crypt(root_pw) if root_pw else None
    cloud_user_hash = _sha512_crypt(cloud_user_pw) if cloud_user_pw else None

    if not root_hash and not cloud_user_hash:
        return [], root_hash, cloud_user_hash

    lines: list[str] = [
        "ssh_pwauth: true",
        "chpasswd:",
        "  expire: false",
        "  users:",
    ]
    if cloud_user_hash:
        lines.extend(
            [
                f"    - name: {login_user}",
                f"      password: {cloud_user_hash}",
                "      type: hash",
            ]
        )
    if root_hash:
        lines.extend(
            [
                "    - name: root",
                f"      password: {root_hash}",
                "      type: hash",
            ]
        )
    return lines, root_hash, cloud_user_hash


def _build_users_lines(
    vm_data: dict,
    all_keys: list[str],
    root_hash: str | None,
    cloud_user_hash: str | None,
) -> list[str]:
    """Build the users section."""
    login_user = _login_user_name(vm_data)
    lines: list[str] = ["disable_root: false", "users:"]
    if root_hash:
        lines.extend(["  - name: root", "    lock_passwd: false"])
    lines.extend([f"  - name: {login_user}", "    lock_passwd: false"])
    if cloud_user_hash:
        lines.append(f"    passwd: {cloud_user_hash}")
    if all_keys:
        lines.append("    ssh_authorized_keys:")
        for key in all_keys:
            lines.append(f"      - {key}")
    if vm_data.get("ciCloudUserSudo", True):
        lines.extend(["    sudo: ALL=(ALL) NOPASSWD:ALL", "    groups: wheel"])
    return lines


def _build_packages_and_chrony(vm_data: dict) -> tuple[list[str], list[str]]:
    """Build packages YAML lines and chrony runcmd lines."""
    _pkg_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+\-._]*$")
    ci_packages = [
        p for p in vm_data.get("ciPackages", []) if _pkg_re.fullmatch(str(p))
    ]
    all_packages = list(ci_packages)
    if not vm_data.get("ciMinimalCloudInit"):
        all_packages = ["qemu-guest-agent"] + [
            p for p in all_packages if p != "qemu-guest-agent"
        ]

    gateway_ip = vm_data.get("gateway_ip")
    chrony_runcmd_lines: list[str] = []
    if gateway_ip:
        if "chrony" not in all_packages:
            all_packages.append("chrony")
        chrony_runcmd_lines.append(
            f'  - printf "server {gateway_ip} iburst prefer\\nmakestep 1 -1\\ndriftfile /var/lib/chrony/drift\\n" > /etc/chrony.conf'
        )
        chrony_runcmd_lines.append("  - systemctl restart chronyd 2>/dev/null || true")

    pkg_lines: list[str] = []
    if all_packages:
        pkg_lines.append("packages:")
        for pkg in all_packages:
            pkg_lines.append(f"  - {pkg}")

    return pkg_lines, chrony_runcmd_lines


def _build_bootcmd_lines(vm_data: dict, all_keys: list[str]) -> list[str]:
    """Build bootcmd section for every-boot config."""
    lines = [
        "ssh_deletekeys: false",
        "bootcmd:",
        "  - mkdir -p /etc/systemd/system/sshd.service.d && printf '[Unit]\\nStartLimitBurst=20\\n' > /etc/systemd/system/sshd.service.d/restart-limit.conf && systemctl daemon-reload 2>/dev/null || true",
    ]
    exec_key = next((k for k in all_keys if "troshka-exec" in k), None)
    if exec_key:
        login_user = _login_user_name(vm_data)
        home = f"/home/{login_user}"
        lines.append(
            f"  - mkdir -p {home}/.ssh && sed -i '/troshka-exec/d' {home}/.ssh/authorized_keys 2>/dev/null; echo '{exec_key}' >> {home}/.ssh/authorized_keys && chmod 700 {home}/.ssh && chmod 600 {home}/.ssh/authorized_keys && chown -R {login_user}:{login_user} {home}/.ssh"
        )
    lines.append(
        "  - printf 'PasswordAuthentication yes\\nPerSourcePenaltyExemptList 10.0.0.0/8\\n' > /etc/ssh/sshd_config.d/50-cloud-init.conf"
    )
    return lines


def _parse_custom_userdata(vm_data: dict) -> tuple[list[str], list[str]]:
    """Parse custom user-data into top-level lines and runcmd items."""
    custom = vm_data.get("ciUserData", "").strip()
    top_lines: list[str] = []
    runcmd_lines: list[str] = []
    if not custom:
        return top_lines, runcmd_lines

    in_runcmd = False
    for line in custom.split("\n"):
        stripped = line.strip()
        if stripped == "runcmd:":
            in_runcmd = True
            continue
        if in_runcmd:
            if line.startswith(("  ", "\t")):
                runcmd_lines.append(line)
            elif stripped and not stripped.startswith("#"):
                in_runcmd = False
                top_lines.append(line)
        elif stripped and not stripped.startswith("#cloud-config"):
            top_lines.append(line)

    return top_lines, runcmd_lines


def _build_runcmd_lines(
    vm_data: dict,
    chrony_runcmd_lines: list[str],
    custom_runcmd_lines: list[str],
) -> list[str]:
    """Build the runcmd section."""
    lines = ["runcmd:"]
    if vm_data.get("ciMinimalCloudInit"):
        lines.append("  - systemctl enable --now sshd 2>/dev/null || true")
    if vm_data.get("guestExecEnabled", True) and not vm_data.get("ciMinimalCloudInit"):
        lines.append(
            "  - python3 -c \"import re,pathlib;f=pathlib.Path('/etc/sysconfig/qemu-ga');t=f.read_text() if f.exists() else '';t2=re.sub(r'(--allow-rpcs=[^\\\"]*)',r'\\\\1,guest-exec,guest-exec-status',t) if 'allow-rpcs' in t else re.sub(r'guest-exec-status,|guest-exec,|,guest-exec-status|,guest-exec','',t);f.write_text(t2)\" 2>/dev/null; systemctl restart qemu-guest-agent 2>/dev/null || true"
        )
    lines.append(
        "  - for d in /dev/sr0 /dev/sr1; do blkid $d 2>/dev/null | grep -q cidata && eject $d 2>/dev/null; done || true"
    )
    lines.extend(chrony_runcmd_lines)
    lines.extend(custom_runcmd_lines)
    return lines


def _validate_cloud_config(result: str) -> None:
    """Validate generated cloud-config is valid YAML with no duplicate keys."""
    import yaml  # type: ignore[import-untyped]

    try:
        parsed = yaml.safe_load(result)
        if not isinstance(parsed, dict):
            raise ValueError("Generated cloud-config is not a YAML mapping")
        if "runcmd" in parsed and not isinstance(parsed["runcmd"], list):
            raise ValueError("Generated cloud-config runcmd is not a list")
    except (yaml.YAMLError, ValueError) as e:
        logger.exception(
            "Generated cloud-config is invalid YAML: %s\n--- BEGIN ---\n%s\n--- END ---",
            e,
            result,
        )
        raise ValueError(f"Cloud-init user-data is invalid YAML: {e}")

    top_keys = re.findall(r"^([a-zA-Z_][a-zA-Z0-9_-]*):", result, re.MULTILINE)
    seen: set[str] = set()
    for k in top_keys:
        if k in seen:
            logger.error("Generated cloud-config has duplicate top-level key: '%s'", k)
        seen.add(k)


def generate_userdata(vm_data: dict) -> str:
    """Generate cloud-init user-data YAML for a VM."""
    custom = vm_data.get("ciUserData", "").strip()
    if vm_data.get("ciUserDataOnly") and custom:
        result = (
            custom if custom.startswith("#cloud-config") else f"#cloud-config\n{custom}"
        )
        _validate_cloud_config(result)
        return result

    lines = ["#cloud-config"]

    hostname = vm_data.get("ciHostname") or vm_data.get("name", "localhost")
    lines.append(f"hostname: {hostname}")
    lines.append(f"fqdn: {hostname}")

    all_keys = _collect_ssh_keys(vm_data)
    lines.extend(_build_ssh_keys_lines(all_keys))

    pwd_lines, root_hash, cloud_user_hash = _build_password_lines(vm_data)
    lines.extend(pwd_lines)
    lines.extend(_build_users_lines(vm_data, all_keys, root_hash, cloud_user_hash))

    pkg_lines, chrony_runcmd_lines = _build_packages_and_chrony(vm_data)
    lines.extend(pkg_lines)
    lines.extend(_build_bootcmd_lines(vm_data, all_keys))

    custom_top_lines, custom_runcmd_lines = _parse_custom_userdata(vm_data)
    lines.extend(custom_top_lines)
    lines.extend(_build_runcmd_lines(vm_data, chrony_runcmd_lines, custom_runcmd_lines))

    result = "\n".join(lines)
    _validate_cloud_config(result)
    return result


def generate_metadata(vm_name: str, _mac: str = "") -> str:
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
        from app.services.deploy_topology import _vm_domain_name

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

    from app.services.deploy_topology import metadata_bridges_for_topology

    bridges = metadata_bridges_for_topology(topology, vni_map)

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
