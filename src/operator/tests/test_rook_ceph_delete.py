"""Tests for Rook Ceph teardown / stuck-finalizer cleanup."""

from unittest.mock import MagicMock, patch

from kubernetes.client.exceptions import ApiException

from helpers.rook_ceph import delete_rook_ceph_crs


def _api_404():
    return ApiException(status=404)


def test_delete_rook_ceph_crs_waits_until_gone():
    custom_api = MagicMock()
    core_api = MagicMock()
    # First poll: still present; second: 404
    custom_api.get_namespaced_custom_object.side_effect = [
        {"metadata": {"name": "troshka-ceph-pool"}},
        _api_404(),
        {"metadata": {"name": "troshka-ceph"}},
        _api_404(),
    ]
    core_api.list_namespaced_secret.return_value = MagicMock(items=[])
    core_api.list_namespaced_config_map.return_value = MagicMock(items=[])

    with patch("helpers.rook_ceph.time.sleep"):
        delete_rook_ceph_crs(
            custom_api, core_api, "ns", wait_seconds=10, poll_seconds=0
        )

    assert custom_api.delete_namespaced_custom_object.call_count == 2
    custom_api.patch_namespaced_custom_object.assert_not_called()


def test_delete_rook_ceph_crs_strips_finalizers_when_stuck():
    custom_api = MagicMock()
    core_api = MagicMock()
    custom_api.get_namespaced_custom_object.return_value = {
        "metadata": {"name": "troshka-ceph"}
    }
    secret = MagicMock()
    secret.metadata.name = "rook-ceph-mon"
    secret.metadata.finalizers = ["ceph.rook.io/disaster-protection"]
    core_api.list_namespaced_secret.return_value = MagicMock(items=[secret])
    core_api.list_namespaced_config_map.return_value = MagicMock(items=[])

    with patch("helpers.rook_ceph.time.sleep"):
        delete_rook_ceph_crs(
            custom_api, core_api, "ns", wait_seconds=0, poll_seconds=0
        )

    assert custom_api.patch_namespaced_custom_object.call_count == 2
    core_api.patch_namespaced_secret.assert_called_once()
    assert (
        core_api.patch_namespaced_secret.call_args.kwargs["body"]["metadata"][
            "finalizers"
        ]
        is None
    )


def test_delete_rook_ceph_crs_ignores_missing():
    custom_api = MagicMock()
    core_api = MagicMock()
    custom_api.delete_namespaced_custom_object.side_effect = ApiException(status=404)
    custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
    core_api.list_namespaced_secret.return_value = MagicMock(items=[])
    core_api.list_namespaced_config_map.return_value = MagicMock(items=[])

    with patch("helpers.rook_ceph.time.sleep"):
        delete_rook_ceph_crs(
            custom_api, core_api, "ns", wait_seconds=5, poll_seconds=0
        )

    custom_api.patch_namespaced_custom_object.assert_not_called()
