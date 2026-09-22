"""Tests for project Ceph capture freeze / unfreeze helpers."""

from unittest.mock import MagicMock, patch

import pytest

from helpers.ceph_freeze import (
    OSD_FREEZE_FLAGS,
    _ceph_cli_command,
    freeze_ceph_for_capture,
    unfreeze_ceph_after_capture,
)


def test_ceph_cli_command_uses_appliance_conf_and_keyring():
    cmd = _ceph_cli_command(["osd", "set", "noout"])
    assert cmd[0] == "sh"
    assert cmd[1] == "-c"
    script = cmd[2]
    assert "--conf /etc/ceph/ceph.conf" in script
    assert "/etc/ceph/ceph.client.admin.keyring" in script
    assert "client.admin" in script
    assert "osd set noout" in script
    assert "ROOK_CEPH_MON_HOST" not in script


@pytest.fixture
def mock_clients():
    core_api = MagicMock()
    apps_api = MagicMock()
    with patch("helpers.ceph_freeze._k8s_clients", return_value=(core_api, apps_api)):
        yield core_api, apps_api


class TestFreezeCephForCapture:
    def test_sets_flags_flushes_then_stops_osds_then_mons(self, mock_clients):
        core_api, apps_api = mock_clients
        calls: list[tuple] = []

        def record_ceph(_core, namespace, args):
            calls.append(("ceph", namespace, list(args)))

        def record_scale(_apps, namespace, label_selector, replicas):
            calls.append(("scale", namespace, label_selector, replicas))
            return {f"dep-{label_selector}-{replicas}": 1}

        def record_wait(_core, namespace, label_selector, running):
            calls.append(("wait", namespace, label_selector, running))

        with (
            patch("helpers.ceph_freeze._ceph_exec", side_effect=record_ceph),
            patch("helpers.ceph_freeze._scale_deployments", side_effect=record_scale),
            patch("helpers.ceph_freeze._wait_for_pods", side_effect=record_wait),
            patch("helpers.ceph_freeze._save_freeze_state") as save_state,
        ):
            freeze_ceph_for_capture("troshka-abc123")

        expected_flags = [
            ("ceph", "troshka-abc123", ["osd", "set", f]) for f in OSD_FREEZE_FLAGS
        ]
        assert calls[: len(expected_flags)] == expected_flags
        flush_idx = len(expected_flags)
        assert calls[flush_idx] == (
            "ceph",
            "troshka-abc123",
            ["tell", "osd.*", "flush_store_cache"],
        )
        assert calls[flush_idx + 1] == (
            "scale",
            "troshka-abc123",
            "app=troshka-ceph-osd",
            0,
        )
        assert calls[flush_idx + 2] == (
            "wait",
            "troshka-abc123",
            "app=troshka-ceph-osd",
            False,
        )
        assert calls[flush_idx + 3] == (
            "scale",
            "troshka-abc123",
            "app=troshka-ceph-mon",
            0,
        )
        assert calls[flush_idx + 4] == (
            "wait",
            "troshka-abc123",
            "app=troshka-ceph-mon",
            False,
        )
        save_state.assert_called_once()

    def test_raises_when_ceph_exec_fails(self, mock_clients):
        with (
            patch(
                "helpers.ceph_freeze._ceph_exec",
                side_effect=RuntimeError("mon pod missing"),
            ),
            patch("helpers.ceph_freeze._scale_deployments"),
            patch("helpers.ceph_freeze._wait_for_pods"),
            patch("helpers.ceph_freeze._save_freeze_state"),
        ):
            with pytest.raises(RuntimeError, match="mon pod missing"):
                freeze_ceph_for_capture("troshka-abc123")


class TestUnfreezeCephAfterCapture:
    def test_restarts_mons_osds_then_unsets_flags(self, mock_clients):
        core_api, apps_api = mock_clients
        calls: list[tuple] = []

        def record_scale(
            _apps, namespace, label_selector, replicas, deployment_replicas=None
        ):
            calls.append(("scale", namespace, label_selector, replicas))
            return deployment_replicas or {}

        def record_wait(_core, namespace, label_selector, running):
            calls.append(("wait", namespace, label_selector, running))

        def record_ceph(_core, namespace, args):
            calls.append(("ceph", namespace, list(args)))

        saved = {
            "troshka-ceph-mon": 1,
            "troshka-ceph-osd-0": 1,
            "troshka-ceph-osd-1": 1,
        }

        with (
            patch(
                "helpers.ceph_freeze._load_freeze_state",
                return_value=saved,
            ),
            patch(
                "helpers.ceph_freeze._restore_deployments",
                side_effect=lambda apps, ns, replicas: calls.append(
                    ("restore", ns, replicas)
                ),
            ),
            patch("helpers.ceph_freeze._wait_for_pods", side_effect=record_wait),
            patch("helpers.ceph_freeze._ceph_exec", side_effect=record_ceph),
            patch("helpers.ceph_freeze._clear_freeze_state"),
        ):
            unfreeze_ceph_after_capture("troshka-abc123")

        assert calls[0] == ("restore", "troshka-abc123", saved)
        assert calls[1] == (
            "wait",
            "troshka-abc123",
            "app=troshka-ceph-mon",
            True,
        )
        assert calls[2] == (
            "wait",
            "troshka-abc123",
            "app=troshka-ceph-osd",
            True,
        )
        unset_flags = list(reversed(OSD_FREEZE_FLAGS))
        for i, flag in enumerate(unset_flags):
            assert calls[3 + i] == (
                "ceph",
                "troshka-abc123",
                ["osd", "unset", flag],
            )

    def test_unfreeze_without_saved_state_scales_defaults(self, mock_clients):
        calls: list[tuple] = []

        def record_scale(_apps, namespace, label_selector, replicas):
            calls.append(("scale", namespace, label_selector, replicas))
            return {}

        with (
            patch("helpers.ceph_freeze._load_freeze_state", return_value=None),
            patch("helpers.ceph_freeze._scale_deployments", side_effect=record_scale),
            patch("helpers.ceph_freeze._wait_for_pods"),
            patch("helpers.ceph_freeze._ceph_exec"),
            patch("helpers.ceph_freeze._clear_freeze_state"),
        ):
            unfreeze_ceph_after_capture("troshka-abc123")

        assert ("scale", "troshka-abc123", "app=troshka-ceph-mon", 1) in calls
        assert ("scale", "troshka-abc123", "app=troshka-ceph-osd", 1) in calls
