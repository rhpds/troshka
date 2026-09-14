"""Tests for KubeVirt-only placement via topology.placement.requires_kubevirt."""

import uuid

from app.models.host import Host
from app.models.project import Project
from app.models.user import User
from app.services.placement import (
    find_available_host,
    place_project,
    topology_requires_kubevirt,
)
from tests.conftest import TestSession


def _make_host(db, provider_id, host_type="shared", vcpus=256, ram_mb=524288):
    h = Host(
        id=str(uuid.uuid4()),
        state="active",
        agent_status="connected",
        host_type=host_type,
        provider_id=provider_id,
        total_vcpus=vcpus,
        total_ram_mb=ram_mb,
        used_vcpus=0,
        used_ram_mb=0,
        max_eips=0,
        ip_address="10.0.0.1",
    )
    db.add(h)
    db.flush()
    return h


def test_topology_requires_kubevirt_reads_placement():
    assert topology_requires_kubevirt({"placement": {"requires_kubevirt": True}})
    assert not topology_requires_kubevirt({"placement": {"requires_kubevirt": False}})
    assert not topology_requires_kubevirt({})


def test_find_available_host_requires_kubevirt_filters_host_type():
    db = TestSession()
    try:
        prov = str(uuid.uuid4())
        _make_host(db, prov, host_type="shared")
        kv = _make_host(db, prov, host_type="kubevirt-cluster")

        assert find_available_host(db, 4, 8000, requires_kubevirt=True) is kv
        assert (
            find_available_host(db, 4, 8000, provider_id=prov, requires_kubevirt=True)
            is kv
        )
    finally:
        db.close()


def test_place_project_requires_kubevirt_skips_shared_hosts():
    db = TestSession()
    try:
        prov = str(uuid.uuid4())
        _make_host(db, prov, host_type="shared")
        kv = _make_host(db, prov, host_type="kubevirt-cluster")

        user = User(email=f"{uuid.uuid4()}@test.com", password_hash="fake")
        db.add(user)
        db.flush()

        project = Project(
            name="cclm",
            owner_id=user.id,
            provider_id=prov,
            topology={
                "placement": {"requires_kubevirt": True},
                "nodes": [
                    {
                        "id": "vm1",
                        "type": "vmNode",
                        "data": {"vcpus": 4, "ram": 8},
                    }
                ],
            },
        )
        db.add(project)
        db.flush()

        result = place_project(db, project)
        assert result.get("host_id") == kv.id
    finally:
        db.close()
