import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.config import config
from app.core.database import get_db
from app.models.host import Host
from app.models.provider import Provider
from app.models.user import User
from app.schemas.host import HostResponse
from app.services.provisioner import provision_host, terminate_host

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hosts", tags=["hosts"])


class ProvisionRequest(BaseModel):
    provider_id: str
    instance_type: str | None = None
    region: str | None = None
    ami_id: str | None = None


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
    query = db.query(Host)
    if region:
        query = query.filter(Host.region == region)
    hosts = query.order_by(Host.region, Host.created_at).all()
    results = []
    for host in hosts:
        resp = HostResponse.model_validate(host)
        resp.used_eips = get_host_eip_usage(db, host.id)
        results.append(resp)
    return results


@router.get("/storage")
def host_storage(user: User = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Get live disk usage for all active hosts."""
    from app.services.troshkad_client import check_disk_usage
    hosts = db.query(Host).filter(Host.state == "active", Host.agent_status == "connected").all()
    result = {}
    for h in hosts:
        disk = check_disk_usage(h)
        if disk.get("error"):
            continue
        result[h.id] = {
            "used_pct": disk["used_pct"],
            "free_gb": round(disk["free_bytes"] / (1024 ** 3), 1),
            "total_gb": round(disk["total_bytes"] / (1024 ** 3), 1),
        }
    return result


@router.get("/summary")
def host_summary(user: User = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Summary of host pool by region."""
    from app.services.placement import get_allocatable

    hosts = db.query(Host).all()
    regions: dict[str, dict] = {}
    for h in hosts:
        alloc_vcpus, alloc_ram = get_allocatable(h)
        r = h.region or "unknown"
        if r not in regions:
            regions[r] = {"region": r, "total_hosts": 0, "active_hosts": 0, "total_vcpus": 0, "alloc_vcpus": 0, "used_vcpus": 0, "total_ram_mb": 0, "alloc_ram_mb": 0, "used_ram_mb": 0}
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
def add_host(body: ProvisionRequest, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Provision a new EC2 host and add it to the pool."""
    provider = db.query(Provider).filter_by(id=body.provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    if provider.state != "active":
        raise HTTPException(status_code=400, detail="Provider is not active")
    if not provider.default_ami and not body.ami_id:
        raise HTTPException(status_code=400, detail="No AMI configured — run Discover AMI on the provider first")
    if not provider.vpc_id or not provider.subnet_id:
        raise HTTPException(status_code=400, detail="No VPC configured — run Setup VPC on the provider first")

    region = body.region or provider.default_region
    creds = provider.get_credentials()

    try:
        result = provision_host(
            instance_type=body.instance_type,
            ami_id=body.ami_id or provider.default_ami,
            region=region,
            credentials=creds,
            vpc_id=provider.vpc_id,
            subnet_id=provider.subnet_id,
            security_group_id=provider.security_group_id,
        )
    except Exception as e:
        logger.exception("Failed to provision host: %s", e)
        raise HTTPException(status_code=500, detail="Failed to provision host. Check server logs.")

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
        agent_status="disconnected",
        key_pair_name=result.get("key_pair_name"),
        private_key=result.get("private_key"),
        storage_size_gb=result.get("storage_size_gb", 500),
        max_eips=result.get("max_eips", 0),
    )
    db.add(host)
    db.commit()
    db.refresh(host)

    # Auto-install agent in background
    import threading
    def _auto_install():
        from app.core.database import SessionLocal
        from app.services.agent_deployer import wait_for_ssh, deploy_agent
        s = SessionLocal()
        try:
            h = s.query(Host).filter_by(id=host.id).first()
            if not h or not h.private_key or not h.ip_address:
                return
            h.agent_status = "waiting_ssh"
            s.commit()
            if not wait_for_ssh(h.ip_address, h.private_key):
                h.agent_status = "install_failed"
                s.commit()
                return
            h.agent_status = "installing"
            s.commit()
            result = deploy_agent(h.ip_address, h.private_key, h.id)
            h.agent_status = "connected" if result["success"] else "install_failed"

            # Store troshkad credentials
            creds = result.get("troshkad_credentials", {})
            if creds.get("token") and creds.get("fingerprint"):
                h.agent_token = creds["token"]
                h.agent_cert_fingerprint = creds["fingerprint"]
                logger.info("Stored troshkad credentials for host %s", h.id[:8])

            s.commit()
        except Exception:
            logger.exception("Auto-install failed for host %s", host.id)
        finally:
            s.close()

    threading.Thread(target=_auto_install, daemon=True).start()

    return host


@router.get("/{host_id}", response_model=HostResponse)
def get_host(host_id: str, user: User = Depends(require_role("operator")), db: Session = Depends(get_db)):
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    return host


@router.post("/{host_id}/install-agent")
def install_agent(host_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
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
        from app.services.agent_deployer import wait_for_ssh, deploy_agent
        s = SessionLocal()
        try:
            h = s.query(Host).filter_by(id=h_id).first()
            if not h:
                return
            ssh_ok = wait_for_ssh(h_ip, h_key)
            if not ssh_ok:
                h.agent_status = "disconnected"
                s.commit()
                return
            h.agent_status = "installing"
            s.commit()
            result = deploy_agent(host_ip=h_ip, private_key=h_key, host_id=h_id)
            h.agent_status = "connected" if result["success"] else "install_failed"

            # Store troshkad credentials
            creds = result.get("troshkad_credentials", {})
            if creds.get("token") and creds.get("fingerprint"):
                h.agent_token = creds["token"]
                h.agent_cert_fingerprint = creds["fingerprint"]
                logger.info("Stored troshkad credentials for host %s", h.id[:8])

            s.commit()
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Agent install failed for %s", h_id[:8])
            h = s.query(Host).filter_by(id=h_id).first()
            if h:
                h.agent_status = "install_failed"
                s.commit()
        finally:
            s.close()

    threading.Thread(target=_install, daemon=True).start()
    return {"status": "installing"}


@router.get("/{host_id}/ssh-key")
def get_ssh_key(host_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Get the SSH private and public key for a host."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if not host.private_key:
        raise HTTPException(status_code=404, detail="No SSH key stored for this host")

    result = {
        "key_pair_name": host.key_pair_name,
        "private_key": host.private_key,
        "ssh_command": f"ssh -i <key-file> ec2-user@{host.ip_address}" if host.ip_address else None,
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
def download_ssh_key(host_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
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
def poweroff_host(host_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Stop the EC2 instance without terminating it."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if not host.instance_id:
        raise HTTPException(status_code=400, detail="No instance ID")
    # Check for running projects (not just allocated — stopped projects are OK to power off)
    from app.models.project import Project
    running_projects = db.query(Project).filter_by(host_id=host.id, state="active").count()
    if running_projects > 0:
        raise HTTPException(status_code=409, detail="Host has running projects — stop them first")

    creds = None
    if host.provider_id:
        provider = db.query(Provider).filter_by(id=host.provider_id).first()
        if provider:
            creds = provider.get_credentials()

    from app.services.provisioner import _get_ec2_client
    client = _get_ec2_client(credentials=creds)
    client.stop_instances(InstanceIds=[host.instance_id])

    host.state = "stopped"
    host.agent_status = "disconnected"
    host.ip_address = None
    db.commit()
    return {"status": "stopped"}


@router.post("/{host_id}/poweron")
def poweron_host(host_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Start a stopped EC2 instance."""
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

    from app.services.provisioner import _get_ec2_client, get_host_status
    client = _get_ec2_client(credentials=creds)

    # Check actual EC2 state first
    status = get_host_status(host.instance_id, credentials=creds)
    ec2_state = status["state"] if status else "unknown"

    if ec2_state == "running":
        # Already running — just update DB and reinstall agent
        host.state = "active"
        host.ip_address = status.get("public_ip")
        host.agent_status = "disconnected"
        db.commit()
    elif ec2_state in ("stopped", "stopping"):
        if ec2_state == "stopped":
            client.start_instances(InstanceIds=[host.instance_id])
        host.state = "starting"
        db.commit()
    else:
        raise HTTPException(status_code=409, detail=f"Instance is in unexpected state: {ec2_state}")

    # Background: wait for running, update IP, reinstall agent
    host_id = host.id
    instance_id = host.instance_id

    import threading
    def _wait_and_reinstall():
        from app.core.database import SessionLocal
        from app.services.provisioner import _get_ec2_client as get_client
        from app.services.agent_deployer import wait_for_ssh, deploy_agent
        s = SessionLocal()
        try:
            # Wait for instance running
            ec2 = get_client(credentials=creds)
            waiter = ec2.get_waiter("instance_running")
            waiter.wait(InstanceIds=[instance_id])
            desc = ec2.describe_instances(InstanceIds=[instance_id])
            inst = desc["Reservations"][0]["Instances"][0]

            h = s.query(Host).filter_by(id=host_id).first()
            if not h:
                return
            h.state = "active"
            h.ip_address = inst.get("PublicIpAddress")
            h.agent_status = "waiting_ssh"
            s.commit()

            if not h.private_key or not h.ip_address:
                return
            if not wait_for_ssh(h.ip_address, h.private_key):
                h.agent_status = "install_failed"
                s.commit()
                return
            h.agent_status = "installing"
            s.commit()
            result = deploy_agent(h.ip_address, h.private_key, h.id)
            h.agent_status = "connected" if result["success"] else "install_failed"

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
                logger.info("Host %s GC on connect: %s", host_id[:8], gc_report.get("orphans_found", 0))
        except Exception:
            logger.exception("Power on failed for host %s", host_id)
            try:
                h = s.query(Host).filter_by(id=host_id).first()
                if h:
                    h.state = "active"
                    h.agent_status = "install_failed"
                    s.commit()
            except Exception:
                pass
        finally:
            s.close()

    threading.Thread(target=_wait_and_reinstall, daemon=True).start()

    return {"status": "starting"}


@router.post("/{host_id}/resize-storage")
def resize_storage(host_id: str, body: dict, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Grow the dedicated EBS storage volume. XFS supports online resize."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    new_size = body.get("size_gb")
    if not new_size or not isinstance(new_size, int) or new_size < 1:
        raise HTTPException(status_code=400, detail="size_gb is required (integer)")
    if new_size <= host.storage_size_gb:
        raise HTTPException(status_code=400, detail=f"New size must be larger than current ({host.storage_size_gb} GB)")
    if not host.instance_id:
        raise HTTPException(status_code=400, detail="No EC2 instance associated — cannot resize")

    provider = host.provider
    creds = None
    if provider:
        creds = {"access_key_id": provider.access_key_id, "secret_access_key": provider.secret_access_key}

    from app.services.provisioner import _get_ec2_client
    ec2 = _get_ec2_client(credentials=creds)

    # Find the data volume (not root)
    volumes = ec2.describe_volumes(Filters=[
        {"Name": "attachment.instance-id", "Values": [host.instance_id]},
        {"Name": "attachment.device", "Values": ["/dev/sdf", "/dev/xvdf"]},
    ])
    if not volumes["Volumes"]:
        raise HTTPException(status_code=404, detail="No data volume found on instance. Was it provisioned with dedicated storage?")

    vol_id = volumes["Volumes"][0]["VolumeId"]
    ec2.modify_volume(VolumeId=vol_id, Size=new_size)

    # Grow the filesystem on the host (XFS online grow)
    if host.ip_address and host.agent_status == "connected":
        from app.services.troshkad_client import start_job, wait_for_job
        job_id = start_job(host, "/host/resize-storage", {})
        job = wait_for_job(host, job_id, timeout=30)
        if job["status"] == "failed":
            raise HTTPException(status_code=500, detail=job["result"].get("error", "Resize failed"))

    host.storage_size_gb = new_size
    db.commit()
    return {"status": "resized", "old_size_gb": host.storage_size_gb, "new_size_gb": new_size, "volume_id": vol_id}


@router.delete("/{host_id}", status_code=204)
def remove_host(host_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Terminate the EC2 instance and remove the host from the pool."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if host.used_vcpus > 0:
        raise HTTPException(status_code=409, detail="Host has active projects — drain first")

    # Get provider credentials for termination
    creds = None
    if host.provider_id:
        provider = db.query(Provider).filter_by(id=host.provider_id).first()
        if provider:
            creds = provider.get_credentials()

    # Mark as terminating first
    host.state = "terminating"
    db.commit()

    if host.instance_id:
        try:
            terminate_host(host.instance_id, credentials=creds)
        except Exception as e:
            logger.exception("Failed to terminate host %s: %s", host_id, e)
            host.state = "active"
            db.commit()
            raise HTTPException(status_code=500, detail="Failed to terminate host. Check server logs.")

    host.state = "shutting_down"
    host.agent_status = "disconnected"
    db.commit()

    # Capture values before spawning thread (avoid DetachedInstanceError)
    instance_id = host.instance_id

    import threading
    def _wait_terminated():
        from app.core.database import SessionLocal
        from app.services.provisioner import get_host_status, _get_ec2_client
        import time
        s = SessionLocal()
        try:
            for _ in range(60):
                time.sleep(5)
                status = get_host_status(instance_id, credentials=creds)
                h = s.query(Host).filter_by(id=host_id).first()
                if not h:
                    return
                if not status or status["state"] == "terminated":
                    # Clean up key pair
                    if h.key_pair_name:
                        try:
                            client = _get_ec2_client(credentials=creds)
                            client.delete_key_pair(KeyName=h.key_pair_name)
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

    threading.Thread(target=_wait_terminated, daemon=True).start()


@router.get("/{host_id}/gc/preview")
def gc_preview(host_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Dry-run garbage collection — show what would be cleaned."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")

    from app.services.gc_service import reconcile_host
    return reconcile_host(host_id, dry_run=True)


@router.post("/{host_id}/gc")
def gc_run(host_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Run garbage collection on a host."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if not host.ip_address or host.agent_status != "connected":
        raise HTTPException(status_code=400, detail="Host must be active with agent connected")

    from app.services.gc_service import reconcile_host
    return reconcile_host(host_id, dry_run=False)


@router.post("/{host_id}/wipe")
def wipe_host(host_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Destroy all projects on a host and clean up all resources. Nuclear option."""
    from app.models.project import Project
    from app.services.deploy_service import destroy_project_sync
    from app.services.troshkad_client import start_job, wait_for_job, TroshkadError

    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")

    results = {"projects_reset": 0, "projects_destroyed": 0, "cleanup": {}}

    projects = db.query(Project).filter_by(host_id=host_id).all()
    for p in projects:
        if p.state in ("active", "stopped"):
            try:
                destroy_project_sync(p.id)
                results["projects_destroyed"] += 1
            except Exception:
                logger.warning("Failed to destroy project %s during wipe", p.id[:8])
        p.state = "draft"
        p.deploy_error = None
        p.deployed_topology = None
        results["projects_reset"] += 1
    db.commit()

    if host.agent_status == "connected" and host.agent_token:
        try:
            job_id = start_job(host, "/gc/discover", {
                "known_project_ids": [],
                "known_domains": [],
            })
            job = wait_for_job(host, job_id, timeout=30)
            if job["status"] == "completed":
                orphans = job["result"]
                job_id = start_job(host, "/gc/clean", {
                    "orphan_dirs": orphans.get("orphan_dirs", []),
                    "orphan_domains": orphans.get("orphan_domains", []),
                    "orphan_bridges": orphans.get("orphan_bridges", []),
                    "orphan_namespaces": orphans.get("orphan_namespaces", []),
                    "cache_items": [c.get("path", c) if isinstance(c, dict) else c for c in orphans.get("cache_items", [])],
                })
                cleanup_job = wait_for_job(host, job_id, timeout=120)
                results["cleanup"] = cleanup_job.get("result", {})
        except TroshkadError as e:
            results["cleanup"] = {"error": str(e)}

    from app.services.placement import sync_host_capacity
    sync_host_capacity(db, host)
    db.commit()

    return results


@router.post("/{host_id}/update-agent")
def update_agent(host_id: str, force: bool = False, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Push a troshkad update to a host."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if host.agent_status != "connected":
        raise HTTPException(status_code=400, detail="Agent not connected")
    if not host.agent_token:
        raise HTTPException(status_code=400, detail="No agent credentials")

    # Read the current troshkad.py
    import os
    troshkad_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))), "troshkad", "troshkad.py")
    if not os.path.exists(troshkad_path):
        raise HTTPException(status_code=500, detail="troshkad.py not found on server")

    with open(troshkad_path, "rb") as f:
        script_bytes = f.read()

    # Extract version from script
    version = "unknown"
    for line in script_bytes.decode().split("\n"):
        if line.startswith("VERSION"):
            version = line.split("=")[1].strip().strip('"').strip("'")
            break

    # Push update in background thread
    import threading
    def _push():
        from app.core.database import SessionLocal
        from app.services.troshkad_client import push_update, check_health, TroshkadError
        s = SessionLocal()
        try:
            h = s.query(Host).filter_by(id=host_id).first()
            if not h:
                return
            try:
                push_update(h, script_bytes, version, force=force)
                # Wait for restart and verify new version
                import time
                for _ in range(30):
                    time.sleep(5)
                    health = check_health(h)
                    if health and health.get("version") == version:
                        h.agent_version = version
                        s.commit()
                        logger.info("Host %s updated to troshkad %s", host_id[:8], version)
                        return
                logger.warning("Host %s update: did not confirm version %s", host_id[:8], version)
            except TroshkadError as e:
                logger.error("Host %s update failed: %s", host_id[:8], e)
        except Exception:
            logger.exception("Update agent failed for host %s", host_id)
        finally:
            s.close()

    threading.Thread(target=_push, daemon=True).start()
    return {"status": "updating", "version": version, "force": force}
