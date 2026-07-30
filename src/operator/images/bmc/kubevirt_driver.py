"""sushy KubeVirt driver — translates Redfish to KubeVirt API calls."""

import json
import os

from kubernetes import client, config

_KUBEVIRT_API_GROUP = "kubevirt.io"
_KUBEVIRT_API_VERSION = "v1"
_VM_PLURAL = "virtualmachines"
_VMI_PLURAL = "virtualmachineinstances"


class KubeVirtDriver:
    def __init__(self):
        config.load_incluster_config()
        self.custom_api = client.CustomObjectsApi()
        self.namespace = os.environ.get("SUSHY_NAMESPACE", "default")
        self.vm_map = json.loads(os.environ.get("SUSHY_VM_MAP", "{}"))

    def _kv_name(self, identity):
        identity = identity.strip("/")
        return self.vm_map.get(identity, identity)

    def _get_vm(self, identity):
        name = self._kv_name(identity)
        return self.custom_api.get_namespaced_custom_object(
            group=_KUBEVIRT_API_GROUP,
            version=_KUBEVIRT_API_VERSION,
            namespace=self.namespace,
            plural=_VM_PLURAL,
            name=name,
        )

    def _get_vmi(self, identity):
        name = self._kv_name(identity)
        try:
            return self.custom_api.get_namespaced_custom_object(
                group=_KUBEVIRT_API_GROUP,
                version=_KUBEVIRT_API_VERSION,
                namespace=self.namespace,
                plural=_VMI_PLURAL,
                name=name,
            )
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return None
            raise

    def _get_vm_devices(self, identity):
        """Extract the devices dict from a VM spec."""
        vm = self._get_vm(identity)
        return (
            vm.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("domain", {})
            .get("devices", {})
        )

    def _patch_vm_devices(self, identity, disks, interfaces):
        """Build and apply a patch to update VM disk/interface boot order."""
        name = self._kv_name(identity)
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "devices": {
                                "disks": disks,
                                "interfaces": interfaces,
                            }
                        }
                    }
                }
            }
        }
        self.custom_api.patch_namespaced_custom_object(
            group=_KUBEVIRT_API_GROUP,
            version=_KUBEVIRT_API_VERSION,
            namespace=self.namespace,
            plural=_VM_PLURAL,
            name=name,
            body=patch,
        )

    def get_power_state(self, identity):
        vmi = self._get_vmi(identity)
        if not vmi:
            return "Off"
        phase = vmi.get("status", {}).get("phase", "")
        return "On" if phase == "Running" else "Off"

    def _delete_vmi(self, identity):
        """Delete a VMI (best-effort, ignores 404)."""
        name = self._kv_name(identity)
        try:
            self.custom_api.delete_namespaced_custom_object(
                group=_KUBEVIRT_API_GROUP,
                version=_KUBEVIRT_API_VERSION,
                namespace=self.namespace,
                plural=_VMI_PLURAL,
                name=name,
            )
        except client.exceptions.ApiException:
            pass

    def _patch_vm_running(self, identity, running):
        """Patch the VM spec.running field."""
        name = self._kv_name(identity)
        self.custom_api.patch_namespaced_custom_object(
            group=_KUBEVIRT_API_GROUP,
            version=_KUBEVIRT_API_VERSION,
            namespace=self.namespace,
            plural=_VM_PLURAL,
            name=name,
            body={"spec": {"running": running}},
        )

    def set_power_state(self, identity, state):
        running = state in ("On", "ForceOn")
        self._patch_vm_running(identity, running)
        if state in ("ForceOff", "GracefulShutdown"):
            self._delete_vmi(identity)
        if state == "ForceRestart":
            self._delete_vmi(identity)
            self._patch_vm_running(identity, True)

    def get_boot_device(self, identity):
        devices = self._get_vm_devices(identity)
        disks = devices.get("disks", [])
        interfaces = devices.get("interfaces", [])
        boot_items = []
        for d in disks:
            order = d.get("bootOrder")
            if order:
                boot_items.append((order, "Hdd" if "disk" in d else "Cd"))
        for iface in interfaces:
            order = iface.get("bootOrder")
            if order:
                boot_items.append((order, "Pxe"))
        boot_items.sort()
        return boot_items[0][1] if boot_items else "Hdd"

    _boot_once_overrides = {}

    @staticmethod
    def _strip_boot_orders(disks, interfaces):
        """Return copies of disks and interfaces with bootOrder removed."""
        clean_disks = []
        for d in disks:
            d_copy = dict(d)
            d_copy.pop("bootOrder", None)
            clean_disks.append(d_copy)
        clean_ifaces = []
        for i in interfaces:
            i_copy = dict(i)
            i_copy.pop("bootOrder", None)
            clean_ifaces.append(i_copy)
        return clean_disks, clean_ifaces

    @staticmethod
    def _assign_boot_orders(patch_disks, patch_ifaces, target_type):
        """Assign boot order numbers based on the target boot device type."""
        order = 1
        if target_type == "interface":
            for i in patch_ifaces:
                i["bootOrder"] = order
                order += 1
            for d in patch_disks:
                if "disk" in d or "cdrom" in d:
                    d["bootOrder"] = order
                    order += 1
        elif target_type == "cdrom":
            for d in patch_disks:
                if "cdrom" in d:
                    d["bootOrder"] = order
                    order += 1
            for d in patch_disks:
                if "disk" in d:
                    d["bootOrder"] = order
                    order += 1
        else:
            for d in patch_disks:
                if "disk" in d:
                    d["bootOrder"] = order
                    order += 1

    _DEVICE_MAP = {"Pxe": "interface", "Hdd": "disk", "Cd": "cdrom"}

    def set_boot_device(self, identity, device, boot_enabled=None):
        name = self._kv_name(identity)
        devices = self._get_vm_devices(identity)
        disks = devices.get("disks", [])
        interfaces = devices.get("interfaces", [])

        if boot_enabled == "Once":
            self._boot_once_overrides[name] = {
                "disks": [
                    {d["name"]: d.get("bootOrder")} for d in disks if d.get("bootOrder")
                ],
                "interfaces": [
                    {i["name"]: i.get("bootOrder")}
                    for i in interfaces
                    if i.get("bootOrder")
                ],
            }
        else:
            self._boot_once_overrides.pop(name, None)

        target_type = self._DEVICE_MAP.get(device, "disk")
        patch_disks, patch_ifaces = self._strip_boot_orders(disks, interfaces)
        self._assign_boot_orders(patch_disks, patch_ifaces, target_type)
        self._patch_vm_devices(identity, patch_disks, patch_ifaces)

    def get_boot_override_enabled(self, identity):
        name = self._kv_name(identity)
        return "Once" if name in self._boot_once_overrides else "Continuous"

    @staticmethod
    def _restore_boot_orders(items, saved_entries):
        """Restore saved boot orders onto a list of disks or interfaces."""
        for entry in saved_entries:
            for item_name, order in entry.items():
                if not order:
                    continue
                for item in items:
                    if item["name"] == item_name:
                        item["bootOrder"] = order

    def revert_boot_once(self, identity):
        name = self._kv_name(identity)
        saved = self._boot_once_overrides.pop(name, None)
        if saved is None:
            return

        devices = self._get_vm_devices(identity)
        disks = devices.get("disks", [])
        interfaces = devices.get("interfaces", [])

        for d in disks:
            d.pop("bootOrder", None)
        for i in interfaces:
            i.pop("bootOrder", None)

        self._restore_boot_orders(disks, saved.get("disks", []))
        self._restore_boot_orders(interfaces, saved.get("interfaces", []))
        self._patch_vm_devices(identity, disks, interfaces)

    def get_boot_mode(self, identity):
        vm = self._get_vm(identity)
        fw = (
            vm.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("domain", {})
            .get("firmware", {})
        )
        if fw.get("bootloader", {}).get("efi"):
            return "UEFI"
        return "Legacy"

    def set_boot_mode(self, identity, mode):
        pass

    def get_total_memory(self, identity):
        vm = self._get_vm(identity)
        res = (
            vm.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("domain", {})
            .get("resources", {})
        )
        mem = res.get("requests", {}).get("memory", "0Mi")
        if mem.endswith("Mi"):
            return int(mem[:-2])
        if mem.endswith("Gi"):
            return int(mem[:-2]) * 1024
        return 0

    def get_total_cpus(self, identity):
        vm = self._get_vm(identity)
        cpu = (
            vm.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("domain", {})
            .get("cpu", {})
        )
        return cpu.get("cores", 1)

    def get_nics(self, identity):
        vm = self._get_vm(identity)
        interfaces = (
            vm.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("domain", {})
            .get("devices", {})
            .get("interfaces", [])
        )
        return [
            {"id": iface["name"], "mac": iface.get("macAddress", "")}
            for iface in interfaces
        ]

    def get_bios_version(self, identity):
        return "KubeVirt BIOS"

    def get_systems(self):
        vms = self.custom_api.list_namespaced_custom_object(
            group=_KUBEVIRT_API_GROUP,
            version=_KUBEVIRT_API_VERSION,
            namespace=self.namespace,
            plural=_VM_PLURAL,
            label_selector="app=troshka",
        )
        systems = []
        for vm in vms.get("items", []):
            uuid = (
                vm.get("spec", {})
                .get("template", {})
                .get("spec", {})
                .get("domain", {})
                .get("firmware", {})
                .get("uuid", vm["metadata"]["name"])
            )
            systems.append(uuid)
        return systems
