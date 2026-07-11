import json
import logging
from typing import Any

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
    default_region: str = ""
    default_image: str = ""
    vpc_id: str = ""
    subnet_id: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    bucket: str | None = None
    endpoint_url: str | None = None
    # OCP Virt / KubeVirt fields
    api_url: str = ""
    token: str = ""
    namespace: str = "troshka"
    verify_ssl: bool = False
    iso_pvc: str | None = None
    cache_namespace: str = ""

    # GCP fields
    gcp_project_id: str = ""
    service_account_json: str = ""

    # Azure fields
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_subscription_id: str = ""
    azure_location: str = ""


class ProviderUpdate(BaseModel):
    name: str | None = None
    default_region: str | None = None
    default_image: str | None = None
    vpc_id: str | None = None
    subnet_id: str | None = None
    security_group_id: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    api_url: str | None = None
    token: str | None = None
    namespace: str | None = None
    cache_namespace: str | None = None
    state: str | None = None


class ProviderResponse(BaseModel):
    id: str
    name: str
    type: str
    default_region: str | None
    default_image: str | None
    vpc_id: str | None
    subnet_id: str | None
    security_group_id: str | None
    console_base_domain: str | None = None
    console_nameservers: list | None = None
    console_configured: bool = False
    iso_pvc: str | None = None

    # GCP
    gcp_project_id: str | None = None
    gcp_network_id: str | None = None
    gcp_subnet_id: str | None = None
    gcp_firewall_policy: str | None = None
    gcp_zone: str | None = None

    # Azure
    azure_subscription_id: str | None = None
    azure_resource_group: str | None = None
    azure_vnet_id: str | None = None
    azure_subnet_id: str | None = None
    azure_nsg_id: str | None = None
    azure_location: str | None = None

    state: str
    has_credentials: bool
    endpoint_url: str | None = None
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
            default_image=p.default_image,
            vpc_id=p.vpc_id,
            subnet_id=p.subnet_id,
            security_group_id=p.security_group_id,
            console_base_domain=p.console_base_domain,
            console_nameservers=p.console_nameservers,
            console_configured=bool(p.console_zone_id or p.console_base_domain),
            iso_pvc=p.get_credentials().get("iso_pvc") if p.credentials else None,
            gcp_project_id=p.gcp_project_id,
            gcp_network_id=p.gcp_network_id,
            gcp_subnet_id=p.gcp_subnet_id,
            gcp_firewall_policy=p.gcp_firewall_policy,
            gcp_zone=p.gcp_zone,
            azure_subscription_id=p.azure_subscription_id,
            azure_resource_group=p.azure_resource_group,
            azure_vnet_id=p.azure_vnet_id,
            azure_subnet_id=p.azure_subnet_id,
            azure_nsg_id=p.azure_nsg_id,
            azure_location=p.azure_location,
            state=p.state,
            has_credentials=bool(p.credentials),
            endpoint_url=(
                p.get_credentials().get("endpoint_url") if p.credentials else None
            ),
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
        default_region=body.default_region or None,
        default_image=body.default_image or None,
        vpc_id=body.vpc_id or None,
        subnet_id=body.subnet_id or None,
        created_by=user.email,
    )
    if body.type == "ocpvirt":
        if not body.api_url or not body.token:
            raise HTTPException(
                status_code=400,
                detail="OCP Virt providers require api_url and token",
            )
        creds = {
            "api_url": body.api_url,
            "token": body.token,
            "namespace": body.namespace or "troshka",
            "verify_ssl": body.verify_ssl,
        }
        if body.iso_pvc is not None:
            creds["iso_pvc"] = body.iso_pvc
        provider.default_region = body.namespace or "troshka"
        api_host = (
            body.api_url.replace("https://", "").replace("http://", "").split(":")[0]
        )
        provider.console_base_domain = api_host.replace("api.", "apps.", 1)
    elif body.type == "gcp":
        if not body.gcp_project_id or not body.service_account_json:
            raise HTTPException(
                status_code=400,
                detail="GCP providers require gcp_project_id and service_account_json",
            )
        import json as json_mod

        try:
            sa_json = json_mod.loads(body.service_account_json)
        except json_mod.JSONDecodeError:
            raise HTTPException(
                status_code=400, detail="service_account_json must be valid JSON"
            )
        creds = {"service_account_json": sa_json}
        provider.gcp_project_id = body.gcp_project_id
    elif body.type == "azure":
        if not all(
            [
                body.azure_tenant_id,
                body.azure_client_id,
                body.azure_client_secret,
                body.azure_subscription_id,
            ]
        ):
            raise HTTPException(
                status_code=400,
                detail="Azure providers require tenant_id, client_id, client_secret, subscription_id",
            )
        creds = {
            "tenant_id": body.azure_tenant_id,
            "client_id": body.azure_client_id,
            "client_secret": body.azure_client_secret,
            "subscription_id": body.azure_subscription_id,
        }
        provider.azure_subscription_id = body.azure_subscription_id
        provider.azure_location = body.azure_location or body.default_region or None
    elif body.type == "kubevirt":
        if not body.api_url or not body.token:
            raise HTTPException(
                status_code=400,
                detail="KubeVirt providers require api_url and token",
            )
        op_ns = body.namespace or "troshka-operator"
        creds = {
            "api_url": body.api_url,
            "token": body.token,
            "namespace": op_ns,
            "verify_ssl": body.verify_ssl,
            "cache_namespace": body.cache_namespace or "troshka-cache",
        }
        provider.default_region = op_ns
        api_host = (
            body.api_url.replace("https://", "").replace("http://", "").split(":")[0]
        )
        provider.console_base_domain = api_host.replace("api.", "apps.", 1)
    elif body.type in ("ec2", "s3", "s3_readonly"):
        creds = {
            "access_key_id": body.access_key_id,
            "secret_access_key": body.secret_access_key,
        }
        if body.bucket:
            creds["bucket"] = body.bucket
        if body.endpoint_url:
            creds["endpoint_url"] = body.endpoint_url
    else:
        raise HTTPException(400, f"Unknown provider type: {body.type}")
    provider.set_credentials(creds)
    db.add(provider)
    db.commit()
    db.refresh(provider)

    if body.type == "s3_readonly":
        try:
            from app.services.central_library import sync_central_library

            result = sync_central_library(db, owner_id=user.id)
            logger.info("Auto-synced central library on provider creation: %s", result)
        except Exception as e:
            logger.warning("Central library auto-sync failed: %s", e)

    return ProviderResponse(
        id=provider.id,
        name=provider.name,
        type=provider.type,
        default_region=provider.default_region,
        default_image=provider.default_image,
        vpc_id=provider.vpc_id,
        subnet_id=provider.subnet_id,
        security_group_id=provider.security_group_id,
        gcp_project_id=provider.gcp_project_id,
        gcp_network_id=provider.gcp_network_id,
        gcp_subnet_id=provider.gcp_subnet_id,
        gcp_firewall_policy=provider.gcp_firewall_policy,
        gcp_zone=provider.gcp_zone,
        azure_subscription_id=provider.azure_subscription_id,
        azure_resource_group=provider.azure_resource_group,
        azure_vnet_id=provider.azure_vnet_id,
        azure_subnet_id=provider.azure_subnet_id,
        azure_nsg_id=provider.azure_nsg_id,
        azure_location=provider.azure_location,
        state=provider.state,
        has_credentials=True,
        endpoint_url=str(creds.get("endpoint_url", "")) or None,
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
    if body.default_image is not None:
        provider.default_image = body.default_image
    if body.vpc_id is not None:
        provider.vpc_id = body.vpc_id
    if body.subnet_id is not None:
        provider.subnet_id = body.subnet_id
    if body.security_group_id is not None:
        provider.security_group_id = body.security_group_id
    if body.state is not None:
        provider.state = body.state

    if body.api_url or body.token or body.namespace:
        creds = provider.get_credentials()
        if body.api_url:
            creds["api_url"] = body.api_url
        if body.token:
            creds["token"] = body.token
        if body.namespace:
            creds["namespace"] = body.namespace
            if provider.type in ("ocpvirt", "kubevirt"):
                provider.default_region = body.namespace
        if body.cache_namespace:
            creds["cache_namespace"] = body.cache_namespace
        provider.set_credentials(creds)
    elif body.access_key_id or body.secret_access_key:
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
        default_image=provider.default_image,
        vpc_id=provider.vpc_id,
        subnet_id=provider.subnet_id,
        security_group_id=provider.security_group_id,
        gcp_project_id=provider.gcp_project_id,
        gcp_network_id=provider.gcp_network_id,
        gcp_subnet_id=provider.gcp_subnet_id,
        gcp_firewall_policy=provider.gcp_firewall_policy,
        gcp_zone=provider.gcp_zone,
        azure_subscription_id=provider.azure_subscription_id,
        azure_resource_group=provider.azure_resource_group,
        azure_vnet_id=provider.azure_vnet_id,
        azure_subnet_id=provider.azure_subnet_id,
        azure_nsg_id=provider.azure_nsg_id,
        azure_location=provider.azure_location,
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


@router.get("/{provider_id}/discover-images")
def discover_images(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """List available RHEL 9 and 10 images (both Access2/Gold and Hourly/Marketplace)."""
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

        image_types = {
            "rhel10-access2": {
                "pattern": "RHEL-10*x86_64*Access2-GP3",
                "label": "RHEL 10 Access2 (Gold Image / BYOS)",
                "source": "BYOS",
            },
            "rhel10-hourly": {
                "pattern": "RHEL-10*x86_64*Hourly2-GP3",
                "label": "RHEL 10 Marketplace (Hourly)",
                "source": "PAYG",
            },
            "rhel9-access2": {
                "pattern": "RHEL-9*x86_64*Access2-GP3",
                "label": "RHEL 9 Access2 (Gold Image / BYOS)",
                "source": "BYOS",
            },
            "rhel9-hourly": {
                "pattern": "RHEL-9*x86_64*Hourly2-GP3",
                "label": "RHEL 9 Marketplace (Hourly)",
                "source": "PAYG",
            },
        }

        results = []
        for image_type, info in image_types.items():
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
                image_name = latest["Name"]
                version_match = re.search(r"RHEL-(\d+\.\d+\.\d+)", image_name)
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
                        "type": info["source"],
                        "label": label,
                        "image_id": latest["ImageId"],
                        "name": latest["Name"],
                        "created": latest["CreationDate"],
                    }
                )

        return {"region": provider.default_region, "images": results}
    except Exception:
        logger.exception("Image discovery failed for %s", provider.name)
        raise HTTPException(
            status_code=500, detail="Image discovery failed. Check server logs."
        )


# Backward-compatible alias for old endpoint name
@router.get("/{provider_id}/discover-ami")
def list_available_amis(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Deprecated: use /discover-images instead."""
    return discover_images(provider_id, user, db)


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


@router.post("/{provider_id}/set-image")
def set_image(
    provider_id: str,
    image_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Set the default image for a provider."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    provider.default_image = image_id
    db.commit()
    return {"image_id": image_id}


# Backward-compatible alias for old endpoint name
@router.post("/{provider_id}/set-ami")
def set_ami(
    provider_id: str,
    ami_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Deprecated: use /set-image instead."""
    return set_image(provider_id, ami_id, user, db)


@router.post("/{provider_id}/set-iso")
def set_iso(
    provider_id: str,
    iso_pvc: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Set the install ISO PVC name for an OCP Virt provider."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    creds = provider.get_credentials()
    creds["iso_pvc"] = iso_pvc
    provider.set_credentials(creds)
    db.commit()
    return {"iso_pvc": iso_pvc}


@router.get("/{provider_id}/discover-isos")
def discover_isos(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """List available ISO PVCs in the troshka namespace."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    if provider.type != "ocpvirt":
        raise HTTPException(
            status_code=400, detail="ISO discovery is only for OCP Virt"
        )

    creds = provider.get_credentials()
    try:
        from app.services.providers.ocpvirt import _get_k8s_clients

        _, core_api = _get_k8s_clients(creds)
        namespace = creds.get("namespace", "troshka")
        pvcs = core_api.list_namespaced_persistent_volume_claim(namespace=namespace)
        isos = []
        for pvc in pvcs.items:
            name = pvc.metadata.name
            if "iso" in name.lower():
                size = pvc.spec.resources.requests.get("storage", "")
                isos.append({"name": name, "size": size})
        isos.sort(key=lambda x: x["name"])
        return {"isos": isos}
    except Exception:
        logger.exception("ISO discovery failed for %s", provider.name)
        raise HTTPException(status_code=400, detail="Failed to list ISOs")


@router.get("/{provider_id}/discover-datasources")
def discover_datasources(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """List available VM base images (DataSources) on an OCP Virt cluster."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    if provider.type != "ocpvirt":
        raise HTTPException(
            status_code=400,
            detail="DataSource discovery is only for OCP Virt providers",
        )

    creds = provider.get_credentials()
    try:
        from app.services.providers.ocpvirt import _get_k8s_clients

        custom_api, _ = _get_k8s_clients(creds)
        ds_list = custom_api.list_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace="openshift-virtualization-os-images",
            plural="datasources",
        )
        results = []
        for ds in ds_list.get("items", []):
            name = ds["metadata"]["name"]
            conditions = ds.get("status", {}).get("conditions", [])
            ready = any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in conditions
            )
            results.append({"name": name, "ready": ready})
        results.sort(key=lambda x: x["name"])
        return {"datasources": results}
    except Exception:
        logger.exception("DataSource discovery failed for %s", provider.name)
        raise HTTPException(status_code=400, detail="Failed to list DataSources")


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
        elif provider.type == "ocpvirt":
            from app.services.providers.ocpvirt import _get_k8s_clients

            custom_api, core_api = _get_k8s_clients(creds)
            ns = creds.get("namespace", "troshka")
            core_api.read_namespace(ns)
            nodes = core_api.list_node()
            node_count = len(nodes.items)
            return {
                "status": "ok",
                "cluster": creds.get("api_url", ""),
                "namespace": ns,
                "nodes": node_count,
            }
        elif provider.type == "kubevirt":
            from app.services.providers.kubevirt import (
                _get_k8s_clients as _get_kv_clients,
            )

            custom_api, core_api, _ = _get_kv_clients(provider)
            nodes = core_api.list_node()
            node_count = len(nodes.items)

            operator_ns = creds.get("namespace", "troshka-operator")
            operator_ready = False
            operator_status = "not installed"
            try:
                core_api.read_namespace(operator_ns)
                deps = custom_api.list_namespaced_custom_object(
                    group="apps",
                    version="v1",
                    namespace=operator_ns,
                    plural="deployments",
                )
                for dep in deps.get("items", []):
                    if dep["metadata"]["name"] == "troshka-operator":
                        ready = dep.get("status", {}).get("readyReplicas", 0)
                        operator_ready = ready > 0
                        operator_status = (
                            f"running ({ready} replica)"
                            if operator_ready
                            else "not ready"
                        )
                        break
                else:
                    operator_status = "namespace exists, deployment missing"
            except Exception:
                pass

            crds_installed = False
            try:
                from kubernetes import client as k8s_client

                ext_api = k8s_client.ApiextensionsV1Api(_get_kv_clients(provider)[2])
                ext_api.read_custom_resource_definition(
                    "troshkaprojects.troshka.redhat.com"
                )
                crds_installed = True
            except Exception:
                pass

            cache_ns = creds.get("cache_namespace", "troshka-cache")
            ns_checks = {}
            for ns_name, ns_label in [
                (operator_ns, "operator"),
                (cache_ns, "cache"),
            ]:
                try:
                    core_api.read_namespace(ns_name)
                    ns_checks[ns_label] = "ok"
                except Exception:
                    try:
                        core_api.create_namespace(
                            body={
                                "apiVersion": "v1",
                                "kind": "Namespace",
                                "metadata": {
                                    "name": ns_name,
                                    "labels": {"app": "troshka"},
                                },
                            }
                        )
                        ns_checks[ns_label] = "ok (just created)"
                    except Exception as e:
                        ns_checks[ns_label] = "no access"

            return {
                "status": "ok",
                "cluster": creds.get("api_url", ""),
                "nodes": node_count,
                "operator": operator_status,
                "crds_installed": crds_installed,
                "namespaces": ns_checks,
            }
        elif provider.type == "gcp":
            import google.auth.transport.requests
            from google.oauth2 import service_account

            sa_json = creds.get("service_account_json", {})
            credential = service_account.Credentials.from_service_account_info(
                sa_json, scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            credential.refresh(google.auth.transport.requests.Request())
            return {
                "status": "ok",
                "project": provider.gcp_project_id,
                "message": f"OK — Project: {provider.gcp_project_id}",
            }
        elif provider.type == "azure":
            from azure.identity import ClientSecretCredential
            from azure.mgmt.resource import ResourceManagementClient

            credential = ClientSecretCredential(
                tenant_id=creds["tenant_id"],
                client_id=creds["client_id"],
                client_secret=creds["client_secret"],
            )
            resource_client = ResourceManagementClient(
                credential, creds["subscription_id"]
            )
            rg = provider.azure_resource_group or "troshka-rg"
            rg_info = resource_client.resource_groups.get(rg)
            return {
                "status": "ok",
                "message": f"OK — Resource Group: {rg} ({rg_info.location})",
            }
        elif provider.type == "ec2":
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
        else:
            raise HTTPException(400, f"Unknown provider type: {provider.type}")
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
    """Set up console infrastructure for direct VNC proxy."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    base_domain = req.base_domain.strip().lower()
    if not base_domain or "." not in base_domain:
        raise HTTPException(status_code=400, detail="Invalid domain name")

    if provider.type == "ocpvirt":
        provider.console_base_domain = base_domain
        db.commit()
        return {
            "zone_id": None,
            "base_domain": base_domain,
            "nameservers": [],
        }

    import boto3

    creds = provider.get_credentials()

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


@router.post("/{provider_id}/create-network-gcp")
def create_network_gcp(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Create a VPC network, subnet, and firewall rules for a GCP provider."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider or provider.type != "gcp":
        raise HTTPException(status_code=404, detail="GCP provider not found")

    from google.cloud import compute_v1
    from google.oauth2 import service_account

    creds = provider.get_credentials()
    sa_json = creds.get("service_account_json", {})
    credential = service_account.Credentials.from_service_account_info(sa_json)
    project = provider.gcp_project_id
    region = provider.default_region or "us-central1"

    # Create VPC network (custom mode — no auto-subnets)
    networks_client = compute_v1.NetworksClient(credentials=credential)
    network = compute_v1.Network(
        name="troshka-vpc",
        auto_create_subnetworks=False,
    )
    op = networks_client.insert(project=project, network_resource=network)
    op.result()
    created_network = networks_client.get(project=project, network="troshka-vpc")

    # Create subnet
    subnets_client = compute_v1.SubnetworksClient(credentials=credential)
    subnet = compute_v1.Subnetwork(
        name="troshka-subnet",
        ip_cidr_range="10.100.1.0/24",
        network=created_network.self_link,
        region=region,
    )
    op = subnets_client.insert(
        project=project, region=region, subnetwork_resource=subnet
    )
    op.result()
    created_subnet = subnets_client.get(
        project=project, region=region, subnetwork="troshka-subnet"
    )

    # Create firewall rules
    firewalls_client = compute_v1.FirewallsClient(credentials=credential)
    fw_rules = [
        ("troshka-allow-ssh", "tcp", ["22"]),
        ("troshka-allow-console", "tcp", ["443"]),
        ("troshka-allow-agent", "tcp", ["31337"]),
        ("troshka-allow-vxlan", "udp", ["4789"]),
    ]
    for fw_name, protocol, ports in fw_rules:
        fw = compute_v1.Firewall(
            name=fw_name,
            network=created_network.self_link,
            allowed=[compute_v1.Allowed(I_p_protocol=protocol, ports=ports)],
            source_ranges=["0.0.0.0/0"],
            target_tags=["troshka-host"],
        )
        try:
            op = firewalls_client.insert(project=project, firewall_resource=fw)
            op.result()
        except Exception as e:
            if "already exists" not in str(e).lower():
                raise

    # Store results on provider
    provider.gcp_network_id = created_network.self_link
    provider.gcp_subnet_id = created_subnet.self_link
    provider.gcp_firewall_policy = "troshka-fw"
    provider.gcp_zone = region + "-a"
    db.commit()

    return {
        "status": "ok",
        "network": created_network.self_link,
        "subnet": created_subnet.self_link,
        "zone": region + "-a",
    }


@router.get("/{provider_id}/discover-images-gcp")
def discover_images_gcp(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Discover RHEL BYOS and PAYG images on GCP."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider or provider.type != "gcp":
        raise HTTPException(status_code=404, detail="GCP provider not found")

    from google.cloud import compute_v1
    from google.oauth2 import service_account

    creds = provider.get_credentials()
    sa_json = creds.get("service_account_json", {})
    credential = service_account.Credentials.from_service_account_info(sa_json)

    images_client = compute_v1.ImagesClient(credentials=credential)
    skip = ("arm64", "eus", "sap", "baremetal")
    latest_by_prefix: dict[str, dict] = {}

    for image_project in ["rhel-cloud"]:
        source = "PAYG"
        try:
            for img in images_client.list(project=image_project):
                name = img.name or ""
                if not name.startswith(("rhel-9", "rhel-10")):
                    continue
                if "lvm" not in name:
                    continue
                if any(s in name for s in skip):
                    continue
                if img.deprecated and img.deprecated.state == "DEPRECATED":
                    continue
                parts = name.rsplit("-v", 1)
                prefix = (
                    f"{source}:{parts[0]}" if len(parts) == 2 else f"{source}:{name}"
                )
                ts = img.creation_timestamp or ""
                if (
                    prefix not in latest_by_prefix
                    or ts > latest_by_prefix[prefix]["creation_timestamp"]
                ):
                    latest_by_prefix[prefix] = {
                        "name": name,
                        "self_link": img.self_link,
                        "family": img.family or "",
                        "source": source,
                        "creation_timestamp": ts,
                    }
        except Exception as e:
            logger.warning("Failed to list images from %s: %s", image_project, e)

    results = sorted(
        latest_by_prefix.values(),
        key=lambda x: x["creation_timestamp"],
        reverse=True,
    )
    return results


@router.post("/{provider_id}/create-network-azure")
def create_network_azure(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Create a Resource Group, VNet, subnet, and NSG for an Azure provider."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider or provider.type != "azure":
        raise HTTPException(status_code=404, detail="Azure provider not found")

    from azure.identity import ClientSecretCredential
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.resource import ResourceManagementClient

    creds = provider.get_credentials()
    credential = ClientSecretCredential(
        tenant_id=creds["tenant_id"],
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
    )
    subscription_id = creds["subscription_id"]
    location = provider.azure_location or provider.default_region or "eastus"
    rg_name = "troshka-rg"

    # Create Resource Group
    resource_client = ResourceManagementClient(credential, subscription_id)
    resource_client.resource_groups.create_or_update(rg_name, {"location": location})

    network_client = NetworkManagementClient(credential, subscription_id)

    # Create NSG with rules
    nsg_params = {
        "location": location,
        "security_rules": [
            {
                "name": "troshka-allow-ssh",
                "priority": 100,
                "direction": "Inbound",
                "access": "Allow",
                "protocol": "Tcp",
                "source_address_prefix": "*",
                "source_port_range": "*",
                "destination_address_prefix": "*",
                "destination_port_range": "22",
            },
            {
                "name": "troshka-allow-console",
                "priority": 110,
                "direction": "Inbound",
                "access": "Allow",
                "protocol": "Tcp",
                "source_address_prefix": "*",
                "source_port_range": "*",
                "destination_address_prefix": "*",
                "destination_port_range": "443",
            },
            {
                "name": "troshka-allow-agent",
                "priority": 120,
                "direction": "Inbound",
                "access": "Allow",
                "protocol": "Tcp",
                "source_address_prefix": "*",
                "source_port_range": "*",
                "destination_address_prefix": "*",
                "destination_port_range": "31337",
            },
            {
                "name": "troshka-allow-vxlan",
                "priority": 130,
                "direction": "Inbound",
                "access": "Allow",
                "protocol": "Udp",
                "source_address_prefix": "VirtualNetwork",
                "source_port_range": "*",
                "destination_address_prefix": "VirtualNetwork",
                "destination_port_range": "4789",
            },
        ],
    }
    nsg_poller = network_client.network_security_groups.begin_create_or_update(  # type: ignore[call-overload]
        rg_name, "troshka-nsg", nsg_params
    )
    nsg = nsg_poller.result()

    # Create VNet with subnet
    vnet_params = {
        "location": location,
        "address_space": {"address_prefixes": ["10.100.0.0/16"]},
        "subnets": [
            {
                "name": "troshka-subnet",
                "address_prefix": "10.100.1.0/24",
                "network_security_group": {"id": nsg.id},
            }
        ],
    }
    vnet_poller = network_client.virtual_networks.begin_create_or_update(  # type: ignore[call-overload]
        rg_name, "troshka-vnet", vnet_params
    )
    vnet = vnet_poller.result()

    subnet = network_client.subnets.get(rg_name, "troshka-vnet", "troshka-subnet")

    # Store results on provider
    provider.azure_resource_group = rg_name
    provider.azure_vnet_id = vnet.id
    provider.azure_subnet_id = subnet.id
    provider.azure_nsg_id = nsg.id
    provider.azure_location = location
    db.commit()

    return {
        "status": "ok",
        "resource_group": rg_name,
        "vnet": vnet.id,
        "subnet": subnet.id,
        "nsg": nsg.id,
    }


@router.get("/{provider_id}/discover-images-azure")
def discover_images_azure(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Discover RHEL BYOS and PAYG images on Azure."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider or provider.type != "azure":
        raise HTTPException(status_code=404, detail="Azure provider not found")

    from azure.identity import ClientSecretCredential
    from azure.mgmt.compute import ComputeManagementClient

    creds = provider.get_credentials()
    credential = ClientSecretCredential(
        tenant_id=creds["tenant_id"],
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
    )
    subscription_id = creds["subscription_id"]
    location = provider.azure_location or provider.default_region or "eastus"

    compute_client = ComputeManagementClient(credential, subscription_id)
    results = []

    offers = [
        ("redhat", "RHEL", "PAYG"),
    ]
    for publisher, offer, source in offers:
        try:
            skus = compute_client.virtual_machine_images.list_skus(
                location, publisher, offer
            )
            for sku in skus:
                sku_name = sku.name or ""
                if not any(
                    sku_name.startswith(p)
                    for p in [
                        "rhel-lvm9",
                        "rhel-lvm10",
                        "9-lvm",
                        "9_",
                        "10-lvm",
                        "10_",
                    ]
                ):
                    continue
                if "lvm" not in sku_name:
                    continue
                if not sku_name.endswith("-gen2") and any(
                    (sku_name + "-gen2") == s.name for s in skus
                ):
                    continue
                try:
                    images = compute_client.virtual_machine_images.list(
                        location, publisher, offer, sku_name
                    )
                    if images:
                        latest = images[-1]
                        urn = f"{publisher}:{offer}:{sku_name}:{latest.name}"
                        results.append(
                            {
                                "name": sku_name,
                                "urn": urn,
                                "version": latest.name,
                                "source": source,
                                "rhel_version": (
                                    sku_name.split("-")[0]
                                    if sku_name[0].isdigit()
                                    else sku_name
                                ),
                            }
                        )
                except Exception:
                    pass
        except Exception as e:
            logger.warning(
                "Failed to list Azure images for %s/%s: %s", publisher, offer, e
            )

    return results


@router.post("/{provider_id}/build-image")
def build_image(
    provider_id: str,
    body: dict[str, Any] | None = None,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    import threading

    from app.services import image_builder_service

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    if provider.type not in ("gcp", "azure"):
        raise HTTPException(
            status_code=400,
            detail="Image Builder only supports GCP or Azure providers",
        )

    current = image_builder_service.get_build_status(provider_id)
    if current.get("status") in ("authenticating", "building"):
        raise HTTPException(status_code=409, detail="A build is already in progress")

    body = body or {}
    rhel_version = body.get("rhel_version", "rhel-10")

    VALID_RHEL_VERSIONS = {"rhel-9", "rhel-10"}
    if rhel_version not in VALID_RHEL_VERSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid RHEL version. Must be one of: {', '.join(sorted(VALID_RHEL_VERSIONS))}",
        )

    threading.Thread(
        target=image_builder_service.build_host_image,
        args=(provider_id, user.id, rhel_version),
        daemon=True,
        name=f"image-build-{provider_id[:8]}",
    ).start()

    return {"status": "started", "message": f"Building {rhel_version} image..."}


@router.get("/{provider_id}/build-image/status")
def build_image_status(
    provider_id: str,
    user: User = Depends(require_role("admin")),
):
    from app.services import image_builder_service

    return image_builder_service.get_build_status(provider_id)


@router.delete("/{provider_id}/build-image/status", status_code=204)
def clear_build_image_status(
    provider_id: str,
    user: User = Depends(require_role("admin")),
):
    from app.services import image_builder_service

    image_builder_service.clear_build_status(provider_id)
