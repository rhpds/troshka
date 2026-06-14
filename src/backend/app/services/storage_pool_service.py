import datetime
import logging
import threading

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
        .sign(ca_key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = host_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem


def _boto_client(service: str, region: str, credentials: dict):
    return boto3.client(
        service,
        region_name=region,
        aws_access_key_id=credentials.get("access_key_id"),
        aws_secret_access_key=credentials.get("secret_access_key"),
    )


def probe_az_capacity(
    credentials: dict, region: str, instance_types: list[str]
) -> dict:
    ec2 = _boto_client("ec2", region, credentials)
    results = {}

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
                pool.status = "error"
                db.commit()
                logger.error(
                    "FSx %s failed for pool %s: %s", filesystem_id, pool_id, status
                )
                return

        pool = db.query(StoragePool).get(pool_id)
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
        pool.fsx_filesystem_id = result["filesystem_id"]
        pool.fsx_dns_name = result.get("dns_name")
        db.commit()
    except Exception as e:
        logger.error("FSx provisioning failed for pool %s: %s", pool_id[:8], e)
        pool = db.query(StoragePool).get(pool_id)
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

    if rules_to_add:
        ec2.authorize_security_group_ingress(
            GroupId=security_group_id,
            IpPermissions=rules_to_add,
        )
        logger.info("Added NFS/migration SG rules to %s", security_group_id)
