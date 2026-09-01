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


def test_preflight_raises_on_missing_central_library_disk():
    central_client = MagicMock()
    central_client.head_object.side_effect = Exception("404 NoSuchKey")
    topology = {
        "nodes": [
            {
                "type": "storageNode",
                "data": {
                    "source": "library",
                    "centralSource": True,
                    "resolvedS3Path": "library/rhel-9.6.qcow2",
                    "label": "vscode-disk0",
                },
            }
        ]
    }
    with pytest.raises(deploy_service.DeployError) as ei:
        deploy_service._preflight_verify_library_disks(
            topology,
            MagicMock(),
            "troshka-images",
            {},
            central_client,
            "troshka-gold-images",
            {},
        )
    assert "vscode-disk0" in str(ei.value)


def test_preflight_skips_local_library_disks():
    central_client = MagicMock()
    topology = {
        "nodes": [
            {
                "type": "storageNode",
                "data": {
                    "source": "library",
                    "centralSource": False,
                    "resolvedS3Path": "library/local.qcow2",
                    "label": "disk0",
                },
            }
        ]
    }
    deploy_service._preflight_verify_library_disks(
        topology,
        MagicMock(),
        "troshka-images",
        {},
        central_client,
        "troshka-gold-images",
        {},
    )
    central_client.head_object.assert_not_called()


def _library_item(db, s3_key, fmt, source="local", item_type="snapshot"):
    from app.models.library import Library, LibraryItem

    lib = Library(id=str(uuid.uuid4()), type="personal")
    db.add(lib)
    db.flush()
    item = LibraryItem(
        id=str(uuid.uuid4()),
        library_id=lib.id,
        name="item",
        type=item_type,
        format=fmt,
        s3_key=s3_key,
        source=source,
        state="ready",
    )
    db.add(item)
    db.flush()
    return item


def test_resolve_snapshot_disk_uses_library_item_s3_key():
    # A snapshot-sourced data disk must resolve to the snapshot's real s3_key,
    # not be skipped (which leaves the operator to guess/blank it).
    db = _make_db()
    try:
        item = _library_item(db, "snapshots/abc/disk-a.qcow2", "qcow2", source="local")
        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {
                        "source": "snapshot",
                        "libraryItemId": item.id,
                        "format": "qcow2",
                        "label": "disk-00",
                    },
                }
            ]
        }
        deploy_service._resolve_disk_s3_paths(
            topology, db, PROV, None, "troshka-images", {}, None, "", {}
        )
        data = topology["nodes"][0]["data"]
        assert data["resolvedS3Path"] == "snapshots/abc/disk-a.qcow2"
        assert data["centralSource"] is False
    finally:
        db.rollback()
        db.close()


def test_resolve_snapshot_iso_uses_central_library_key():
    # A snapshot ISO points at the ORIGINAL (central) library item and must
    # resolve to its nested s3_key with central=True — not the flat guess.
    db = _make_db()
    try:
        item = _library_item(
            db,
            "library/lib-x/iso-1/RHEL 10.2 Binary DVD.iso",
            "iso",
            source="central",
            item_type="iso",
        )
        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {
                        "source": "snapshot",
                        "libraryItemId": item.id,
                        "format": "iso",
                        "label": "boot-00",
                    },
                }
            ]
        }
        deploy_service._resolve_disk_s3_paths(
            topology, db, PROV, None, "troshka-images", {}, None, "", {}
        )
        data = topology["nodes"][0]["data"]
        assert data["resolvedS3Path"] == "library/lib-x/iso-1/RHEL 10.2 Binary DVD.iso"
        assert data["centralSource"] is True
    finally:
        db.rollback()
        db.close()


def test_resolve_library_disk_bumps_size_from_virtual_size_bytes():
    from app.models.library import LibraryItemDisk

    db = _make_db()
    try:
        item = _library_item(db, "snapshots/abc/disk-a.qcow2", "qcow2", source="local")
        db.add(
            LibraryItemDisk(
                library_item_id=item.id,
                s3_key=item.s3_key,
                format="qcow2",
                size_bytes=2_000_000_000,
                virtual_size_bytes=80 * 1073741824,
                boot_order=0,
                state="available",
            )
        )
        db.flush()
        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {
                        "source": "library",
                        "libraryItemId": item.id,
                        "label": "bastion-disk0",
                        "size": 50,
                    },
                }
            ]
        }
        deploy_service._resolve_disk_s3_paths(
            topology, db, PROV, None, "troshka-images", {}, None, "", {}
        )
        data = topology["nodes"][0]["data"]
        assert data["size"] >= 80
        # sourceSizeGb recorded from virtual size (80 GiB)
        assert data["sourceSizeGb"] == 80
    finally:
        db.rollback()
        db.close()


def test_library_disk_virtual_size_falls_back_to_size_bytes_for_iso():
    # Raw ISO images have virtual_size_bytes=0 but a real size_bytes (~11GB).
    # Sizing must fall back to size_bytes so goldens/clones are sized correctly.
    from app.models.library import LibraryItemDisk

    db = _make_db()
    try:
        item = _library_item(
            db,
            "library/lib-x/iso-1/RHEL 10.2 Binary DVD.iso",
            "iso",
            source="central",
            item_type="iso",
        )
        db.add(
            LibraryItemDisk(
                library_item_id=item.id,
                s3_key=item.s3_key,
                format="iso",
                size_bytes=11 * 1073741824,
                virtual_size_bytes=0,
                boot_order=0,
                state="available",
            )
        )
        db.flush()
        result = deploy_service._library_disk_virtual_size_bytes(
            db, item, item.s3_key or ""
        )
        assert result == 11 * 1073741824
    finally:
        db.rollback()
        db.close()


def test_library_disk_virtual_size_prefers_virtual_over_size_bytes():
    from app.models.library import LibraryItemDisk

    db = _make_db()
    try:
        item = _library_item(db, "library/lib-y/disk-a.qcow2", "qcow2", source="local")
        db.add(
            LibraryItemDisk(
                library_item_id=item.id,
                s3_key=item.s3_key,
                format="qcow2",
                size_bytes=2 * 1073741824,
                virtual_size_bytes=80 * 1073741824,
                boot_order=0,
                state="available",
            )
        )
        db.flush()
        result = deploy_service._library_disk_virtual_size_bytes(
            db, item, item.s3_key or ""
        )
        assert result == 80 * 1073741824
    finally:
        db.rollback()
        db.close()


def test_library_disk_virtual_size_returns_zero_when_unknown():
    db = _make_db()
    try:
        item = _library_item(db, "library/lib-z/disk-a.qcow2", "qcow2", source="local")
        result = deploy_service._library_disk_virtual_size_bytes(
            db, item, item.s3_key or ""
        )
        assert result == 0
    finally:
        db.rollback()
        db.close()


def test_library_disk_virtual_size_uses_library_item_size_bytes_for_iso():
    # ISO library items have no LibraryItemDisk rows and no vm_config disks, but
    # the LibraryItem itself records the real file size. Sizing must use it so the
    # golden/clone aren't sized by the (tiny) per-disk sizeGb heuristic.
    db = _make_db()
    try:
        item = _library_item(
            db, "library/x/RHEL.iso", "iso", source="local", item_type="iso"
        )
        item.size_bytes = 11 * 1073741824
        db.flush()
        result = deploy_service._library_disk_virtual_size_bytes(
            db, item, item.s3_key or ""
        )
        assert result == 11 * 1073741824
    finally:
        db.rollback()
        db.close()


def test_resolve_pattern_disk_records_source_size_gb():
    db = _make_db()
    try:
        pat, pd = _pattern_disk(db, virtual_bytes=40 * 1073741824)
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
        assert data["sourceSizeGb"] == 40
    finally:
        db.rollback()
        db.close()
