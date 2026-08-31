import json

from helpers import kubevirt as kv_helpers
from helpers.kubevirt import (
    _apply_serial_console,
    _apply_video_and_input_devices,
    admission_api_error_summary,
    build_clone_datavolume,
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
        collect_kubevirt_vm_warnings({"videoModel": "vga"}, video_config_enabled=True)
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


def test_apply_serial_console_enabled_by_default():
    domain: dict = {"devices": {}}
    _apply_serial_console(domain, {})
    assert domain["devices"]["autoattachSerialConsole"] is True


def test_apply_serial_console_disabled():
    domain: dict = {"devices": {}}
    _apply_serial_console(domain, {"serialConsole": False})
    assert domain["devices"]["autoattachSerialConsole"] is False


def test_apply_serial_console_headless_for_eos():
    domain: dict = {"devices": {}}
    _apply_serial_console(domain, {"serialExecType": "eos"})
    assert domain["devices"]["autoattachSerialConsole"] is True
    assert domain["devices"]["autoattachGraphicsDevice"] is False


def test_apply_serial_console_keeps_graphics_for_junos():
    domain: dict = {"devices": {}}
    _apply_serial_console(domain, {"serialExecType": "junos"})
    assert domain["devices"]["autoattachSerialConsole"] is True
    assert "autoattachGraphicsDevice" not in domain["devices"]


def test_apply_serial_console_keeps_graphics_for_linux():
    domain: dict = {"devices": {}}
    _apply_serial_console(domain, {"serialExecType": "linux"})
    assert domain["devices"]["autoattachSerialConsole"] is True
    assert "autoattachGraphicsDevice" not in domain["devices"]


def test_build_kubevirt_vm_includes_serial_console():
    vm_cr = {
        "metadata": {"name": "vm-abc12345", "namespace": "troshka-test"},
        "spec": {
            "cpus": 2,
            "memory": 4096,
            "serialConsole": False,
            "disks": [],
            "nics": [],
        },
    }
    body = build_kubevirt_vm(vm_cr, {}, {}, None)
    devices = body["spec"]["template"]["spec"]["domain"]["devices"]
    assert devices["autoattachSerialConsole"] is False


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


def test_build_kubevirt_vm_eos_nics_use_legacy_root_pci_addresses():
    vm_cr = {
        "metadata": {"name": "vm-abc12345", "namespace": "troshka-test"},
        "spec": {
            "cpus": 2,
            "memory": 4096,
            "legacyRootBus": True,
            "disks": [],
            "nics": [
                {
                    "id": "nic-0663165c-467b-4445-b9ce-cf5588a68105",
                    "mac": "52:54:00:00:01",
                    "model": "e1000",
                    "networkRef": "net-lab",
                },
                {
                    "id": "nic-199c831c-bdc5-4f1c-8906-5343d4ee3571",
                    "mac": "52:54:00:00:02",
                    "model": "e1000",
                    "networkRef": "net-link",
                },
            ],
        },
    }
    body = build_kubevirt_vm(
        vm_cr, {}, {"net-lab": "net-lab-nad", "net-link": "net-link-nad"}, None
    )
    ifaces = body["spec"]["template"]["spec"]["domain"]["devices"]["interfaces"]
    assert ifaces[0]["pciAddress"] == "0000:00:03.0"
    assert ifaces[1]["pciAddress"] == "0000:00:04.0"
    assert ifaces[0]["model"] == "e1000"


def test_build_kubevirt_vm_ios_nics_omit_pci_address():
    vm_cr = {
        "metadata": {"name": "vm-abc12345", "namespace": "troshka-test"},
        "spec": {
            "cpus": 2,
            "memory": 4096,
            "legacyRootBus": False,
            "disks": [],
            "nics": [
                {
                    "id": "nic-a96cfdfa-b6ca-4682-b89a-ad50a1806c65",
                    "mac": "52:54:00:00:01",
                    "model": "virtio",
                    "networkRef": "net-lab",
                },
            ],
        },
    }
    body = build_kubevirt_vm(vm_cr, {}, {"net-lab": "net-lab-nad"}, None)
    iface = body["spec"]["template"]["spec"]["domain"]["devices"]["interfaces"][0]
    assert "pciAddress" not in iface


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


def _clone_storage(dv):
    return dv["spec"]["pvc"]["resources"]["requests"]["storage"]


def test_build_clone_datavolume_pads_size_when_source_smaller():
    # No source floor: request is the padded disk size (max(size+10, size*1.2)).
    dv = build_clone_datavolume("d", "ns", "golden", "cache", 20)
    assert _clone_storage(dv) == "30Gi"


def test_build_clone_datavolume_floors_at_source_size():
    # Golden cached larger (30Gi) than the current 10Gi disk -> clone must be >= 30
    # or CDI rejects it with CloneValidationFailed. Regression for rtr3 ISO clone.
    dv = build_clone_datavolume("d", "ns", "golden", "cache", 10, source_size_gb=30)
    assert _clone_storage(dv) == "30Gi"


def test_build_clone_datavolume_source_floor_ignored_when_smaller():
    dv = build_clone_datavolume("d", "ns", "golden", "cache", 50, source_size_gb=30)
    assert _clone_storage(dv) == "60Gi"
