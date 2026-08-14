import uuid
from unittest.mock import MagicMock

import pytest

from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.user import User
from app.services import deploy_service
from tests.conftest import TestSession


def _make_db():
    db = TestSession()
    return db


def _pattern_disk(db, virtual_bytes=21474836480):
    user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4()}@e.com", display_name="t")
    db.add(user)
    db.flush()
    pat = Pattern(id=str(uuid.uuid4()), name="p", owner_id=user.id, topology={})
    db.add(pat)
    db.flush()
    pd = PatternDisk(
        id=str(uuid.uuid4()),
        pattern_id=pat.id,
        source_disk_id="d",
        source_vm_id="v",
        s3_key="patterns/p/d.qcow2",
        format="qcow2",
        size_bytes=5000,
        virtual_size_bytes=virtual_bytes,
        state="available",
    )
    db.add(pd)
    db.flush()
    return pat, pd


PROV = str(uuid.uuid4())


def _data(pat, pd):
    return {
        "source": "pattern",
        "patternId": pat.id,
        "patternDiskId": pd.id,
        "label": "boot",
        "size": 10,
    }


def test_resolve_prefers_obc_on_source_provider():
    db = _make_db()
    try:
        pat, pd = _pattern_disk(db)
        db.add(
            PatternLocation(
                pattern_disk_id=pd.id,
                provider_id=PROV,
                location_type="obc",
                s3_key=pd.s3_key,
                state="synced",
            )
        )
        db.flush()
        data = _data(pat, pd)
        deploy_service._resolve_pattern_disk(data, db, PROV)
        assert data["diskSource"] == "obc"
        assert data["resolvedS3Path"] == pd.s3_key
        # 20 GiB virtual -> size bumped to at least 20
        assert data["size"] >= 20
    finally:
        db.rollback()
        db.close()


def test_resolve_uses_central_when_no_obc():
    db = _make_db()
    try:
        pat, pd = _pattern_disk(db)
        db.add(
            PatternLocation(
                pattern_disk_id=pd.id,
                provider_id=None,
                location_type="central",
                s3_key=pd.s3_key,
                state="synced",
            )
        )
        db.flush()
        data = _data(pat, pd)
        deploy_service._resolve_pattern_disk(data, db, PROV)
        assert data["diskSource"] == "central"
    finally:
        db.rollback()
        db.close()


def test_resolve_raises_when_nowhere():
    db = _make_db()
    try:
        pat, pd = _pattern_disk(db)
        data = _data(pat, pd)
        with pytest.raises(deploy_service.DeployError):
            deploy_service._resolve_pattern_disk(data, db, PROV)
    finally:
        db.rollback()
        db.close()


def test_preflight_raises_on_central_miss():
    s3_client = MagicMock()
    s3_client.head_object.side_effect = Exception("404 NoSuchKey")
    topology = {
        "nodes": [
            {
                "type": "storageNode",
                "data": {
                    "source": "pattern",
                    "diskSource": "central",
                    "resolvedS3Path": "patterns/p/d.qcow2",
                    "label": "boot",
                },
            }
        ]
    }
    with pytest.raises(deploy_service.DeployError) as ei:
        deploy_service._preflight_verify_pattern_disks(
            topology, s3_client, "troshka-images", {}
        )
    assert "boot" in str(ei.value)


def test_preflight_skips_obc_disks():
    s3_client = MagicMock()
    topology = {
        "nodes": [
            {
                "type": "storageNode",
                "data": {
                    "source": "pattern",
                    "diskSource": "obc",
                    "resolvedS3Path": "patterns/p/d.qcow2",
                    "label": "boot",
                },
            }
        ]
    }
    deploy_service._preflight_verify_pattern_disks(
        topology, s3_client, "troshka-images", {}
    )
    s3_client.head_object.assert_not_called()
