from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import native_collection_jobs as jobs


def test_redis_failure_never_enqueues():
    redis = MagicMock()
    redis.ping.side_effect = ConnectionError()
    with patch.object(jobs, "get_redis_raw", return_value=redis), patch.object(
        jobs, "Queue"
    ) as queue:
        assert jobs.enqueue_collection("domain", "project") is False
        queue.assert_not_called()


def test_duplicate_does_not_enqueue():
    redis = MagicMock()
    redis.set.return_value = False
    with patch.object(jobs, "get_redis_raw", return_value=redis), patch.object(
        jobs, "Queue"
    ) as queue:
        assert jobs.enqueue_collection("domain", "project") is False
        queue.assert_not_called()


def test_identifier_payload_and_cleanup_priority():
    redis = MagicMock()
    with patch.object(jobs, "get_redis_raw", return_value=redis), patch.object(
        jobs, "Queue"
    ) as queue:
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

    def set(self, key, value, *, nx=False, ex):
        if nx and self.get(key) is not None:
            return False
        self.values[key] = (value, self.now + ex)
        return True

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
    with patch.object(jobs, "get_redis_raw", return_value=redis), patch.object(
        jobs, "Queue"
    ) as queue:
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
    with patch.object(jobs, "get_redis_raw", return_value=redis), patch.object(
        jobs, "Queue"
    ) as queue:
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
    with patch.object(jobs, "get_redis_raw", return_value=redis), patch.object(
        jobs, "Queue"
    ) as queue:
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
