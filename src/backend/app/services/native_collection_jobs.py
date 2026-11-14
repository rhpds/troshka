"""Identifier-only RQ collection, with fail-closed distributed execution limits.

No lifecycle job pointer or local execution fallback is used here. The periodic
dispatcher retries deferred work; a busy worker never waits for a storage slot.
"""

import hashlib
import logging
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from redis import Redis
from rq import Queue, get_current_job

from app.core.config import config
from app.core.redis import get_redis_raw

logger = logging.getLogger(__name__)
_execution: ContextVar[tuple[Redis, dict[str, str]] | None] = ContextVar(
    "native_collection_execution", default=None
)
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
_PROMOTE = """
local pending = redis.call('get', KEYS[1])
if not pending or string.sub(pending, 1, 8) == 'cleanup-' or redis.call('exists', KEYS[2]) == 1 then
    return 0
end
redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""


def settings():
    return getattr(config, "native_collection", {})


def _timeout():
    return max(30, min(900, int(settings().get("job_timeout_seconds", 240))))


def readiness_timeout():
    return max(15, min(50, int(settings().get("readiness_timeout_seconds", 45))))


def _readiness_key(domain_id):
    return f"native:readiness:{domain_id}"


def enqueue_readiness(domain_id):
    """Readiness has its own queue and reservation, independent of storage backlog."""
    redis = get_redis_raw()
    token = uuid.uuid4().hex
    try:
        redis.ping()
        if not redis.set(
            _readiness_key(domain_id) + ":pending",
            token,
            nx=True,
            ex=2 * readiness_timeout() + 60,
        ):
            return False
        Queue("native_readiness", connection=redis).enqueue(
            run_domain_readiness,
            str(domain_id),
            job_id=token,
            job_timeout=readiness_timeout(),
            ttl=readiness_timeout(),
            result_ttl=0,
            failure_ttl=3600,
        )
        return True
    except Exception:
        logger.warning("Native readiness enqueue unavailable", exc_info=True)
        return False


def _key(domain_id, project_id):
    return f"native:collection:{domain_id}:{project_id or 'domain'}"


def release_lease(redis, key, token):
    return redis.eval(_RELEASE, 1, key, token)


def assert_collection_lease():
    """Runtime calls this before publication or cleanup; CLI has no RQ context."""
    context = _execution.get()
    if context is None:
        return
    redis, owned = context
    for key, token in owned.items():
        value = redis.get(key)
        if isinstance(value, bytes):
            value = value.decode()
        if value != token:
            raise RuntimeError(
                "Native collection execution lease expired or superseded"
            )


def enqueue_collection(domain_id, project_id=None, *, cleanup=False):
    """Atomically reserve one pending identity before enqueueing IDs only."""
    redis = get_redis_raw()
    key = _key(domain_id, project_id) + ":pending"
    token = ("cleanup-" if cleanup else "") + uuid.uuid4().hex
    try:
        redis.ping()
        if not redis.set(key, token, nx=True, ex=2 * _timeout() + 60):
            if not cleanup or not redis.eval(
                _PROMOTE,
                2,
                key,
                _key(domain_id, project_id) + ":executing",
                token,
                2 * _timeout() + 60,
            ):
                return False
        Queue(
            "native_cleanup" if cleanup else "native_collection", connection=redis
        ).enqueue(
            run_project_collection,
            str(domain_id),
            str(project_id) if project_id else None,
            job_id=token,
            job_timeout=_timeout(),
            ttl=_timeout(),
            result_ttl=0,
            failure_ttl=3600,
        )
        return True
    except Exception:
        # An ambiguous enqueue may have succeeded. Keep the reservation until
        # expiry rather than immediately enqueue a second copy.
        logger.warning("Native collection enqueue unavailable", exc_info=True)
        return False


def _identities(domain_id, project_id):
    from app.core.database import SessionLocal
    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeEnrollment,
        NativeProjectRoot,
    )
    from app.services.kubevirt_storage_enrollment import root_key, validated_enrollment

    with SessionLocal() as db:
        domain = db.get(NativeCollectionDomain, domain_id)
        if domain is None:
            raise ValueError("Unknown collection domain")
        enrollment = db.get(NativeEnrollment, domain.enrollment_id)
        pins = validated_enrollment(enrollment).pins
        providers = {str(p.provider_id) for p in pins.provider_clusters}
        if project_id is not None:
            keys = [
                root_key(r)
                for r in pins.project_roots
                if str(r.project_id) == project_id
            ]
            roots = (
                db.query(NativeProjectRoot)
                .filter_by(domain_id=domain_id, project_id=project_id)
                .filter(NativeProjectRoot.key.in_(keys))
                .all()
            )
            if len(roots) != 1:
                raise ValueError(
                    "Collection requires one current enrolled project root"
                )
            providers = {str(roots[0].provider_id)}
        # Lock by FSID, never provider alias; conservatively reserve all domain
        # backends when a root can use more than one storage class.
        backends = {p.ceph_fsid for p in pins.pool_keys}
        if not providers or not backends:
            raise ValueError("Collection enrollment lacks provider/backend identity")
        return sorted(providers), sorted(backends)


def _cleanup_due(project_id):
    from app.core.database import SessionLocal
    from app.services.quota_cleanup import native_intent

    if project_id is None:
        return False
    with SessionLocal() as db:
        intent = native_intent(db, project_id)
        return intent is not None and intent.phase not in {"settled", "finalized"}


def _acquire_slots(redis, keys, token, owned, *, readiness=False):
    capacity = (
        1 if readiness else max(1, min(32, int(settings().get("provider_slots", 2))))
    )
    timeout = readiness_timeout() if readiness else _timeout()
    for key in keys:
        for slot in range(capacity):
            slot_key = f"{key}:{slot}"
            if redis.set(slot_key, token, nx=True, ex=timeout + 60):
                owned[slot_key] = token
                break
        else:
            return False
    return True


def run_project_collection(domain_id, project_id=None):
    return _run_collection_job(domain_id, project_id)


def run_domain_readiness(domain_id):
    return _run_collection_job(domain_id, None, readiness=True)


@contextmanager
def _owned_execution(redis):
    owned = {}
    context_token = _execution.set((redis, owned))
    try:
        yield owned
    finally:
        _execution.reset(context_token)
        for key, value in owned.items():
            try:
                release_lease(redis, key, value)
            except Exception:
                logger.warning(
                    "Collection lease release unavailable; expiry will recover it"
                )


def _queued_priority(redis, job, base, token, owned, domain_id, project_id):
    if job is None:
        return False
    owned[base + ":pending"] = token
    assert_collection_lease()  # Reject queued work whose reservation expired.
    if job.origin != "native_collection" or not _cleanup_due(project_id):
        return False
    release_lease(redis, base + ":pending", token)
    enqueue_collection(domain_id, project_id, cleanup=True)
    return True


def _execution_slots(domain_id, project_id, readiness):
    providers, backends = _identities(domain_id, project_id)
    prefix = "native:readiness" if readiness else "native"
    return [f"{prefix}:provider:{p}" for p in providers] + [
        f"{prefix}:backend:{b}" for b in backends
    ]


def _invoke_runtime(domain_id, project_id, readiness):
    if readiness:
        from app.services.native_collection_runtime import run_readiness

        return run_readiness(domain_id)
    from app.services.native_collection_runtime import run_collection

    return run_collection(domain_id, project_id)


def _run_collection_job(domain_id, project_id, *, readiness=False):
    redis = get_redis_raw()
    redis.ping()  # Never run without Redis, even when dev normally falls back.
    job = get_current_job()
    token = job.id if job else uuid.uuid4().hex
    base = _readiness_key(domain_id) if readiness else _key(domain_id, project_id)
    timeout = readiness_timeout() if readiness else _timeout()
    with _owned_execution(redis) as owned:
        if _queued_priority(redis, job, base, token, owned, domain_id, project_id):
            return "reprioritized"
        if not redis.set(base + ":executing", token, nx=True, ex=timeout + 60):
            return "deferred"
        owned[base + ":executing"] = token
        keys = _execution_slots(domain_id, project_id, readiness)
        if not _acquire_slots(redis, keys, token, owned, readiness=readiness):
            return "deferred"
        assert_collection_lease()
        result = _invoke_runtime(domain_id, project_id, readiness)
        assert_collection_lease()
        return result


def dispatch_candidates(db):
    """Read current enrollment and durable retirement, including deleted projects."""
    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeEnrollment,
        NativeProjectRoot,
    )
    from app.services.kubevirt_storage_enrollment import root_key, validated_enrollment
    from app.services.quota_cleanup import native_intents, settled_root_keys

    profiles = settings().get("profiles", [])
    allowed = {str(p["domain_id"]) for p in profiles}
    retired = settled_root_keys(db)
    cleanup = {
        str(v.root.project_id)
        for v in native_intents(db)
        if v.phase not in {"settled", "finalized"}
    }
    domains = (
        db.query(NativeCollectionDomain)
        .filter(NativeCollectionDomain.id.in_(allowed))
        .all()
    )
    for domain in domains:
        yield str(domain.id), None, False
        manifest = validated_enrollment(db.get(NativeEnrollment, domain.enrollment_id))
        keys = [root_key(r) for r in manifest.pins.project_roots]
        roots = (
            db.query(NativeProjectRoot)
            .filter_by(domain_id=domain.id)
            .filter(NativeProjectRoot.key.in_(keys))
            .all()
        )
        for root in roots:
            if root.key not in retired:
                yield str(domain.id), str(root.project_id), str(
                    root.project_id
                ) in cleanup


def dispatch_once():
    if not settings().get("enabled", False):
        return 0
    from app.core.database import SessionLocal

    redis = get_redis_raw()
    redis.ping()
    count = 0
    with SessionLocal() as db:
        candidates = list(dispatch_candidates(db))
    for domain_id, project_id, cleanup in candidates:
        if project_id is None:
            count += _dispatch_readiness(redis, domain_id)
        count += _dispatch_collection(redis, domain_id, project_id, cleanup)
    return count


def _dispatch_collection(redis, domain_id, project_id, cleanup):
    key = _key(domain_id, project_id) + (":due:cleanup" if cleanup else ":due")
    jitter = int(hashlib.sha256(key.encode()).hexdigest()[:4], 16) % 11
    setting = "domain_interval_seconds" if project_id is None else "interval_seconds"
    interval = 10 if cleanup else max(10, int(settings().get(setting, 60))) + jitter
    token = uuid.uuid4().hex
    if not redis.set(key, token, nx=True, ex=interval):
        return 0
    if enqueue_collection(domain_id, project_id, cleanup=cleanup):
        return 1
    release_lease(redis, key, token)
    return 0


def _dispatch_readiness(redis, domain_id):
    key = _readiness_key(domain_id) + ":due"
    jitter = int(hashlib.sha256(key.encode()).hexdigest()[:4], 16) % 6
    interval = (
        max(10, min(30, int(settings().get("readiness_interval_seconds", 20)))) + jitter
    )
    token = uuid.uuid4().hex
    if not redis.set(key, token, nx=True, ex=interval):
        return 0
    if enqueue_readiness(domain_id):
        return 1
    release_lease(redis, key, token)
    return 0


def start_collection_dispatcher():
    stop = threading.Event()
    if not settings().get("enabled", False):
        return stop

    def loop():
        while not stop.is_set():
            try:
                dispatch_once()
            except Exception:
                logger.warning("Native collection dispatch unavailable", exc_info=True)
            stop.wait(5)

    threading.Thread(
        target=loop, daemon=True, name="native-collection-dispatch"
    ).start()
    return stop
