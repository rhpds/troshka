"""Tests for materializing project-Ceph mon/OSD PVCs on pattern restore."""

import asyncio
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException

from helpers.ceph_restore import (
    MON_RESTORE_JOB_NAME,
    build_ceph_restore_datavolumes,
    build_ceph_restore_resources,
    build_identity_object_manifests,
    build_mon_restore_job,
    build_mon_restore_pvc,
    build_osd_restore_datavolume,
    device_s3_config,
    materialize_ceph_restore_pvcs,
    osd_restore_pvc_name,
    restore_identity_objects,
    wait_for_ceph_restore_datavolumes,
)
from helpers.rook_ceph import CEPH_MON_PVC_NAME

_DEFAULT_SECRET = "s3-credentials"  # pragma: allowlist secret
_CENTRAL_SECRET = "s3-central-credentials"  # pragma: allowlist secret
_OBC_SECRET = "s3-obc-credentials"  # pragma: allowlist secret

_S3_CONFIG = {
    "bucket": "troshka-images",
    "endpoint": "https://s3.example.com",
    "region": "us-east-1",
}
_MON_DEVICE = {
    "s3Path": "patterns/pat1/ceph-mon-0.tar.gz",
    "format": "tar.gz",
    "sizeBytes": 5 * 1073741824,
    "virtualSizeBytes": 8 * 1073741824,
    "source": "central",
    "index": 0,
}
_OSD_DEVICE_0 = {
    "s3Path": "patterns/pat1/ceph-osd-0.qcow2",
    "format": "qcow2",
    "sizeBytes": 40 * 1073741824,
    "virtualSizeBytes": 50 * 1073741824,
    "source": "obc",
    "index": 0,
}
_OSD_DEVICE_1 = {
    "s3Path": "patterns/pat1/ceph-osd-1.qcow2",
    "format": "qcow2",
    "sizeBytes": 40 * 1073741824,
    "virtualSizeBytes": 50 * 1073741824,
    "source": "central",
    "index": 1,
}


def test_osd_restore_pvc_name_is_deterministic():
    assert osd_restore_pvc_name(0) == "troshka-ceph-osd-0"
    assert osd_restore_pvc_name(3) == "troshka-ceph-osd-3"


class TestDeviceS3Config:
    def test_obc_source_uses_obc_config_when_present(self):
        s3_config = {
            **_S3_CONFIG,
            "obcConfig": {"bucket": "local", "credentialsSecret": _OBC_SECRET},
        }
        cfg, secret = device_s3_config({"source": "obc"}, s3_config, None)
        assert cfg == s3_config["obcConfig"]
        assert secret == _OBC_SECRET

    def test_central_source_uses_central_config(self):
        central = {"bucket": "central-bucket"}
        cfg, secret = device_s3_config({"source": "central"}, _S3_CONFIG, central)
        assert cfg == central
        assert secret == _CENTRAL_SECRET

    def test_falls_back_to_project_s3_config(self):
        cfg, secret = device_s3_config({"source": "central"}, _S3_CONFIG, None)
        assert cfg == _S3_CONFIG
        assert secret == _DEFAULT_SECRET

    def test_obc_source_without_obc_config_falls_back(self):
        cfg, secret = device_s3_config({"source": "obc"}, _S3_CONFIG, None)
        assert cfg == _S3_CONFIG
        assert secret == _DEFAULT_SECRET


class TestBuildMonRestorePvcAndJob:
    def test_pvc_uses_fixed_mon_name_filesystem(self):
        pvc = build_mon_restore_pvc("troshka-abc", _MON_DEVICE)
        assert pvc["metadata"]["name"] == CEPH_MON_PVC_NAME
        assert pvc["metadata"]["namespace"] == "troshka-abc"
        assert "volumeMode" not in pvc["spec"]
        assert pvc["spec"]["storageClassName"] == "ocs-storagecluster-ceph-rbd"
        storage = pvc["spec"]["resources"]["requests"]["storage"]
        assert int(storage[:-2]) > 8

    def test_job_rclone_untars_onto_mon_pvc(self):
        job = build_mon_restore_job(
            "troshka-abc", _MON_DEVICE, _S3_CONFIG, _DEFAULT_SECRET
        )
        assert job["metadata"]["name"] == MON_RESTORE_JOB_NAME
        container = job["spec"]["template"]["spec"]["containers"][0]
        cmd = container["command"][2]
        assert "rclone copyto" in cmd
        assert "tar -C /disk -xzf" in cmd
        assert "--exclude=lost+found" in cmd
        assert "--no-same-owner" in cmd
        assert "--no-same-permissions" in cmd
        assert "test -d /disk/data/store.db" in cmd
        assert "ceph-mon-0.tar.gz" in cmd
        volumes = job["spec"]["template"]["spec"]["volumes"]
        disk = next(v for v in volumes if v["name"] == "disk")
        assert disk["persistentVolumeClaim"]["claimName"] == CEPH_MON_PVC_NAME


class TestBuildOsdRestoreDatavolume:
    def test_block_mode_with_device_set_labels(self):
        dv = build_osd_restore_datavolume(
            "troshka-abc", _OSD_DEVICE_0, _S3_CONFIG, _DEFAULT_SECRET, "ocs-storage"
        )
        assert dv["metadata"]["name"] == "troshka-ceph-osd-0"
        assert dv["spec"]["pvc"]["volumeMode"] == "Block"
        assert dv["spec"]["pvc"]["storageClassName"] == "ocs-storage"
        labels = dv["metadata"]["labels"]
        assert labels["troshka-role"] == "ceph-osd"
        assert labels["troshka-ceph-osd-index"] == "0"

    def test_index_1_labels(self):
        dv = build_osd_restore_datavolume(
            "troshka-abc", _OSD_DEVICE_1, _S3_CONFIG, _DEFAULT_SECRET, "ocs-storage"
        )
        assert dv["metadata"]["name"] == "troshka-ceph-osd-1"
        assert dv["metadata"]["labels"]["troshka-ceph-osd-index"] == "1"

    def test_sizes_above_virtual_size(self):
        dv = build_osd_restore_datavolume(
            "troshka-abc", _OSD_DEVICE_0, _S3_CONFIG, _DEFAULT_SECRET, "ocs-storage"
        )
        storage = dv["spec"]["pvc"]["resources"]["requests"]["storage"]
        assert int(storage[:-2]) > 50


class TestBuildCephRestoreResources:
    def test_none_when_no_restore_capture(self):
        assert build_ceph_restore_resources("ns", None, _S3_CONFIG) == (
            None,
            None,
            [],
            "",
            [],
        )

    def test_none_when_empty_devices(self):
        result = build_ceph_restore_resources(
            "ns", {"mon": None, "osds": []}, _S3_CONFIG
        )
        assert result == (None, None, [], "", [])

    def test_full_capture_emits_mon_pvc_job_and_osd_dvs(self):
        restore_capture = {"mon": _MON_DEVICE, "osds": [_OSD_DEVICE_1, _OSD_DEVICE_0]}
        mon_pvc, mon_job, osd_dvs, mon_name, osd_names = build_ceph_restore_resources(
            "troshka-abc",
            restore_capture,
            _S3_CONFIG,
            {"bucket": "central"},
            "ocs-storage",
        )
        assert mon_pvc is not None
        assert mon_job is not None
        assert mon_name == CEPH_MON_PVC_NAME
        assert osd_names == ["troshka-ceph-osd-0", "troshka-ceph-osd-1"]
        assert [dv["metadata"]["name"] for dv in osd_dvs] == osd_names
        assert osd_dvs[0]["spec"]["source"]["s3"]["secretRef"] == _DEFAULT_SECRET
        assert osd_dvs[1]["spec"]["source"]["s3"]["secretRef"] == _CENTRAL_SECRET

    def test_compat_datavolumes_alias_returns_mon_job(self):
        mon_job, osd_dvs, mon_name, osd_names = build_ceph_restore_datavolumes(
            "ns", {"mon": _MON_DEVICE, "osds": []}, _S3_CONFIG
        )
        assert mon_job["kind"] == "Job"
        assert mon_name == CEPH_MON_PVC_NAME
        assert osd_dvs == []
        assert osd_names == []

    def test_skips_devices_missing_s3_path(self):
        restore_capture = {
            "mon": {"source": "central"},
            "osds": [{"index": 0, "source": "central"}],
        }
        mon_pvc, mon_job, osd_dvs, mon_name, osd_names = build_ceph_restore_resources(
            "ns", restore_capture, _S3_CONFIG
        )
        assert mon_pvc is None
        assert mon_job is None
        assert mon_name == ""
        assert osd_dvs == []
        assert osd_names == []


class TestMaterializeCephRestorePvcs:
    def test_creates_mon_pvc_job_and_osd_datavolumes(self):
        custom_api = MagicMock()
        core_api = MagicMock()
        batch_api = MagicMock()
        restore_capture = {"mon": _MON_DEVICE, "osds": [_OSD_DEVICE_0]}

        mon_name, osd_names = materialize_ceph_restore_pvcs(
            custom_api,
            "troshka-abc",
            restore_capture,
            _S3_CONFIG,
            storage_class="ocs-storage",
            core_api=core_api,
            batch_api=batch_api,
        )

        assert mon_name == CEPH_MON_PVC_NAME
        assert osd_names == ["troshka-ceph-osd-0"]
        custom_api.delete_namespaced_custom_object.assert_called()
        core_api.create_namespaced_persistent_volume_claim.assert_called_once()
        batch_api.create_namespaced_job.assert_called_once()
        assert custom_api.create_namespaced_custom_object.call_count == 1
        created_dv = custom_api.create_namespaced_custom_object.call_args.kwargs["body"]
        assert created_dv["metadata"]["name"] == "troshka-ceph-osd-0"

    def test_returns_empty_when_no_capture(self):
        custom_api = MagicMock()
        mon_name, osd_names = materialize_ceph_restore_pvcs(
            custom_api, "ns", None, _S3_CONFIG
        )
        assert (mon_name, osd_names) == ("", [])
        custom_api.create_namespaced_custom_object.assert_not_called()

    def test_requires_core_and_batch_for_mon(self):
        custom_api = MagicMock()
        with pytest.raises(ValueError, match="core_api and batch_api"):
            materialize_ceph_restore_pvcs(
                custom_api, "ns", {"mon": _MON_DEVICE, "osds": []}, _S3_CONFIG
            )

    def test_idempotent_on_409(self):
        custom_api = MagicMock()
        custom_api.delete_namespaced_custom_object.side_effect = ApiException(
            status=404
        )
        core_api = MagicMock()
        core_api.read_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )
        core_api.create_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=409
        )
        batch_api = MagicMock()
        batch_api.create_namespaced_job.side_effect = ApiException(status=409)
        job = MagicMock()
        job.status.succeeded = 1
        batch_api.read_namespaced_job.return_value = job
        restore_capture = {"mon": _MON_DEVICE, "osds": []}

        mon_name, osd_names = materialize_ceph_restore_pvcs(
            custom_api,
            "ns",
            restore_capture,
            _S3_CONFIG,
            core_api=core_api,
            batch_api=batch_api,
        )
        assert mon_name == CEPH_MON_PVC_NAME
        assert osd_names == []

    def test_reraises_non_409_api_exception(self):
        custom_api = MagicMock()
        custom_api.delete_namespaced_custom_object.side_effect = ApiException(
            status=404
        )
        core_api = MagicMock()
        core_api.create_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=500
        )
        batch_api = MagicMock()
        restore_capture = {"mon": _MON_DEVICE, "osds": []}

        with pytest.raises(ApiException):
            materialize_ceph_restore_pvcs(
                custom_api,
                "ns",
                restore_capture,
                _S3_CONFIG,
                core_api=core_api,
                batch_api=batch_api,
            )


class TestWaitForCephRestoreDatavolumes:
    def test_returns_when_names_empty(self):
        custom_api = MagicMock()
        asyncio.run(wait_for_ceph_restore_datavolumes(custom_api, "ns", []))
        custom_api.get_namespaced_custom_object.assert_not_called()

    def test_returns_once_all_succeeded(self):
        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Succeeded"}
        }
        batch_api = MagicMock()
        job = MagicMock()
        job.status.succeeded = 1
        job.status.conditions = []
        job.status.failed = 0
        batch_api.read_namespaced_job.return_value = job
        asyncio.run(
            wait_for_ceph_restore_datavolumes(
                custom_api,
                "ns",
                ["troshka-ceph-mon", "troshka-ceph-osd-0"],
                batch_api=batch_api,
            )
        )
        assert custom_api.get_namespaced_custom_object.call_count >= 1
        batch_api.read_namespaced_job.assert_called()

    def test_raises_on_failed_phase(self):
        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Failed"}
        }
        with pytest.raises(RuntimeError, match="failed to import"):
            asyncio.run(
                wait_for_ceph_restore_datavolumes(
                    custom_api, "ns", ["troshka-ceph-osd-0"]
                )
            )

    def test_raises_on_mon_job_failed(self):
        custom_api = MagicMock()
        batch_api = MagicMock()
        job = MagicMock()
        job.status.succeeded = 0
        cond = MagicMock()
        cond.type = "Failed"
        cond.status = "True"
        job.status.conditions = [cond]
        job.status.failed = 1
        batch_api.read_namespaced_job.return_value = job
        with pytest.raises(RuntimeError, match="mon Job"):
            asyncio.run(
                wait_for_ceph_restore_datavolumes(
                    custom_api, "ns", ["troshka-ceph-mon"], batch_api=batch_api
                )
            )

    def test_raises_on_timeout(self):
        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "ImportInProgress"}
        }
        with pytest.raises(RuntimeError, match="not complete"):
            asyncio.run(
                wait_for_ceph_restore_datavolumes(
                    custom_api,
                    "ns",
                    ["troshka-ceph-osd-0"],
                    max_wait_seconds=1,
                    sleep_seconds=1,
                )
            )

    def test_polls_until_succeeded(self):
        custom_api = MagicMock()
        phases = iter(
            [
                {"status": {"phase": "ImportInProgress"}},
                {"status": {"phase": "Succeeded"}},
            ]
        )
        custom_api.get_namespaced_custom_object.side_effect = lambda **_: next(phases)
        asyncio.run(
            wait_for_ceph_restore_datavolumes(
                custom_api,
                "ns",
                ["troshka-ceph-osd-0"],
                max_wait_seconds=10,
                sleep_seconds=1,
            )
        )
        assert custom_api.get_namespaced_custom_object.call_count == 2

    def test_missing_datavolume_treated_as_pending_not_error(self):
        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
        with pytest.raises(RuntimeError, match="not complete"):
            asyncio.run(
                wait_for_ceph_restore_datavolumes(
                    custom_api,
                    "ns",
                    ["troshka-ceph-osd-0"],
                    max_wait_seconds=1,
                    sleep_seconds=1,
                )
            )


_IDENTITY_OBJECTS = [
    {
        "kind": "Secret",
        "name": "troshka-ceph-fsid",
        "data": {
            "fsid": "ZnNpZC0xMjM=",
        },
    },
    {
        "kind": "ConfigMap",
        "name": "troshka-ceph-conf",
        # base64("[global]\nfsid = x")
        "data": {"ceph.conf": "W2dsb2JhbF0KZnNpZCA9IHg="},
    },
    {
        "kind": "Secret",
        "name": "troshka-ceph-admin-keyring",
        "data": {
            "keyring": "W2NsaWVudC5hZG1pbl0=",  # pragma: allowlist secret  # gitleaks:allow
        },
    },
]


class TestBuildIdentityObjectManifests:
    def test_none_when_no_identity_objects(self):
        assert build_identity_object_manifests("ns", None) == []
        assert build_identity_object_manifests("ns", []) == []

    def test_secret_data_passed_through_as_is(self):
        manifests = build_identity_object_manifests("troshka-abc", _IDENTITY_OBJECTS)
        mon_secret = next(
            m for m in manifests if m["metadata"]["name"] == "troshka-ceph-fsid"
        )
        assert mon_secret["kind"] == "Secret"
        assert mon_secret["metadata"]["namespace"] == "troshka-abc"
        assert mon_secret["type"] == "Opaque"
        assert mon_secret["data"] == {
            "fsid": "ZnNpZC0xMjM=",
        }

    def test_configmap_data_decoded_from_base64(self):
        manifests = build_identity_object_manifests("troshka-abc", _IDENTITY_OBJECTS)
        conf = next(
            m for m in manifests if m["metadata"]["name"] == "troshka-ceph-conf"
        )
        assert conf["kind"] == "ConfigMap"
        assert conf["data"] == {"ceph.conf": "[global]\nfsid = x"}

    def test_no_owner_references(self):
        """Orphan-safe: TroshkaCeph does not exist yet at restore time."""
        manifests = build_identity_object_manifests("ns", _IDENTITY_OBJECTS)
        assert all("ownerReferences" not in m["metadata"] for m in manifests)

    def test_skips_malformed_entries(self):
        malformed = [{"kind": "Secret"}, {"name": "x"}, {"kind": "Bogus", "name": "y"}]
        assert build_identity_object_manifests("ns", malformed) == []


class TestRestoreIdentityObjects:
    def test_creates_secrets_and_configmaps(self):
        core_api = MagicMock()
        restore_identity_objects(core_api, "troshka-abc", _IDENTITY_OBJECTS)

        assert core_api.create_namespaced_secret.call_count == 2
        assert core_api.create_namespaced_config_map.call_count == 1
        created_secret_names = {
            call.kwargs["body"]["metadata"]["name"]
            for call in core_api.create_namespaced_secret.call_args_list
        }
        assert created_secret_names == {
            "troshka-ceph-fsid",
            "troshka-ceph-admin-keyring",
        }
        cm_call = core_api.create_namespaced_config_map.call_args
        assert cm_call.kwargs["body"]["metadata"]["name"] == "troshka-ceph-conf"
        assert cm_call.kwargs["namespace"] == "troshka-abc"

    def test_noop_when_no_identity_objects(self):
        core_api = MagicMock()
        restore_identity_objects(core_api, "ns", None)
        core_api.create_namespaced_secret.assert_not_called()
        core_api.create_namespaced_config_map.assert_not_called()

    def test_idempotent_on_409(self):
        core_api = MagicMock()
        core_api.create_namespaced_secret.side_effect = ApiException(status=409)
        core_api.create_namespaced_config_map.side_effect = ApiException(status=409)
        # Must not raise.
        restore_identity_objects(core_api, "ns", _IDENTITY_OBJECTS)

    def test_reraises_non_409_api_exception(self):
        core_api = MagicMock()
        core_api.create_namespaced_secret.side_effect = ApiException(status=500)
        with pytest.raises(ApiException):
            restore_identity_objects(core_api, "ns", _IDENTITY_OBJECTS)
