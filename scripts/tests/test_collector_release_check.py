"""Release proof rejects stale, failed, and superseded accounting attempts."""

import importlib.util
import sys
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
