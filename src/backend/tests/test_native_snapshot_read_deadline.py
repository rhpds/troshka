"""Snapshot metadata reads share the bounded scan across intervening Ceph reads."""

import json
from types import SimpleNamespace

import pytest

from app.services.native_managed_power import helpers
from app.services.native_online_enrollment import ReadOnlyManaged
from app.services.native_snapshot_custody import _api


@pytest.fixture
def transport(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    module = helpers()
    monkeypatch.setattr(module.time, "monotonic", lambda: clock.now)
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, kwargs["timeout"].total))
        return SimpleNamespace(
            status=200,
            read=lambda size: json.dumps(
                {
                    "metadata": {},
                    "items": [{"metadata": {"uid": "snap", "namespace": "project"}}],
                }
            ).encode(),
            close=lambda: None,
        )

    api = SimpleNamespace(
        default_headers={},
        configuration=SimpleNamespace(host="https://unused.invalid"),
        update_params_for_auth=lambda *args: None,
        rest_client=SimpleNamespace(pool_manager=SimpleNamespace(request=request)),
    )
    return SimpleNamespace(clock=clock, calls=calls, api=api, module=module)


def test_snapshot_reads_survive_backend_work_but_stop_at_scan_deadline(transport):
    reader = _api(SimpleNamespace(api=transport.api, scan_deadline=310.0))
    # Ceph resolution used 110 seconds of the existing 210-second scan budget.
    transport.clock.now = 210.0
    assert reader.rows("VolumeSnapshot", "project")[0]["metadata"]["uid"] == "snap"
    transport.clock.now = 308.0
    assert reader.rows("VolumeSnapshot", "project")
    transport.clock.now = 310.0
    with pytest.raises(
        transport.module.ManagedPowerError, match="managed_observation_timeout"
    ):
        reader.rows("VolumeSnapshot", "project")
    assert transport.calls == [("GET", 5), ("GET", 2)]


def test_snapshot_short_scan_cannot_outlive_ordinary_inventory_window(transport):
    reader = _api(SimpleNamespace(api=transport.api, scan_deadline=110.0))
    transport.clock.now = 110.0
    with pytest.raises(
        transport.module.ManagedPowerError, match="managed_observation_timeout"
    ):
        reader.rows("VolumeSnapshot", "project")
    assert transport.calls == []


@pytest.mark.parametrize("snapshot", [False, True])
def test_reader_without_scan_deadline_retains_inventory_bound(transport, snapshot):
    kube = SimpleNamespace(api=transport.api)
    reader = _api(kube) if snapshot else ReadOnlyManaged(kube)
    transport.clock.now = 154.0
    assert reader.rows("VolumeSnapshot", "project")
    transport.clock.now = 155.0
    with pytest.raises(
        transport.module.ManagedPowerError, match="managed_observation_timeout"
    ):
        reader.rows("VolumeSnapshot", "project")
    assert transport.calls == [("GET", 1)]
