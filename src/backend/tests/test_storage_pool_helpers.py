"""Tests for storage pool service + API helpers and central library helpers."""

import os

os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test.db"

import io
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# storage_pool_service helpers
# ---------------------------------------------------------------------------
from app.services.storage_pool_service import (
    _resolve_fsx_mount_ip,
    _set_pool_status,
    find_best_az,
)


class TestFindBestAz:
    """find_best_az — pure logic, no AWS calls."""

    def test_returns_first_az_supporting_all(self):
        az_results = {
            "us-east-1b": {"supported": ["m5.xlarge"], "unsupported": ["i4i.xlarge"]},
            "us-east-1a": {
                "supported": ["m5.xlarge", "i4i.xlarge"],
                "unsupported": [],
            },
        }
        assert find_best_az(az_results, ["m5.xlarge", "i4i.xlarge"]) == "us-east-1a"

    def test_returns_none_when_no_az_supports_all(self):
        az_results = {
            "us-east-1a": {"supported": ["m5.xlarge"], "unsupported": ["i4i.xlarge"]},
            "us-east-1b": {"supported": ["i4i.xlarge"], "unsupported": ["m5.xlarge"]},
        }
        assert find_best_az(az_results, ["m5.xlarge", "i4i.xlarge"]) is None

    def test_returns_none_on_empty(self):
        assert find_best_az({}, ["m5.xlarge"]) is None

    def test_sorted_alphabetically(self):
        az_results = {
            "us-east-1c": {"supported": ["m5.xlarge"], "unsupported": []},
            "us-east-1a": {"supported": ["m5.xlarge"], "unsupported": []},
            "us-east-1b": {"supported": ["m5.xlarge"], "unsupported": []},
        }
        assert find_best_az(az_results, ["m5.xlarge"]) == "us-east-1a"


class TestSetPoolStatus:
    """_set_pool_status — DB setter helper."""

    def test_sets_status_when_pool_exists(self):
        mock_pool = MagicMock()
        mock_pool.status = "creating"
        mock_db = MagicMock()
        mock_db.get.return_value = mock_pool
        result = _set_pool_status(mock_db, "pool-123", "available")
        assert result is True
        assert mock_pool.status == "available"

    def test_returns_false_when_pool_not_found(self):
        mock_db = MagicMock()
        mock_db.get.return_value = None
        result = _set_pool_status(mock_db, "pool-missing", "error")
        assert result is False


class TestResolveFsxMountIp:
    """_resolve_fsx_mount_ip — ENI IP lookup via mocked boto3."""

    @patch("app.services.storage_pool_service._boto_client")
    def test_returns_ip_when_eni_found(self, mock_boto):
        mock_ec2 = MagicMock()
        mock_ec2.describe_network_interfaces.return_value = {
            "NetworkInterfaces": [{"PrivateIpAddress": "10.0.1.42"}]
        }
        mock_boto.return_value = mock_ec2

        fs = {"NetworkInterfaceIds": ["eni-abc123"]}
        ip = _resolve_fsx_mount_ip(fs, "us-east-1", {"access_key_id": "x"})
        assert ip == "10.0.1.42"
        mock_ec2.describe_network_interfaces.assert_called_once_with(
            NetworkInterfaceIds=["eni-abc123"]
        )

    @patch("app.services.storage_pool_service._boto_client")
    def test_returns_none_when_no_enis(self, mock_boto):
        fs = {"NetworkInterfaceIds": []}
        ip = _resolve_fsx_mount_ip(fs, "us-east-1", {})
        assert ip is None
        mock_boto.assert_not_called()

    @patch("app.services.storage_pool_service._boto_client")
    def test_returns_none_when_key_missing(self, mock_boto):
        fs = {}
        ip = _resolve_fsx_mount_ip(fs, "us-east-1", {})
        assert ip is None

    @patch("app.services.storage_pool_service._boto_client")
    def test_returns_none_when_eni_response_empty(self, mock_boto):
        mock_ec2 = MagicMock()
        mock_ec2.describe_network_interfaces.return_value = {"NetworkInterfaces": []}
        mock_boto.return_value = mock_ec2
        fs = {"NetworkInterfaceIds": ["eni-abc123"]}
        ip = _resolve_fsx_mount_ip(fs, "us-east-1", {})
        assert ip is None


class TestPollFsxUntilAvailable:
    """_poll_fsx_until_available — mocked boto3 + DB."""

    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service._resolve_fsx_mount_ip")
    @patch("app.services.storage_pool_service._boto_client")
    @patch("time.sleep", return_value=None)
    def test_sets_available_on_success(self, _sleep, mock_boto, mock_mount_ip, mock_sl):
        from app.services.storage_pool_service import _poll_fsx_until_available

        mock_fsx = MagicMock()
        mock_fsx.describe_file_systems.return_value = {
            "FileSystems": [
                {
                    "Lifecycle": "AVAILABLE",
                    "DNSName": "fs-abc.fsx.us-east-1.amazonaws.com",
                }
            ]
        }
        mock_boto.return_value = mock_fsx
        mock_mount_ip.return_value = "10.0.1.99"

        mock_pool = MagicMock()
        mock_db = MagicMock()
        mock_db.get.return_value = mock_pool
        mock_sl.return_value = mock_db

        _poll_fsx_until_available("pool-1", {}, "us-east-1", "fs-abc")

        assert mock_pool.status == "available"
        assert mock_pool.fsx_dns_name == "fs-abc.fsx.us-east-1.amazonaws.com"
        assert mock_pool.fsx_mount_ip == "10.0.1.99"
        mock_db.commit.assert_called()
        mock_db.close.assert_called()

    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service._boto_client")
    @patch("time.sleep", return_value=None)
    def test_sets_error_on_failed_status(self, _sleep, mock_boto, mock_sl):
        from app.services.storage_pool_service import _poll_fsx_until_available

        mock_fsx = MagicMock()
        mock_fsx.describe_file_systems.return_value = {
            "FileSystems": [{"Lifecycle": "FAILED"}]
        }
        mock_boto.return_value = mock_fsx

        mock_pool = MagicMock()
        mock_db = MagicMock()
        mock_db.get.return_value = mock_pool
        mock_sl.return_value = mock_db

        _poll_fsx_until_available("pool-1", {}, "us-east-1", "fs-abc")

        assert mock_pool.status == "error"
        mock_db.close.assert_called()

    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service._boto_client")
    @patch("time.sleep", return_value=None)
    def test_returns_early_when_pool_not_found(self, _sleep, mock_boto, mock_sl):
        from app.services.storage_pool_service import _poll_fsx_until_available

        mock_fsx = MagicMock()
        mock_fsx.describe_file_systems.return_value = {
            "FileSystems": [{"Lifecycle": "AVAILABLE", "DNSName": "x"}]
        }
        mock_boto.return_value = mock_fsx

        mock_db = MagicMock()
        mock_db.get.return_value = None  # pool not found
        mock_sl.return_value = mock_db

        _poll_fsx_until_available("pool-gone", {}, "us-east-1", "fs-abc")
        mock_db.commit.assert_not_called()
        mock_db.close.assert_called()


# ---------------------------------------------------------------------------
# storage_pools API helpers
# ---------------------------------------------------------------------------

from app.api.storage_pools import (
    _apply_auto_extend_fields,
    _validate_azure_files,
    _validate_byo,
    _validate_ceph_nfs,
    _validate_fsx,
    _validate_netapp,
    _validate_pool_mode,
)


class TestValidateFsx:
    def test_raises_when_no_az(self):
        body = MagicMock(az=None, fsx_throughput_mbps=128, fsx_storage_gb=64)
        with pytest.raises(HTTPException) as exc:
            _validate_fsx(body)
        assert exc.value.status_code == 400
        assert "AZ" in str(exc.value.detail)

    def test_raises_when_no_throughput(self):
        body = MagicMock(az="us-east-1a", fsx_throughput_mbps=None, fsx_storage_gb=64)
        with pytest.raises(HTTPException) as exc:
            _validate_fsx(body)
        assert exc.value.status_code == 400

    def test_passes_with_valid_fields(self):
        body = MagicMock(az="us-east-1a", fsx_throughput_mbps=128, fsx_storage_gb=128)
        _validate_fsx(body)  # should not raise


class TestValidateByo:
    def test_raises_when_no_nfs_endpoint(self):
        body = MagicMock(nfs_endpoint=None)
        with pytest.raises(HTTPException) as exc:
            _validate_byo(body)
        assert exc.value.status_code == 400

    def test_passes_with_endpoint(self):
        body = MagicMock(nfs_endpoint="10.0.1.5:/vol")
        _validate_byo(body)  # should not raise


class TestValidateCephNfs:
    def test_raises_when_not_ocpvirt(self):
        provider = MagicMock(type="ec2")
        with pytest.raises(HTTPException) as exc:
            _validate_ceph_nfs(provider)
        assert exc.value.status_code == 400
        assert "OCP Virt" in str(exc.value.detail)

    def test_passes_for_ocpvirt(self):
        provider = MagicMock(type="ocpvirt")
        _validate_ceph_nfs(provider)  # should not raise


class TestValidateNetapp:
    def test_raises_when_not_gcp(self):
        body = MagicMock(netapp_capacity_gb=256)
        provider = MagicMock(type="ec2")
        with pytest.raises(HTTPException) as exc:
            _validate_netapp(body, provider)
        assert exc.value.status_code == 400

    def test_raises_when_no_capacity(self):
        body = MagicMock(netapp_capacity_gb=None)
        provider = MagicMock(type="gcp")
        with pytest.raises(HTTPException) as exc:
            _validate_netapp(body, provider)
        assert exc.value.status_code == 400

    def test_passes_with_valid(self):
        body = MagicMock(netapp_capacity_gb=256)
        provider = MagicMock(type="gcp")
        _validate_netapp(body, provider)


class TestValidateAzureFiles:
    def test_raises_when_not_azure(self):
        body = MagicMock(azure_files_capacity_gb=100)
        provider = MagicMock(type="gcp")
        with pytest.raises(HTTPException) as exc:
            _validate_azure_files(body, provider)
        assert exc.value.status_code == 400

    def test_raises_when_no_capacity(self):
        body = MagicMock(azure_files_capacity_gb=None)
        provider = MagicMock(type="azure")
        with pytest.raises(HTTPException) as exc:
            _validate_azure_files(body, provider)
        assert exc.value.status_code == 400

    def test_passes_with_valid(self):
        body = MagicMock(azure_files_capacity_gb=100)
        provider = MagicMock(type="azure")
        _validate_azure_files(body, provider)


class TestValidatePoolMode:
    def test_dispatches_to_fsx_validator(self):
        body = MagicMock(mode="shared-fsx", az=None)
        provider = MagicMock()
        with pytest.raises(HTTPException):
            _validate_pool_mode(body, provider)

    def test_noop_for_local_mode(self):
        body = MagicMock(mode="local")
        provider = MagicMock()
        _validate_pool_mode(body, provider)  # should not raise

    def test_dispatches_to_byo_validator(self):
        body = MagicMock(mode="shared-byo", nfs_endpoint=None)
        provider = MagicMock()
        with pytest.raises(HTTPException):
            _validate_pool_mode(body, provider)


class TestApplyAutoExtendFields:
    def test_copies_all_fields_when_set(self):
        pool = MagicMock()
        body = MagicMock(
            auto_extend_enabled=True,
            auto_extend_threshold_pct=85,
            auto_extend_increment_gb=50,
            auto_extend_max_gb=1000,
            pb_auto_sleep_minutes=30,
        )
        _apply_auto_extend_fields(pool, body)
        assert pool.auto_extend_enabled is True
        assert pool.auto_extend_threshold_pct == 85
        assert pool.auto_extend_increment_gb == 50
        assert pool.auto_extend_max_gb == 1000
        assert pool.pb_auto_sleep_minutes == 30

    def test_skips_none_values(self):
        pool = MagicMock()
        pool.auto_extend_enabled = False
        pool.auto_extend_threshold_pct = 90
        body = MagicMock(
            auto_extend_enabled=None,
            auto_extend_threshold_pct=None,
            auto_extend_increment_gb=None,
            auto_extend_max_gb=None,
            pb_auto_sleep_minutes=None,
        )
        _apply_auto_extend_fields(pool, body)
        # Values should not have been overwritten
        assert pool.auto_extend_enabled is False
        assert pool.auto_extend_threshold_pct == 90


# ---------------------------------------------------------------------------
# central_library helpers
# ---------------------------------------------------------------------------

from app.services.central_library import (
    _create_pattern_record,
    _load_manifest,
    _remap_library_refs,
    _scan_bucket,
    _scan_s3_pattern_groups,
)


class TestScanS3PatternGroups:
    def test_groups_objects_by_pattern_id(self):
        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "patterns/pid1/metadata.json", "Size": 200},
                    {"Key": "patterns/pid1/disk-a.qcow2", "Size": 5000},
                    {"Key": "patterns/pid2/disk-b.qcow2", "Size": 8000},
                ]
            }
        ]
        mock_s3.get_paginator.return_value = paginator
        mock_s3.get_object.return_value = {
            "Body": io.BytesIO(json.dumps({"name": "test-pattern"}).encode())
        }

        groups = _scan_s3_pattern_groups(mock_s3, "my-bucket")

        assert "pid1" in groups
        assert "pid2" in groups
        assert groups["pid1"]["metadata"] == {"name": "test-pattern"}
        assert len(groups["pid1"]["files"]) == 1
        assert groups["pid1"]["files"][0]["key"] == "patterns/pid1/disk-a.qcow2"
        assert groups["pid2"]["metadata"] is None
        assert len(groups["pid2"]["files"]) == 1

    def test_skips_short_keys(self):
        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "patterns/", "Size": 0}]}
        ]
        mock_s3.get_paginator.return_value = paginator

        groups = _scan_s3_pattern_groups(mock_s3, "bucket")
        assert groups == {}

    def test_handles_metadata_read_error(self):
        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "patterns/pid1/metadata.json", "Size": 100}]}
        ]
        mock_s3.get_paginator.return_value = paginator
        mock_s3.get_object.side_effect = Exception("S3 error")

        groups = _scan_s3_pattern_groups(mock_s3, "bucket")
        assert groups["pid1"]["metadata"] is None

    def test_empty_pages(self):
        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{}]
        mock_s3.get_paginator.return_value = paginator

        groups = _scan_s3_pattern_groups(mock_s3, "bucket")
        assert groups == {}


class TestScanBucket:
    def test_infers_metadata_from_keys(self):
        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "library/rhel-9-base.qcow2", "Size": 1_000_000},
                    {"Key": "library/ubuntu-22.iso", "Size": 2_000_000},
                    {"Key": "library/windows.vmdk", "Size": 3_000_000},
                ]
            }
        ]
        mock_s3.get_paginator.return_value = paginator

        items = _scan_bucket(mock_s3, "bucket")

        assert len(items) == 3
        assert items[0]["name"] == "Rhel 9 Base"
        assert items[0]["format"] == "qcow2"
        assert items[0]["type"] == "image"
        assert items[1]["format"] == "iso"
        assert items[1]["type"] == "iso"
        assert items[2]["format"] == "vmdk"

    def test_skips_manifest_and_patterns(self):
        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "library/manifest.json", "Size": 500},
                    {"Key": "patterns/pid/disk.qcow2", "Size": 1000},
                    {"Key": "images/", "Size": 0},
                    {"Key": "library/real-image.raw", "Size": 9000},
                ]
            }
        ]
        mock_s3.get_paginator.return_value = paginator

        items = _scan_bucket(mock_s3, "bucket")

        assert len(items) == 1
        assert items[0]["s3_key"] == "library/real-image.raw"
        assert items[0]["format"] == "raw"

    def test_unknown_extension_defaults_to_qcow2(self):
        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "library/some-disk.bin", "Size": 500}]}
        ]
        mock_s3.get_paginator.return_value = paginator

        items = _scan_bucket(mock_s3, "bucket")
        assert items[0]["format"] == "qcow2"


class TestLoadManifest:
    def test_returns_manifest_when_valid_json_list(self):
        mock_s3 = MagicMock()
        manifest_data = [{"s3_key": "library/img.qcow2", "name": "Image"}]
        mock_s3.get_object.return_value = {
            "Body": io.BytesIO(json.dumps(manifest_data).encode())
        }
        result = _load_manifest(mock_s3, "bucket")
        assert result == manifest_data

    def test_falls_back_to_scan_on_error(self):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = Exception("NoSuchKey")
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "library/fallback.qcow2", "Size": 100}]}
        ]
        mock_s3.get_paginator.return_value = paginator

        result = _load_manifest(mock_s3, "bucket")
        assert len(result) == 1
        assert result[0]["s3_key"] == "library/fallback.qcow2"

    def test_falls_back_when_manifest_is_not_list(self):
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": io.BytesIO(json.dumps({"not": "a list"}).encode())
        }
        paginator = MagicMock()
        paginator.paginate.return_value = [{"Contents": []}]
        mock_s3.get_paginator.return_value = paginator

        result = _load_manifest(mock_s3, "bucket")
        assert result == []


class TestRemapLibraryRefs:
    def test_remaps_by_name_match(self):
        mock_db = MagicMock()
        mock_item = MagicMock()
        mock_item.id = "local-item-id"
        mock_item.name = "RHEL 9"
        mock_item.format = "qcow2"
        mock_item.size_bytes = 5000
        mock_item.source = "local"

        mock_db.query.return_value.filter.return_value.all.return_value = [mock_item]
        # filter_by(id=...).first() returns None (item not found locally)
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {
                        "libraryItemId": "remote-uuid",
                        "libraryItemName": "rhel 9",
                        "format": "qcow2",
                        "sizeBytes": 5000,
                    },
                }
            ],
            "edges": [],
        }

        _remap_library_refs(topology, mock_db)

        assert topology["nodes"][0]["data"]["libraryItemId"] == "local-item-id"
        assert topology["nodes"][0]["data"]["libraryItemName"] == "RHEL 9"

    def test_skips_pattern_source_nodes(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []

        topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "data": {
                        "libraryItemId": "some-id",
                        "source": "pattern",
                    },
                }
            ],
            "edges": [],
        }
        _remap_library_refs(topology, mock_db)
        # Should not modify since source is "pattern"
        assert topology["nodes"][0]["data"]["libraryItemId"] == "some-id"

    def test_skips_non_storage_nodes(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []

        topology = {
            "nodes": [{"type": "vmNode", "data": {"libraryItemId": "x"}}],
            "edges": [],
        }
        _remap_library_refs(topology, mock_db)
        assert topology["nodes"][0]["data"]["libraryItemId"] == "x"


class TestCreatePatternRecord:
    @patch("app.services.central_library._remap_library_refs")
    def test_creates_pattern_and_disks(self, mock_remap):
        mock_db = MagicMock()

        meta = {
            "name": "My Pattern",
            "description": "A test pattern",
            "topology": {"nodes": [], "edges": []},
            "total_size_bytes": 12345,
            "tags": {"env": "dev"},
            "disks": [
                {
                    "id": "disk-1",
                    "source_disk_id": "sd-1",
                    "source_vm_id": "vm-1",
                    "s3_key": "patterns/pid/disk-1.qcow2",
                    "format": "qcow2",
                    "size_bytes": 5000,
                    "virtual_size_bytes": 10000,
                    "checksum_sha256": "abc123",
                }
            ],
        }

        _create_pattern_record(mock_db, "pid-001", meta, "owner-1", "prov-1")

        # Pattern added
        assert mock_db.add.call_count == 3  # Pattern + PatternDisk + PatternLocation
        mock_db.flush.assert_called_once()

        pattern_arg = mock_db.add.call_args_list[0][0][0]
        assert pattern_arg.id == "pid-001"
        assert pattern_arg.name == "My Pattern"
        assert pattern_arg.state == "available"
        assert pattern_arg.visibility == "public"
        assert pattern_arg.tags["source"] == "central"
        assert pattern_arg.tags["source_provider_id"] == "prov-1"
        assert pattern_arg.tags["env"] == "dev"

        disk_arg = mock_db.add.call_args_list[1][0][0]
        assert disk_arg.id == "disk-1"
        assert disk_arg.pattern_id == "pid-001"
        assert disk_arg.s3_key == "patterns/pid/disk-1.qcow2"

        loc_arg = mock_db.add.call_args_list[2][0][0]
        assert loc_arg.pattern_disk_id == "disk-1"
        assert loc_arg.location_type == "central"
        assert loc_arg.state == "synced"
        assert loc_arg.provider_id is None

    @patch("app.services.central_library._remap_library_refs")
    def test_defaults_when_meta_fields_missing(self, mock_remap):
        mock_db = MagicMock()

        meta = {"disks": [{"s3_key": "patterns/pid2/d.qcow2"}]}

        _create_pattern_record(mock_db, "pid-002", meta, None, "prov-x")

        pattern_arg = mock_db.add.call_args_list[0][0][0]
        assert pattern_arg.name == "pattern-pid-002"
        assert pattern_arg.owner_id == "system"
        assert pattern_arg.total_size_bytes == 0
        assert pattern_arg.tags["source"] == "central"

        disk_arg = mock_db.add.call_args_list[1][0][0]
        assert disk_arg.source_disk_id == ""
        assert disk_arg.format == "qcow2"
        assert disk_arg.size_bytes == 0


# ── _pool_response tests ──


class TestPoolResponse:
    def test_basic_response_no_worker(self):
        from app.api.storage_pools import _pool_response

        pool = MagicMock()
        pool.worker_host_id = None
        pool.worker_instance_type = None
        pool.id = "pool-1"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.count.return_value = 3

        with patch("app.api.storage_pools.StoragePoolResponse") as MockResp:
            resp = MagicMock()
            MockResp.model_validate.return_value = resp
            result = _pool_response(pool, mock_db)
            assert result.host_count == 3

    def test_response_with_connected_worker(self):
        from app.api.storage_pools import _pool_response

        pool = MagicMock()
        pool.worker_host_id = "host-1"
        pool.worker_instance_type = "i4i.xlarge"
        pool.id = "pool-1"

        worker = MagicMock()
        worker.ip_address = "1.2.3.4"
        worker.private_ip = "10.0.0.1"
        worker.instance_id = "i-abc"
        worker.agent_version = "v1"
        worker.agent_status = "connected"
        worker.state = "active"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.count.return_value = 2
        mock_db.query.return_value.filter_by.return_value.first.return_value = worker

        with patch("app.api.storage_pools.StoragePoolResponse") as MockResp:
            resp = MagicMock()
            MockResp.model_validate.return_value = resp
            result = _pool_response(pool, mock_db)
            assert result.worker_status == "connected"
            assert result.worker_ip == "1.2.3.4"

    def test_response_with_installing_worker(self):
        from app.api.storage_pools import _pool_response

        pool = MagicMock()
        pool.worker_host_id = "host-1"
        pool.worker_instance_type = "i4i.xlarge"
        pool.id = "pool-1"

        worker = MagicMock()
        worker.ip_address = "1.2.3.4"
        worker.private_ip = "10.0.0.1"
        worker.instance_id = "i-abc"
        worker.agent_version = "v1"
        worker.agent_status = "disconnected"
        worker.state = "active"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.count.return_value = 0
        mock_db.query.return_value.filter_by.return_value.first.return_value = worker

        with patch("app.api.storage_pools.StoragePoolResponse") as MockResp:
            resp = MagicMock()
            MockResp.model_validate.return_value = resp
            result = _pool_response(pool, mock_db)
            assert result.worker_status == "installing"

    def test_response_with_missing_worker(self):
        from app.api.storage_pools import _pool_response

        pool = MagicMock()
        pool.worker_host_id = "host-gone"
        pool.worker_instance_type = "i4i.xlarge"
        pool.id = "pool-1"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.count.return_value = 0
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        with patch("app.api.storage_pools.StoragePoolResponse") as MockResp:
            resp = MagicMock()
            MockResp.model_validate.return_value = resp
            result = _pool_response(pool, mock_db)
            assert result.worker_status == "error"

    def test_response_provisioning_worker(self):
        from app.api.storage_pools import _pool_response

        pool = MagicMock()
        pool.worker_host_id = None
        pool.worker_instance_type = "i4i.xlarge"
        pool.id = "pool-1"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.count.return_value = 0

        with patch("app.api.storage_pools.StoragePoolResponse") as MockResp:
            resp = MagicMock()
            MockResp.model_validate.return_value = resp
            with patch(
                "app.services.pattern_buffer_service.is_provisioning",
                return_value=True,
            ):
                result = _pool_response(pool, mock_db)
                assert result.worker_status == "provisioning"


# ── _get_or_create_central_library tests ──


class TestGetOrCreateCentralLibrary:
    def test_returns_existing_library(self):
        from app.services.central_library import _get_or_create_central_library

        mock_lib = MagicMock()
        mock_lib.id = "lib-central"
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_lib

        result = _get_or_create_central_library(mock_db)
        assert result.id == "lib-central"
        mock_db.add.assert_not_called()

    def test_creates_new_central_library(self):
        from app.services.central_library import _get_or_create_central_library

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        _get_or_create_central_library(mock_db)
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()
        mock_db.refresh.assert_called_once()


# ── sync_central_library tests ──


class TestSyncCentralLibrary:
    @patch("app.services.s3_storage._get_readonly_s3_config", return_value=None)
    def test_returns_error_when_no_config(self, mock_cfg):
        from app.services.central_library import sync_central_library

        mock_db = MagicMock()
        result = sync_central_library(mock_db)
        assert "error" in result

    @patch(
        "app.services.central_library.sync_central_patterns",
        return_value={"created": 0, "skipped": 0},
    )
    @patch("app.services.central_library._load_manifest")
    @patch("app.services.s3_storage._get_readonly_s3_client")
    @patch("app.services.s3_storage._get_readonly_s3_config")
    def test_creates_new_items(
        self, mock_cfg, mock_client, mock_manifest, mock_patterns
    ):
        from app.services.central_library import sync_central_library

        mock_cfg.return_value = {"bucket": "test-bucket", "provider_id": "prov-1"}
        mock_client.return_value = MagicMock()
        mock_manifest.return_value = [
            {
                "s3_key": "library/item1.qcow2",
                "name": "RHEL 9",
                "size_bytes": 1000,
                "format": "qcow2",
            }
        ]

        mock_lib = MagicMock()
        mock_lib.id = "lib-central"

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_lib
        # For existing items query
        mock_db.query.return_value.filter_by.return_value.__iter__ = lambda s: iter([])
        mock_db.query.return_value.filter.return_value.all.return_value = []

        result = sync_central_library(mock_db)
        assert result["created"] == 1

    @patch(
        "app.services.central_library.sync_central_patterns",
        return_value={"created": 0, "skipped": 0},
    )
    @patch("app.services.central_library._load_manifest")
    @patch("app.services.s3_storage._get_readonly_s3_client")
    @patch("app.services.s3_storage._get_readonly_s3_config")
    def test_skips_duplicate_fingerprints(
        self, mock_cfg, mock_client, mock_manifest, mock_patterns
    ):
        from app.services.central_library import sync_central_library

        mock_cfg.return_value = {"bucket": "test-bucket", "provider_id": "prov-1"}
        mock_client.return_value = MagicMock()
        mock_manifest.return_value = [
            {
                "s3_key": "library/item1.qcow2",
                "name": "RHEL 9",
                "size_bytes": 1000,
                "format": "qcow2",
            }
        ]

        mock_lib = MagicMock()
        mock_lib.id = "lib-central"

        # Create a local item with same fingerprint
        local_item = MagicMock()
        local_item.size_bytes = 1000
        local_item.format = "qcow2"

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_lib
        mock_db.query.return_value.filter_by.return_value.__iter__ = lambda s: iter([])
        mock_db.query.return_value.filter.return_value.all.return_value = [local_item]

        result = sync_central_library(mock_db)
        assert result["skipped"] == 1


# ── sync_central_patterns tests ──


class TestSyncCentralPatterns:
    @patch("app.services.s3_storage._get_readonly_s3_config", return_value=None)
    def test_returns_error_when_no_config(self, mock_cfg):
        from app.services.central_library import sync_central_patterns

        mock_db = MagicMock()
        result = sync_central_patterns(mock_db)
        assert "error" in result

    @patch("app.services.central_library._scan_s3_pattern_groups")
    @patch("app.services.central_library._create_pattern_record")
    def test_creates_new_patterns(self, mock_create, mock_scan):
        from app.services.central_library import sync_central_patterns

        mock_scan.return_value = {
            "pat-1": {"metadata": {"name": "test-pattern"}, "disks": []}
        }

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        cfg = {"bucket": "test", "provider_id": "prov-1"}
        client = MagicMock()

        result = sync_central_patterns(mock_db, client=client, cfg=cfg)
        assert result["created"] == 1
        mock_create.assert_called_once()

    @patch("app.services.central_library._scan_s3_pattern_groups")
    def test_skips_existing_patterns(self, mock_scan):
        from app.services.central_library import sync_central_patterns

        mock_scan.return_value = {
            "pat-existing": {"metadata": {"name": "test"}, "disks": []}
        }

        existing_pattern = MagicMock()
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = (
            existing_pattern
        )

        cfg = {"bucket": "test", "provider_id": "prov-1"}
        client = MagicMock()

        result = sync_central_patterns(mock_db, client=client, cfg=cfg)
        assert result["skipped"] == 1

    @patch("app.services.central_library._scan_s3_pattern_groups")
    def test_skips_patterns_without_metadata(self, mock_scan):
        from app.services.central_library import sync_central_patterns

        mock_scan.return_value = {"pat-no-meta": {"metadata": None, "disks": []}}

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        cfg = {"bucket": "test", "provider_id": "prov-1"}
        client = MagicMock()

        result = sync_central_patterns(mock_db, client=client, cfg=cfg)
        assert result["skipped"] == 1


# ---------------------------------------------------------------------------
# Storage Pool CRUD API endpoint tests (via TestClient)
# ---------------------------------------------------------------------------

import uuid

from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.host import Host
from app.models.provider import Provider
from app.models.storage_pool import StoragePool
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
_sp_client = TestClient(app)

# ---------------------------------------------------------------------------
# Module-level fixtures for pool API tests
# ---------------------------------------------------------------------------
_sp_db = TestSession()
_sp_admin = User(
    email="sp-admin@example.com",
    display_name="SP Admin",
    role="admin",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_sp_db.add(_sp_admin)
_sp_db.commit()
_sp_db.refresh(_sp_admin)
_SP_ADMIN_ID = _sp_admin.id
_SP_ADMIN_TOKEN = create_jwt(
    user_id=_sp_admin.id, email=_sp_admin.email, role=_sp_admin.role
)
SP_ADMIN_HEADERS = {"Authorization": f"Bearer {_SP_ADMIN_TOKEN}"}

_sp_user = User(
    email="sp-user@example.com",
    display_name="SP User",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_sp_db.add(_sp_user)
_sp_db.commit()
_sp_db.refresh(_sp_user)
_SP_USER_TOKEN = create_jwt(
    user_id=_sp_user.id, email=_sp_user.email, role=_sp_user.role
)
SP_USER_HEADERS = {"Authorization": f"Bearer {_SP_USER_TOKEN}"}
_sp_db.close()


def _sp_create_provider(name=None):
    db = TestSession()
    prov = Provider(
        id=str(uuid.uuid4()),
        name=name or f"sp-prov-{uuid.uuid4().hex[:6]}",
        type="ec2",
        credentials=json.dumps({"access_key_id": "f", "secret_access_key": "f"}),
        default_region="us-east-1",
        state="active",
    )
    db.add(prov)
    db.commit()
    db.refresh(prov)
    pid = prov.id
    db.close()
    return pid


def _sp_create_pool(provider_id, name=None, mode="local"):
    db = TestSession()
    pool = StoragePool(
        id=str(uuid.uuid4()),
        name=name or f"pool-{uuid.uuid4().hex[:6]}",
        mode=mode,
        status="available",
        provider_id=provider_id,
    )
    db.add(pool)
    db.commit()
    db.refresh(pool)
    pool_id = pool.id
    db.close()
    return pool_id


# ===================================================================
# GET /storage-pools/ (list)
# ===================================================================


class TestListPools:
    def test_list_pools_returns_200(self):
        resp = _sp_client.get("/api/v1/storage-pools/", headers=SP_ADMIN_HEADERS)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_list_pools_includes_created_pool(self):
        prov_id = _sp_create_provider()
        pool_id = _sp_create_pool(prov_id, name=f"list-test-{uuid.uuid4().hex[:6]}")
        resp = _sp_client.get("/api/v1/storage-pools/", headers=SP_ADMIN_HEADERS)
        assert resp.status_code == 200
        ids = [p["id"] for p in resp.json()]
        assert pool_id in ids

    def test_list_pools_requires_admin(self):
        resp = _sp_client.get("/api/v1/storage-pools/", headers=SP_USER_HEADERS)
        assert resp.status_code == 403


# ===================================================================
# GET /storage-pools/{id} (single pool)
# ===================================================================


class TestGetPool:
    def test_get_pool_success(self):
        prov_id = _sp_create_provider()
        pool_id = _sp_create_pool(prov_id, name=f"get-test-{uuid.uuid4().hex[:6]}")
        resp = _sp_client.get(
            f"/api/v1/storage-pools/{pool_id}", headers=SP_ADMIN_HEADERS
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == pool_id
        assert resp.json()["mode"] == "local"

    def test_get_pool_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = _sp_client.get(
            f"/api/v1/storage-pools/{fake_id}", headers=SP_ADMIN_HEADERS
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_get_pool_has_host_count(self):
        """Response includes a host_count field."""
        prov_id = _sp_create_provider()
        pool_id = _sp_create_pool(prov_id, name=f"hc-test-{uuid.uuid4().hex[:6]}")
        resp = _sp_client.get(
            f"/api/v1/storage-pools/{pool_id}", headers=SP_ADMIN_HEADERS
        )
        assert resp.status_code == 200
        assert "host_count" in resp.json()
        assert resp.json()["host_count"] == 0

    def test_get_pool_requires_admin(self):
        prov_id = _sp_create_provider()
        pool_id = _sp_create_pool(prov_id)
        resp = _sp_client.get(
            f"/api/v1/storage-pools/{pool_id}", headers=SP_USER_HEADERS
        )
        assert resp.status_code == 403


# ===================================================================
# POST /storage-pools/ (create)
# ===================================================================


class TestCreatePool:
    def test_create_local_pool(self):
        prov_id = _sp_create_provider()
        pool_name = f"new-local-{uuid.uuid4().hex[:6]}"
        resp = _sp_client.post(
            "/api/v1/storage-pools/",
            json={"name": pool_name, "mode": "local", "provider_id": prov_id},
            headers=SP_ADMIN_HEADERS,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == pool_name
        assert data["mode"] == "local"
        assert data["status"] == "available"

    def test_create_pool_provider_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = _sp_client.post(
            "/api/v1/storage-pools/",
            json={"name": "orphan-pool", "mode": "local", "provider_id": fake_id},
            headers=SP_ADMIN_HEADERS,
        )
        assert resp.status_code == 404
        assert "Provider" in resp.json()["detail"]

    def test_create_pool_duplicate_name(self):
        prov_id = _sp_create_provider()
        pool_name = f"dup-{uuid.uuid4().hex[:6]}"
        _sp_create_pool(prov_id, name=pool_name)
        resp = _sp_client.post(
            "/api/v1/storage-pools/",
            json={"name": pool_name, "mode": "local", "provider_id": prov_id},
            headers=SP_ADMIN_HEADERS,
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    def test_create_pool_invalid_mode(self):
        prov_id = _sp_create_provider()
        resp = _sp_client.post(
            "/api/v1/storage-pools/",
            json={
                "name": f"bad-mode-{uuid.uuid4().hex[:6]}",
                "mode": "totally-invalid",
                "provider_id": prov_id,
            },
            headers=SP_ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "Invalid mode" in resp.json()["detail"]

    def test_create_pool_requires_admin(self):
        resp = _sp_client.post(
            "/api/v1/storage-pools/",
            json={"name": "nope", "mode": "local", "provider_id": "x"},
            headers=SP_USER_HEADERS,
        )
        assert resp.status_code == 403


# ===================================================================
# DELETE /storage-pools/{id}
# ===================================================================


class TestDeletePool:
    def test_delete_empty_pool(self):
        prov_id = _sp_create_provider()
        pool_id = _sp_create_pool(prov_id, name=f"del-test-{uuid.uuid4().hex[:6]}")
        resp = _sp_client.delete(
            f"/api/v1/storage-pools/{pool_id}", headers=SP_ADMIN_HEADERS
        )
        assert resp.status_code == 204

        # Verify pool is gone
        db = TestSession()
        assert db.get(StoragePool, pool_id) is None
        db.close()

    def test_delete_pool_with_hosts_rejected(self):
        prov_id = _sp_create_provider()
        pool_id = _sp_create_pool(prov_id, name=f"del-hosts-{uuid.uuid4().hex[:6]}")
        # Add a host to the pool
        db = TestSession()
        h = Host(
            id=str(uuid.uuid4()),
            state="running",
            host_type="shared",
            storage_pool_id=pool_id,
        )
        db.add(h)
        db.commit()
        db.close()

        resp = _sp_client.delete(
            f"/api/v1/storage-pools/{pool_id}", headers=SP_ADMIN_HEADERS
        )
        assert resp.status_code == 400
        assert "hosts assigned" in resp.json()["detail"]

    def test_delete_pool_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = _sp_client.delete(
            f"/api/v1/storage-pools/{fake_id}", headers=SP_ADMIN_HEADERS
        )
        assert resp.status_code == 404

    def test_delete_pool_requires_admin(self):
        prov_id = _sp_create_provider()
        pool_id = _sp_create_pool(prov_id)
        resp = _sp_client.delete(
            f"/api/v1/storage-pools/{pool_id}", headers=SP_USER_HEADERS
        )
        assert resp.status_code == 403


# ===================================================================
# PATCH /storage-pools/{id} (update)
# ===================================================================


class TestUpdatePool:
    def test_update_auto_extend_fields(self):
        prov_id = _sp_create_provider()
        pool_id = _sp_create_pool(prov_id, name=f"upd-test-{uuid.uuid4().hex[:6]}")
        resp = _sp_client.patch(
            f"/api/v1/storage-pools/{pool_id}",
            json={"auto_extend_enabled": True, "auto_extend_threshold_pct": 85},
            headers=SP_ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["auto_extend_enabled"] is True
        assert data["auto_extend_threshold_pct"] == 85

    def test_update_pool_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = _sp_client.patch(
            f"/api/v1/storage-pools/{fake_id}",
            json={"auto_extend_enabled": True},
            headers=SP_ADMIN_HEADERS,
        )
        assert resp.status_code == 404

    def test_update_pool_requires_admin(self):
        prov_id = _sp_create_provider()
        pool_id = _sp_create_pool(prov_id)
        resp = _sp_client.patch(
            f"/api/v1/storage-pools/{pool_id}",
            json={"auto_extend_enabled": True},
            headers=SP_USER_HEADERS,
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# _pool_response — additional edge cases (lines 40-65)
# ---------------------------------------------------------------------------


class TestPoolResponseEdgeCases:
    """Additional edge cases for _pool_response() covering uncovered branches."""

    def test_worker_state_not_active(self):
        """When worker exists, agent_status != connected, and state != active,
        worker_status should be set to the worker's state value (line 49)."""
        from app.api.storage_pools import _pool_response

        pool = MagicMock()
        pool.worker_host_id = "host-1"
        pool.worker_instance_type = "i4i.xlarge"
        pool.id = "pool-1"

        worker = MagicMock()
        worker.ip_address = "1.2.3.4"
        worker.private_ip = "10.0.0.1"
        worker.instance_id = "i-abc"
        worker.agent_version = "v1"
        worker.agent_status = "disconnected"
        worker.state = "provisioning"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.count.return_value = 0
        mock_db.query.return_value.filter_by.return_value.first.return_value = worker

        with patch("app.api.storage_pools.StoragePoolResponse") as MockResp:
            resp = MagicMock()
            MockResp.model_validate.return_value = resp
            result = _pool_response(pool, mock_db)
            assert result.worker_status == "provisioning"

    def test_worker_state_terminated(self):
        """Worker with state 'terminated' should pass through to worker_status."""
        from app.api.storage_pools import _pool_response

        pool = MagicMock()
        pool.worker_host_id = "host-1"
        pool.worker_instance_type = "i4i.xlarge"
        pool.id = "pool-1"

        worker = MagicMock()
        worker.ip_address = "5.6.7.8"
        worker.private_ip = "10.0.0.2"
        worker.instance_id = "i-def"
        worker.agent_version = "v2"
        worker.agent_status = "disconnected"
        worker.state = "terminated"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.count.return_value = 1
        mock_db.query.return_value.filter_by.return_value.first.return_value = worker

        with patch("app.api.storage_pools.StoragePoolResponse") as MockResp:
            resp = MagicMock()
            MockResp.model_validate.return_value = resp
            result = _pool_response(pool, mock_db)
            assert result.worker_status == "terminated"

    def test_no_worker_provision_error(self):
        """When no worker_host_id but worker_instance_type set, and provisioning
        is done but an error exists, worker_status should be 'error' with message
        (lines 60-64)."""
        from app.api.storage_pools import _pool_response

        pool = MagicMock()
        pool.worker_host_id = None
        pool.worker_instance_type = "i4i.xlarge"
        pool.id = "pool-err"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.count.return_value = 0

        with patch("app.api.storage_pools.StoragePoolResponse") as MockResp:
            resp = MagicMock()
            MockResp.model_validate.return_value = resp
            with patch(
                "app.services.pattern_buffer_service.is_provisioning",
                return_value=False,
            ), patch(
                "app.services.pattern_buffer_service.get_provision_error",
                return_value="Instance launch failed: capacity exceeded",
            ):
                result = _pool_response(pool, mock_db)
                assert result.worker_status == "error"
                assert (
                    result.worker_error == "Instance launch failed: capacity exceeded"
                )

    def test_no_worker_no_provisioning_no_error(self):
        """When no worker_host_id, worker_instance_type set, not provisioning,
        and no error, worker_status should remain unset (lines 60-64 else branch)."""
        from app.api.storage_pools import _pool_response

        pool = MagicMock()
        pool.worker_host_id = None
        pool.worker_instance_type = "i4i.xlarge"
        pool.id = "pool-idle"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.count.return_value = 0

        with patch("app.api.storage_pools.StoragePoolResponse") as MockResp:
            resp = MagicMock()
            resp.worker_status = None
            resp.worker_error = None
            MockResp.model_validate.return_value = resp
            with patch(
                "app.services.pattern_buffer_service.is_provisioning",
                return_value=False,
            ), patch(
                "app.services.pattern_buffer_service.get_provision_error",
                return_value=None,
            ):
                result = _pool_response(pool, mock_db)
                # Neither provisioning nor error — worker_status left untouched
                assert result.worker_status is None

    def test_no_worker_no_instance_type(self):
        """When no worker_host_id and no worker_instance_type, neither branch
        is entered — worker_status stays at default (None)."""
        from app.api.storage_pools import _pool_response

        pool = MagicMock()
        pool.worker_host_id = None
        pool.worker_instance_type = None
        pool.id = "pool-plain"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.count.return_value = 5

        with patch("app.api.storage_pools.StoragePoolResponse") as MockResp:
            resp = MagicMock()
            resp.worker_status = None
            MockResp.model_validate.return_value = resp
            result = _pool_response(pool, mock_db)
            assert result.host_count == 5
            assert result.worker_status is None

    def test_host_count_excludes_pattern_buffer(self):
        """host_count query filters out host_type='pattern_buffer'."""
        from app.api.storage_pools import _pool_response

        pool = MagicMock()
        pool.worker_host_id = None
        pool.worker_instance_type = None
        pool.id = "pool-count"

        mock_db = MagicMock()
        # Count returns 3 (only non-pattern_buffer hosts)
        mock_db.query.return_value.filter.return_value.count.return_value = 3

        with patch("app.api.storage_pools.StoragePoolResponse") as MockResp:
            resp = MagicMock()
            MockResp.model_validate.return_value = resp
            result = _pool_response(pool, mock_db)
            assert result.host_count == 3
