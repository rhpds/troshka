import base64

CACHE_NAMESPACE = "troshka-cache"
STORAGE_CLASS = "ocs-storagecluster-ceph-rbd-virtualization"


def s3_keys_from_secret(secret) -> dict:
    """Extract CDI-compatible S3 keys from a Kubernetes Secret."""
    data = secret.string_data or {}
    if not data and secret.data:
        data = {
            k: base64.b64decode(v).decode() for k, v in secret.data.items()
        }
    return {
        "accessKeyId": data.get("accessKeyId") or data.get("AWS_ACCESS_KEY_ID", ""),
        "secretKey": data.get("secretKey") or data.get("AWS_SECRET_ACCESS_KEY", ""),
    }


def hydrate_s3_config_from_project_secret(
    core_api,
    project_namespace: str | None,
    s3_config: dict | None,
    default_secret_name: str = "s3-credentials",  # pragma: allowlist secret
) -> dict:
    """Fill accessKeyId/secretKey from a project-namespace secret when CR omits them."""
    from kubernetes.client.exceptions import ApiException

    cfg = dict(s3_config or {})
    if cfg.get("accessKeyId") or not project_namespace:
        return cfg
    secret_name = cfg.get("credentialsSecret") or default_secret_name
    try:
        secret = core_api.read_namespaced_secret(secret_name, project_namespace)
    except ApiException as e:
        if e.status == 404:
            return cfg
        raise
    keys = s3_keys_from_secret(secret)
    if keys.get("accessKeyId"):
        cfg.update(keys)
    return cfg


def _build_base_domain(spec):
    """Build the base domain configuration for KubeVirt VM."""
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

    return domain


def _apply_firmware_settings(domain, spec):
    """Apply firmware settings (BIOS/UEFI/SecureBoot) to domain."""
    firmware_type = spec.get("firmware", "bios")
    if firmware_type == "uefi":
        domain.setdefault("firmware", {})["bootloader"] = {
            "efi": {"secureBoot": False, "persistent": True}
        }
    elif firmware_type == "uefi-secure":
        domain.setdefault("firmware", {})["bootloader"] = {
            "efi": {"secureBoot": True, "persistent": True}
        }
        domain.setdefault("features", {})["smm"] = {"enabled": True}


def _find_boot_order_index(device_id, device_type, boot_order, boot_idx):
    """Find and return boot order index for a device, or None if not in boot order."""
    for bo in boot_order:
        bo_id = bo.get("id") if isinstance(bo, dict) else bo
        bo_type = bo.get("type", "disk") if isinstance(bo, dict) else "disk"
        if bo_type == device_type and bo_id == device_id:
            return boot_idx
    return None


def _add_disks_to_domain(spec, disk_pvcs, domain, volumes):
    """Add disk devices and volumes to domain configuration."""
    boot_idx = 1
    boot_order = spec.get("bootOrder", [])

    for i, disk_info in enumerate(spec.get("disks", [])):
        disk_id = disk_info.get("id", f"disk-{i}")[:8]
        vol_name = f"disk-{disk_id}"
        bus = disk_info.get("bus", "virtio")

        disk_entry = {"name": vol_name, "disk": {"bus": bus}}

        order_idx = _find_boot_order_index(
            disk_info.get("id"), "disk", boot_order, boot_idx
        )
        if order_idx is not None:
            disk_entry["bootOrder"] = order_idx
            boot_idx += 1

        domain["devices"]["disks"].append(disk_entry)

        pvc_name = disk_pvcs.get(disk_info.get("id", ""), vol_name)
        volumes.append(
            {
                "name": vol_name,
                "persistentVolumeClaim": {"claimName": pvc_name},
            }
        )

    return boot_idx


def _add_cdrom_if_present(spec, disk_pvcs, domain, volumes):
    """Add CDROM device and volume if configured."""
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


def _add_nics_to_domain(spec, domain, boot_idx):
    """Add network interface devices to domain configuration."""
    boot_order = spec.get("bootOrder", [])

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

        order_idx = _find_boot_order_index(
            nic.get("id"), "network", boot_order, boot_idx
        )
        if order_idx is not None:
            iface["bootOrder"] = order_idx
            boot_idx += 1

        domain["devices"]["interfaces"].append(iface)


def _add_cloudinit_if_present(cloudinit_secret_name, domain, volumes):
    """Add cloud-init disk and volume if configured."""
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


def _build_networks(spec, nad_refs):
    """Build network attachments for NICs."""
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
    return networks


def build_kubevirt_vm(vm_cr, disk_pvcs, nad_refs, cloudinit_secret_name):
    spec = vm_cr["spec"]
    name = vm_cr["metadata"]["name"]
    kv_name = f"troshka-{name}"

    domain = _build_base_domain(spec)
    _apply_firmware_settings(domain, spec)

    volumes = []
    boot_idx = _add_disks_to_domain(spec, disk_pvcs, domain, volumes)
    _add_cdrom_if_present(spec, disk_pvcs, domain, volumes)
    _add_nics_to_domain(spec, domain, boot_idx)
    _add_cloudinit_if_present(cloudinit_secret_name, domain, volumes)

    networks = _build_networks(spec, nad_refs)

    template_spec: dict = {
        "domain": domain,
        "volumes": volumes,
        "networks": networks,
    }
    if spec.get("bmcEnabled"):
        template_spec["rebootPolicy"] = "Terminate"

    vm_body = {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachine",
        "metadata": {
            "name": kv_name,
            "namespace": vm_cr["metadata"]["namespace"],
            "labels": {"app": "troshka", "troshka-vm": name},
        },
        "spec": {
            "running": False,
            "template": {
                "metadata": {
                    "labels": {"app": "troshka", "troshka-vm": name},
                },
                "spec": template_spec,
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

    metadata = json.dumps(
        {
            "instance-id": f"{name}-{uuid.uuid4().hex[:8]}",
            "local-hostname": spec.get("name", name),
        }
    )

    data = {
        "metadata": base64.b64encode(metadata.encode()).decode(),
    }
    if ci.get("userData"):
        data["userdata"] = base64.b64encode(ci["userData"].encode()).decode()
    if ci.get("networkConfig"):
        data["networkdata"] = base64.b64encode(ci["networkConfig"].encode()).decode()

    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": f"cloudinit-{name}",
            "namespace": namespace,
        },
        "data": data,
    }


def s3_import_url(s3_path, s3_config):
    """Build the CDI S3 import URL for a bucket/path pair."""
    bucket = s3_config.get("bucket", "")
    endpoint = s3_config.get("endpoint", "")
    region = s3_config.get("region", "us-east-1")
    if endpoint and "://" in endpoint:
        return f"{endpoint.rstrip('/')}/{bucket}/{s3_path}"
    return f"https://s3.{region}.amazonaws.com/{bucket}/{s3_path}"


def golden_import_matches(dv, s3_path, s3_config, secret_name):
    """Return True when an existing golden DataVolume matches the desired import."""
    src = dv.get("spec", {}).get("source", {}).get("s3", {})
    return (
        src.get("url") == s3_import_url(s3_path, s3_config)
        and src.get("secretRef") == secret_name
    )


def delete_golden_import(custom_api, core_api, namespace, pvc_name):
    """Delete a golden DataVolume/PVC pair so it can be recreated."""
    from kubernetes.client.exceptions import ApiException

    for plural in ("datavolumes",):
        try:
            custom_api.delete_namespaced_custom_object(
                group="cdi.kubevirt.io",
                version="v1beta1",
                namespace=namespace,
                plural=plural,
                name=pvc_name,
            )
        except ApiException as e:
            if e.status != 404:
                raise
    try:
        core_api.delete_namespaced_persistent_volume_claim(
            name=pvc_name, namespace=namespace
        )
    except ApiException as e:
        if e.status != 404:
            raise


def build_datavolume_from_s3(
    name,
    namespace,
    s3_path,
    size_gb,
    s3_config,
    secret_name="s3-credentials",  # pragma: allowlist secret
):
    s3_url = s3_import_url(s3_path, s3_config)
    return {
        "apiVersion": "cdi.kubevirt.io/v1beta1",
        "kind": "DataVolume",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {
            "source": {
                "s3": {
                    "url": s3_url,
                    "secretRef": secret_name,
                },
            },
            "pvc": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {
                    "requests": {
                        "storage": f"{max(size_gb + 10, int(size_gb * 1.2))}Gi"
                    }
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
            "resources": {"requests": {"storage": f"{size_gb}Gi"}},
            "storageClassName": STORAGE_CLASS,
        },
    }


def build_clone_datavolume(name, namespace, source_pvc, source_namespace, size_gb):
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
                    "requests": {
                        "storage": f"{max(size_gb + 10, int(size_gb * 1.2))}Gi"
                    }
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
    vm_name=None,
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
            {
                "name": "bastion-disk",
                "persistentVolumeClaim": {"claimName": bastion_pvc},
            }
        )
        volume_mounts.append({"name": "bastion-disk", "mountPath": "/bastion"})
        bastion_cmds = (
            'echo "Mounting bastion disk..."\n'
            "BLOOP=$(losetup -f --show /bastion/disk.img)\n"
            "BLOOP_BASE=$(basename $BLOOP)\n"
            "kpartx -av $BLOOP\n"
            "sleep 1\n"
            "BPART=/dev/mapper/${BLOOP_BASE}p3; [ -e $BPART ] || BPART=/dev/mapper/${BLOOP_BASE}p1\n"
            "mkdir -p /mnt/bastion\n"
            "if ! mount $BPART /mnt/bastion 2>/dev/null && ! mount -o nouuid $BPART /mnt/bastion 2>/dev/null; then\n"
            "  xfs_repair -L $BPART >/dev/null 2>&1 && echo 'Repaired XFS log on bastion disk'\n"
            "  mount $BPART /mnt/bastion 2>/dev/null || mount -o nouuid $BPART /mnt/bastion\n"
            "fi\n"
            'KC_SRC="/etc/kubernetes/static-pod-resources/kube-apiserver-certs/secrets/node-kubeconfigs/lb-ext.kubeconfig"\n'
            'if [ -f "$KC_SRC" ]; then\n'
            '  KC_DIR="/mnt/bastion/home/cloud-user/ocp-install/auth"\n'
            '  mkdir -p "$KC_DIR"\n'
            + (
                f'  cp "$KC_SRC" "$KC_DIR/kubeconfig-{vm_name}"\n'
                f'  echo "Wrote kubeconfig-{vm_name} to bastion"\n'
                if vm_name
                else '  cp "$KC_SRC" "$KC_DIR/kubeconfig"\n'
            )
            + "  rm -f /mnt/bastion/etc/pki/ca-trust/source/anchors/ocp-ingress.pem\n"
            "fi\n"
        )
        bastion_cleanup = (
            "umount /mnt/bastion 2>/dev/null || true\n"
            "kpartx -dv $BLOOP 2>/dev/null || true\n"
            "losetup -d $BLOOP 2>/dev/null || true\n"
        )

    script = (
        "#!/bin/bash\nset -e\n"
        'echo "Connecting RHCOS disk..."\n'
        "LOOP=$(losetup -f --show /rhcos/disk.img)\n"
        "LOOP_BASE=$(basename $LOOP)\n"
        "kpartx -av $LOOP\n"
        "sleep 1\n"
        "RHCOS_PART=''\n"
        "mkdir -p /mnt/rhcos\n"
        "for p in /dev/mapper/${LOOP_BASE}p4 /dev/mapper/${LOOP_BASE}p3 /dev/mapper/${LOOP_BASE}p2 /dev/mapper/${LOOP_BASE}p1; do\n"
        "  [ -e $p ] || continue\n"
        "  if ! mount $p /mnt/rhcos 2>/dev/null && ! mount -o nouuid $p /mnt/rhcos 2>/dev/null; then\n"
        '    xfs_repair -L $p >/dev/null 2>&1 && echo "Repaired XFS log on $p"\n'
        "    mount $p /mnt/rhcos 2>/dev/null || mount -o nouuid $p /mnt/rhcos 2>/dev/null || continue\n"
        "  fi\n"
        "  if [ -d /mnt/rhcos/ostree/deploy/rhcos ]; then\n"
        "    RHCOS_PART=$p; break\n"
        "  fi; umount /mnt/rhcos\n"
        "done\n"
        "[ -n \"$RHCOS_PART\" ] || { echo 'ERROR: no RHCOS partition found';"
        " fdisk -l $LOOP 2>&1; kpartx -dv $LOOP; losetup -d $LOOP; exit 1; }\n"
        'echo "Found RHCOS on $RHCOS_PART"\n'
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
        "# Bind-mount so recert sees standard paths (same as troshkad podman -v)\n"
        "mkdir -p /etc/kubernetes /etc/machine-config-daemon /var/lib/kubelet\n"
        "mount --bind $ETC_K8S /etc/kubernetes\n"
        "mount --bind $ETC_MCD /etc/machine-config-daemon\n"
        "mount --bind $VAR_KUBELET /var/lib/kubelet\n"
        "ETCD_BIN=etcd; ETCDCTL_BIN=etcdctl\n"
        'echo "Using etcd $(etcd --version 2>&1 | head -1)"\n'
        'echo "Starting etcd..."\n'
        "$ETCD_BIN --data-dir=$VAR_ETCD --name=recert-temp "
        "--listen-client-urls=http://127.0.0.1:2479 "
        "--advertise-client-urls=http://127.0.0.1:2479 "
        "--listen-peer-urls=http://127.0.0.1:2489 "
        "--force-new-cluster &\n"
        "ETCD_PID=$!\n"
        "for i in $(seq 1 30); do"
        " $ETCDCTL_BIN --endpoints=http://127.0.0.1:2479 endpoint health"
        " 2>/dev/null | grep -q healthy && break; sleep 1; done\n"
        'echo "Running recert..."\n'
        "recert --etcd-endpoint=http://127.0.0.1:2479 "
        "--crypto-dir /etc/kubernetes --crypto-dir /etc/machine-config-daemon --crypto-dir /var/lib/kubelet "
        "--cluster-customization-dir /etc/kubernetes "
        "--cluster-customization-dir /var/lib/kubelet "
        f"{recert_flags} {password_args}\n"
        'echo "Recert done"\n'
        "# Relax kube-apiserver liveness probe to survive boot storm\n"
        "# (cert-regeneration sidecar causes brief TLS disruptions that kill apiserver\n"
        "#  before OVN can sync — increasing failureThreshold from 3 to 8 gives 80s)\n"
        'APIMAN="/etc/kubernetes/manifests/kube-apiserver-pod.yaml"\n'
        'if [ -f "$APIMAN" ]; then\n'
        '  python3 -c "\n'
        "import json, sys\n"
        "with open(sys.argv[1]) as f: pod = json.load(f)\n"
        "for c in pod.get('spec',{}).get('containers',[]):\n"
        "  for probe in ('livenessProbe','startupProbe'):\n"
        "    if probe in c:\n"
        "      c[probe]['failureThreshold'] = 8\n"
        "with open(sys.argv[1],'w') as f: json.dump(pod, f)\n"
        '" "$APIMAN" && echo \'Relaxed apiserver liveness probe\'\n'
        "fi\n"
        'KC="/etc/kubernetes/static-pod-resources/kube-apiserver-certs/secrets/'
        'node-kubeconfigs/lb-ext.kubeconfig"\n'
        '[ -f "$KC" ] && cp "$KC" /output/kubeconfig\n'
        '[ -f "$KC" ] && echo "KUBECONFIG_B64_BEGIN" && base64 -w0 "$KC" && echo && echo "KUBECONFIG_B64_END"\n'
        + bastion_cmds
        + "kill $ETCD_PID 2>/dev/null; wait $ETCD_PID 2>/dev/null || true\n"
        + bastion_cleanup
        + "umount /etc/kubernetes /etc/machine-config-daemon /var/lib/kubelet 2>/dev/null\n"
        + "umount /mnt/rhcos; kpartx -dv $LOOP; losetup -d $LOOP\n"
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
                            "imagePullPolicy": "Always",
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
