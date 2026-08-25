"""OCP Virt (KubeVirt) provider driver.

Creates large nested-virt RHEL VMs on OpenShift Virtualization.
The VMs run troshkad identically to EC2 instances.
"""

import logging
import time
from typing import Any, cast

from app.services.providers.base import ProviderDriver

logger = logging.getLogger(__name__)

_KUBEVIRT_GROUP = "kubevirt.io"
_KUBEVIRT_DOMAIN_LABEL = "kubevirt.io/domain"
_ROUTE_API = "route.openshift.io"
_HOST_ID_LABEL = "troshka/host-id"
SSH_LB_PORT = 22000

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
{repo_write_file}runcmd:
{repo_setup}
  - dnf install -y qemu-kvm libvirt libvirt-client virt-install python3 python3-libvirt dnsmasq nftables xorriso nmap-ncat sshpass nfs-utils || true
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
{repo_write_file}runcmd:
{repo_setup}
  - dnf install -y python3 qemu-img nfs-utils || true
  - mkdir -p /var/lib/troshka /etc/troshka-agent
  - 'echo "host_id: {host_id}" > /etc/troshka-agent/host-id'
"""

# Repo-setup runcmd item. Default: mount the RHEL DVD ISO as a local repo.
_REPO_SETUP_DVD = """  - |
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
    fi"""

# HTTP repo: write the .repo file via cloud-init (not a shell heredoc — indented
# REPOEOF delimiters break bash and swallow the rest of runcmd).
_REPO_WRITE_FILE_HTTP = """  - path: /etc/yum.repos.d/troshka-rhel.repo
    permissions: '0644'
    content: |
      [troshka-baseos]
      name=Troshka RHEL BaseOS
      baseurl={repo_url}/BaseOS
      enabled=1
      gpgcheck=0
      sslverify=0
      username={repo_user}
      password={repo_pass}
      [troshka-appstream]
      name=Troshka RHEL AppStream
      baseurl={repo_url}/AppStream
      enabled=1
      gpgcheck=0
      sslverify=0
      username={repo_user}
      password={repo_pass}
"""


def _resolve_pkg_repo_creds(creds: dict[str, Any]) -> tuple[str, str, str]:
    from app.core.ocpvirt_pkg_repo import resolve_pkg_repo

    return resolve_pkg_repo(creds)


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


def _build_cloud_init_userdata(
    host_type,
    public_key,
    host_id,
    nfs_server=None,
    nfs_path=None,
    nfs_port=None,
    repo_url="",
    repo_user="",
    repo_pass="",
):
    """Build cloud-init userdata string for a host VM.

    When repo_url is set, packages come from the central-S4 HTTP repo (basic
    auth) instead of a mounted DVD ISO.
    """
    template = (
        CLOUD_INIT_PATTERN_BUFFER
        if host_type == "pattern_buffer"
        else CLOUD_INIT_TEMPLATE
    )
    if repo_url:
        repo_write_file = _REPO_WRITE_FILE_HTTP.format(
            repo_url=repo_url, repo_user=repo_user, repo_pass=repo_pass
        )
        repo_setup = ""
    else:
        repo_write_file = ""
        repo_setup = _REPO_SETUP_DVD
    user_data = template.format(
        ssh_pubkey=public_key,
        host_id=host_id,
        repo_setup=repo_setup,
        repo_write_file=repo_write_file,
    )

    if nfs_server and nfs_path:
        mount_opts = "nfsvers=4.1,nconnect=16,hard,_netdev"
        if nfs_port:
            mount_opts = f"port={nfs_port},{mount_opts}"
        user_data = user_data.rstrip() + (
            f"\n  - mkdir -p /var/lib/troshka/shared"
            f"\n  - 'echo \"{nfs_server}:{nfs_path} /var/lib/troshka/shared nfs "
            f"{mount_opts} 0 0\" >> /etc/fstab'"
            f"\n  - mount /var/lib/troshka/shared"
            f"\n  - setsebool -P virt_use_nfs 1"
            f"\n"
        )

    return user_data


def _build_root_source(rhel_image_url, datasource_name):
    """Return the root disk source spec for a VM DataVolume."""
    if rhel_image_url:
        return {"source": {"http": {"url": rhel_image_url}}}
    return {
        "sourceRef": {
            "kind": "DataSource",
            "name": datasource_name,
            "namespace": "openshift-virtualization-os-images",
        }
    }


def _build_vm_disks_and_volumes(
    hostname, storage_size_gb, host_type, iso_pvc, root_source
):
    """Build data_volumes, disks, and volumes lists for a VM spec."""
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

    disks = [
        {"disk": {"bus": "virtio"}, "name": "rootdisk"},
        {"disk": {"bus": "virtio"}, "name": "datadisk"},
        {"disk": {"bus": "virtio"}, "name": "cloudinitdisk"},
    ]
    volumes = [
        {"dataVolume": {"name": f"{hostname}-root"}, "name": "rootdisk"},
        {"dataVolume": {"name": f"{hostname}-data"}, "name": "datadisk"},
        {
            "cloudInitNoCloud": {
                "secretRef": {"name": f"{hostname}-userdata"},
            },
            "name": "cloudinitdisk",
        },
    ]

    if iso_pvc:
        disks.insert(
            2, {"cdrom": {"bus": "sata", "readonly": True}, "name": "installiso"}
        )
        volumes.insert(
            2,
            {
                "persistentVolumeClaim": {"claimName": iso_pvc},
                "name": "installiso",
            },
        )

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

    return data_volumes, disks, volumes


def _wait_for_vmi_running(custom_api, namespace, hostname):
    """Poll VMI until Running state. Returns pod_ip or raises RuntimeError."""
    from kubernetes import client

    for _ in range(120):
        time.sleep(5)
        try:
            vmi = cast(
                dict[str, Any],
                custom_api.get_namespaced_custom_object(
                    group=_KUBEVIRT_GROUP,
                    version="v1",
                    namespace=namespace,
                    plural="virtualmachineinstances",
                    name=hostname,
                ),
            )
            phase = vmi.get("status", {}).get("phase")
            if phase == "Running":
                interfaces = vmi.get("status", {}).get("interfaces", [])
                if interfaces:
                    return interfaces[0].get("ipAddress")
                return None
        except client.ApiException:
            pass
    raise RuntimeError(f"VM {hostname} did not reach Running state within 10 minutes")


def _wait_for_lb_ip(core_api, svc_name, namespace):
    """Poll LoadBalancer service for external IP. Returns IP or None."""
    for _ in range(60):
        lb_svc = cast(
            Any,
            core_api.read_namespaced_service(svc_name, namespace),
        )
        lb_status = lb_svc.status.load_balancer
        ingress = lb_status.ingress if lb_status else None
        if ingress and ingress[0].ip:
            return ingress[0].ip
        time.sleep(2)
    return None


def _cleanup_host_k8s_resources(custom_api, core_api, namespace, instance_id):
    """Delete services, secrets, and routes associated with a host."""
    from kubernetes import client

    host_short = instance_id.replace("troshka-host-", "")

    for prefix in ["troshka-lb-", "troshka-vncd-"]:
        try:
            core_api.delete_namespaced_service(f"{prefix}{host_short}", namespace)
        except client.ApiException:
            pass

    try:
        eip_svcs = core_api.list_namespaced_service(
            namespace,
            label_selector=f"troshka/host-id={host_short}",
        )
        for svc in cast(Any, eip_svcs).items:
            if svc.metadata.name.startswith("troshka-eip-"):
                try:
                    core_api.delete_namespaced_service(svc.metadata.name, namespace)
                except client.ApiException:
                    pass
    except client.ApiException:
        pass

    try:
        core_api.delete_namespaced_secret(f"{instance_id}-userdata", namespace)
    except client.ApiException:
        pass

    try:
        custom_api.delete_namespaced_custom_object(
            group=_ROUTE_API,
            version="v1",
            namespace=namespace,
            plural="routes",
            name=f"troshka-console-{host_short}",
        )
    except client.ApiException:
        pass


def _setup_route_dnat(host, project_id, transit_port, int_ip, vm_port):
    """Set up nftables DNAT rule on the host for route access."""
    from app.services.troshkad_client import start_job, wait_for_job

    ns_name = f"troshka-{project_id[:8]}"
    try:
        job_id = start_job(
            host,
            "/networks/add-dnat",
            {
                "namespace": ns_name,
                "transit_port": transit_port,
                "dst_ip": int_ip,
                "dst_port": vm_port,
            },
        )
        wait_for_job(host, job_id, timeout=30)
        logger.info(
            "Route DNAT: %d → %s:%d in %s",
            transit_port,
            int_ip,
            vm_port,
            ns_name,
        )
    except Exception:
        logger.warning(
            "Route DNAT setup failed for %s:%d (transit %d), continuing",
            int_ip,
            vm_port,
            transit_port,
            exc_info=True,
        )


def _create_or_get_route(custom_api, namespace, route_body, resource_name):
    """Create an OCP Route, returning the hostname.

    On 409 conflict, merge-patches spec (TLS/port/target) then returns hostname.
    """
    from kubernetes import client

    try:
        result = cast(
            dict[str, Any],
            custom_api.create_namespaced_custom_object(
                group=_ROUTE_API,
                version="v1",
                namespace=namespace,
                plural="routes",
                body=route_body,
            ),
        )
        return result.get("spec", {}).get("host", "")
    except client.ApiException as e:
        if e.status == 409:
            try:
                route_spec = route_body.get("spec", {})
                patch_body = {
                    "spec": {
                        key: route_spec[key]
                        for key in ("to", "port", "tls")
                        if route_spec.get(key) is not None
                    }
                }
                if patch_body["spec"]:
                    custom_api.patch_namespaced_custom_object(
                        group=_ROUTE_API,
                        version="v1",
                        namespace=namespace,
                        plural="routes",
                        name=resource_name,
                        body=patch_body,
                        _content_type="application/merge-patch+json",
                    )
                existing = cast(
                    dict[str, Any],
                    custom_api.get_namespaced_custom_object(
                        group=_ROUTE_API,
                        version="v1",
                        namespace=namespace,
                        plural="routes",
                        name=resource_name,
                    ),
                )
                return existing.get("spec", {}).get("host", "")
            except client.ApiException:
                return ""
        raise


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

        repo_url, repo_user, repo_pass = _resolve_pkg_repo_creds(creds)
        user_data = _build_cloud_init_userdata(
            host_type,
            public_key,
            host_id,
            nfs_server=kwargs.get("nfs_server"),
            nfs_path=kwargs.get("nfs_path"),
            nfs_port=kwargs.get("nfs_port"),
            repo_url=repo_url,
            repo_user=repo_user,
            repo_pass=repo_pass,
        )

        rhel_image_url = kwargs.get("rhel_image_url", "")
        datasource_name = kwargs.get("image_id") or "rhel9"
        root_source = _build_root_source(rhel_image_url, datasource_name)

        # With the HTTP package repo, the host installs packages over the network,
        # so no DVD ISO boot volume (CDROM) is attached.
        if repo_url:
            iso_pvc = ""
        else:
            iso_pvc = kwargs.get("iso_pvc", creds.get("iso_pvc", "rhel-10.2-dvd-iso"))
        data_volumes, disks, volumes = _build_vm_disks_and_volumes(
            hostname, storage_size_gb, host_type, iso_pvc, root_source
        )

        # Create Secret with cloud-init userdata (KubeVirt enforces 2KB inline limit)
        import base64

        secret_body = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=f"{hostname}-userdata",
                namespace=namespace,
                labels={"app": "troshka", _HOST_ID_LABEL: host_id},
            ),
            data={
                "userdata": base64.b64encode(user_data.encode()).decode(),
            },
        )
        core_api.create_namespaced_secret(namespace=namespace, body=secret_body)

        vm_manifest = {
            "apiVersion": "kubevirt.io/v1",
            "kind": "VirtualMachine",
            "metadata": {
                "name": hostname,
                "namespace": namespace,
                "labels": {
                    "app": "troshka",
                    _HOST_ID_LABEL: host_id,
                    "troshka/host-type": host_type,
                },
            },
            "spec": {
                "running": True,
                "dataVolumeTemplates": data_volumes,
                "template": {
                    "metadata": {
                        "labels": {
                            _KUBEVIRT_DOMAIN_LABEL: hostname,
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
            group=_KUBEVIRT_GROUP,
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
                labels={"app": "troshka", _HOST_ID_LABEL: host_id},
            ),
            spec=client.V1ServiceSpec(
                type="LoadBalancer",
                selector={_KUBEVIRT_DOMAIN_LABEL: hostname},
                ports=[
                    client.V1ServicePort(
                        name="ssh", port=SSH_LB_PORT, target_port=22, protocol="TCP"
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

        pod_ip = _wait_for_vmi_running(custom_api, namespace, hostname)

        external_ip = _wait_for_lb_ip(core_api, f"troshka-lb-{host_id[:8]}", namespace)
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
            "_ssh_port": SSH_LB_PORT,
        }

    def terminate_host(self, provider, instance_id):
        from kubernetes import client

        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, core_api = _get_k8s_clients(creds)

        # Force stop: set running=false, then force-delete VMI (like virtctl stop --force)
        try:
            custom_api.patch_namespaced_custom_object(
                group=_KUBEVIRT_GROUP,
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
                group=_KUBEVIRT_GROUP,
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
                group=_KUBEVIRT_GROUP,
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
                name=instance_id,
            )
        except client.ApiException as e:
            if e.status != 404:
                raise

        _cleanup_host_k8s_resources(custom_api, core_api, namespace, instance_id)
        logger.info("Terminated OCP Virt host %s", instance_id)

    def get_host_status(self, provider, instance_id):
        from kubernetes import client

        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, _ = _get_k8s_clients(creds)

        try:
            vmi = cast(
                dict[str, Any],
                custom_api.get_namespaced_custom_object(
                    group=_KUBEVIRT_GROUP,
                    version="v1",
                    namespace=namespace,
                    plural="virtualmachineinstances",
                    name=instance_id,
                ),
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
                selector={_KUBEVIRT_DOMAIN_LABEL: host.instance_id},
                ports=[client.V1ServicePort(port=8080, target_port=8080)],
            ),
        )
        try:
            core_api.create_namespaced_service(namespace=namespace, body=svc)
        except client.ApiException as e:
            if e.status != 409:
                raise

        # Create edge-terminated Route (omit spec.host — OCP auto-generates
        # {route-name}-{namespace}.{apps-domain} from the wildcard cert)
        route_name = f"troshka-console-{host_short}"
        route = {
            "apiVersion": "route.openshift.io/v1",
            "kind": "Route",
            "metadata": {
                "name": route_name,
                "namespace": namespace,
                "annotations": {
                    "haproxy.router.openshift.io/timeout": "3600s",
                },
            },
            "spec": {
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
            result = custom_api.create_namespaced_custom_object(
                group=_ROUTE_API,
                version="v1",
                namespace=namespace,
                plural="routes",
                body=route,
            )
            return cast(dict[str, Any], result).get("spec", {}).get("host", "")
        except client.ApiException as e:
            if e.status == 409:
                return f"{route_name}-{namespace}.{hostname}"
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
                group=_ROUTE_API,
                version="v1",
                namespace=namespace,
                plural="routes",
                name=f"troshka-console-{host_short}",
            )
        except client.ApiException:
            pass

    def create_route_access(
        self,
        provider,
        host,
        project_id,
        vm_name,
        int_ip,
        port,
        target_port=None,
        *,
        transit_port=None,
        setup_dnat=True,
    ):
        """Create a ClusterIP Service + OCP Route for external access to a VM port.

        Uses a host transit port (40000+, same as EIP forwards) as the Service
        targetPort so KubeVirt forwards traffic into the guest where nftables
        DNAT reaches the VM. Reuse the EIP transit port when available.

        Returns dict with hostname, route_name, service_name, transit_port.
        """
        import re

        from kubernetes import client

        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, core_api = _get_k8s_clients(creds)

        vm_port = target_port or port
        if transit_port is None:
            raise ValueError("transit_port is required for OCP Virt route access")

        safe_name = re.sub(r"[^a-z0-9-]", "-", vm_name.lower())[:20]
        resource_name = f"troshka-pf-{project_id[:8]}-{safe_name}-{port}"

        labels = {
            "app": "troshka",
            "troshka/project-id": project_id[:8],
            "troshka/access-type": "route",
        }

        if setup_dnat:
            _setup_route_dnat(host, project_id, transit_port, int_ip, vm_port)

        # ClusterIP → virt-launcher pod:transit_port (KubeVirt forwards to guest)
        svc = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=resource_name,
                namespace=namespace,
                labels=labels,
            ),
            spec=client.V1ServiceSpec(
                type="ClusterIP",
                selector={_KUBEVIRT_DOMAIN_LABEL: host.instance_id},
                ports=[
                    client.V1ServicePort(
                        port=port,
                        target_port=transit_port,
                        name=f"pf-{port}",
                    )
                ],
            ),
        )
        try:
            core_api.create_namespaced_service(namespace=namespace, body=svc)
        except client.ApiException as e:
            if e.status == 409:
                core_api.patch_namespaced_service(
                    resource_name,
                    namespace,
                    {
                        "spec": {
                            "type": "ClusterIP",
                            "selector": {_KUBEVIRT_DOMAIN_LABEL: host.instance_id},
                            "ports": [
                                {
                                    "port": port,
                                    "targetPort": transit_port,
                                    "name": f"pf-{port}",
                                }
                            ],
                        }
                    },
                    _content_type="application/merge-patch+json",
                )
            else:
                raise

        # Passthrough only when the guest actually speaks TLS on the target port
        # (e.g. API 6443, or 443→443). Showroom-style 443→80 HTTP needs edge
        # termination so the router presents the cluster wildcard cert.
        passthrough = port == 6443 or (port == 443 and vm_port == 443)
        route = {
            "apiVersion": "route.openshift.io/v1",
            "kind": "Route",
            "metadata": {
                "name": resource_name,
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "to": {"kind": "Service", "name": resource_name},
                "port": {"targetPort": f"pf-{port}"},
                "tls": (
                    {"termination": "passthrough"}
                    if passthrough
                    else {
                        "termination": "edge",
                        "insecureEdgeTerminationPolicy": "Redirect",
                    }
                ),
            },
        }
        hostname = _create_or_get_route(custom_api, namespace, route, resource_name)

        logger.info(
            "Created Route %s → %s:%d (host: %s)", resource_name, int_ip, port, hostname
        )
        return {
            "hostname": hostname,
            "route_name": resource_name,
            "service_name": resource_name,
            "transit_port": transit_port,
        }

    def delete_route_access(self, provider, project_id, namespace=None):
        """Delete all Route and Service resources created for a project's external access."""
        from kubernetes import client

        creds = provider.get_credentials()
        namespace = namespace or creds.get("namespace", "troshka")
        custom_api, core_api = _get_k8s_clients(creds)

        label_selector = (
            f"troshka/project-id={project_id[:8]},troshka/access-type=route"
        )

        try:
            svcs = cast(
                Any,
                core_api.list_namespaced_service(
                    namespace, label_selector=label_selector
                ),
            )
            for svc in svcs.items:
                try:
                    core_api.delete_namespaced_service(svc.metadata.name, namespace)
                    logger.info("Deleted Route access Service %s", svc.metadata.name)
                except client.ApiException:
                    pass
        except client.ApiException:
            pass

        try:
            routes = cast(
                dict[str, Any],
                custom_api.list_namespaced_custom_object(
                    group=_ROUTE_API,
                    version="v1",
                    namespace=namespace,
                    plural="routes",
                    label_selector=label_selector,
                ),
            )
            for route in routes.get("items", []):
                try:
                    custom_api.delete_namespaced_custom_object(
                        group=_ROUTE_API,
                        version="v1",
                        namespace=namespace,
                        plural="routes",
                        name=route["metadata"]["name"],
                    )
                    logger.info(
                        "Deleted Route access Route %s", route["metadata"]["name"]
                    )
                except client.ApiException:
                    pass
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
            group=_KUBEVIRT_GROUP,
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
            group=_KUBEVIRT_GROUP,
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=instance_id,
            body={"spec": {"running": False}},
        )

    def detach_iso(self, provider, instance_id):
        """Remove the install ISO cdrom from the VM spec.

        Takes effect on next VM restart — the running VM is not affected.
        This unblocks live migration (ISO PVC is RWO/Filesystem which
        prevents KubeVirt live migration on Ceph RBD).
        """
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, _ = _get_k8s_clients(creds)

        vm = cast(
            dict[str, Any],
            custom_api.get_namespaced_custom_object(
                group=_KUBEVIRT_GROUP,
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
                name=instance_id,
            ),
        )

        spec = vm.get("spec", {}).get("template", {}).get("spec", {})
        disks = spec.get("domain", {}).get("devices", {}).get("disks", [])
        volumes = spec.get("volumes", [])

        new_disks = [d for d in disks if d.get("name") != "installiso"]
        new_volumes = [v for v in volumes if v.get("name") != "installiso"]

        if len(new_disks) == len(disks):
            return

        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {"devices": {"disks": new_disks}},
                        "volumes": new_volumes,
                    }
                }
            }
        }
        custom_api.patch_namespaced_custom_object(
            group=_KUBEVIRT_GROUP,
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=instance_id,
            body=patch,
        )

    def allocate_eip(self, provider, host, eip_id, project_id=None):
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
                    _HOST_ID_LABEL: host.instance_id.replace("troshka-host-", ""),
                },
            ),
            spec=client.V1ServiceSpec(
                type="LoadBalancer",
                selector={_KUBEVIRT_DOMAIN_LABEL: host.instance_id},
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
            lb_svc = cast(Any, core_api.read_namespaced_service(svc_name, namespace))
            lb_status = lb_svc.status.load_balancer
            ingress = lb_status.ingress if lb_status else None
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

    def update_eip_ports(self, provider, host, allocation_id, ports, namespace=None):
        creds = provider.get_credentials()
        namespace = namespace or creds.get("namespace", "troshka")
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
            allocation_id,
            namespace,
            {"spec": {"ports": svc_ports}},
            _content_type="application/merge-patch+json",
        )
        logger.info(
            "Updated EIP %s ports: %s",
            allocation_id,
            [p["port"] for p in ports],
        )
