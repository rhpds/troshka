"""Tests for KubeVirtDriver.pull_file (snapshot+guestfish via operator annotation)."""

import base64
import json
from unittest.mock import MagicMock, patch

import app.services.providers.kubevirt as kv


class _U:
    hex = "reqid1234567"


def _drv():
    return kv.KubeVirtDriver()


def test_pull_file_sets_annotation_and_returns_content():
    custom, core = MagicMock(), MagicMock()
    payload = b"LB-EXT-KUBECONFIG"
    custom.get_namespaced_custom_object.return_value = {
        "status": {
            "filePull": {
                "requestId": "reqid1234567",
                "ready": True,
                "contentB64": base64.b64encode(payload).decode(),
            }
        }
    }
    with patch.object(
        kv, "_get_k8s_clients", return_value=(custom, core, None)
    ), patch.object(
        kv, "_resolve_vm_root_pvc", return_value=("vm-x-disk", 144)
    ), patch.object(
        kv, "_project_ns", return_value="ns1"
    ), patch(
        "uuid.uuid4", return_value=_U()
    ):
        out = _drv().pull_file(
            MagicMock(), "abcd1234ef", "a6f1d956zzzz", "/etc/k/lb-ext.kubeconfig"
        )
    assert out == payload
    ann = custom.patch_namespaced_custom_object.call_args[1]["body"]["metadata"][
        "annotations"
    ]["troshka.redhat.com/file-pull-request"]
    d = json.loads(ann)
    assert d["path"] == "/etc/k/lb-ext.kubeconfig"
    assert d["pvcName"] == "vm-x-disk"
    assert d["vmName"] == "troshka-vm-a6f1d956"
    assert d["requestId"] == "reqid1234567"


def test_pull_file_raises_on_operator_error():
    custom, core = MagicMock(), MagicMock()
    custom.get_namespaced_custom_object.return_value = {
        "status": {
            "filePull": {
                "requestId": "reqid1234567",
                "ready": True,
                "error": "not found",
            }
        }
    }
    with patch.object(
        kv, "_get_k8s_clients", return_value=(custom, core, None)
    ), patch.object(
        kv, "_resolve_vm_root_pvc", return_value=("vm-x-disk", 144)
    ), patch.object(
        kv, "_project_ns", return_value="ns1"
    ), patch(
        "uuid.uuid4", return_value=_U()
    ):
        try:
            _drv().pull_file(MagicMock(), "abcd1234ef", "a6f1d956", "/etc/x")
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "not found" in str(e)
