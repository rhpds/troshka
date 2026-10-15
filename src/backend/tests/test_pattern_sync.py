"""Tests for pattern_sync — OBC->central sync worker with capacity guard."""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.provider import Provider
from app.models.user import User
from app.services import pattern_sync
from tests.conftest import TestSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pattern_with_disks(db, provider_id, total_bytes=1000):
    """Insert a User → Provider → Pattern → PatternDisk → OBC PatternLocation."""
    user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4()}@e.com")
    db.add(user)
    # Stub provider so sync_pattern_to_central can resolve it from the DB.
    db.add(
        Provider(
            id=provider_id,
            name=f"kv-{provider_id[:8]}",
            type="kubevirt_native",
        )
    )
    db.flush()
    pat = Pattern(
        id=str(uuid.uuid4()),
        name="p",
        owner_id=user.id,
        topology={"nodes": []},
        source_provider_id=provider_id,
        total_size_bytes=total_bytes,
    )
    db.add(pat)
    db.flush()
    pd = PatternDisk(
        id=str(uuid.uuid4()),
        pattern_id=pat.id,
        source_disk_id="d",
        source_vm_id="v",
        s3_key="patterns/x/d.qcow2",
        format="qcow2",
        size_bytes=total_bytes,
        state="available",
    )
    db.add(pd)
    db.add(
        PatternLocation(
            pattern_disk_id=pd.id,
            provider_id=provider_id,
            location_type="obc",
            s3_key=pd.s3_key,
            state="synced",
            size_bytes=total_bytes,
        )
    )
    db.flush()
    return pat, pd


def _sync_session():
    """Session that survives sync_pattern_to_central's finally db.close()."""
    db = TestSession()
    db.close = lambda: None  # patched close — test closes via Session.close(db)
    return db


# ---------------------------------------------------------------------------
# capacity guard tests (pure-ish — no K8s, no rclone)
# ---------------------------------------------------------------------------


def test_capacity_available_when_unconfigured():
    db = TestSession()
    try:
        with patch.object(pattern_sync, "_max_central_bytes", return_value=None):
            assert pattern_sync.central_capacity_available(db, 10**12) is True
    finally:
        db.close()


def test_capacity_guard_blocks_over_ceiling():
    db = TestSession()
    try:
        _, pd = _pattern_with_disks(db, str(uuid.uuid4()), total_bytes=100)
        db.add(
            PatternLocation(
                pattern_disk_id=pd.id,
                provider_id=None,
                location_type="central",
                s3_key=pd.s3_key,
                state="synced",
                size_bytes=900,
            )
        )
        db.flush()
        with patch.object(pattern_sync, "_max_central_bytes", return_value=1000):
            # 900 synced + 150 additional = 1050 > 1000 → False (blocked)
            assert pattern_sync.central_capacity_available(db, 150) is False
            # 900 synced + 100 additional = 1000 ≤ 1000 → True (at exact limit)
            assert pattern_sync.central_capacity_available(db, 100) is True
    finally:
        db.close()


# ---------------------------------------------------------------------------
# build_sync_rclone_job — pure; no DB needed
# ---------------------------------------------------------------------------


def test_rclone_job_body_copies_all_keys():
    body = pattern_sync.build_sync_rclone_job(
        "sync-abc",
        "troshka-cache",
        ["patterns/x/a.qcow2", "patterns/x/b.qcow2"],
        {
            "access_key_id": "AK",
            "secret_access_key": "SK",
            "endpoint": "https://rgw.svc",
            "bucket": "obc-bucket",
        },
        {
            "access_key_id": "CK",
            "secret_access_key": "CS",
            "endpoint_url": "https://s4",
            "bucket": "troshka-images",
        },
    )
    assert body["kind"] == "Job"
    cmd = body["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    assert "patterns/x/a.qcow2" in cmd
    assert "patterns/x/b.qcow2" in cmd
    assert "obc-bucket" in cmd
    assert "troshka-images" in cmd


def test_rclone_job_limits_multipart_parts_and_buffers_for_large_disks():
    body = pattern_sync.build_sync_rclone_job(
        "sync-large",
        "troshka-cache",
        ["patterns/large.qcow2"],
        {"bucket": "source"},
        {"bucket": "destination"},
        size_bytes=2 * 1024**4,
    )
    container = body["spec"]["template"]["spec"]["containers"][0]
    command = container["command"][-1]
    assert "--s3-max-upload-parts 900" in command
    assert "--s3-upload-concurrency 1" in command
    assert "--transfers 1" in command
    assert "--size-only" in command
    # Rclone grows its part buffers for large known-size files. The job must
    # reserve enough memory even for a 2 TiB disk, and enough time to copy it.
    assert int(container["resources"]["limits"]["memory"].removesuffix("Mi")) >= 3000
    assert body["spec"]["activeDeadlineSeconds"] > 3600


def test_retrying_job_is_not_failed_until_retries_are_exhausted():
    api = MagicMock()
    api.read_namespaced_job.side_effect = [
        SimpleNamespace(status=SimpleNamespace(succeeded=0, failed=1, conditions=[])),
        SimpleNamespace(status=SimpleNamespace(succeeded=1, failed=1, conditions=[])),
    ]
    with patch.object(pattern_sync.time, "sleep"):
        assert pattern_sync._poll_job(api, "sync", timeout=30) is True


def test_sync_exception_is_saved_as_visible_central_failure():
    db = _sync_session()
    try:
        pattern, disk = _pattern_with_disks(db, str(uuid.uuid4()))
        db.commit()
        with (
            patch.object(pattern_sync, "SessionLocal", return_value=db),
            patch.object(pattern_sync, "_max_central_bytes", return_value=None),
            patch.object(
                pattern_sync, "get_cluster_s3_config", return_value={"bucket": "source"}
            ),
            patch.object(
                pattern_sync, "_get_s3_config", return_value={"bucket": "central"}
            ),
            patch.object(
                pattern_sync, "_run_rclone_job", side_effect=RuntimeError("OOMKilled")
            ),
        ):
            pattern_sync.sync_pattern_to_central(pattern.id)
        central = (
            db.query(PatternLocation)
            .filter_by(pattern_disk_id=disk.id, location_type="central")
            .one()
        )
        assert central.state == "error"
        assert "OOMKilled" in central.error_message
    finally:
        Session.close(db)


# ---------------------------------------------------------------------------
# sync_pattern_to_central integration tests (DB + mocked rclone/k8s)
# ---------------------------------------------------------------------------


def test_sync_marks_synced_on_job_success():
    db = _sync_session()
    try:
        provider_id = str(uuid.uuid4())
        pat, pd = _pattern_with_disks(db, provider_id, total_bytes=500)

        with (
            patch.object(pattern_sync, "SessionLocal", return_value=db),
            patch.object(pattern_sync, "_max_central_bytes", return_value=None),
            patch.object(
                pattern_sync,
                "get_cluster_s3_config",
                return_value={
                    "access_key_id": "AK",
                    "secret_access_key": "SK",
                    "endpoint": "https://rgw.svc",
                    "bucket": "obc",
                },
            ),
            patch.object(
                pattern_sync,
                "_get_s3_config",
                return_value={
                    "access_key_id": "CK",
                    "secret_access_key": "CS",
                    "endpoint_url": "https://s4",
                    "bucket": "troshka-images",
                    "region": "us-east-1",
                },
            ),
            patch.object(pattern_sync, "_run_rclone_job", return_value=True),
            patch.object(pattern_sync, "_verify_central_readback", return_value=[]),
        ):
            pattern_sync.sync_pattern_to_central(pat.id)

        central = (
            db.query(PatternLocation)
            .filter_by(pattern_disk_id=pd.id, location_type="central")
            .first()
        )
        assert central is not None
        assert central.state == "synced"
        assert central.provider_id is None
    finally:
        Session.close(db)


def test_sync_marks_error_when_not_readable():
    # rclone reports success, but the object is not readable from the shared
    # read/write bucket that deploy sources "central" disks from → the row must
    # go to error, never a false "synced".
    db = _sync_session()
    try:
        provider_id = str(uuid.uuid4())
        pat, pd = _pattern_with_disks(db, provider_id, total_bytes=500)

        with (
            patch.object(pattern_sync, "SessionLocal", return_value=db),
            patch.object(pattern_sync, "_max_central_bytes", return_value=None),
            patch.object(
                pattern_sync,
                "get_cluster_s3_config",
                return_value={
                    "access_key_id": "AK",
                    "secret_access_key": "SK",
                    "endpoint": "https://rgw.svc",
                    "bucket": "obc",
                },
            ),
            patch.object(
                pattern_sync,
                "_get_s3_config",
                return_value={
                    "access_key_id": "CK",
                    "secret_access_key": "CS",
                    "endpoint_url": "https://s4",
                    "bucket": "troshka-images",
                    "region": "us-east-1",
                },
            ),
            patch.object(pattern_sync, "_run_rclone_job", return_value=True),
            patch.object(
                pattern_sync,
                "_verify_central_readback",
                return_value=[pd.s3_key],
            ),
        ):
            pattern_sync.sync_pattern_to_central(pat.id)

        central = (
            db.query(PatternLocation)
            .filter_by(pattern_disk_id=pd.id, location_type="central")
            .first()
        )
        assert central is not None
        assert central.state == "error"
        assert "readable" in (central.error_message or "").lower()
    finally:
        Session.close(db)


def test_sync_capacity_rejection_marks_error():
    db = _sync_session()
    try:
        provider_id = str(uuid.uuid4())
        pat, pd = _pattern_with_disks(db, provider_id, total_bytes=500)

        with (
            patch.object(pattern_sync, "SessionLocal", return_value=db),
            patch.object(pattern_sync, "_max_central_bytes", return_value=100),
            patch.object(pattern_sync, "_run_rclone_job") as run,
        ):
            pattern_sync.sync_pattern_to_central(pat.id)
            run.assert_not_called()

        central = (
            db.query(PatternLocation)
            .filter_by(pattern_disk_id=pd.id, location_type="central")
            .first()
        )
        assert central is not None
        assert central.state == "error"
        assert "capacity" in (central.error_message or "").lower()
    finally:
        Session.close(db)
