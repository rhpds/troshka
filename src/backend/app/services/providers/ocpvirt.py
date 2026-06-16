"""OCP Virt (KubeVirt) provider driver.

Creates large nested-virt RHEL VMs on OpenShift Virtualization.
The VMs run troshkad identically to EC2 instances.
"""

import logging
import time

from app.services.providers.base import ProviderDriver

logger = logging.getLogger(__name__)

CLOUD_INIT_TEMPLATE = """#cloud-config
user: cloud-user
ssh_authorized_keys:
  - {ssh_pubkey}
write_files:
  - path: /etc/resolv.conf
    content: |
      search troshka.svc.cluster.local svc.cluster.local cluster.local
      nameserver 172.30.0.10
      options ndots:5
    permissions: '0644'
runcmd:
  - |
    mkdir -p /mnt/iso
    mount /dev/sr0 /mnt/iso || mount /dev/cdrom /mnt/iso || true
    if [ -d /mnt/iso/BaseOS ]; then
      cat > /etc/yum.repos.d/local-baseos.repo << 'REPOEOF'
    [local-baseos]
    name=Local BaseOS
    baseurl=file:///mnt/iso/BaseOS
    enabled=1
    gpgcheck=0
    REPOEOF
      cat > /etc/yum.repos.d/local-appstream.repo << 'REPOEOF'
    [local-appstream]
    name=Local AppStream
    baseurl=file:///mnt/iso/AppStream
    enabled=1
    gpgcheck=0
    REPOEOF
    fi
  - dnf install -y qemu-kvm libvirt libvirt-client virt-install python3 python3-libvirt dnsmasq nftables nmap-ncat nfs-utils || true
  - systemctl enable --now libvirtd || systemctl enable --now virtqemud.socket virtnetworkd.socket virtstoraged.socket
  - systemctl enable --now nftables
  - systemctl disable --now dnsmasq 2>/dev/null || true
  - |
    DATA_DEV=/dev/vdb
    if [ -b "$DATA_DEV" ]; then
      blkid "$DATA_DEV" || mkfs.xfs "$DATA_DEV"
      mkdir -p /var/lib/troshka
      mount "$DATA_DEV" /var/lib/troshka
      grep -q /var/lib/troshka /etc/fstab || echo "$DATA_DEV /var/lib/troshka xfs defaults,nofail 0 2" >> /etc/fstab
    else
      mkdir -p /var/lib/troshka
    fi
  - mkdir -p /var/lib/troshka/images /var/lib/troshka/vms /var/lib/troshka/tmp /etc/troshka-agent
  - semanage fcontext -a -t virt_image_t '/var/lib/troshka(/.*)?' 2>/dev/null || true
  - restorecon -R /var/lib/troshka
  - 'echo "host_id: {host_id}" > /etc/troshka-agent/host-id'
"""

CLOUD_INIT_PATTERN_BUFFER = """#cloud-config
user: cloud-user
ssh_authorized_keys:
  - {ssh_pubkey}
write_files:
  - path: /etc/resolv.conf
    content: |
      search troshka.svc.cluster.local svc.cluster.local cluster.local
      nameserver 172.30.0.10
      options ndots:5
    permissions: '0644'
runcmd:
  - |
    mkdir -p /mnt/iso
    mount /dev/sr0 /mnt/iso || mount /dev/cdrom /mnt/iso || true
    if [ -d /mnt/iso/BaseOS ]; then
      cat > /etc/yum.repos.d/local-baseos.repo << 'REPOEOF'
    [local-baseos]
    name=Local BaseOS
    baseurl=file:///mnt/iso/BaseOS
    enabled=1
    gpgcheck=0
    REPOEOF
      cat > /etc/yum.repos.d/local-appstream.repo << 'REPOEOF'
    [local-appstream]
    name=Local AppStream
    baseurl=file:///mnt/iso/AppStream
    enabled=1
    gpgcheck=0
    REPOEOF
    fi
  - dnf install -y python3 qemu-img nfs-utils || true
  - mkdir -p /var/lib/troshka /etc/troshka-agent
  - 'echo "host_id: {host_id}" > /etc/troshka-agent/host-id'
"""


def _get_k8s_clients(credentials):
    from kubernetes import client

    configuration = client.Configuration()
    configuration.host = credentials["api_url"]
    configuration.api_key = {"authorization": f"Bearer {credentials['token']}"}
    configuration.verify_ssl = credentials.get("verify_ssl", False)
    api_client = client.ApiClient(configuration)
    custom_api = client.CustomObjectsApi(api_client)
    core_api = client.CoreV1Api(api_client)
    return custom_api, core_api


def _parse_instance_type(instance_type):
    """Parse '64c-256g' into (cores, memory_gi)."""
    if not instance_type or "-" not in instance_type:
        return 64, 256
    parts = instance_type.replace("c-", " ").replace("g", "").split()
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return 64, 256


def _generate_ssh_keypair():
    """Generate an SSH keypair for provisioning."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    public_key = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    return private_pem, public_key


class OCPVirtDriver(ProviderDriver):
    def provision_host(
        self, provider, host_id, instance_type, storage_size_gb, **kwargs
    ):
        from kubernetes import client

        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, core_api = _get_k8s_clients(creds)

        host_type = kwargs.get("host_type", "shared")
        cores, memory_gi = _parse_instance_type(instance_type)
        hostname = f"troshka-host-{host_id[:8]}"
        private_key, public_key = _generate_ssh_keypair()

        # Ensure namespace exists
        try:
            core_api.read_namespace(namespace)
        except client.ApiException as e:
            if e.status == 404:
                core_api.create_namespace(
                    client.V1Namespace(metadata=client.V1ObjectMeta(name=namespace))
                )

        # Build cloud-init
        template = (
            CLOUD_INIT_PATTERN_BUFFER
            if host_type == "pattern_buffer"
            else CLOUD_INIT_TEMPLATE
        )

        user_data = template.format(ssh_pubkey=public_key, host_id=host_id)

        # Append NFS mount if shared storage
        nfs_server = kwargs.get("nfs_server")
        nfs_path = kwargs.get("nfs_path")
        if nfs_server and nfs_path:
            user_data = user_data.rstrip() + (
                f"\n  - mkdir -p /var/lib/troshka/shared"
                f"\n  - 'echo \"{nfs_server}:{nfs_path} /var/lib/troshka/shared nfs "
                f"nfsvers=4.1,nconnect=16,hard,_netdev 0 0\" >> /etc/fstab'"
                f"\n  - mount /var/lib/troshka/shared"
                f"\n  - setsebool -P virt_use_nfs 1"
                f"\n"
            )

        # Build VM spec — use DataSource ref (preferred) or HTTP URL fallback
        rhel_image_url = kwargs.get("rhel_image_url", "")
        datasource_name = kwargs.get("ami_id") or "rhel9"
        if rhel_image_url:
            root_source = {"source": {"http": {"url": rhel_image_url}}}
        else:
            root_source = {
                "sourceRef": {
                    "kind": "DataSource",
                    "name": datasource_name,
                    "namespace": "openshift-virtualization-os-images",
                }
            }

        data_volumes = [
            {
                "metadata": {"name": f"{hostname}-root"},
                "spec": {
                    **root_source,
                    "storage": {
                        "resources": {"requests": {"storage": "50Gi"}},
                        "storageClassName": "ocs-storagecluster-ceph-rbd-virtualization",
                    },
                },
            },
            {
                "metadata": {"name": f"{hostname}-data"},
                "spec": {
                    "source": {"blank": {}},
                    "storage": {
                        "resources": {"requests": {"storage": f"{storage_size_gb}Gi"}},
                        "storageClassName": "ocs-storagecluster-ceph-rbd-virtualization",
                    },
                },
            },
        ]

        # ISO PVC name — must exist in the same namespace
        iso_pvc = kwargs.get("iso_pvc") or creds.get("iso_pvc", "rhel-10.2-dvd-iso")

        disks = [
            {"disk": {"bus": "virtio"}, "name": "rootdisk"},
            {"disk": {"bus": "virtio"}, "name": "datadisk"},
            {"cdrom": {"bus": "sata", "readonly": True}, "name": "installiso"},
            {"disk": {"bus": "virtio"}, "name": "cloudinitdisk"},
        ]
        volumes = [
            {"dataVolume": {"name": f"{hostname}-root"}, "name": "rootdisk"},
            {"dataVolume": {"name": f"{hostname}-data"}, "name": "datadisk"},
            {
                "persistentVolumeClaim": {"claimName": iso_pvc},
                "name": "installiso",
            },
            {
                "cloudInitNoCloud": {
                    "secretRef": {"name": f"{hostname}-userdata"},
                },
                "name": "cloudinitdisk",
            },
        ]

        # Create Secret with cloud-init userdata (KubeVirt enforces 2KB inline limit)
        import base64

        secret_body = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=f"{hostname}-userdata",
                namespace=namespace,
                labels={"app": "troshka", "troshka/host-id": host_id},
            ),
            data={
                "userdata": base64.b64encode(user_data.encode()).decode(),
            },
        )
        core_api.create_namespaced_secret(namespace=namespace, body=secret_body)

        # Pattern buffer gets a scratch volume for qemu-img/NBD capture
        if host_type == "pattern_buffer":
            data_volumes.append(
                {
                    "metadata": {"name": f"{hostname}-scratch"},
                    "spec": {
                        "source": {"blank": {}},
                        "storage": {
                            "resources": {"requests": {"storage": "500Gi"}},
                            "storageClassName": "ocs-storagecluster-ceph-rbd-virtualization",
                        },
                    },
                }
            )
            disks.append({"disk": {"bus": "virtio"}, "name": "scratch"})
            volumes.append(
                {"dataVolume": {"name": f"{hostname}-scratch"}, "name": "scratch"}
            )

        vm_manifest = {
            "apiVersion": "kubevirt.io/v1",
            "kind": "VirtualMachine",
            "metadata": {
                "name": hostname,
                "namespace": namespace,
                "labels": {
                    "app": "troshka",
                    "troshka/host-id": host_id,
                    "troshka/host-type": host_type,
                },
            },
            "spec": {
                "running": True,
                "dataVolumeTemplates": data_volumes,
                "template": {
                    "metadata": {
                        "labels": {
                            "kubevirt.io/domain": hostname,
                            "app": "troshka",
                        }
                    },
                    "spec": {
                        "domain": {
                            "cpu": {
                                "cores": cores,
                                "model": "host-passthrough",
                            },
                            "memory": {"guest": f"{memory_gi}Gi"},
                            "devices": {
                                "disks": disks,
                                "interfaces": [
                                    {
                                        "masquerade": {},
                                        "model": "virtio",
                                        "name": "default",
                                    }
                                ],
                                "rng": {},
                            },
                        },
                        "networks": [{"name": "default", "pod": {}}],
                        "volumes": volumes,
                        "terminationGracePeriodSeconds": 180,
                    },
                },
            },
        }

        custom_api.create_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            body=vm_manifest,
        )
        logger.info("Created VirtualMachine %s in namespace %s", hostname, namespace)

        # Create LoadBalancer service for SSH + troshkad (MetalLB assigns external IP)
        svc = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=f"troshka-lb-{host_id[:8]}",
                namespace=namespace,
                labels={"app": "troshka", "troshka/host-id": host_id},
            ),
            spec=client.V1ServiceSpec(
                type="LoadBalancer",
                selector={"kubevirt.io/domain": hostname},
                ports=[
                    client.V1ServicePort(
                        name="ssh", port=22000, target_port=22, protocol="TCP"
                    ),
                    client.V1ServicePort(
                        name="agent", port=31337, target_port=31337, protocol="TCP"
                    ),
                    client.V1ServicePort(
                        name="console", port=443, target_port=443, protocol="TCP"
                    ),
                ],
            ),
        )
        core_api.create_namespaced_service(namespace=namespace, body=svc)

        # Wait for VMI to reach Running
        pod_ip = None
        for attempt in range(120):
            time.sleep(5)
            try:
                vmi = custom_api.get_namespaced_custom_object(
                    group="kubevirt.io",
                    version="v1",
                    namespace=namespace,
                    plural="virtualmachineinstances",
                    name=hostname,
                )
                phase = vmi.get("status", {}).get("phase")
                if phase == "Running":
                    interfaces = vmi.get("status", {}).get("interfaces", [])
                    if interfaces:
                        pod_ip = interfaces[0].get("ipAddress")
                    break
            except client.ApiException:
                pass
        else:
            raise RuntimeError(
                f"VM {hostname} did not reach Running state within 10 minutes"
            )

        # Wait for LoadBalancer external IP assignment (MetalLB)
        external_ip = None
        for _ in range(60):
            lb_svc = core_api.read_namespaced_service(
                f"troshka-lb-{host_id[:8]}", namespace
            )
            ingress = (lb_svc.status.load_balancer or {}).ingress
            if ingress and ingress[0].ip:
                external_ip = ingress[0].ip
                break
            time.sleep(2)
        if not external_ip:
            logger.warning("No external IP assigned for host %s", host_id[:8])

        return {
            "host_id": host_id,
            "instance_id": hostname,
            "instance_type": instance_type or f"{cores}c-{memory_gi}g",
            "public_ip": external_ip,
            "private_ip": pod_ip,
            "total_vcpus": cores,
            "total_ram_mb": memory_gi * 1024,
            "key_pair_name": None,
            "private_key": private_key,
            "storage_size_gb": storage_size_gb,
            "max_eips": 100,
            "_ssh_host": external_ip,
            "_ssh_port": 22000,
        }

    def terminate_host(self, provider, instance_id):
        from kubernetes import client

        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, core_api = _get_k8s_clients(creds)

        # Force stop: set running=false, then force-delete VMI (like virtctl stop --force)
        try:
            custom_api.patch_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
                name=instance_id,
                body={"spec": {"running": False}},
            )
        except client.ApiException:
            pass
        try:
            custom_api.delete_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachineinstances",
                name=instance_id,
                grace_period_seconds=0,
                body=client.V1DeleteOptions(grace_period_seconds=0),
            )
        except client.ApiException:
            pass

        import time

        time.sleep(3)

        # Then delete the VM
        try:
            custom_api.delete_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
                name=instance_id,
            )
        except client.ApiException as e:
            if e.status != 404:
                raise

        # Clean up all associated services and routes
        host_short = instance_id.replace("troshka-host-", "")
        for prefix in [
            "troshka-lb-",
            "troshka-vncd-",
        ]:
            try:
                core_api.delete_namespaced_service(f"{prefix}{host_short}", namespace)
            except client.ApiException:
                pass

        # Clean up EIP LB services by label
        try:
            eip_svcs = core_api.list_namespaced_service(
                namespace,
                label_selector=f"troshka/host-id={host_short}",
            )
            for svc in eip_svcs.items:
                if svc.metadata.name.startswith("troshka-eip-"):
                    try:
                        core_api.delete_namespaced_service(svc.metadata.name, namespace)
                    except client.ApiException:
                        pass
        except client.ApiException:
            pass

        # Clean up userdata secret
        try:
            core_api.delete_namespaced_secret(f"{instance_id}-userdata", namespace)
        except client.ApiException:
            pass

        try:
            custom_api.delete_namespaced_custom_object(
                group="route.openshift.io",
                version="v1",
                namespace=namespace,
                plural="routes",
                name=f"troshka-console-{host_short}",
            )
        except client.ApiException:
            pass

        logger.info("Terminated OCP Virt host %s", instance_id)

    def get_host_status(self, provider, instance_id):
        from kubernetes import client

        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, _ = _get_k8s_clients(creds)

        try:
            vmi = custom_api.get_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachineinstances",
                name=instance_id,
            )
            phase = vmi.get("status", {}).get("phase", "Unknown")
            interfaces = vmi.get("status", {}).get("interfaces", [])
            pod_ip = interfaces[0].get("ipAddress") if interfaces else None
            state_map = {
                "Running": "running",
                "Succeeded": "terminated",
                "Failed": "terminated",
                "Pending": "pending",
                "Scheduling": "pending",
            }
            return {
                "instance_id": instance_id,
                "state": state_map.get(phase, "unknown"),
                "public_ip": None,
                "private_ip": pod_ip,
            }
        except client.ApiException:
            return None

    def resize_host(self, provider, instance_id, new_instance_type):
        raise NotImplementedError("Resize is not supported for OCP Virt hosts")

    def extend_host_storage(self, provider, host, db, increment_gb=None):
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        _, core_api = _get_k8s_clients(creds)

        hostname = host.instance_id
        pvc_name = f"{hostname}-root"
        increment = increment_gb or host.auto_extend_increment_gb
        new_size = host.storage_size_gb + increment

        if host.auto_extend_max_gb:
            new_size = min(new_size, host.auto_extend_max_gb)
        if new_size <= host.storage_size_gb:
            raise ValueError(
                f"Cannot extend: already at max ({host.storage_size_gb} GB)"
            )

        core_api.patch_namespaced_persistent_volume_claim(
            pvc_name,
            namespace,
            {"spec": {"resources": {"requests": {"storage": f"{new_size}Gi"}}}},
        )

        old_size = host.storage_size_gb
        host.storage_size_gb = new_size
        db.commit()
        logger.info("Extended PVC %s from %d to %d GB", pvc_name, old_size, new_size)
        return {"old_size_gb": old_size, "new_size_gb": new_size}

    def setup_console(self, provider, base_domain):
        return {
            "console_base_domain": base_domain,
            "console_zone_id": None,
            "console_nameservers": None,
        }

    def create_console_record(self, provider, host, hostname, ip_address):
        from kubernetes import client

        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, core_api = _get_k8s_clients(creds)
        host_short = host.instance_id.replace("troshka-host-", "")

        # Create vncd Service (plain WebSocket on 8080, TLS handled by OCP router)
        svc = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=f"troshka-vncd-{host_short}",
                namespace=namespace,
            ),
            spec=client.V1ServiceSpec(
                selector={"kubevirt.io/domain": host.instance_id},
                ports=[client.V1ServicePort(port=8080, target_port=8080)],
            ),
        )
        try:
            core_api.create_namespaced_service(namespace=namespace, body=svc)
        except client.ApiException as e:
            if e.status != 409:
                raise

        # Create edge-terminated Route
        route = {
            "apiVersion": "route.openshift.io/v1",
            "kind": "Route",
            "metadata": {
                "name": f"troshka-console-{host_short}",
                "namespace": namespace,
            },
            "spec": {
                "host": hostname,
                "to": {
                    "kind": "Service",
                    "name": f"troshka-vncd-{host_short}",
                },
                "port": {"targetPort": 8080},
                "tls": {
                    "termination": "edge",
                    "insecureEdgeTerminationPolicy": "Redirect",
                },
            },
        }
        try:
            custom_api.create_namespaced_custom_object(
                group="route.openshift.io",
                version="v1",
                namespace=namespace,
                plural="routes",
                body=route,
            )
        except client.ApiException as e:
            if e.status != 409:
                raise

    def delete_console_record(self, provider, host, hostname, ip_address):
        from kubernetes import client

        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, core_api = _get_k8s_clients(creds)
        host_short = host.instance_id.replace("troshka-host-", "")

        try:
            core_api.delete_namespaced_service(f"troshka-vncd-{host_short}", namespace)
        except client.ApiException:
            pass
        try:
            custom_api.delete_namespaced_custom_object(
                group="route.openshift.io",
                version="v1",
                namespace=namespace,
                plural="routes",
                name=f"troshka-console-{host_short}",
            )
        except client.ApiException:
            pass

    def get_host_powerstate(self, provider, instance_id):
        status = self.get_host_status(provider, instance_id)
        return status["state"] if status else "unknown"

    def start_host(self, provider, instance_id):
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, _ = _get_k8s_clients(creds)
        custom_api.patch_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=instance_id,
            body={"spec": {"running": True}},
        )

    def stop_host(self, provider, instance_id):
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, _ = _get_k8s_clients(creds)
        custom_api.patch_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=instance_id,
            body={"spec": {"running": False}},
        )

    def allocate_eip(self, provider, host, eip_id):
        from kubernetes import client

        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        _, core_api = _get_k8s_clients(creds)

        svc_name = f"troshka-eip-{eip_id[:8]}"
        svc = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=svc_name,
                namespace=namespace,
                labels={
                    "app": "troshka",
                    "troshka/eip-id": eip_id,
                    "troshka/host-id": host.instance_id.replace("troshka-host-", ""),
                },
            ),
            spec=client.V1ServiceSpec(
                type="LoadBalancer",
                selector={"kubevirt.io/domain": host.instance_id},
                ports=[
                    client.V1ServicePort(
                        name="placeholder",
                        port=1,
                        target_port=1,
                        protocol="TCP",
                    )
                ],
            ),
        )
        core_api.create_namespaced_service(namespace=namespace, body=svc)

        external_ip = None
        for _ in range(60):
            time.sleep(2)
            lb_svc = core_api.read_namespaced_service(svc_name, namespace)
            ingress = (lb_svc.status.load_balancer or {}).ingress
            if ingress and ingress[0].ip:
                external_ip = ingress[0].ip
                break
        if not external_ip:
            raise RuntimeError(f"MetalLB did not assign IP for {svc_name}")

        logger.info(
            "Allocated EIP %s (%s) for host %s",
            external_ip,
            svc_name,
            host.instance_id,
        )
        return {"public_ip": external_ip, "allocation_id": svc_name}

    def associate_eip(self, provider, host, allocation_id):
        return {}

    def release_eip(self, provider, allocation_id, namespace=None):
        from kubernetes import client

        creds = provider.get_credentials()
        ns = namespace or creds.get("namespace", "troshka")
        _, core_api = _get_k8s_clients(creds)

        try:
            core_api.delete_namespaced_service(allocation_id, ns)
            logger.info("Deleted EIP LB Service %s", allocation_id)
        except client.ApiException as e:
            if e.status != 404:
                raise

    def update_eip_ports(self, provider, host, allocation_id, ports):
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        _, core_api = _get_k8s_clients(creds)

        svc_ports = [
            {
                "port": p["port"],
                "targetPort": p["targetPort"],
                "name": p["name"],
                "protocol": "TCP",
            }
            for p in ports
        ]
        core_api.patch_namespaced_service(
            allocation_id, namespace, {"spec": {"ports": svc_ports}}
        )
        logger.info(
            "Updated EIP %s ports: %s",
            allocation_id,
            [p["port"] for p in ports],
        )
