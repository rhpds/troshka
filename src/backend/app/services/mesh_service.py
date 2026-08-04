from __future__ import annotations

import base64
import threading

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.core.encryption import decrypt, encrypt
from app.models.mesh_peer import ProjectMeshPeer
from app.models.project import Project

_WG_PORT_BASE = 51820
_WG_PORT_MAX = 51850
_MESH_SUBNET_PREFIX = "10.252"

_subnet_lock = threading.Lock()


def generate_wireguard_keypair() -> tuple[str, str]:
    private = X25519PrivateKey.generate()
    private_bytes = private.private_bytes_raw()
    public_bytes = private.public_key().public_bytes_raw()
    return (
        base64.b64encode(private_bytes).decode(),
        base64.b64encode(public_bytes).decode(),
    )


def allocate_mesh_subnet(db: Session) -> int:
    with _subnet_lock:
        max_id = db.query(sa_func.max(Project.mesh_subnet_id)).scalar() or 0
        return max_id + 1


def allocate_wg_port(db: Session, host_id: str) -> int:
    used_ports = {
        row[0]
        for row in db.query(ProjectMeshPeer.wg_port)
        .filter(ProjectMeshPeer.host_id == host_id)
        .all()
    }
    for port in range(_WG_PORT_BASE, _WG_PORT_MAX + 1):
        if port not in used_ports:
            return port
    raise RuntimeError(
        f"No available WireGuard ports on host {host_id} "
        f"(range {_WG_PORT_BASE}-{_WG_PORT_MAX})"
    )


def create_mesh_peers(
    db: Session,
    project_id: str,
    host_assignments: dict[str, list[str]],
    network_host_id: str,
    host_ips: dict[str, str],
) -> list[ProjectMeshPeer]:
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise ValueError(f"Project {project_id} not found")

    with _subnet_lock:
        max_id = db.query(sa_func.max(Project.mesh_subnet_id)).scalar() or 0
        subnet_id = max_id + 1
        project.mesh_subnet_id = subnet_id
        db.flush()

    project.mesh_network_host_id = network_host_id
    flat_assignments = {}
    for hid, vm_ids in host_assignments.items():
        for vm_id in vm_ids:
            flat_assignments[vm_id] = hid
    project.host_assignments = flat_assignments

    peers = []
    for idx, host_id in enumerate(sorted(host_assignments.keys()), start=1):
        private_key, public_key = generate_wireguard_keypair()
        wg_port = allocate_wg_port(db, host_id)
        if host_id not in host_ips:
            raise ValueError(f"Missing host IP for host {host_id}")
        host_ip = host_ips[host_id]

        peer = ProjectMeshPeer(
            project_id=project_id,
            host_id=host_id,
            peer_type="troshkad",
            wg_public_key=public_key,
            wg_private_key=encrypt(private_key),
            wg_endpoint=f"{host_ip}:{wg_port}",
            wg_address=f"{_MESH_SUBNET_PREFIX}.{subnet_id}.{idx}/24",
            wg_port=wg_port,
            is_network_host=(host_id == network_host_id),
        )
        db.add(peer)
        peers.append(peer)

    db.commit()
    for p in peers:
        db.refresh(p)
    return peers


def get_peer_config_for_host(db: Session, project_id: str, host_id: str) -> dict:
    all_peers = db.query(ProjectMeshPeer).filter_by(project_id=project_id).all()
    this_peer = next((p for p in all_peers if p.host_id == host_id), None)
    if not this_peer:
        raise ValueError(f"No mesh peer found for project {project_id}, host {host_id}")
    other_peers = [p for p in all_peers if p.host_id != host_id]

    return {
        "project_id": project_id,
        "wg_private_key": decrypt(this_peer.wg_private_key),
        "wg_address": this_peer.wg_address,
        "wg_port": this_peer.wg_port,
        "peers": [
            {
                "public_key": p.wg_public_key,
                "endpoint": p.wg_endpoint,
                "allowed_ips": p.wg_address.replace("/24", "/32"),
            }
            for p in other_peers
        ],
    }


def delete_mesh_peers(db: Session, project_id: str) -> None:
    db.query(ProjectMeshPeer).filter_by(project_id=project_id).delete()
    project = db.query(Project).filter_by(id=project_id).first()
    if project:
        project.mesh_subnet_id = None
        project.mesh_network_host_id = None
        project.host_assignments = None
    db.commit()
