from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import native_collection_jobs as jobs


def test_explicit_refresh_runs_with_scheduler_disabled_and_preserves_leases(
    monkeypatch,
):
    redis = RedisMemory()
    queued = MagicMock()
    monkeypatch.setattr(jobs, "settings", lambda: {"enabled": False})
    monkeypatch.setattr(jobs, "get_redis_raw", lambda: redis)
    monkeypatch.setattr(jobs, "Queue", lambda *args, **kwargs: queued)
    assert jobs.enqueue_collection("domain", "project", observation_only=True)
    assert not jobs.enqueue_collection("domain", "project", observation_only=True)
    call = queued.enqueue.call_args
    assert call.args == (jobs.run_project_refresh, "domain", "project")
    monkeypatch.setattr(
        jobs,
        "get_current_job",
        lambda: SimpleNamespace(id=call.kwargs["job_id"], origin="native_collection"),
    )
    monkeypatch.setattr(jobs, "_identities", lambda *args: (["provider"], ["fsid"]))
    runtime = MagicMock(return_value="report")
    monkeypatch.setattr(
        "app.services.native_collection_runtime.run_collection", runtime
    )
    assert jobs.run_project_refresh("domain", "project") == "report"
    runtime.assert_called_once_with("domain", "project", observation_only=True)
    assert redis.values == {}


def test_observation_refresh_rejects_domain_and_cleanup():
    with pytest.raises(ValueError):
        jobs.enqueue_collection("domain", observation_only=True)
    with pytest.raises(ValueError):
        jobs.enqueue_collection(
            "domain", "project", cleanup=True, observation_only=True
        )


def test_refresh_with_cleanup_intent_stays_observation_only(monkeypatch):
    redis = RedisMemory()
    redis.set(jobs._key("domain", "project") + ":pending", "refresh", ex=300)
    monkeypatch.setattr(jobs, "get_redis_raw", lambda: redis)
    monkeypatch.setattr(
        jobs,
        "get_current_job",
        lambda: SimpleNamespace(id="refresh", origin="native_collection"),
    )
    monkeypatch.setattr(jobs, "_identities", lambda *args: (["provider"], ["fsid"]))
    monkeypatch.setattr(jobs, "_cleanup_due", lambda project: True)
    enqueue = MagicMock()
    monkeypatch.setattr(jobs, "enqueue_collection", enqueue)
    runtime = MagicMock(return_value="report")
    monkeypatch.setattr(
        "app.services.native_collection_runtime.run_collection", runtime
    )
    assert jobs.run_project_refresh("domain", "project") == "report"
    runtime.assert_called_once_with("domain", "project", observation_only=True)
    enqueue.assert_not_called()
    assert redis.values == {}


def test_redis_failure_never_enqueues():
    redis = MagicMock()
    redis.ping.side_effect = ConnectionError()
    with (
        patch.object(jobs, "get_redis_raw", return_value=redis),
        patch.object(jobs, "Queue") as queue,
    ):
        assert jobs.enqueue_collection("domain", "project") is False
        queue.assert_not_called()


def test_duplicate_does_not_enqueue():
    redis = MagicMock()
    redis.set.return_value = False
    with (
        patch.object(jobs, "get_redis_raw", return_value=redis),
        patch.object(jobs, "Queue") as queue,
    ):
        assert jobs.enqueue_collection("domain", "project") is False
        queue.assert_not_called()


def test_identifier_payload_and_cleanup_priority():
    redis = MagicMock()
    with (
        patch.object(jobs, "get_redis_raw", return_value=redis),
        patch.object(jobs, "Queue") as queue,
    ):
        assert jobs.enqueue_collection("domain", "project", cleanup=True)
        assert queue.call_args.args == ("native_cleanup",)
        call = queue.return_value.enqueue.call_args
        assert call.args[1:] == ("domain", "project")
        assert "project_id" not in call.kwargs
        assert all(
            not str(c.args[0]).startswith("job:project:")
            for c in redis.set.call_args_list
        )


def test_release_uses_owner_token():
    redis = MagicMock()
    jobs.release_lease(redis, "key", "owner")
    assert redis.eval.call_args.args[1:] == (1, "key", "owner")
    assert "get" in redis.eval.call_args.args[0]


def test_lease_fences_late_publication():
    redis = MagicMock()
    redis.get.return_value = b"replacement"
    context = jobs._execution.set((redis, {"key": "old"}))
    try:
        with pytest.raises(RuntimeError, match="lease"):
            jobs.assert_collection_lease()
    finally:
        jobs._execution.reset(context)


def test_capacity_defers_without_runtime():
    redis = MagicMock()
    redis.set.return_value = False
    with (
        patch.object(jobs, "get_redis_raw", return_value=redis),
        patch.object(jobs, "_identities", return_value=(["p"], ["fsid"])),
        patch.object(jobs, "get_current_job", return_value=None),
    ):
        assert jobs.run_project_collection("d", "p") == "deferred"


def test_queue_profiles_isolate_lifecycle():
    from app.workers.deploy_worker import worker_queues

    assert worker_queues("collection") == ["native_cleanup", "native_collection"]
    assert worker_queues("readiness") == ["native_readiness"]
    assert worker_queues("lifecycle") == [
        "project_lifecycle",
        "host_lifecycle",
        "default",
    ]
    with pytest.raises(ValueError):
        worker_queues("everything")


class RedisMemory:
    """Small atomic Redis contract fake with explicit simulated clock."""

    def __init__(self):
        self.values = {}
        self.now = 0

    def ping(self):
        return True

    def get(self, key):
        value = self.values.get(key)
        return value[0] if value and value[1] > self.now else None

    def set(self, key, value, *, nx=False, ex, xx=False):
        if nx and self.get(key) is not None:
            return False
        if xx and self.get(key) is None:
            return False
        self.values[key] = (value, self.now + ex)
        return True

    def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.values:
                del self.values[key]
                removed += 1
        return removed

    def eval(self, script, count, key, *args):
        if count == 2:
            executing, token, ttl = args
            pending = self.get(key)
            if pending is None or pending.startswith("cleanup-") or self.get(executing):
                return 0
            self.set(key, token, ex=ttl)
            return 1
        (token,) = args
        if self.get(key) != token:
            return 0
        del self.values[key]
        return 1


def test_crash_expiry_and_old_owner_cannot_release_replacement():
    redis = RedisMemory()
    owned = {}
    with patch.object(jobs, "settings", return_value={"provider_slots": 1}):
        assert jobs._acquire_slots(redis, ["backend"], "dead", owned)
        assert not jobs._acquire_slots(redis, ["backend"], "new", {})
        redis.now += jobs._timeout() + 61
        assert jobs._acquire_slots(redis, ["backend"], "new", {})
        jobs.release_lease(redis, "backend:0", "dead")
        assert redis.get("backend:0") == "new"


def test_shared_backend_limits_provider_aliases():
    redis = RedisMemory()
    with patch.object(jobs, "settings", return_value={"provider_slots": 1}):
        assert jobs._acquire_slots(redis, ["provider:A", "backend:fsid"], "first", {})
        assert not jobs._acquire_slots(
            redis, ["provider:B", "backend:fsid"], "second", {}
        )


def test_runtime_failure_releases_all_owned_tokens():
    redis = RedisMemory()
    runtime = SimpleNamespace(
        run_collection=MagicMock(side_effect=ValueError("failed"))
    )
    with (
        patch.object(jobs, "get_redis_raw", return_value=redis),
        patch.object(jobs, "_identities", return_value=(["provider"], ["fsid"])),
        patch.object(jobs, "get_current_job", return_value=None),
        patch.dict("sys.modules", {"app.services.native_collection_runtime": runtime}),
    ):
        with pytest.raises(ValueError, match="failed"):
            jobs.run_project_collection("domain", "project")
    assert redis.values == {}


def test_expired_queued_job_cannot_start_runtime():
    redis = RedisMemory()
    job = SimpleNamespace(id="expired", origin="native_collection")
    with (
        patch.object(jobs, "get_redis_raw", return_value=redis),
        patch.object(jobs, "get_current_job", return_value=job),
        patch.object(jobs, "_identities") as identities,
    ):
        with pytest.raises(RuntimeError, match="lease"):
            jobs.run_project_collection("domain", "project")
        identities.assert_not_called()


def test_cleanup_reprioritized_when_queued_routine_starts():
    redis = RedisMemory()
    key = jobs._key("domain", "project") + ":pending"
    redis.set(key, "job", ex=300)
    job = SimpleNamespace(id="job", origin="native_collection")
    with (
        patch.object(jobs, "get_redis_raw", return_value=redis),
        patch.object(jobs, "get_current_job", return_value=job),
        patch.object(jobs, "_cleanup_due", return_value=True),
        patch.object(jobs, "Queue") as queue,
    ):
        assert jobs.run_project_collection("domain", "project") == "reprioritized"
        assert queue.call_args.args == ("native_cleanup",)
        assert redis.get(key) != "job"
        assert redis.get(key) is not None  # old finally did not delete new owner


def test_500_projects_two_dispatchers_have_no_duplicate_pending_jobs():
    redis = RedisMemory()
    candidates = [("domain", str(n), False) for n in range(500)]
    with (
        patch.object(jobs, "get_redis_raw", return_value=redis),
        patch.object(jobs, "settings", return_value={"enabled": True}),
        patch.object(jobs, "dispatch_candidates", return_value=candidates),
        patch("app.core.database.SessionLocal"),
        patch.object(jobs, "Queue") as queue,
    ):
        assert jobs.dispatch_once() == 500
        assert jobs.dispatch_once() == 0
        redis.now += 72  # another periodic pass, while first jobs remain pending
        assert jobs.dispatch_once() == 0
        assert queue.return_value.enqueue.call_count == 500
        assert (
            len(
                {
                    call.kwargs["job_id"]
                    for call in queue.return_value.enqueue.call_args_list
                }
            )
            == 500
        )


def test_cleanup_promotes_queued_project_past_500_routine_jobs():
    redis = RedisMemory()
    with (
        patch.object(jobs, "get_redis_raw", return_value=redis),
        patch.object(jobs, "Queue") as queue,
    ):
        for project in range(500):
            assert jobs.enqueue_collection("domain", str(project))
        key = jobs._key("domain", "499") + ":pending"
        old_token = redis.get(key)
        assert jobs.enqueue_collection("domain", "499", cleanup=True)
        assert queue.call_args.args == ("native_cleanup",)
        assert not jobs.enqueue_collection("domain", "499", cleanup=True)
        assert queue.return_value.enqueue.call_count == 501
        assert redis.get(key) != old_token
        old_job = SimpleNamespace(id=old_token, origin="native_collection")
        with patch.object(jobs, "get_current_job", return_value=old_job):
            with pytest.raises(RuntimeError, match="lease"):
                jobs.run_project_collection("domain", "499")
        assert redis.get(key).startswith("cleanup-")


def test_cleanup_cannot_supersede_running_project():
    redis = RedisMemory()
    with (
        patch.object(jobs, "get_redis_raw", return_value=redis),
        patch.object(jobs, "Queue") as queue,
    ):
        assert jobs.enqueue_collection("domain", "project")
        base = jobs._key("domain", "project")
        token = redis.get(base + ":pending")
        redis.set(base + ":executing", token, ex=300)
        assert not jobs.enqueue_collection("domain", "project", cleanup=True)
        assert redis.get(base + ":pending") == token
        assert queue.return_value.enqueue.call_count == 1


def test_cleanup_dispatch_does_not_wait_for_routine_due_interval():
    redis = RedisMemory()
    with (
        patch.object(jobs, "get_redis_raw", return_value=redis),
        patch.object(jobs, "settings", return_value={"enabled": True}),
        patch("app.core.database.SessionLocal"),
        patch.object(jobs, "Queue"),
        patch.object(jobs, "dispatch_candidates") as candidates,
    ):
        candidates.return_value = [("domain", "project", False)]
        assert jobs.dispatch_once() == 1
        candidates.return_value = [("domain", "project", True)]
        assert jobs.dispatch_once() == 1  # no simulated time advance


def test_disabled_routine_collection_still_dispatches_only_pending_cleanup():
    redis = RedisMemory()
    with (
        patch.object(jobs, "get_redis_raw", return_value=redis),
        patch.object(jobs, "settings", return_value={"enabled": False}),
        patch("app.core.database.SessionLocal"),
        patch.object(jobs, "Queue") as queue,
        patch.object(
            jobs,
            "dispatch_candidates",
            return_value=[
                ("domain", None, False),
                ("domain", "ordinary", False),
                ("domain", "retiring", True),
            ],
        ),
    ):
        assert jobs.dispatch_once() == 1
        assert jobs.dispatch_once() == 0
        assert queue.call_args.args == ("native_cleanup",)
        assert queue.return_value.enqueue.call_count == 1
        assert queue.return_value.enqueue.call_args.args[1:] == ("domain", "retiring")


def test_disabled_routine_collection_without_cleanup_does_not_require_redis():
    with (
        patch.object(jobs, "settings", return_value={"enabled": False}),
        patch("app.core.database.SessionLocal"),
        patch.object(jobs, "get_redis_raw") as redis,
        patch.object(
            jobs, "dispatch_candidates", return_value=[("domain", None, False)]
        ),
    ):
        assert jobs.dispatch_once() == 0
        redis.assert_not_called()


def test_cleanup_dispatcher_starts_when_routine_collection_disabled():
    with (
        patch.object(jobs, "settings", return_value={"enabled": False}),
        patch.object(jobs.threading, "Thread") as thread,
    ):
        stop = jobs.start_collection_dispatcher()
        thread.return_value.start.assert_called_once()
        stop.set()


def test_readiness_dispatch_interval_preserves_shared_freshness_headroom():
    redis = RedisMemory()
    with (
        patch.object(jobs, "get_redis_raw", return_value=redis),
        patch.object(
            jobs, "settings", return_value={"enabled": True, "interval_seconds": 600}
        ),
        patch("app.core.database.SessionLocal"),
        patch.object(jobs, "Queue"),
        patch.object(
            jobs, "dispatch_candidates", return_value=[("domain", None, False)]
        ),
    ):
        assert jobs.dispatch_once() == 2
        expires = redis.values[jobs._readiness_key("domain") + ":due"][1]
        assert 20 <= expires <= 25


def test_readiness_queue_and_slots_are_independent_of_storage_backlog():
    redis = RedisMemory()
    runtime = SimpleNamespace(run_readiness=MagicMock(return_value="fresh"))
    with (
        patch.object(jobs, "get_redis_raw", return_value=redis),
        patch.object(jobs, "Queue") as queue,
    ):
        for project in range(500):
            assert jobs.enqueue_collection("domain", str(project))
        assert jobs.enqueue_collection("domain")
        assert jobs.enqueue_readiness("domain")
        assert queue.call_args.args == ("native_readiness",)
        assert queue.return_value.enqueue.call_args.args[1:] == ("domain",)
        assert not jobs.enqueue_readiness("domain")
        redis.set("native:provider:provider:0", "heavy", ex=300)
        redis.set("native:backend:fsid:0", "heavy", ex=300)
        token = redis.get(jobs._readiness_key("domain") + ":pending")
        job = SimpleNamespace(id=token, origin="native_readiness")
        with (
            patch.object(jobs, "get_current_job", return_value=job),
            patch.object(jobs, "_identities", return_value=(["provider"], ["fsid"])),
            patch.dict(
                "sys.modules", {"app.services.native_collection_runtime": runtime}
            ),
            patch.object(jobs, "settings", return_value={"provider_slots": 1}),
        ):
            assert jobs.run_domain_readiness("domain") == "fresh"
        assert redis.get("native:backend:fsid:0") == "heavy"


def test_readiness_has_one_separate_backend_slot():
    redis = RedisMemory()
    assert jobs._acquire_slots(
        redis, ["native:readiness:backend:fsid"], "first", {}, readiness=True
    )
    assert not jobs._acquire_slots(
        redis, ["native:readiness:backend:fsid"], "second", {}, readiness=True
    )


def test_readiness_reservations_cover_queue_wait_and_longer_execution():
    redis = RedisMemory()
    with (
        patch.object(jobs, "settings", return_value={}),
        patch.object(jobs, "get_redis_raw", return_value=redis),
        patch.object(jobs, "Queue") as queue,
    ):
        assert jobs.enqueue_readiness("domain")
        call = queue.return_value.enqueue.call_args
        assert call.kwargs["job_timeout"] == 60
        assert call.kwargs["ttl"] == 60
        assert jobs._acquire_slots(redis, ["readiness"], "owner", {}, readiness=True)
        redis.now = 119
        assert redis.get("readiness:0") == "owner"
        redis.now = 120
        assert redis.get("readiness:0") is None
        redis.now = 179
        assert redis.get(jobs._readiness_key("domain") + ":pending") is not None
        redis.now = 180
        assert redis.get(jobs._readiness_key("domain") + ":pending") is None


def test_settlement_phases_poll_faster_than_teardown():
    from app.services import native_project_namespace_cleanup as cleanup

    redis = RedisMemory()
    with patch("app.core.redis.enqueue_job") as enqueue:
        assert (
            cleanup.dispatch_pending(
                redis, [("ns-absent", "namespace_absent"), ("fencing", "fencing")]
            )
            == 2
        )
        assert redis.values["native:namespace-cleanup:ns-absent"][1] == 10
        assert redis.values["native:namespace-cleanup:fencing"][1] == 60
        assert enqueue.call_count == 2
        assert cleanup.dispatch_pending(redis, [("ns-absent", "namespace_absent")]) == 0


def test_cleanup_dispatch_does_not_overlap_active_execution():
    from app.services import native_project_namespace_cleanup as cleanup

    redis = RedisMemory()
    redis.set("native:namespace-cleanup:cid:executing", "worker", ex=1200)
    redis.now = 61
    with patch("app.core.redis.enqueue_job") as enqueue:
        assert cleanup.dispatch_pending(redis, [("cid", "fencing")]) == 0
        enqueue.assert_not_called()


def test_duplicate_cleanup_job_never_enters_database(monkeypatch):
    from app.services import native_project_namespace_cleanup as cleanup

    redis = RedisMemory()
    redis.set("native:namespace-cleanup:cid:executing", "original", ex=1200)
    monkeypatch.setattr("app.core.redis.get_redis_raw", lambda: redis)
    monkeypatch.setattr(
        "rq.get_current_job", lambda: SimpleNamespace(id="duplicate", timeout=1200)
    )
    database = MagicMock(side_effect=AssertionError("duplicate entered database"))
    monkeypatch.setattr("app.core.database.SessionLocal", database)
    cleanup.reconcile("cid")
    database.assert_not_called()
    assert redis.get("native:namespace-cleanup:cid:executing") == "original"


@pytest.mark.parametrize("outcome", ["success", "error", "replacement"])
def test_cleanup_execution_lease_spans_poll_interval_and_releases_only_owner(
    monkeypatch, outcome
):
    from app.services import native_project_namespace_cleanup as cleanup

    redis = RedisMemory()
    key = "native:namespace-cleanup:cid:executing"
    monkeypatch.setattr("app.core.redis.get_redis_raw", lambda: redis)
    monkeypatch.setattr(
        "rq.get_current_job", lambda: SimpleNamespace(id="owner", timeout=1200)
    )
    calls = []

    def work(cleanup_id):
        calls.append(cleanup_id)
        redis.now = 61
        assert redis.get(key) == "owner"
        cleanup.reconcile(cleanup_id)
        if outcome == "error":
            raise RuntimeError("provider unavailable")
        if outcome == "replacement":
            redis.now = 1261
            assert redis.get(key) is None
            redis.set(key, "replacement", ex=1260)

    monkeypatch.setattr(cleanup, "_reconcile", work)
    if outcome == "error":
        with pytest.raises(RuntimeError, match="provider unavailable"):
            cleanup.reconcile("cid")
    else:
        cleanup.reconcile("cid")
    assert calls == ["cid"]
    assert redis.get(key) == ("replacement" if outcome == "replacement" else None)


def test_successful_cleanup_collection_nudges_settlement(monkeypatch):
    redis = RedisMemory()
    nudged = MagicMock(return_value=1)
    monkeypatch.setattr(jobs, "get_redis_raw", lambda: redis)
    monkeypatch.setattr(jobs, "_identities", lambda *a: (["provider"], ["fsid"]))
    monkeypatch.setattr(jobs, "get_current_job", lambda: None)
    monkeypatch.setattr(jobs, "_cleanup_due", lambda project: True)
    monkeypatch.setattr(
        "app.services.native_project_namespace_cleanup.nudge_settlement", nudged
    )
    runtime = MagicMock(return_value="report-id")
    monkeypatch.setattr(
        "app.services.native_collection_runtime.run_collection", runtime
    )
    assert jobs.run_project_collection("domain", "project") == "report-id"
    nudged.assert_called_once_with("project")


def test_nudge_settlement_clears_poll_lock_and_enqueues(monkeypatch):
    from app.services import native_project_namespace_cleanup as cleanup

    redis = RedisMemory()
    redis.set("native:namespace-cleanup:cid", "queued", ex=60)
    monkeypatch.setattr(
        cleanup,
        "states_for_project",
        lambda project_id: [
            SimpleNamespace(id="cid", phase="namespace_absent"),
        ],
    )
    monkeypatch.setattr("app.core.redis.get_redis_raw", lambda: redis)
    with patch("app.core.redis.enqueue_job") as enqueue:
        assert cleanup.nudge_settlement("project") == 1
        assert enqueue.call_args.args[0] is cleanup.reconcile
        assert enqueue.call_args.args[1] == "cid"
        assert redis.values["native:namespace-cleanup:cid"][1] == 10
