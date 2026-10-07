from datetime import UTC, datetime, timedelta

from app.services.metering_math import (
    covered_seconds,
    desired_resources,
    diff_intervals,
    interval_cost,
    month_bounds,
    period_usage,
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


def test_covered_seconds_skips_stopped_gap():
    start = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    now = start + timedelta(hours=3)
    rows = [
        {
            "started_at": start,
            "ended_at": start + timedelta(hours=1),
        },
        {
            "started_at": start + timedelta(hours=2),
            "ended_at": None,
        },
    ]
    assert abs(covered_seconds(rows, now) - 7200.0) < 1e-6


def test_period_usage_clips_to_month():
    start = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    rows = [
        {
            "kind": "vcpu",
            "qty": 2,
            "unit_rate": 1.0,
            "started_at": start,
            "ended_at": None,
        }
    ]
    p0, p1 = month_bounds(2026, 10)
    total, lines = period_usage(rows, p0, p1, now)
    # Oct 1 00:00 → Oct 2 12:00 = 36 hours × 2 × $1 = $72
    assert abs(total - 72.0) < 1e-9
    assert lines[0]["kind"] == "vcpu"
    assert abs(lines[0]["qty_hours"] - 72.0) < 1e-9
    assert abs(lines[0]["hours"] - 36.0) < 1e-9
    assert abs(lines[0]["unit_rate"] - 1.0) < 1e-9


def test_period_usage_hours_are_wall_clock_not_summed():
    """Overlapping same-kind intervals must not inflate Hours."""
    start = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    now = start + timedelta(hours=3)
    rows = [
        {
            "kind": "disk",
            "qty": 100,
            "unit_rate": 0.001,
            "started_at": start,
            "ended_at": None,
        },
        {
            "kind": "disk",
            "qty": 200,
            "unit_rate": 0.001,
            "started_at": start,
            "ended_at": None,
        },
    ]
    p0, p1 = month_bounds(2026, 10)
    total, lines = period_usage(rows, p0, p1, now)
    assert lines[0]["kind"] == "disk"
    assert abs(lines[0]["hours"] - 3.0) < 1e-9
    assert abs(lines[0]["qty_hours"] - 900.0) < 1e-9  # (100+200)*3
    assert abs(total - 0.9) < 1e-9


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


def test_desired_active_project_assumes_running_without_power_flag():
    got = desired_resources(
        {
            "provider_type": "ocpvirt",
            "host_id": "h1",
            "project_state": "active",
            "vms": [{"id": "vm1", "vcpus": 4, "ram_gib": 8}],
        }
    )
    assert ("vcpu", "vm1", 4.0, "h1", "ocpvirt") in got
    assert ("ram", "vm1", 8.0, "h1", "ocpvirt") in got


def test_desired_stopped_project_without_power_flag_skips_cpu():
    got = desired_resources(
        {
            "provider_type": "ocpvirt",
            "host_id": "h1",
            "project_state": "stopped",
            "vms": [{"id": "vm1", "vcpus": 4, "ram_gib": 8}],
            "disks": [{"id": "d1", "size_gib": 80, "host_id": "h1"}],
        }
    )
    kinds = {row[0] for row in got}
    assert not kinds


def test_desired_error_project_skips_all():
    got = desired_resources(
        {
            "provider_type": "ocpvirt",
            "host_id": "h1",
            "project_state": "error",
            "vms": [
                {
                    "id": "vm1",
                    "vcpus": 4,
                    "ram_gib": 8,
                    "live_state": "running",
                }
            ],
            "disks": [{"id": "d1", "size_gib": 80, "host_id": "h1"}],
        }
    )
    assert not got


def test_desired_dedicated_error_skips_full_host():
    got = desired_resources(
        {
            "provider_type": "ocpvirt",
            "host_id": "h1",
            "billing_mode": "dedicated",
            "project_state": "error",
            "host_vcpus": 64,
            "host_ram_gib": 256,
            "vms": [],
            "disks": [],
        }
    )
    assert not got


def test_desired_dedicated_bills_full_host_when_stopped():
    got = desired_resources(
        {
            "provider_type": "ocpvirt",
            "host_id": "h1",
            "billing_mode": "dedicated",
            "project_state": "stopped",
            "host_vcpus": 64,
            "host_ram_gib": 256,
            "vms": [{"id": "vm1", "vcpus": 4, "ram_gib": 8, "running": False}],
            "disks": [{"id": "d1", "size_gib": 80, "host_id": "h1"}],
        }
    )
    assert ("vcpu", "host:h1", 64.0, "h1", "ocpvirt") in got
    assert ("ram", "host:h1", 256.0, "h1", "ocpvirt") in got
    assert not any(row[0] == "disk" for row in got)
    assert not any(row[1] == "vm1" for row in got)


def test_desired_autostart_false_skips_cpu_on_active_project():
    got = desired_resources(
        {
            "provider_type": "kubevirt",
            "host_id": "h1",
            "project_state": "active",
            "vms": [
                {
                    "id": "vm1",
                    "vcpus": 16,
                    "ram_gib": 64,
                    "auto_start": False,
                }
            ],
        }
    )
    assert not any(row[0] == "vcpu" for row in got)


def test_desired_live_running_overrides_autostart_false():
    got = desired_resources(
        {
            "provider_type": "kubevirt",
            "host_id": "h1",
            "project_state": "active",
            "vms": [
                {
                    "id": "vm1",
                    "vcpus": 2,
                    "ram_gib": 4,
                    "auto_start": False,
                    "live_state": "running",
                }
            ],
        }
    )
    assert ("vcpu", "vm1", 2.0, "h1", "kubevirt") in got


def test_desired_live_shutoff_skips_cpu_on_active_project():
    got = desired_resources(
        {
            "provider_type": "ocpvirt",
            "host_id": "h1",
            "project_state": "active",
            "vms": [
                {
                    "id": "vm1",
                    "vcpus": 4,
                    "ram_gib": 8,
                    "live_state": "shutoff",
                }
            ],
        }
    )
    assert not any(row[0] == "vcpu" for row in got)


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


def test_desired_paused_vm_still_bills_cpu_ram():
    got = desired_resources(
        {
            "provider_type": "libvirt",
            "host_id": "h1",
            "project_state": "active",
            "vms": [
                {
                    "id": "vm1",
                    "vcpus": 4,
                    "ram_gib": 8,
                    "live_state": "paused",
                }
            ],
            "disks": [{"id": "d1", "size_gib": 80, "host_id": "h1"}],
        }
    )
    assert ("vcpu", "vm1", 4.0, "h1", "libvirt") in got
    assert ("ram", "vm1", 8.0, "h1", "libvirt") in got
    assert ("disk", "d1", 80.0, "h1", "libvirt") in got


def test_desired_hibernated_vm_skips_cpu_ram_keeps_disk():
    got = desired_resources(
        {
            "provider_type": "libvirt",
            "host_id": "h1",
            "project_state": "active",
            "vms": [
                {
                    "id": "vm1",
                    "vcpus": 4,
                    "ram_gib": 8,
                    "live_state": "hibernated",
                }
            ],
            "disks": [{"id": "d1", "size_gib": 80, "host_id": "h1"}],
        }
    )
    kinds = {row[0] for row in got if row[1] == "vm1"}
    assert "vcpu" not in kinds and "ram" not in kinds
    assert ("disk", "d1", 80.0, "h1", "libvirt") in got
