"""Tests for uncovered paths in pattern_buffer_service.py.

Covers:
  - _find_ec2_provider
  - provision_pattern_buffer_async
  - _resolve_instance_type
  - _resolve_nfs_kwargs
  - _check_pattern_buffer_busy
  - replace_pattern_buffer
  - stop_pattern_buffer
  - _cleanup_failed_instance
"""

from unittest.mock import MagicMock, patch

from app.services.pattern_buffer_service import (
    _check_pattern_buffer_busy,
    _cleanup_failed_instance,
    _find_ec2_provider,
    _resolve_instance_type,
    _resolve_nfs_kwargs,
    provision_pattern_buffer_async,
    replace_pattern_buffer,
    stop_pattern_buffer,
)

# ═══════════════════════════════════════════════════════════════════════════
# _find_ec2_provider
# ═══════════════════════════════════════════════════════════════════════════


class TestFindEc2Provider:
    def test_finds_via_pool_provider_id(self):
        db = MagicMock()
        pool = MagicMock()
        pool.provider_id = "prov-1"
        pool.id = "pool-1"
        expected = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = expected
        result = _find_ec2_provider(db, pool)
        assert result == expected

    def test_falls_back_to_host_provider(self):
        db = MagicMock()
        pool = MagicMock()
        pool.provider_id = None
        pool.id = "pool-1"
        host = MagicMock()
        host.provider_id = "prov-2"
        expected_prov = MagicMock()
        # First call: query(Provider) for pool.provider_id → None (skipped since provider_id is None)
        # Then query(Host) → host
        # Then query(Provider) for host.provider_id → expected_prov
        db.query.return_value.filter.return_value.first.return_value = host
        db.query.return_value.filter_by.return_value.first.return_value = expected_prov
        result = _find_ec2_provider(db, pool)
        assert result == expected_prov

    def test_no_provider_found(self):
        db = MagicMock()
        pool = MagicMock()
        pool.provider_id = None
        pool.id = "pool-1"
        db.query.return_value.filter.return_value.first.return_value = None
        result = _find_ec2_provider(db, pool)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# provision_pattern_buffer_async
# ═══════════════════════════════════════════════════════════════════════════


class TestProvisionPatternBufferAsync:
    @patch("app.core.redis.enqueue_job")
    def test_enqueues_job(self, mock_enqueue):
        provision_pattern_buffer_async("pool-abc")
        mock_enqueue.assert_called_once()
        args = mock_enqueue.call_args
        assert args[0][1] == "pool-abc"
        assert args[1]["queue_name"] == "host_lifecycle"


# ═══════════════════════════════════════════════════════════════════════════
# _resolve_instance_type
# ═══════════════════════════════════════════════════════════════════════════


class TestResolveInstanceType:
    def test_pool_override(self):
        pool = MagicMock()
        pool.worker_instance_type = "m5.xlarge"
        provider = MagicMock()
        assert _resolve_instance_type(pool, provider) == "m5.xlarge"

    def test_gcp_default(self):
        pool = MagicMock()
        pool.worker_instance_type = None
        provider = MagicMock()
        provider.type = "gcp"
        assert _resolve_instance_type(pool, provider) == "e2-standard-2"

    def test_azure_default(self):
        pool = MagicMock()
        pool.worker_instance_type = None
        provider = MagicMock()
        provider.type = "azure"
        assert _resolve_instance_type(pool, provider) == "Standard_E2s_v5"

    def test_ec2_default(self):
        pool = MagicMock()
        pool.worker_instance_type = None
        provider = MagicMock()
        provider.type = "ec2"
        result = _resolve_instance_type(pool, provider)
        assert "i4i" in result


# ═══════════════════════════════════════════════════════════════════════════
# _resolve_nfs_kwargs
# ═══════════════════════════════════════════════════════════════════════════


class TestResolveNfsKwargs:
    def test_fsx(self):
        pool = MagicMock()
        pool.mode = "shared-fsx"
        pool.fsx_dns_name = "fs-12345.fsx.us-east-1.amazonaws.com"
        result = _resolve_nfs_kwargs(pool)
        assert result["nfs_server"] == pool.fsx_dns_name
        assert result["nfs_path"] == "/fsx"

    def test_netapp(self):
        pool = MagicMock()
        pool.mode = "shared-netapp"
        pool.netapp_mount_ip = "10.0.0.1"
        pool.netapp_volume_name = "vol1"
        result = _resolve_nfs_kwargs(pool)
        assert result["nfs_server"] == "10.0.0.1"
        assert result["nfs_path"] == "/vol1"

    def test_azure_files(self):
        pool = MagicMock()
        pool.mode = "shared-azure-files"
        pool.azure_file_share_url = "10.0.0.2:/share"
        result = _resolve_nfs_kwargs(pool)
        assert result["nfs_server"] == "10.0.0.2"
        assert result["nfs_path"] == "/share"

    def test_byo_with_port(self):
        pool = MagicMock()
        pool.mode = "shared-byo"
        pool.nfs_endpoint = "nfs.example.com:/data"
        pool.nfs_port = 2049
        result = _resolve_nfs_kwargs(pool)
        assert result["nfs_server"] == "nfs.example.com"
        assert result["nfs_path"] == "/data"
        assert result["nfs_port"] == 2049

    def test_ceph_nfs(self):
        pool = MagicMock()
        pool.mode = "shared-ceph-nfs"
        pool.nfs_endpoint = "ceph-gw:/export"
        pool.nfs_port = None
        result = _resolve_nfs_kwargs(pool)
        assert result["nfs_server"] == "ceph-gw"
        assert "nfs_port" not in result

    def test_local_returns_empty(self):
        pool = MagicMock()
        pool.mode = "local"
        result = _resolve_nfs_kwargs(pool)
        assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# _check_pattern_buffer_busy
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckPatternBufferBusy:
    def test_no_worker_host(self):
        pool = MagicMock()
        pool.worker_host_id = None
        db = MagicMock()
        assert _check_pattern_buffer_busy(db, pool) is None

    def test_host_disconnected(self):
        pool = MagicMock()
        pool.worker_host_id = "host-1"
        db = MagicMock()
        host = MagicMock()
        host.agent_status = "disconnected"
        db.query.return_value.filter_by.return_value.first.return_value = host
        assert _check_pattern_buffer_busy(db, pool) is None

    @patch("app.services.troshkad_client.check_health")
    def test_running_jobs(self, mock_health):
        mock_health.return_value = {"running_jobs": 3}
        pool = MagicMock()
        pool.worker_host_id = "host-1"
        db = MagicMock()
        host = MagicMock()
        host.agent_status = "connected"
        db.query.return_value.filter_by.return_value.first.return_value = host
        result = _check_pattern_buffer_busy(db, pool)
        assert "3 active" in result

    @patch("app.services.troshkad_client.check_health")
    def test_capture_in_progress(self, mock_health):
        mock_health.return_value = {"running_jobs": 0}
        pool = MagicMock()
        pool.worker_host_id = "host-1"
        db = MagicMock()
        host = MagicMock()
        host.agent_status = "connected"
        capturing_pattern = MagicMock()
        capturing_pattern.id = "pat-1234-abcd-0000-000000000000"

        def query_side_effect(model):
            q = MagicMock()
            if model.__name__ == "Pattern":
                q.filter.return_value.first.return_value = capturing_pattern
            else:
                q.filter_by.return_value.first.return_value = host
            return q

        db.query.side_effect = query_side_effect
        result = _check_pattern_buffer_busy(db, pool)
        assert "capture" in result.lower()


# ═══════════════════════════════════════════════════════════════════════════
# replace_pattern_buffer
# ═══════════════════════════════════════════════════════════════════════════


class TestReplacePatternBuffer:
    @patch("app.services.pattern_buffer_service.provision_pattern_buffer_async")
    @patch(
        "app.services.pattern_buffer_service._check_pattern_buffer_busy",
        return_value=None,
    )
    def test_replaces_with_old_host(self, mock_busy, mock_provision):
        db = MagicMock()
        pool = MagicMock()
        pool.worker_host_id = "old-host"
        pool.id = "pool-1"
        old_host = MagicMock()
        old_host.instance_id = "i-12345"
        pool.provider = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = old_host

        with patch("app.services.providers.get_provider_driver"):
            replace_pattern_buffer(db, pool)
        assert old_host.state == "terminated"
        assert pool.worker_host_id is None
        mock_provision.assert_called_once()

    @patch("app.services.pattern_buffer_service._check_pattern_buffer_busy")
    def test_raises_when_busy(self, mock_busy):
        mock_busy.return_value = "2 active jobs"
        db = MagicMock()
        pool = MagicMock()
        try:
            replace_pattern_buffer(db, pool)
            assert False, "Should have raised"
        except RuntimeError as e:
            assert "cannot replace" in str(e).lower()


# ═══════════════════════════════════════════════════════════════════════════
# stop_pattern_buffer
# ═══════════════════════════════════════════════════════════════════════════


class TestStopPatternBuffer:
    @patch(
        "app.services.pattern_buffer_service._check_pattern_buffer_busy",
        return_value=None,
    )
    @patch("app.services.providers.get_provider_driver")
    def test_stops_non_ec2(self, mock_drv_fn, mock_busy):
        db = MagicMock()
        pool = MagicMock()
        pool.worker_host_id = "host-1"
        pool.provider = MagicMock()
        pool.provider.type = "gcp"
        host = MagicMock()
        host.instance_id = "gcp-inst-1"
        host.id = "host-aabb"
        db.query.return_value.filter_by.return_value.first.return_value = host
        stop_pattern_buffer(db, pool)
        assert host.state == "stopped"
        assert host.agent_status == "disconnected"
        db.commit.assert_called()

    @patch("app.services.pattern_buffer_service._check_pattern_buffer_busy")
    def test_raises_when_busy(self, mock_busy):
        mock_busy.return_value = "capture in progress"
        db = MagicMock()
        pool = MagicMock()
        try:
            stop_pattern_buffer(db, pool)
            assert False, "Should have raised"
        except RuntimeError:
            pass

    @patch(
        "app.services.pattern_buffer_service._check_pattern_buffer_busy",
        return_value=None,
    )
    def test_no_worker_host(self, mock_busy):
        db = MagicMock()
        pool = MagicMock()
        pool.worker_host_id = None
        stop_pattern_buffer(db, pool)

    @patch(
        "app.services.pattern_buffer_service._check_pattern_buffer_busy",
        return_value=None,
    )
    def test_no_provider(self, mock_busy):
        db = MagicMock()
        pool = MagicMock()
        pool.worker_host_id = "host-1"
        pool.provider = None
        host = MagicMock()
        host.instance_id = "i-1"
        db.query.return_value.filter_by.return_value.first.return_value = host
        stop_pattern_buffer(db, pool)


# ═══════════════════════════════════════════════════════════════════════════
# _cleanup_failed_instance
# ═══════════════════════════════════════════════════════════════════════════


class TestCleanupFailedInstance:
    @patch("app.services.providers.get_provider_driver")
    def test_terminates_instance(self, mock_drv_fn):
        result = {"instance_id": "i-abc123"}
        provider = MagicMock()
        _cleanup_failed_instance(result, provider)
        mock_drv_fn.return_value.terminate_host.assert_called_once_with(
            provider, "i-abc123"
        )

    @patch("app.services.providers.get_provider_driver", side_effect=Exception("err"))
    def test_exception_handled(self, mock_drv_fn):
        result = {"instance_id": "i-abc123"}
        _cleanup_failed_instance(result, MagicMock())

    def test_no_instance_id(self):
        _cleanup_failed_instance({}, MagicMock())
