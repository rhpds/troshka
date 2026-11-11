"""Source observations survive API/RQ local-cache isolation without retiming."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from redis.exceptions import ConnectionError

from app.services import quota_accounting as accounting
from app.services import quota_observations as observations
from app.services import ws_pubsub

NOW = datetime(2026, 11, 30, 23, 59, tzinfo=UTC)


class SharedRedis:
    def __init__(self):
        self.data = {}
        self.writes = 0

    def get(self, key):
        return self.data.get(key)

    def eval(self, script, count, key, value, ttl):
        previous = self.data.get(key)
        if (
            previous
            and json.loads(previous)["observed_us"] >= json.loads(value)["observed_us"]
        ):
            return 0
        self.data[key] = value
        self.writes += 1
        assert ttl == 120
        return 1


@pytest.fixture
def transport(monkeypatch):
    shared = SharedRedis()
    monkeypatch.setattr(observations, "_client", shared)
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    monkeypatch.setattr(ws_pubsub, "_last_states", {})
    monkeypatch.setattr(ws_pubsub, "notify_project", Mock())
    monkeypatch.setattr(ws_pubsub, "_log_vm_state_changes", Mock())
    return shared


def publish(project_id="project", timestamp=NOW, state="off"):
    project = SimpleNamespace(
        id=project_id, name="shared", state="stopped", deploy_error=None
    )
    ws_pubsub._check_and_notify_project_changes(
        project_id,
        project,
        None,
        {"vm": state},
        {},
        {},
        observed_at=timestamp,
        quota_states={"vm": state},
    )


def test_separate_producer_consumer_caches_preserve_original_identity_and_timestamp(
    transport, monkeypatch
):
    publish()
    produced = ws_pubsub.get_cached_vm_states("project")
    monkeypatch.setattr(ws_pubsub, "_last_states", {})
    consumed = ws_pubsub.get_cached_vm_states("project")
    assert consumed["states"] == {"vm": "off"}
    assert consumed["observed_at"] == NOW
    assert consumed["observation_id"] == produced["observation_id"]
    assert ws_pubsub.get_cached_vm_states("project") == consumed
    assert (
        ws_pubsub._last_states == {}
    )  # Reading is not a new local provider observation.


def test_old_replica_cannot_replace_or_refresh_new_source_observation(
    transport, monkeypatch
):
    publish()
    publish(timestamp=NOW - timedelta(minutes=1), state="running")
    assert transport.writes == 1
    assert ws_pubsub.get_cached_vm_states("project")["states"] == {"vm": "off"}
    monkeypatch.setattr(ws_pubsub, "_last_states", {})
    assert ws_pubsub.get_cached_vm_states("project")["observed_at"] == NOW


def test_missing_redis_fails_closed_in_worker_but_preserves_same_process_fallback(
    transport, monkeypatch
):
    publish()
    monkeypatch.setattr(
        observations, "_client", Mock(get=Mock(side_effect=ConnectionError("offline")))
    )
    assert ws_pubsub.get_cached_vm_states("project")["observed_at"] == NOW
    monkeypatch.setattr(ws_pubsub, "_last_states", {})
    assert ws_pubsub.get_cached_vm_states("project") is None


def test_observation_client_has_short_no_retry_timeouts(monkeypatch):
    monkeypatch.setattr(observations, "_client", None)
    factory = Mock()
    monkeypatch.setattr(observations.redis, "from_url", factory)
    observations._redis()
    assert factory.call_args.kwargs["socket_connect_timeout"] <= 0.5
    assert factory.call_args.kwargs["socket_timeout"] <= 0.5
    assert factory.call_args.kwargs["retry"].get_retries() == 0


@pytest.mark.parametrize("body", [[], {}, {"version": 1, "project_id": "other"}])
def test_malformed_observation_never_becomes_provider_evidence(transport, body):
    transport.data[observations._key("project")] = json.dumps(body)
    assert ws_pubsub.get_cached_vm_states("project") is None


def test_display_fallback_cannot_change_trusted_source_states_or_identity(
    transport, monkeypatch
):
    publish(state="running")
    original = ws_pubsub.get_cached_vm_states("project")
    publish(timestamp=None, state="off")  # UI fallback after a failed provider poll.
    monkeypatch.setattr(
        observations, "_client", Mock(get=Mock(side_effect=ConnectionError("offline")))
    )
    retained = ws_pubsub.get_cached_vm_states("project")
    assert retained["states"] == {"vm": "running"}
    assert retained["observation_id"] == original["observation_id"]
    assert retained["observed_at"] == NOW


def test_display_snapshot_normalizes_shared_raw_states_without_mutating_evidence(
    transport, monkeypatch
):
    publish(state="suspended")
    monkeypatch.setattr(ws_pubsub, "_last_states", {})
    original = ws_pubsub.get_cached_vm_states("project")
    assert ws_pubsub.get_display_vm_states("project")["states"] == {"vm": "stopped"}
    assert ws_pubsub.get_cached_vm_states("project") == original
    assert original["states"] == {"vm": "suspended"}
