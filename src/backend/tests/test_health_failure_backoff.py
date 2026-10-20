"""Health failure boundaries and cloud-state precedence."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import health_poller as hp


@pytest.mark.parametrize(
    "status,kind,age,expected_status,expected_state,delay",
    [
        ("connected", "libvirt", 180, "connected", "active", 15),
        ("connected", "libvirt", 181, "disconnected", "active", 30),
        ("connected", "libvirt", None, "connected", "active", 60),
        ("disconnected", "pattern_buffer", 600, "disconnected", "active", 30),
        ("disconnected", "pattern_buffer", 601, "disconnected", "stopped", 86400),
        ("disconnected", "pattern_buffer", None, "disconnected", "active", 30),
        ("disconnected", "libvirt", 601, "disconnected", "active", 30),
        ("installing", "libvirt", None, "installing", "active", 60),
    ],
)
def test_failure_backoff_boundaries(
    status, kind, age, expected_status, expected_state, delay
):
    now = datetime.now(UTC)
    host = SimpleNamespace(
        id="host-backoff",
        state="active",
        agent_status=status,
        host_type=kind,
        last_health_at=now - timedelta(seconds=age) if age is not None else None,
        storage_warnings=[],
    )
    with (
        patch.dict(hp._skip_until, {}, clear=True),
        patch.object(hp, "_DISCONNECT_AFTER_SECONDS", 180),
        patch("app.services.health_poller.time.time", return_value=1000),
        patch("app.services.provisioner.get_public_ip", side_effect=OSError),
    ):
        hp._handle_health_failure(host, now)
        assert hp._skip_until[host.id] == 1000 + delay
    assert host.agent_status == expected_status
    assert host.state == expected_state


@pytest.mark.parametrize("cloud_updated", [True, False])
def test_cloud_sync_and_pause_take_precedence_over_agent_timeout(cloud_updated):
    host = SimpleNamespace(id="paused-host", state="paused")
    db = MagicMock()
    with (
        patch.dict(hp._skip_until, {}, clear=True),
        patch.object(hp, "_sync_cloud_powerstate", return_value=cloud_updated) as sync,
        patch.object(hp, "_warn_agent_unreachable") as warn,
        patch("app.services.health_poller.time.time", return_value=1000),
    ):
        hp._handle_health_failure(host, datetime.now(UTC), db)
        sync.assert_called_once_with(host, db)
        warn.assert_not_called()
        assert hp._skip_until == ({} if cloud_updated else {host.id: 1030})
