"""Release proof rejects stale, failed, and superseded accounting attempts."""

import importlib.util
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def check():
    path = Path(__file__).resolve().parents[1] / "collector_release_check.py"
    spec = importlib.util.spec_from_file_location("collector_release_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evidence(**changes):
    return dict(enrollment_id="current", current_enrollment_id="current",
                started_at="2026-11-14T12:00:01+00:00",
                completed_at="2026-11-14T12:00:02+00:00",
                status="complete", conflicted=False, **changes)


def test_fresh_complete_evidence_passes(check):
    assert check.evidence_status(evidence(), "2026-11-14T12:00:00+00:00") == "complete"


@pytest.mark.parametrize("field,value,want", [
    ("started_at", "2026-11-14T11:59:59+00:00", "pending"),
    ("status", "failed", "failed"),
    ("status", "incomplete", "failed"),
    ("conflicted", True, "failed"),
    ("enrollment_id", "previous", "failed"),
    ("completed_at", None, "pending"),
])
def test_invalid_evidence_never_passes(check, field, value, want):
    row = evidence()
    row[field] = value
    assert check.evidence_status(row, "2026-11-14T12:00:00+00:00") == want


def test_no_evidence_is_pending(check):
    assert check.evidence_status(None, "2026-11-14T12:00:00+00:00") == "pending"


def test_health_reads_old_success_without_starting_jobs(check, monkeypatch, capsys):
    monkeypatch.setattr(check, "candidates", lambda: [("d", "p", False)])
    monkeypatch.setattr(check, "observe", lambda *_: evidence())
    monkeypatch.setattr(check, "verify_attempt", lambda *_: pytest.fail("must not scan"))
    assert check.remote_main("health") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "health"
    assert len(report["completed"]) == 3


@pytest.mark.parametrize("bad", [None, "failed", "conflicted", "enrollment"])
def test_health_blocks_missing_or_invalid_latest_evidence(check, monkeypatch, bad):
    row = evidence()
    if bad == "failed":
        row["status"] = "failed"
    elif bad == "conflicted":
        row["conflicted"] = True
    elif bad == "enrollment":
        row["enrollment_id"] = "old"
    else:
        row = None
    monkeypatch.setattr(check, "candidates", lambda: [("d", "p", False)])
    monkeypatch.setattr(check, "observe", lambda *_: row)
    assert check.remote_main("health") == 1


def test_targeted_scan_checks_only_selected_projects_and_domains(check, monkeypatch):
    rows = [("a", "one", False), ("a", "two", False), ("b", "three", False)]
    calls = []
    monkeypatch.setattr(check, "candidates", lambda: rows)
    monkeypatch.setattr(check, "observe", lambda *_: evidence())
    def verify(d, p, k):
        calls.append((d, p, k))
        return dict(domain_id=d, project_id=p, kind=k, started_at=evidence()["started_at"])
    monkeypatch.setattr(check, "verify_attempt", verify)
    assert check.remote_main("fresh", ["one"]) == 0
    assert calls == [("a", None, "readiness"), ("a", None, "shared"), ("a", "one", "project")]


def test_unknown_target_fails_before_enqueuing(check, monkeypatch):
    monkeypatch.setattr(check, "candidates", lambda: [("d", "p", False)])
    monkeypatch.setattr(check, "verify_attempt", lambda *_: pytest.fail("must not scan"))
    assert check.remote_main("fresh", ["typo"]) == 1


def test_failed_enqueue_is_not_retried_or_reported_as_success(check):
    calls = []
    def enqueue(*args):
        calls.append(args)
        return False
    with pytest.raises(RuntimeError, match="enqueue"):
        check.verify_attempt("domain", None, "readiness", enqueue=enqueue,
                             observe=lambda *_: evidence(), timeout=1)
    assert len(calls) == 1


def test_known_busy_job_requires_fresh_evidence(check, monkeypatch):
    class Waiter:
        def wait(self, seconds):
            pass
    monkeypatch.setattr(check.threading, "Event", Waiter)
    old = evidence()
    fresh = evidence()
    fresh.update(started_at="2099-01-01T00:00:00+00:00", completed_at="2099-01-01T00:00:01+00:00")
    observations = iter([old, fresh])
    requests = iter(["busy", True])
    result = check.verify_attempt("domain", None, "readiness", enqueue=lambda *_: next(requests),
                                  observe=lambda *_: next(observations), timeout=1)
    assert result["kind"] == "readiness"


def test_busy_job_with_only_stale_evidence_cannot_pass(check):
    with pytest.raises(TimeoutError):
        check.verify_attempt("domain", None, "readiness", enqueue=lambda *_: "busy",
                             observe=lambda *_: evidence(), timeout=0)


@pytest.mark.parametrize("args,status,want", [
    (("domain",), "started", "busy"),
    (("domain",), "queued", "busy"),
    (("another-domain",), "started", False),
    (("domain",), "failed", False),
])
def test_pending_collision_requires_matching_active_job(check, monkeypatch, args, status, want):
    redis = SimpleNamespace(get=lambda key: b"pending-id")
    job = SimpleNamespace(func_name="app.services.native_collection_jobs.run_domain_readiness",
                          args=args, get_status=lambda: status)
    monkeypatch.setitem(sys.modules, "app.core.redis", SimpleNamespace(get_redis_raw=lambda: redis))
    monkeypatch.setitem(sys.modules, "rq.job", SimpleNamespace(Job=SimpleNamespace(fetch=lambda *a, **k: job)))
    assert check.pending_attempt("domain", None, "readiness") == want


def test_fresh_failure_stops_without_waiting_for_older_success(check):
    row = evidence()
    row.update(started_at="2099-01-01T00:00:00+00:00", status="failed")
    with pytest.raises(RuntimeError, match="failed"):
        check.verify_attempt("domain", None, "readiness", enqueue=lambda *_: True,
                             observe=lambda *_: row, timeout=1)


def test_missing_evidence_times_out(check):
    with pytest.raises(TimeoutError):
        check.verify_attempt("domain", None, "readiness", enqueue=lambda *_: True,
                             observe=lambda *_: None, timeout=0)


def test_cleanup_candidate_prevents_release_jobs(check, monkeypatch, capsys):
    monkeypatch.setattr(check, "candidates", lambda: [("domain", "project", True)])
    def unexpected(*args):
        pytest.fail("release check must not drive active cleanup")
    monkeypatch.setattr(check, "verify_attempt", unexpected)
    assert check.remote_main() == 1
    assert '"ready": false' in capsys.readouterr().out


def test_new_project_after_coordination_is_checked(check, monkeypatch, capsys):
    initial = [("domain", None, False)]
    current = initial + [("domain", "new-project", False)]
    rounds = iter([initial, current, current])
    monkeypatch.setattr(check, "candidates", lambda: next(rounds))
    def verify(domain, project, kind):
        return {"domain_id": domain, "project_id": project, "kind": kind,
                "started_at": "2026-11-14T12:00:00+00:00"}
    monkeypatch.setattr(check, "verify_attempt", verify)
    monkeypatch.setattr(check, "observe", lambda *_: evidence())
    assert check.remote_main() == 0
    assert '"project_id": "new-project", "kind": "project"' in capsys.readouterr().out


def test_remote_payload_is_valid_python(check):
    compile(check.remote_source(), "remote-check", "exec")


def test_project_checks_overlap_with_two_active_limit(check, monkeypatch, capsys):
    rows = [("domain", None, False)] + [("domain", str(i), False) for i in range(5)]
    monkeypatch.setattr(check, "candidates", lambda: rows)
    monkeypatch.setattr(check, "observe", lambda *_: evidence())
    both_started, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    calls, active, peak = [], 0, 0

    def verify(domain, project, kind):
        nonlocal active, peak
        with lock:
            calls.append((project, kind))
            if project is not None:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    both_started.set()
        if project is not None:
            assert release.wait(5)
            with lock:
                active -= 1
        return {"domain_id": domain, "project_id": project, "kind": kind,
                "started_at": "2026-11-14T12:00:00+00:00"}

    monkeypatch.setattr(check, "verify_attempt", verify)
    with ThreadPoolExecutor(max_workers=1) as runner:
        result = runner.submit(check.remote_main)
        try:
            assert both_started.wait(2), "project verification remained serial"
            with lock:
                assert calls[:2] == [(None, "readiness"), (None, "shared")]
                assert len(calls) == 4  # Remaining projects have not started yet.
        finally:
            release.set()
        assert result.result(timeout=5) == 0
    assert peak == 2
    assert sorted(p for p, _ in calls if p is not None) == [str(i) for i in range(5)]
    assert len(json.loads(capsys.readouterr().out)["completed"]) == 7


def test_parallel_failure_keeps_other_inflight_success(check, monkeypatch, capsys):
    rows = [("domain", None, False), ("domain", "bad", False), ("domain", "good", False)]
    monkeypatch.setattr(check, "candidates", lambda: rows)
    started = threading.Barrier(2, timeout=2)

    def verify(domain, project, kind):
        if project is not None:
            started.wait()
            if project == "bad":
                raise RuntimeError("sensitive provider error must remain hidden")
        return {"domain_id": domain, "project_id": project, "kind": kind,
                "started_at": "2026-11-14T12:00:00+00:00"}

    monkeypatch.setattr(check, "verify_attempt", verify)
    assert check.remote_main() == 1
    report = json.loads(capsys.readouterr().out)
    assert [v["project_id"] for v in report["completed"]] == [None, None, "good"]
    assert "sensitive" not in json.dumps(report)


@pytest.mark.parametrize("change", ["cleanup", "inventory", "superseded"])
def test_parallel_release_preserves_final_safety_gates(check, monkeypatch, capsys, change):
    initial = [("domain", None, False)]
    current = initial + [("domain", "a", change == "cleanup"), ("domain", "b", False)]
    final = current + [("domain", "new", False)] if change == "inventory" else current
    rounds = iter([initial, current, final])
    monkeypatch.setattr(check, "candidates", lambda: next(rounds))

    def verify(domain, project, kind):
        return {"domain_id": domain, "project_id": project, "kind": kind,
                "started_at": "2026-11-14T12:00:00+00:00"}

    def observe(domain, project, kind):
        row = evidence()
        if change == "superseded" and project == "a":
            row["status"] = "failed"
        return row

    monkeypatch.setattr(check, "verify_attempt", verify)
    monkeypatch.setattr(check, "observe", observe)
    assert check.remote_main() == 1
    report = json.loads(capsys.readouterr().out)
    assert len(report["completed"]) == (2 if change == "cleanup" else 4)
