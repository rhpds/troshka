"""Eager OBC -> central S4 sync for captured patterns.

An RQ worker orchestrates the copy; the bytes move via an rclone Job on the
source cluster (the OBC endpoint is an in-cluster .svc address unreachable
from the backend workers). Central S4 = the primary ``troshka-images`` bucket.
"""

from __future__ import annotations

import datetime
import logging
import math
import time
import uuid
from typing import cast

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.provider import Provider
from app.services.s3_storage import _get_s3_config, get_cluster_s3_config, owner_params

log = logging.getLogger(__name__)

SYNC_NAMESPACE = "troshka-cache"
_SYNC_POLL_TIMEOUT = 3600
_SYNC_POLL_INTERVAL = 10


# ---------------------------------------------------------------------------
# Config helper
# ---------------------------------------------------------------------------


def sync_copy_deadline(size_bytes: int) -> int:
    """Bound a copy at a conservative 30 MiB/s plus one hour for retries."""
    return max(_SYNC_POLL_TIMEOUT, math.ceil(size_bytes / (30 * 1024**2)) + 3600)


def _max_central_bytes() -> int | None:
    """Configured central S4 ceiling in bytes, or None when unset."""
    from app.core.config import config

    return getattr(getattr(config, "central_s4", None), "max_bytes", None)


# ---------------------------------------------------------------------------
# Capacity guard
# ---------------------------------------------------------------------------


def _synced_central_bytes(db) -> int:
    """Sum of size_bytes across all synced central PatternLocation rows."""
    rows = db.scalars(
        select(PatternLocation).filter_by(location_type="central", state="synced")
    ).all()
    return sum(r.size_bytes or 0 for r in rows)


def central_capacity_available(db, additional_bytes: int) -> bool:
    """True if additional_bytes fit under the configured central ceiling."""
    ceiling = _max_central_bytes()
    if ceiling is None:
        return True
    return _synced_central_bytes(db) + additional_bytes <= ceiling


# ---------------------------------------------------------------------------
# rclone Job builder (pure — no DB / K8s calls)
# ---------------------------------------------------------------------------


def build_sync_rclone_job(
    name, namespace, keys, src_cfg, dst_cfg, *, size_bytes=0, largest_object_bytes=None
) -> dict:
    """Build a BatchV1 Job that rclone-copies each key OBC(src) -> central(dst)."""
    src_endpoint = src_cfg.get("endpoint") or src_cfg.get("endpoint_url", "")
    dst_endpoint = dst_cfg.get("endpoint_url") or dst_cfg.get("endpoint", "")
    src_bucket = src_cfg.get("bucket", "")
    dst_bucket = dst_cfg.get("bucket", "troshka-images")
    # S4's POSIX multipart completion fails across the 1,000-part page boundary.
    # Rclone enlarges chunks automatically for known-size objects to respect
    # this cap. Reserve memory for that larger buffer, not just the base chunk.
    largest = size_bytes if largest_object_bytes is None else largest_object_bytes
    # Temporary S4 pagination workaround. Retire only after a released S4 image
    # passes >1000-part tests; see infra/central-s4/patches/posix-multipart/README.md.
    chunk_mib = max(256, math.ceil(largest / (900 * 1024**2)))
    if chunk_mib > 5120:
        raise ValueError("Disk exceeds S4's safe 900-part upload size (4500 GiB)")
    memory_mib = max(4096, chunk_mib * 4 + 1024)
    deadline = sync_copy_deadline(size_bytes)

    copies = "\n".join(
        f'rclone copyto "src:{src_bucket}/{k}" "dst:{dst_bucket}/{k}" '
        f"--s3-chunk-size {chunk_mib}M --s3-max-upload-parts 900 "
        "--s3-upload-concurrency 1 --transfers 1 --multi-thread-streams 0 --size-only "
        "--log-level INFO --stats 15s --stats-one-line;"
        for k in keys
    )
    cmd = (
        "set -e; export HOME=/tmp; export RCLONE_CONFIG=/tmp/rclone.conf;\n"
        "cat > $RCLONE_CONFIG <<REOF\n"
        "[src]\n"
        "type = s3\n"
        "provider = Ceph\n"
        "access_key_id = $SRC_ACCESS_KEY_ID\n"
        "secret_access_key = $SRC_SECRET_ACCESS_KEY\n"
        f"endpoint = {src_endpoint}\n"
        "no_check_bucket = true\n"
        "no_verify_ssl = true\n"
        "[dst]\n"
        "type = s3\n"
        "provider = Ceph\n"
        "access_key_id = $DST_ACCESS_KEY_ID\n"
        "secret_access_key = $DST_SECRET_ACCESS_KEY\n"
        f"endpoint = {dst_endpoint}\n"
        "no_check_bucket = true\n"
        "no_verify_ssl = true\n"
        "REOF\n" + copies
    )
    env = [
        {"name": "GOMEMLIMIT", "value": str(memory_mib * 1024**2 * 3 // 4)},
        {"name": "SRC_ACCESS_KEY_ID", "value": src_cfg.get("access_key_id", "")},
        {
            "name": "SRC_SECRET_ACCESS_KEY",
            "value": src_cfg.get("secret_access_key", ""),
        },
        {"name": "DST_ACCESS_KEY_ID", "value": dst_cfg.get("access_key_id", "")},
        {
            "name": "DST_SECRET_ACCESS_KEY",
            "value": dst_cfg.get("secret_access_key", ""),
        },
    ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"troshka-role": "pattern-sync"},
        },
        "spec": {
            "backoffLimit": 3,
            "activeDeadlineSeconds": deadline,
            "ttlSecondsAfterFinished": 86400,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "sync",
                            "image": "rclone/rclone:latest",
                            "command": ["sh", "-c", cmd],
                            "env": env,
                            "resources": {
                                "requests": {
                                    "cpu": "500m",
                                    "memory": f"{max(1024, chunk_mib * 2)}Mi",
                                },
                                "limits": {"cpu": "2", "memory": f"{memory_mib}Mi"},
                            },
                        }
                    ],
                }
            },
        },
    }


# ---------------------------------------------------------------------------
# rclone Job runner (separate from builder so tests can patch it)
# ---------------------------------------------------------------------------


def _ensure_namespace(core_api, k8s_client) -> None:
    """Create SYNC_NAMESPACE if it does not already exist."""
    try:
        core_api.create_namespace(
            body=k8s_client.V1Namespace(
                metadata=k8s_client.V1ObjectMeta(name=SYNC_NAMESPACE)
            )
        )
    except Exception as exc:
        if "AlreadyExists" not in str(exc):
            raise


def _submit_job(batch_api, body: dict) -> None:
    """Submit the rclone Job, ignoring AlreadyExists."""
    try:
        batch_api.create_namespaced_job(namespace=SYNC_NAMESPACE, body=body)
    except Exception as exc:
        if "AlreadyExists" not in str(exc):
            raise


def _poll_job(batch_api, name: str, timeout: int = _SYNC_POLL_TIMEOUT) -> bool:
    """Poll until the Job succeeds or fails; return True on success."""
    waited = 0
    while waited < timeout:
        job = batch_api.read_namespaced_job(name=name, namespace=SYNC_NAMESPACE)
        status = job.status
        if status and status.succeeded:
            return True
        if status and any(
            c.type == "Failed" and c.status == "True" for c in (status.conditions or [])
        ):
            return False
        time.sleep(_SYNC_POLL_INTERVAL)
        waited += _SYNC_POLL_INTERVAL
    return False


def _run_rclone_job(
    provider, name, keys, src_cfg, dst_cfg, *, size_bytes=0, largest_object_bytes=None
) -> bool:
    """Create the rclone Job on the source cluster and poll to completion."""
    from kubernetes import client as k8s_client

    from app.services.providers.kubevirt import _get_k8s_clients

    _custom, core_api, api_client = _get_k8s_clients(provider)
    batch_api = k8s_client.BatchV1Api(api_client)

    _ensure_namespace(core_api, k8s_client)
    body = build_sync_rclone_job(
        name,
        SYNC_NAMESPACE,
        keys,
        src_cfg,
        dst_cfg,
        size_bytes=size_bytes,
        largest_object_bytes=largest_object_bytes,
    )
    _submit_job(batch_api, body)
    if _poll_job(batch_api, name, timeout=body["spec"]["activeDeadlineSeconds"] + 60):
        return True
    job = cast(k8s_client.V1Job, batch_api.read_namespaced_job(name, SYNC_NAMESPACE))
    conditions = (job.status.conditions or []) if job.status else []
    details = [f"{c.reason}: {c.message}" for c in conditions if c.type == "Failed"]
    raise RuntimeError(
        f"Central copy job {name} failed: " + ("; ".join(details) or "timed out")
    )


# ---------------------------------------------------------------------------
# DB helpers for sync_pattern_to_central
# ---------------------------------------------------------------------------


def _fail_central_rows(db, rows, message: str) -> None:
    """Mark a list of PatternLocation rows as errored and commit."""
    for row in rows:
        row.state = "error"
        row.error_message = message[:500]
    db.commit()


def _verify_central_readback(dst_cfg: dict, keys: list[str]) -> list[str]:
    """Return the keys NOT readable in the destination read/write bucket.

    Deploy sources "central" disks from this same bucket, so a row must not be
    marked synced unless the object is actually fetchable here. Guards against
    a write bucket that differs from the read bucket, or a partial rclone copy.
    """
    import boto3

    bucket = dst_cfg.get("bucket", "troshka-images")
    kw = {
        "aws_access_key_id": dst_cfg.get("access_key_id", ""),
        "aws_secret_access_key": dst_cfg.get("secret_access_key", ""),
        "region_name": dst_cfg.get("region", "us-east-1"),
    }
    endpoint = dst_cfg.get("endpoint_url") or dst_cfg.get("endpoint")
    if endpoint:
        kw["endpoint_url"] = endpoint
    client = boto3.client("s3", **kw)
    head_op = owner_params(dst_cfg)
    missing = []
    for k in keys:
        try:
            client.head_object(Bucket=bucket, Key=k, **head_op)
        except Exception:  # noqa: BLE001 — any failure means "not readable here"
            missing.append(k)
    return missing


def _collect_pending_rows(db, disks) -> list:
    """Return (PatternDisk, PatternLocation) pairs for disks not yet central-synced."""
    pending = []
    for pd in disks:
        existing = db.scalars(
            select(PatternLocation).filter_by(
                pattern_disk_id=pd.id, location_type="central"
            )
        ).first()
        if existing and existing.state == "synced":
            continue
        if not existing:
            existing = PatternLocation(
                pattern_disk_id=pd.id,
                provider_id=None,
                location_type="central",
                s3_key=pd.s3_key,
                state="syncing",
                size_bytes=pd.size_bytes or 0,
            )
            db.add(existing)
        else:
            existing.state = "syncing"
            existing.error_message = None
        pending.append((pd, existing))
    return pending


# ---------------------------------------------------------------------------
# RQ worker entrypoint
# ---------------------------------------------------------------------------


def _mark_sync_failed(db, pending, message: str) -> None:
    _fail_central_rows(db, [row for _, row in pending], message)


def _finalize_central_sync(db, pending, keys: list[str], pattern_id: str) -> None:
    now = datetime.datetime.now(datetime.UTC)
    for _pd, row in pending:
        row.state = "synced"
        row.synced_at = now
    db.commit()
    log.info("sync: pattern %s synced %d disks to central", pattern_id, len(keys))


def sync_pattern_to_central(pattern_id: str) -> None:
    """RQ worker: copy a pattern's disks from source OBC to central S4."""
    db = SessionLocal()
    pending = []
    try:
        pattern = db.scalars(select(Pattern).filter_by(id=pattern_id)).first()
        if not pattern or not pattern.source_provider_id:
            log.warning(
                "sync: pattern %s missing or has no source provider", pattern_id
            )
            return

        disks = db.scalars(select(PatternDisk).filter_by(pattern_id=pattern_id)).all()
        pending = _collect_pending_rows(db, disks)
        if not pending:
            log.info("sync: pattern %s already central; nothing to do", pattern_id)
            return
        db.commit()

        addl = sum(pd.size_bytes or 0 for pd, _ in pending)
        if not central_capacity_available(db, addl):
            _mark_sync_failed(
                db, pending, "central S4 capacity exceeded — cannot sync pattern"
            )
            log.error(
                "sync: capacity guard rejected pattern %s (%d bytes)", pattern_id, addl
            )
            return

        provider = db.scalars(
            select(Provider).filter_by(id=pattern.source_provider_id)
        ).first()
        src_cfg = get_cluster_s3_config(db, pattern.source_provider_id)
        dst_cfg = _get_s3_config()
        if not provider or not src_cfg:
            _mark_sync_failed(db, pending, "source cluster OBC config unavailable")
            return

        keys = [pd.s3_key for pd, _ in pending]
        job_name = f"sync-{pattern_id[:8]}-{uuid.uuid4().hex[:8]}"
        if not _run_rclone_job(
            provider,
            job_name,
            keys,
            src_cfg,
            dst_cfg,
            size_bytes=addl,
            largest_object_bytes=max(pd.size_bytes or 0 for pd, _ in pending),
        ):
            _mark_sync_failed(db, pending, "rclone sync job failed or timed out")
            return
        missing = _verify_central_readback(dst_cfg, keys)
        if missing:
            _mark_sync_failed(
                db,
                pending,
                "copied but not readable in the shared central bucket: "
                + ", ".join(missing),
            )
            log.error(
                "sync: pattern %s copied but %d disks not readable in central",
                pattern_id,
                len(missing),
            )
            return
        _finalize_central_sync(db, pending, keys, pattern_id)
    except Exception as exc:
        log.exception("Central copy failed for pattern %s", pattern_id)
        db.rollback()
        if pending:
            _mark_sync_failed(db, pending, f"Central copy failed: {exc}")
    finally:
        db.close()
