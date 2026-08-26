import json

from helpers import kubevirt as kv_helpers
from helpers.kubevirt import (
    _apply_video_and_input_devices,
    admission_api_error_summary,
    build_kubevirt_vm,
    collect_kubevirt_vm_warnings,
    golden_import_matches,
    is_video_config_enabled,
    parse_admission_api_warnings,
    s3_import_url,
    video_config_skipped_warning,
)
from kubernetes.client.exceptions import ApiException


def test_s3_import_url_aws_style():
    url = s3_import_url(
        "library/rhel-9.6.qcow2",
        {"bucket": "troshka-images", "region": "us-east-1"},
    )
    assert (
        url
        == "https://s3.us-east-1.amazonaws.com/troshka-images/library/rhel-9.6.qcow2"
    )


def test_s3_import_url_custom_endpoint():
    url = s3_import_url(
        "library/rhel-9.6.qcow2",
        {
            "bucket": "troshka-gold-images",
            "endpoint": "https://s4.example.com",
        },
    )
    assert url == "https://s4.example.com/troshka-gold-images/library/rhel-9.6.qcow2"


def test_golden_import_matches_true():
    cfg = {"bucket": "troshka-gold-images", "endpoint": "https://s4.example.com"}
    path = "library/rhel-9.6.qcow2"
    dv = {
        "spec": {
            "source": {
                "s3": {
                    "url": s3_import_url(path, cfg),
                    "secretRef": "s3-central-credentials",  # pragma: allowlist secret
                }
            }
        }
    }
    assert golden_import_matches(dv, path, cfg, "s3-central-credentials")


def test_golden_import_matches_false_on_bucket():
    cfg = {"bucket": "troshka-gold-images", "endpoint": "https://s4.example.com"}
    path = "library/rhel-9.6.qcow2"
    dv = {
        "spec": {
            "source": {
                "s3": {
                    "url": "https://s3.us-east-1.amazonaws.com/troshka-images/library/rhel-9.6.qcow2",
                    "secretRef": "s3-central-credentials",  # pragma: allowlist secret
                }
            }
        }
    }
    assert not golden_import_matches(dv, path, cfg, "s3-central-credentials")


def test_apply_video_and_input_virtio():
    domain: dict = {"devices": {}}
    _apply_video_and_input_devices(
        domain,
        {"videoModel": "qxl", "inputModel": "virtio"},
        video_config_enabled=True,
    )
    assert domain["devices"]["video"] == {"type": "qxl"}
    assert domain["devices"]["inputs"] == [
        {"type": "tablet", "bus": "virtio", "name": "tablet0"}
    ]


def test_apply_video_omitted_when_video_config_gate_off():
    domain: dict = {"devices": {}}
    _apply_video_and_input_devices(
        domain,
        {"videoModel": "vga", "inputModel": "virtio"},
        video_config_enabled=False,
    )
    assert "video" not in domain["devices"]
    assert domain["devices"]["inputs"] == [
        {"type": "tablet", "bus": "virtio", "name": "tablet0"}
    ]
    warning = video_config_skipped_warning(
        {"videoModel": "vga"}, video_config_enabled=False
    )
    assert warning is not None
    assert "VideoConfig" in warning
    assert "vga" in warning


def test_collect_kubevirt_vm_warnings_empty_when_gate_on():
    assert (
        collect_kubevirt_vm_warnings(
            {"videoModel": "vga"}, video_config_enabled=True
        )
        == []
    )


def test_collect_kubevirt_vm_warnings_includes_video_skip():
    warnings = collect_kubevirt_vm_warnings(
        {"videoModel": "qxl"}, video_config_enabled=False
    )
    assert len(warnings) == 1
    assert "qxl" in warnings[0]


def test_apply_video_and_input_usb():
    domain: dict = {"devices": {}}
    _apply_video_and_input_devices(
        domain, {"inputModel": "usb"}, video_config_enabled=True
    )
    assert domain["devices"]["video"] == {"type": "virtio"}
    assert domain["devices"]["inputs"][0]["bus"] == "usb"


def test_apply_input_ps2_omits_tablet():
    domain: dict = {"devices": {}}
    _apply_video_and_input_devices(domain, {"inputModel": "ps2"})
    assert "inputs" not in domain["devices"]


def test_build_kubevirt_vm_includes_video_and_input():
    vm_cr = {
        "metadata": {"name": "vm-abc12345", "namespace": "troshka-test"},
        "spec": {
            "cpus": 2,
            "memory": 4096,
            "videoModel": "vga",
            "inputModel": "usb",
            "disks": [],
            "nics": [],
        },
    }
    body = build_kubevirt_vm(vm_cr, {}, {}, None, video_config_enabled=True)
    devices = body["spec"]["template"]["spec"]["domain"]["devices"]
    assert devices["video"] == {"type": "vga"}
    assert devices["inputs"] == [{"type": "tablet", "bus": "usb", "name": "tablet0"}]


def test_build_kubevirt_vm_rejects_missing_network_ref():
    import pytest

    from helpers.kubevirt import _build_networks

    with pytest.raises(ValueError, match="missing networkRef"):
        _build_networks({"nics": [{"id": "nic-abc"}]}, {})


def test_is_video_config_enabled_reads_kubevirt_cr():
    from helpers import kubevirt as kv_helpers

    kv_helpers._feature_gate_cache = None

    class FakeApi:
        def list_namespaced_custom_object(self, **kwargs):
            assert kwargs["namespace"] == "openshift-cnv"
            return {
                "items": [
                    {
                        "spec": {
                            "configuration": {
                                "developerConfiguration": {
                                    "featureGates": ["HotplugVolumes", "VideoConfig"]
                                }
                            }
                        }
                    }
                ]
            }

    assert is_video_config_enabled(FakeApi()) is True


def test_is_video_config_enabled_false_when_gate_missing():
    from helpers import kubevirt as kv_helpers

    kv_helpers._feature_gate_cache = None

    class FakeApi:
        def list_namespaced_custom_object(self, **kwargs):
            return {
                "items": [
                    {
                        "spec": {
                            "configuration": {
                                "developerConfiguration": {
                                    "featureGates": ["HotplugVolumes"]
                                }
                            }
                        }
                    }
                ]
            }

    assert is_video_config_enabled(FakeApi()) is False


def test_parse_admission_api_warnings_from_causes():
    body = {
        "message": (
            'admission webhook "virtualmachine-validator.kubevirt.io" denied '
            "the request: IDE bus is not supported"
        ),
        "details": {
            "causes": [
                {
                    "message": "IDE bus is not supported",
                    "field": "spec.template.spec.domain.devices.disks[0].disk.bus",
                },
                {
                    "message": "VideoConfig feature gate is not enabled",
                    "field": "spec.template.spec.video",
                },
            ]
        },
    }
    exc = ApiException(status=422)
    exc.body = json.dumps(body)
    warnings = parse_admission_api_warnings(exc)
    assert len(warnings) == 2
    assert "IDE bus is not supported" in warnings[0]
    assert "disks[0].disk.bus" in warnings[0]
    assert "VideoConfig" in warnings[1]


def test_parse_admission_api_warnings_ignores_non_admission_status():
    exc = ApiException(status=500, reason="Internal Server Error")
    exc.body = '{"message":"boom"}'
    assert parse_admission_api_warnings(exc) == []


def test_admission_api_error_summary_joins_causes():
    body = {
        "details": {
            "causes": [
                {"message": "IDE bus is not supported"},
                {"message": "VideoConfig feature gate is not enabled"},
            ]
        }
    }
    exc = ApiException(status=422, reason="Unprocessable Entity")
    exc.body = json.dumps(body)
    summary = admission_api_error_summary(exc)
    assert "IDE bus is not supported" in summary
    assert "VideoConfig" in summary
