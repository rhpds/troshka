from helpers.topology import _build_disk_from_storage
from handlers.vm import _resolve_disk_s3


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
