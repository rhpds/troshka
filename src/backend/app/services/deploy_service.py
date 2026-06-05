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
        vm_node = next((n for n in nodes if n["id"] == vm_node_id), None)
        mac = ""
        if vm_node:
            for nic in vm_node.get("data", {}).get("nics", []):
                if nic["id"] == handle:
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

        # Create disk images
        if vm_disks:
            for disk in vm_disks:
                disk_path = f"/var/lib/troshka/vms/{vm_name}-{disk['name']}.{disk['format']}"
                if disk["format"] == "iso":
                    continue
                lines.append(f"qemu-img create -f {disk['format']} {disk_path} {disk['size_gb']}G")
        else:
            disk_path = f"/var/lib/troshka/vms/{vm_name}-disk0.qcow2"
            lines.append(f"qemu-img create -f qcow2 {disk_path} 10G")
            vm_disks = [{"name": "disk0", "size_gb": 10, "format": "qcow2", "bus": "virtio"}]

        # Boot devices can be type strings or node IDs (for storage nodes)
        boot_type_map = {"hd": "hd", "disk": "hd", "network": "network", "cdrom": "cdrom"}
        all_nodes = topology.get("nodes", [])
        storage_node_ids = {n["id"] for n in all_nodes if n.get("type") == "storageNode"}
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
        boot_arg = ",".join(boot_devs)

        cmd_parts = [
            "virt-install",
            f"--name {vm_name}",
            f"--vcpus {vm['vcpus']}",
            f"--memory {vm['ram_gb'] * 1024}",
            "--os-variant detect=on,name=linux2022",
            "--graphics vnc,listen=0.0.0.0",
            f"--boot {boot_arg}",
            "--noautoconsole",
            "--noreboot",
        ]

        # Add disks
        for disk in vm_disks:
            if disk["format"] == "iso":
                continue
            disk_path = f"/var/lib/troshka/vms/{vm_name}-{disk['name']}.{disk['format']}"
            cmd_parts.append(f"--disk path={disk_path},format={disk['format']},bus={disk['bus']}")

        if not any(d["format"] != "iso" for d in vm_disks):
            disk_path = f"/var/lib/troshka/vms/{vm_name}-disk0.qcow2"
            cmd_parts.append(f"--disk path={disk_path},format=qcow2,bus=virtio")

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


def generate_network_teardown_script(vni_map: dict) -> str:
    """Generate a script to tear down only the network infrastructure."""
    lines = ["#!/bin/bash", "set -uo pipefail", ""]

    for vni in vni_map.values():
        lines.append(f"ip link del br-{vni} 2>/dev/null || true")
        lines.append(f"ip link del vxlan-{vni} 2>/dev/null || true")
        lines.append(f"rm -f /etc/dnsmasq.d/troshka-{vni}.conf 2>/dev/null || true")

    lines.append("systemctl restart dnsmasq 2>/dev/null || true")
    return "\n".join(lines)


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

        # Step 2: Create VMs
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
