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
) -> bool:
    """Reconfigure a VM's boot order, CPU, and RAM without touching disks."""
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
