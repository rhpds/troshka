"""sushy KubeVirt driver — translates Redfish to KubeVirt API calls."""

import json
import os
import time

from kubernetes import client, config

_KUBEVIRT_API_GROUP = "kubevirt.io"
_KUBEVIRT_API_VERSION = "v1"
_VM_PLURAL = "virtualmachines"
_VMI_PLURAL = "virtualmachineinstances"

_CDI_API_GROUP = "cdi.kubevirt.io"
_CDI_API_VERSION = "v1beta1"
_DV_PLURAL = "datavolumes"

_VMEDIA_VOL_NAME = "vmedia-cd"
_VMEDIA_DV_SUFFIX = "-vmedia-cd"
_VMEDIA_DV_SIZE = "5Gi"
_VMEDIA_POLL_INTERVAL = 5
_VMEDIA_POLL_TIMEOUT = 300
_NVRAM_POLL_INTERVAL = 2
_NVRAM_POLL_TIMEOUT = 60
_BOOT_NEXT_INDEX = 0x00FF


class KubeVirtDriver:
    def __init__(self):
        config.load_incluster_config()
        self.custom_api = client.CustomObjectsApi()
        self.core_api = client.CoreV1Api()
        self.namespace = os.environ.get("SUSHY_NAMESPACE", "default")
        self.vm_map = json.loads(os.environ.get("SUSHY_VM_MAP", "{}"))
        self.storage_class = os.environ.get("SUSHY_STORAGE_CLASS", "")
        self._vmedia_state = {}

    def _kv_name(self, identity):
        identity = identity.strip("/")
        return self.vm_map.get(identity, identity)

    def _get_vm(self, identity) -> dict:
        name = self._kv_name(identity)
        return self.custom_api.get_namespaced_custom_object(  # type: ignore[return-value]
            group=_KUBEVIRT_API_GROUP,
            version=_KUBEVIRT_API_VERSION,
            namespace=self.namespace,
            plural=_VM_PLURAL,
            name=name,
        )

    def _get_vmi(self, identity) -> dict | None:
        name = self._kv_name(identity)
        try:
            return self.custom_api.get_namespaced_custom_object(  # type: ignore[return-value]
                group=_KUBEVIRT_API_GROUP,
                version=_KUBEVIRT_API_VERSION,
                namespace=self.namespace,
                plural=_VMI_PLURAL,
                name=name,
            )
        except client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def _get_vm_devices(self, identity):
        """Extract the devices dict from a VM spec."""
        vm = self._get_vm(identity)
        return (
            vm.get("spec", {})  # type: ignore[union-attr]  # type: ignore[union-attr]
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
        phase = vmi.get("status", {}).get("phase", "")  # type: ignore[union-attr]
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
        except client.ApiException:
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
        if state == "ForceRestart":
            self._delete_vmi(identity)
            self._patch_vm_running(identity, True)
            return
        running = state in ("On", "ForceOn")
        self._patch_vm_running(identity, running)
        if state in ("ForceOff", "GracefulShutdown"):
            self._delete_vmi(identity)

    def get_boot_device(self, identity):
        name = self._kv_name(identity)
        override = self._boot_device_override.get(name)
        if override:
            return override
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
    _boot_device_override = {}

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
    def _set_boot_order_on(items, predicate, start_order):
        """Assign incrementing bootOrder to items matching predicate.

        Returns the next available order number.
        """
        order = start_order
        for item in items:
            if predicate(item):
                item["bootOrder"] = order
                order += 1
        return order

    @staticmethod
    def _assign_boot_orders(patch_disks, patch_ifaces, target_type):
        """Assign boot order numbers based on the target boot device type."""
        assign = KubeVirtDriver._set_boot_order_on
        if target_type == "interface":
            order = assign(patch_ifaces, lambda _: True, 1)
            assign(patch_disks, lambda d: "disk" in d or "cdrom" in d, order)
        elif target_type == "cdrom":
            order = assign(patch_disks, lambda d: "disk" in d, 1)
            assign(patch_disks, lambda d: "cdrom" in d, order)
        else:
            assign(patch_disks, lambda d: "disk" in d, 1)

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

        self._boot_device_override[name] = device

        target_type = self._DEVICE_MAP.get(device, "disk")
        patch_disks, patch_ifaces = self._strip_boot_orders(disks, interfaces)
        self._assign_boot_orders(patch_disks, patch_ifaces, target_type)
        self._patch_vm_devices(identity, patch_disks, patch_ifaces)

        # KubeVirt can't change boot order on a running VMI — delete it so the
        # controller recreates it from the updated template.
        vmi = self._get_vmi(identity)
        if vmi:
            self._delete_vmi(identity)

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
        self._boot_device_override.pop(name, None)
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
            vm.get("spec", {})  # type: ignore[union-attr]
            .get("template", {})
            .get("spec", {})
            .get("domain", {})
            .get("firmware", {})
        )
        if fw.get("bootloader", {}).get("efi"):
            return "UEFI"
        return "Legacy"

    def set_boot_mode(self, identity, mode):
        # No-op: boot mode is set at VM creation time via KubeVirt spec, not changeable at runtime
        pass

    def get_total_memory(self, identity):
        vm = self._get_vm(identity)
        res = (
            vm.get("spec", {})  # type: ignore[union-attr]
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
            vm.get("spec", {})  # type: ignore[union-attr]
            .get("template", {})
            .get("spec", {})
            .get("domain", {})
            .get("cpu", {})
        )
        return cpu.get("cores", 1)

    def get_nics(self, identity):
        vm = self._get_vm(identity)
        interfaces = (
            vm.get("spec", {})  # type: ignore[union-attr]
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

    def get_uuid(self, identity):
        vm = self._get_vm(identity)
        return (
            vm.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("domain", {})
            .get("firmware", {})
            .get("uuid", identity)
        )

    def get_bios_version(self, _identity):
        return "KubeVirt BIOS"

    def get_systems(self):
        vms: dict = self.custom_api.list_namespaced_custom_object(  # type: ignore[assignment]
            group=_KUBEVIRT_API_GROUP,
            version=_KUBEVIRT_API_VERSION,
            namespace=self.namespace,
            plural=_VM_PLURAL,
            label_selector="app=troshka",
        )
        if self.vm_map:
            return list(self.vm_map.keys())
        systems = []
        for vm in vms.get("items", []):  # type: ignore[union-attr]
            systems.append(vm["metadata"]["uid"])
        return systems

    # ── Virtual Media ──

    def get_vmedia_state(self, identity):
        return self._vmedia_state.get(identity, {})

    def _cleanup_stale_vmedia(self, identity, dv_name):
        """Remove leftover DV and CDROM from a previous attempt."""
        try:
            self.custom_api.delete_namespaced_custom_object(
                group=_CDI_API_GROUP,
                version=_CDI_API_VERSION,
                namespace=self.namespace,
                plural=_DV_PLURAL,
                name=dv_name,
            )
            time.sleep(2)
        except Exception:
            pass

        try:
            name = self._kv_name(identity)
            vm = self._get_vm(identity)
            volumes = vm.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])  # type: ignore[union-attr]
            disks = vm.get("spec", {}).get("template", {}).get("spec", {}).get("domain", {}).get("devices", {}).get("disks", [])  # type: ignore[union-attr]
            new_volumes = [v for v in volumes if v.get("name") != _VMEDIA_VOL_NAME]
            new_disks = [d for d in disks if d.get("name") != _VMEDIA_VOL_NAME]
            if len(new_volumes) != len(volumes) or len(new_disks) != len(disks):
                self.custom_api.patch_namespaced_custom_object(
                    group=_KUBEVIRT_API_GROUP,
                    version=_KUBEVIRT_API_VERSION,
                    namespace=self.namespace,
                    plural=_VM_PLURAL,
                    name=name,
                    body={
                        "spec": {
                            "template": {
                                "spec": {
                                    "volumes": new_volumes,
                                    "domain": {"devices": {"disks": new_disks}},
                                }
                            }
                        }
                    },
                )
        except Exception:
            pass

    def _wait_for_dv(self, dv_name):
        """Poll a DataVolume until Succeeded, Failed, or timeout."""
        import threading

        event = threading.Event()
        result = {"phase": "Pending", "error": ""}

        def _poll():
            deadline = time.monotonic() + _VMEDIA_POLL_TIMEOUT
            while time.monotonic() < deadline:
                try:
                    dv = self.custom_api.get_namespaced_custom_object(
                        group=_CDI_API_GROUP,
                        version=_CDI_API_VERSION,
                        namespace=self.namespace,
                        plural=_DV_PLURAL,
                        name=dv_name,
                    )
                    phase = dv.get("status", {}).get("phase", "")  # type: ignore[union-attr]
                    result["phase"] = phase
                    if phase == "Succeeded":
                        event.set()
                        return
                    if phase in ("Failed", "Error"):
                        result["error"] = f"DataVolume {dv_name} failed: {phase}"
                        event.set()
                        return
                except Exception:
                    pass
                time.sleep(_VMEDIA_POLL_INTERVAL)
            result["error"] = f"DataVolume {dv_name} timed out"
            event.set()

        t = threading.Thread(target=_poll, daemon=True)
        t.start()
        return event, result

    def _find_nvram_pvc(self, identity):
        """Return the PVC name backing this VM's persistent NVRAM, or None."""
        name = self._kv_name(identity)
        try:
            pvcs = self.core_api.list_namespaced_persistent_volume_claim(self.namespace)
            for pvc in pvcs.items:  # type: ignore[union-attr]
                pvc_name = pvc.metadata.name
                if "persistent-state" in pvc_name and name in pvc_name:
                    print(f"[BMC] Found NVRAM PVC: {pvc_name} for {name}")
                    return pvc_name
        except Exception as e:
            print(f"[BMC] Error listing PVCs for NVRAM: {e}")
        print(f"[BMC] No NVRAM PVC found for {name}")
        return None

    def _set_boot_next_cdrom(self, identity):
        """Set UEFI BootNext to CDROM in the VM's persistent NVRAM.

        Uses a temporary Pod that mounts the NVRAM PVC and runs
        virt-firmware to write the BootNext variable.  This gives
        real UEFI 'boot once' semantics: OVMF reads BootNext on
        the next start, boots from CDROM, clears BootNext, and
        subsequent reboots use the normal BootOrder (disk first).
        """
        nvram_pvc = self._find_nvram_pvc(identity)
        if not nvram_pvc:
            print(f"[BMC] Skipping BootNext — no persistent NVRAM for {identity}")
            return

        name = self._kv_name(identity)

        # Wait for VMI to fully terminate so the NVRAM PVC is released.
        # eject_image() already deleted the VMI but the virt-launcher pod
        # takes time to stop and release the RWO PVC.
        deadline = time.monotonic() + _NVRAM_POLL_TIMEOUT
        while time.monotonic() < deadline:
            if not self._get_vmi(identity):
                # Also wait a few seconds for PVC detach to propagate
                time.sleep(5)
                break
            print(f"[BMC] Waiting for VMI {name} to terminate before BootNext...")
            time.sleep(_NVRAM_POLL_INTERVAL)

        pod_name = f"bootnext-{name}"[:63]
        print(f"[BMC] Creating BootNext pod {pod_name} with NVRAM PVC {nvram_pvc}")
        bmc_image = os.environ.get(
            "SUSHY_BMC_IMAGE",
            "quay.io/redhat-gpte/troshka-bmc:production",
        )

        script = (
            "import sys, struct, glob\n"
            "from virt.firmware.varstore.edk2 import Edk2VarStore\n"
            "paths = glob.glob('/mnt/nvram/*VARS*') or glob.glob('/mnt/nvram/disk.img')\n"
            "if not paths:\n"
            "    print('no NVRAM file found'); sys.exit(0)\n"
            "nvram = paths[0]\n"
            "store = Edk2VarStore()\n"
            "store.readfile(nvram)\n"
            "varlist = store.get_varlist()\n"
            "# Find a CDROM boot entry, or pick the highest Boot#### index\n"
            "cd_idx = None\n"
            "for key in varlist:\n"
            "    if key.startswith('Boot') and key[4:].isalnum() and key != 'BootOrder' and key != 'BootNext' and key != 'BootCurrent' and key != 'BootOptionSupport':\n"
            "        var = varlist[key]\n"
            "        if var.data and b'CDROM' in var.data or b'CD-ROM' in var.data or b'DVD' in var.data or b'SATA' in var.data:\n"
            "            cd_idx = int(key[4:], 16)\n"
            "            break\n"
            "if cd_idx is None:\n"
            "    # Create a BootNext entry pointing to the SATA CDROM\n"
            f"    cd_idx = {_BOOT_NEXT_INDEX}\n"
            "    varlist.set_boot_entry(cd_idx, 'Virtual CD', 'PciRoot(0x0)/Pci(0x1f,0x2)/Sata(0,0,0)')\n"
            "    varlist.append_boot_order(cd_idx)\n"
            "varlist.set_boot_next(cd_idx)\n"
            "store.write_varstore(nvram)\n"
            "print(f'BootNext set to 0x{cd_idx:04X}')\n"
        )

        pod_body = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": self.namespace},
            "spec": {
                "restartPolicy": "Never",
                "serviceAccountName": "troshka-bmc",
                "containers": [
                    {
                        "name": "bootnext",
                        "image": bmc_image,
                        "command": ["python3", "-c", script],
                        "volumeMounts": [{"name": "nvram", "mountPath": "/mnt/nvram"}],
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                        },
                    }
                ],
                "volumes": [
                    {
                        "name": "nvram",
                        "persistentVolumeClaim": {"claimName": nvram_pvc},
                    }
                ],
            },
        }

        try:
            self.core_api.create_namespaced_pod(self.namespace, pod_body)
            print(f"[BMC] BootNext pod {pod_name} created")
        except Exception as e:
            print(f"[BMC] Failed to create BootNext pod {pod_name}: {e}")
            return

        deadline = time.monotonic() + _NVRAM_POLL_TIMEOUT
        while time.monotonic() < deadline:
            try:
                pod = self.core_api.read_namespaced_pod(pod_name, self.namespace)
                phase = pod.status.phase  # type: ignore[union-attr]
                if phase == "Succeeded":
                    print(f"[BMC] BootNext pod {pod_name} succeeded")
                    break
                if phase == "Failed":
                    print(f"[BMC] BootNext pod {pod_name} failed")
                    break
            except Exception as e:
                print(f"[BMC] Error polling BootNext pod {pod_name}: {e}")
                break
            time.sleep(_NVRAM_POLL_INTERVAL)

        try:
            self.core_api.delete_namespaced_pod(pod_name, self.namespace)
        except Exception:
            pass

    def insert_image(self, identity, image_url, proxy_base_url):
        """Create CDI DataVolume, attach CDROM to VM, return immediately."""
        name = self._kv_name(identity)
        dv_name = f"{name}{_VMEDIA_DV_SUFFIX}"

        self.eject_image(identity)
        self._cleanup_stale_vmedia(identity, dv_name)

        print(f"[BMC] InsertVirtualMedia for {name}: {image_url}")
        self._set_boot_next_cdrom(identity)

        proxy_url = f"{proxy_base_url}/vmedia/download/{identity}"

        dv_spec = {
            "apiVersion": f"{_CDI_API_GROUP}/{_CDI_API_VERSION}",
            "kind": "DataVolume",
            "metadata": {"name": dv_name, "namespace": self.namespace},
            "spec": {
                "source": {"http": {"url": proxy_url}},
                "pvc": {
                    "accessModes": ["ReadWriteOnce"],
                    "volumeMode": "Filesystem",
                    "resources": {"requests": {"storage": _VMEDIA_DV_SIZE}},
                },
            },
        }
        if self.storage_class:
            dv_spec["spec"]["pvc"]["storageClassName"] = self.storage_class

        self.custom_api.create_namespaced_custom_object(
            group=_CDI_API_GROUP,
            version=_CDI_API_VERSION,
            namespace=self.namespace,
            plural=_DV_PLURAL,
            body=dv_spec,
        )

        event, result = self._wait_for_dv(dv_name)

        vm = self._get_vm(identity)
        volumes = list(
            vm.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])
        )
        disks = list(
            vm.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("domain", {})
            .get("devices", {})
            .get("disks", [])
        )

        volumes.append(
            {
                "name": _VMEDIA_VOL_NAME,
                "dataVolume": {"name": dv_name},
            }
        )
        disks.append(
            {
                "name": _VMEDIA_VOL_NAME,
                "cdrom": {"bus": "sata"},
            }
        )

        self.custom_api.patch_namespaced_custom_object(
            group=_KUBEVIRT_API_GROUP,
            version=_KUBEVIRT_API_VERSION,
            namespace=self.namespace,
            plural=_VM_PLURAL,
            name=name,
            body={
                "spec": {
                    "template": {
                        "spec": {
                            "volumes": volumes,
                            "domain": {"devices": {"disks": disks}},
                        }
                    }
                }
            },
        )

        self._vmedia_state[identity] = {
            "url": image_url,
            "inserted": True,
            "dv_name": dv_name,
            "_dv_event": event,
            "_dv_result": result,
        }

    def eject_image(self, identity):
        """Remove CDROM from VM, delete DataVolume, and restart VMI if running.

        Checks the VM spec directly instead of relying on in-memory state,
        so ejecting works even if the BMC pod restarted since InsertMedia.
        """
        self._vmedia_state.pop(identity, None)
        name = self._kv_name(identity)
        self._boot_device_override.pop(name, None)
        dv_name = f"{name}{_VMEDIA_DV_SUFFIX}"
        removed = False

        try:
            vm = self._get_vm(identity)
            volumes = vm.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])  # type: ignore[union-attr]
            disks = vm.get("spec", {}).get("template", {}).get("spec", {}).get("domain", {}).get("devices", {}).get("disks", [])  # type: ignore[union-attr]
            new_volumes = [v for v in volumes if v.get("name") != _VMEDIA_VOL_NAME]
            new_disks = [d for d in disks if d.get("name") != _VMEDIA_VOL_NAME]
            if len(new_volumes) != len(volumes) or len(new_disks) != len(disks):
                self.custom_api.patch_namespaced_custom_object(
                    group=_KUBEVIRT_API_GROUP,
                    version=_KUBEVIRT_API_VERSION,
                    namespace=self.namespace,
                    plural=_VM_PLURAL,
                    name=name,
                    body={
                        "spec": {
                            "template": {
                                "spec": {
                                    "volumes": new_volumes,
                                    "domain": {"devices": {"disks": new_disks}},
                                }
                            }
                        }
                    },
                )
                removed = True
        except Exception:
            pass

        try:
            self.custom_api.delete_namespaced_custom_object(
                group=_CDI_API_GROUP,
                version=_CDI_API_VERSION,
                namespace=self.namespace,
                plural=_DV_PLURAL,
                name=dv_name,
            )
        except Exception:
            pass

        if removed:
            self._delete_vmi(identity)
