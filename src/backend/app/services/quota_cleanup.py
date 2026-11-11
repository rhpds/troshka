"""Confirmed absence is the only whole-environment resource release boundary."""

from app.models.quota import QuotaReservation
from app.schemas.quota import Resources
from app.services import quota_accounting as accounting
from app.services import quota_admission as admission
from app.services import quota_operations as operations


def fence(db, project_id):
    """Prevent every unclaimed/next provider dispatch before cleanup begins."""
    rows = db.query(QuotaReservation).filter_by(project_id=project_id).all()
    admission.lock_operations(db, [row.operation_id for row in rows])
    from app.services.quota_locks import lock_retained_project

    lock_retained_project(db, project_id)
    current = (
        db.query(QuotaReservation)
        .filter_by(project_id=project_id)
        .populate_existing()
        .all()
    )
    if {row.operation_id for row in current} != {row.operation_id for row in rows}:
        raise accounting.AccountingUnavailable(
            "Allocation changed before cleanup; retry cleanup"
        )
    rows = current
    for row in rows:
        row.dispatch_metadata = {**row.dispatch_metadata, "cancelled": True}
    db.flush()


def _provider_work_settled(db, project_id):
    rows = (
        db.query(QuotaReservation)
        .filter_by(project_id=project_id)
        .populate_existing()
        .all()
    )
    for row in rows:
        if not row.dispatch_metadata.get("cancelled"):
            return False
        if row.dispatch_metadata.get("claim") and not row.dispatch_metadata.get(
            "worker_finished"
        ):
            return False
        calls = _settled_calls(db, row.dispatch_metadata.get("calls", {}))
        if calls is None:
            return False
        row.dispatch_metadata = {**row.dispatch_metadata, "calls": calls}
    db.flush()
    return True


def _settled_calls(db, previous):
    from app.models.host import Host
    from app.services.troshkad_client import poll_job

    calls = dict(previous)
    for key, call in calls.items():
        if call.get("terminal"):
            continue
        if not call.get("job_id"):
            return None  # Ambiguous HTTP dispatch cannot be inferred absent.
        host = db.get(Host, call["host_id"])
        if not host:
            return None
        result = poll_job(host, call["job_id"])
        if result.get("status") not in {"completed", "failed", "cancelled"}:
            return None
        calls[key] = {**call, "terminal": True, "status": result["status"]}
    return calls


def verify_absence(db, project, host):
    """Query the agent after all accepted creation jobs have reached a terminal state."""
    from app.services.deploy_topology import _vm_dir
    from app.services.troshkad_client import get_all_vm_states, start_job, wait_for_job

    if not _provider_work_settled(db, project.id):
        return None
    # An unsupported adapter can still clean up, but must retain ledger/history
    # until an adapter can prove all resources absent (not merely a missing host).
    try:
        operations.supported(db, project, host_id=host.id, deployed=True)
    except accounting.AccountingUnavailable:
        return None
    timestamp = operations.now()  # Original request-start time, never receipt time.
    states = get_all_vm_states(host)
    if states is None or any(
        name.startswith(f"troshka-{project.id[:8]}-") for name in states
    ):
        return None
    from types import SimpleNamespace

    # Check both placements: old metadata alone cannot prove which path exists.
    for pool in (None, SimpleNamespace(mode="shared")):
        job_id = start_job(host, "/files/stat", {"path": _vm_dir(project.id, pool)})
        result = wait_for_job(host, job_id, timeout=30)
        if (
            result.get("status") != "completed"
            or result.get("result", {}).get("exists") is not False
        ):
            return None
    return timestamp


def finalize_absence(db, project, timestamp):
    """Caller supplies verified absence after fencing and settling provider work.

    Does not require accounting readiness or a spending allowance. The transaction
    atomically closes meters, retains charges, and releases the retained ledger.
    """
    from app.models.metering import MeteringInterval
    from app.services.quota_locks import lock_retained_project

    rows = db.query(QuotaReservation).filter_by(project_id=project.id).all()
    admission.lock_operations(db, [row.operation_id for row in rows])
    lock_retained_project(db, project.id)
    for row in rows:
        if not row.dispatch_metadata.get("cancelled"):
            raise accounting.AccountingUnavailable(
                "Cleanup must fence queued allocation before release"
            )
        admission.confirm(
            db,
            row.operation_id,
            Resources(),
            f"destroy:{timestamp.isoformat()}",
            timestamp,
        )
    for row in (
        db.query(MeteringInterval)
        .filter_by(project_id=project.id, ended_at=None)
        .with_for_update()
    ):
        row.ended_at = timestamp
    accounting.accrue(db, project.id, timestamp, retained=True)
    db.flush()


def complete(db, project, host):
    timestamp = verify_absence(db, project, host)
    if timestamp is None:
        return False
    finalize_absence(db, project, timestamp)
    from app.services.metering_service import _freeze_quota_invoice

    _freeze_quota_invoice(db, project, timestamp, refresh_live=False)
    db.commit()
    return True


def complete_empty(db, project):
    """A never-dispatched empty draft needs no hypothetical provider observation."""
    from app.models.metering import MeteringInterval

    rows = operations.current_generation(
        db.query(QuotaReservation).filter_by(project_id=project.id).all()
    )
    if (
        project.host_id
        or db.query(MeteringInterval)
        .filter_by(project_id=project.id, ended_at=None)
        .first()
    ):
        return False
    if any(
        row.dispatch_metadata.get("claim")
        or row.dispatch_metadata.get("calls")
        or any(row.observed.values())
        for row in rows
    ):
        return False
    fence(db, project.id)
    timestamp = operations.now()
    finalize_absence(db, project, timestamp)
    from app.services.metering_service import _freeze_quota_invoice

    _freeze_quota_invoice(db, project, timestamp, refresh_live=False)
    return True
