import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.host import Host
from app.models.provider import Provider
from app.models.user import User
from app.schemas.host import HostResponse
from app.services.provisioner import resize_instance

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hosts", tags=["hosts"])


class ProvisionRequest(BaseModel):
    provider_id: str
    instance_type: str | None = None
    region: str | None = None
    image_id: str | None = None
    storage_pool_id: str | None = None


@router.get("/expected-agent-version")
def get_expected_agent_version():
    import hashlib

    troshkad_path = os.path.join(
        os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        ),
        "troshkad",
        "troshkad.py",
    )
    with open(troshkad_path, "rb") as f:
        return {"version": hashlib.sha256(f.read()).hexdigest()[:12]}


@router.get("/overcommit")
def get_overcommit():
    from app.services.placement import _get_overcommit_ratios

    cpu, ram = _get_overcommit_ratios()
    return {"cpu_ratio": cpu, "ram_ratio": ram}


@router.get("/", response_model=list[HostResponse])
def list_hosts(
    region: str | None = None,
    user: User = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    from app.services.eip_service import get_host_eip_usage
    from app.services.placement import sync_host_capacity

    query = db.query(Host).filter(
        Host.state != "terminated", Host.host_type != "pattern_buffer"
    )
    if region:
        query = query.filter(Host.region == region)
    hosts = query.order_by(Host.region, Host.created_at).all()
    for host in hosts:
        sync_host_capacity(db, host)
    db.commit()
    from app.models.project import Project

    results = []
    for host in hosts:
        resp = HostResponse.model_validate(host)
        resp.used_eips = get_host_eip_usage(db, host.id)
        all_projects = (
            db.query(Project)
            .filter(
                Project.host_id == host.id,
                Project.state.in_(["active", "deployed", "stopped", "draft"]),
            )
            .all()
        )
        running = [p for p in all_projects if p.state in ("active", "deployed")]
        resp.running_projects = len(running)
        resp.total_projects = len(
            [p for p in all_projects if p.state in ("active", "deployed", "stopped")]
        )
        resp.running_vms = sum(
            len(
                [
                    n
                    for n in (p.deployed_topology or {}).get("nodes", [])
                    if n.get("type") == "vmNode"
                ]
            )
            for p in running
        )
        resp.total_vms = sum(
            len(
                [
                    n
                    for n in (p.deployed_topology or {}).get("nodes", [])
                    if n.get("type") == "vmNode"
                ]
            )
            for p in all_projects
            if p.deployed_topology
        )
        if host.provider_id:
            from app.models.provider import Provider

            prov = db.query(Provider).filter_by(id=host.provider_id).first()
            if prov:
                resp.provider_type = prov.type
                from app.services.agent_deployer import (
                    get_provider_ssh_port,
                    get_provider_ssh_user,
                )

                try:
                    resp.ssh_port = get_provider_ssh_port(prov.type)
                    resp.ssh_user = get_provider_ssh_user(prov.type)
                except ValueError:
                    pass
        results.append(resp)
    return results


@router.get("/storage")
def host_storage(
    user: User = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Get live disk usage for all active hosts."""
    from app.services.troshkad_client import check_disk_usage

    hosts = (
        db.query(Host)
        .filter(Host.state == "active", Host.agent_status == "connected")
        .all()
    )
    result = {}
    for h in hosts:
        try:
            disk = check_disk_usage(h)
        except Exception:
            continue
        if disk.get("error"):
            continue
        partitions = disk.get("partitions")
        if partitions:
            result[h.id] = {"partitions": partitions}
        elif "used_pct" in disk:
            result[h.id] = {
                "used_pct": disk["used_pct"],
                "free_gb": round(disk["free_bytes"] / (1024**3), 1),
                "total_gb": round(disk["total_bytes"] / (1024**3), 1),
            }
    return result


@router.get("/summary")
def host_summary(
    user: User = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Summary of host pool by region."""
    from app.services.placement import get_allocatable

    hosts = (
        db.query(Host)
        .filter(Host.state != "terminated", Host.host_type != "pattern_buffer")
        .all()
    )
    regions: dict[str, dict] = {}
    for h in hosts:
        alloc_vcpus, alloc_ram = get_allocatable(h)
        r = h.region or "unknown"
        if r not in regions:
            regions[r] = {
                "region": r,
                "total_hosts": 0,
                "active_hosts": 0,
                "total_vcpus": 0,
                "alloc_vcpus": 0,
                "used_vcpus": 0,
                "total_ram_mb": 0,
                "alloc_ram_mb": 0,
                "used_ram_mb": 0,
            }
        regions[r]["total_hosts"] += 1
        if h.state == "active":
            regions[r]["active_hosts"] += 1
        regions[r]["total_vcpus"] += h.total_vcpus
        regions[r]["alloc_vcpus"] += alloc_vcpus
        regions[r]["used_vcpus"] += h.used_vcpus
        regions[r]["total_ram_mb"] += h.total_ram_mb
        regions[r]["alloc_ram_mb"] += alloc_ram
        regions[r]["used_ram_mb"] += h.used_ram_mb
    return list(regions.values())


@router.post("/", response_model=HostResponse, status_code=201)
def add_host(
    body: ProvisionRequest,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Provision a new host and add it to the pool."""
    provider = db.query(Provider).filter_by(id=body.provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    if provider.state != "active":
        raise HTTPException(status_code=400, detail="Provider is not active")
    if provider.type == "ec2":
        if not provider.default_image and not body.image_id:
            raise HTTPException(
                status_code=400,
                detail="No image configured — select an image on the provider first",
            )
        if not provider.vpc_id or not provider.subnet_id:
            raise HTTPException(
                status_code=400,
                detail="No VPC configured — run Setup VPC on the provider first",
            )

    pool = None
    subnet_override = None
    if body.storage_pool_id:
        from app.models.storage_pool import StoragePool

        pool = db.query(StoragePool).get(body.storage_pool_id)
        if not pool:
            raise HTTPException(status_code=404, detail="Storage pool not found")
        if pool.provider_id != provider.id:
            raise HTTPException(
                status_code=400, detail="Pool belongs to a different provider"
            )
        if pool.mode.startswith("shared") and pool.status != "available":
            raise HTTPException(
                status_code=400, detail=f"Pool is not available (status: {pool.status})"
            )
        subnet_override = pool.subnet_id

    region = body.region or provider.default_region
    if not region and provider.type == "ocpvirt":
        region = provider.name
    creds = provider.get_credentials()

    nfs_kwargs = {}
    if pool and pool.mode == "shared-fsx" and pool.fsx_dns_name:
        nfs_kwargs = {"nfs_server": pool.fsx_dns_name, "nfs_path": "/fsx"}
    elif pool and pool.mode in ("shared-byo", "shared-ceph-nfs") and pool.nfs_endpoint:
        parts = pool.nfs_endpoint.split(":", 1)
        nfs_kwargs = {
            "nfs_server": parts[0],
            "nfs_path": parts[1] if len(parts) > 1 else "/",
        }
        if pool.nfs_port:
            nfs_kwargs["nfs_port"] = pool.nfs_port

    try:
        import uuid as _uuid

        from app.services.providers import get_provider_driver

        driver = get_provider_driver(provider)
        result = driver.provision_host(
            provider=provider,
            host_id=str(_uuid.uuid4()),
            instance_type=body.instance_type,
            storage_size_gb=500,
            image_id=body.image_id or provider.default_image,
            region=region,
            vpc_id=provider.vpc_id,
            subnet_id=subnet_override or provider.subnet_id,
            security_group_id=provider.security_group_id,
            subnet_override=subnet_override,
            host_type="shared",
            **nfs_kwargs,
        )
    except Exception as e:
        logger.exception("Failed to provision host: %s", e)
        raise HTTPException(
            status_code=500, detail="Failed to provision host. Check server logs."
        )

    host = Host(
        id=result["host_id"],
        provider_id=provider.id,
        instance_id=result["instance_id"],
        instance_type=result["instance_type"],
        region=region,
        state="active",
        host_type="shared",
        total_vcpus=result["total_vcpus"],
        total_ram_mb=result["total_ram_mb"],
        ip_address=result["public_ip"],
        private_ip=result.get("private_ip"),
        agent_status="disconnected",
        key_pair_name=result.get("key_pair_name"),
        private_key=result.get("private_key"),
        storage_size_gb=result.get("storage_size_gb", 500),
        max_eips=result.get("max_eips", 0),
    )
    db.add(host)
    db.commit()
    db.refresh(host)

    if body.storage_pool_id:
        host.storage_pool_id = body.storage_pool_id
        db.commit()

    # Auto-install agent in background
    import threading

    provider_creds = creds  # Capture for use in thread
    provider_console_domain = provider.console_base_domain if provider else None
    provider_console_zone = provider.console_zone_id if provider else None
    provider_type = provider.type
    ssh_host = result.get("_ssh_host") or result.get("public_ip")
    ssh_port = result.get("_ssh_port", 22)
    agent_port = result.get("_agent_port", 31337)

    def _auto_install():
        from app.core.database import SessionLocal
        from app.services.agent_deployer import (
            deploy_agent,
            get_provider_data_disk,
            get_provider_ssh_user,
            wait_for_ssh,
        )

        s = SessionLocal()
        try:
            h = s.query(Host).filter_by(id=host.id).first()
            if not h or not h.private_key or not (h.ip_address or ssh_host):
                return
            h.agent_status = "waiting_ssh"
            s.commit()
            _ssh_user = get_provider_ssh_user(provider_type)
            _data_disk = get_provider_data_disk(provider_type)

            if not wait_for_ssh(
                ssh_host, h.private_key, port=ssh_port, ssh_user=_ssh_user
            ):
                h.agent_status = "install_failed"
                s.commit()
                return
            h.agent_status = "installing"
            s.commit()
            _sm = "shared" if nfs_kwargs.get("nfs_server") else "local"
            _ca_cert, _host_cert, _host_key = "", "", ""
            if _sm == "shared" and h.storage_pool_id and h.ip_address:
                from app.models.storage_pool import StoragePool
                from app.services.storage_pool_service import sign_host_cert

                _pool = s.query(StoragePool).filter_by(id=h.storage_pool_id).first()
                if _pool and _pool.ca_cert and _pool.ca_key:
                    _host_cert, _host_key = sign_host_cert(
                        _pool.ca_cert, _pool.ca_key, h.ip_address, h.private_ip or ""
                    )
                    _ca_cert = _pool.ca_cert
            result = deploy_agent(
                ssh_host or h.ip_address,
                h.private_key,
                h.id,
                storage_mode=_sm,
                nfs_server=nfs_kwargs.get("nfs_server", ""),
                nfs_path=nfs_kwargs.get("nfs_path", ""),
                nfs_port=nfs_kwargs.get("nfs_port", 0),
                ssh_port=ssh_port,
                ssh_user=_ssh_user,
                ca_cert=_ca_cert,
                host_cert=_host_cert,
                host_key=_host_key,
                console_domain=h.console_domain or "",
                vncd_no_tls=provider_type == "ocpvirt",
                data_disk_device=_data_disk,
            )
            h.agent_status = "connected" if result["success"] else "install_failed"

            # Store troshkad credentials
            creds = result.get("troshkad_credentials", {})
            if creds.get("token") and creds.get("fingerprint"):
                h.agent_token = creds["token"]
                h.agent_cert_fingerprint = creds["fingerprint"]
                logger.info("Stored troshkad credentials for host %s", h.id[:8])

            # Create console DNS/Route record
            if h.instance_id and h.ip_address:
                prov_obj = s.query(Provider).get(h.provider_id)
                if provider_console_domain:
                    from app.services.console_dns import console_domain_for_host
                    from app.services.providers import get_provider_driver

                    fqdn = console_domain_for_host(
                        h.instance_id, provider_console_domain
                    )
                    try:
                        drv = get_provider_driver(prov_obj)
                        result = drv.create_console_record(
                            prov_obj, h, fqdn, h.ip_address
                        )
                        h.console_domain = result if result else fqdn
                    except Exception as e:
                        logger.warning(
                            "Failed to create console record for %s: %s",
                            h.id[:8],
                            e,
                        )

            s.commit()
        except Exception:
            logger.exception("Auto-install failed for host %s", host.id)
        finally:
            s.close()

    threading.Thread(target=_auto_install, daemon=True, name="auto-install").start()

    return host


@router.get("/{host_id}", response_model=HostResponse)
def get_host(
    host_id: str,
    user: User = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    return host


@router.post("/{host_id}/install-agent")
def install_agent(
    host_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """SSH into the host and install the troshka agent. Runs async."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if not host.ip_address:
        raise HTTPException(status_code=400, detail="Host has no IP address")
    if not host.private_key:
        raise HTTPException(status_code=400, detail="No SSH key stored for this host")
    if host.agent_status in ("waiting_ssh", "installing"):
        raise HTTPException(status_code=409, detail="Agent install already in progress")

    host.agent_status = "waiting_ssh"
    db.commit()

    h_id = host.id
    h_ip = host.ip_address
    h_key = host.private_key

    import threading

    def _install():
        from app.core.database import SessionLocal
        from app.services.agent_deployer import (
            deploy_agent,
            get_provider_data_disk,
            get_provider_ssh_user,
            wait_for_ssh,
        )

        s = SessionLocal()
        try:
            h = s.query(Host).filter_by(id=h_id).first()
            if not h:
                return

            # Determine provider type for SSH user and disk device
            _provider_type = "ec2"  # default
            if h.provider_id:
                from app.models.provider import Provider as _Prov

                _prov = s.query(_Prov).get(h.provider_id)
                if _prov:
                    _provider_type = _prov.type

            _ssh_user = get_provider_ssh_user(_provider_type)
            _data_disk = get_provider_data_disk(_provider_type)

            ssh_ok = wait_for_ssh(h_ip, h_key, ssh_user=_ssh_user)
            if not ssh_ok:
                h.agent_status = "disconnected"
                s.commit()
                return
            h.agent_status = "installing"
            s.commit()

            _install_kwargs = {
                "ssh_user": _ssh_user,
                "data_disk_device": _data_disk,
                "vncd_no_tls": _provider_type == "ocpvirt",
            }
            if h.console_domain:
                _install_kwargs["console_domain"] = h.console_domain
            if h.storage_pool_id:
                from app.models.storage_pool import StoragePool as _SP2

                _pool = s.query(_SP2).get(h.storage_pool_id)
                if _pool and _pool.mode.startswith("shared"):
                    _install_kwargs["storage_mode"] = "shared"
                    if _pool.mode == "shared-fsx" and _pool.fsx_dns_name:
                        _install_kwargs["nfs_server"] = _pool.fsx_dns_name
                        _install_kwargs["nfs_path"] = "/fsx"
                    elif (
                        _pool.mode in ("shared-byo", "shared-ceph-nfs")
                        and _pool.nfs_endpoint
                    ):
                        _parts = _pool.nfs_endpoint.split(":", 1)
                        _install_kwargs["nfs_server"] = _parts[0]
                        _install_kwargs["nfs_path"] = (
                            _parts[1] if len(_parts) > 1 else "/"
                        )
                        if _pool.nfs_port:
                            _install_kwargs["nfs_port"] = _pool.nfs_port
                    if _pool.ca_cert and _pool.ca_key and h.ip_address:
                        from app.services.storage_pool_service import (
                            sign_host_cert as _shc2,
                        )

                        _hc, _hk = _shc2(
                            _pool.ca_cert,
                            _pool.ca_key,
                            h.ip_address,
                            h.private_ip or "",
                        )
                        _install_kwargs["ca_cert"] = _pool.ca_cert
                        _install_kwargs["host_cert"] = _hc
                        _install_kwargs["host_key"] = _hk
            result = deploy_agent(
                host_ip=h_ip, private_key=h_key, host_id=h_id, **_install_kwargs
            )
            h.agent_status = "connected" if result["success"] else "install_failed"

            # Store troshkad credentials FIRST (needed for health check below)
            creds = result.get("troshkad_credentials", {})
            if creds.get("token") and creds.get("fingerprint"):
                h.agent_token = creds["token"]
                h.agent_cert_fingerprint = creds["fingerprint"]
                logger.info("Stored troshkad credentials for host %s", h.id[:8])

            # Verify agent version and push update if source changed during reinstall
            if result["success"]:
                try:
                    import hashlib
                    import time

                    from app.services.troshkad_client import (
                        check_health,
                        push_update,
                        troshkad_request,
                    )

                    time.sleep(5)
                    health = troshkad_request(h, "GET", "/health", timeout=10)
                    if health and health.get("version"):
                        h.agent_version = health["version"]
                        logger.info(
                            "Agent install verified: host %s running version %s",
                            h.id[:8],
                            h.agent_version,
                        )

                        troshkad_path = os.path.join(
                            os.path.dirname(
                                os.path.dirname(
                                    os.path.dirname(
                                        os.path.dirname(os.path.abspath(__file__))
                                    )
                                )
                            ),
                            "troshkad",
                            "troshkad.py",
                        )
                        with open(troshkad_path, "rb") as _f:
                            current_hash = hashlib.sha256(_f.read()).hexdigest()[:12]
                        if h.agent_version != current_hash:
                            logger.info(
                                "Agent %s version %s != source %s, pushing update",
                                h.id[:8],
                                h.agent_version,
                                current_hash,
                            )
                            script_text = (
                                open(troshkad_path)
                                .read()
                                .replace(
                                    'VERSION = "dev"', f'VERSION = "{current_hash}"'
                                )
                            )
                            push_update(h, script_text.encode(), current_hash)
                            time.sleep(5)
                            health2 = check_health(h)
                            if health2 and health2.get("version"):
                                h.agent_version = health2["version"]
                                logger.info(
                                    "Agent %s updated to %s after reinstall",
                                    h.id[:8],
                                    h.agent_version,
                                )
                except Exception as _ve:
                    logger.warning(
                        "Could not verify agent version after install: %s", _ve
                    )

            s.commit()
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "Agent install failed for %s", h_id[:8]
            )
            h = s.query(Host).filter_by(id=h_id).first()
            if h:
                h.agent_status = "install_failed"
                s.commit()
        finally:
            s.close()

    threading.Thread(target=_install, daemon=True, name=f"install-{h_id[:8]}").start()
    return {"status": "installing"}


@router.get("/{host_id}/ssh-key")
def get_ssh_key(
    host_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Get the SSH private and public key for a host."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if not host.private_key:
        raise HTTPException(status_code=404, detail="No SSH key stored for this host")

    ssh_user = "ec2-user"
    ssh_port = 22
    if host.provider_id:
        prov = db.query(Provider).filter_by(id=host.provider_id).first()
        if prov and prov.type == "ocpvirt":
            ssh_user = "cloud-user"
            ssh_port = 22000
    ssh_cmd = None
    if host.ip_address:
        port_flag = f" -p {ssh_port}" if ssh_port != 22 else ""
        ssh_cmd = f"ssh -i <key-file>{port_flag} {ssh_user}@{host.ip_address}"

    result = {
        "key_pair_name": host.key_pair_name,
        "private_key": host.private_key,
        "ssh_command": ssh_cmd,
        "ssh_script_command": f"scripts/host-ssh.sh {host.id[:8]}",
    }

    # Derive public key from private key
    try:
        import subprocess

        proc = subprocess.run(
            ["ssh-keygen", "-y", "-f", "/dev/stdin"],
            input=host.private_key,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            result["public_key"] = proc.stdout.strip()
    except Exception:
        pass

    return result


@router.get("/{host_id}/ssh-key/download")
def download_ssh_key(
    host_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Download the SSH private key as a file."""
    from fastapi.responses import Response

    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if not host.private_key:
        raise HTTPException(status_code=404, detail="No SSH key stored for this host")

    filename = f"{host.key_pair_name or host_id}.txt"
    return Response(
        content=host.private_key,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{host_id}/poweroff")
def poweroff_host(
    host_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Stop the EC2 instance without terminating it."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if not host.instance_id:
        raise HTTPException(status_code=400, detail="No instance ID")
    # Check for running projects (not just allocated — stopped projects are OK to power off)
    from app.models.project import Project

    running_projects = (
        db.query(Project).filter_by(host_id=host.id, state="active").count()
    )
    if running_projects > 0:
        raise HTTPException(
            status_code=409, detail="Host has running projects — stop them first"
        )

    creds = None
    if host.provider_id:
        provider = db.query(Provider).filter_by(id=host.provider_id).first()
        if provider:
            creds = provider.get_credentials()

    if not host.provider:
        raise HTTPException(
            status_code=400, detail="No provider configured for this host"
        )

    # Set state immediately and do cloud API call in background
    host.state = "stopped"
    host.agent_status = "disconnected"
    host.ip_address = None
    from app.services.placement import sync_host_capacity

    sync_host_capacity(db, host)
    db.commit()

    import threading

    _host_id = host.id
    _instance_id = host.instance_id
    _provider_id = host.provider_id

    def _do_stop():
        from app.services.providers import get_provider_driver

        try:
            from app.core.database import SessionLocal

            s = SessionLocal()
            prov = s.query(Provider).filter_by(id=_provider_id).first()
            if prov:
                drv = get_provider_driver(prov)
                drv.stop_host(prov, _instance_id)
            s.close()
        except Exception:
            logger.exception("Background stop failed for host %s", _host_id[:8])

    threading.Thread(target=_do_stop, daemon=True, name=f"stop-{_host_id[:8]}").start()

    return {"status": "stopped"}


class PowerOnRequest(BaseModel):
    instance_type: str | None = None


@router.post("/{host_id}/poweron")
def poweron_host(
    host_id: str,
    body: PowerOnRequest | None = None,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Start a stopped EC2 instance, optionally changing instance type first."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if not host.instance_id:
        raise HTTPException(status_code=400, detail="No instance ID")

    creds = None
    if host.provider_id:
        provider = db.query(Provider).filter_by(id=host.provider_id).first()
        if provider:
            creds = provider.get_credentials()

    # Resize if a different instance type was requested
    new_type = body.instance_type if body else None
    if new_type and new_type != host.instance_type:
        if host.state != "stopped":
            raise HTTPException(
                status_code=409, detail="Host must be stopped to resize"
            )
        try:
            from app.services.providers import get_provider_driver

            drv = get_provider_driver(host.provider)
            result = drv.resize_host(host.provider, host.instance_id, new_type)
        except Exception:
            logger.exception("Failed to resize host %s before power on", host_id[:8])
            raise HTTPException(status_code=500, detail="Failed to resize instance")
        old_type = host.instance_type
        host.instance_type = result["instance_type"]
        host.total_vcpus = result["total_vcpus"]
        host.total_ram_mb = result["total_ram_mb"]
        host.max_eips = result["max_eips"]
        db.commit()
        logger.info(
            "Host %s resized %s → %s before power on", host_id[:8], old_type, new_type
        )

    from app.services.providers import get_provider_driver

    provider = (
        db.query(Provider).filter_by(id=host.provider_id).first()
        if host.provider_id
        else None
    )
    if not provider:
        raise HTTPException(
            status_code=400, detail="No provider configured for this host"
        )

    # Set state to starting immediately and do the actual cloud API call
    # in the background thread — cloud API calls can take 5-10 seconds
    # and would otherwise timeout the frontend fetch
    host.state = "starting"
    host.agent_status = "disconnected"
    db.commit()

    # Background: wait for running, update IP, reinstall agent
    host_id = host.id
    instance_id = host.instance_id

    import threading

    provider_id = host.provider_id
    provider_type = provider.type

    def _wait_and_reinstall():
        import time

        from app.core.database import SessionLocal
        from app.services.agent_deployer import (
            deploy_agent,
            get_provider_data_disk,
            get_provider_ssh_user,
            wait_for_ssh,
        )
        from app.services.providers import get_provider_driver as _get_drv

        s = SessionLocal()
        try:
            _prov = s.query(Provider).filter_by(id=provider_id).first()
            if not _prov:
                return
            _drv = _get_drv(_prov)

            # Wait for instance to fully stop if it's still shutting down
            for _ in range(60):
                st_check = _drv.get_host_status(_prov, instance_id)
                state_now = st_check["state"] if st_check else "unknown"
                if state_now in ("stopped", "deallocated", "terminated"):
                    break
                if state_now == "running":
                    break
                logger.info(
                    "Host %s in %s state, waiting for stop...",
                    host_id[:8],
                    state_now,
                )
                time.sleep(5)

            # Start the instance via cloud API
            if state_now != "running":
                try:
                    _drv.start_host(_prov, instance_id)
                except Exception as e:
                    logger.warning("start_host failed for %s: %s", host_id[:8], e)

            # Poll until instance is running (up to 5 min)
            deadline = time.time() + 300
            new_ip = None
            while time.time() < deadline:
                st = _drv.get_host_status(_prov, instance_id)
                if st and st.get("state") == "running":
                    new_ip = st.get("public_ip")
                    break
                time.sleep(10)

            h = s.query(Host).filter_by(id=host_id).first()
            if not h:
                return

            if not new_ip:
                logger.warning(
                    "Host %s never reached running state after power-on",
                    host_id[:8],
                )
                h.state = "stopped"
                h.agent_status = "disconnected"
                s.commit()
                return

            old_ip = h.ip_address
            h.state = "active"
            h.ip_address = new_ip
            h.private_ip = (st or {}).get("private_ip") or h.private_ip
            h.agent_status = "waiting_ssh"
            s.commit()

            if new_ip and new_ip != old_ip and h.console_domain:
                try:
                    prov = s.query(Provider).filter_by(id=h.provider_id).first()
                    if prov:
                        from app.services.providers import get_provider_driver

                        drv = get_provider_driver(prov)
                        drv.create_console_record(prov, h, h.console_domain, new_ip)
                        logger.info(
                            "Updated console DNS %s -> %s", h.console_domain, new_ip
                        )
                except Exception as e:
                    logger.warning(
                        "Failed to update console DNS for %s: %s", h.id[:8], e
                    )

            if not h.private_key or not h.ip_address:
                return

            # Determine provider type for SSH user and disk device
            _provider_type = "ec2"  # default
            if h.provider_id:
                from app.models.provider import Provider as _Prov2

                _prov2 = s.query(_Prov2).get(h.provider_id)
                if _prov2:
                    _provider_type = _prov2.type

            _ssh_user2 = get_provider_ssh_user(_provider_type)
            _data_disk2 = get_provider_data_disk(_provider_type)

            if not wait_for_ssh(
                h.ip_address, h.private_key, ssh_user=_ssh_user2, timeout=300
            ):
                h.agent_status = "disconnected"
                s.commit()
                logger.warning("Host %s SSH not available after power on", host_id[:8])
                return
            h.agent_status = "installing"
            s.commit()

            _kwargs = {
                "ssh_user": _ssh_user2,
                "data_disk_device": _data_disk2,
                "vncd_no_tls": _provider_type == "ocpvirt",
            }
            if h.console_domain:
                _kwargs["console_domain"] = h.console_domain
            if h.storage_pool_id:
                from app.models.storage_pool import StoragePool as _SP

                _pool = s.query(_SP).get(h.storage_pool_id)
                if _pool and _pool.mode.startswith("shared"):
                    _kwargs["storage_mode"] = "shared"
                    if _pool.mode == "shared-fsx" and _pool.fsx_dns_name:
                        _kwargs["nfs_server"] = _pool.fsx_dns_name
                        _kwargs["nfs_path"] = "/fsx"
                    elif (
                        _pool.mode in ("shared-byo", "shared-ceph-nfs")
                        and _pool.nfs_endpoint
                    ):
                        parts = _pool.nfs_endpoint.split(":", 1)
                        _kwargs["nfs_server"] = parts[0]
                        _kwargs["nfs_path"] = parts[1] if len(parts) > 1 else "/"
                        if _pool.nfs_port:
                            _kwargs["nfs_port"] = _pool.nfs_port
                    if _pool.ca_cert and _pool.ca_key and h.ip_address:
                        from app.services.storage_pool_service import (
                            sign_host_cert as _shc,
                        )

                        _hc, _hk = _shc(
                            _pool.ca_cert,
                            _pool.ca_key,
                            h.ip_address,
                            h.private_ip or "",
                        )
                        _kwargs["ca_cert"] = _pool.ca_cert
                        _kwargs["host_cert"] = _hc
                        _kwargs["host_key"] = _hk
            result = deploy_agent(h.ip_address, h.private_key, h.id, **_kwargs)
            h.agent_status = "connected" if result["success"] else "disconnected"

            # Store troshkad credentials
            troshkad_creds = result.get("troshkad_credentials", {})
            if troshkad_creds.get("token") and troshkad_creds.get("fingerprint"):
                h.agent_token = troshkad_creds["token"]
                h.agent_cert_fingerprint = troshkad_creds["fingerprint"]
                logger.info("Stored troshkad credentials for host %s", h.id[:8])

            s.commit()

            if result["success"]:
                from app.services.gc_service import reconcile_host

                gc_report = reconcile_host(host_id)
                logger.info(
                    "Host %s GC on connect: %s",
                    host_id[:8],
                    gc_report.get("orphans_found", 0),
                )
        except Exception:
            logger.exception("Power on failed for host %s", host_id)
            try:
                h = s.query(Host).filter_by(id=host_id).first()
                if h:
                    h.state = "active"
                    h.agent_status = "disconnected"
                    s.commit()
            except Exception:
                pass
        finally:
            s.close()

    threading.Thread(
        target=_wait_and_reinstall, daemon=True, name=f"reinstall-{host_id[:8]}"
    ).start()

    return {"status": "starting"}


@router.post("/{host_id}/resize")
def resize_host(
    host_id: str,
    body: dict,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Change the instance type of a stopped host."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if host.state != "stopped":
        raise HTTPException(status_code=409, detail="Host must be stopped to resize")
    new_type = body.get("instance_type")
    if not new_type or not isinstance(new_type, str):
        raise HTTPException(status_code=400, detail="instance_type is required")
    if new_type == host.instance_type:
        raise HTTPException(status_code=400, detail="Already that instance type")
    if not host.instance_id:
        raise HTTPException(status_code=400, detail="No EC2 instance associated")

    creds = None
    if host.provider_id:
        provider = db.query(Provider).filter_by(id=host.provider_id).first()
        if provider:
            creds = provider.get_credentials()

    try:
        result = resize_instance(host.instance_id, new_type, credentials=creds)
    except Exception:
        logger.exception("Failed to resize host %s", host_id[:8])
        raise HTTPException(status_code=500, detail="Failed to resize instance")

    old_type = host.instance_type
    host.instance_type = result["instance_type"]
    host.total_vcpus = result["total_vcpus"]
    host.total_ram_mb = result["total_ram_mb"]
    host.max_eips = result["max_eips"]
    db.commit()

    logger.info("Host %s resized %s → %s", host_id[:8], old_type, new_type)
    return {
        "status": "resized",
        "old_instance_type": old_type,
        "new_instance_type": result["instance_type"],
        "total_vcpus": result["total_vcpus"],
        "total_ram_mb": result["total_ram_mb"],
        "max_eips": result["max_eips"],
    }


@router.post("/{host_id}/resize-storage")
def resize_storage(
    host_id: str,
    body: dict,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Grow the dedicated EBS storage volume. XFS supports online resize."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    new_size = body.get("size_gb")
    if not new_size or not isinstance(new_size, int) or new_size < 1:
        raise HTTPException(status_code=400, detail="size_gb is required (integer)")
    if new_size <= host.storage_size_gb:
        raise HTTPException(
            status_code=400,
            detail=f"New size must be larger than current ({host.storage_size_gb} GB)",
        )
    if not host.instance_id:
        raise HTTPException(
            status_code=400, detail="No instance associated — cannot resize"
        )

    provider = host.provider
    if not provider or provider.type != "ec2":
        raise HTTPException(
            status_code=400,
            detail="Storage resize via this endpoint is only supported for AWS EC2 hosts",
        )
    creds = provider.get_credentials()

    from app.services.provisioner import _get_ec2_client

    ec2 = _get_ec2_client(credentials=creds)

    # Find the data volume (not root)
    volumes = ec2.describe_volumes(
        Filters=[
            {"Name": "attachment.instance-id", "Values": [host.instance_id]},
            {"Name": "attachment.device", "Values": ["/dev/sdf", "/dev/xvdf"]},
        ]
    )
    if not volumes["Volumes"]:
        raise HTTPException(
            status_code=404,
            detail="No data volume found on instance. Was it provisioned with dedicated storage?",
        )

    vol_id = volumes["Volumes"][0]["VolumeId"]
    try:
        ec2.modify_volume(VolumeId=vol_id, Size=new_size)
    except Exception as e:
        msg = str(e)
        if "ModificationState" in msg or "OPTIMIZING" in msg:
            raise HTTPException(
                status_code=409,
                detail="EBS volume is still optimizing from a previous resize. Try again in a few minutes.",
            )
        logger.exception("EBS modify_volume failed for %s", vol_id)
        raise HTTPException(
            status_code=500,
            detail="Failed to resize EBS volume. Check server logs for details.",
        )

    # Grow the filesystem on the host (XFS online grow)
    if host.ip_address and host.agent_status == "connected":
        from app.services.troshkad_client import start_job, wait_for_job

        job_id = start_job(host, "/host/resize-storage", {})
        job = wait_for_job(host, job_id, timeout=30)
        if job["status"] == "failed":
            raise HTTPException(
                status_code=500, detail=job["result"].get("error", "Resize failed")
            )

    old_size = host.storage_size_gb
    host.storage_size_gb = new_size
    db.commit()
    return {
        "status": "resized",
        "old_size_gb": old_size,
        "new_size_gb": new_size,
        "volume_id": vol_id,
    }


@router.post("/{host_id}/extend-storage")
def extend_storage(
    host_id: str,
    body: dict | None = None,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Auto-extend the host's storage volume by the configured increment."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if not host.instance_id:
        raise HTTPException(status_code=400, detail="No instance associated")

    increment_gb = (body or {}).get("increment_gb")

    try:
        if host.provider:
            from app.services.providers import get_provider_driver

            drv = get_provider_driver(host.provider)
            result = drv.extend_host_storage(
                host.provider, host, db, increment_gb=increment_gb
            )
        else:
            from app.services.storage_extend import extend_host_ebs

            result = extend_host_ebs(host, db, increment_gb=increment_gb)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@router.patch("/{host_id}")
def update_host(
    host_id: str,
    body: dict,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    allowed = {
        "auto_extend_enabled": bool,
        "auto_extend_threshold_pct": int,
        "auto_extend_increment_gb": int,
        "auto_extend_max_gb": (int, type(None)),
    }
    for key, val in body.items():
        if key not in allowed:
            raise HTTPException(status_code=400, detail=f"Cannot update field: {key}")
        if not isinstance(val, allowed[key]):
            raise HTTPException(status_code=400, detail=f"Invalid type for {key}")
        setattr(host, key, val)
    db.commit()
    db.refresh(host)
    return {"status": "updated"}


@router.delete("/{host_id}", status_code=204)
def remove_host(
    host_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Terminate the EC2 instance and remove the host from the pool."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")

    from app.models.project import Project

    running = (
        db.query(Project)
        .filter(
            Project.host_id == host.id,
            Project.state.in_(("active", "deploying", "reconfiguring", "starting")),
        )
        .count()
    )
    if running > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Host has {running} running project(s) — stop them first",
        )

    # Reset stopped projects to draft (host is going away)
    stopped = (
        db.query(Project)
        .filter(
            Project.host_id == host.id,
            Project.state.in_(("stopped", "error", "stopping")),
        )
        .all()
    )
    for p in stopped:
        p.state = "draft"
        p.host_id = None
        p.deployed_topology = None
        p.deploy_error = None
        p.vni_map = None
    if stopped:
        db.flush()
        logger.info(
            "Reset %d stopped projects to draft for host %s removal",
            len(stopped),
            host_id[:8],
        )

    # Get provider credentials for termination
    creds = None
    if host.provider_id:
        provider = db.query(Provider).filter_by(id=host.provider_id).first()
        if provider:
            creds = provider.get_credentials()

    # Mark as terminating first
    host.state = "terminating"
    db.commit()

    # Clean up console DNS/Route record
    if host.console_domain and host.ip_address:
        try:
            prov = (
                db.query(Provider).filter_by(id=host.provider_id).first()
                if host.provider_id
                else None
            )
            if prov:
                from app.services.providers import get_provider_driver

                drv = get_provider_driver(prov)
                drv.delete_console_record(
                    prov, host, host.console_domain, host.ip_address
                )
        except Exception as e:
            logger.warning("Failed to delete console record for %s: %s", host_id[:8], e)

    if host.instance_id:
        try:
            prov = (
                db.query(Provider).filter_by(id=host.provider_id).first()
                if host.provider_id
                else None
            )
            if prov:
                from app.services.providers import get_provider_driver

                drv = get_provider_driver(prov)
                drv.terminate_host(prov, host.instance_id)
            else:
                from app.services.provisioner import terminate_host

                terminate_host(host.instance_id, credentials=creds)
        except Exception as e:
            logger.exception("Failed to terminate host %s: %s", host_id, e)
            host.state = "active"
            db.commit()
            raise HTTPException(
                status_code=500, detail="Failed to terminate host. Check server logs."
            )

    host.state = "shutting_down"
    host.agent_status = "disconnected"
    db.commit()

    # OCP Virt: force-delete is immediate, just clean up the DB record
    if provider and provider.type == "ocpvirt":
        import time

        time.sleep(2)
        host.state = "terminated"
        db.commit()
        db.delete(host)
        db.commit()
        logger.info("Host %s terminated and removed (ocpvirt)", host_id[:8])
        return {"status": "terminated"}

    # Capture values before spawning thread (avoid DetachedInstanceError)
    instance_id = host.instance_id

    import threading

    provider_id = host.provider_id
    provider_type = provider.type if provider else "ec2"

    def _wait_terminated():
        import time

        from app.core.database import SessionLocal

        s = SessionLocal()
        try:
            prov = s.query(Provider).get(provider_id) if provider_id else None
            if prov:
                from app.services.providers import get_provider_driver

                drv = get_provider_driver(prov)
            else:
                drv = None

            for _ in range(60):
                time.sleep(5)
                status = (
                    drv.get_host_status(prov, instance_id) if drv and prov else None
                )
                h = s.query(Host).filter_by(id=host_id).first()
                if not h:
                    return
                if not status or status["state"] == "terminated":
                    if h.key_pair_name and prov:
                        try:
                            drv.delete_key_pair(prov, h.key_pair_name)
                        except Exception:
                            pass
                    h.state = "terminated"
                    s.commit()
                    time.sleep(10)
                    s.delete(h)
                    s.commit()
                    logger.info("Host %s terminated and removed", host_id)
                    return
                elif status["state"] == "shutting-down":
                    h.state = "shutting_down"
                    s.commit()
            logger.warning("Timeout waiting for host %s to terminate", host_id)
        except Exception:
            logger.exception("Error tracking termination for %s", host_id)
        finally:
            s.close()

    threading.Thread(
        target=_wait_terminated, daemon=True, name=f"terminate-{host_id[:8]}"
    ).start()


@router.get("/{host_id}/gc/preview")
def gc_preview(
    host_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Dry-run garbage collection — show what would be cleaned."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")

    from app.services.gc_service import reconcile_host

    return reconcile_host(host_id, dry_run=True)


@router.post("/{host_id}/gc")
def gc_run(
    host_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Run garbage collection on a host."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if not host.ip_address or host.agent_status != "connected":
        raise HTTPException(
            status_code=400, detail="Host must be active with agent connected"
        )

    from app.services.gc_service import reconcile_host

    return reconcile_host(host_id, dry_run=False)


@router.post("/{host_id}/wipe")
def wipe_host(
    host_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Destroy all projects on a host and clean up all resources. Nuclear option."""
    from app.models.project import Project
    from app.services.deploy_service import destroy_project_sync
    from app.services.troshkad_client import TroshkadError, start_job, wait_for_job

    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")

    results = {"projects_reset": 0, "projects_destroyed": 0, "cleanup": {}}

    projects = db.query(Project).filter_by(host_id=host_id).all()
    for p in projects:
        if p.state in ("active", "stopped"):
            try:
                destroy_project_sync(
                    {
                        "project_id": p.id,
                        "host_id": p.host_id,
                        "vni_map": p.vni_map or {},
                        "topology": p.deployed_topology or p.topology or {},
                        "dns_provider_id": p.dns_provider_id,
                        "domain": p.domain,
                    }
                )
                results["projects_destroyed"] += 1
            except Exception:
                logger.warning("Failed to destroy project %s during wipe", p.id[:8])
        elif p.state in ("deploying", "error") and p.vni_map:
            try:
                from app.services.deploy_service import _teardown_networks_via_troshkad

                _teardown_networks_via_troshkad(host, p.id, p.vni_map)
                jid = start_job(
                    host, "/files/remove", {"paths": [f"/var/lib/troshka/vms/{p.id}"]}
                )
                wait_for_job(host, jid, timeout=15)
            except Exception:
                logger.warning("Failed to teardown project %s during wipe", p.id[:8])
        p.state = "draft"
        p.deploy_error = None
        p.deployed_topology = None
        results["projects_reset"] += 1
    db.commit()

    if host.agent_status == "connected" and host.agent_token:
        try:
            job_id = start_job(
                host,
                "/gc/discover",
                {
                    "known_project_ids": [],
                    "known_domains": [],
                },
            )
            job = wait_for_job(host, job_id, timeout=30)
            if job["status"] == "completed":
                orphans = job["result"]
                job_id = start_job(
                    host,
                    "/gc/clean",
                    {
                        "orphan_dirs": orphans.get("orphan_dirs", []),
                        "orphan_domains": orphans.get("orphan_domains", []),
                        "orphan_bridges": orphans.get("orphan_bridges", []),
                        "orphan_namespaces": orphans.get("orphan_namespaces", []),
                        "cache_items": [],  # preserve image/pattern cache — only regular GC cleans stale cache
                    },
                )
                cleanup_job = wait_for_job(host, job_id, timeout=120)
                results["cleanup"] = cleanup_job.get("result", {})

            # Flush all troshka nftables chains from host namespace
            try:
                nft_job_id = start_job(host, "/host/nft-reset", {})
                nft_job = wait_for_job(host, nft_job_id, timeout=15)
                results["nft_reset"] = nft_job.get("result", {})
            except TroshkadError:
                pass
        except TroshkadError as e:
            results["cleanup"] = {"error": str(e)}

    from app.services.placement import sync_host_capacity

    sync_host_capacity(db, host)
    db.commit()

    return results


@router.post("/{host_id}/update-agent")
def update_agent(
    host_id: str,
    force: bool = False,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Push a troshkad update to a host."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if host.agent_status != "connected":
        raise HTTPException(status_code=400, detail="Agent not connected")
    if not host.agent_token:
        raise HTTPException(status_code=400, detail="No agent credentials")

    if not force:
        from app.models.project import Project

        active_deploys = (
            db.query(Project)
            .filter(
                Project.host_id == host_id,
                Project.state == "deploying",
            )
            .count()
        )
        if active_deploys:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot update agent: {active_deploys} deploy(s) running on this host. Use force=true to override.",
            )

    # Read the current troshkad.py
    import os

    troshkad_path = os.path.join(
        os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        ),
        "troshkad",
        "troshkad.py",
    )
    if not os.path.exists(troshkad_path):
        raise HTTPException(status_code=500, detail="troshkad.py not found on server")

    with open(troshkad_path, "rb") as f:
        script_bytes = f.read()

    import hashlib

    version = hashlib.sha256(script_bytes).hexdigest()[:12]

    if not force and host.agent_version == version:
        return {"status": "up_to_date", "version": version}

    # Stamp the version into the script before pushing
    script_text = script_bytes.decode()
    script_text = script_text.replace(
        'VERSION = "dev"',
        f'VERSION = "{version}"',
    )
    script_bytes = script_text.encode()

    # Push update in background thread
    import threading

    def _push():
        from app.core.database import SessionLocal
        from app.services.troshkad_client import (
            TroshkadError,
            check_health,
            push_update,
        )

        s = SessionLocal()
        try:
            h = s.query(Host).filter_by(id=host_id).first()
            if not h:
                return
            try:
                old_version = h.agent_version
                push_update(h, script_bytes, version, force=force)
                import time

                # Wait for agent to go down (drain + restart)
                for _ in range(10):
                    time.sleep(2)
                    health = check_health(h)
                    if not health:
                        break
                # Wait for agent to come back with new version
                for _ in range(30):
                    time.sleep(5)
                    health = check_health(h)
                    if health:
                        new_ver = health.get("version", "")
                        h.agent_version = new_ver
                        s.commit()
                        if new_ver != old_version:
                            logger.info(
                                "Host %s updated troshkad %s → %s",
                                host_id[:8],
                                old_version,
                                new_ver,
                            )
                        else:
                            logger.info(
                                "Host %s troshkad restarted (same version %s)",
                                host_id[:8],
                                new_ver,
                            )
                        return
                logger.warning(
                    "Host %s update: agent did not come back after 150s", host_id[:8]
                )
            except TroshkadError as e:
                logger.error("Host %s update failed: %s", host_id[:8], e)
        except Exception:
            logger.exception("Update agent failed for host %s", host_id)
        finally:
            s.close()

    threading.Thread(
        target=_push, daemon=True, name=f"update-agent-{host_id[:8]}"
    ).start()
    return {"status": "updating", "version": version, "force": force}


@router.post("/{host_id}/evacuate")
def evacuate_host_endpoint(
    host_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    from app.models.project import Project
    from app.models.storage_pool import StoragePool
    from app.services.migration_service import evacuate_host

    host = db.query(Host).get(host_id)
    if not host:
        raise HTTPException(404, "Host not found")
    if not host.storage_pool_id:
        raise HTTPException(400, "Host is not in a storage pool")

    pool = db.query(StoragePool).get(host.storage_pool_id)
    if pool.mode == "local":
        raise HTTPException(400, "Cannot evacuate hosts in local-mode pools")

    project_count = (
        db.query(Project)
        .filter(Project.host_id == host_id, Project.state == "active")
        .count()
    )
    if project_count == 0:
        raise HTTPException(400, "No active projects to evacuate")

    evacuate_host(host_id)
    return {"status": "evacuating", "host_id": host_id, "project_count": project_count}
