"""
Libvirt manager — direct API control of VMs on remote hosts.

Uses qemu+ssh:// transport with per-host SSH keys from the database.
"""
import logging
import os
import tempfile
import xml.etree.ElementTree as ET
import defusedxml.ElementTree as SafeET

import libvirt

logger = logging.getLogger(__name__)


def connect(host_ip: str, private_key: str) -> libvirt.virConnect:
    """Open a remote libvirt connection to a host."""
    kf = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
    kf.write(private_key)
    kf.close()
    os.chmod(kf.name, 0o600)

    uri = f"qemu+ssh://ec2-user@{host_ip}/system?keyfile={kf.name}&no_verify=1&known_hosts_verify=ignore"
    try:
        conn = libvirt.open(uri)
    finally:
        os.unlink(kf.name)
    return conn


def list_vms(conn: libvirt.virConnect) -> list[dict]:
    """List all VMs with their state."""
    result = []
    for dom in conn.listAllDomains():
        state, _ = dom.state()
        result.append({
            "name": dom.name(),
            "state": _state_name(state),
            "uuid": dom.UUIDString(),
        })
    return result


def get_vm_state(conn: libvirt.virConnect, name: str) -> str:
    """Get a VM's current state."""
    try:
        dom = conn.lookupByName(name)
        state, _ = dom.state()
        return _state_name(state)
    except libvirt.libvirtError:
        return "not_found"


def start_vm(conn: libvirt.virConnect, name: str) -> bool:
    try:
        dom = conn.lookupByName(name)
        if dom.isActive():
            return True
        dom.create()
        return True
    except libvirt.libvirtError as e:
        logger.error("Failed to start %s: %s", name, e)
        return False


def shutdown_vm(conn: libvirt.virConnect, name: str) -> bool:
    try:
        dom = conn.lookupByName(name)
        if not dom.isActive():
            return True
        dom.shutdown()
        return True
    except libvirt.libvirtError as e:
        logger.error("Failed to shutdown %s: %s", name, e)
        return False


def destroy_vm(conn: libvirt.virConnect, name: str) -> bool:
    try:
        dom = conn.lookupByName(name)
        if dom.isActive():
            dom.destroy()
        return True
    except libvirt.libvirtError as e:
        logger.error("Failed to destroy %s: %s", name, e)
        return False


def undefine_vm(conn: libvirt.virConnect, name: str, remove_storage: bool = True) -> bool:
    try:
        dom = conn.lookupByName(name)
        if dom.isActive():
            dom.destroy()
        flags = libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE
        if remove_storage:
            flags |= libvirt.VIR_DOMAIN_UNDEFINE_STORAGE
        dom.undefineFlags(flags)
        return True
    except libvirt.libvirtError as e:
        logger.error("Failed to undefine %s: %s", name, e)
        return False


def reboot_vm(conn: libvirt.virConnect, name: str) -> bool:
    try:
        dom = conn.lookupByName(name)
        dom.reboot()
        return True
    except libvirt.libvirtError as e:
        logger.error("Failed to reboot %s: %s", name, e)
        return False


def get_vnc_port(conn: libvirt.virConnect, name: str) -> int | None:
    """Get the VNC port for a VM."""
    try:
        dom = conn.lookupByName(name)
        xml_str = dom.XMLDesc()
        root = SafeET.fromstring(xml_str)
        graphics = root.find(".//graphics[@type='vnc']")
        if graphics is not None:
            port = graphics.get("port")
            if port and port != "-1":
                return int(port)
    except libvirt.libvirtError:
        pass
    return None


def reconfigure_vm(
    conn: libvirt.virConnect,
    name: str,
    boot_devs: list[str] | None = None,
    vcpus: int | None = None,
    ram_mb: int | None = None,
    nics: list[dict] | None = None,
    disks: list[dict] | None = None,
    vnc_listen: str = "127.0.0.1",
) -> bool:
    """Reconfigure a VM without wiping existing disks.

    Args:
        boot_devs: List of boot devices (e.g., ["network", "hd"])
        vcpus: Number of vCPUs
        ram_mb: RAM in MB
        nics: List of {"bridge": "br-1000", "mac": "52:54:00:xx:xx:xx", "model": "virtio"}
        disks: List of {"path": "/var/.../disk.qcow2", "format": "qcow2", "bus": "virtio"}
               Only adds missing disks and removes orphaned ones. Never touches existing.
        vnc_listen: VNC listen address
    """
    try:
        dom = conn.lookupByName(name)
        was_active = dom.isActive()
        if was_active:
            dom.destroy()

        xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
        root = SafeET.fromstring(xml_str)

        if boot_devs is not None:
            os_elem = root.find("os")
            for boot in os_elem.findall("boot"):
                os_elem.remove(boot)
            type_elem = os_elem.find("type")
            insert_idx = list(os_elem).index(type_elem) + 1
            for i, dev in enumerate(boot_devs):
                boot_elem = ET.Element("boot")
                boot_elem.set("dev", dev)
                os_elem.insert(insert_idx + i, boot_elem)

        if vcpus is not None:
            vcpu_elem = root.find("vcpu")
            vcpu_elem.text = str(vcpus)
            vcpu_elem.set("placement", "static")

        if ram_mb is not None:
            ram_kib = ram_mb * 1024
            mem = root.find("memory")
            mem.text = str(ram_kib)
            mem.set("unit", "KiB")
            cur_mem = root.find("currentMemory")
            if cur_mem is not None:
                cur_mem.text = str(ram_kib)
                cur_mem.set("unit", "KiB")

        if nics is not None:
            devices = root.find("devices")
            for iface in devices.findall("interface"):
                devices.remove(iface)
            for nic in nics:
                iface = ET.SubElement(devices, "interface")
                iface.set("type", "bridge")
                source = ET.SubElement(iface, "source")
                source.set("bridge", nic["bridge"])
                if nic.get("mac"):
                    mac_elem = ET.SubElement(iface, "mac")
                    mac_elem.set("address", nic["mac"])
                model = ET.SubElement(iface, "model")
                model.set("type", nic.get("model", "virtio"))

        if disks is not None:
            devices = root.find("devices")
            existing_disks = devices.findall("disk") if devices is not None else []
            existing_paths = set()
            for d in existing_disks:
                source = d.find("source")
                if source is not None and source.get("file"):
                    existing_paths.add(source.get("file"))

            desired_paths = {d["path"] for d in disks}

            # Remove disks no longer in the topology (but keep cdrom devices)
            for d in existing_disks:
                if d.get("device") == "cdrom":
                    continue
                source = d.find("source")
                path = source.get("file") if source is not None else None
                if path and path not in desired_paths:
                    devices.remove(d)
                    logger.info("Removed disk %s from %s", path, name)

            # Add new disks that don't exist yet
            target_letters = "bcdefghijklmnop"
            used_targets = {d.find("target").get("dev") for d in devices.findall("disk") if d.find("target") is not None}
            for disk_info in disks:
                if disk_info["path"] in existing_paths:
                    continue
                # Find next available target dev
                target_dev = None
                for letter in target_letters:
                    dev_name = f"vd{letter}"
                    if dev_name not in used_targets:
                        target_dev = dev_name
                        used_targets.add(dev_name)
                        break
                if not target_dev:
                    continue

                disk_elem = ET.SubElement(devices, "disk")
                disk_elem.set("type", "file")
                disk_elem.set("device", "disk")
                driver = ET.SubElement(disk_elem, "driver")
                driver.set("name", "qemu")
                driver.set("type", disk_info.get("format", "qcow2"))
                source = ET.SubElement(disk_elem, "source")
                source.set("file", disk_info["path"])
                target = ET.SubElement(disk_elem, "target")
                target.set("dev", target_dev)
                target.set("bus", disk_info.get("bus", "virtio"))
                logger.info("Added disk %s as %s to %s", disk_info["path"], target_dev, name)

        if vnc_listen:
            devices = root.find("devices")
            graphics = devices.find("graphics[@type='vnc']") if devices is not None else None
            if graphics is not None:
                graphics.set("listen", vnc_listen)
                listen_elem = graphics.find("listen")
                if listen_elem is not None:
                    listen_elem.set("address", vnc_listen)
            elif devices is not None:
                graphics = ET.SubElement(devices, "graphics")
                graphics.set("type", "vnc")
                graphics.set("port", "-1")
                graphics.set("autoport", "yes")
                graphics.set("listen", vnc_listen)
                listen_sub = ET.SubElement(graphics, "listen")
                listen_sub.set("type", "address")
                listen_sub.set("address", vnc_listen)

        new_xml = ET.tostring(root, encoding="unicode")
        conn.defineXML(new_xml)

        if was_active:
            dom2 = conn.lookupByName(name)
            dom2.create()

        logger.info("Reconfigured %s", name)
        return True
    except libvirt.libvirtError as e:
        logger.error("Failed to reconfigure %s: %s", name, e)
        return False


def resolve_boot_devs(boot_devices: list, topology: dict) -> list[str]:
    """Resolve canvas boot device entries to libvirt boot device names."""
    boot_type_map = {"hd": "hd", "disk": "hd", "network": "network", "cdrom": "cdrom"}
    storage_node_ids = {n["id"] for n in topology.get("nodes", []) if n.get("type") == "storageNode"}
    boot_devs = []
    seen = set()
    for d in boot_devices:
        if d in boot_type_map:
            dev = boot_type_map[d]
        elif d in storage_node_ids:
            dev = "hd"
        else:
            continue
        if dev not in seen:
            boot_devs.append(dev)
            seen.add(dev)
    return boot_devs or ["hd"]


def _state_name(state: int) -> str:
    return {
        libvirt.VIR_DOMAIN_NOSTATE: "unknown",
        libvirt.VIR_DOMAIN_RUNNING: "running",
        libvirt.VIR_DOMAIN_BLOCKED: "blocked",
        libvirt.VIR_DOMAIN_PAUSED: "paused",
        libvirt.VIR_DOMAIN_SHUTDOWN: "shutting_down",
        libvirt.VIR_DOMAIN_SHUTOFF: "shut_off",
        libvirt.VIR_DOMAIN_CRASHED: "crashed",
        libvirt.VIR_DOMAIN_PMSUSPENDED: "suspended",
    }.get(state, "unknown")
