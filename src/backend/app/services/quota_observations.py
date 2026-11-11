"""Bounded Redis transport for trusted provider observations, never a quota ledger.

Only the API provider poller publishes. Consumers retain the original timestamp
and content identity; cache receipt/TTL never establishes freshness. Dedicated
connect/read timeouts are 250ms each with zero retry. All quota decisions still
apply their own observation-age and worker-generation fences.
"""

import json
import logging
from datetime import UTC, datetime
from hashlib import sha256

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.core.redis import _get_redis_url

logger = logging.getLogger(__name__)
_client = None
_TTL = 120
_PUBLISH = """
local previous = redis.call('GET', KEYS[1])
local incoming = cjson.decode(ARGV[1])
if previous and cjson.decode(previous).observed_us >= incoming.observed_us then
    return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""


def _redis():
    global _client
    if _client is None:
        _client = redis.from_url(
            _get_redis_url(),
            decode_responses=True,
            socket_connect_timeout=0.25,
            socket_timeout=0.25,
            retry=Retry(NoBackoff(), 0),
            retry_on_timeout=False,
        )
    return _client


def _key(project_id):
    return f"quota:observation:v1:{project_id}"


def _utc(timestamp):
    return (
        timestamp.astimezone(UTC) if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)
    )


def _identity(body):
    source = {
        key: body[key]
        for key in ("project_id", "states", "container_states", "observed_at")
    }
    return sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def publish(project_id, snapshot):
    """Publish only a new source observation; older replicas/replays cannot refresh it."""
    timestamp = snapshot.get("observed_at")
    if not isinstance(timestamp, datetime) or not snapshot.get("states"):
        return None
    timestamp = _utc(timestamp)
    delta = timestamp - datetime(1970, 1, 1, tzinfo=UTC)
    body = {
        **snapshot,
        "project_id": project_id,
        "version": 1,
        "observed_at": timestamp.isoformat(timespec="microseconds"),
        "observed_us": (delta.days * 86400 + delta.seconds) * 1000000
        + delta.microseconds,
    }
    body["observation_id"] = _identity(body)
    try:
        _redis().eval(_PUBLISH, 1, _key(project_id), json.dumps(body), _TTL)
    except (redis.RedisError, TypeError, ValueError):
        logger.debug("Shared quota observation publish unavailable")
    return body["observation_id"]


def read(project_id):
    """Missing, unavailable or malformed transport never invents an observation."""
    try:
        raw = _redis().get(_key(project_id))
        if raw is None:
            return None
        body = json.loads(raw)
        return _decode(project_id, body)
    except (redis.RedisError, TypeError, ValueError, KeyError):
        logger.debug("Shared quota observation read unavailable")
        return None


def _decode(project_id, body):
    if not isinstance(body, dict):
        return None
    if body.get("version") != 1 or body.get("project_id") != project_id:
        return None
    if not isinstance(body.get("states"), dict) or not body["states"]:
        return None
    if body.get("observation_id") != _identity(body):
        return None
    timestamp = datetime.fromisoformat(body["observed_at"])
    return {
        "states": body["states"],
        "container_states": body["container_states"],
        "progress": body.get("progress", {}),
        "observed_at": timestamp,
        "observation_id": body["observation_id"],
    }
