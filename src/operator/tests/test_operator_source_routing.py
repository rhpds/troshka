from helpers.topology import _build_disk_from_storage
from handlers.vm import _resolve_disk_s3, _cdrom_s3_creds


def test_pattern_disk_carries_obc_source():
    sd = {
        "source": "pattern",
        "patternId": "pat-1",
        "patternDiskId": "pd-1",
        "resolvedS3Path": "patterns/pat-1/pd-1.qcow2",
        "diskSource": "obc",
        "format": "qcow2",
        "size": 20,
    }
    result = _build_disk_from_storage(sd, "storage-1")
    assert result["disk"]["patternImage"]["source"] == "obc"


def test_pattern_disk_defaults_source_central():
    sd = {
        "source": "pattern",
        "patternId": "pat-1",
        "patternDiskId": "pd-1",
        "resolvedS3Path": "patterns/pat-1/pd-1.qcow2",
        "format": "qcow2",
        "size": 20,
    }
    result = _build_disk_from_storage(sd, "storage-1")
    assert result["disk"]["patternImage"]["source"] == "central"


def test_resolve_obc_source_uses_obc_config():
    s3_config = {
        "bucket": "troshka-images",
        "endpoint": "https://s4",
        "obcConfig": {
            "bucket": "obc-bucket",
            "endpoint": "https://rgw.svc",
            "credentialsSecret": "s3-obc-credentials",  # pragma: allowlist secret
        },
    }
    disk = {"patternImage": {"s3Path": "patterns/p/d.qcow2", "source": "obc"}}
    path, cfg, secret = _resolve_disk_s3(disk, s3_config, None)
    assert path == "patterns/p/d.qcow2"
    assert cfg["bucket"] == "obc-bucket"
    assert secret == "s3-obc-credentials"  # pragma: allowlist secret


def test_resolve_central_source_uses_primary():
    s3_config = {"bucket": "troshka-images", "endpoint": "https://s4"}
    disk = {"patternImage": {"s3Path": "patterns/p/d.qcow2", "source": "central"}}
    path, cfg, secret = _resolve_disk_s3(disk, s3_config, {"bucket": "gold"})
    assert cfg["bucket"] == "troshka-images"
    assert secret == "s3-credentials"  # pragma: allowlist secret


def test_resolve_obc_source_falls_back_when_no_obc_config():
    s3_config = {"bucket": "troshka-images", "endpoint": "https://s4"}
    disk = {"patternImage": {"s3Path": "patterns/p/d.qcow2", "source": "obc"}}
    path, cfg, secret = _resolve_disk_s3(disk, s3_config, None)
    assert cfg["bucket"] == "troshka-images"
    assert secret == "s3-credentials"  # pragma: allowlist secret


def test_snapshot_data_disk_clones_from_resolved_path():
    # A snapshot-sourced qcow2 disk must clone from its resolved S3 path,
    # not be provisioned as a blank disk (which silently drops the data).
    sd = {
        "source": "snapshot",
        "libraryItemId": "snap-1",
        "resolvedS3Path": "snapshots/snap-1/disk-a.qcow2",
        "format": "qcow2",
        "size": 20,
    }
    result = _build_disk_from_storage(sd, "storage-1")
    assert "blank" not in result["disk"]
    assert result["disk"]["libraryImage"]["s3Path"] == "snapshots/snap-1/disk-a.qcow2"


def test_snapshot_data_disk_blank_without_resolved_path():
    # Defensive: no fabricated key for snapshots — fall back to blank.
    sd = {
        "source": "snapshot",
        "libraryItemId": "snap-1",
        "format": "qcow2",
        "size": 20,
    }
    result = _build_disk_from_storage(sd, "storage-1")
    assert result["disk"].get("blank") is True
    assert "libraryImage" not in result["disk"]


def test_snapshot_iso_uses_resolved_path_not_flat():
    # A snapshot ISO node keeps its original library key via resolvedS3Path;
    # it must NOT fall back to the flat library/<id>.iso guess (404s).
    sd = {
        "source": "snapshot",
        "libraryItemId": "iso-1",
        "resolvedS3Path": "library/lib-x/iso-1/RHEL.iso",
        "format": "iso",
        "centralSource": True,
    }
    result = _build_disk_from_storage(sd, "storage-1")
    assert result["cdrom"]["s3Path"] == "library/lib-x/iso-1/RHEL.iso"
    assert result["cdrom"]["central"] is True


def test_cdrom_central_uses_central_credentials():
    s3_config = {"bucket": "troshka-images"}
    central = {"bucket": "central-s4"}
    cfg, secret = _cdrom_s3_creds(
        {"s3Path": "library/x.iso", "central": True}, s3_config, central
    )
    assert cfg["bucket"] == "central-s4"
    assert secret == "s3-central-credentials"  # pragma: allowlist secret


def test_cdrom_non_central_uses_primary_credentials():
    s3_config = {"bucket": "troshka-images"}
    central = {"bucket": "central-s4"}
    cfg, secret = _cdrom_s3_creds(
        {"s3Path": "library/x.iso", "central": False}, s3_config, central
    )
    assert cfg["bucket"] == "troshka-images"
    assert secret == "s3-credentials"  # pragma: allowlist secret
