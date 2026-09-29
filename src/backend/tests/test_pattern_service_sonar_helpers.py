"""Unit tests for pattern_service helpers extracted for Sonar S3776."""

from unittest.mock import MagicMock, patch

from app.services.pattern_service import (
    PATTERN_STORED_FORMAT,
    _bastion_ip_and_password,
    _build_direct_disk_params,
    _cluster_operators_all_available,
    _cluster_s3_capture_configs,
    _find_quiesce_bastion,
    _first_content_disk_id,
    _kv_ceph_devices_or_abort,
    _mark_pattern_capture_error,
)


class TestFindQuiesceBastion:
    def test_finds_bastion(self):
        topo = {
            "nodes": [
                {"type": "vmNode", "id": "m1", "data": {"label": "master"}},
                {"type": "vmNode", "id": "b1", "data": {"label": "bastion"}},
            ]
        }
        assert _find_quiesce_bastion(topo)["id"] == "b1"

    def test_missing_bastion(self):
        assert _find_quiesce_bastion({"nodes": []}) is None


class TestBastionIpAndPassword:
    def test_uses_first_nic_ip(self):
        bastion = {
            "data": {
                "nics": [{"ip": ""}, {"ip": "10.1.1.5"}],
                "ciCloudUserPassword": "secret",
            }
        }
        assert _bastion_ip_and_password(bastion) == ("10.1.1.5", "secret")

    def test_default_ip_when_no_nics(self):
        bastion = {"data": {"nics": [], "ciCloudUserPassword": "pw"}}
        assert _bastion_ip_and_password(bastion) == ("10.0.0.50", "pw")


class TestClusterOperatorsAllAvailable:
    def test_none_result(self):
        assert _cluster_operators_all_available(None) is False

    def test_all_good(self):
        assert (
            _cluster_operators_all_available("  12 True False\n   1 True False\n")
            is True
        )

    def test_still_progressing(self):
        assert (
            _cluster_operators_all_available("  11 True False\n   1 False True\n")
            is False
        )


class TestFirstContentDiskId:
    def test_picks_first_non_empty(self):
        disks = [{"node_id": "d1"}, {"node_id": "d2"}, {"node_id": "d3"}]
        sizes = {"d1": 100, "d2": 50_000_000, "d3": 90_000_000}
        assert _first_content_disk_id(disks, sizes) == "d2"

    def test_none_when_all_empty(self):
        disks = [{"node_id": "d1"}]
        assert _first_content_disk_id(disks, {"d1": 100}) is None


class TestClusterS3CaptureConfigs:
    def test_maps_fields(self):
        secret, capture = _cluster_s3_capture_configs(
            {
                "access_key_id": "ak",
                "secret_access_key": "sk",
                "region": "us-east-2",
                "endpoint": "https://s3.example",
                "bucket": "bkt",
            }
        )
        assert secret["access_key_id"] == "ak"
        assert secret["endpoint_url"] == "https://s3.example"
        assert capture["bucket"] == "bkt"
        assert capture["credentialsSecret"] == "s3-credentials"


class TestMarkPatternCaptureError:
    def test_sets_error_and_commits(self):
        db = MagicMock()
        pattern = MagicMock()
        _mark_pattern_capture_error(db, pattern, "pat-abcdef01", "boom")
        assert pattern.state == "error"
        db.commit.assert_called_once()


class TestKvCephDevicesOrAbort:
    def test_no_ceph_returns_empty(self):
        assert _kv_ceph_devices_or_abort(
            False, None, None, "ns", "pid", MagicMock(), MagicMock()
        ) == ([], [])

    @patch("app.services.pattern_service._prepare_ceph_for_kubevirt_capture")
    def test_abort_on_prepare_failure(self, mock_prep):
        mock_prep.return_value = None
        devices, identity = _kv_ceph_devices_or_abort(
            True, MagicMock(), MagicMock(), "ns", "pid", MagicMock(), MagicMock()
        )
        assert devices is None
        assert identity is None

    @patch("app.services.pattern_service._prepare_ceph_for_kubevirt_capture")
    def test_returns_prepared(self, mock_prep):
        mock_prep.return_value = ([{"id": "osd"}], [{"kind": "Secret"}])
        assert _kv_ceph_devices_or_abort(
            True, MagicMock(), MagicMock(), "ns", "pid", MagicMock(), MagicMock()
        ) == ([{"id": "osd"}], [{"kind": "Secret"}])


class TestBuildDirectDiskParams:
    @patch("app.services.deploy_topology._disk_path", return_value="/path/disk.qcow2")
    def test_skips_iso_and_builds_params(self, _mock_path):
        nodes = [
            {"id": "iso1", "data": {"format": "iso", "size": 1}},
            {"id": "disk1", "data": {"format": "qcow2", "size": 50}},
        ]
        params, meta = _build_direct_disk_params(
            "vm1", nodes, "proj", "pat", None, "bucket"
        )
        assert len(params) == 1
        assert params[0]["s3_url"] == (
            f"s3://bucket/patterns/pat/disk1.{PATTERN_STORED_FORMAT}"
        )
        assert meta[0]["disk_id"] == "disk1"
        assert meta[0]["virtual_size_bytes"] == 50 * 1073741824
