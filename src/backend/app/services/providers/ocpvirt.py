"""OCP Virt (KubeVirt) provider driver.

Creates large nested-virt RHEL VMs on OpenShift Virtualization.
The VMs run troshkad identically to EC2 instances.
"""

import logging
import time

from app.services.providers.base import ProviderDriver

logger = logging.getLogger(__name__)

CLOUD_INIT_TEMPLATE = """#cloud-config
user: ec2-user
ssh_authorized_keys:
  - {ssh_pubkey}
packages:
  - qemu-kvm
  - libvirt
  - libvirt-client
  - virt-install
  - python3
  - python3-libvirt
  - dnsmasq
  - nftables
  - nmap-ncat
  - nfs-utils
write_files:
  - path: /etc/resolv.conf
    content: |
      search troshka.svc.cluster.local svc.cluster.local cluster.local
      nameserver 172.30.0.10
      options ndots:5
    permissions: '0644'
runcmd:
  - systemctl enable --now libvirtd || systemctl enable --now virtqemud.socket virtnetworkd.socket virtstoraged.socket
  - systemctl enable --now nftables
  - systemctl disable --now dnsmasq 2>/dev/null || true
  - mkdir -p /var/lib/troshka /etc/troshka-agent
  - 'echo "host_id: {host_id}" > /etc/troshka-agent/host-id'
"""

CLOUD_INIT_PATTERN_BUFFER = """#cloud-config
user: ec2-user
ssh_authorized_keys:
  - {ssh_pubkey}
packages:
  - python3
  - qemu-img
  - nfs-utils
write_files:
  - path: /etc/resolv.conf
    content: |
      search troshka.svc.cluster.local svc.cluster.local cluster.local
      nameserver 172.30.0.10
      options ndots:5
    permissions: '0644'
runcmd:
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

        # Build VM spec
        data_volumes = [
            {
                "metadata": {"name": f"{hostname}-root"},
                "spec": {
                    "source": {"http": {"url": kwargs.get("rhel_image_url", "")}},
                    "storage": {
                        "resources": {"requests": {"storage": f"{storage_size_gb}Gi"}},
                        "storageClassName": "ocs-storagecluster-ceph-rbd-virtualization",
                    },
                },
            }
        ]

        disks = [
            {"disk": {"bus": "virtio"}, "name": "rootdisk"},
            {"disk": {"bus": "virtio"}, "name": "cloudinitdisk"},
        ]
        volumes = [
            {"dataVolume": {"name": f"{hostname}-root"}, "name": "rootdisk"},
            {"cloudInitNoCloud": {"userData": user_data}, "name": "cloudinitdisk"},
        ]

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
                                "sockets": 1,
                                "threads": 1,
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
                            "features": {"kvm": {"hidden": False}},
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

        # Create NodePort services for SSH (temporary) and troshkad (persistent)
        for svc_name, port, target_port in [
            (f"troshka-ssh-{host_id[:8]}", 22, 22),
            (f"troshka-agent-{host_id[:8]}", 31337, 31337),
        ]:
            svc = client.V1Service(
                metadata=client.V1ObjectMeta(
                    name=svc_name,
                    namespace=namespace,
                    labels={"app": "troshka", "troshka/host-id": host_id},
                ),
                spec=client.V1ServiceSpec(
                    type="NodePort",
                    selector={"kubevirt.io/domain": hostname},
                    ports=[client.V1ServicePort(port=port, target_port=target_port)],
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

        # Get NodePort numbers for SSH and agent access
        ssh_svc = core_api.read_namespaced_service(
            f"troshka-ssh-{host_id[:8]}", namespace
        )
        ssh_nodeport = ssh_svc.spec.ports[0].node_port

        agent_svc = core_api.read_namespaced_service(
            f"troshka-agent-{host_id[:8]}", namespace
        )
        agent_nodeport = agent_svc.spec.ports[0].node_port

        # Get a worker node IP for NodePort access
        nodes = core_api.list_node()
        node_ip = None
        for node in nodes.items:
            for addr in node.status.addresses:
                if addr.type == "InternalIP":
                    node_ip = addr.address
                    break
            if node_ip:
                break

        return {
            "host_id": host_id,
            "instance_id": hostname,
            "instance_type": instance_type or f"{cores}c-{memory_gi}g",
            "public_ip": f"{node_ip}:{agent_nodeport}" if node_ip else None,
            "private_ip": pod_ip,
            "total_vcpus": cores,
            "total_ram_mb": memory_gi * 1024,
            "key_pair_name": None,
            "private_key": private_key,
            "storage_size_gb": storage_size_gb,
            "max_eips": 0,
            "_ssh_host": node_ip,
            "_ssh_port": ssh_nodeport,
        }

    def terminate_host(self, provider, instance_id):
        from kubernetes import client

        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, core_api = _get_k8s_clients(creds)

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
            "troshka-ssh-",
            "troshka-agent-",
            "troshka-vncd-",
        ]:
            try:
                core_api.delete_namespaced_service(f"{prefix}{host_short}", namespace)
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
