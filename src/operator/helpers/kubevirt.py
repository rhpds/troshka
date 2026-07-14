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
        domain.setdefault("firmware", {})["bootloader"] = {"efi": {"secureBoot": False}}
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
                disk_entry["bootOrder"] = boot_idx
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

    if spec.get("cdrom", {}).get("s3Path") and "cdrom" in disk_pvcs:
        cd_vol_name = "cdrom"
        domain["devices"]["disks"].append(
            {
                "name": cd_vol_name,
                "cdrom": {"bus": "sata"},
            }
        )
        volumes.append(
            {
                "name": cd_vol_name,
                "persistentVolumeClaim": {"claimName": disk_pvcs["cdrom"]},
            }
        )

    for i, nic in enumerate(spec.get("nics", [])):
        nic_id = nic.get("id", f"nic-{i}")[:8]
        iface_name = f"nic-{nic_id}"
        model = nic.get("model", "virtio")

        iface = {"name": iface_name, "bridge": {}}
        mac = nic.get("mac", "")
        if mac:
            iface["macAddress"] = mac
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

    import json
    import uuid

    metadata = json.dumps({
        "instance-id": f"{name}-{uuid.uuid4().hex[:8]}",
        "local-hostname": spec.get("name", name),
    })

    data = {
        "metadata": base64.b64encode(metadata.encode()).decode(),
    }
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


def build_recert_job(
    name,
    namespace,
    rhcos_pvc,
    bastion_pvc=None,
    extend_expiration=True,
    kubeadmin_password_hash=None,
):
    """Build a Job that runs recert on a cloned RHCOS PVC before VM boot."""
    from helpers.k8s import TOOLS_IMAGE

    recert_flags = "--extend-expiration" if extend_expiration else "--force-expire"
    password_args = ""
    if kubeadmin_password_hash:
        password_args = f'--kubeadmin-password-hash "{kubeadmin_password_hash}"'

    bastion_cmds = ""
    bastion_cleanup = ""
    volumes = [
        {"name": "rhcos-disk", "persistentVolumeClaim": {"claimName": rhcos_pvc}},
        {"name": "output", "emptyDir": {}},
    ]
    volume_mounts = [
        {"name": "rhcos-disk", "mountPath": "/rhcos"},
        {"name": "output", "mountPath": "/output"},
    ]

    if bastion_pvc:
        volumes.append(
            {"name": "bastion-disk", "persistentVolumeClaim": {"claimName": bastion_pvc}}
        )
        volume_mounts.append({"name": "bastion-disk", "mountPath": "/bastion"})
        bastion_cmds = (
            'echo "Mounting bastion disk..."\n'
            "qemu-nbd --connect /dev/nbd1 --format=raw /bastion/disk.img\n"
            "sleep 1; partprobe /dev/nbd1 2>/dev/null || true; sleep 1\n"
            "BPART=/dev/nbd1p3; [ -e /dev/nbd1p3 ] || BPART=/dev/nbd1p1\n"
            "mkdir -p /mnt/bastion; mount -o nouuid $BPART /mnt/bastion\n"
            'KC_SRC="$ETC_K8S/static-pod-resources/kube-apiserver-certs/secrets/node-kubeconfigs/lb-ext.kubeconfig"\n'
            'if [ -f "$KC_SRC" ]; then\n'
            '  KC_DST="/mnt/bastion/home/cloud-user/ocp-install/auth/kubeconfig"\n'
            '  mkdir -p "$(dirname $KC_DST)"; cp "$KC_SRC" "$KC_DST"\n'
            "  rm -f /mnt/bastion/etc/pki/ca-trust/source/anchors/ocp-ingress.pem\n"
            '  echo "Bastion kubeconfig updated"\n'
            "fi\n"
        )
        bastion_cleanup = (
            "umount /mnt/bastion 2>/dev/null || true\n"
            "qemu-nbd --disconnect /dev/nbd1 2>/dev/null || true\n"
        )

    script = (
        "#!/bin/bash\nset -e\n"
        "modprobe nbd max_part=8 2>/dev/null || true\n"
        'echo "Connecting RHCOS disk..."\n'
        "qemu-nbd --connect /dev/nbd0 --format=raw /rhcos/disk.img\n"
        "sleep 1; partprobe /dev/nbd0 2>/dev/null || true; sleep 1\n"
        "RHCOS_PART=''\n"
        "for p in /dev/nbd0p4 /dev/nbd0p3 /dev/nbd0p2 /dev/nbd0p1; do\n"
        "  [ -e $p ] || continue\n"
        "  mkdir -p /mnt/rhcos\n"
        "  if mount -o nouuid,ro $p /mnt/rhcos 2>/dev/null; then\n"
        "    if [ -d /mnt/rhcos/ostree/deploy/rhcos ]; then\n"
        "      RHCOS_PART=$p; umount /mnt/rhcos; break\n"
        "    fi; umount /mnt/rhcos\n"
        "  fi\n"
        "done\n"
        "[ -n \"$RHCOS_PART\" ] || { echo 'ERROR: no RHCOS partition found';"
        " fdisk -l /dev/nbd0 2>&1; qemu-nbd --disconnect /dev/nbd0; exit 1; }\n"
        "echo \"Found RHCOS on $RHCOS_PART\"\n"
        "mount -o nouuid $RHCOS_PART /mnt/rhcos\n"
        "DEPLOY_DIR=/mnt/rhcos/ostree/deploy/rhcos/deploy\n"
        "DEPLOY_HASH=$(ls $DEPLOY_DIR | grep -v .origin | head -1)\n"
        '[ -n "$DEPLOY_HASH" ] || { echo "ERROR: no OSTree deploy";'
        " umount /mnt/rhcos; qemu-nbd --disconnect /dev/nbd0; exit 1; }\n"
        'echo "OSTree: ${DEPLOY_HASH:0:12}"\n'
        'DEPLOY_ROOT="$DEPLOY_DIR/$DEPLOY_HASH"\n'
        'VAR_ROOT="/mnt/rhcos/ostree/deploy/rhcos/var"\n'
        'ETC_K8S="$DEPLOY_ROOT/etc/kubernetes"\n'
        'ETC_MCD="$DEPLOY_ROOT/etc/machine-config-daemon"\n'
        'VAR_KUBELET="$VAR_ROOT/lib/kubelet"\n'
        'VAR_ETCD="$VAR_ROOT/lib/etcd"\n'
        'echo "Starting etcd..."\n'
        "etcd --data-dir=$VAR_ETCD --name=recert-temp "
        "--listen-client-urls=http://127.0.0.1:2479 "
        "--advertise-client-urls=http://127.0.0.1:2479 "
        "--listen-peer-urls=http://127.0.0.1:2489 "
        "--force-new-cluster &\n"
        "ETCD_PID=$!\n"
        "for i in $(seq 1 30); do"
        " etcdctl --endpoints=http://127.0.0.1:2479 endpoint health"
        " 2>/dev/null | grep -q healthy && break; sleep 1; done\n"
        'echo "Running recert..."\n'
        "recert --etcd-endpoint=http://127.0.0.1:2479 "
        "--crypto-dir $ETC_K8S --crypto-dir $ETC_MCD --crypto-dir $VAR_KUBELET "
        "--cluster-customization-dir $ETC_K8S "
        "--cluster-customization-dir $VAR_KUBELET "
        f"{recert_flags} {password_args}\n"
        'echo "Recert done"\n'
        'KC="$ETC_K8S/static-pod-resources/kube-apiserver-certs/secrets/'
        'node-kubeconfigs/lb-ext.kubeconfig"\n'
        '[ -f "$KC" ] && cp "$KC" /output/kubeconfig\n'
        + bastion_cmds
        + "kill $ETCD_PID 2>/dev/null; wait $ETCD_PID 2>/dev/null || true\n"
        + bastion_cleanup
        + "umount /mnt/rhcos; qemu-nbd --disconnect /dev/nbd0\n"
        + 'echo "Recert job complete"\n'
    )

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"recert-{name}",
            "namespace": namespace,
            "labels": {"troshka-role": "recert"},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 600,
            "template": {
                "spec": {
                    "serviceAccountName": "troshka-recert",
                    "containers": [
                        {
                            "name": "recert",
                            "image": TOOLS_IMAGE,
                            "command": ["bash", "-c", script],
                            "volumeMounts": volume_mounts,
                            "securityContext": {"privileged": True},
                            "resources": {
                                "requests": {"cpu": "1", "memory": "2Gi"},
                                "limits": {"cpu": "4", "memory": "4Gi"},
                            },
                        }
                    ],
                    "volumes": volumes,
                    "restartPolicy": "Never",
                },
            },
        },
    }
