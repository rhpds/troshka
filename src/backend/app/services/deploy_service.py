"""
Deploy service — creates VMs and networks on hosts via SSH.

Translates canvas topology into libvirt VMs and VXLAN networks,
then executes setup scripts on the target host over SSH.
"""
import logging
import os
import subprocess
import tempfile

from app.services.vxlan import generate_setup_script

logger = logging.getLogger(__name__)


def run_ssh_script(host_ip: str, private_key: str, script: str, timeout: int = 600) -> dict:
    """Execute a bash script on a remote host via SSH."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as kf:
        kf.write(private_key)
        key_path = kf.name
    os.chmod(key_path, 0o600)

    try:
        result = subprocess.run(
            [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=30",
                "-i", key_path,
                f"ec2-user@{host_ip}",
                "sudo", "bash", "-s",
            ],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        return {
            "success": result.returncode == 0,
            "exit_code": result.returncode,
            "output": output,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "exit_code": -1, "output": f"SSH command timed out after {timeout}s"}
    except Exception as e:
        return {"success": False, "exit_code": -1, "output": str(e)}
    finally:
        os.unlink(key_path)


# ── Topology parsing ──

def _extract_vms(topology: dict) -> list[dict]:
    """Extract VM nodes with their properties."""
    vms = []
    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        data = node.get("data", {})
        vms.append({
            "node_id": node["id"],
            "name": data.get("name", "vm"),
            "vcpus": data.get("vcpus", 2),
            "ram_gb": data.get("ram", 4),
            "os": data.get("os", ""),
            "nics": data.get("nics", []),
            "disk_controllers": data.get("diskControllers", []),
            "boot_devices": data.get("bootDevices", ["hd"]),
            "cloud_init": data.get("cloudInit", False),
        })
    return vms


def _find_vm_networks(vm_node_id: str, topology: dict, vni_map: dict) -> list[dict]:
    """Find networks connected to a VM via NIC handles."""
    edges = topology.get("edges", [])
    nodes = topology.get("nodes", [])
    networks = []

    for edge in edges:
        handle = None
        network_node_id = None

        if edge.get("source") == vm_node_id:
            handle = edge.get("sourceHandle", "")
            network_node_id = edge.get("target")
        elif edge.get("target") == vm_node_id:
            handle = edge.get("targetHandle", "")
            network_node_id = edge.get("source")
        else:
            continue

        if not handle or not handle.startswith("nic-"):
            continue
        if network_node_id not in vni_map:
            continue

        # Find the NIC data to get MAC address
        # Handle format: "nic-{nicId}-top" or "nic-{nicId}-bottom"
        vm_node = next((n for n in nodes if n["id"] == vm_node_id), None)
        mac = ""
        if vm_node:
            for nic in vm_node.get("data", {}).get("nics", []):
                if nic["id"] in handle:
                    mac = nic.get("mac", "")
                    break

        vni = vni_map[network_node_id]
        networks.append({
            "bridge": f"br-{vni}",
            "mac": mac,
            "nic_id": handle,
        })

    return networks


def _find_vm_disks(vm_node_id: str, topology: dict) -> list[dict]:
    """Find storage nodes connected to a VM via disk controller handles."""
    edges = topology.get("edges", [])
    nodes = topology.get("nodes", [])
    disks = []

    for edge in edges:
        handle = None
        storage_node_id = None

        if edge.get("source") == vm_node_id:
            handle = edge.get("sourceHandle", "")
            storage_node_id = edge.get("target")
        elif edge.get("target") == vm_node_id:
            handle = edge.get("targetHandle", "")
            storage_node_id = edge.get("source")
        else:
            continue

        if not handle or not handle.startswith("dp-"):
            continue

        storage_node = next((n for n in nodes if n["id"] == storage_node_id and n.get("type") == "storageNode"), None)
        if not storage_node:
            continue

        sdata = storage_node.get("data", {})

        # Find bus type from the disk controller
        vm_node = next((n for n in nodes if n["id"] == vm_node_id), None)
        bus = "virtio"
        if vm_node:
            for dc in vm_node.get("data", {}).get("diskControllers", []):
                if dc["id"] == handle:
                    bus = dc.get("bus", "virtio")
                    break

        disks.append({
            "name": sdata.get("name", "disk"),
            "size_gb": sdata.get("size", 10),
            "format": sdata.get("format", "qcow2"),
            "bus": bus,
            "source": sdata.get("source", "blank"),
            "library_item_id": sdata.get("libraryItemId"),
        })

    return disks


# ── Script generators ──

def generate_vm_script(project_id: str, topology: dict, vni_map: dict) -> str:
    """Generate a bash script to create libvirt VMs from topology."""
    prefix = f"troshka-{project_id[:8]}"
    vms = _extract_vms(topology)
    lines = [
        "#!/bin/bash",
        "set -uo pipefail",
        f'echo "=== Creating {len(vms)} VM(s) for project {project_id[:8]} ==="',
        "",
    ]

    for vm in vms:
        vm_name = f"{prefix}-{vm['name']}"
        vm_networks = _find_vm_networks(vm["node_id"], topology, vni_map)
        vm_disks = _find_vm_disks(vm["node_id"], topology)

        lines.append(f'echo "Creating VM: {vm_name}"')

        # Create/download disk images
        if vm_disks:
            for disk in vm_disks:
                disk_path = f"/var/lib/troshka/vms/{vm_name}-{disk['name']}.{disk['format']}"
                if disk["format"] == "iso":
                    if disk.get("library_item_id"):
                        cache_path = f"/var/lib/troshka/images/{disk['library_item_id']}.iso"
                    continue
                if disk.get("source") == "library" and disk.get("library_item_id"):
                    cache_path = f"/var/lib/troshka/images/{disk['library_item_id']}.{disk['format']}"
                    lines.append(f"qemu-img create -f {disk['format']} -b {cache_path} -F {disk['format']} {disk_path} {disk['size_gb']}G")
                else:
                    lines.append(f"qemu-img create -f {disk['format']} {disk_path} {disk['size_gb']}G")

        # Boot devices can be type strings or node IDs (for storage nodes)
        boot_type_map = {"hd": "hd", "disk": "hd", "network": "network", "cdrom": "cdrom"}
        all_nodes = topology.get("nodes", [])
        storage_nodes = {n["id"]: n for n in all_nodes if n.get("type") == "storageNode"}

        raw_boot_devs = vm.get("boot_devices") or None
        has_iso = any(d["format"] == "iso" for d in vm_disks)
        has_disk = any(d["format"] != "iso" for d in vm_disks)
        # Auto-detect if no boot devices set, or if default ["hd"] but ISO is connected
        if raw_boot_devs is None or (raw_boot_devs == ["hd"] and has_iso):
            if has_iso and has_disk:
                boot_devs = ["cdrom", "hd"]
            elif has_iso:
                boot_devs = ["cdrom"]
            elif has_disk:
                boot_devs = ["hd"]
            else:
                boot_devs = ["network"]
        else:
            boot_devs = []
            seen = set()
            for d in raw_boot_devs:
                if d in boot_type_map:
                    dev = boot_type_map[d]
                elif d in storage_nodes:
                    dev = "cdrom" if storage_nodes[d].get("data", {}).get("format") == "iso" else "hd"
                else:
                    continue
                if dev not in seen:
                    boot_devs.append(dev)
                    seen.add(dev)
            if not boot_devs:
                boot_devs = ["hd"]
        boot_arg = ",".join(boot_devs)

        cmd_parts = [
            "virt-install",
            f"--name {vm_name}",
            f"--vcpus {vm['vcpus']}",
            f"--memory {vm['ram_gb'] * 1024}",
            "--os-variant detect=on,name=linux2022",
            "--graphics vnc,listen=127.0.0.1",
            f"--boot {boot_arg}",
            "--noautoconsole",
            "--noreboot",
        ]

        # Add disks and ISOs
        has_disk = False
        for disk in vm_disks:
            if disk["format"] == "iso":
                if disk.get("library_item_id"):
                    cache_path = f"/var/lib/troshka/images/{disk['library_item_id']}.iso"
                    cmd_parts.append(f"--disk path={cache_path},device=cdrom,readonly=on")
                continue
            if disk.get("source") == "library" and disk.get("library_item_id"):
                disk_path = f"/var/lib/troshka/vms/{vm_name}-{disk['name']}.{disk['format']}"
                cmd_parts.append(f"--disk path={disk_path},format={disk['format']},bus={disk['bus']}")
            else:
                disk_path = f"/var/lib/troshka/vms/{vm_name}-{disk['name']}.{disk['format']}"
                cmd_parts.append(f"--disk path={disk_path},format={disk['format']},bus={disk['bus']}")
            has_disk = True

        if not has_disk:
            cmd_parts.append("--disk none")

        # Add cloud-init seed ISO if cloud-init is enabled
        if vm.get("cloud_init"):
            seed_iso = f"/var/lib/troshka/vms/{vm_name}-seed.iso"
            cmd_parts.append(f"--disk path={seed_iso},device=cdrom,readonly=on")

        # Add networks
        if vm_networks:
            for net in vm_networks:
                mac_arg = f",mac={net['mac']}" if net["mac"] else ""
                cmd_parts.append(f"--network bridge={net['bridge']},model=virtio{mac_arg}")
        else:
            cmd_parts.append("--network none")

        lines.append(" \\\n  ".join(cmd_parts))
        lines.append(f'echo "VM {vm_name} defined"')
        lines.append("")

    lines.append('echo "=== All VMs created ==="')
    return "\n".join(lines)


def generate_start_script(project_id: str, topology: dict) -> str:
    """Generate a bash script to start VMs, respecting start order."""
    prefix = f"troshka-{project_id[:8]}"
    vms = _extract_vms(topology)
    start_order = topology.get("startOrder", [])

    lines = [
        "#!/bin/bash",
        "set -uo pipefail",
        f'echo "=== Starting VMs for project {project_id[:8]} ==="',
        "",
    ]

    # Build ordered groups
    ordered_vm_ids = set()
    if start_order:
        for entry in start_order:
            vm_id = entry.get("vmId", "")
            vm = next((v for v in vms if v["node_id"] == vm_id), None)
            if vm:
                vm_name = f"{prefix}-{vm['name']}"
                delay = entry.get("delaySeconds", 0)
                if delay > 0:
                    lines.append(f"sleep {delay}")
                lines.append(f"virsh start {vm_name} || true")
                lines.append(f'echo "Started {vm_name}"')
                ordered_vm_ids.add(vm_id)

    # Start remaining VMs not in start order
    for vm in vms:
        if vm["node_id"] not in ordered_vm_ids:
            vm_name = f"{prefix}-{vm['name']}"
            lines.append(f"virsh start {vm_name} || true")
            lines.append(f'echo "Started {vm_name}"')

    lines.append("")
    lines.append('echo "=== All VMs started ==="')
    return "\n".join(lines)


def generate_stop_script(project_id: str, topology: dict) -> str:
    """Generate a bash script to gracefully stop all VMs."""
    prefix = f"troshka-{project_id[:8]}"
    vms = _extract_vms(topology)

    lines = [
        "#!/bin/bash",
        "set -uo pipefail",
        f'echo "=== Stopping VMs for project {project_id[:8]} ==="',
        "",
    ]

    # Graceful shutdown
    for vm in vms:
        vm_name = f"{prefix}-{vm['name']}"
        lines.append(f"virsh shutdown {vm_name} 2>/dev/null || true")

    # Wait for graceful shutdown
    lines.append('echo "Waiting for VMs to shut down..."')
    lines.append("sleep 15")

    # Force destroy any still running
    for vm in vms:
        vm_name = f"{prefix}-{vm['name']}"
        lines.append(f"virsh destroy {vm_name} 2>/dev/null || true")

    lines.append("")
    lines.append('echo "=== All VMs stopped ==="')
    return "\n".join(lines)


def generate_destroy_script(project_id: str, topology: dict, vni_map: dict) -> str:
    """Generate a bash script to destroy VMs and tear down networks."""
    prefix = f"troshka-{project_id[:8]}"
    vms = _extract_vms(topology)

    lines = [
        "#!/bin/bash",
        "set -uo pipefail",
        f'echo "=== Destroying project {project_id[:8]} ==="',
        "",
    ]

    # Stop and undefine VMs
    for vm in vms:
        vm_name = f"{prefix}-{vm['name']}"
        lines.append(f"virsh destroy {vm_name} 2>/dev/null || true")
        lines.append(f"virsh undefine {vm_name} --remove-all-storage 2>/dev/null || true")

    # Remove any remaining disk images
    lines.append(f"rm -f /var/lib/troshka/vms/{prefix}-* 2>/dev/null || true")

    # Tear down networks
    for vni in vni_map.values():
        bridge = f"br-{vni}"
        vxlan_if = f"vxlan-{vni}"
        lines.append(f"ip link del {bridge} 2>/dev/null || true")
        lines.append(f"ip link del {vxlan_if} 2>/dev/null || true")
        lines.append(f"rm -f /etc/dnsmasq.d/troshka-{vni}.conf 2>/dev/null || true")

    lines.append("systemctl restart dnsmasq 2>/dev/null || true")
    lines.append("")
    lines.append('echo "=== Project destroyed ==="')
    return "\n".join(lines)


def generate_reconfigure_script(project_id: str, topology: dict, vni_map: dict) -> str:
    """Update VM definitions in-place without destroying disks."""
    prefix = f"troshka-{project_id[:8]}"
    vms = _extract_vms(topology)
    lines = [
        "#!/bin/bash",
        "set -uo pipefail",
        f'echo "=== Reconfiguring VMs for project {project_id[:8]} ==="',
        "",
    ]

    boot_type_map = {"hd": "hd", "disk": "hd", "network": "network", "cdrom": "cdrom"}
    all_nodes = topology.get("nodes", [])
    storage_node_ids = {n["id"] for n in all_nodes if n.get("type") == "storageNode"}

    for vm in vms:
        vm_name = f"{prefix}-{vm['name']}"
        vm_networks = _find_vm_networks(vm["node_id"], topology, vni_map)

        boot_devs = []
        seen = set()
        for d in vm.get("boot_devices", ["hd"]):
            if d in boot_type_map:
                dev = boot_type_map[d]
            elif d in storage_node_ids:
                dev = "hd"
            else:
                continue
            if dev not in seen:
                boot_devs.append(dev)
                seen.add(dev)
        if not boot_devs:
            boot_devs = ["hd"]

        boot_xml = "".join(f"<boot dev='{d}'/>" for d in boot_devs)

        boot_xml_lines = "".join(f"    <boot dev=\\'{d}\\'/>\n" for d in boot_devs)
        mem_kib = vm['ram_gb'] * 1024 * 1024

        lines.append(f'echo "Reconfiguring {vm_name}"')
        lines.append(f"virsh destroy {vm_name} 2>/dev/null || true")
        lines.append(f"""
TMPXML=$(mktemp)
virsh dumpxml --inactive {vm_name} > $TMPXML

python3 -c "
import re, sys
xml = open('$TMPXML').read()
# Remove existing boot lines
xml = re.sub(r'\\s*<boot dev=[^/]*/>', '', xml)
# Insert new boot lines after <type...>...</type>
boot = '''{boot_xml_lines}'''
xml = re.sub(r'(</type>)', r'\\1\\n' + boot, xml, count=1)
# Update vcpus
xml = re.sub(r'<vcpu[^>]*>[^<]*</vcpu>', '<vcpu placement=\"static\">{vm['vcpus']}</vcpu>', xml)
# Update memory
xml = re.sub(r'<memory[^>]*>[^<]*</memory>', '<memory unit=\"KiB\">{mem_kib}</memory>', xml)
xml = re.sub(r'<currentMemory[^>]*>[^<]*</currentMemory>', '<currentMemory unit=\"KiB\">{mem_kib}</currentMemory>', xml)
open('$TMPXML', 'w').write(xml)
"

virsh define $TMPXML
rm -f $TMPXML
virsh start {vm_name}
echo "{vm_name} reconfigured and started"
""")

    lines.append('echo "=== Reconfiguration complete ==="')
    return "\n".join(lines)


def diff_topologies(current: dict, deployed: dict) -> dict:
    """Diff current topology against what was deployed. Returns changes."""
    cur_nodes = {n["id"]: n for n in current.get("nodes", [])}
    dep_nodes = {n["id"]: n for n in deployed.get("nodes", [])}

    added_vms = []
    removed_vms = []
    changed_vms = []
    added_networks = []
    removed_networks = []

    for nid, node in cur_nodes.items():
        if nid not in dep_nodes:
            if node.get("type") == "vmNode":
                added_vms.append(node)
            elif node.get("type") == "networkNode":
                added_networks.append(node)

    for nid, node in dep_nodes.items():
        if nid not in cur_nodes:
            if node.get("type") == "vmNode":
                removed_vms.append(node)
            elif node.get("type") == "networkNode":
                removed_networks.append(node)

    for nid, node in cur_nodes.items():
        if nid in dep_nodes and node.get("type") == "vmNode":
            cur_data = node.get("data", {})
            dep_data = dep_nodes[nid].get("data", {})
            if (cur_data.get("vcpus") != dep_data.get("vcpus") or
                cur_data.get("ram") != dep_data.get("ram") or
                cur_data.get("bootDevices") != dep_data.get("bootDevices")):
                changed_vms.append(node)

    return {
        "added_vms": added_vms,
        "removed_vms": removed_vms,
        "changed_vms": changed_vms,
        "added_networks": added_networks,
        "removed_networks": removed_networks,
        "has_changes": bool(added_vms or removed_vms or changed_vms or added_networks or removed_networks),
    }


def generate_incremental_script(
    project_id: str,
    topology: dict,
    diff: dict,
    vni_map: dict,
) -> str:
    """Generate script for incremental changes — add/remove/update without touching untouched VMs."""
    prefix = f"troshka-{project_id[:8]}"
    lines = [
        "#!/bin/bash",
        "set -uo pipefail",
        f'echo "=== Applying incremental changes for {project_id[:8]} ==="',
        "",
    ]

    # Remove VMs that were deleted from canvas
    for node in diff["removed_vms"]:
        vm_name = f"{prefix}-{node['data']['name']}"
        lines.append(f'echo "Removing VM: {vm_name}"')
        lines.append(f"virsh destroy {vm_name} 2>/dev/null || true")
        lines.append(f"virsh undefine {vm_name} --remove-all-storage 2>/dev/null || true")

    # Remove networks that were deleted
    for node in diff["removed_networks"]:
        nid = node["id"]
        if nid in vni_map:
            vni = vni_map[nid]
            lines.append(f"ip link del br-{vni} 2>/dev/null || true")
            lines.append(f"ip link del vxlan-{vni} 2>/dev/null || true")
            lines.append(f"rm -f /etc/dnsmasq.d/troshka-{vni}.conf 2>/dev/null || true")

    # Reconfigure changed VMs (boot order, CPU, RAM)
    all_nodes = topology.get("nodes", [])
    storage_node_ids = {n["id"] for n in all_nodes if n.get("type") == "storageNode"}
    boot_type_map = {"hd": "hd", "disk": "hd", "network": "network", "cdrom": "cdrom"}

    for node in diff["changed_vms"]:
        d = node.get("data", {})
        vm_name = f"{prefix}-{d['name']}"
        boot_devs = []
        seen = set()
        for bd in d.get("bootDevices", ["hd"]):
            if bd in boot_type_map:
                dev = boot_type_map[bd]
            elif bd in storage_node_ids:
                dev = "hd"
            else:
                continue
            if dev not in seen:
                boot_devs.append(dev)
                seen.add(dev)
        if not boot_devs:
            boot_devs = ["hd"]
        boot_xml = "".join(f"<boot dev='{bd}'/>" for bd in boot_devs)

        boot_xml_lines2 = "".join(f"    <boot dev=\\'{bd}\\'/>\n" for bd in boot_devs)
        mem_kib2 = d.get('ram', 4) * 1024 * 1024
        vcpus2 = d.get('vcpus', 2)

        lines.append(f'echo "Reconfiguring {vm_name}"')
        lines.append(f"virsh destroy {vm_name} 2>/dev/null || true")
        lines.append(f"""
TMPXML=$(mktemp)
virsh dumpxml --inactive {vm_name} > $TMPXML

python3 -c "
import re
xml = open('$TMPXML').read()
xml = re.sub(r'\\s*<boot dev=[^/]*/>', '', xml)
boot = '''{boot_xml_lines2}'''
xml = re.sub(r'(</type>)', r'\\1\\n' + boot, xml, count=1)
xml = re.sub(r'<vcpu[^>]*>[^<]*</vcpu>', '<vcpu placement=\"static\">{vcpus2}</vcpu>', xml)
xml = re.sub(r'<memory[^>]*>[^<]*</memory>', '<memory unit=\"KiB\">{mem_kib2}</memory>', xml)
xml = re.sub(r'<currentMemory[^>]*>[^<]*</currentMemory>', '<currentMemory unit=\"KiB\">{mem_kib2}</currentMemory>', xml)
open('$TMPXML', 'w').write(xml)
"

virsh define $TMPXML
rm -f $TMPXML
virsh start {vm_name}
echo "{vm_name} reconfigured"
""")

    # Add new VMs
    for node in diff["added_vms"]:
        d = node.get("data", {})
        vm_name = f"{prefix}-{d.get('name', 'vm')}"
        vm_data = {
            "node_id": node["id"],
            "name": d.get("name", "vm"),
            "vcpus": d.get("vcpus", 2),
            "ram_gb": d.get("ram", 4),
            "boot_devices": d.get("bootDevices", ["hd"]),
        }

        vm_disks = _find_vm_disks(node["id"], topology)
        vm_networks = _find_vm_networks(node["id"], topology, vni_map)

        lines.append(f'echo "Adding new VM: {vm_name}"')

        # Create disks
        if vm_disks:
            for disk in vm_disks:
                if disk["format"] == "iso":
                    if disk.get("library_item_id"):
                        cache_path = f"/var/lib/troshka/images/{disk['library_item_id']}.iso"
                        lines.append(f"curl -sfL -C - -o {cache_path} \"$(cat /tmp/troshka-presigned-{disk['library_item_id']})\"")
                    continue
                disk_path = f"/var/lib/troshka/vms/{vm_name}-{disk['name']}.{disk['format']}"
                if disk.get("source") == "library" and disk.get("library_item_id"):
                    cache_path = f"/var/lib/troshka/images/{disk['library_item_id']}.{disk['format']}"
                    lines.append(f"curl -sfL -C - -o {cache_path} \"$(cat /tmp/troshka-presigned-{disk['library_item_id']})\"")
                    lines.append(f"qemu-img create -f {disk['format']} -b {cache_path} -F {disk['format']} {disk_path} {disk['size_gb']}G")
                else:
                    lines.append(f"qemu-img create -f {disk['format']} {disk_path} {disk['size_gb']}G")

        boot_devs = [boot_type_map[bd] for bd in vm_data.get("boot_devices", ["hd"]) if bd in boot_type_map]
        boot_devs += ["hd" for bd in vm_data.get("boot_devices", []) if bd in storage_node_ids and "hd" not in boot_devs]
        if not boot_devs:
            boot_devs = ["hd"]

        cmd_parts = [
            "virt-install",
            f"--name {vm_name}",
            f"--vcpus {vm_data['vcpus']}",
            f"--memory {vm_data['ram_gb'] * 1024}",
            "--os-variant detect=on,name=linux2022",
            "--graphics vnc,listen=127.0.0.1",
            f"--boot {','.join(boot_devs)}",
            "--noautoconsole",
            "--noreboot",
        ]

        has_disk = False
        for disk in vm_disks:
            if disk["format"] == "iso":
                continue
            dp = f"/var/lib/troshka/vms/{vm_name}-{disk['name']}.{disk['format']}"
            cmd_parts.append(f"--disk path={dp},format={disk['format']},bus={disk['bus']}")
            has_disk = True

        if not has_disk:
            cmd_parts.append("--disk none")

        # Cloud-init seed ISO
        if d.get("cloudInit"):
            seed_iso = f"/var/lib/troshka/vms/{vm_name}-seed.iso"
            cmd_parts.append(f"--disk path={seed_iso},device=cdrom,readonly=on")

        if vm_networks:
            for net in vm_networks:
                mac_arg = f",mac={net['mac']}" if net["mac"] else ""
                cmd_parts.append(f"--network bridge={net['bridge']},model=virtio{mac_arg}")
        else:
            cmd_parts.append("--network none")

        lines.append(" \\\n  ".join(cmd_parts))
        lines.append(f"virsh start {vm_name}")
        lines.append(f'echo "VM {vm_name} created and started"')
        lines.append("")

    lines.append('echo "=== Incremental changes applied ==="')
    return "\n".join(lines)


def generate_network_teardown_script(vni_map: dict) -> str:
    """Generate a script to tear down only the network infrastructure."""
    lines = ["#!/bin/bash", "set -uo pipefail", ""]

    for vni in vni_map.values():
        lines.append(f"ip link del br-{vni} 2>/dev/null || true")
        lines.append(f"ip link del vxlan-{vni} 2>/dev/null || true")
        lines.append(f"rm -f /etc/dnsmasq.d/troshka-{vni}.conf 2>/dev/null || true")

    lines.append("systemctl restart dnsmasq 2>/dev/null || true")
    return "\n".join(lines)


def cache_library_images(topology: dict, host_ip: str, private_key: str, db_session, progress_callback=None):
    """Download all library images to host cache with progress tracking.

    Starts downloads in background on the host, polls until complete.
    progress_callback(downloaded_bytes, total_bytes, item_name) called periodically.
    """
    from app.models.library import LibraryItem
    from app.services import s3_storage
    import time as _time

    nodes = topology.get("nodes", [])
    items_to_cache = []
    for node in nodes:
        if node.get("type") != "storageNode":
            continue
        item_id = node.get("data", {}).get("libraryItemId")
        if not item_id:
            continue
        item = db_session.query(LibraryItem).filter_by(id=item_id).first()
        if not item or not item.s3_key:
            continue
        fmt = node.get("data", {}).get("format", "qcow2")
        cache_path = f"/var/lib/troshka/images/{item_id}.{fmt}"
        items_to_cache.append({
            "item_id": item_id,
            "name": item.name,
            "s3_key": item.s3_key,
            "cache_path": cache_path,
            "expected_size": item.size_bytes,
        })

    if not items_to_cache:
        return

    # Generate presigned URLs
    for ic in items_to_cache:
        url = s3_storage.generate_presigned_url(ic["s3_key"], expires=7200)
        ic["url"] = url

    # Start all downloads in background on host using python3
    import base64 as _b64
    for ic in items_to_cache:
        status_file = ic["cache_path"] + ".status"
        log_file = f"/tmp/troshka-dl-{ic['item_id']}.log"
        script_file = f"/tmp/troshka-dl-{ic['item_id']}.py"
        py_lines = [
            "import os, sys, urllib.request",
            "cache = %r" % ic["cache_path"],
            "status = %r" % status_file,
            "expected = %d" % ic["expected_size"],
            "url = %r" % ic["url"],
            "try:",
            "    current = os.path.getsize(cache) if os.path.exists(cache) else 0",
            "    if current >= expected - 1024 and expected > 0:",
            "        open(status, 'w').write('DONE')",
            "        sys.exit(0)",
            "    print('downloading %d bytes remaining' % (expected - current), flush=True)",
            "    req = urllib.request.Request(url)",
            "    if current > 0:",
            "        req.add_header('Range', 'bytes=%d-' % current)",
            "    with urllib.request.urlopen(req, timeout=300) as resp:",
            "        mode = 'ab' if current > 0 and resp.status == 206 else 'wb'",
            "        with open(cache, mode) as f:",
            "            while True:",
            "                chunk = resp.read(1048576)",
            "                if not chunk:",
            "                    break",
            "                f.write(chunk)",
            "    open(status, 'w').write('DONE')",
            "except Exception as e:",
            "    print('FAILED: %s' % e, flush=True)",
            "    open(status, 'w').write('FAIL')",
        ]
        py_script = "\n".join(py_lines) + "\n"
        b64 = _b64.b64encode(py_script.encode()).decode()
        run_ssh_script(host_ip, private_key,
            f"echo '{b64}' | base64 -d > {script_file}\n"
            f"nohup python3 {script_file} > {log_file} 2>&1 &",
            timeout=15)

    # Poll until all downloads complete
    total_expected = sum(ic["expected_size"] for ic in items_to_cache)
    last_total = 0
    stale_polls = 0
    while True:
        _time.sleep(5)
        poll_cmds = []
        for ic in items_to_cache:
            poll_cmds.append(f"echo \"FILE:{ic['item_id']}:$(cat {ic['cache_path']}.status 2>/dev/null || echo PENDING):$(stat -c%s {ic['cache_path']} 2>/dev/null || echo 0)\"")
        result = run_ssh_script(host_ip, private_key, "\n".join(poll_cmds), timeout=15)

        all_done = True
        total_downloaded = 0
        for line in result["output"].strip().split("\n"):
            line = line.strip()
            if not line.startswith("FILE:"):
                continue
            parts = line.split(":")
            if len(parts) >= 4:
                status = parts[2]
                size = int(parts[3]) if parts[3].isdigit() else 0
                total_downloaded += size
                if status == "FAIL":
                    logger.error("Download failed for %s", parts[1])
                    return
                if status != "DONE":
                    all_done = False

        print(f"[CACHE] poll: {total_downloaded}/{total_expected} bytes, all_done={all_done}", flush=True)

        if progress_callback:
            progress_callback(total_downloaded, total_expected)

        if total_downloaded == last_total:
            stale_polls += 1
        else:
            stale_polls = 0
            last_total = total_downloaded

        if stale_polls >= 12:
            logger.error("Download stalled for 60s at %d/%d bytes", total_downloaded, total_expected)
            return

        if all_done:
            cleanup = [f"rm -f {ic['cache_path']}.status" for ic in items_to_cache]
            run_ssh_script(host_ip, private_key, "\n".join(cleanup), timeout=15)
            return


def _prepare_library_downloads(topology: dict, host_ip: str, private_key: str, db_session):
    """Generate presigned S3 URLs for library items and write them to the host."""
    from app.models.library import LibraryItem
    from app.services import s3_storage

    nodes = topology.get("nodes", [])
    library_item_ids = set()
    for node in nodes:
        if node.get("type") == "storageNode":
            item_id = node.get("data", {}).get("libraryItemId")
            if item_id:
                library_item_ids.add(item_id)

    if not library_item_ids:
        return

    lines = ["#!/bin/bash"]
    for item_id in library_item_ids:
        item = db_session.query(LibraryItem).filter_by(id=item_id).first()
        if not item or not item.s3_key:
            continue
        url = s3_storage.generate_presigned_url(item.s3_key, expires=7200)
        lines.append(f"echo '{url}' > /tmp/troshka-presigned-{item_id}")

    if len(lines) > 1:
        run_ssh_script(host_ip, private_key, "\n".join(lines), timeout=15)


# ── Async orchestrators ──

def deploy_project_async(project_id: str):
    """Background thread: deploy a project's topology to a host."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project

    s = SessionLocal()
    try:
        project = s.query(Project).filter_by(id=project_id).first()
        if not project or project.state != "deploying":
            return

        host = s.query(Host).filter_by(id=project.host_id).first()
        if not host or not host.private_key or not host.ip_address:
            project.state = "error"
            project.deploy_error = "Host not available or missing SSH key"
            s.commit()
            return

        topology = project.topology
        vni_map = project.vni_map or {}
        host_ip = host.ip_address
        private_key = host.private_key

        # Step 1: Set up VXLAN networks
        logger.info("Deploy %s: setting up networks on %s", project_id[:8], host_ip)
        from app.services.vxlan import build_host_network_config
        all_hosts = s.query(Host).filter(Host.state == "active").all()
        peer_ips = [h.ip_address for h in all_hosts if h.ip_address]
        network_config = build_host_network_config(topology, vni_map, peer_ips)
        net_script = generate_setup_script(network_config, host_ip)

        result = run_ssh_script(host_ip, private_key, net_script, timeout=120)
        if not result["success"]:
            logger.error("Deploy %s: network setup failed: %s", project_id[:8], result["output"][-500:])
            project.state = "error"
            project.deploy_error = f"Network setup failed (exit {result['exit_code']}):\n{result['output'][-2000:]}"
            s.commit()
            return

        # Step 2: Create cloud-init seed ISOs
        from app.services.cloud_init import generate_seed_iso_script
        seed_script = generate_seed_iso_script(project_id, topology)
        if seed_script:
            logger.info("Deploy %s: creating cloud-init seed ISOs", project_id[:8])
            run_ssh_script(host_ip, private_key, seed_script, timeout=30)

        # Step 3: Cache library images on host
        logger.info("Deploy %s: caching library images", project_id[:8])
        cache_library_images(topology, host_ip, private_key, s)

        # Step 3: Create VMs
        logger.info("Deploy %s: creating VMs", project_id[:8])
        vm_script = generate_vm_script(project_id, topology, vni_map)

        result = run_ssh_script(host_ip, private_key, vm_script, timeout=300)
        if not result["success"]:
            logger.error("Deploy %s: VM creation failed: %s", project_id[:8], result["output"][-500:])
            project.state = "error"
            project.deploy_error = f"VM creation failed (exit {result['exit_code']}):\n{result['output'][-2000:]}"
            s.commit()
            return

        # Step 3: Start VMs
        logger.info("Deploy %s: starting VMs", project_id[:8])
        start_script = generate_start_script(project_id, topology)

        result = run_ssh_script(host_ip, private_key, start_script, timeout=120)
        if not result["success"]:
            logger.error("Deploy %s: VM start failed: %s", project_id[:8], result["output"][-500:])
            project.state = "error"
            project.deploy_error = f"VM start failed (exit {result['exit_code']}):\n{result['output'][-2000:]}"
            s.commit()
            return

        project.state = "active"
        project.deploy_error = None
        project.deployed_topology = project.topology
        s.commit()
        logger.info("Deploy %s: complete — all VMs running", project_id[:8])

    except Exception:
        logger.exception("Deploy %s failed unexpectedly", project_id[:8])
        try:
            project = s.query(Project).filter_by(id=project_id).first()
            if project:
                project.state = "error"
                project.deploy_error = "Unexpected deploy error. Check server logs."
                s.commit()
        except Exception:
            pass
    finally:
        s.close()


def stop_project_async(project_id: str):
    """Background thread: stop a project's VMs and tear down networks."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project

    s = SessionLocal()
    try:
        project = s.query(Project).filter_by(id=project_id).first()
        if not project:
            return

        host = s.query(Host).filter_by(id=project.host_id).first()
        if not host or not host.private_key or not host.ip_address:
            project.state = "error"
            project.deploy_error = "Host not available"
            s.commit()
            return

        # Stop VMs
        stop_script = generate_stop_script(project_id, project.topology)
        result = run_ssh_script(host.ip_address, host.private_key, stop_script, timeout=120)
        if not result["success"]:
            logger.warning("Stop %s: VM shutdown had issues: %s", project_id[:8], result["output"][-300:])

        # Tear down networks
        vni_map = project.vni_map or {}
        if vni_map:
            teardown_script = generate_network_teardown_script(vni_map)
            run_ssh_script(host.ip_address, host.private_key, teardown_script, timeout=60)

        project.state = "stopped"
        project.deploy_error = None
        s.commit()
        logger.info("Stop %s: complete", project_id[:8])

    except Exception:
        logger.exception("Stop %s failed", project_id[:8])
        try:
            project = s.query(Project).filter_by(id=project_id).first()
            if project:
                project.state = "error"
                project.deploy_error = "Stop failed unexpectedly. Check server logs."
                s.commit()
        except Exception:
            pass
    finally:
        s.close()


def start_project_async(project_id: str):
    """Background thread: restart a stopped project."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project

    s = SessionLocal()
    try:
        from app.services.vxlan import build_host_network_config

        project = s.query(Project).filter_by(id=project_id).first()
        if not project:
            return

        host = s.query(Host).filter_by(id=project.host_id).first()
        if not host or not host.private_key or not host.ip_address:
            project.state = "error"
            project.deploy_error = "Host not available"
            s.commit()
            return

        topology = project.topology
        vni_map = project.vni_map or {}

        # Recreate networks (they were torn down on stop)
        if vni_map:
            all_hosts = s.query(Host).filter(Host.state == "active").all()
            peer_ips = [h.ip_address for h in all_hosts if h.ip_address]
            network_config = build_host_network_config(topology, vni_map, peer_ips)
            net_script = generate_setup_script(network_config, host.ip_address)
            result = run_ssh_script(host.ip_address, host.private_key, net_script, timeout=120)
            if not result["success"]:
                project.state = "error"
                project.deploy_error = f"Network setup failed on restart:\n{result['output'][-2000:]}"
                s.commit()
                return

        # Start VMs
        start_script = generate_start_script(project_id, topology)
        result = run_ssh_script(host.ip_address, host.private_key, start_script, timeout=120)
        if not result["success"]:
            project.state = "error"
            project.deploy_error = f"VM start failed:\n{result['output'][-2000:]}"
            s.commit()
            return

        project.state = "active"
        project.deploy_error = None
        s.commit()
        logger.info("Start %s: complete", project_id[:8])

    except Exception:
        logger.exception("Start %s failed", project_id[:8])
        try:
            project = s.query(Project).filter_by(id=project_id).first()
            if project:
                project.state = "error"
                project.deploy_error = "Start failed unexpectedly. Check server logs."
                s.commit()
        except Exception:
            pass
    finally:
        s.close()


def destroy_project_sync(project_id: str):
    """Synchronously destroy a project's VMs and networks. Called before DB delete."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project
    from app.services.placement import calculate_project_requirements

    s = SessionLocal()
    try:
        project = s.query(Project).filter_by(id=project_id).first()
        if not project or not project.host_id:
            return

        host = s.query(Host).filter_by(id=project.host_id).first()
        if not host or not host.private_key or not host.ip_address:
            return

        vni_map = project.vni_map or {}
        destroy_script = generate_destroy_script(project_id, project.topology, vni_map)
        run_ssh_script(host.ip_address, host.private_key, destroy_script, timeout=120)

        # Release host capacity
        reqs = calculate_project_requirements(project.topology)
        host.used_vcpus = max(0, host.used_vcpus - reqs["total_vcpus"])
        host.used_ram_mb = max(0, host.used_ram_mb - reqs["total_ram_mb"])
        s.commit()

        logger.info("Destroy %s: complete, released capacity", project_id[:8])
    except Exception:
        logger.exception("Destroy %s failed", project_id[:8])
    finally:
        s.close()
