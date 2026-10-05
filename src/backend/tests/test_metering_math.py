from datetime import UTC, datetime, timedelta

from app.services.metering_math import (
    desired_resources,
    diff_intervals,
    interval_cost,
    resolve_rate,
    spend_total,
)


def test_host_override_beats_type_default():
    assert (
        resolve_rate(
            "vcpu",
            "aws",
            {"vcpu_hour": 0.05},
            {"aws": {"vcpu_hour": 0.02}},
        )
        == 0.05
    )


def test_missing_rate_is_zero():
    assert resolve_rate("vcpu", "aws", None, {}) == 0.0
    assert resolve_rate("nope", "aws", None, {"aws": {"vcpu_hour": 1}}) == 0.0


def test_interval_cost_two_vcpu_hour_and_half():
    start = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    end = start + timedelta(hours=1.5)
    assert abs(interval_cost(2, 0.10, start, end) - 0.30) < 1e-9


def test_spend_total_closed_plus_open():
    start = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    now = start + timedelta(hours=2)
    rows = [
        {
            "qty": 1,
            "unit_rate": 1.0,
            "started_at": start,
            "ended_at": start + timedelta(hours=1),
        },
        {"qty": 1, "unit_rate": 1.0, "started_at": start, "ended_at": None},
    ]
    assert abs(spend_total(rows, now) - 3.0) < 1e-9


def test_desired_stopped_vm_keeps_disk_not_cpu():
    got = desired_resources(
        {
            "provider_type": "aws",
            "host_id": "h1",
            "vms": [
                {
                    "id": "vm1",
                    "running": False,
                    "vcpus": 4,
                    "ram_gib": 8,
                    "host_id": "h1",
                }
            ],
            "disks": [{"id": "d1", "size_gib": 50, "host_id": "h1"}],
        }
    )
    kinds = {row[0] for row in got}
    assert "vcpu" not in kinds
    assert "ram" not in kinds
    assert ("disk", "d1", 50.0, "h1", "aws") in got


def test_desired_running_vm_cpu_ram():
    got = desired_resources(
        {
            "provider_type": "libvirt",
            "host_id": "h1",
            "vms": [
                {
                    "id": "vm1",
                    "state": "running",
                    "vcpus": 2,
                    "ram_gib": 4,
                }
            ],
        }
    )
    assert ("vcpu", "vm1", 2.0, "h1", "libvirt") in got
    assert ("ram", "vm1", 4.0, "h1", "libvirt") in got


def test_diff_missed_stop_and_new_disk():
    open_rows = [("vcpu", "vm1", 2.0), ("disk", "d1", 10.0)]
    desired = {("disk", "d1", 10.0, "h1", "aws"), ("disk", "d2", 20.0, "h1", "aws")}
    to_close, to_open = diff_intervals(open_rows, desired)
    assert ("vcpu", "vm1", 2.0) in to_close
    assert any(row[0] == "disk" and row[1] == "d2" for row in to_open)
    assert not any(row[1] == "d1" and row[0] == "disk" for row in to_open)
