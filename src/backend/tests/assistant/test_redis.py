import os
import subprocess
import sys
import uuid

import pytest
import redis
from fastapi import HTTPException

from app.services.assistant.runs import RunLease


@pytest.fixture
def leases():
    url = os.environ.get("TROSHKA_ASSISTANT_TEST_REDIS_URL")
    if not url:
        pytest.skip(
            "Set TROSHKA_ASSISTANT_TEST_REDIS_URL to a disposable/local test Redis"
        )
    connection = redis.Redis.from_url(url, decode_responses=True, socket_timeout=2)
    actor, cid = str(uuid.uuid4()), str(uuid.uuid4())
    first, second = RunLease(actor, cid, connection), RunLease(actor, cid, connection)
    yield first, second, url, actor, cid
    connection.delete(*first.keys, first.rate_key)


def test_atomic_admission_cancel_and_rate_limit(leases):
    first, second, _, _, _ = leases
    first.acquire()
    with pytest.raises(HTTPException) as denied:
        second.acquire()
    assert denied.value.status_code == 409
    first.redis.delete(first.keys[1])
    with pytest.raises(HTTPException):
        first.check()
    first.release()
    for _ in range(19):
        second.acquire()
        second.release()
    with pytest.raises(HTTPException) as limited:
        first.acquire()
    assert limited.value.status_code == 429


def test_admission_is_shared_across_processes(leases):
    first, _, url, actor, cid = leases
    script = """import sys, redis
from app.services.assistant.runs import RunLease
from fastapi import HTTPException
lease = RunLease(sys.argv[2], sys.argv[3], redis.Redis.from_url(sys.argv[1], decode_responses=True))
try:
    lease.acquire()
    print("admitted")
except HTTPException as exc:
    print(exc.status_code)
"""
    children = [
        subprocess.Popen(
            [sys.executable, "-c", script, url, actor, cid],
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results = [child.communicate(timeout=10)[0].strip() for child in children]
    assert sorted(results) == ["409", "admitted"]
