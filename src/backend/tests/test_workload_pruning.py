import datetime

from app.models.workload_run import WorkloadRun
from app.services.workloads.run_service import prune_workload_runs
from tests.conftest import TestSession


def test_prune_deletes_old_terminal_runs():
    db = TestSession()
    try:
        old = WorkloadRun(
            kind="ad_hoc",
            role_fqcn="x",
            status="succeeded",
            ended_at=datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC),
        )
        recent = WorkloadRun(
            kind="ad_hoc",
            role_fqcn="y",
            status="succeeded",
            ended_at=datetime.datetime.now(datetime.UTC),
        )
        running = WorkloadRun(kind="ad_hoc", role_fqcn="z", status="running")
        db.add_all([old, recent, running])
        db.commit()
        old_id, recent_id, running_id = old.id, recent.id, running.id
        n = prune_workload_runs(db, retention_days=30)
        assert n == 1
        assert db.get(WorkloadRun, old_id) is None
        assert db.get(WorkloadRun, recent_id) is not None
        assert db.get(WorkloadRun, running_id) is not None  # never prune running
    finally:
        db.close()
