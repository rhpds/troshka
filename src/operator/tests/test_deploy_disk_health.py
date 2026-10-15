from unittest.mock import MagicMock

import pytest
from handlers.project import _handle_deploying_phase
from helpers.disk_health import datavolume_error, deployment_disk_error


def _dv(name, source=None, message=None):
    return {
        "metadata": {"name": name},
        "spec": {"source": source or {}},
        "status": {
            "phase": "ImportInProgress",
            "conditions": (
                [
                    {
                        "type": "Running",
                        "status": "False",
                        "reason": "Error",
                        "message": message,
                    }
                ]
                if message
                else []
            ),
        },
    }


def test_missing_golden_image_fails_project_with_source_error():
    api = MagicMock()
    api.list_namespaced_custom_object.return_value = {
        "items": [
            _dv(
                "vm-disk",
                {"pvc": {"name": "golden-image", "namespace": "troshka-cache"}},
            )
        ]
    }
    api.get_namespaced_custom_object.return_value = _dv(
        "golden-image", message="Unable to get s3 object: NoSuchKey: status code: 404"
    )
    error = deployment_disk_error(api, "troshka-project")
    assert "golden-image" in error
    assert "NoSuchKey" in error


def test_transient_import_error_does_not_fail_a_slow_valid_import():
    api = MagicMock()
    api.list_namespaced_custom_object.return_value = {
        "items": [_dv("vm-disk", message="Connection reset by peer")]
    }
    assert deployment_disk_error(api, "troshka-project") is None


def test_missing_direct_ceph_image_fails_project_too():
    api = MagicMock()
    api.list_namespaced_custom_object.return_value = {
        "items": [_dv("troshka-ceph-osd-0", message="NoSuchKey: 404")]
    }
    assert "troshka-ceph-osd-0" in deployment_disk_error(api, "project")


def test_project_stops_waiting_and_reports_missing_disk():
    api = MagicMock()
    api.list_namespaced_custom_object.return_value = {
        "items": [_dv("disk", message="NoSuchKey: 404")]
    }
    patch = MagicMock()
    patch.status = {}
    _handle_deploying_phase({}, {}, "ns", "proj", {}, patch, api, [], 0)
    assert patch.status["phase"] == "Error"
    assert "NoSuchKey" in patch.status["error"]


def test_project_has_a_deadline_even_without_a_terminal_disk_error(monkeypatch):
    monkeypatch.setattr("handlers.project.time.time", lambda: 8000)
    api = MagicMock()
    api.list_namespaced_custom_object.return_value = {"items": []}
    patch = MagicMock()
    patch.status = {}
    _handle_deploying_phase(
        {}, {"deployStartedAt": 1}, "ns", "proj", {}, patch, api, [], 0
    )
    assert patch.status["phase"] == "Error"
    assert "timed out" in patch.status["error"]


def test_active_golden_transfer_ignores_stale_bound_s3_error():
    golden = {
        "status": {
            "phase": "ImportInProgress",
            "conditions": [
                {
                    "type": "Bound",
                    "status": "False",
                    "reason": "Error",
                    "message": "Unable to get s3 object: NoSuchKey: status code: 404",
                },
                {
                    "type": "Ready",
                    "status": "False",
                    "reason": "TransferRunning",
                    "message": "",
                },
                {
                    "type": "Running",
                    "status": "True",
                    "reason": "TransferRunning",
                    "message": "",
                },
            ],
        },
    }
    api = MagicMock()
    api.list_namespaced_custom_object.return_value = {
        "items": [
            _dv(
                "vm-disk",
                {"pvc": {"name": "golden-image", "namespace": "troshka-cache"}},
            ),
        ]
    }
    api.get_namespaced_custom_object.return_value = golden
    assert datavolume_error(golden) is None
    assert deployment_disk_error(api, "project") is None


@pytest.mark.parametrize(
    "message",
    [
        "NoSuchKey: status code: 404",
        "CloneValidationFailed: target is invalid",
        "target PVC is smaller than the source",
        "PVC too small to contain image",
    ],
)
def test_actual_running_failures_remain_terminal(message):
    assert datavolume_error(_dv("disk", message=message)) == message


@pytest.mark.parametrize(
    "reason,message",
    [
        ("CloneValidationFailed", "target is invalid"),
        ("Error", "target PVC is smaller than the source"),
        ("Error", "PVC too small to contain image"),
    ],
)
def test_active_transfer_does_not_hide_clone_validation_or_size_failure(
    reason, message
):
    dv = {
        "status": {
            "phase": "ImportInProgress",
            "conditions": [
                {
                    "type": "Bound",
                    "status": "False",
                    "reason": reason,
                    "message": message,
                },
                {"type": "Running", "status": "True", "reason": "TransferRunning"},
            ],
        }
    }
    assert datavolume_error(dv) == message
