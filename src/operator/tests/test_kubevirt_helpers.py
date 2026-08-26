from helpers.kubevirt import (
    _apply_video_and_input_devices,
    build_kubevirt_vm,
    golden_import_matches,
    s3_import_url,
)


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
        domain, {"videoModel": "qxl", "inputModel": "virtio"}
    )
    assert domain["devices"]["video"] == {"type": "qxl"}
    assert domain["devices"]["inputs"] == [
        {"type": "tablet", "bus": "virtio", "name": "tablet0"}
    ]


def test_apply_video_and_input_usb():
    domain: dict = {"devices": {}}
    _apply_video_and_input_devices(domain, {"inputModel": "usb"})
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
    body = build_kubevirt_vm(vm_cr, {}, {}, None)
    devices = body["spec"]["template"]["spec"]["domain"]["devices"]
    assert devices["video"] == {"type": "vga"}
    assert devices["inputs"] == [{"type": "tablet", "bus": "usb", "name": "tablet0"}]
