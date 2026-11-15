"""Verify fresh accounting through Troshka's normal RQ workers.

Runs inside the application via stdin. No scheduling or quota settings change.
Only identifiers and outcome codes are returned; client exception text is hidden.
"""

import argparse
import json
import os
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def evidence_status(row, started):
    if not row or not row.get("started_at"):
        return "pending"
    if timestamp(row["started_at"]) < timestamp(started):
        return "pending"
    if row["conflicted"] or row["enrollment_id"] != row["current_enrollment_id"]:
        return "failed"
    if row["status"] != "complete":
        return "failed"
    return "complete" if row.get("completed_at") else "pending"


def observe(domain_id, project_id, kind):
    from app.core.database import SessionLocal
    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeProjectAttempt,
        NativeSharedObservation,
        NativeSharedStorageObservation,
        NativeStorageSnapshot,
    )
    with SessionLocal() as db:
        domain = db.get(NativeCollectionDomain, domain_id)
        if domain is None:
            raise ValueError("collection domain disappeared")
        conflicted = domain.conflicted
        if kind == "project":
            cursor = db.get(NativeProjectAttempt, (domain_id, project_id))
            if cursor is None or cursor.latest_report_id is None:
                return None
            row = db.get(NativeStorageSnapshot, cursor.latest_report_id)
            if row is None:
                return None
            conflicted = conflicted or cursor.conflicted
            payload = row.payload
            complete = (row.accepted and not row.reason_codes
                        and not payload.get("collection_errors")
                        and not payload.get("inventory_blockers"))
            status = "complete" if complete else "failed"
        else:
            model = NativeSharedObservation if kind == "readiness" else NativeSharedStorageObservation
            row = db.get(model, domain_id)
            if row is None:
                return None
            status = row.status
        return {
            "enrollment_id": row.enrollment_id, "current_enrollment_id": domain.enrollment_id,
            "started_at": row.started_at.isoformat(),
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "status": status, "conflicted": conflicted,
        }


def enqueue(domain_id, project_id, kind):
    from app.services.native_collection_jobs import (
        enqueue_collection,
        enqueue_readiness,
    )
    accepted = enqueue_readiness(domain_id) if kind == "readiness" else enqueue_collection(domain_id, project_id)
    return True if accepted else pending_attempt(domain_id, project_id, kind)


def pending_attempt(domain_id, project_id, kind):
    """Distinguish a known queue collision from an unavailable/ambiguous enqueue."""
    from app.core.redis import get_redis_raw
    from rq.job import Job
    redis = get_redis_raw()
    key = (f"native:readiness:{domain_id}" if kind == "readiness"
           else f"native:collection:{domain_id}:{project_id or 'domain'}") + ":pending"
    token = redis.get(key)
    if not token:
        return False
    token = token.decode() if isinstance(token, bytes) else token
    if token.startswith("cleanup-"):
        return False
    job = Job.fetch(token, connection=redis)
    function = "run_domain_readiness" if kind == "readiness" else "run_project_collection"
    expected_args = (str(domain_id),) if kind == "readiness" else (str(domain_id), str(project_id) if project_id else None)
    if job.func_name != f"app.services.native_collection_jobs.{function}" or tuple(job.args) != expected_args:
        return False
    return "busy" if job.get_status() in {"queued", "started"} else False


def verify_attempt(domain_id, project_id, kind, *, enqueue=enqueue, observe=observe, timeout=960):
    started = datetime.now(UTC).isoformat()
    deadline = time.monotonic() + timeout
    queued = enqueue(domain_id, project_id, kind)
    if not queued:
        raise RuntimeError("enqueue unavailable or already pending; inspect before retry")
    waiter = threading.Event()
    while time.monotonic() < deadline:
        status = evidence_status(observe(domain_id, project_id, kind), started)
        if status == "complete":
            return {"domain_id": domain_id, "project_id": project_id, "kind": kind, "started_at": started}
        if status == "failed":
            raise RuntimeError("fresh accounting attempt failed")
        waiter.wait(min(2, max(0, deadline - time.monotonic())))
        if queued == "busy":
            queued = enqueue(domain_id, project_id, kind)
            if not queued:
                raise RuntimeError("enqueue unavailable; inspect before retry")
    raise TimeoutError("fresh accounting attempt not completed before deadline")


def candidates():
    from app.core.database import SessionLocal
    from app.services.native_collection_jobs import dispatch_candidates
    with SessionLocal() as db:
        return list(dispatch_candidates(db))


def remote_main():
    completed = []
    try:
        initial = candidates()
        if any(cleanup for _, _, cleanup in initial):
            raise ValueError("project cleanup active")
        domains = sorted({domain for domain, _, _ in initial})
        # Coordinate first: this can enroll newly created project roots. Re-read
        # candidates afterward so projects created since the prior cycle count.
        for domain in domains:
            for kind in ("readiness", "shared"):
                completed.append(verify_attempt(domain, None, kind))
        current = candidates()
        if any(cleanup for _, _, cleanup in current):
            raise ValueError("project cleanup active")
        for domain, project, _ in current:
            if project is not None:
                completed.append(verify_attempt(domain, project, "project"))
        if set(current) != set(candidates()):
            raise ValueError("project inventory changed during verification")
        # A newer failure must invalidate an earlier successful observation.
        for item in completed:
            if evidence_status(observe(item["domain_id"], item["project_id"], item["kind"]),
                               item["started_at"]) != "complete":
                raise ValueError("accounting proof superseded")
        result = {"ready": bool(domains), "completed": completed}
    except Exception:  # noqa: BLE001 -- provider exceptions can contain credentials
        result = {"ready": False, "completed": completed, "blocker": "fresh_accounting_unverified"}
    print(json.dumps(result), flush=True)
    return 0 if result["ready"] else 1


def remote_source():
    return Path(__file__).read_text().rsplit('\nif __name__ == "__main__":', 1)[0] + "\nraise SystemExit(remote_main())\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application", nargs=2, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    reports = []
    source = remote_source()
    for kubeconfig, namespace in args.application:
        try:
            process = subprocess.run(
                ["oc", f"--kubeconfig={kubeconfig}", "--request-timeout=30s",
                 "exec", "-i", "deployment/troshka-collection-worker", "-n", namespace,
                 "-c", "worker", "--", "python3", "-"],
                input=source, text=True,
                capture_output=True, check=False, timeout=7200,
            )
            report = json.loads(process.stdout)
            report["ready"] = process.returncode == 0 and report.get("ready") is True
        except Exception:  # noqa: BLE001 -- no credential-bearing command output
            report = {"ready": False, "blocker": "accounting_check_unavailable"}
        reports.append(dict(namespace=namespace, **report))
        args.output.write_text(json.dumps({"ready": False, "applications": reports}, indent=2) + "\n")
    result = {"ready": all(r["ready"] for r in reports), "applications": reports}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
