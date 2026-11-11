"""Physical filesystem accounting, deliberately separate from compute admission.

Provider measurements cannot authorize compute growth. One retained deployment
generation owns all project files, including stopped and orphaned files. Other
start reservations in that generation always own zero storage bytes.
"""

import json
from datetime import datetime
from hashlib import sha256
from pathlib import PurePosixPath

from sqlalchemy import text

from app.models.host import Host
from app.models.quota import QuotaAllocation, QuotaReservation
from app.schemas.quota import AccountingUnavailable, QuotaDenied

VERSION = "allocated-v1"
MAX_AGE = 60
DISK_ALLOWANCE = 64 * 2**20
SEED_ALLOWANCE = 16 * 2**20


def initial_allocation(topology):
    # Validate topology/virtual capacities separately in the caller.
    nodes = topology.get("nodes", [])
    disks = {
        edge.get(side)
        for edge in topology.get("edges", [])
        for side in ("source", "target")
    }
    count = sum(
        node.get("type") == "storageNode" and node["id"] in disks for node in nodes
    )
    return (
        count * DISK_ALLOWANCE
        + sum(node.get("type") == "vmNode" for node in nodes) * SEED_ALLOWANCE
    )


def _integer(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise AccountingUnavailable("Invalid physical storage quantity")
    return value


def _files(items, root, pool, seen):
    if not isinstance(items, list) or len(items) > 10000:
        raise AccountingUnavailable("Missing or oversized storage file inventory")
    total = 0
    paths = set()
    for item in items:
        path = PurePosixPath(item["path"])
        if (
            not path.is_relative_to(root)
            or ".." in path.parts
            or str(path) != item["path"]
        ):
            raise AccountingUnavailable("Storage path lies outside its ownership root")
        identity = item["device"], item["inode"]
        if identity in seen or str(path) in paths or item["device"] != pool:
            raise AccountingUnavailable("Ambiguous physical storage identity")
        seen.add(identity)
        paths.add(str(path))
        total += _integer(item["allocated_bytes"])
    return _integer(total)


def validate(evidence, project_id, generation_id, host_id, now):
    """Validate trusted agent response, original request time and exact identities."""
    from app.services.quota_accounting import utc

    try:
        if (
            evidence.get("version") != VERSION
            or evidence.get("complete") is not True
            or evidence["project_id"] != project_id
            or evidence["generation_id"] != generation_id
            or evidence["host_id"] != host_id
        ):
            raise AccountingUnavailable(
                "Complete allocated-byte evidence for this generation is required"
            )
        timestamp = datetime.fromisoformat(evidence["observed_at"])
        if not 0 <= (utc(now) - utc(timestamp)).total_seconds() <= MAX_AGE:
            raise AccountingUnavailable(
                "Physical storage observation is stale or future-dated"
            )
        return _validate_inventory(evidence, project_id)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise AccountingUnavailable("Malformed physical storage evidence") from error


def _validate_inventory(evidence, project_id):
    pools = evidence["pools"]
    if (
        not isinstance(pools, list)
        or len(pools) != 1
        or pools[0]["id"] != evidence["writable_pool"]
    ):
        raise AccountingUnavailable(
            "Only one verified local filesystem pool is supported"
        )
    pool = pools[0]
    capacity, free = _integer(pool["capacity_bytes"]), _integer(pool["free_bytes"])
    if not capacity or free > capacity:
        raise AccountingUnavailable("Invalid physical pool capacity")
    seen = set()
    allocated = _files(
        evidence["files"],
        PurePosixPath(f"/var/lib/troshka/vms/{project_id}"),
        pool["id"],
        seen,
    )
    bases = evidence["bases"]
    for item in bases:
        if not any(
            PurePosixPath(item["path"]).is_relative_to(f"/var/lib/troshka/{root}")
            for root in ("images", "cache/patterns", "cache/snapshots")
        ):
            raise AccountingUnavailable("Unmapped shared base identity")
    platform = _files(bases, PurePosixPath("/var/lib/troshka"), pool["id"], seen)
    if platform != _integer(pool["platform_allocated_bytes"]):
        raise AccountingUnavailable("Shared base inventory total differs")
    return allocated


def fetch(host, project_id, generation_id):
    from app.services.quota_operations import now
    from app.services.troshkad_client import TroshkadError, troshkad_request

    timestamp = now()
    try:
        evidence = troshkad_request(
            host,
            "POST",
            "/storage/inventory",
            body={"project_id": project_id, "generation_id": generation_id},
            timeout=5,
            retries=1,
        )
    except TroshkadError as error:
        raise AccountingUnavailable(
            "Physical storage inventory unavailable; upgrade/check the host agent"
        ) from error
    evidence = {**evidence, "host_id": host.id, "observed_at": timestamp.isoformat()}
    validate(evidence, project_id, generation_id, host.id, now())
    evidence["observation_id"] = sha256(
        json.dumps(evidence, sort_keys=True).encode()
    ).hexdigest()
    return evidence


def reserve_floor(pool):
    """Operating headroom is separate from project ownership and never billable."""
    from app.core.config import config

    settings = getattr(config, "quotas", None)
    minimum = _integer(getattr(settings, "storage_pool_reserve_bytes", 2**30))
    percent = _integer(getattr(settings, "storage_pool_reserve_percent", 5))
    if not 1 <= percent <= 99 or not minimum:
        raise AccountingUnavailable("Invalid physical pool reserve configuration")
    return max(minimum, pool["capacity_bytes"] * percent // 100)


def _pool_lock(db, host_id, pool_id):
    # Always AFTER identity locks, never acquire an identity lock after this.
    if db.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(
            sha256(f"physical-pool:{host_id}:{pool_id}".encode()).digest()[:8],
            signed=True,
        )
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def check_pool(db, evidence, requested=0, exclude=None):
    from app.services.quota_admission import PENDING
    from app.services.quota_operations import now

    pool = evidence["pools"][0]
    _pool_lock(db, evidence["host_id"], pool["id"])
    # Lock contention must not refresh or extend the original observation age.
    validate(
        evidence,
        evidence["project_id"],
        evidence["generation_id"],
        evidence["host_id"],
        now(),
    )
    pending = 0
    for row in (
        db.query(QuotaReservation)
        .filter(QuotaReservation.state.in_(PENDING))
        .populate_existing()
    ):
        metadata = row.dispatch_metadata or {}
        if (
            row.operation_id != exclude
            and metadata.get("storage_host_id") == evidence["host_id"]
        ):
            pending += max(
                0, row.requested["storage_bytes"] - row.observed.get("storage_bytes", 0)
            )
    if pool["free_bytes"] <= reserve_floor(pool) + pending + requested:
        raise AccountingUnavailable(
            "Physical storage pool has insufficient operating headroom"
        )


def bind_host(db, project, host, operation_id):
    """Bind the selected pool before any provider growth; caller already holds identities."""
    row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
    evidence = fetch(host, project.id, row.dispatch_metadata["generation_id"])
    from app.services import quota_operations as operations

    root = measurement_row(
        operations.current_generation(operations._rows(db, project.id))
    )
    previous = root.dispatch_metadata.get("storage_evidence")
    if previous and (previous["host_id"], previous["writable_pool"]) != (
        host.id,
        evidence["writable_pool"],
    ):
        raise AccountingUnavailable(
            "Physical pool identity changed; reconcile the original allocation"
        )
    if row.dispatch_metadata.get("known_empty") and evidence["files"]:
        raise AccountingUnavailable(
            "Draft retains physical files; reconcile ownership before deployment"
        )
    check_pool(db, evidence, row.requested["storage_bytes"], exclude=operation_id)
    row.dispatch_metadata = {
        **row.dispatch_metadata,
        "storage_host_id": host.id,
        "storage_pool_id": evidence["writable_pool"],
        "storage_version": VERSION,
        "storage_evidence": evidence,
    }
    db.flush()


def measurement_row(rows):
    roots = [row for row in rows if row.dispatch_metadata.get("kind") == "deploy"]
    if len(roots) != 1 or any(
        row.dispatch_metadata.get("generation_id")
        != roots[0].dispatch_metadata.get("generation_id")
        for row in rows
    ):
        raise AccountingUnavailable(
            "Physical storage needs one retained deployment generation"
        )
    return roots[0]


def apply_measurement(db, project, rows, evidence, now):
    """Caller holds all operation and current membership locks. No compute increases."""
    from app.services import quota_accounting as accounting
    from app.services import quota_operations as operations

    root = measurement_row(rows)
    generation = root.dispatch_metadata["generation_id"]
    amount = validate(evidence, project.id, generation, project.host_id, now)
    previous_evidence = root.dispatch_metadata.get("storage_evidence")
    if (
        previous_evidence
        and previous_evidence["writable_pool"] != evidence["writable_pool"]
    ):
        raise AccountingUnavailable(
            "Physical pool identity changed; missing files are not release evidence"
        )
    timestamp = datetime.fromisoformat(evidence["observed_at"])
    settled = all(
        operations._observation_follows_worker(row, timestamp) for row in rows
    )
    if not settled and not _after_dispatch(rows, timestamp):
        raise AccountingUnavailable(
            "Storage observation predates its provider generation"
        )
    if settled:
        require_disks(root.dispatch_metadata.get("resources", {}), evidence)
    previous = root.dispatch_metadata.get("storage_measured_at")
    if previous and accounting.utc(timestamp) <= accounting.utc(
        datetime.fromisoformat(previous)
    ):
        return False
    _apply_storage_vectors(db, rows, root, amount, settled, timestamp, now)
    root.dispatch_metadata = {
        **root.dispatch_metadata,
        "storage_version": VERSION,
        "storage_host_id": project.host_id,
        "storage_pool_id": evidence["writable_pool"],
        "storage_evidence": evidence,
        "storage_measured_at": evidence["observed_at"],
    }
    db.flush()
    return True


def require_disks(mapping, evidence):
    expected = {path for path in mapping.get("disks", {}) if "/shared/" not in path}
    present = {item["path"] for item in evidence["files"]}
    if not expected.issubset(present):
        raise AccountingUnavailable(
            "Deployed disks missing from physical inventory; stop and reconcile open/unlinked files before release"
        )


def _after_dispatch(rows, timestamp):
    from app.services import quota_accounting as accounting
    from app.services import quota_operations as operations

    for row in rows:
        if operations._observation_follows_worker(row, timestamp):
            continue
        if not row.dispatch_metadata.get("claim") or not row.dispatched_at:
            return False
        if accounting.utc(timestamp) <= accounting.utc(row.dispatched_at):
            return False
    return True


def _apply_storage_vectors(db, rows, root, amount, settled, timestamp, now):
    # While provider work can still create files, only increases are evidence.
    # Keep the pending reservation and its remainder; never release it on a poll.
    if not settled:
        amount = max(amount, sum(row.observed.get("storage_bytes", 0) for row in rows))
    for row in rows:
        allocation = (
            db.query(QuotaAllocation)
            .filter_by(operation_id=row.operation_id)
            .populate_existing()
            .first()
        )
        if allocation is None:
            allocation = QuotaAllocation(
                operation_id=row.operation_id,
                project_id=row.project_id,
                owner_id=row.owner_id,
                organization_id=row.organization_id,
                requested=row.requested,
                observed_at=timestamp,
                observation_id="storage-only",
                state="reconciling",
            )
            db.add(allocation)
        if (row.owner_id, row.organization_id) != (
            allocation.owner_id,
            allocation.organization_id,
        ):
            raise AccountingUnavailable("Physical storage attribution changed; retry")
        vector = {**row.observed, "storage_bytes": amount if row == root else 0}
        row.observed = allocation.observed = vector
        allocation.updated_at = row.updated_at = now


def reconcile_project(project_id):
    """Provider I/O happens before locks; generation is rechecked after acquiring them."""
    from app.core.database import SessionLocal
    from app.models.project import Project
    from app.services import quota_operations as operations

    with SessionLocal() as db:
        project = db.get(Project, project_id)
        if not project or not project.host_id:
            return
        rows = operations.current_generation(operations._rows(db, project_id))
        if not rows:
            return
        root = measurement_row(rows)
        host = db.get(Host, project.host_id)
        evidence = fetch(host, project_id, root.dispatch_metadata["generation_id"])
        db.rollback()
        rows = operations._lock_observation_batch(db, project_id)
        if rows:
            project = db.get(Project, project_id)
            apply_measurement(db, project, rows, evidence, operations.now())
            db.commit()


def scope_evidence(db, scope, scope_id, now):
    """Freshness of every retained physical allocation, not just the requesting project."""
    from app.services import quota_operations as operations

    column = {
        "project": "project_id",
        "user": "owner_id",
        "organization": "organization_id",
    }[scope]
    rows = (
        db.query(QuotaReservation)
        .filter(getattr(QuotaReservation, column) == scope_id)
        .populate_existing()
        .all()
    )
    for row in rows:
        if (
            not any(row.observed.values())
            or row.dispatch_metadata.get("kind") != "deploy"
        ):
            continue
        evidence = row.dispatch_metadata.get("storage_evidence", {})
        validate(
            evidence,
            row.project_id,
            operations._generation(row),
            row.dispatch_metadata.get("storage_host_id"),
            now,
        )
        if row.dispatch_metadata.get("storage_measured_at") != evidence.get(
            "observed_at"
        ):
            raise AccountingUnavailable(
                "Physical storage allocation has not been measured"
            )


def check_allowance(db, scope, scope_id, now):
    from app.services.quota_admission import usage
    from app.services.quota_policy import get_limits

    scope_evidence(db, scope, scope_id, now)
    limit = get_limits(db, scope, scope_id).storage_bytes
    used, reserved = usage(db, scope, scope_id)
    if limit is not None and (
        used.storage_bytes >= limit
        or used.storage_bytes + reserved.storage_bytes > limit
    ):
        raise QuotaDenied(
            scope=scope,
            scope_id=scope_id,
            dimension="storage_bytes",
            used=used.storage_bytes,
            reserved=reserved.storage_bytes,
            requested=0,
            limit=limit,
            reason="resource_limit",
        )


def usage_details(db, scope, scope_id, now):
    from app.services import quota_operations as operations

    column = {
        "project": "project_id",
        "user": "owner_id",
        "organization": "organization_id",
    }[scope]
    rows = (
        db.query(QuotaReservation)
        .filter(getattr(QuotaReservation, column) == scope_id)
        .all()
    )
    rows = [
        row
        for row in operations.current_generation(rows)
        if row.dispatch_metadata.get("kind") == "deploy"
    ]
    result = {
        "storage_accounting_status": "unavailable",
        "provisioned_storage_bytes": None,
        "storage_as_of": None,
        "platform_storage_bytes": None,
    }
    try:
        scope_evidence(db, scope, scope_id, now)
        if any(row.dispatch_metadata.get("storage_version") != VERSION for row in rows):
            return result
        pools, dates = {}, []
        provisioned = 0
        for row in rows:
            provisioned += _integer(row.dispatch_metadata["provisioned_storage_bytes"])
            evidence = row.dispatch_metadata.get("storage_evidence")
            if not evidence:
                return result
            dates.append(datetime.fromisoformat(evidence["observed_at"]))
            key = evidence["host_id"], evidence["writable_pool"]
            if key not in pools or evidence["observed_at"] > pools[key]["observed_at"]:
                pools[key] = evidence
        return {
            "storage_accounting_status": "ready",
            "provisioned_storage_bytes": provisioned,
            "storage_as_of": min(dates) if dates else now,
            "platform_storage_bytes": sum(
                item["pools"][0]["platform_allocated_bytes"] for item in pools.values()
            ),
        }
    except (AccountingUnavailable, KeyError, TypeError, ValueError):
        return result
