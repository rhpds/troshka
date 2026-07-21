import datetime
import logging
import threading
from typing import Any

import boto3

from app.core.database import SessionLocal
from app.models.storage_pool import StoragePool

logger = logging.getLogger(__name__)


def generate_pool_ca(pool_name: str) -> tuple[str, str]:
    """Generate a self-signed CA cert+key for a storage pool. Returns (cert_pem, key_pem)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, f"troshka-pool-{pool_name}"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Troshka"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC))
        .not_valid_after(
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=3650)
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem


def sign_host_cert(
    ca_cert_pem: str, ca_key_pem: str, host_ip: str, private_ip: str = ""
) -> tuple[str, str]:
    """Generate a host cert+key signed by the pool CA. Returns (cert_pem, key_pem)."""
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem.encode())
    ca_key = serialization.load_pem_private_key(ca_key_pem.encode(), password=None)

    host_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san_ips = [x509.IPAddress(ipaddress.ip_address(host_ip))]
    if private_ip and private_ip != host_ip:
        san_ips.append(x509.IPAddress(ipaddress.ip_address(private_ip)))
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, host_ip),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Troshka"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(host_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC))
        .not_valid_after(
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365)
        )
        .add_extension(
            x509.SubjectAlternativeName(san_ips),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())  # type: ignore[arg-type]
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = host_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem


def _boto_client(service: str, region: str, credentials: dict):
    return boto3.client(  # type: ignore[call-overload]
        service,
        region_name=region,
        aws_access_key_id=credentials.get("access_key_id"),
        aws_secret_access_key=credentials.get("secret_access_key"),
    )


def probe_az_capacity(
    credentials: dict, region: str, instance_types: list[str]
) -> dict:
    ec2 = _boto_client("ec2", region, credentials)
    results: dict[str, dict[str, list]] = {}

    for itype in instance_types:
        resp = ec2.describe_instance_type_offerings(
            LocationType="availability-zone",
            Filters=[{"Name": "instance-type", "Values": [itype]}],
        )
        supported_azs = {o["Location"] for o in resp["InstanceTypeOfferings"]}

        all_azs = ec2.describe_availability_zones(
            Filters=[{"Name": "state", "Values": ["available"]}]
        )
        for az_info in all_azs["AvailabilityZones"]:
            az = az_info["ZoneName"]
            if az not in results:
                results[az] = {"supported": [], "unsupported": []}
            if az in supported_azs:
                results[az]["supported"].append(itype)
            else:
                results[az]["unsupported"].append(itype)

    return results


def find_best_az(az_results: dict, instance_types: list[str]) -> str | None:
    for az, data in sorted(az_results.items()):
        if len(data["supported"]) == len(instance_types):
            return az
    return None


def ensure_subnet_in_az(credentials: dict, region: str, vpc_id: str, az: str) -> str:
    ec2 = _boto_client("ec2", region, credentials)

    existing = ec2.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "availability-zone", "Values": [az]},
            {"Name": "tag:ManagedBy", "Values": ["troshka"]},
        ]
    )
    if existing["Subnets"]:
        return existing["Subnets"][0]["SubnetId"]

    all_subnets = ec2.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "tag:ManagedBy", "Values": ["troshka"]},
        ]
    )
    used_thirds = set()
    for s in all_subnets["Subnets"]:
        parts = s["CidrBlock"].split(".")
        used_thirds.add(int(parts[2]))

    third_octet = 1
    while third_octet in used_thirds:
        third_octet += 1
    cidr = f"10.100.{third_octet}.0/24"

    subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=az)
    subnet_id = subnet["Subnet"]["SubnetId"]
    ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})
    ec2.create_tags(
        Resources=[subnet_id],
        Tags=[
            {"Key": "Name", "Value": f"troshka-{az}"},
            {"Key": "ManagedBy", "Value": "troshka"},
        ],
    )

    vpc_data = ec2.describe_route_tables(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "tag:ManagedBy", "Values": ["troshka"]},
        ]
    )
    if vpc_data["RouteTables"]:
        ec2.associate_route_table(
            RouteTableId=vpc_data["RouteTables"][0]["RouteTableId"],
            SubnetId=subnet_id,
        )

    return subnet_id


def create_fsx_filesystem(
    credentials: dict,
    region: str,
    subnet_id: str,
    security_group_id: str,
    storage_gb: int,
    throughput_mbps: int,
) -> dict:
    fsx = _boto_client("fsx", region, credentials)

    resp = fsx.create_file_system(
        FileSystemType="OPENZFS",
        StorageCapacity=storage_gb,
        SubnetIds=[subnet_id],
        SecurityGroupIds=[security_group_id],
        Tags=[
            {"Key": "Name", "Value": "troshka-shared-storage"},
            {"Key": "ManagedBy", "Value": "troshka"},
        ],
        OpenZFSConfiguration={
            "DeploymentType": "SINGLE_AZ_2",
            "ThroughputCapacity": throughput_mbps,
            "RootVolumeConfiguration": {
                "DataCompressionType": "LZ4",
                "NfsExports": [
                    {
                        "ClientConfigurations": [
                            {
                                "Clients": "*",
                                "Options": ["rw", "no_root_squash", "sync", "crossmnt"],
                            }
                        ]
                    }
                ],
            },
        },
    )

    return {
        "filesystem_id": resp["FileSystem"]["FileSystemId"],
        "dns_name": resp["FileSystem"].get("DNSName"),
    }


def _poll_fsx_until_available(
    pool_id: str, credentials: dict, region: str, filesystem_id: str
):
    import time

    fsx = _boto_client("fsx", region, credentials)
    db = SessionLocal()
    try:
        for _ in range(120):
            time.sleep(10)
            resp = fsx.describe_file_systems(FileSystemIds=[filesystem_id])
            fs = resp["FileSystems"][0]
            status = fs["Lifecycle"]
            if status == "AVAILABLE":
                pool = db.query(StoragePool).get(pool_id)
                if not pool:
                    return
                pool.status = "available"
                pool.fsx_dns_name = fs.get("DNSName")
                if fs.get("NetworkInterfaceIds"):
                    enis = _boto_client("ec2", region, credentials)
                    eni_resp = enis.describe_network_interfaces(
                        NetworkInterfaceIds=fs["NetworkInterfaceIds"][:1]
                    )
                    if eni_resp["NetworkInterfaces"]:
                        pool.fsx_mount_ip = eni_resp["NetworkInterfaces"][0][
                            "PrivateIpAddress"
                        ]
                db.commit()
                logger.info("FSx %s is available for pool %s", filesystem_id, pool_id)
                return
            elif status in ("FAILED", "DELETING"):
                pool = db.query(StoragePool).get(pool_id)
                if not pool:
                    return
                pool.status = "error"
                db.commit()
                logger.error(
                    "FSx %s failed for pool %s: %s", filesystem_id, pool_id, status
                )
                return

        pool = db.query(StoragePool).get(pool_id)
        if not pool:
            return
        pool.status = "error"
        db.commit()
        logger.error("FSx %s timed out for pool %s", filesystem_id, pool_id)
    finally:
        db.close()


def provision_fsx_pool(
    pool_id: str,
    credentials: dict,
    region: str,
    subnet_id: str,
    security_group_id: str,
    storage_gb: int,
    throughput_mbps: int,
):
    db = SessionLocal()
    try:
        result = create_fsx_filesystem(
            credentials,
            region,
            subnet_id,
            security_group_id,
            storage_gb,
            throughput_mbps,
        )
        pool = db.query(StoragePool).get(pool_id)
        if not pool:
            return
        pool.fsx_filesystem_id = result["filesystem_id"]
        pool.fsx_dns_name = result.get("dns_name")
        db.commit()
    except Exception as e:
        logger.error("FSx provisioning failed for pool %s: %s", pool_id[:8], e)
        pool = db.query(StoragePool).get(pool_id)
        if not pool:
            return
        pool.status = "error"
        db.commit()
        return
    finally:
        db.close()

    t = threading.Thread(
        target=_poll_fsx_until_available,
        args=(pool_id, credentials, region, result["filesystem_id"]),
        daemon=True,
    )
    t.start()


def delete_fsx_filesystem(credentials: dict, region: str, filesystem_id: str):
    fsx = _boto_client("fsx", region, credentials)
    fsx.delete_file_system(FileSystemId=filesystem_id)


def update_fsx_throughput(
    credentials: dict, region: str, filesystem_id: str, throughput_mbps: int
):
    fsx = _boto_client("fsx", region, credentials)
    fsx.update_file_system(
        FileSystemId=filesystem_id,
        OpenZFSConfiguration={"ThroughputCapacity": throughput_mbps},
    )


def update_fsx_storage(
    credentials: dict, region: str, filesystem_id: str, storage_gb: int
):
    fsx = _boto_client("fsx", region, credentials)
    fsx.update_file_system(
        FileSystemId=filesystem_id,
        StorageCapacity=storage_gb,
    )


def add_sg_rules_for_shared_storage(
    credentials: dict, region: str, security_group_id: str, include_nfs: bool = True
):
    """Add SG rules for shared storage. NFS rule only needed for FSx (managed by us), not BYO."""
    ec2 = _boto_client("ec2", region, credentials)

    existing = ec2.describe_security_group_rules(
        Filters=[{"Name": "group-id", "Values": [security_group_id]}]
    )
    existing_ports = {
        r.get("FromPort")
        for r in existing["SecurityGroupRules"]
        if r["IsEgress"] is False
    }

    rules_to_add = []
    if include_nfs and 2049 not in existing_ports:
        rules_to_add.append(
            {
                "IpProtocol": "tcp",
                "FromPort": 2049,
                "ToPort": 2049,
                "UserIdGroupPairs": [{"GroupId": security_group_id}],
            }
        )
    if 16514 not in existing_ports:
        rules_to_add.append(
            {
                "IpProtocol": "tcp",
                "FromPort": 16514,
                "ToPort": 16514,
                "UserIdGroupPairs": [{"GroupId": security_group_id}],
            }
        )
    if 49152 not in existing_ports:
        rules_to_add.append(
            {
                "IpProtocol": "tcp",
                "FromPort": 49152,
                "ToPort": 49215,
                "UserIdGroupPairs": [{"GroupId": security_group_id}],
            }
        )
    if 10809 not in existing_ports:
        rules_to_add.append(
            {
                "IpProtocol": "tcp",
                "FromPort": 10809,
                "ToPort": 10829,
                "UserIdGroupPairs": [{"GroupId": security_group_id}],
            }
        )

    if rules_to_add:
        ec2.authorize_security_group_ingress(
            GroupId=security_group_id,
            IpPermissions=rules_to_add,
        )
        logger.info("Added NFS/migration SG rules to %s", security_group_id)


# ---------------------------------------------------------------------------
# Ceph-NFS provisioning (OCP Virt)
# ---------------------------------------------------------------------------

CEPH_FS_NAME = "ocs-storagecluster-cephfilesystem"
CEPH_NFS_CLUSTER = "ocs-storagecluster-cephnfs"
CEPH_NFS_POD_SELECTOR = "app=rook-ceph-tools"
CEPH_NFS_SVC_SELECTOR = {
    "app": "rook-ceph-nfs",
    "app.kubernetes.io/instance": f"{CEPH_NFS_CLUSTER}-a",
}
ODF_NAMESPACE = "openshift-storage"


def _get_k8s_clients(credentials):
    from kubernetes import client

    configuration = client.Configuration()
    configuration.host = credentials["api_url"]
    configuration.api_key = {"authorization": f"Bearer {credentials['token']}"}
    configuration.verify_ssl = credentials.get("verify_ssl", False)
    api_client = client.ApiClient(configuration)
    core_api = client.CoreV1Api(api_client)
    return core_api, api_client


def _find_toolbox_pod(core_api) -> str:
    pods = core_api.list_namespaced_pod(
        ODF_NAMESPACE, label_selector=CEPH_NFS_POD_SELECTOR
    )
    for pod in pods.items:
        if pod.status.phase == "Running":
            return pod.metadata.name
    raise RuntimeError("No running Rook toolbox pod found in openshift-storage")


def _ceph_exec(core_api, toolbox_pod: str, command: list[str]) -> str:
    from kubernetes import stream

    resp = stream.stream(
        core_api.connect_get_namespaced_pod_exec,
        toolbox_pod,
        ODF_NAMESPACE,
        command=command,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )
    return resp.strip()


def provision_ceph_nfs_pool(pool_id: str, credentials: dict):
    db = SessionLocal()
    try:
        pool = db.query(StoragePool).get(pool_id)
        if not pool:
            return

        short_id = pool_id[:8]
        group_name = f"troshka-pool-{short_id}"
        vol_name = f"troshka-{short_id}"
        pseudo_path = f"/troshka-{short_id}"
        quota_bytes = (pool.fsx_storage_gb or 500) * 1073741824

        core_api, api_client = _get_k8s_clients(credentials)
        toolbox = _find_toolbox_pod(core_api)

        _ceph_exec(
            core_api,
            toolbox,
            [
                "ceph",
                "fs",
                "subvolumegroup",
                "create",
                CEPH_FS_NAME,
                group_name,
            ],
        )
        logger.info("Ceph subvolumegroup %s created", group_name)

        _ceph_exec(
            core_api,
            toolbox,
            [
                "ceph",
                "fs",
                "subvolume",
                "create",
                CEPH_FS_NAME,
                vol_name,
                group_name,
                f"--size={quota_bytes}",
            ],
        )
        logger.info(
            "Ceph subvolume %s created (%d GB)", vol_name, quota_bytes // 1073741824
        )

        subvol_path = _ceph_exec(
            core_api,
            toolbox,
            [
                "ceph",
                "fs",
                "subvolume",
                "getpath",
                CEPH_FS_NAME,
                vol_name,
                group_name,
            ],
        )
        logger.info("Ceph subvolume path: %s", subvol_path)

        _ceph_exec(
            core_api,
            toolbox,
            [
                "ceph",
                "nfs",
                "export",
                "create",
                "cephfs",
                CEPH_NFS_CLUSTER,
                pseudo_path,
                CEPH_FS_NAME,
                f"--path={subvol_path}",
                "--squash=no_root_squash",
            ],
        )
        logger.info("Ceph NFS export %s created with no_root_squash", pseudo_path)

        from kubernetes import client

        svc_name = f"troshka-nfs-{short_id}"
        svc = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=svc_name,
                namespace=ODF_NAMESPACE,
                labels={"app": "troshka", "troshka/pool-id": pool_id},
            ),
            spec=client.V1ServiceSpec(
                type="NodePort",
                selector=CEPH_NFS_SVC_SELECTOR,
                ports=[
                    client.V1ServicePort(
                        name="nfs", port=2049, target_port=2049, protocol="TCP"
                    )
                ],
            ),
        )
        created_svc: Any = core_api.create_namespaced_service(ODF_NAMESPACE, body=svc)
        node_port = created_svc.spec.ports[0].node_port
        logger.info("NodePort service %s created, port %d", svc_name, node_port)

        nfs_pod: Any = core_api.list_namespaced_pod(
            ODF_NAMESPACE,
            label_selector=",".join(
                f"{k}={v}" for k, v in CEPH_NFS_SVC_SELECTOR.items()
            ),
        )
        nfs_node_name = nfs_pod.items[0].spec.node_name if nfs_pod.items else None
        node_ip = None
        if nfs_node_name:
            node: Any = core_api.read_node(nfs_node_name)
            for addr in node.status.addresses:
                if addr.type == "InternalIP":
                    node_ip = addr.address
                    break
        if not node_ip:
            nodes: Any = core_api.list_node()
            for n in nodes.items:
                for addr in n.status.addresses:
                    if addr.type == "InternalIP":
                        node_ip = addr.address
                        break
                if node_ip:
                    break

        pool.nfs_endpoint = f"{node_ip}:{pseudo_path}"
        pool.nfs_port = node_port
        pool.ceph_subvolume_group = group_name
        pool.status = "available"
        db.commit()
        logger.info(
            "Ceph-NFS pool %s available: endpoint=%s port=%d",
            short_id,
            pool.nfs_endpoint,
            node_port,
        )

    except Exception as e:
        logger.error("Ceph-NFS provisioning failed for pool %s: %s", pool_id[:8], e)
        pool = db.query(StoragePool).get(pool_id)
        if pool:
            pool.status = "error"
            db.commit()
    finally:
        db.close()


def delete_ceph_nfs_pool(
    pool_id: str, credentials: dict, ceph_subvolume_group: str | None
):
    short_id = pool_id[:8]
    pseudo_path = f"/troshka-{short_id}"
    vol_name = f"troshka-{short_id}"
    group_name = ceph_subvolume_group or f"troshka-pool-{short_id}"
    svc_name = f"troshka-nfs-{short_id}"

    try:
        core_api, api_client = _get_k8s_clients(credentials)

        try:
            core_api.delete_namespaced_service(svc_name, ODF_NAMESPACE)
            logger.info("Deleted NodePort service %s", svc_name)
        except Exception:
            logger.warning("NodePort service %s not found, skipping", svc_name)

        try:
            toolbox = _find_toolbox_pod(core_api)
            _ceph_exec(
                core_api,
                toolbox,
                [
                    "ceph",
                    "nfs",
                    "export",
                    "rm",
                    CEPH_NFS_CLUSTER,
                    pseudo_path,
                ],
            )
            logger.info("Removed NFS export %s", pseudo_path)

            _ceph_exec(
                core_api,
                toolbox,
                [
                    "ceph",
                    "fs",
                    "subvolume",
                    "rm",
                    CEPH_FS_NAME,
                    vol_name,
                    group_name,
                ],
            )
            logger.info("Removed Ceph subvolume %s", vol_name)

            _ceph_exec(
                core_api,
                toolbox,
                [
                    "ceph",
                    "fs",
                    "subvolumegroup",
                    "rm",
                    CEPH_FS_NAME,
                    group_name,
                ],
            )
            logger.info("Removed Ceph subvolumegroup %s", group_name)
        except Exception as e:
            logger.warning("Ceph cleanup for pool %s partial: %s", short_id, e)

    except Exception as e:
        logger.error("Ceph-NFS cleanup failed for pool %s: %s", short_id, e)


# ---------------------------------------------------------------------------
# GCP Filestore provisioning
# ---------------------------------------------------------------------------


def create_netapp_pool_and_volume(
    credentials: dict,
    project: str,
    region: str,
    network: str,
    capacity_gb: int,
    volume_name: str = "troshka",
    service_level: str = "FLEX",
) -> dict:
    from google.cloud import netapp_v1
    from google.oauth2 import service_account

    sa_json = credentials.get("service_account_json", {})
    cred = service_account.Credentials.from_service_account_info(sa_json)
    client = netapp_v1.NetAppClient(credentials=cred)

    network_name = network.split("/")[-1] if "/" in network else network
    parent = f"projects/{project}/locations/{region}"

    pool = netapp_v1.StoragePool(
        service_level=netapp_v1.ServiceLevel.FLEX,
        capacity_gib=capacity_gb,
        network=f"projects/{project}/global/networks/{network_name}",
    )
    op = client.create_storage_pool(
        parent=parent,
        storage_pool_id="troshka-pool",
        storage_pool=pool,
    )
    pool_result = op.result()
    assert pool_result is not None
    logger.info("NetApp storage pool created: %s", pool_result.name)

    volume = netapp_v1.Volume(
        share_name=volume_name,
        storage_pool=pool_result.name,
        capacity_gib=capacity_gb,
        protocols=[netapp_v1.Protocols.NFSV4],
    )
    op = client.create_volume(
        parent=parent,
        volume_id=volume_name,
        volume=volume,
    )
    vol_result = op.result()
    assert vol_result is not None
    logger.info("NetApp volume created: %s", vol_result.name)

    mount_ip = None
    if vol_result.mount_options:
        for mo in vol_result.mount_options:
            if mo.export:
                parts = mo.export.split(":")
                mount_ip = parts[0] if parts else mo.export
                break

    return {
        "pool_name": pool_result.name,
        "volume_name": vol_result.name,
        "mount_ip": mount_ip,
        "share_name": volume_name,
    }


def update_netapp_capacity(credentials: dict, volume_name: str, new_capacity_gb: int):
    from google.cloud import netapp_v1
    from google.oauth2 import service_account
    from google.protobuf import field_mask_pb2

    sa_json = credentials.get("service_account_json", {})
    cred = service_account.Credentials.from_service_account_info(sa_json)
    client = netapp_v1.NetAppClient(credentials=cred)

    volume = netapp_v1.Volume(
        name=volume_name,
        capacity_gib=new_capacity_gb,
    )
    update_mask = field_mask_pb2.FieldMask(paths=["capacity_gib"])
    op = client.update_volume(volume=volume, update_mask=update_mask)
    op.result()


def provision_netapp_pool(
    pool_id: str,
    credentials: dict,
    project: str,
    region: str,
    network: str,
    capacity_gb: int,
    volume_name: str = "troshka",
    service_level: str = "FLEX",
):
    db = SessionLocal()
    try:
        result = create_netapp_pool_and_volume(
            credentials,
            project,
            region,
            network,
            capacity_gb,
            volume_name,
            service_level,
        )
        pool = db.query(StoragePool).get(pool_id)
        if not pool:
            return
        pool.netapp_pool_id = result["pool_name"]
        pool.netapp_mount_ip = result["mount_ip"]
        pool.netapp_volume_name = result["share_name"]
        pool.netapp_capacity_gb = capacity_gb
        pool.netapp_service_level = service_level
        pool.status = "available"
        db.commit()
        logger.info("NetApp pool %s is available", pool_id[:8])
    except Exception as e:
        logger.error("NetApp provisioning failed for pool %s: %s", pool_id[:8], e)
        pool = db.query(StoragePool).get(pool_id)
        if not pool:
            return
        pool.status = "error"
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Azure Files NFS provisioning
# ---------------------------------------------------------------------------


def create_azure_files_nfs(
    credentials: dict,
    resource_group: str,
    location: str,
    subnet_id: str,
    capacity_gb: int,
    share_name: str = "troshka",
    account_name: str | None = None,
) -> dict:
    from azure.identity import ClientSecretCredential
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.storage import StorageManagementClient

    credential = ClientSecretCredential(
        tenant_id=credentials["tenant_id"],
        client_id=credentials["client_id"],
        client_secret=credentials["client_secret"],
    )
    subscription_id = credentials["subscription_id"]

    if not account_name:
        import hashlib

        suffix = hashlib.md5(resource_group.encode()).hexdigest()[:8]
        account_name = f"troshkasa{suffix}"

    storage_client = StorageManagementClient(credential, subscription_id)

    # NFS v3 requires supportsHttpsTrafficOnly=False; network ACL + private
    # endpoint below are the compensating controls (no public access).
    sa_params = {
        "location": location,
        "sku": {"name": "Premium_LRS"},
        "kind": "FileStorage",
        "properties": {
            "supportsHttpsTrafficOnly": False,
            "enableNfsV3": True,
        },
    }
    poller = storage_client.storage_accounts.begin_create(
        resource_group, account_name, sa_params
    )
    poller.result()

    # Lock down: deny all public access, only reachable via private endpoint
    storage_client.storage_accounts.update(
        resource_group,
        account_name,
        {
            "properties": {
                "networkAcls": {
                    "defaultAction": "Deny",
                    "bypass": "None",
                }
            }
        },
    )

    # Private endpoint is mandatory — account is deny-all without it
    network_client = NetworkManagementClient(credential, subscription_id)
    sa_resource_id = f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers/Microsoft.Storage/storageAccounts/{account_name}"

    pe_params = {
        "location": location,
        "properties": {
            "subnet": {"id": subnet_id},
            "privateLinkServiceConnections": [
                {
                    "name": f"{account_name}-pe-conn",
                    "properties": {
                        "privateLinkServiceId": sa_resource_id,
                        "groupIds": ["file"],
                    },
                }
            ],
        },
    }
    pe_poller = network_client.private_endpoints.begin_create_or_update(  # type: ignore[call-overload]
        resource_group, f"{account_name}-pe", pe_params  # type: ignore[arg-type]
    )
    pe_result = pe_poller.result()

    # Private DNS zone so VMs can resolve the storage FQDN to the private endpoint IP
    from azure.mgmt.privatedns import PrivateDnsManagementClient

    dns_client = PrivateDnsManagementClient(credential, subscription_id)
    dns_zone = "privatelink.file.core.windows.net"

    # Extract VNet ID from subnet ID (strip /subnets/...)
    vnet_id = subnet_id.rsplit("/subnets/", 1)[0]

    try:
        dns_client.private_zones.get(resource_group, dns_zone)
    except Exception:
        dns_client.private_zones.begin_create_or_update(  # type: ignore[call-overload]
            resource_group, dns_zone, {"location": "global"}  # type: ignore[arg-type]
        ).result()

    try:
        dns_client.virtual_network_links.get(
            resource_group, dns_zone, "troshka-vnet-link"
        )
    except Exception:
        dns_client.virtual_network_links.begin_create_or_update(  # type: ignore[call-overload]
            resource_group,
            dns_zone,
            "troshka-vnet-link",
            {  # type: ignore[arg-type]
                "location": "global",
                "virtual_network": {"id": vnet_id},
                "registration_enabled": False,
            },
        ).result()

    pe_ip = pe_result.custom_dns_configs[0].ip_addresses[0]
    try:
        dns_client.record_sets.create_or_update(  # type: ignore[call-overload]
            resource_group,
            dns_zone,
            account_name,
            "A",
            {"ttl": 300, "a_records": [{"ipv4_address": pe_ip}]},  # type: ignore[arg-type]
        )
    except Exception:
        logger.warning("Failed to create DNS A record for %s", account_name)

    # Create NFS file share
    share_params = {
        "properties": {
            "shareQuota": capacity_gb,
            "enabledProtocols": "NFS",
        }
    }
    storage_client.file_shares.create(
        resource_group, account_name, share_name, share_params
    )

    mount_url = (
        f"{account_name}.privatelink.file.core.windows.net:/{account_name}/{share_name}"
    )

    return {
        "storage_account": account_name,
        "share_name": share_name,
        "mount_url": mount_url,
    }


def update_azure_files_capacity(
    credentials: dict,
    resource_group: str,
    account_name: str,
    share_name: str,
    new_capacity_gb: int,
):
    from azure.identity import ClientSecretCredential
    from azure.mgmt.storage import StorageManagementClient

    credential = ClientSecretCredential(
        tenant_id=credentials["tenant_id"],
        client_id=credentials["client_id"],
        client_secret=credentials["client_secret"],
    )
    subscription_id = credentials["subscription_id"]

    storage_client = StorageManagementClient(credential, subscription_id)
    share_params = {
        "properties": {
            "shareQuota": new_capacity_gb,
        }
    }
    storage_client.file_shares.update(
        resource_group, account_name, share_name, share_params
    )


def provision_azure_files_pool(
    pool_id: str,
    credentials: dict,
    resource_group: str,
    location: str,
    subnet_id: str,
    capacity_gb: int,
    iops: int | None = None,
    throughput: int | None = None,
    share_name: str = "troshka",
):
    db = SessionLocal()
    try:
        result = create_azure_files_nfs(
            credentials, resource_group, location, subnet_id, capacity_gb, share_name
        )
        pool = db.query(StoragePool).get(pool_id)
        if not pool:
            return
        pool.azure_storage_account = result["storage_account"]
        pool.azure_file_share_name = result["share_name"]
        pool.azure_file_share_url = result["mount_url"]
        pool.azure_files_capacity_gb = capacity_gb
        if iops:
            pool.azure_files_iops = iops
        if throughput:
            pool.azure_files_throughput = throughput
        pool.status = "available"
        db.commit()
        logger.info("Azure Files NFS pool %s is available", pool_id[:8])
    except Exception as e:
        logger.error("Azure Files provisioning failed for pool %s: %s", pool_id[:8], e)
        pool = db.query(StoragePool).get(pool_id)
        if not pool:
            return
        pool.status = "error"
        db.commit()
    finally:
        db.close()
