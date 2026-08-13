import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.core.logging_utils import sanitize_log
from app.models.provider import Provider
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/providers", tags=["providers"])

AdminUser = Annotated[User, Depends(require_role("admin"))]
DbSession = Annotated[Session, Depends(get_db)]

_PROVIDER_NOT_FOUND = "Provider not found"
_SUBNET_CIDR = "10.100.0.0/16"  # NOSONAR


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
    project_prefix: str = ""
    # When false, registering an ocpvirt/kubevirt provider does NOT auto-create
    # a host. The dedicated-CI role sets this so it can create a correctly-sized
    # host explicitly (avoids a duplicate, wrongly sized, wasted host).
    auto_provision_host: bool = True
    # ocpvirt: HTTP package repo (basic auth) for host dnf. When set, hosts
    # install packages from it instead of mounting the RHEL DVD ISO.
    pkg_repo_url: str = ""
    pkg_repo_username: str = ""
    pkg_repo_password: str = ""

    # GCP fields
    gcp_project_id: str = ""
    service_account_json: str = ""

    # Azure fields
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_subscription_id: str = ""
    azure_location: str = ""

    # Libvirt "bring your own host" fields
    credentials: dict[str, Any] | None = None


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
    project_prefix: str | None = None
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


def _build_cluster_credentials(
    body: ProviderCreate, provider: Provider
) -> dict[str, Any]:
    """Build credentials for OCP Virt and KubeVirt cluster providers."""
    if not body.api_url or not body.token:
        label = "OCP Virt" if body.type == "ocpvirt" else "KubeVirt"
        raise HTTPException(
            status_code=400,
            detail=f"{label} providers require api_url and token",
        )
    api_host = body.api_url.replace("https://", "").replace("http://", "").split(":")[0]
    provider.console_base_domain = api_host.replace("api.", "apps.", 1)

    if body.type == "ocpvirt":
        creds: dict[str, Any] = {
            "api_url": body.api_url,
            "token": body.token,
            "namespace": body.namespace or "troshka",
            "verify_ssl": body.verify_ssl,
        }
        if body.iso_pvc is not None:
            creds["iso_pvc"] = body.iso_pvc
        if body.pkg_repo_url:
            creds["pkg_repo_url"] = body.pkg_repo_url
            creds["pkg_repo_username"] = body.pkg_repo_username
            creds["pkg_repo_password"] = body.pkg_repo_password
        provider.default_region = body.namespace or "troshka"
        return creds

    # kubevirt
    op_ns = body.namespace or "troshka-operator"
    provider.default_region = op_ns
    return {
        "api_url": body.api_url,
        "token": body.token,
        "namespace": op_ns,
        "verify_ssl": body.verify_ssl,
        "cache_namespace": body.cache_namespace or "troshka-cache",
        "project_prefix": body.project_prefix or "troshka-",
    }


def _build_cloud_credentials(
    body: ProviderCreate, provider: Provider
) -> dict[str, Any]:
    """Build credentials for GCP and Azure cloud providers."""
    if body.type == "gcp":
        if not body.gcp_project_id or not body.service_account_json:
            raise HTTPException(
                status_code=400,
                detail="GCP providers require gcp_project_id and service_account_json",
            )
        try:
            sa_json = json.loads(body.service_account_json)
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=400, detail="service_account_json must be valid JSON"
            )
        provider.gcp_project_id = body.gcp_project_id
        return {"service_account_json": sa_json}

    # azure
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
    provider.azure_subscription_id = body.azure_subscription_id
    provider.azure_location = body.azure_location or body.default_region or None
    return {
        "tenant_id": body.azure_tenant_id,
        "client_id": body.azure_client_id,
        "client_secret": body.azure_client_secret,
        "subscription_id": body.azure_subscription_id,
    }


def _build_provider_credentials(
    body: ProviderCreate, provider: Provider
) -> dict[str, Any]:
    """Build credentials dict and update provider fields based on type.

    Returns the credentials dict to be stored on the provider.
    Raises HTTPException on validation errors.
    """
    if body.type in ("ocpvirt", "kubevirt"):
        return _build_cluster_credentials(body, provider)

    if body.type in ("gcp", "azure"):
        return _build_cloud_credentials(body, provider)

    if body.type in ("ec2", "s3", "s3_readonly"):
        creds: dict[str, Any] = {
            "access_key_id": body.access_key_id,
            "secret_access_key": body.secret_access_key,
        }
        if body.bucket:
            creds["bucket"] = body.bucket
        if body.endpoint_url:
            creds["endpoint_url"] = body.endpoint_url
        return creds

    if body.type == "libvirt":
        ssh_private_key = (body.credentials or {}).get("ssh_private_key", "")
        if not ssh_private_key:
            raise HTTPException(
                status_code=400,
                detail="libvirt providers require credentials.ssh_private_key",
            )
        return {"ssh_private_key": ssh_private_key}

    raise HTTPException(400, f"Unknown provider type: {body.type}")


def _enqueue_cluster_host_provision(provider: Provider, db: Session) -> None:
    """Create a placeholder host and enqueue provisioning for cluster providers."""
    import uuid as _uuid

    from app.core.redis import enqueue_job
    from app.models.host import Host

    host_id = str(_uuid.uuid4())
    host = Host(
        id=host_id,
        provider_id=provider.id,
        instance_id="",
        instance_type="kubevirt-cluster",
        region=provider.default_region or "",
        state="provisioning",
        host_type="kubevirt-cluster",
        total_vcpus=0,
        total_ram_mb=0,
        ip_address="",
        agent_status="provisioning",
        storage_size_gb=0,
        max_eips=0,
    )
    db.add(host)
    db.commit()
    db.refresh(host)

    if provider.type == "ocpvirt":
        from app.workers.jobs import job_provision_ocpvirt_host

        enqueue_job(
            job_provision_ocpvirt_host,
            provider.id,
            host_id,
            queue_name="host_lifecycle",
        )
    elif provider.type == "kubevirt":
        from app.workers.jobs import job_provision_kubevirt

        enqueue_job(job_provision_kubevirt, provider.id, queue_name="host_lifecycle")


def _build_provider_response(
    provider: Provider,
    has_credentials: bool = False,
    endpoint_url: str | None = None,
    host_count: int | None = None,
) -> ProviderResponse:
    """Build a ProviderResponse from a Provider model instance."""
    if host_count is None:
        host_count = len(provider.hosts)
    if not has_credentials:
        has_credentials = bool(provider.credentials)
    if endpoint_url is None and provider.credentials:
        endpoint_url = provider.get_credentials().get("endpoint_url") or None
    return ProviderResponse(
        id=provider.id,
        name=provider.name,
        type=provider.type,
        default_region=provider.default_region,
        default_image=provider.default_image,
        vpc_id=provider.vpc_id,
        subnet_id=provider.subnet_id,
        security_group_id=provider.security_group_id,
        console_base_domain=provider.console_base_domain,
        console_nameservers=provider.console_nameservers,
        console_configured=bool(
            provider.console_zone_id or provider.console_base_domain
        ),
        iso_pvc=(
            provider.get_credentials().get("iso_pvc") if provider.credentials else None
        ),
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
        has_credentials=has_credentials,
        endpoint_url=endpoint_url,
        host_count=host_count,
        created_at=provider.created_at.isoformat() if provider.created_at else "",
    )


@router.get("/", response_model=list[ProviderResponse])
def list_providers(user: AdminUser, db: DbSession):
    providers = db.query(Provider).order_by(Provider.name).all()
    return [_build_provider_response(p) for p in providers]


@router.post(
    "/",
    response_model=ProviderResponse,
    status_code=201,
    responses={
        400: {"description": "Bad request"},
        409: {"description": "Provider name already exists"},
    },
)
def create_provider(
    body: ProviderCreate,
    user: AdminUser,
    db: DbSession,
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
    creds = _build_provider_credentials(body, provider)
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

    if body.type in ("ocpvirt", "kubevirt") and body.auto_provision_host:
        _enqueue_cluster_host_provision(provider, db)

    return _build_provider_response(
        provider,
        has_credentials=True,
        endpoint_url=str(creds.get("endpoint_url", "")) or None,
        host_count=0,
    )


def _update_provider_basic_fields(provider: Provider, body: ProviderUpdate) -> None:
    """Update basic provider fields from request body."""
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


def _update_cluster_credentials(provider: Provider, body: ProviderUpdate) -> None:
    """Update cluster provider credentials (OCP Virt/KubeVirt)."""
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
    if body.project_prefix:
        creds["project_prefix"] = body.project_prefix
    provider.set_credentials(creds)


def _update_aws_credentials(provider: Provider, body: ProviderUpdate) -> None:
    """Update AWS credentials (access key/secret)."""
    creds = provider.get_credentials()
    if body.access_key_id:
        creds["access_key_id"] = body.access_key_id
    if body.secret_access_key:
        creds["secret_access_key"] = body.secret_access_key
    provider.set_credentials(creds)


@router.patch(
    "/{provider_id}",
    response_model=ProviderResponse,
    responses={404: {"description": _PROVIDER_NOT_FOUND}},
)
def update_provider(
    provider_id: str,
    body: ProviderUpdate,
    user: AdminUser,
    db: DbSession,
):
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail=_PROVIDER_NOT_FOUND)

    _update_provider_basic_fields(provider, body)

    if body.api_url or body.token or body.namespace:
        _update_cluster_credentials(provider, body)
    elif body.access_key_id or body.secret_access_key:
        _update_aws_credentials(provider, body)

    db.commit()
    db.refresh(provider)

    return _build_provider_response(provider)


def _cleanup_kubevirt_k8s_resources(provider: Provider, creds: dict) -> Any | None:
    """Delete operator deployment, CRDs, and namespaces from the K8s cluster.

    Returns the core_api client (or None if connection failed).
    """
    core_api = None
    try:
        from app.services.providers.kubevirt import _get_k8s_clients

        _custom_api, core_api, api_client = _get_k8s_clients(provider)
        operator_ns = creds.get("namespace", "troshka-operator")
        cache_ns = creds.get("cache_namespace", "troshka-cache")

        from kubernetes import client as k8s_client

        apps_api = k8s_client.AppsV1Api(api_client)

        try:
            apps_api.delete_namespaced_deployment(
                name="troshka-operator", namespace=operator_ns
            )
        except Exception:
            pass
        try:
            core_api.delete_namespaced_service_account(
                name="troshka-operator", namespace=operator_ns
            )
        except Exception:
            pass

        ext_api = k8s_client.ApiextensionsV1Api(api_client)
        for crd_name in [
            "troshkaprojects.troshka.redhat.com",
            "troshkanetworks.troshka.redhat.com",
            "troshkavms.troshka.redhat.com",
        ]:
            try:
                ext_api.delete_custom_resource_definition(name=crd_name)
            except Exception:
                pass

        for ns in [operator_ns, cache_ns]:
            try:
                core_api.delete_namespace(name=ns)
            except Exception:
                pass

        logger.info(
            "Cleaned up kubevirt operator resources for provider %s",
            provider.id[:8],
        )
    except Exception as e:
        logger.warning(
            "Failed to clean up kubevirt resources for %s: %s",
            provider.id[:8],
            e,
        )
    return core_api


def _cleanup_kubevirt_db_resources(
    db: Session, provider: Provider, creds: dict, core_api: Any | None
) -> None:
    """Delete project namespaces and remove DB records for hosts/projects."""
    from app.models.host import Host
    from app.models.project import Project

    hosts = db.query(Host).filter_by(provider_id=provider.id).all()
    host_ids = [h.id for h in hosts]
    if host_ids:
        projects = db.query(Project).filter(Project.host_id.in_(host_ids)).all()
        for project in projects:
            try:
                prefix = creds.get("project_prefix", "troshka-")
                proj_ns = f"{prefix}{project.id[:8]}"
                if core_api is not None:
                    core_api.delete_namespace(name=proj_ns)
            except Exception:
                pass
            db.delete(project)

    for host in hosts:
        db.delete(host)


@router.delete(
    "/{provider_id}",
    status_code=204,
    responses={
        404: {"description": _PROVIDER_NOT_FOUND},
        409: {"description": "Provider has hosts"},
    },
)
def delete_provider(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
):
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail=_PROVIDER_NOT_FOUND)

    if provider.type == "kubevirt":
        creds = provider.get_credentials()
        core_api = _cleanup_kubevirt_k8s_resources(provider, creds)
        _cleanup_kubevirt_db_resources(db, provider, creds, core_api)
    elif provider.hosts:
        raise HTTPException(
            status_code=409, detail="Provider has hosts — remove them first"
        )

    db.delete(provider)
    db.commit()


@router.get(
    "/{provider_id}/discover-images",
    responses={
        404: {"description": _PROVIDER_NOT_FOUND},
        500: {"description": "Image discovery failed"},
    },
)
def discover_images(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
):
    """List available RHEL 9 and 10 images (both Access2/Gold and Hourly/Marketplace)."""
    import re

    import boto3

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail=_PROVIDER_NOT_FOUND)

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
@router.get(
    "/{provider_id}/discover-ami",
    responses={
        404: {"description": _PROVIDER_NOT_FOUND},
        500: {"description": "Image discovery failed"},
    },
)
def list_available_amis(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
):
    """Deprecated: use /discover-images instead."""
    return discover_images(provider_id, user, db)


@router.get(
    "/{provider_id}/discover-vpcs",
    responses={
        404: {"description": _PROVIDER_NOT_FOUND},
        500: {"description": "VPC discovery failed"},
    },
)
def discover_vpcs(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
):
    """List available VPCs and subnets in the provider's region."""
    import boto3

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail=_PROVIDER_NOT_FOUND)

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


@router.post(
    "/{provider_id}/create-vpc",
    responses={
        404: {"description": _PROVIDER_NOT_FOUND},
        500: {"description": "VPC creation failed"},
    },
)
def create_vpc(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
):
    """Create a new VPC with a public subnet for troshka hosts."""
    import boto3

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail=_PROVIDER_NOT_FOUND)

    creds = provider.get_credentials()
    try:
        ec2 = boto3.client(
            "ec2",
            region_name=provider.default_region,
            aws_access_key_id=creds.get("access_key_id"),
            aws_secret_access_key=creds.get("secret_access_key"),
        )

        vpc = ec2.create_vpc(CidrBlock=_SUBNET_CIDR)
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
            "cidr": _SUBNET_CIDR,
            "availability_zones": azs,
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("VPC creation failed for %s", provider.name)
        raise HTTPException(
            status_code=500, detail="VPC creation failed. Check server logs."
        )


@router.post(
    "/{provider_id}/setup-infra",
    responses={
        404: {"description": _PROVIDER_NOT_FOUND},
        500: {"description": "Infrastructure setup failed"},
    },
)
def setup_infrastructure(
    provider_id: str,
    vpc_id: str,
    subnet_id: str,
    user: AdminUser,
    db: DbSession,
):
    """Set VPC/subnet on the provider and ensure security group exists."""

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail=_PROVIDER_NOT_FOUND)

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


@router.post(
    "/{provider_id}/set-image",
    responses={404: {"description": _PROVIDER_NOT_FOUND}},
)
def set_image(
    provider_id: str,
    image_id: str,
    user: AdminUser,
    db: DbSession,
):
    """Set the default image for a provider."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail=_PROVIDER_NOT_FOUND)
    provider.default_image = image_id
    db.commit()
    return {"image_id": image_id}


# Backward-compatible alias for old endpoint name
@router.post(
    "/{provider_id}/set-ami",
    responses={404: {"description": _PROVIDER_NOT_FOUND}},
)
def set_ami(
    provider_id: str,
    ami_id: str,
    user: AdminUser,
    db: DbSession,
):
    """Deprecated: use /set-image instead."""
    return set_image(provider_id, ami_id, user, db)


@router.post(
    "/{provider_id}/set-iso",
    responses={404: {"description": _PROVIDER_NOT_FOUND}},
)
def set_iso(
    provider_id: str,
    iso_pvc: str,
    user: AdminUser,
    db: DbSession,
):
    """Set the install ISO PVC name for an OCP Virt provider."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail=_PROVIDER_NOT_FOUND)
    creds = provider.get_credentials()
    creds["iso_pvc"] = iso_pvc
    provider.set_credentials(creds)
    db.commit()
    return {"iso_pvc": iso_pvc}


@router.get(
    "/{provider_id}/discover-isos",
    responses={
        400: {"description": "Bad request"},
        404: {"description": _PROVIDER_NOT_FOUND},
    },
)
def discover_isos(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
):
    """List available ISO PVCs in the troshka namespace."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail=_PROVIDER_NOT_FOUND)
    if provider.type != "ocpvirt":
        raise HTTPException(
            status_code=400, detail="ISO discovery is only for OCP Virt"
        )

    creds = provider.get_credentials()
    try:
        from app.services.providers.ocpvirt import _get_k8s_clients

        _, core_api = _get_k8s_clients(creds)
        namespace = creds.get("namespace", "troshka")
        pvcs: Any = core_api.list_namespaced_persistent_volume_claim(
            namespace=namespace
        )
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


@router.get(
    "/{provider_id}/discover-datasources",
    responses={
        400: {"description": "Bad request"},
        404: {"description": _PROVIDER_NOT_FOUND},
    },
)
def discover_datasources(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
):
    """List available VM base images (DataSources) on an OCP Virt cluster."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail=_PROVIDER_NOT_FOUND)
    if provider.type != "ocpvirt":
        raise HTTPException(
            status_code=400,
            detail="DataSource discovery is only for OCP Virt providers",
        )

    creds = provider.get_credentials()
    try:
        from app.services.providers.ocpvirt import _get_k8s_clients

        custom_api, _ = _get_k8s_clients(creds)
        ds_list: dict[str, Any] = custom_api.list_namespaced_custom_object(  # type: ignore[assignment]
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


def _test_s3_provider(provider: Provider, creds: dict[str, Any]) -> dict[str, Any]:
    """Test S3 provider credentials and bucket access."""
    import boto3

    endpoint_url = creds.get("endpoint_url") or None
    s3 = boto3.client(
        "s3",
        region_name=provider.default_region,
        aws_access_key_id=creds.get("access_key_id"),
        aws_secret_access_key=creds.get("secret_access_key"),
        endpoint_url=endpoint_url,
    )
    bucket = creds.get("bucket", "troshka-images")

    # STS is an AWS-only API — skip it for self-hosted S3-compatible endpoints
    # (e.g. dev MinIO), which don't implement it and would otherwise hang
    # trying to reach real AWS.
    account_id = ""
    if not endpoint_url:
        sts = boto3.client(
            "sts",
            region_name=provider.default_region,
            aws_access_key_id=creds.get("access_key_id"),
            aws_secret_access_key=creds.get("secret_access_key"),
        )
        account_id = sts.get_caller_identity()["Account"]

    owner_kwargs = {"ExpectedBucketOwner": account_id} if account_id else {}
    try:
        s3.head_bucket(Bucket=bucket, **owner_kwargs)
        return {"status": "ok", "bucket": bucket, "account": account_id}
    except s3.exceptions.ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "404":
            return {
                "status": "ok",
                "bucket_missing": True,
                "bucket": bucket,
                "account": account_id,
                "message": f"Credentials OK but bucket '{bucket}' does not exist. Click Create Bucket.",
            }
        if code == "403":
            return {
                "status": "ok",
                "bucket_denied": True,
                "bucket": bucket,
                "account": account_id,
                "message": f"Credentials OK but no access to bucket '{bucket}'.",
            }
        raise


def _test_kubevirt_provider(
    provider: Provider, creds: dict[str, Any]
) -> dict[str, Any]:
    """Test KubeVirt provider: cluster connectivity, operator, CRDs, namespaces."""
    from app.services.providers.kubevirt import _get_k8s_clients as _get_kv_clients

    custom_api, core_api, api_client = _get_kv_clients(provider)
    nodes: Any = core_api.list_node()
    node_count = len(nodes.items)

    operator_ns = creds.get("namespace", "troshka-operator")
    operator_status = _check_operator_deployment(custom_api, core_api, operator_ns)
    crds_installed, crds_status = _check_crds_installed(api_client)

    cache_ns = creds.get("cache_namespace", "troshka-cache")
    ns_checks = _ensure_namespaces(
        core_api, [(operator_ns, "operator"), (cache_ns, "cache")]
    )

    return {
        "status": "ok",
        "cluster": creds.get("api_url", ""),
        "nodes": node_count,
        "operator": operator_status,
        "crds": crds_status,
        "crds_installed": crds_installed,
        "namespaces": ns_checks,
    }


def _check_operator_deployment(custom_api: Any, core_api: Any, operator_ns: str) -> str:
    """Check if the troshka-operator deployment is running."""
    try:
        core_api.read_namespace(operator_ns)
        deps: dict[str, Any] = custom_api.list_namespaced_custom_object(  # type: ignore[assignment]
            group="apps",
            version="v1",
            namespace=operator_ns,
            plural="deployments",
        )
        for dep in deps.get("items", []):
            if dep["metadata"]["name"] == "troshka-operator":
                ready = dep.get("status", {}).get("readyReplicas", 0)
                return f"running ({ready} replica)" if ready > 0 else "not ready"
        return "namespace exists, deployment missing"
    except Exception:
        return "not installed"


def _check_crds_installed(api_client: Any) -> tuple[bool, str]:
    """Check if Troshka CRDs are installed on the cluster."""
    from kubernetes import client as k8s_client
    from kubernetes.client.exceptions import ApiException as K8sApiException

    try:
        ext_api = k8s_client.ApiextensionsV1Api(api_client)
        ext_api.read_custom_resource_definition("troshkaprojects.troshka.redhat.com")
        return True, "installed"
    except K8sApiException as e:
        if e.status == 403:
            return False, "no permission (SA needs apiextensions.k8s.io access)"
        return False, "missing"
    except Exception:
        return False, "missing"


def _ensure_namespaces(
    core_api: Any, namespaces: list[tuple[str, str]]
) -> dict[str, str]:
    """Check or create namespaces, returning status per label."""
    ns_checks: dict[str, str] = {}
    for ns_name, ns_label in namespaces:
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
            except Exception:
                ns_checks[ns_label] = "no access"
    return ns_checks


def _test_libvirt_provider(creds: dict[str, Any]) -> dict[str, Any]:
    """Test a libvirt "bring your own host" provider's SSH private key.

    Unlike the cloud providers, libvirt has no API to call — the only stored
    credential is the SSH private key used to adopt hosts, so "testing" it
    just means confirming it's present and parses as a valid private key.
    """
    from cryptography.hazmat.primitives.serialization import load_ssh_private_key

    private_key = creds.get("ssh_private_key", "")
    if not private_key:
        raise HTTPException(
            status_code=400,
            detail="Provider credentials are missing 'ssh_private_key'",
        )
    try:
        key = load_ssh_private_key(private_key.encode(), password=None)
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"ssh_private_key is not a valid private key: {e}"
        )
    return {
        "status": "ok",
        "key_type": type(key).__name__,
        "message": "SSH private key is valid",
    }


@router.post(
    "/{provider_id}/test",
    responses={
        400: {"description": "Bad request"},
        404: {"description": _PROVIDER_NOT_FOUND},
    },
)
def test_provider(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
):
    """Test provider credentials by calling the provider's API."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail=_PROVIDER_NOT_FOUND)

    creds = provider.get_credentials()
    try:
        if provider.type == "s3":
            return _test_s3_provider(provider, creds)
        if provider.type == "ocpvirt":
            from app.services.providers.ocpvirt import _get_k8s_clients

            _, core_api = _get_k8s_clients(creds)
            ns = creds.get("namespace", "troshka")
            core_api.read_namespace(ns)
            nodes: Any = core_api.list_node()
            return {
                "status": "ok",
                "cluster": creds.get("api_url", ""),
                "namespace": ns,
                "nodes": len(nodes.items),
            }
        if provider.type == "kubevirt":
            return _test_kubevirt_provider(provider, creds)
        if provider.type == "gcp":
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
        if provider.type == "azure":
            from azure.identity import ClientSecretCredential
            from azure.mgmt.resource import ResourceManagementClient  # type: ignore[attr-defined]

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
        if provider.type == "ec2":
            import boto3

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
        if provider.type == "libvirt":
            return _test_libvirt_provider(creds)
        raise HTTPException(400, f"Unknown provider type: {provider.type}")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Provider test failed for %s", provider.name)
        raise HTTPException(status_code=400, detail="Credentials test failed")


@router.post(
    "/{provider_id}/create-bucket",
    responses={
        400: {"description": "Bad request"},
        404: {"description": _PROVIDER_NOT_FOUND},
        500: {"description": "Bucket creation failed"},
    },
)
def create_s3_bucket(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
):
    """Create the S3 bucket for a storage provider."""
    import boto3

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail=_PROVIDER_NOT_FOUND)
    if provider.type != "s3":
        raise HTTPException(status_code=400, detail="Not an S3 provider")

    creds = provider.get_credentials()
    bucket = creds.get("bucket", "troshka-images")

    s3 = boto3.client(
        "s3",
        region_name=provider.default_region,
        aws_access_key_id=creds.get("access_key_id"),
        aws_secret_access_key=creds.get("secret_access_key"),
        endpoint_url=creds.get("endpoint_url") or None,
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


@router.post(
    "/{provider_id}/install-operator",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": _PROVIDER_NOT_FOUND},
        500: {"description": "Operator install failed"},
    },
)
def install_operator(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
):
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail=_PROVIDER_NOT_FOUND)
    if provider.type != "kubevirt":
        raise HTTPException(
            status_code=400, detail="Install operator is only for kubevirt providers"
        )

    try:
        from app.services.providers.kubevirt import _deploy_operator

        _deploy_operator(provider)
    except Exception as e:
        err_str = str(e)
        if "Forbidden" in err_str and (
            "clusterroles" in err_str or "clusterrolebindings" in err_str
        ):
            api_url = provider.get_credentials().get("api_url", "")
            raise HTTPException(
                status_code=403,
                detail=(
                    "The service account lacks permission to create ClusterRoles. "
                    "An OCP admin must run these commands first:\n\n"
                    f"oc login {api_url}\n"
                    "oc apply -f src/operator/deploy/clusterrole.yaml\n"
                    "oc apply -f src/operator/deploy/clusterrolebinding.yaml\n\n"
                    "Then click Install Operator again."
                ),
            )
        logger.exception(
            "Failed to install operator for %s", sanitize_log(provider_id[:8])
        )
        raise HTTPException(status_code=500, detail=f"Failed to install operator: {e}")

    from app.models.host import Host
    from app.services.providers import get_provider_driver

    drv = get_provider_driver(provider)
    try:
        capacity = drv.provision_host(
            provider=provider,
            host_id="capacity-check",
            instance_type="kubevirt-cluster",
            storage_size_gb=0,
        )
    except Exception:
        capacity = {"total_vcpus": 0, "total_ram_mb": 0}

    host = db.query(Host).filter_by(provider_id=provider.id).first()
    if host:
        host.total_vcpus = capacity.get("total_vcpus", 0)
        host.total_ram_mb = capacity.get("total_ram_mb", 0)
        host.storage_size_gb = capacity.get("storage_size_gb", 0)
        host.agent_status = "connected"
        db.commit()
    else:
        import uuid as _uuid

        host = Host(
            id=str(_uuid.uuid4()),
            provider_id=provider.id,
            instance_id=provider.get_credentials().get("api_url", ""),
            instance_type="kubevirt-cluster",
            region=provider.default_region or "",
            state="active",
            host_type="kubevirt-cluster",
            total_vcpus=capacity.get("total_vcpus", 0),
            total_ram_mb=capacity.get("total_ram_mb", 0),
            ip_address=provider.get_credentials()
            .get("api_url", "")
            .replace("https://", "")
            .split(":")[0],
            agent_status="connected",
            agent_token=provider.get_credentials().get("token", ""),
            storage_size_gb=capacity.get("storage_size_gb", 0),
            max_eips=0,
        )
        db.add(host)
        db.commit()

    return {"status": "ok", "message": "Operator installed successfully"}


@router.get(
    "/{provider_id}/availability-zones",
    responses={
        400: {"description": "Bad request"},
        404: {"description": _PROVIDER_NOT_FOUND},
    },
)
def list_availability_zones(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
):
    """List available AZs in the provider's region."""
    import boto3

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail=_PROVIDER_NOT_FOUND)
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


@router.post(
    "/{provider_id}/setup-console",
    responses={
        400: {"description": "Bad request"},
        404: {"description": _PROVIDER_NOT_FOUND},
        500: {"description": "Console setup failed"},
    },
)
def setup_console(
    provider_id: str,
    req: ConsoleSetupRequest,
    user: AdminUser,
    db: DbSession,
):
    """Set up console infrastructure for direct VNC proxy."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail=_PROVIDER_NOT_FOUND)

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
            logger.info(
                "Created hosted zone %s for %s", zone_id, sanitize_log(base_domain)
            )

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
        logger.exception(
            "Failed to setup console for provider %s", sanitize_log(provider_id)
        )
        raise HTTPException(status_code=500, detail=str(e))


def _delete_dns_records(r53_client: Any, zone_id: str) -> None:
    """Delete all A and CNAME records in a Route53 hosted zone."""
    paginator = r53_client.get_paginator("list_resource_record_sets")
    changes = []
    for page in paginator.paginate(HostedZoneId=zone_id):
        for rrs in page["ResourceRecordSets"]:
            if rrs["Type"] in ("A", "CNAME"):
                changes.append({"Action": "DELETE", "ResourceRecordSet": rrs})
    if changes:
        for i in range(0, len(changes), 100):
            r53_client.change_resource_record_sets(
                HostedZoneId=zone_id,
                ChangeBatch={"Changes": changes[i : i + 100]},
            )


def _delete_hosted_zone_if_unused(
    db: Session, provider_id: str, zone_id: str, creds: dict[str, Any]
) -> None:
    """Delete Route53 hosted zone if no other providers share it."""
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
            import boto3

            r53 = boto3.client(
                "route53",
                aws_access_key_id=creds.get("access_key_id"),
                aws_secret_access_key=creds.get("secret_access_key"),
            )
            _delete_dns_records(r53, zone_id)
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


def _clear_console_config(db: Session, provider: Provider) -> None:
    """Clear console configuration from provider and all its hosts."""
    from app.models.host import Host

    hosts = db.query(Host).filter_by(provider_id=provider.id).all()
    for h in hosts:
        h.console_domain = None
    provider.console_zone_id = None
    provider.console_base_domain = None
    provider.console_nameservers = None


@router.delete(
    "/{provider_id}/console",
    responses={
        400: {"description": "Bad request"},
        404: {"description": _PROVIDER_NOT_FOUND},
    },
)
def delete_console(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
):
    """Remove console DNS configuration and hosted zone."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail=_PROVIDER_NOT_FOUND)
    if not provider.console_zone_id:
        raise HTTPException(status_code=400, detail="Console not configured")

    creds = provider.get_credentials()
    zone_id = provider.console_zone_id

    _delete_hosted_zone_if_unused(db, provider_id, zone_id, creds)
    _clear_console_config(db, provider)
    db.commit()

    return {"status": "removed"}


@router.post(
    "/{provider_id}/create-network-gcp",
    responses={404: {"description": "GCP provider not found"}},
)
def create_network_gcp(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
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
        ("troshka-allow-wireguard", "udp", ["51820-51850"]),
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


def _is_valid_gcp_image(img: Any, skip_terms: tuple[str, ...]) -> bool:
    """Check if a GCP image meets filter criteria."""
    name = img.name or ""
    if not name.startswith(("rhel-9", "rhel-10")):
        return False
    if "lvm" not in name:
        return False
    if any(s in name for s in skip_terms):
        return False
    if img.deprecated and img.deprecated.state == "DEPRECATED":
        return False
    return True


def _build_gcp_image_prefix(name: str, source: str) -> str:
    """Build prefix key for tracking latest version of each GCP image family."""
    parts = name.rsplit("-v", 1)
    return f"{source}:{parts[0]}" if len(parts) == 2 else f"{source}:{name}"


def _collect_gcp_images(
    images_client: Any, image_project: str, source: str, skip_terms: tuple[str, ...]
) -> dict[str, dict]:
    """Collect latest GCP images by prefix."""
    latest_by_prefix: dict[str, dict] = {}
    try:
        for img in images_client.list(project=image_project):
            if not _is_valid_gcp_image(img, skip_terms):
                continue

            name = img.name or ""
            prefix = _build_gcp_image_prefix(name, source)
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
    return latest_by_prefix


@router.get(
    "/{provider_id}/discover-images-gcp",
    responses={404: {"description": "GCP provider not found"}},
)
def discover_images_gcp(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
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
    skip_terms = ("arm64", "eus", "sap", "baremetal")

    latest_by_prefix = _collect_gcp_images(
        images_client, "rhel-cloud", "PAYG", skip_terms
    )

    results = sorted(
        latest_by_prefix.values(),
        key=lambda x: x["creation_timestamp"],
        reverse=True,
    )
    return results


@router.post(
    "/{provider_id}/create-network-azure",
    responses={404: {"description": "Azure provider not found"}},
)
def create_network_azure(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
):
    """Create a Resource Group, VNet, subnet, and NSG for an Azure provider."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider or provider.type != "azure":
        raise HTTPException(status_code=404, detail="Azure provider not found")

    from azure.identity import ClientSecretCredential
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.resource import ResourceManagementClient  # type: ignore[attr-defined]

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
    rg_params: Any = {"location": location}
    resource_client.resource_groups.create_or_update(rg_name, rg_params)

    network_client = NetworkManagementClient(credential, subscription_id)

    # Create NSG with rules
    nsg_params: Any = {
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
            {
                "name": "troshka-allow-wireguard",
                "priority": 140,
                "direction": "Inbound",
                "access": "Allow",
                "protocol": "Udp",
                "source_address_prefix": "VirtualNetwork",
                "source_port_range": "*",
                "destination_address_prefix": "VirtualNetwork",
                "destination_port_range": "51820-51850",
            },
        ],
    }
    nsg_poller = network_client.network_security_groups.begin_create_or_update(
        rg_name, "troshka-nsg", nsg_params
    )
    nsg = nsg_poller.result()

    # Create VNet with subnet
    vnet_params: Any = {
        "location": location,
        "address_space": {"address_prefixes": [_SUBNET_CIDR]},
        "subnets": [
            {
                "name": "troshka-subnet",
                "address_prefix": "10.100.1.0/24",
                "network_security_group": {"id": nsg.id},
            }
        ],
    }
    vnet_poller = network_client.virtual_networks.begin_create_or_update(
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


def _is_valid_azure_sku(sku_name: str, all_skus: Any) -> bool:
    """Check if an Azure SKU meets filter criteria."""
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
        return False
    if "lvm" not in sku_name:
        return False
    # Skip non-gen2 if gen2 variant exists
    if not sku_name.endswith("-gen2") and any(
        (sku_name + "-gen2") == s.name for s in all_skus
    ):
        return False
    return True


def _build_azure_image_result(
    publisher: str, offer: str, sku_name: str, latest_image: Any, source: str
) -> dict[str, str]:
    """Build result dict for an Azure image."""
    urn = f"{publisher}:{offer}:{sku_name}:{latest_image.name}"
    rhel_version = sku_name.split("-")[0] if sku_name[0].isdigit() else sku_name
    return {
        "name": sku_name,
        "urn": urn,
        "version": latest_image.name,
        "source": source,
        "rhel_version": rhel_version,
    }


def _collect_azure_images_for_offer(
    compute_client: Any,
    location: str,
    publisher: str,
    offer: str,
    source: str,
) -> list[dict[str, str]]:
    """Collect Azure images for a single publisher/offer combination."""
    results = []
    try:
        skus = compute_client.virtual_machine_images.list_skus(
            location, publisher, offer
        )
        for sku in skus:
            sku_name = sku.name or ""
            if not _is_valid_azure_sku(sku_name, skus):
                continue
            try:
                images = compute_client.virtual_machine_images.list(
                    location, publisher, offer, sku_name
                )
                if images:
                    latest = images[-1]
                    results.append(
                        _build_azure_image_result(
                            publisher, offer, sku_name, latest, source
                        )
                    )
            except Exception:
                pass
    except Exception as e:
        logger.warning("Failed to list Azure images for %s/%s: %s", publisher, offer, e)
    return results


@router.get(
    "/{provider_id}/discover-images-azure",
    responses={404: {"description": "Azure provider not found"}},
)
def discover_images_azure(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
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

    offers = [("redhat", "RHEL", "PAYG")]
    for publisher, offer, source in offers:
        results.extend(
            _collect_azure_images_for_offer(
                compute_client, location, publisher, offer, source
            )
        )

    return results


@router.post(
    "/{provider_id}/build-image",
    responses={
        400: {"description": "Bad request"},
        404: {"description": _PROVIDER_NOT_FOUND},
        409: {"description": "Build already in progress"},
    },
)
def build_image(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
    body: dict[str, Any] | None = None,
):
    from app.services import image_builder_service

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail=_PROVIDER_NOT_FOUND)
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

    from app.core.redis import enqueue_job

    enqueue_job(
        image_builder_service.build_host_image,
        provider_id,
        user.id,
        rhel_version,
        queue_name="host_lifecycle",
    )

    return {"status": "started", "message": f"Building {rhel_version} image..."}


@router.get("/{provider_id}/build-image/status")
def build_image_status(
    provider_id: str,
    user: AdminUser,
):
    from app.services import image_builder_service

    return image_builder_service.get_build_status(provider_id)


@router.delete("/{provider_id}/build-image/status", status_code=204)
def clear_build_image_status(
    provider_id: str,
    user: AdminUser,
):
    from app.services import image_builder_service

    image_builder_service.clear_build_status(provider_id)


@router.get(
    "/{provider_id}/operator-status",
    responses={404: {"description": _PROVIDER_NOT_FOUND}},
)
def get_operator_status(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
):
    from app.services.operator_updater import (
        _fetch_registry_digest,
        _get_operator_info,
    )

    provider = db.get(Provider, provider_id)
    if not provider:
        raise HTTPException(404, _PROVIDER_NOT_FOUND)
    running, rolling_out, tag = _get_operator_info(provider)
    registry = _fetch_registry_digest(tag)

    if rolling_out:
        up_to_date: bool | None = True
    elif running and registry:
        up_to_date = running == registry
    else:
        up_to_date = None

    return {
        "operator_digest": running[:20] if running else None,
        "registry_digest": registry[:20] if registry else None,
        "up_to_date": up_to_date,
        "rolling_out": rolling_out,
    }


@router.post(
    "/{provider_id}/update-operator",
    responses={
        400: {"description": "Not a kubevirt provider"},
        404: {"description": _PROVIDER_NOT_FOUND},
    },
)
def update_operator_endpoint(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
):
    provider = db.get(Provider, provider_id)
    if not provider:
        raise HTTPException(404, _PROVIDER_NOT_FOUND)
    if provider.type != "kubevirt":
        raise HTTPException(400, "Not a kubevirt provider")

    from app.services.operator_updater import update_operator

    return update_operator(provider)
