"""sushy KubeVirt driver — translates Redfish to KubeVirt API calls."""

import json
import os

from kubernetes import client, config


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
            group="kubevirt.io",
            version="v1",
            namespace=self.namespace,
            plural="virtualmachines",
            name=name,
        )

    def _get_vmi(self, identity):
        name = self._kv_name(identity)
        try:
            return self.custom_api.get_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=self.namespace,
                plural="virtualmachineinstances",
                name=name,
            )
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return None
            raise

    def get_power_state(self, identity):
        vmi = self._get_vmi(identity)
        if not vmi:
            return "Off"
        phase = vmi.get("status", {}).get("phase", "")
        return "On" if phase == "Running" else "Off"

    def set_power_state(self, identity, state):
        name = self._kv_name(identity)
        running = state in ("On", "ForceOn")
        self.custom_api.patch_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=self.namespace,
            plural="virtualmachines",
            name=name,
            body={"spec": {"running": running}},
        )
        if state in ("ForceOff", "GracefulShutdown"):
            try:
                self.custom_api.delete_namespaced_custom_object(
                    group="kubevirt.io",
                    version="v1",
                    namespace=self.namespace,
                    plural="virtualmachineinstances",
                    name=name,
                )
            except client.exceptions.ApiException:
                pass
        if state == "ForceRestart":
            try:
                self.custom_api.delete_namespaced_custom_object(
                    group="kubevirt.io",
                    version="v1",
                    namespace=self.namespace,
                    plural="virtualmachineinstances",
                    name=name,
                )
            except client.exceptions.ApiException:
                pass
            self.custom_api.patch_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=self.namespace,
                plural="virtualmachines",
                name=name,
                body={"spec": {"running": True}},
            )

    def get_boot_device(self, identity):
        vm = self._get_vm(identity)
        devices = (
            vm.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("domain", {})
            .get("devices", {})
        )
        disks = devices.get("disks", [])
        interfaces = devices.get("interfaces", [])
        boot_items = []
        for d in disks:
            order = d.get("disk", {}).get("bootOrder") or d.get(
                "cdrom", {}
            ).get("bootOrder")
            if order:
                boot_items.append(
                    (order, "Hdd" if "disk" in d else "Cd")
                )
        for iface in interfaces:
            order = iface.get("bootOrder")
            if order:
                boot_items.append((order, "Pxe"))
        boot_items.sort()
        return boot_items[0][1] if boot_items else "Hdd"

    def set_boot_device(self, identity, device):
        pass

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
            group="kubevirt.io",
            version="v1",
            namespace=self.namespace,
            plural="virtualmachines",
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
