import uuid

from app.models.mesh_peer import ProjectMeshPeer
from app.models.project import Project
from app.models.user import User
from app.services.mesh_service import (
    allocate_mesh_subnet,
    allocate_wg_port,
    create_mesh_peers,
    delete_mesh_peers,
    generate_wireguard_keypair,
    get_peer_config_for_host,
)
from tests.conftest import TestSession


def _make_user(db):
    u = User(
        email=f"mesh-svc-{uuid.uuid4().hex[:6]}@test.com",
        display_name="Test",
        role="admin",
        auth_source="local",
        password_hash="x",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_project(db, user, **kwargs):
    p = Project(
        name=f"proj-{uuid.uuid4().hex[:6]}",
        owner_id=user.id,
        state="deploying",
        **kwargs,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_generate_wireguard_keypair():
    private_key, public_key = generate_wireguard_keypair()
    assert len(private_key) == 44  # base64-encoded 32 bytes
    assert len(public_key) == 44
    assert private_key != public_key


def test_allocate_mesh_subnet_sequential():
    db = TestSession()
    try:
        # Fix: create a project with mesh_subnet_id between calls
        user = _make_user(db)
        subnet1 = allocate_mesh_subnet(db)

        # Commit a project with the allocated subnet
        p1 = _make_project(db, user, mesh_subnet_id=subnet1)

        subnet2 = allocate_mesh_subnet(db)
        assert subnet1 >= 1
        assert subnet2 == subnet1 + 1

        db.delete(p1)
        db.delete(user)
        db.commit()
    finally:
        db.rollback()
        db.close()


def test_allocate_wg_port_starts_at_base():
    db = TestSession()
    try:
        port = allocate_wg_port(db, "host-new")
        assert port == 51820
    finally:
        db.rollback()
        db.close()


def test_allocate_wg_port_avoids_conflicts():
    db = TestSession()
    try:
        user = _make_user(db)
        project = _make_project(db, user)

        # Use a UUID for host_id
        host_id = str(uuid.uuid4())
        peer = ProjectMeshPeer(
            project_id=project.id,
            host_id=host_id,
            peer_type="troshkad",
            wg_public_key="pub",
            wg_private_key="priv",
            wg_endpoint="1.2.3.4:51820",
            wg_address="10.252.1.1/24",
            wg_port=51820,
            is_network_host=False,
        )
        db.add(peer)
        db.commit()

        port = allocate_wg_port(db, host_id)
        assert port == 51821

        # Clean up in reverse order
        db.query(ProjectMeshPeer).filter_by(project_id=project.id).delete()
        db.commit()
        db.delete(project)
        db.delete(user)
        db.commit()
    finally:
        db.close()


def test_create_mesh_peers():
    db = TestSession()
    try:
        user = _make_user(db)
        project = _make_project(db, user)

        # Use UUIDs for host_ids
        host_a_id = str(uuid.uuid4())
        host_b_id = str(uuid.uuid4())
        host_assignments = {
            host_a_id: ["vm1", "vm2"],
            host_b_id: ["vm3", "vm4"],
        }
        host_ips = {host_a_id: "10.0.1.50", host_b_id: "10.0.1.51"}

        peers = create_mesh_peers(db, project.id, host_assignments, host_a_id, host_ips)

        assert len(peers) == 2
        network_hosts = [p for p in peers if p.is_network_host]
        assert len(network_hosts) == 1
        assert network_hosts[0].host_id == host_a_id

        for p in peers:
            assert p.wg_address.startswith("10.252.")
            assert p.wg_port >= 51820

        # Clean up
        db.query(ProjectMeshPeer).filter_by(project_id=project.id).delete()
        db.commit()
        db.delete(project)
        db.delete(user)
        db.commit()
    finally:
        db.close()


def test_get_peer_config_for_host():
    db = TestSession()
    try:
        user = _make_user(db)
        project = _make_project(db, user)

        # Use UUIDs for host_ids
        host_a_id = str(uuid.uuid4())
        host_b_id = str(uuid.uuid4())
        host_assignments = {
            host_a_id: ["vm1"],
            host_b_id: ["vm2"],
        }
        host_ips = {host_a_id: "10.0.1.50", host_b_id: "10.0.1.51"}

        create_mesh_peers(db, project.id, host_assignments, host_a_id, host_ips)

        config = get_peer_config_for_host(db, project.id, host_a_id)
        assert config["project_id"] == project.id
        assert config["wg_address"].startswith("10.252.")
        assert config["wg_port"] >= 51820
        assert "wg_private_key" in config
        assert len(config["peers"]) == 1
        assert config["peers"][0]["endpoint"] == "10.0.1.51:" + str(
            config["peers"][0]["endpoint"].split(":")[1]
        )

        # Clean up
        db.query(ProjectMeshPeer).filter_by(project_id=project.id).delete()
        db.commit()
        db.delete(project)
        db.delete(user)
        db.commit()
    finally:
        db.close()


def test_delete_mesh_peers():
    db = TestSession()
    try:
        user = _make_user(db)
        project = _make_project(db, user)

        # Use UUIDs for host_ids
        host_a_id = str(uuid.uuid4())
        host_b_id = str(uuid.uuid4())
        host_assignments = {host_a_id: ["vm1"], host_b_id: ["vm2"]}
        host_ips = {host_a_id: "10.0.1.50", host_b_id: "10.0.1.51"}

        create_mesh_peers(db, project.id, host_assignments, host_a_id, host_ips)
        assert db.query(ProjectMeshPeer).filter_by(project_id=project.id).count() == 2

        delete_mesh_peers(db, project.id)
        assert db.query(ProjectMeshPeer).filter_by(project_id=project.id).count() == 0

        db.delete(project)
        db.delete(user)
        db.commit()
    finally:
        db.close()
