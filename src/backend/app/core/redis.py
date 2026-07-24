"""
Redis client singleton — shared state, distributed locks, pub/sub, and job queue.

When Redis is unavailable (dev, tests), all operations fall back to in-memory
equivalents. This gives single-process correctness without requiring Redis.

Thread-safe: redis-py uses connection pooling internally, so one client
instance is safe for both async FastAPI endpoints and sync background threads.
"""

import json
import logging
import threading
import time
import uuid

import redis as _redis

from app.core.config import config

logger = logging.getLogger(__name__)

_client: _redis.Redis | None = None
_client_raw: _redis.Redis | None = None
_redis_available = False
_pubsub_thread: threading.Thread | None = None

# In-memory fallback stores (used when Redis is unavailable)
_mem_progress: dict[str, dict] = {}
_mem_cancelled: set[str] = set()
_mem_sets: dict[str, set[str]] = {}
_mem_counters: dict[str, int] = {}
_mem_locks: dict[str, threading.Lock] = {}
_mem_locks_guard = threading.Lock()
_mem_lock = threading.Lock()

# Queue names
QUEUE_DEFAULT = "default"
QUEUE_DEPLOY = "deploy"
QUEUE_PROVISION = "provision"


def _get_redis_url() -> str:
    url = getattr(config, "redis", {})
    if isinstance(url, dict):
        return url.get("url", "redis://localhost:6379/0")
    return getattr(url, "url", "redis://localhost:6379/0")


def get_redis() -> _redis.Redis:
    global _client, _redis_available
    if _client is None:
        url = _get_redis_url()
        _client = _redis.from_url(url, decode_responses=True)
        try:
            _client.ping()
            _redis_available = True
            logger.info("Redis connected: %s", url)
        except _redis.ConnectionError:
            _redis_available = False
            logger.warning("Redis not available at %s — using in-memory fallback", url)
    return _client


def get_redis_raw() -> _redis.Redis:
    """Get a Redis client WITHOUT decode_responses — needed for RQ (binary pickle data)."""
    global _client_raw
    if _client_raw is None:
        url = _get_redis_url()
        _client_raw = _redis.from_url(url, decode_responses=False)
    return _client_raw


def is_redis_available() -> bool:
    if _client is None:
        try:
            get_redis()
        except Exception:
            pass
    return _redis_available


def close_redis():
    global _client, _redis_available
    if _client:
        try:
            _client.close()
        except Exception:
            pass
        _client = None
        _redis_available = False
        logger.info("Redis client closed")


def _release_stale_lock(key: str):
    """Force-delete a lock key. Used by failure callbacks to clean up after crashes."""
    if _redis_available:
        try:
            get_redis().delete(key)
        except Exception:
            pass


# ── Job enqueue helpers ──


def _on_job_success(job, connection, result, *args, **kwargs):
    """Called by RQ when a job completes successfully."""
    project_id = job.meta.get("project_id")
    host_id = job.meta.get("host_id")
    if host_id:
        try:
            from app.services.placement import record_deploy_end

            record_deploy_end(host_id)
        except Exception:
            pass
    if project_id:
        try:
            connection.delete(f"job:project:{project_id}")
        except Exception:
            pass


def _on_job_failure(job, connection, exc_type, exc_value, traceback):
    """Called by RQ when a job fails (exception, timeout, or worker crash)."""
    project_id = job.meta.get("project_id")
    host_id = job.meta.get("host_id")
    logger.error(
        "Job %s failed: %s: %s",
        job.id[:8],
        exc_type.__name__ if exc_type else "unknown",
        exc_value,
    )
    if host_id:
        try:
            from app.services.placement import record_deploy_end

            record_deploy_end(host_id)
        except Exception:
            pass
        try:
            _release_stale_lock(f"lock:network:{host_id}")
        except Exception:
            pass
    if project_id:
        try:
            delete_progress(f"deploy:{project_id}")
            connection.delete(f"job:project:{project_id}")
        except Exception:
            pass
        # Try to find host_id from the project for lock cleanup
        if not host_id:
            try:
                from app.core.database import SessionLocal
                from app.models.project import Project as _P

                _s = SessionLocal()
                _proj = _s.get(_P, project_id)
                if _proj and _proj.host_id:
                    _release_stale_lock(f"lock:network:{_proj.host_id}")
                _s.close()
            except Exception:
                pass
        try:
            from app.core.database import SessionLocal
            from app.models.project import Project

            s = SessionLocal()
            p = s.get(Project, project_id)
            if p and p.state in ("deploying", "starting", "stopping", "deleting"):
                p.state = "error"
                p.deploy_error = f"Worker job failed: {exc_value}"
                s.commit()
            s.close()
        except Exception:
            pass


def enqueue_job(
    func,
    *args,
    queue_name: str = QUEUE_DEPLOY,
    job_timeout: int = 7200,
    project_id: str | None = None,
    **kwargs,
):
    """Enqueue a function for execution by an RQ worker.

    Falls back to running in a daemon thread when Redis is unavailable.
    Pass project_id to track queue position for the project.
    """
    if is_redis_available():
        try:
            from rq import Queue
            from rq.job import Callback

            r = get_redis_raw()
            q = Queue(queue_name, connection=r, default_timeout=job_timeout)
            meta = {}
            if project_id:
                meta["project_id"] = project_id
            job = q.enqueue(
                func,
                *args,
                job_timeout=job_timeout,
                on_success=Callback(_on_job_success),
                on_failure=Callback(_on_job_failure),
                meta=meta,
                **kwargs,
            )
            logger.info(
                "Enqueued job %s: %s.%s (queue=%s)",
                job.id[:8],
                func.__module__,
                func.__qualname__,
                queue_name,
            )
            if project_id:
                get_redis().set(f"job:project:{project_id}", job.id, ex=7200)
            return job
        except Exception:
            logger.warning("RQ enqueue failed, falling back to thread")

    # Fallback: run in a daemon thread (single-process mode)
    t = threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True)
    t.start()
    return t


def get_job_info(project_id: str) -> dict | None:
    """Get job status and queue position for a project's active job."""
    if not _redis_available:
        return None
    try:
        raw_job_id = get_redis().get(f"job:project:{project_id}")
        if not raw_job_id:
            return None
        job_id = str(raw_job_id)

        from rq import Queue
        from rq.job import Job

        r_raw = get_redis_raw()
        job = Job.fetch(job_id, connection=r_raw)
        status = job.get_status()

        result: dict = {"job_id": job_id, "status": status}

        if status == "queued":
            q = Queue(job.origin, connection=r_raw)
            job_ids = q.get_job_ids()
            try:
                position = job_ids.index(job_id) + 1
            except ValueError:
                position = 0
            result["queue_position"] = position
            result["queue_length"] = len(job_ids)

        return result
    except Exception:
        return None


# ── State helpers (replace in-memory dicts) ──


def set_progress(key: str, data: dict, ttl: int = 7200):
    if _redis_available:
        try:
            get_redis().set(f"progress:{key}", json.dumps(data), ex=ttl)
            return
        except _redis.ConnectionError:
            pass
    with _mem_lock:
        _mem_progress[key] = data


def get_progress(key: str) -> dict | None:
    if _redis_available:
        try:
            raw = get_redis().get(f"progress:{key}")
            if raw:
                return json.loads(raw)
            return None
        except _redis.ConnectionError:
            pass
    with _mem_lock:
        return _mem_progress.get(key)


def delete_progress(key: str):
    if _redis_available:
        try:
            get_redis().delete(f"progress:{key}")
        except _redis.ConnectionError:
            pass
    with _mem_lock:
        _mem_progress.pop(key, None)


def mark_cancelled(key: str, ttl: int = 3600):
    if _redis_available:
        try:
            get_redis().set(f"cancelled:{key}", "1", ex=ttl)
            return
        except _redis.ConnectionError:
            pass
    with _mem_lock:
        _mem_cancelled.add(key)


def is_cancelled(key: str) -> bool:
    if _redis_available:
        try:
            return get_redis().get(f"cancelled:{key}") is not None
        except _redis.ConnectionError:
            pass
    with _mem_lock:
        return key in _mem_cancelled


def clear_cancelled(key: str):
    if _redis_available:
        try:
            get_redis().delete(f"cancelled:{key}")
        except _redis.ConnectionError:
            pass
    with _mem_lock:
        _mem_cancelled.discard(key)


def add_to_set(set_name: str, value: str, ttl: int | None = None):
    if _redis_available:
        try:
            r = get_redis()
            r.sadd(set_name, value)
            if ttl:
                r.expire(set_name, ttl)
            return
        except _redis.ConnectionError:
            pass
    with _mem_lock:
        if set_name not in _mem_sets:
            _mem_sets[set_name] = set()
        _mem_sets[set_name].add(value)


def remove_from_set(set_name: str, value: str):
    if _redis_available:
        try:
            get_redis().srem(set_name, value)
            return
        except _redis.ConnectionError:
            pass
    with _mem_lock:
        s = _mem_sets.get(set_name)
        if s:
            s.discard(value)


def is_in_set(set_name: str, value: str) -> bool:
    if _redis_available:
        try:
            return bool(get_redis().sismember(set_name, value))
        except _redis.ConnectionError:
            pass
    with _mem_lock:
        return value in _mem_sets.get(set_name, set())


# ── Distributed lock (falls back to threading.Lock) ──


class _InMemoryLock:
    def __init__(self, name: str, timeout: int = 300):
        self.name = name
        self.timeout = timeout
        with _mem_locks_guard:
            if name not in _mem_locks:
                _mem_locks[name] = threading.Lock()
            self._lock = _mem_locks[name]

    def acquire(self, blocking: bool = True, poll_interval: float = 0.2) -> bool:
        return self._lock.acquire(blocking=blocking, timeout=self.timeout)

    def release(self):
        try:
            self._lock.release()
        except RuntimeError:
            pass

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError(f"Could not acquire lock {self.name}")
        return self

    def __exit__(self, *exc):
        self.release()


class RedisLock:
    def __init__(self, name: str, timeout: int = 300):
        self.name = f"lock:{name}"
        self.timeout = timeout
        self._token = str(uuid.uuid4())

    def acquire(self, blocking: bool = True, poll_interval: float = 0.2) -> bool:
        r = get_redis()
        if r.set(self.name, self._token, nx=True, ex=self.timeout):
            return True
        if not blocking:
            return False
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            if r.set(self.name, self._token, nx=True, ex=self.timeout):
                return True
        return False

    def release(self):
        r = get_redis()
        lua = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        r.eval(lua, 1, self.name, self._token)

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError(f"Could not acquire lock {self.name}")
        return self

    def __exit__(self, *exc):
        self.release()


def get_lock(name: str, timeout: int = 300):
    if _redis_available:
        return RedisLock(name, timeout)
    return _InMemoryLock(name, timeout)


# ── Pub/Sub bridge for WebSocket notifications ──


_pubsub_callbacks: dict[str, list] = {}
_pubsub_lock = threading.Lock()


def publish(channel: str, message: dict):
    if _redis_available:
        try:
            get_redis().publish(channel, json.dumps(message))
            return
        except _redis.ConnectionError:
            pass
    # No fallback needed — local delivery handled by ws_pubsub directly


def subscribe_channel(channel: str, callback):
    with _pubsub_lock:
        if channel not in _pubsub_callbacks:
            _pubsub_callbacks[channel] = []
        _pubsub_callbacks[channel].append(callback)
    if _redis_available:
        _ensure_pubsub_listener()


def _ensure_pubsub_listener():
    global _pubsub_thread
    if _pubsub_thread and _pubsub_thread.is_alive():
        return
    _pubsub_thread = threading.Thread(
        target=_pubsub_listen_loop, daemon=True, name="redis-pubsub"
    )
    _pubsub_thread.start()


def _pubsub_listen_loop():
    try:
        r = get_redis()
        ps = r.pubsub()
        ps.psubscribe("project:*", "pattern:*")
        logger.info("Redis pub/sub listener started")

        for msg in ps.listen():
            if msg["type"] not in ("pmessage",):
                continue
            channel = msg["channel"]
            try:
                data = json.loads(msg["data"])
            except (json.JSONDecodeError, TypeError):
                continue
            with _pubsub_lock:
                cbs = list(_pubsub_callbacks.get(channel, []))
                for pattern, callbacks in _pubsub_callbacks.items():
                    if pattern.endswith("*") and channel.startswith(pattern[:-1]):
                        cbs.extend(callbacks)

            for cb in cbs:
                try:
                    cb(channel, data)
                except Exception:
                    logger.exception("Pub/sub callback error on %s", channel)
    except Exception:
        logger.exception("Redis pub/sub listener crashed")


# ── Rate limiting helpers ──


def increment_counter(key: str, ttl: int = 60) -> int:
    if _redis_available:
        try:
            r = get_redis()
            pipe = r.pipeline()
            pipe.incr(key)
            pipe.expire(key, ttl)
            result = pipe.execute()
            return result[0]
        except _redis.ConnectionError:
            pass
    with _mem_lock:
        _mem_counters[key] = _mem_counters.get(key, 0) + 1
        return _mem_counters[key]


def get_counter(key: str) -> int:
    if _redis_available:
        try:
            val = get_redis().get(key)
            return int(val) if val else 0
        except _redis.ConnectionError:
            pass
    with _mem_lock:
        return _mem_counters.get(key, 0)


def decrement_counter(key: str):
    if _redis_available:
        try:
            get_redis().decr(key)
            return
        except _redis.ConnectionError:
            pass
    with _mem_lock:
        _mem_counters[key] = max(0, _mem_counters.get(key, 0) - 1)


def sliding_window_rate(
    key: str, window: int = 60, limit: int = 100
) -> tuple[bool, int]:
    if _redis_available:
        try:
            r = get_redis()
            now = time.time()
            pipe = r.pipeline()
            pipe.zremrangebyscore(key, 0, now - window)
            pipe.zadd(key, {f"{now}:{uuid.uuid4().hex[:8]}": now})
            pipe.zcard(key)
            pipe.expire(key, window + 1)
            results = pipe.execute()
            count = results[2]
            if count > limit:
                return False, count
            return True, count
        except _redis.ConnectionError:
            pass
    return True, 0


# ── Semaphore (falls back to threading.Semaphore) ──


class RedisSemaphore:
    def __init__(self, name: str, limit: int = 100, ttl: int = 7200):
        self.name = f"semaphore:{name}"
        self.limit = limit
        self.ttl = ttl
        self._token: str | None = None
        self._fallback = threading.Semaphore(limit)

    def acquire(self, timeout: float = 1800) -> bool:
        if not _redis_available:
            return self._fallback.acquire(timeout=timeout)

        r = get_redis()
        self._token = str(uuid.uuid4())
        now = time.time()
        deadline = now + timeout

        while time.time() < deadline:
            try:
                pipe = r.pipeline()
                pipe.zremrangebyscore(self.name, 0, now - self.ttl)
                pipe.zcard(self.name)
                results = pipe.execute()
                count = results[1]

                if count < self.limit:
                    added = r.zadd(self.name, {self._token: time.time()}, nx=True)
                    if added:
                        if r.zcard(self.name) <= self.limit:
                            return True
                        r.zrem(self.name, self._token)
            except _redis.ConnectionError:
                return self._fallback.acquire(timeout=timeout)

            time.sleep(0.5)

        self._token = None
        return False

    def release(self):
        if self._token and _redis_available:
            try:
                get_redis().zrem(self.name, self._token)
            except _redis.ConnectionError:
                self._fallback.release()
            self._token = None
        else:
            try:
                self._fallback.release()
            except ValueError:
                pass

    def count(self) -> int:
        if not _redis_available:
            return 0
        try:
            r = get_redis()
            r.zremrangebyscore(self.name, 0, time.time() - self.ttl)
            return r.zcard(self.name)
        except _redis.ConnectionError:
            return 0

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError(f"Could not acquire semaphore {self.name}")
        return self

    def __exit__(self, *exc):
        self.release()
