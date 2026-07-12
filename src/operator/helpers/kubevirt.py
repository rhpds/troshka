import base64

CACHE_NAMESPACE = "troshka-cache"
STORAGE_CLASS = "ocs-storagecluster-ceph-rbd-virtualization"


def build_kubevirt_vm(vm_cr, disk_pvcs, nad_refs, cloudinit_secret_name):
    spec = vm_cr["spec"]
    name = vm_cr["metadata"]["name"]

    kv_name = f"troshka-{name}"

    domain = {
        "cpu": {"cores": spec["cpus"]},
        "resources": {"requests": {"memory": f"{spec['memory']}Mi"}},
        "devices": {
            "disks": [],
            "interfaces": [],
        },
    }

    if spec.get("machineType"):
        domain["machine"] = {"type": spec["machineType"]}

    if spec.get("smbiosUuid"):
        domain.setdefault("firmware", {})["uuid"] = spec["smbiosUuid"]

    firmware_type = spec.get("firmware", "bios")
    if firmware_type == "uefi":
        domain.setdefault("firmware", {})["bootloader"] = {"efi": {}}
    elif firmware_type == "uefi-secure":
        domain.setdefault("firmware", {})["bootloader"] = {
            "efi": {"secureBoot": True}
        }
        domain.setdefault("features", {})["smm"] = {"enabled": True}

    volumes = []
    boot_idx = 1

    for i, disk_info in enumerate(spec.get("disks", [])):
        disk_id = disk_info.get("id", f"disk-{i}")[:8]
        vol_name = f"disk-{disk_id}"
        bus = disk_info.get("bus", "virtio")

        disk_entry = {"name": vol_name, "disk": {"bus": bus}}

        for bo in spec.get("bootOrder", []):
            bo_id = bo.get("id") if isinstance(bo, dict) else bo
            bo_type = bo.get("type", "disk") if isinstance(bo, dict) else "disk"
            if bo_type == "disk" and bo_id == disk_info.get("id"):
                disk_entry["disk"]["bootOrder"] = boot_idx
                boot_idx += 1
                break

        domain["devices"]["disks"].append(disk_entry)

        pvc_name = disk_pvcs.get(disk_info.get("id", ""), vol_name)
        volumes.append(
            {
                "name": vol_name,
                "persistentVolumeClaim": {"claimName": pvc_name},
            }
        )

    if spec.get("cdrom", {}).get("s3Path"):
        cd_vol_name = "cdrom"
        domain["devices"]["disks"].append(
            {
                "name": cd_vol_name,
                "cdrom": {"bus": "sata"},
            }
        )
        cd_pvc = disk_pvcs.get("cdrom", "cdrom-pvc")
        volumes.append(
            {
                "name": cd_vol_name,
                "persistentVolumeClaim": {"claimName": cd_pvc},
            }
        )

    for i, nic in enumerate(spec.get("nics", [])):
        nic_id = nic.get("id", f"nic-{i}")[:8]
        iface_name = f"nic-{nic_id}"
        model = nic.get("model", "virtio")

        iface = {"name": iface_name, "bridge": {}}
        if model and model != "virtio":
            iface["model"] = model

        for bo in spec.get("bootOrder", []):
            bo_id = bo.get("id") if isinstance(bo, dict) else bo
            bo_type = bo.get("type", "disk") if isinstance(bo, dict) else "disk"
            if bo_type == "network" and bo_id == nic.get("id"):
                iface["bootOrder"] = boot_idx
                boot_idx += 1
                break

        domain["devices"]["interfaces"].append(iface)

    if cloudinit_secret_name:
        domain["devices"]["disks"].append(
            {
                "name": "cloudinit",
                "disk": {"bus": "virtio"},
            }
        )
        volumes.append(
            {
                "name": "cloudinit",
                "cloudInitNoCloud": {
                    "secretRef": {"name": cloudinit_secret_name},
                },
            }
        )

    networks = []
    for i, nic in enumerate(spec.get("nics", [])):
        nic_id = nic.get("id", f"nic-{i}")[:8]
        iface_name = f"nic-{nic_id}"
        net_ref = nic.get("networkRef", "")
        nad_name = nad_refs.get(net_ref, f"{net_ref}-nad")

        networks.append(
            {
                "name": iface_name,
                "multus": {"networkName": nad_name},
            }
        )

    vm_body = {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachine",
        "metadata": {
            "name": kv_name,
            "namespace": vm_cr["metadata"]["namespace"],
            "labels": {"app": "troshka", "troshka-vm": name},
        },
        "spec": {
            "running": spec.get("powerOnAtDeploy", True),
            "template": {
                "metadata": {
                    "labels": {"app": "troshka", "troshka-vm": name},
                },
                "spec": {
                    "domain": domain,
                    "volumes": volumes,
                    "networks": networks,
                },
            },
        },
    }

    return vm_body


def build_cloudinit_secret(vm_cr):
    spec = vm_cr["spec"]
    ci = spec.get("cloudInit", {})
    if not ci.get("userData") and not ci.get("networkConfig"):
        return None

    name = vm_cr["metadata"]["name"]
    namespace = vm_cr["metadata"]["namespace"]

    data = {}
    if ci.get("userData"):
        data["userdata"] = base64.b64encode(
            ci["userData"].encode()
        ).decode()
    if ci.get("networkConfig"):
        data["networkdata"] = base64.b64encode(
            ci["networkConfig"].encode()
        ).decode()

    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": f"cloudinit-{name}",
            "namespace": namespace,
        },
        "data": data,
    }


def build_datavolume_from_s3(
    name, namespace, s3_path, size_gb, s3_config, presigned_url=""
):
    url = presigned_url
    if not url:
        bucket = s3_config.get("bucket", "")
        endpoint = s3_config.get("endpoint", "")
        region = s3_config.get("region", "us-east-1")
        if endpoint and "://" in endpoint:
            url = f"{endpoint}/{bucket}/{s3_path}"
        else:
            url = f"https://s3.{region}.amazonaws.com/{bucket}/{s3_path}"
    return {
        "apiVersion": "cdi.kubevirt.io/v1beta1",
        "kind": "DataVolume",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {
            "source": {
                "http": {
                    "url": url,
                },
            },
            "pvc": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {
                    "requests": {"storage": f"{max(size_gb + 10, int(size_gb * 1.2))}Gi"}
                },
                "storageClassName": STORAGE_CLASS,
            },
        },
    }


def build_blank_pvc(name, namespace, size_gb):
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {
                "requests": {"storage": f"{size_gb}Gi"}
            },
            "storageClassName": STORAGE_CLASS,
        },
    }


def build_clone_datavolume(
    name, namespace, source_pvc, source_namespace, size_gb
):
    return {
        "apiVersion": "cdi.kubevirt.io/v1beta1",
        "kind": "DataVolume",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {
            "source": {
                "pvc": {
                    "name": source_pvc,
                    "namespace": source_namespace,
                },
            },
            "pvc": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {
                    "requests": {"storage": f"{max(size_gb + 10, int(size_gb * 1.2))}Gi"}
                },
                "storageClassName": STORAGE_CLASS,
            },
        },
    }
