"""Redis admission is mandatory; never use the core's in-memory fallback."""

import asyncio
import uuid

import redis
from fastapi import HTTPException

from app.core.redis import _get_redis_url

_ADMIT = """
if redis.call('exists', KEYS[1]) == 1 or redis.call('exists', KEYS[2]) == 1 then return 0 end
local count = tonumber(redis.call('get', KEYS[3]) or '0')
if count >= 20 then return -1 end
redis.call('set', KEYS[1], ARGV[1], 'EX', 130)
redis.call('set', KEYS[2], ARGV[1], 'EX', 130)
redis.call('incr', KEYS[3])
if count == 0 then redis.call('expire', KEYS[3], 3600) end
return 1
"""
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


def client():
    return redis.Redis.from_url(
        _get_redis_url(),
        decode_responses=True,
        socket_timeout=2,
        socket_connect_timeout=2,
    )


def conversation_key(conversation_id):
    return f"assistant:conversation:{conversation_id}"


class RunLease:
    def __init__(self, actor_id, conversation_id, connection=None):
        self.redis = connection or client()
        self.token = str(uuid.uuid4())
        self.keys = [f"assistant:user:{actor_id}", conversation_key(conversation_id)]
        self.rate_key = f"assistant:rate:{actor_id}"

    def acquire(self):
        try:
            result = self.redis.eval(_ADMIT, 3, *self.keys, self.rate_key, self.token)
        except redis.RedisError:
            raise HTTPException(503, "Assistant coordination unavailable") from None
        if result != 1:
            raise HTTPException(
                429 if result == -1 else 409,
                "Assistant rate limit reached"
                if result == -1
                else "An assistant request is already active",
            )

    def check(self):
        try:
            if any(value != self.token for value in self.redis.mget(self.keys)):
                raise HTTPException(409, "Assistant request cancelled or interrupted")
        except redis.RedisError:
            raise HTTPException(503, "Assistant coordination unavailable") from None

    def release(self):
        for key in self.keys:
            try:
                self.redis.eval(_RELEASE, 1, key, self.token)
            except redis.RedisError:
                pass  # Fixed TTL handles an unavailable Redis during finalization.


def cancel(conversation_id):
    try:
        client().delete(conversation_key(conversation_id))
    except redis.RedisError:
        raise HTTPException(503, "Cancellation coordination unavailable") from None


async def guarded(awaitable, check):
    """Cancel pending inference on revoke/expiry/disable/disconnect."""
    task = asyncio.ensure_future(awaitable)
    try:
        while not task.done():
            await asyncio.to_thread(check)
            await asyncio.wait({task}, timeout=0.5)
        await asyncio.to_thread(check)
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
