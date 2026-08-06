"""Tests for uncovered functions in app.core.redis — state helpers, locks,
semaphores, pub/sub, rate limiting, enqueue_job, get_job_info, get_redis_raw.

Covers both the in-memory fallback path (_redis_available=False) and the
mocked-Redis path (_redis_available=True), plus ConnectionError fallbacks.
"""

import json
import threading
from unittest.mock import MagicMock, patch

import redis as _redis

import app.core.redis as redis_mod

# ── Helpers to save / restore module-level state ──


def _save_state():
    return {
        "_redis_available": redis_mod._redis_available,
        "_client": redis_mod._client,
        "_client_raw": redis_mod._client_raw,
        "_mem_progress": dict(redis_mod._mem_progress),
        "_mem_cancelled": set(redis_mod._mem_cancelled),
        "_mem_sets": {k: set(v) for k, v in redis_mod._mem_sets.items()},
        "_mem_counters": dict(redis_mod._mem_counters),
        "_pubsub_callbacks": dict(redis_mod._pubsub_callbacks),
    }


def _restore_state(state):
    redis_mod._redis_available = state["_redis_available"]
    redis_mod._client = state["_client"]
    redis_mod._client_raw = state["_client_raw"]
    redis_mod._mem_progress.clear()
    redis_mod._mem_progress.update(state["_mem_progress"])
    redis_mod._mem_cancelled.clear()
    redis_mod._mem_cancelled.update(state["_mem_cancelled"])
    redis_mod._mem_sets.clear()
    redis_mod._mem_sets.update(state["_mem_sets"])
    redis_mod._mem_counters.clear()
    redis_mod._mem_counters.update(state["_mem_counters"])
    with redis_mod._pubsub_lock:
        redis_mod._pubsub_callbacks.clear()
        redis_mod._pubsub_callbacks.update(state["_pubsub_callbacks"])


def _set_inmemory():
    """Configure module for in-memory fallback."""
    redis_mod._redis_available = False
    # Set _client to a sentinel so is_redis_available() won't call get_redis()
    # and accidentally connect to a real Redis instance.
    redis_mod._client = MagicMock()
    redis_mod._mem_progress.clear()
    redis_mod._mem_cancelled.clear()
    redis_mod._mem_sets.clear()
    redis_mod._mem_counters.clear()


def _set_redis_mock():
    """Configure module for mocked Redis and return the mock client."""
    mock_client = MagicMock()
    redis_mod._redis_available = True
    redis_mod._client = mock_client
    return mock_client


# ═══════════════════════════════════════════════════════════════════
# 1. State helpers — set_progress, get_progress, delete_progress
# ═══════════════════════════════════════════════════════════════════


class TestSetProgress:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_set_progress_inmemory(self):
        _set_inmemory()
        redis_mod.set_progress("deploy:p1", {"pct": 50})
        assert redis_mod._mem_progress["deploy:p1"] == {"pct": 50}

    def test_set_progress_redis(self):
        mc = _set_redis_mock()
        mc.set.return_value = True
        redis_mod.set_progress("deploy:p2", {"pct": 75}, ttl=600)
        mc.set.assert_called_once_with(
            "progress:deploy:p2", json.dumps({"pct": 75}), ex=600
        )

    def test_set_progress_redis_connection_error_falls_back(self):
        mc = _set_redis_mock()
        mc.set.side_effect = _redis.ConnectionError("gone")
        redis_mod.set_progress("deploy:p3", {"pct": 10})
        assert redis_mod._mem_progress["deploy:p3"] == {"pct": 10}


class TestGetProgress:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_get_progress_inmemory_found(self):
        _set_inmemory()
        redis_mod._mem_progress["k1"] = {"x": 1}
        assert redis_mod.get_progress("k1") == {"x": 1}

    def test_get_progress_inmemory_missing(self):
        _set_inmemory()
        assert redis_mod.get_progress("nope") is None

    def test_get_progress_redis_found(self):
        mc = _set_redis_mock()
        mc.get.return_value = json.dumps({"pct": 42})
        result = redis_mod.get_progress("k2")
        mc.get.assert_called_once_with("progress:k2")
        assert result == {"pct": 42}

    def test_get_progress_redis_missing(self):
        mc = _set_redis_mock()
        mc.get.return_value = None
        assert redis_mod.get_progress("missing") is None

    def test_get_progress_redis_connection_error_falls_back(self):
        mc = _set_redis_mock()
        mc.get.side_effect = _redis.ConnectionError("down")
        redis_mod._mem_progress["k3"] = {"fallback": True}
        assert redis_mod.get_progress("k3") == {"fallback": True}


class TestDeleteProgress:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_delete_progress_inmemory(self):
        _set_inmemory()
        redis_mod._mem_progress["d1"] = {"x": 1}
        redis_mod.delete_progress("d1")
        assert "d1" not in redis_mod._mem_progress

    def test_delete_progress_inmemory_missing(self):
        _set_inmemory()
        redis_mod.delete_progress("nonexistent")  # should not raise

    def test_delete_progress_redis(self):
        mc = _set_redis_mock()
        redis_mod.delete_progress("d2")
        mc.delete.assert_called_once_with("progress:d2")

    def test_delete_progress_redis_connection_error(self):
        mc = _set_redis_mock()
        mc.delete.side_effect = _redis.ConnectionError("down")
        redis_mod._mem_progress["d3"] = {"y": 2}
        redis_mod.delete_progress("d3")
        # Falls through to in-memory cleanup
        assert "d3" not in redis_mod._mem_progress


# ═══════════════════════════════════════════════════════════════════
# 2. Cancelled helpers — mark_cancelled, is_cancelled, clear_cancelled
# ═══════════════════════════════════════════════════════════════════


class TestMarkCancelled:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_mark_cancelled_inmemory(self):
        _set_inmemory()
        redis_mod.mark_cancelled("job1")
        assert "job1" in redis_mod._mem_cancelled

    def test_mark_cancelled_redis(self):
        mc = _set_redis_mock()
        mc.set.return_value = True
        redis_mod.mark_cancelled("job2", ttl=120)
        mc.set.assert_called_once_with("cancelled:job2", "1", ex=120)

    def test_mark_cancelled_redis_connection_error(self):
        mc = _set_redis_mock()
        mc.set.side_effect = _redis.ConnectionError("err")
        redis_mod.mark_cancelled("job3")
        assert "job3" in redis_mod._mem_cancelled


class TestIsCancelled:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_is_cancelled_inmemory_true(self):
        _set_inmemory()
        redis_mod._mem_cancelled.add("c1")
        assert redis_mod.is_cancelled("c1") is True

    def test_is_cancelled_inmemory_false(self):
        _set_inmemory()
        assert redis_mod.is_cancelled("c2") is False

    def test_is_cancelled_redis_true(self):
        mc = _set_redis_mock()
        mc.get.return_value = "1"
        assert redis_mod.is_cancelled("c3") is True
        mc.get.assert_called_once_with("cancelled:c3")

    def test_is_cancelled_redis_false(self):
        mc = _set_redis_mock()
        mc.get.return_value = None
        assert redis_mod.is_cancelled("c4") is False

    def test_is_cancelled_redis_connection_error(self):
        mc = _set_redis_mock()
        mc.get.side_effect = _redis.ConnectionError("err")
        redis_mod._mem_cancelled.add("c5")
        assert redis_mod.is_cancelled("c5") is True


class TestClearCancelled:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_clear_cancelled_inmemory(self):
        _set_inmemory()
        redis_mod._mem_cancelled.add("cc1")
        redis_mod.clear_cancelled("cc1")
        assert "cc1" not in redis_mod._mem_cancelled

    def test_clear_cancelled_inmemory_missing(self):
        _set_inmemory()
        redis_mod.clear_cancelled("nope")  # should not raise

    def test_clear_cancelled_redis(self):
        mc = _set_redis_mock()
        redis_mod.clear_cancelled("cc2")
        mc.delete.assert_called_once_with("cancelled:cc2")

    def test_clear_cancelled_redis_connection_error(self):
        mc = _set_redis_mock()
        mc.delete.side_effect = _redis.ConnectionError("err")
        redis_mod._mem_cancelled.add("cc3")
        redis_mod.clear_cancelled("cc3")
        assert "cc3" not in redis_mod._mem_cancelled


# ═══════════════════════════════════════════════════════════════════
# 3. Set helpers — add_to_set, remove_from_set, is_in_set
# ═══════════════════════════════════════════════════════════════════


class TestAddToSet:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_add_to_set_inmemory(self):
        _set_inmemory()
        redis_mod.add_to_set("hosts", "h1")
        assert "h1" in redis_mod._mem_sets["hosts"]

    def test_add_to_set_inmemory_creates_set(self):
        _set_inmemory()
        redis_mod.add_to_set("new_set", "val")
        assert redis_mod._mem_sets["new_set"] == {"val"}

    def test_add_to_set_redis(self):
        mc = _set_redis_mock()
        redis_mod.add_to_set("hosts", "h2")
        mc.sadd.assert_called_once_with("hosts", "h2")

    def test_add_to_set_redis_with_ttl(self):
        mc = _set_redis_mock()
        redis_mod.add_to_set("hosts", "h3", ttl=300)
        mc.sadd.assert_called_once_with("hosts", "h3")
        mc.expire.assert_called_once_with("hosts", 300)

    def test_add_to_set_redis_without_ttl_no_expire(self):
        mc = _set_redis_mock()
        redis_mod.add_to_set("hosts", "h4")
        mc.expire.assert_not_called()

    def test_add_to_set_redis_connection_error(self):
        mc = _set_redis_mock()
        mc.sadd.side_effect = _redis.ConnectionError("err")
        redis_mod.add_to_set("hosts", "h5")
        assert "h5" in redis_mod._mem_sets["hosts"]


class TestRemoveFromSet:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_remove_from_set_inmemory(self):
        _set_inmemory()
        redis_mod._mem_sets["s1"] = {"a", "b"}
        redis_mod.remove_from_set("s1", "a")
        assert "a" not in redis_mod._mem_sets["s1"]

    def test_remove_from_set_inmemory_missing_value(self):
        _set_inmemory()
        redis_mod._mem_sets["s2"] = {"x"}
        redis_mod.remove_from_set("s2", "y")  # should not raise

    def test_remove_from_set_inmemory_missing_set(self):
        _set_inmemory()
        redis_mod.remove_from_set("nonexistent", "val")  # should not raise

    def test_remove_from_set_redis(self):
        mc = _set_redis_mock()
        redis_mod.remove_from_set("s3", "v1")
        mc.srem.assert_called_once_with("s3", "v1")

    def test_remove_from_set_redis_connection_error(self):
        mc = _set_redis_mock()
        mc.srem.side_effect = _redis.ConnectionError("err")
        redis_mod._mem_sets["s4"] = {"v2", "v3"}
        redis_mod.remove_from_set("s4", "v2")
        assert "v2" not in redis_mod._mem_sets["s4"]


class TestIsInSet:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_is_in_set_inmemory_true(self):
        _set_inmemory()
        redis_mod._mem_sets["my_set"] = {"val"}
        assert redis_mod.is_in_set("my_set", "val") is True

    def test_is_in_set_inmemory_false(self):
        _set_inmemory()
        assert redis_mod.is_in_set("my_set", "nope") is False

    def test_is_in_set_inmemory_missing_set(self):
        _set_inmemory()
        assert redis_mod.is_in_set("nonexistent", "x") is False

    def test_is_in_set_redis_true(self):
        mc = _set_redis_mock()
        mc.sismember.return_value = True
        assert redis_mod.is_in_set("s", "v") is True
        mc.sismember.assert_called_once_with("s", "v")

    def test_is_in_set_redis_false(self):
        mc = _set_redis_mock()
        mc.sismember.return_value = False
        assert redis_mod.is_in_set("s", "v") is False

    def test_is_in_set_redis_connection_error(self):
        mc = _set_redis_mock()
        mc.sismember.side_effect = _redis.ConnectionError("err")
        redis_mod._mem_sets["s"] = {"v"}
        assert redis_mod.is_in_set("s", "v") is True


# ═══════════════════════════════════════════════════════════════════
# 4. Rate limiting — increment_counter, get_counter, decrement_counter,
#    sliding_window_rate
# ═══════════════════════════════════════════════════════════════════


class TestIncrementCounter:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_increment_counter_inmemory(self):
        _set_inmemory()
        assert redis_mod.increment_counter("rate:user1") == 1
        assert redis_mod.increment_counter("rate:user1") == 2

    def test_increment_counter_redis(self):
        mc = _set_redis_mock()
        pipe = MagicMock()
        mc.pipeline.return_value = pipe
        pipe.execute.return_value = [5, True]
        result = redis_mod.increment_counter("rate:user2", ttl=120)
        assert result == 5
        pipe.incr.assert_called_once_with("rate:user2")
        pipe.expire.assert_called_once_with("rate:user2", 120)

    def test_increment_counter_redis_connection_error(self):
        mc = _set_redis_mock()
        pipe = MagicMock()
        mc.pipeline.return_value = pipe
        pipe.execute.side_effect = _redis.ConnectionError("err")
        result = redis_mod.increment_counter("rate:user3")
        assert result == 1
        assert redis_mod._mem_counters["rate:user3"] == 1


class TestGetCounter:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_get_counter_inmemory_exists(self):
        _set_inmemory()
        redis_mod._mem_counters["c1"] = 7
        assert redis_mod.get_counter("c1") == 7

    def test_get_counter_inmemory_missing(self):
        _set_inmemory()
        assert redis_mod.get_counter("c_missing") == 0

    def test_get_counter_redis_exists(self):
        mc = _set_redis_mock()
        mc.get.return_value = "42"
        assert redis_mod.get_counter("c2") == 42

    def test_get_counter_redis_missing(self):
        mc = _set_redis_mock()
        mc.get.return_value = None
        assert redis_mod.get_counter("c3") == 0

    def test_get_counter_redis_connection_error(self):
        mc = _set_redis_mock()
        mc.get.side_effect = _redis.ConnectionError("err")
        redis_mod._mem_counters["c4"] = 99
        assert redis_mod.get_counter("c4") == 99


class TestDecrementCounter:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_decrement_counter_inmemory(self):
        _set_inmemory()
        redis_mod._mem_counters["d1"] = 5
        redis_mod.decrement_counter("d1")
        assert redis_mod._mem_counters["d1"] == 4

    def test_decrement_counter_inmemory_floor_zero(self):
        _set_inmemory()
        redis_mod._mem_counters["d2"] = 0
        redis_mod.decrement_counter("d2")
        assert redis_mod._mem_counters["d2"] == 0

    def test_decrement_counter_inmemory_missing(self):
        _set_inmemory()
        redis_mod.decrement_counter("d_missing")
        assert redis_mod._mem_counters["d_missing"] == 0

    def test_decrement_counter_redis(self):
        mc = _set_redis_mock()
        redis_mod.decrement_counter("d3")
        mc.decr.assert_called_once_with("d3")

    def test_decrement_counter_redis_connection_error(self):
        mc = _set_redis_mock()
        mc.decr.side_effect = _redis.ConnectionError("err")
        redis_mod._mem_counters["d4"] = 3
        redis_mod.decrement_counter("d4")
        assert redis_mod._mem_counters["d4"] == 2


class TestSlidingWindowRate:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_sliding_window_rate_inmemory_always_allowed(self):
        _set_inmemory()
        allowed, count = redis_mod.sliding_window_rate("rate:u1")
        assert allowed is True
        assert count == 0

    def test_sliding_window_rate_redis_under_limit(self):
        mc = _set_redis_mock()
        pipe = MagicMock()
        mc.pipeline.return_value = pipe
        pipe.execute.return_value = [0, 1, 5, True]
        allowed, count = redis_mod.sliding_window_rate("rate:u2", window=60, limit=100)
        assert allowed is True
        assert count == 5

    def test_sliding_window_rate_redis_over_limit(self):
        mc = _set_redis_mock()
        pipe = MagicMock()
        mc.pipeline.return_value = pipe
        pipe.execute.return_value = [0, 1, 101, True]
        allowed, count = redis_mod.sliding_window_rate("rate:u3", window=60, limit=100)
        assert allowed is False
        assert count == 101

    def test_sliding_window_rate_redis_connection_error(self):
        mc = _set_redis_mock()
        pipe = MagicMock()
        mc.pipeline.return_value = pipe
        pipe.execute.side_effect = _redis.ConnectionError("err")
        allowed, count = redis_mod.sliding_window_rate("rate:u4")
        assert allowed is True
        assert count == 0


# ═══════════════════════════════════════════════════════════════════
# 5. Distributed lock — _InMemoryLock, RedisLock, get_lock
# ═══════════════════════════════════════════════════════════════════


class TestInMemoryLock:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_acquire_and_release(self):
        lock = redis_mod._InMemoryLock("test-lock-1", timeout=5)
        assert lock.acquire(blocking=True) is True
        lock.release()

    def test_acquire_blocking_timeout_when_held(self):
        lock1 = redis_mod._InMemoryLock("test-lock-2", timeout=5)
        lock1.acquire()
        # Second lock with timeout=0 should fail to acquire
        lock2 = redis_mod._InMemoryLock("test-lock-2", timeout=0)
        assert lock2.acquire(blocking=True) is False
        lock1.release()

    def test_context_manager_success(self):
        with redis_mod._InMemoryLock("test-lock-3", timeout=5) as lk:
            assert lk is not None

    def test_context_manager_timeout_raises(self):
        lock1 = redis_mod._InMemoryLock("test-lock-4", timeout=5)
        lock1.acquire()
        try:
            import pytest

            with pytest.raises(TimeoutError, match="Could not acquire lock"):
                # Use a very short timeout so the test doesn't hang
                lock2 = redis_mod._InMemoryLock("test-lock-4", timeout=0)
                with lock2:
                    pass  # pragma: no cover
        finally:
            lock1.release()

    def test_release_when_not_held(self):
        lock = redis_mod._InMemoryLock("test-lock-5", timeout=5)
        # Releasing when not held should not raise (RuntimeError caught)
        lock.release()

    def test_same_name_shares_underlying_lock(self):
        lock1 = redis_mod._InMemoryLock("shared-lock", timeout=5)
        lock2 = redis_mod._InMemoryLock("shared-lock", timeout=5)
        assert lock1._lock is lock2._lock


class TestRedisLock:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_acquire_immediate_success(self):
        mc = _set_redis_mock()
        mc.set.return_value = True
        lock = redis_mod.RedisLock("mylock", timeout=10)
        assert lock.acquire(blocking=True) is True
        mc.set.assert_called_once_with("lock:mylock", lock._token, nx=True, ex=10)

    def test_acquire_non_blocking_fail(self):
        mc = _set_redis_mock()
        mc.set.return_value = False
        lock = redis_mod.RedisLock("mylock2", timeout=10)
        assert lock.acquire(blocking=False) is False

    def test_acquire_blocking_retry_success(self):
        mc = _set_redis_mock()
        # First call fails, second succeeds
        mc.set.side_effect = [False, True]
        lock = redis_mod.RedisLock("mylock3", timeout=10)
        assert lock.acquire(blocking=True, poll_interval=0.01) is True
        assert mc.set.call_count == 2

    def test_acquire_blocking_timeout(self):
        mc = _set_redis_mock()
        mc.set.return_value = False
        lock = redis_mod.RedisLock("mylock4", timeout=0)
        # timeout=0 means deadline is immediate
        assert lock.acquire(blocking=True, poll_interval=0.01) is False

    def test_release_calls_eval(self):
        mc = _set_redis_mock()
        lock = redis_mod.RedisLock("mylock5", timeout=10)
        lock.release()
        mc.eval.assert_called_once()
        args = mc.eval.call_args
        assert args[0][1] == 1  # numkeys
        assert args[0][2] == "lock:mylock5"
        assert args[0][3] == lock._token

    def test_context_manager_success(self):
        mc = _set_redis_mock()
        mc.set.return_value = True
        lock = redis_mod.RedisLock("ctx-lock", timeout=10)
        with lock:
            pass
        mc.eval.assert_called_once()

    def test_context_manager_timeout_raises(self):
        mc = _set_redis_mock()
        mc.set.return_value = False
        import pytest

        with pytest.raises(TimeoutError, match="Could not acquire lock"):
            lock = redis_mod.RedisLock("ctx-lock-fail", timeout=0)
            with lock:
                pass  # pragma: no cover


class TestGetLock:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_get_lock_returns_redis_lock_when_available(self):
        _set_redis_mock()
        lock = redis_mod.get_lock("test", timeout=60)
        assert isinstance(lock, redis_mod.RedisLock)

    def test_get_lock_returns_inmemory_lock_when_unavailable(self):
        _set_inmemory()
        lock = redis_mod.get_lock("test", timeout=60)
        assert isinstance(lock, redis_mod._InMemoryLock)


# ═══════════════════════════════════════════════════════════════════
# 6. Semaphore — RedisSemaphore
# ═══════════════════════════════════════════════════════════════════


class TestRedisSemaphore:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_acquire_fallback_when_redis_unavailable(self):
        _set_inmemory()
        sem = redis_mod.RedisSemaphore("test-sem", limit=2)
        assert sem.acquire(timeout=1) is True

    def test_release_fallback_when_redis_unavailable(self):
        _set_inmemory()
        sem = redis_mod.RedisSemaphore("test-sem-rel", limit=2)
        sem.acquire(timeout=1)
        sem.release()  # should not raise

    def test_release_fallback_without_acquire(self):
        _set_inmemory()
        sem = redis_mod.RedisSemaphore("test-sem-noac", limit=2)
        # Release without acquire triggers ValueError in Semaphore, caught
        sem.release()  # should not raise

    def test_acquire_redis_success(self):
        mc = _set_redis_mock()
        pipe = MagicMock()
        mc.pipeline.return_value = pipe
        pipe.execute.return_value = [0, 0]  # zremrangebyscore, zcard=0 < limit
        mc.zadd.return_value = 1  # added successfully
        mc.zcard.return_value = 1  # still under limit
        sem = redis_mod.RedisSemaphore("sem-redis", limit=5)
        assert sem.acquire(timeout=1) is True
        assert sem._token is not None

    def test_acquire_redis_at_limit_then_over(self):
        mc = _set_redis_mock()
        pipe = MagicMock()
        mc.pipeline.return_value = pipe
        # Count is at limit
        pipe.execute.return_value = [0, 5]
        sem = redis_mod.RedisSemaphore("sem-full", limit=5, ttl=10)
        assert sem.acquire(timeout=0.1) is False
        assert sem._token is None

    def test_acquire_redis_zadd_race_removes_token(self):
        mc = _set_redis_mock()
        pipe = MagicMock()
        mc.pipeline.return_value = pipe
        pipe.execute.return_value = [0, 0]  # under limit
        mc.zadd.return_value = 1  # added
        mc.zcard.return_value = 6  # but now over limit (race)
        sem = redis_mod.RedisSemaphore("sem-race", limit=5, ttl=10)
        # Will loop, remove token, then timeout
        assert sem.acquire(timeout=0.2) is False
        mc.zrem.assert_called()  # removed our token after race

    def test_acquire_redis_connection_error_falls_back(self):
        mc = _set_redis_mock()
        pipe = MagicMock()
        mc.pipeline.return_value = pipe
        pipe.execute.side_effect = _redis.ConnectionError("gone")
        sem = redis_mod.RedisSemaphore("sem-cerr", limit=5)
        assert sem.acquire(timeout=1) is True

    def test_release_redis(self):
        mc = _set_redis_mock()
        redis_mod._redis_available = True
        sem = redis_mod.RedisSemaphore("sem-rel", limit=5)
        sem._token = "my-token"
        sem.release()
        mc.zrem.assert_called_once_with("semaphore:sem-rel", "my-token")
        assert sem._token is None

    def test_release_redis_connection_error_falls_back(self):
        mc = _set_redis_mock()
        mc.zrem.side_effect = _redis.ConnectionError("err")
        sem = redis_mod.RedisSemaphore("sem-rel-err", limit=5)
        sem._token = "my-token"
        sem.release()  # should not raise, falls back to _fallback.release()

    def test_count_redis(self):
        mc = _set_redis_mock()
        mc.zcard.return_value = 3
        sem = redis_mod.RedisSemaphore("sem-count", limit=5, ttl=100)
        assert sem.count() == 3

    def test_count_redis_unavailable(self):
        _set_inmemory()
        sem = redis_mod.RedisSemaphore("sem-count-no", limit=5)
        assert sem.count() == 0

    def test_count_redis_connection_error(self):
        mc = _set_redis_mock()
        mc.zremrangebyscore.side_effect = _redis.ConnectionError("err")
        sem = redis_mod.RedisSemaphore("sem-count-err", limit=5, ttl=100)
        assert sem.count() == 0

    def test_context_manager_success(self):
        _set_inmemory()
        sem = redis_mod.RedisSemaphore("sem-ctx", limit=2)
        with sem:
            pass  # acquire and release via context manager

    def test_context_manager_timeout_raises(self):
        _set_inmemory()
        import pytest

        sem = redis_mod.RedisSemaphore("sem-ctx-fail", limit=2)
        # Mock the fallback semaphore to return False immediately
        sem._fallback = MagicMock()
        sem._fallback.acquire.return_value = False
        with pytest.raises(TimeoutError, match="Could not acquire semaphore"):
            with sem:
                pass  # pragma: no cover


# ═══════════════════════════════════════════════════════════════════
# 7. Pub/Sub — publish, subscribe_channel
# ═══════════════════════════════════════════════════════════════════


class TestPublish:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_publish_redis(self):
        mc = _set_redis_mock()
        redis_mod.publish("project:abc", {"type": "state", "state": "active"})
        mc.publish.assert_called_once_with(
            "project:abc", json.dumps({"type": "state", "state": "active"})
        )

    def test_publish_redis_connection_error(self):
        mc = _set_redis_mock()
        mc.publish.side_effect = _redis.ConnectionError("err")
        # Should not raise
        redis_mod.publish("project:abc", {"type": "state"})

    def test_publish_redis_unavailable_noop(self):
        _set_inmemory()
        # Should not raise, no-op
        redis_mod.publish("project:abc", {"msg": "ignored"})


class TestSubscribeChannel:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_subscribe_channel_registers_callback(self):
        _set_inmemory()
        cb = MagicMock()
        redis_mod.subscribe_channel("project:xyz", cb)
        with redis_mod._pubsub_lock:
            assert cb in redis_mod._pubsub_callbacks.get("project:xyz", [])

    def test_subscribe_channel_multiple_callbacks(self):
        _set_inmemory()
        cb1 = MagicMock()
        cb2 = MagicMock()
        redis_mod.subscribe_channel("ch1", cb1)
        redis_mod.subscribe_channel("ch1", cb2)
        with redis_mod._pubsub_lock:
            assert cb1 in redis_mod._pubsub_callbacks["ch1"]
            assert cb2 in redis_mod._pubsub_callbacks["ch1"]

    @patch("app.core.redis._ensure_pubsub_listener")
    def test_subscribe_channel_starts_listener_when_redis_available(self, mock_ensure):
        _set_redis_mock()
        cb = MagicMock()
        redis_mod.subscribe_channel("ch2", cb)
        mock_ensure.assert_called_once()

    def test_subscribe_channel_no_listener_when_redis_unavailable(self):
        _set_inmemory()
        with patch("app.core.redis._ensure_pubsub_listener") as mock_ensure:
            redis_mod.subscribe_channel("ch3", MagicMock())
            mock_ensure.assert_not_called()


# ═══════════════════════════════════════════════════════════════════
# 8. enqueue_job
# ═══════════════════════════════════════════════════════════════════


class TestEnqueueJob:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_enqueue_job_fallback_creates_thread(self):
        _set_inmemory()
        sentinel = threading.Event()

        def target_func():
            sentinel.set()

        result = redis_mod.enqueue_job(target_func)
        assert isinstance(result, threading.Thread)
        sentinel.wait(timeout=2)
        assert sentinel.is_set()

    def test_enqueue_job_fallback_passes_args(self):
        _set_inmemory()
        results = []

        def target_func(a, b, key=None):
            results.append((a, b, key))

        t = redis_mod.enqueue_job(target_func, 1, 2, key="val")
        t.join(timeout=2)
        assert results == [(1, 2, "val")]

    @patch("app.core.redis.is_redis_available", return_value=True)
    @patch("app.core.redis.get_redis_raw")
    @patch("app.core.redis.get_redis")
    def test_enqueue_job_redis_path(self, mock_get_redis, mock_get_raw, mock_avail):
        mock_redis_client = MagicMock()
        mock_get_redis.return_value = mock_redis_client
        mock_get_raw.return_value = MagicMock()

        mock_queue_cls = MagicMock()
        mock_job = MagicMock()
        mock_job.id = "job-12345678-abcd"
        mock_queue_cls.return_value.enqueue.return_value = mock_job

        mock_callback_cls = MagicMock()

        def dummy_func():
            pass

        with patch("rq.Queue", mock_queue_cls), patch(
            "rq.job.Callback", mock_callback_cls
        ):
            result = redis_mod.enqueue_job(
                dummy_func,
                queue_name="deploy",
                project_id="proj-1",
                host_id="host-1",
            )

        assert result is mock_job
        # Verify project_id was stored in Redis via get_redis().set()
        mock_redis_client.set.assert_called_once_with(
            "job:project:proj-1", mock_job.id, ex=7200
        )

    @patch("app.core.redis.is_redis_available", return_value=True)
    @patch("app.core.redis.get_redis_raw")
    def test_enqueue_job_redis_exception_falls_back(self, mock_get_raw, mock_avail):
        redis_mod._redis_available = True
        mock_get_raw.side_effect = Exception("RQ broken")
        sentinel = threading.Event()

        def target():
            sentinel.set()

        result = redis_mod.enqueue_job(target)
        assert isinstance(result, threading.Thread)
        sentinel.wait(timeout=2)
        assert sentinel.is_set()

    @patch("app.core.redis.is_redis_available", return_value=True)
    @patch("app.core.redis.get_redis_raw")
    @patch("app.core.redis.get_redis")
    def test_enqueue_job_no_project_id_skips_redis_set(
        self, mock_get_redis, mock_get_raw, mock_avail
    ):
        mock_redis_client = MagicMock()
        mock_get_redis.return_value = mock_redis_client
        mock_get_raw.return_value = MagicMock()

        mock_queue_cls = MagicMock()
        mock_job = MagicMock()
        mock_job.id = "job-abcdef00-1234"
        mock_queue_cls.return_value.enqueue.return_value = mock_job

        def dummy():
            pass

        with patch("rq.Queue", mock_queue_cls), patch("rq.job.Callback", MagicMock()):
            redis_mod.enqueue_job(dummy)

        mock_redis_client.set.assert_not_called()


# ═══════════════════════════════════════════════════════════════════
# 9. get_job_info
# ═══════════════════════════════════════════════════════════════════


class TestGetJobInfo:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    def test_get_job_info_redis_unavailable(self):
        _set_inmemory()
        assert redis_mod.get_job_info("proj-x") is None

    def test_get_job_info_no_job_id_stored(self):
        mc = _set_redis_mock()
        mc.get.return_value = None
        assert redis_mod.get_job_info("proj-y") is None
        mc.get.assert_called_once_with("job:project:proj-y")

    @patch("app.core.redis.get_redis_raw")
    def test_get_job_info_queued_job(self, mock_raw):
        mc = _set_redis_mock()
        mc.get.return_value = "job-id-123"
        raw_client = MagicMock()
        mock_raw.return_value = raw_client

        mock_job = MagicMock()
        mock_job.get_status.return_value = "queued"
        mock_job.origin = "deploy"

        mock_queue = MagicMock()
        mock_queue.get_job_ids.return_value = ["job-id-000", "job-id-123", "job-id-456"]

        with patch("rq.job.Job.fetch", return_value=mock_job), patch(
            "rq.Queue", return_value=mock_queue
        ):
            result = redis_mod.get_job_info("proj-q")

        assert result is not None
        assert result["status"] == "queued"
        assert result["queue_position"] == 2
        assert result["queue_length"] == 3

    @patch("app.core.redis.get_redis_raw")
    def test_get_job_info_started_job_valid_worker(self, mock_raw):
        mc = _set_redis_mock()
        mc.get.return_value = "job-id-started"
        raw_client = MagicMock()
        mock_raw.return_value = raw_client

        mock_job = MagicMock()
        mock_job.get_status.return_value = "started"
        mock_job.worker_name = "worker-1"

        mock_worker = MagicMock()
        mock_worker.name = "worker-1"

        with patch("rq.job.Job.fetch", return_value=mock_job), patch(
            "rq.Worker.all", return_value=[mock_worker]
        ):
            result = redis_mod.get_job_info("proj-s")

        assert result is not None
        assert result["status"] == "started"

    @patch("app.core.redis.get_redis_raw")
    def test_get_job_info_started_job_stale_worker(self, mock_raw):
        mc = _set_redis_mock()
        mc.get.return_value = "job-id-stale"
        raw_client = MagicMock()
        mock_raw.return_value = raw_client

        mock_job = MagicMock()
        mock_job.get_status.return_value = "started"
        mock_job.worker_name = "dead-worker"

        with patch("rq.job.Job.fetch", return_value=mock_job), patch(
            "rq.Worker.all", return_value=[]
        ):
            result = redis_mod.get_job_info("proj-stale")

        assert result is None

    @patch("app.core.redis.get_redis_raw")
    def test_get_job_info_queued_job_not_in_queue(self, mock_raw):
        mc = _set_redis_mock()
        mc.get.return_value = "job-missing"
        raw_client = MagicMock()
        mock_raw.return_value = raw_client

        mock_job = MagicMock()
        mock_job.get_status.return_value = "queued"
        mock_job.origin = "deploy"

        mock_queue = MagicMock()
        mock_queue.get_job_ids.return_value = ["other-job"]

        with patch("rq.job.Job.fetch", return_value=mock_job), patch(
            "rq.Queue", return_value=mock_queue
        ):
            result = redis_mod.get_job_info("proj-mq")

        assert result["queue_position"] == 0

    def test_get_job_info_exception_returns_none(self):
        mc = _set_redis_mock()
        mc.get.side_effect = Exception("boom")
        assert redis_mod.get_job_info("proj-err") is None


# ═══════════════════════════════════════════════════════════════════
# 10. get_redis_raw
# ═══════════════════════════════════════════════════════════════════


class TestGetRedisRaw:
    def setup_method(self):
        self._state = _save_state()

    def teardown_method(self):
        _restore_state(self._state)

    @patch("redis.from_url")
    def test_get_redis_raw_creates_client(self, mock_from_url):
        redis_mod._client_raw = None
        mock_client = MagicMock()
        mock_from_url.return_value = mock_client

        result = redis_mod.get_redis_raw()
        assert result is mock_client
        mock_from_url.assert_called_once()
        # decode_responses should be False
        assert mock_from_url.call_args[1]["decode_responses"] is False

    def test_get_redis_raw_returns_cached(self):
        cached = MagicMock()
        redis_mod._client_raw = cached
        result = redis_mod.get_redis_raw()
        assert result is cached
