import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.provider import Provider
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/providers", tags=["providers"])


class ProviderCreate(BaseModel):
    name: str
    type: str
    default_region: str
    default_ami: str = ""
    vpc_id: str = ""
    subnet_id: str = ""
    access_key_id: str
    secret_access_key: str
    bucket: str | None = None


class ProviderUpdate(BaseModel):
    name: str | None = None
    default_region: str | None = None
    default_ami: str | None = None
    vpc_id: str | None = None
    subnet_id: str | None = None
    security_group_id: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    state: str | None = None


class ProviderResponse(BaseModel):
    id: str
    name: str
    type: str
    default_region: str | None
    default_ami: str | None
    vpc_id: str | None
    subnet_id: str | None
    security_group_id: str | None
    console_base_domain: str | None = None
    console_nameservers: list | None = None
    console_configured: bool = False
    state: str
    has_credentials: bool
    host_count: int
    created_at: str

    model_config = {"from_attributes": False}


@router.get("/", response_model=list[ProviderResponse])
def list_providers(
    user: User = Depends(require_role("admin")), db: Session = Depends(get_db)
):
    providers = db.query(Provider).order_by(Provider.name).all()
    return [
        ProviderResponse(
            id=p.id,
            name=p.name,
            type=p.type,
            default_region=p.default_region,
            default_ami=p.default_ami,
            vpc_id=p.vpc_id,
            subnet_id=p.subnet_id,
            security_group_id=p.security_group_id,
            console_base_domain=p.console_base_domain,
            console_nameservers=p.console_nameservers,
            console_configured=p.console_zone_id is not None,
            state=p.state,
            has_credentials=bool(p.credentials),
            host_count=len(p.hosts),
            created_at=p.created_at.isoformat() if p.created_at else "",
        )
        for p in providers
    ]


@router.post("/", response_model=ProviderResponse, status_code=201)
def create_provider(
    body: ProviderCreate,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    existing = db.query(Provider).filter_by(name=body.name).first()
    if existing:
        raise HTTPException(status_code=409, detail="Provider name already exists")

    provider = Provider(
        name=body.name,
        type=body.type,
        default_region=body.default_region,
        default_ami=body.default_ami or None,
        vpc_id=body.vpc_id or None,
        subnet_id=body.subnet_id or None,
        created_by=user.email,
    )
    creds = {
        "access_key_id": body.access_key_id,
        "secret_access_key": body.secret_access_key,
    }
    if body.bucket:
        creds["bucket"] = body.bucket
    provider.set_credentials(creds)
    db.add(provider)
    db.commit()
    db.refresh(provider)

    return ProviderResponse(
        id=provider.id,
        name=provider.name,
        type=provider.type,
        default_region=provider.default_region,
        default_ami=provider.default_ami,
        vpc_id=provider.vpc_id,
        subnet_id=provider.subnet_id,
        security_group_id=provider.security_group_id,
        state=provider.state,
        has_credentials=True,
        host_count=0,
        created_at=provider.created_at.isoformat() if provider.created_at else "",
    )


@router.patch("/{provider_id}", response_model=ProviderResponse)
def update_provider(
    provider_id: str,
    body: ProviderUpdate,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    if body.name is not None:
        provider.name = body.name
    if body.default_region is not None:
        provider.default_region = body.default_region
    if body.default_ami is not None:
        provider.default_ami = body.default_ami
    if body.vpc_id is not None:
        provider.vpc_id = body.vpc_id
    if body.subnet_id is not None:
        provider.subnet_id = body.subnet_id
    if body.security_group_id is not None:
        provider.security_group_id = body.security_group_id
    if body.state is not None:
        provider.state = body.state

    if body.access_key_id or body.secret_access_key:
        creds = provider.get_credentials()
        if body.access_key_id:
            creds["access_key_id"] = body.access_key_id
        if body.secret_access_key:
            creds["secret_access_key"] = body.secret_access_key
        provider.set_credentials(creds)

    db.commit()
    db.refresh(provider)

    return ProviderResponse(
        id=provider.id,
        name=provider.name,
        type=provider.type,
        default_region=provider.default_region,
        default_ami=provider.default_ami,
        vpc_id=provider.vpc_id,
        subnet_id=provider.subnet_id,
        security_group_id=provider.security_group_id,
        state=provider.state,
        has_credentials=bool(provider.credentials),
        host_count=len(provider.hosts),
        created_at=provider.created_at.isoformat() if provider.created_at else "",
    )


@router.delete("/{provider_id}", status_code=204)
def delete_provider(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    if provider.hosts:
        raise HTTPException(
            status_code=409, detail="Provider has hosts — remove them first"
        )
    db.delete(provider)
    db.commit()


@router.get("/{provider_id}/discover-ami")
def list_available_amis(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """List available RHEL 9 and 10 AMIs (both Access2/Gold and Hourly/Marketplace)."""
    import re

    import boto3

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    creds = provider.get_credentials()
    try:
        ec2 = boto3.client(
            "ec2",
            region_name=provider.default_region,
            aws_access_key_id=creds.get("access_key_id"),
            aws_secret_access_key=creds.get("secret_access_key"),
        )

        ami_types = {
            "rhel10-access2": {
                "pattern": "RHEL-10*x86_64*Access2-GP3",
                "label": "RHEL 10 Access2 (Gold Image / BYOS)",
            },
            "rhel10-hourly": {
                "pattern": "RHEL-10*x86_64*Hourly2-GP3",
                "label": "RHEL 10 Marketplace (Hourly)",
            },
            "rhel9-access2": {
                "pattern": "RHEL-9*x86_64*Access2-GP3",
                "label": "RHEL 9 Access2 (Gold Image / BYOS)",
            },
            "rhel9-hourly": {
                "pattern": "RHEL-9*x86_64*Hourly2-GP3",
                "label": "RHEL 9 Marketplace (Hourly)",
            },
        }

        results = []
        for ami_type, info in ami_types.items():
            response = ec2.describe_images(
                Owners=["309956199498"],
                Filters=[
                    {"Name": "name", "Values": [info["pattern"]]},
                    {"Name": "state", "Values": ["available"]},
                ],
            )

            def version_key(img):
                m = re.search(r"RHEL-(\d+)\.(\d+)\.(\d+)", img["Name"])
                if m:
                    return (
                        int(m.group(1)),
                        int(m.group(2)),
                        int(m.group(3)),
                        img["CreationDate"],
                    )
                return (0, 0, 0, img["CreationDate"])

            images = sorted(response["Images"], key=version_key)
            if images:
                latest = images[-1]
                # Extract version from name like "RHEL-10.2.0_HVM..." or "RHEL-9.7.0_HVM..."
                ami_name = latest["Name"]
                version_match = re.search(r"RHEL-(\d+\.\d+\.\d+)", ami_name)
                version = version_match.group(1) if version_match else ""
                label = (
                    info["label"]
                    .replace("RHEL 10", f"RHEL {version}")
                    .replace("RHEL 9", f"RHEL {version}")
                    if version
                    else info["label"]
                )
                results.append(
                    {
                        "type": ami_type,
                        "label": label,
                        "ami_id": latest["ImageId"],
                        "name": latest["Name"],
                        "created": latest["CreationDate"],
                    }
                )

        return {"region": provider.default_region, "amis": results}
    except Exception:
        logger.exception("AMI discovery failed for %s", provider.name)
        raise HTTPException(
            status_code=500, detail="AMI discovery failed. Check server logs."
        )


@router.get("/{provider_id}/discover-vpcs")
def discover_vpcs(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """List available VPCs and subnets in the provider's region."""
    import boto3

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    creds = provider.get_credentials()
    try:
        ec2 = boto3.client(
            "ec2",
            region_name=provider.default_region,
            aws_access_key_id=creds.get("access_key_id"),
            aws_secret_access_key=creds.get("secret_access_key"),
        )

        vpcs_resp = ec2.describe_vpcs(
            Filters=[{"Name": "tag:ManagedBy", "Values": ["troshka"]}]
        )
        vpcs = []
        for vpc in vpcs_resp["Vpcs"]:
            vpc_id = vpc["VpcId"]
            name = ""
            for tag in vpc.get("Tags", []):
                if tag["Key"] == "Name":
                    name = tag["Value"]

            subnets_resp = ec2.describe_subnets(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            )
            subnets = [
                {
                    "subnet_id": s["SubnetId"],
                    "az": s["AvailabilityZone"],
                    "cidr": s["CidrBlock"],
                    "public": s.get("MapPublicIpOnLaunch", False),
                }
                for s in subnets_resp["Subnets"]
            ]

            vpcs.append(
                {
                    "vpc_id": vpc_id,
                    "name": name or vpc_id,
                    "cidr": vpc["CidrBlock"],
                    "is_default": vpc.get("IsDefault", False),
                    "subnets": subnets,
                }
            )

        return {"region": provider.default_region, "vpcs": vpcs}
    except Exception:
        logger.exception("VPC discovery failed for %s", provider.name)
        raise HTTPException(
            status_code=500, detail="VPC discovery failed. Check server logs."
        )


@router.post("/{provider_id}/create-vpc")
def create_vpc(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Create a new VPC with a public subnet for troshka hosts."""
    import boto3

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    creds = provider.get_credentials()
    try:
        ec2 = boto3.client(
            "ec2",
            region_name=provider.default_region,
            aws_access_key_id=creds.get("access_key_id"),
            aws_secret_access_key=creds.get("secret_access_key"),
        )

        vpc = ec2.create_vpc(CidrBlock="10.100.0.0/16")
        vpc_id = vpc["Vpc"]["VpcId"]
        ec2.create_tags(
            Resources=[vpc_id],
            Tags=[
                {"Key": "Name", "Value": "troshka-vpc"},
                {"Key": "Project", "Value": "troshka"},
                {"Key": "ManagedBy", "Value": "troshka"},
            ],
        )
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

        igw = ec2.create_internet_gateway()
        igw_id = igw["InternetGateway"]["InternetGatewayId"]
        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
        ec2.create_tags(
            Resources=[igw_id],
            Tags=[
                {"Key": "Name", "Value": "troshka-igw"},
                {"Key": "ManagedBy", "Value": "troshka"},
            ],
        )

        # Create a subnet in every AZ so the provisioner can pick one that supports the instance type
        azs_resp = ec2.describe_availability_zones(
            Filters=[{"Name": "state", "Values": ["available"]}]
        )
        azs = [az["ZoneName"] for az in azs_resp["AvailabilityZones"]]

        subnet_ids = []
        first_subnet_id = None
        for i, az in enumerate(azs):
            cidr = f"10.100.{i + 1}.0/24"
            subnet = ec2.create_subnet(
                VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=az
            )
            sid = subnet["Subnet"]["SubnetId"]
            ec2.modify_subnet_attribute(
                SubnetId=sid, MapPublicIpOnLaunch={"Value": True}
            )
            ec2.create_tags(
                Resources=[sid],
                Tags=[
                    {"Key": "Name", "Value": f"troshka-{az}"},
                    {"Key": "ManagedBy", "Value": "troshka"},
                ],
            )
            subnet_ids.append(sid)
            if not first_subnet_id:
                first_subnet_id = sid

        route_tables = ec2.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )
        rt_id = route_tables["RouteTables"][0]["RouteTableId"]
        ec2.create_route(
            RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id
        )
        # Associate all subnets with the route table
        for sid in subnet_ids:
            try:
                ec2.associate_route_table(RouteTableId=rt_id, SubnetId=sid)
            except Exception:
                pass

        # S3 Gateway Endpoint — keeps S3 traffic off the internet (free, faster)
        try:
            ec2.create_vpc_endpoint(
                VpcId=vpc_id,
                ServiceName=f"com.amazonaws.{provider.default_region}.s3",
                RouteTableIds=[rt_id],
                VpcEndpointType="Gateway",
                TagSpecifications=[
                    {
                        "ResourceType": "vpc-endpoint",
                        "Tags": [
                            {"Key": "Name", "Value": "troshka-s3-endpoint"},
                            {"Key": "ManagedBy", "Value": "troshka"},
                        ],
                    }
                ],
            )
            logger.info("Created S3 Gateway Endpoint for VPC %s", vpc_id)
        except Exception as e:
            logger.warning("S3 endpoint creation failed (non-fatal): %s", e)

        from app.services.provisioner import ensure_security_group

        sg_id = ensure_security_group(vpc_id, credentials=creds)

        provider.vpc_id = vpc_id
        provider.subnet_id = first_subnet_id
        provider.security_group_id = sg_id
        db.commit()

        return {
            "vpc_id": vpc_id,
            "subnet_ids": subnet_ids,
            "security_group_id": sg_id,
            "internet_gateway_id": igw_id,
            "cidr": "10.100.0.0/16",
            "availability_zones": azs,
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("VPC creation failed for %s", provider.name)
        raise HTTPException(
            status_code=500, detail="VPC creation failed. Check server logs."
        )


@router.post("/{provider_id}/setup-infra")
def setup_infrastructure(
    provider_id: str,
    vpc_id: str,
    subnet_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Set VPC/subnet on the provider and ensure security group exists."""

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    creds = provider.get_credentials()
    try:
        from app.services.provisioner import ensure_security_group

        sg_id = ensure_security_group(vpc_id, credentials=creds)

        provider.vpc_id = vpc_id
        provider.subnet_id = subnet_id
        provider.security_group_id = sg_id
        db.commit()

        return {
            "vpc_id": vpc_id,
            "subnet_id": subnet_id,
            "security_group_id": sg_id,
        }
    except Exception:
        logger.exception("Infrastructure setup failed for %s", provider.name)
        raise HTTPException(
            status_code=500, detail="Infrastructure setup failed. Check server logs."
        )


@router.post("/{provider_id}/set-ami")
def set_ami(
    provider_id: str,
    ami_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Set the default AMI for a provider."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    provider.default_ami = ami_id
    db.commit()
    return {"ami_id": ami_id}


@router.post("/{provider_id}/test")
def test_provider(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Test provider credentials by calling AWS STS."""
    import boto3

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    creds = provider.get_credentials()
    try:
        if provider.type == "s3":
            s3 = boto3.client(
                "s3",
                region_name=provider.default_region,
                aws_access_key_id=creds.get("access_key_id"),
                aws_secret_access_key=creds.get("secret_access_key"),
            )
            bucket = creds.get("bucket", "troshka-images")
            # Test credentials first via STS
            sts = boto3.client(
                "sts",
                region_name=provider.default_region,
                aws_access_key_id=creds.get("access_key_id"),
                aws_secret_access_key=creds.get("secret_access_key"),
            )
            identity = sts.get_caller_identity()
            # Then check bucket
            try:
                s3.head_bucket(Bucket=bucket)
                return {
                    "status": "ok",
                    "bucket": bucket,
                    "account": identity["Account"],
                }
            except s3.exceptions.ClientError as e:
                code = e.response["Error"]["Code"]
                if code == "404":
                    return {
                        "status": "ok",
                        "bucket_missing": True,
                        "bucket": bucket,
                        "account": identity["Account"],
                        "message": f"Credentials OK but bucket '{bucket}' does not exist. Click Create Bucket.",
                    }
                elif code == "403":
                    return {
                        "status": "ok",
                        "bucket_denied": True,
                        "bucket": bucket,
                        "account": identity["Account"],
                        "message": f"Credentials OK but no access to bucket '{bucket}'.",
                    }
                raise
        else:
            sts = boto3.client(
                "sts",
                region_name=provider.default_region,
                aws_access_key_id=creds.get("access_key_id"),
                aws_secret_access_key=creds.get("secret_access_key"),
            )
            identity = sts.get_caller_identity()
            return {
                "status": "ok",
                "account": identity["Account"],
                "arn": identity["Arn"],
            }
    except Exception:
        logger.exception("Provider test failed for %s", provider.name)
        raise HTTPException(status_code=400, detail="Credentials test failed")


@router.post("/{provider_id}/create-bucket")
def create_s3_bucket(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Create the S3 bucket for a storage provider."""
    import boto3

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    if provider.type != "s3":
        raise HTTPException(status_code=400, detail="Not an S3 provider")

    creds = provider.get_credentials()
    bucket = creds.get("bucket", "troshka-images")

    s3 = boto3.client(
        "s3",
        region_name=provider.default_region,
        aws_access_key_id=creds.get("access_key_id"),
        aws_secret_access_key=creds.get("secret_access_key"),
    )

    try:
        if provider.default_region == "us-east-1":
            s3.create_bucket(Bucket=bucket)
        else:
            s3.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={
                    "LocationConstraint": provider.default_region
                },
            )
        return {"status": "created", "bucket": bucket}
    except s3.exceptions.BucketAlreadyOwnedByYou:
        return {"status": "exists", "bucket": bucket}
    except Exception as e:
        logger.exception("Failed to create bucket %s: %s", bucket, e)
        raise HTTPException(status_code=500, detail=f"Failed to create bucket: {e}")


@router.get("/{provider_id}/availability-zones")
def list_availability_zones(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """List available AZs in the provider's region."""
    import boto3

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    if provider.type != "ec2":
        raise HTTPException(status_code=400, detail="Not an EC2 provider")

    creds = provider.get_credentials()
    ec2 = boto3.client(
        "ec2",
        region_name=provider.default_region,
        aws_access_key_id=creds.get("access_key_id"),
        aws_secret_access_key=creds.get("secret_access_key"),
    )

    resp = ec2.describe_availability_zones(
        Filters=[{"Name": "state", "Values": ["available"]}]
    )
    azs = sorted(az["ZoneName"] for az in resp["AvailabilityZones"])
    return azs


class ConsoleSetupRequest(BaseModel):
    base_domain: str


@router.post("/{provider_id}/setup-console")
def setup_console(
    provider_id: str,
    req: ConsoleSetupRequest,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Create Route53 hosted zone and IAM resources for direct console proxy."""
    import boto3

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    creds = provider.get_credentials()
    base_domain = req.base_domain.strip().lower()
    if not base_domain or "." not in base_domain:
        raise HTTPException(status_code=400, detail="Invalid domain name")

    try:
        r53 = boto3.client(
            "route53",
            aws_access_key_id=creds.get("access_key_id"),
            aws_secret_access_key=creds.get("secret_access_key"),
        )

        # Check if zone already exists
        existing = r53.list_hosted_zones_by_name(DNSName=base_domain, MaxItems="1")
        zone_id = None
        nameservers = []
        for zone in existing.get("HostedZones", []):
            if zone["Name"].rstrip(".") == base_domain:
                zone_id = zone["Id"].split("/")[-1]
                ns_resp = r53.get_hosted_zone(Id=zone_id)
                nameservers = ns_resp["DelegationSet"]["NameServers"]
                break

        if not zone_id:
            import time

            resp = r53.create_hosted_zone(
                Name=base_domain,
                CallerReference=f"troshka-console-{int(time.time())}",
                HostedZoneConfig={"Comment": "Troshka console proxy DNS"},
            )
            zone_id = resp["HostedZone"]["Id"].split("/")[-1]
            nameservers = resp["DelegationSet"]["NameServers"]
            logger.info("Created hosted zone %s for %s", zone_id, base_domain)

        # Create IAM role + instance profile (idempotent)
        iam = boto3.client(
            "iam",
            aws_access_key_id=creds.get("access_key_id"),
            aws_secret_access_key=creds.get("secret_access_key"),
        )
        role_name = "troshka-certbot-role"
        profile_name = "troshka-certbot-profile"

        try:
            iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "ec2.amazonaws.com"},
                                "Action": "sts:AssumeRole",
                            }
                        ],
                    }
                ),
                Description="Allows EC2 hosts to manage Route53 for certbot DNS-01",
                Tags=[{"Key": "ManagedBy", "Value": "troshka"}],
            )
        except iam.exceptions.EntityAlreadyExistsException:
            pass

        iam.put_role_policy(
            RoleName=role_name,
            PolicyName="troshka-certbot-dns",
            PolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "route53:ChangeResourceRecordSets",
                            "Resource": f"arn:aws:route53:::hostedzone/{zone_id}",
                        },
                        {
                            "Effect": "Allow",
                            "Action": ["route53:GetChange", "route53:ListHostedZones"],
                            "Resource": "*",
                        },
                    ],
                }
            ),
        )

        try:
            iam.create_instance_profile(InstanceProfileName=profile_name)
        except iam.exceptions.EntityAlreadyExistsException:
            pass

        try:
            iam.add_role_to_instance_profile(
                InstanceProfileName=profile_name, RoleName=role_name
            )
        except iam.exceptions.LimitExceededException:
            pass

        # Store on provider
        provider.console_zone_id = zone_id
        provider.console_base_domain = base_domain
        provider.console_nameservers = nameservers
        db.commit()

        return {
            "zone_id": zone_id,
            "base_domain": base_domain,
            "nameservers": nameservers,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to setup console for provider %s", provider_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{provider_id}/console")
def delete_console(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Remove console DNS configuration and hosted zone."""
    import boto3

    from app.models.host import Host

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    if not provider.console_zone_id:
        raise HTTPException(status_code=400, detail="Console not configured")

    creds = provider.get_credentials()
    zone_id = provider.console_zone_id

    # Only delete the hosted zone if no other providers share it
    other_users = (
        db.query(Provider)
        .filter(
            Provider.console_zone_id == zone_id,
            Provider.id != provider_id,
        )
        .count()
    )

    if other_users == 0:
        try:
            r53 = boto3.client(
                "route53",
                aws_access_key_id=creds.get("access_key_id"),
                aws_secret_access_key=creds.get("secret_access_key"),
            )

            # Delete all A records in the zone
            paginator = r53.get_paginator("list_resource_record_sets")
            changes = []
            for page in paginator.paginate(HostedZoneId=zone_id):
                for rrs in page["ResourceRecordSets"]:
                    if rrs["Type"] in ("A", "CNAME"):
                        changes.append({"Action": "DELETE", "ResourceRecordSet": rrs})
            if changes:
                for i in range(0, len(changes), 100):
                    r53.change_resource_record_sets(
                        HostedZoneId=zone_id,
                        ChangeBatch={"Changes": changes[i : i + 100]},
                    )

            r53.delete_hosted_zone(Id=zone_id)
            logger.info("Deleted hosted zone %s", zone_id)

        except Exception as e:
            logger.warning("Failed to fully clean up hosted zone %s: %s", zone_id, e)
    else:
        logger.info(
            "Hosted zone %s still used by %d other provider(s), keeping it",
            zone_id,
            other_users,
        )

    # Clear console_domain on all hosts under this provider
    hosts = db.query(Host).filter_by(provider_id=provider_id).all()
    for h in hosts:
        h.console_domain = None
    provider.console_zone_id = None
    provider.console_base_domain = None
    provider.console_nameservers = None
    db.commit()

    return {"status": "removed"}
