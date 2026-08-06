# src/backend/tests/test_storage_extend_coverage.py
"""Extended coverage tests for storage_extend service.

Covers: _on_cooldown, _mark_extended, additional should_extend_host/pool
branches, extend_host_ebs, extend_pool_fsx, extend_pool_netapp,
extend_pool_azure_files.
"""
import os
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test.db"


class TestOnCooldown(unittest.TestCase):
    def setUp(self):
        from app.services.storage_extend import _last_extend

        _last_extend.clear()

    def test_not_on_cooldown_when_never_extended(self):
        from app.services.storage_extend import _on_cooldown

        self.assertFalse(_on_cooldown("host:never-seen"))

    def test_on_cooldown_when_recently_extended(self):
        from app.services.storage_extend import _last_extend, _on_cooldown

        _last_extend["host:recent"] = time.time()
        self.assertTrue(_on_cooldown("host:recent"))

    def test_not_on_cooldown_after_expiry(self):
        from app.services.storage_extend import (
            _COOLDOWN_SECONDS,
            _last_extend,
            _on_cooldown,
        )

        _last_extend["host:old"] = time.time() - _COOLDOWN_SECONDS - 1
        self.assertFalse(_on_cooldown("host:old"))


class TestMarkExtended(unittest.TestCase):
    def setUp(self):
        from app.services.storage_extend import _last_extend

        _last_extend.clear()

    def test_mark_sets_timestamp(self):
        from app.services.storage_extend import _last_extend, _mark_extended

        _mark_extended("pool:abc")
        self.assertIn("pool:abc", _last_extend)
        self.assertAlmostEqual(_last_extend["pool:abc"], time.time(), delta=2)


class TestShouldExtendHostAdditional(unittest.TestCase):
    def setUp(self):
        from app.services.storage_extend import _last_extend

        _last_extend.clear()

    def test_cooldown_active_returns_false(self):
        from app.services.storage_extend import _last_extend, should_extend_host

        host = MagicMock()
        host.id = "cooldown-host"
        host.auto_extend_enabled = True
        host.auto_extend_max_gb = 1000
        host.storage_size_gb = 500
        host.auto_extend_threshold_pct = 80
        host.storage_warnings = [
            {"mount": "/var/lib/troshka", "used_pct": 90.0, "level": "warning"}
        ]
        _last_extend["host:cooldown-host"] = time.time()
        self.assertFalse(should_extend_host(host))

    def test_non_data_mount_warning_returns_false(self):
        from app.services.storage_extend import should_extend_host

        host = MagicMock()
        host.id = "non-data-mount"
        host.auto_extend_enabled = True
        host.auto_extend_max_gb = 1000
        host.storage_size_gb = 500
        host.auto_extend_threshold_pct = 80
        host.storage_warnings = [{"mount": "/", "used_pct": 95.0, "level": "critical"}]
        self.assertFalse(should_extend_host(host))

    def test_multiple_warnings_data_mount_matches(self):
        from app.services.storage_extend import should_extend_host

        host = MagicMock()
        host.id = "multi-warn"
        host.auto_extend_enabled = True
        host.auto_extend_max_gb = 1000
        host.storage_size_gb = 500
        host.auto_extend_threshold_pct = 80
        host.storage_warnings = [
            {"mount": "/", "used_pct": 95.0, "level": "critical"},
            {"mount": "/var/lib/troshka/local", "used_pct": 85.0, "level": "warning"},
        ]
        self.assertTrue(should_extend_host(host))

    def test_data_mount_below_threshold_returns_false(self):
        from app.services.storage_extend import should_extend_host

        host = MagicMock()
        host.id = "below-thresh"
        host.auto_extend_enabled = True
        host.auto_extend_max_gb = 1000
        host.storage_size_gb = 500
        host.auto_extend_threshold_pct = 80
        host.storage_warnings = [
            {"mount": "/var/lib/troshka", "used_pct": 70.0, "level": "info"}
        ]
        self.assertFalse(should_extend_host(host))

    def test_no_max_gb_still_extends(self):
        """auto_extend_max_gb=None means no upper cap check."""
        from app.services.storage_extend import should_extend_host

        host = MagicMock()
        host.id = "no-max"
        host.auto_extend_enabled = True
        host.auto_extend_max_gb = None
        host.storage_size_gb = 9999
        host.auto_extend_threshold_pct = 80
        host.storage_warnings = [
            {"mount": "/var/lib/troshka", "used_pct": 90.0, "level": "warning"}
        ]
        self.assertTrue(should_extend_host(host))

    def test_empty_warnings_returns_false(self):
        from app.services.storage_extend import should_extend_host

        host = MagicMock()
        host.id = "no-warn"
        host.auto_extend_enabled = True
        host.auto_extend_max_gb = 1000
        host.storage_size_gb = 500
        host.auto_extend_threshold_pct = 80
        host.storage_warnings = []
        self.assertFalse(should_extend_host(host))


class TestShouldExtendPoolAdditional(unittest.TestCase):
    def setUp(self):
        from app.services.storage_extend import _last_extend

        _last_extend.clear()

    def test_netapp_mode_extends(self):
        from app.services.storage_extend import should_extend_pool

        pool = MagicMock()
        pool.id = "netapp-pool"
        pool.mode = "shared-netapp"
        pool.auto_extend_enabled = True
        pool.auto_extend_threshold_pct = 80
        pool.auto_extend_max_gb = 2000
        pool.fsx_storage_gb = None
        pool.netapp_capacity_gb = 500
        pool.azure_files_capacity_gb = None
        self.assertTrue(should_extend_pool(pool, current_used_pct=85.0))

    def test_azure_files_mode_extends(self):
        from app.services.storage_extend import should_extend_pool

        pool = MagicMock()
        pool.id = "azure-pool"
        pool.mode = "shared-azure-files"
        pool.auto_extend_enabled = True
        pool.auto_extend_threshold_pct = 80
        pool.auto_extend_max_gb = 2000
        pool.fsx_storage_gb = None
        pool.netapp_capacity_gb = None
        pool.azure_files_capacity_gb = 300
        self.assertTrue(should_extend_pool(pool, current_used_pct=90.0))

    def test_cooldown_active_returns_false(self):
        from app.services.storage_extend import _last_extend, should_extend_pool

        pool = MagicMock()
        pool.id = "cool-pool"
        pool.mode = "shared-fsx"
        pool.auto_extend_enabled = True
        pool.auto_extend_threshold_pct = 80
        pool.auto_extend_max_gb = 1000
        pool.fsx_storage_gb = 256
        pool.netapp_capacity_gb = None
        pool.azure_files_capacity_gb = None
        _last_extend["pool:cool-pool"] = time.time()
        self.assertFalse(should_extend_pool(pool, current_used_pct=90.0))

    def test_at_max_returns_false(self):
        from app.services.storage_extend import should_extend_pool

        pool = MagicMock()
        pool.id = "max-pool"
        pool.mode = "shared-fsx"
        pool.auto_extend_enabled = True
        pool.auto_extend_threshold_pct = 80
        pool.auto_extend_max_gb = 256
        pool.fsx_storage_gb = 256
        pool.netapp_capacity_gb = None
        pool.azure_files_capacity_gb = None
        self.assertFalse(should_extend_pool(pool, current_used_pct=90.0))

    def test_below_threshold_returns_false(self):
        from app.services.storage_extend import should_extend_pool

        pool = MagicMock()
        pool.id = "low-pool"
        pool.mode = "shared-fsx"
        pool.auto_extend_enabled = True
        pool.auto_extend_threshold_pct = 80
        pool.auto_extend_max_gb = 1000
        pool.fsx_storage_gb = 256
        pool.netapp_capacity_gb = None
        pool.azure_files_capacity_gb = None
        self.assertFalse(should_extend_pool(pool, current_used_pct=60.0))

    def test_local_mode_returns_false(self):
        from app.services.storage_extend import should_extend_pool

        pool = MagicMock()
        pool.id = "local-pool"
        pool.mode = "local"
        pool.auto_extend_enabled = True
        self.assertFalse(should_extend_pool(pool, current_used_pct=90.0))

    def test_disabled_returns_false(self):
        from app.services.storage_extend import should_extend_pool

        pool = MagicMock()
        pool.id = "disabled-pool"
        pool.mode = "shared-fsx"
        pool.auto_extend_enabled = False
        self.assertFalse(should_extend_pool(pool, current_used_pct=90.0))


class TestExtendHostEbs(unittest.TestCase):
    def setUp(self):
        from app.services.storage_extend import _last_extend

        _last_extend.clear()

    def _make_host(self, **overrides):
        host = MagicMock()
        host.id = "host-uuid-1234"
        host.storage_size_gb = 500
        host.auto_extend_increment_gb = 100
        host.auto_extend_max_gb = 1000
        host.instance_id = "i-abc123"
        host.agent_status = "connected"
        provider = MagicMock()
        provider.get_credentials.return_value = {
            "access_key_id": "x",
            "secret_access_key": "y",
        }
        host.provider = provider
        for k, v in overrides.items():
            setattr(host, k, v)
        return host

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    @patch("app.services.provisioner._get_ec2_client")
    def test_success_with_explicit_increment(self, mock_ec2_fn, mock_start, mock_wait):
        from app.services.storage_extend import extend_host_ebs

        mock_ec2 = MagicMock()
        mock_ec2_fn.return_value = mock_ec2
        mock_ec2.describe_volumes.return_value = {"Volumes": [{"VolumeId": "vol-123"}]}
        mock_start.return_value = "job-1"

        host = self._make_host()
        db = MagicMock()

        result = extend_host_ebs(host, db, increment_gb=200)

        self.assertEqual(result["old_size_gb"], 500)
        self.assertEqual(result["new_size_gb"], 700)
        self.assertEqual(result["volume_id"], "vol-123")
        mock_ec2.modify_volume.assert_called_once_with(VolumeId="vol-123", Size=700)
        db.commit.assert_called_once()
        mock_start.assert_called_once()
        mock_wait.assert_called_once()

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    @patch("app.services.provisioner._get_ec2_client")
    def test_success_uses_host_increment(self, mock_ec2_fn, mock_start, mock_wait):
        from app.services.storage_extend import extend_host_ebs

        mock_ec2 = MagicMock()
        mock_ec2_fn.return_value = mock_ec2
        mock_ec2.describe_volumes.return_value = {"Volumes": [{"VolumeId": "vol-456"}]}
        mock_start.return_value = "job-2"

        host = self._make_host(auto_extend_increment_gb=50)
        db = MagicMock()

        result = extend_host_ebs(host, db)

        self.assertEqual(result["new_size_gb"], 550)
        mock_ec2.modify_volume.assert_called_once_with(VolumeId="vol-456", Size=550)

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    @patch("app.services.provisioner._get_ec2_client")
    def test_capped_at_max(self, mock_ec2_fn, mock_start, mock_wait):
        from app.services.storage_extend import extend_host_ebs

        mock_ec2 = MagicMock()
        mock_ec2_fn.return_value = mock_ec2
        mock_ec2.describe_volumes.return_value = {"Volumes": [{"VolumeId": "vol-789"}]}
        mock_start.return_value = "job-3"

        host = self._make_host(storage_size_gb=950, auto_extend_max_gb=1000)
        db = MagicMock()

        result = extend_host_ebs(host, db, increment_gb=200)

        self.assertEqual(result["new_size_gb"], 1000)

    def test_already_at_max_raises(self):
        from app.services.storage_extend import extend_host_ebs

        host = self._make_host(storage_size_gb=1000, auto_extend_max_gb=1000)
        db = MagicMock()

        with self.assertRaises(ValueError) as ctx:
            extend_host_ebs(host, db, increment_gb=100)
        self.assertIn("already at max", str(ctx.exception))

    def test_no_provider_raises(self):
        from app.services.storage_extend import extend_host_ebs

        host = self._make_host()
        host.provider = None
        db = MagicMock()

        with self.assertRaises(ValueError) as ctx:
            extend_host_ebs(host, db, increment_gb=100)
        self.assertIn("No provider", str(ctx.exception))

    @patch("app.services.provisioner._get_ec2_client")
    def test_no_data_volume_raises(self, mock_ec2_fn):
        from app.services.storage_extend import extend_host_ebs

        mock_ec2 = MagicMock()
        mock_ec2_fn.return_value = mock_ec2
        mock_ec2.describe_volumes.return_value = {"Volumes": []}

        host = self._make_host()
        db = MagicMock()

        with self.assertRaises(ValueError) as ctx:
            extend_host_ebs(host, db, increment_gb=100)
        self.assertIn("No data volume", str(ctx.exception))

    @patch("app.services.provisioner._get_ec2_client")
    def test_agent_not_connected_skips_resize_job(self, mock_ec2_fn):
        from app.services.storage_extend import extend_host_ebs

        mock_ec2 = MagicMock()
        mock_ec2_fn.return_value = mock_ec2
        mock_ec2.describe_volumes.return_value = {
            "Volumes": [{"VolumeId": "vol-nojob"}]
        }

        host = self._make_host(agent_status="disconnected")
        db = MagicMock()

        result = extend_host_ebs(host, db, increment_gb=100)

        self.assertEqual(result["new_size_gb"], 600)
        mock_ec2.modify_volume.assert_called_once()
        db.commit.assert_called_once()

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    @patch("app.services.provisioner._get_ec2_client")
    def test_marks_cooldown_after_extend(self, mock_ec2_fn, mock_start, mock_wait):
        from app.services.storage_extend import _last_extend, extend_host_ebs

        mock_ec2 = MagicMock()
        mock_ec2_fn.return_value = mock_ec2
        mock_ec2.describe_volumes.return_value = {"Volumes": [{"VolumeId": "vol-cd"}]}
        mock_start.return_value = "job-cd"

        host = self._make_host()
        db = MagicMock()

        extend_host_ebs(host, db, increment_gb=100)

        self.assertIn("host:host-uuid-1234", _last_extend)

    @patch("app.services.provisioner._get_ec2_client")
    def test_no_max_gb_does_not_cap(self, mock_ec2_fn):
        """auto_extend_max_gb=None means no capping."""
        from app.services.storage_extend import extend_host_ebs

        mock_ec2 = MagicMock()
        mock_ec2_fn.return_value = mock_ec2
        mock_ec2.describe_volumes.return_value = {
            "Volumes": [{"VolumeId": "vol-nomax"}]
        }

        host = self._make_host(auto_extend_max_gb=None, agent_status="disconnected")
        db = MagicMock()

        result = extend_host_ebs(host, db, increment_gb=500)

        self.assertEqual(result["new_size_gb"], 1000)


class TestExtendPoolFsx(unittest.TestCase):
    def setUp(self):
        from app.services.storage_extend import _last_extend

        _last_extend.clear()

    def _make_pool(self, **overrides):
        pool = MagicMock()
        pool.id = "fsx-pool-uuid"
        pool.name = "test-fsx-pool"
        pool.fsx_storage_gb = 256
        pool.auto_extend_increment_gb = 64
        pool.auto_extend_max_gb = 1024
        pool.provider_id = "prov-uuid"
        pool.fsx_filesystem_id = "fs-abc123"
        for k, v in overrides.items():
            setattr(pool, k, v)
        return pool

    def _mock_provider(self):
        provider = MagicMock()
        provider.get_credentials.return_value = {"access_key_id": "x"}
        provider.default_region = "us-west-2"
        return provider

    @patch("app.services.storage_pool_service.update_fsx_storage")
    def test_success_path(self, mock_update_fsx):
        from app.services.storage_extend import extend_pool_fsx

        pool = self._make_pool()
        db = MagicMock()
        db.get.return_value = self._mock_provider()

        result = extend_pool_fsx(pool, db, increment_gb=100)

        self.assertEqual(result["old_size_gb"], 256)
        self.assertEqual(result["new_size_gb"], 356)
        self.assertEqual(result["filesystem_id"], "fs-abc123")
        mock_update_fsx.assert_called_once()
        db.commit.assert_called_once()

    @patch("app.services.storage_pool_service.update_fsx_storage")
    def test_uses_pool_increment(self, mock_update_fsx):
        from app.services.storage_extend import extend_pool_fsx

        pool = self._make_pool(auto_extend_increment_gb=128)
        db = MagicMock()
        db.get.return_value = self._mock_provider()

        result = extend_pool_fsx(pool, db)

        self.assertEqual(result["new_size_gb"], 384)

    @patch("app.services.storage_pool_service.update_fsx_storage")
    def test_capped_at_max(self, mock_update_fsx):
        from app.services.storage_extend import extend_pool_fsx

        # Use values where min_grow (ceil(500*1.1)=550) stays below max (600)
        pool = self._make_pool(fsx_storage_gb=500, auto_extend_max_gb=600)
        db = MagicMock()
        db.get.return_value = self._mock_provider()

        result = extend_pool_fsx(pool, db, increment_gb=200)

        self.assertEqual(result["new_size_gb"], 600)

    def test_already_at_max_raises(self):
        from app.services.storage_extend import extend_pool_fsx

        pool = self._make_pool(fsx_storage_gb=1024, auto_extend_max_gb=1024)
        db = MagicMock()

        with self.assertRaises(ValueError) as ctx:
            extend_pool_fsx(pool, db, increment_gb=100)
        self.assertIn("already at max", str(ctx.exception))

    @patch("app.services.storage_pool_service.update_fsx_storage")
    def test_min_grow_kicks_in(self, mock_update_fsx):
        """When increment is small, min 10% growth floor is applied."""
        from app.services.storage_extend import extend_pool_fsx

        pool = self._make_pool(fsx_storage_gb=256, auto_extend_max_gb=2000)
        db = MagicMock()
        db.get.return_value = self._mock_provider()

        # increment of 1 GB would be 257, but min_grow = ceil(256 * 1.1) = 282
        result = extend_pool_fsx(pool, db, increment_gb=1)

        self.assertEqual(result["new_size_gb"], 282)

    def test_no_provider_raises(self):
        from app.services.storage_extend import extend_pool_fsx

        pool = self._make_pool()
        db = MagicMock()
        db.get.return_value = None

        with self.assertRaises(ValueError) as ctx:
            extend_pool_fsx(pool, db, increment_gb=100)
        self.assertIn("No provider", str(ctx.exception))

    @patch("app.services.storage_pool_service.update_fsx_storage")
    def test_fsx_6_hour_cooldown_error(self, mock_update_fsx):
        from app.services.storage_extend import extend_pool_fsx

        mock_update_fsx.side_effect = Exception(
            "Cannot update: 6 hours must elapse since prior storage capacity increase"
        )

        pool = self._make_pool()
        db = MagicMock()
        db.get.return_value = self._mock_provider()

        with self.assertRaises(ValueError) as ctx:
            extend_pool_fsx(pool, db, increment_gb=100)
        self.assertIn("6 hours", str(ctx.exception))

    @patch("app.services.storage_pool_service.update_fsx_storage")
    def test_generic_fsx_error(self, mock_update_fsx):
        from app.services.storage_extend import extend_pool_fsx

        mock_update_fsx.side_effect = Exception("Some unexpected AWS error")

        pool = self._make_pool()
        db = MagicMock()
        db.get.return_value = self._mock_provider()

        with self.assertRaises(ValueError) as ctx:
            extend_pool_fsx(pool, db, increment_gb=100)
        self.assertIn("FSx extend failed", str(ctx.exception))

    @patch("app.services.storage_pool_service.update_fsx_storage")
    def test_marks_cooldown_after_extend(self, mock_update_fsx):
        from app.services.storage_extend import _last_extend, extend_pool_fsx

        pool = self._make_pool()
        db = MagicMock()
        db.get.return_value = self._mock_provider()

        extend_pool_fsx(pool, db, increment_gb=100)

        self.assertIn("pool:fsx-pool-uuid", _last_extend)


class TestExtendPoolNetapp(unittest.TestCase):
    def setUp(self):
        from app.services.storage_extend import _last_extend

        _last_extend.clear()

    def _make_pool(self, **overrides):
        pool = MagicMock()
        pool.id = "netapp-pool-uuid"
        pool.name = "test-netapp-pool"
        pool.netapp_capacity_gb = 500
        pool.auto_extend_increment_gb = 100
        pool.auto_extend_max_gb = 2000
        pool.provider_id = "prov-uuid"
        pool.netapp_pool_id = "netapp-vol-123"
        for k, v in overrides.items():
            setattr(pool, k, v)
        return pool

    def _mock_provider(self):
        provider = MagicMock()
        provider.get_credentials.return_value = {"service_account_json": {}}
        return provider

    @patch("app.services.storage_pool_service.update_netapp_capacity")
    def test_success_path(self, mock_update):
        from app.services.storage_extend import extend_pool_netapp

        pool = self._make_pool()
        db = MagicMock()
        db.get.return_value = self._mock_provider()

        result = extend_pool_netapp(pool, db, increment_gb=200)

        self.assertEqual(result["old_size_gb"], 500)
        self.assertEqual(result["new_size_gb"], 700)
        self.assertEqual(result["netapp_pool_id"], "netapp-vol-123")
        mock_update.assert_called_once()
        db.commit.assert_called_once()

    @patch("app.services.storage_pool_service.update_netapp_capacity")
    def test_uses_pool_increment(self, mock_update):
        from app.services.storage_extend import extend_pool_netapp

        pool = self._make_pool(auto_extend_increment_gb=50)
        db = MagicMock()
        db.get.return_value = self._mock_provider()

        result = extend_pool_netapp(pool, db)

        self.assertEqual(result["new_size_gb"], 550)

    @patch("app.services.storage_pool_service.update_netapp_capacity")
    def test_capped_at_max(self, mock_update):
        from app.services.storage_extend import extend_pool_netapp

        pool = self._make_pool(netapp_capacity_gb=1900, auto_extend_max_gb=2000)
        db = MagicMock()
        db.get.return_value = self._mock_provider()

        result = extend_pool_netapp(pool, db, increment_gb=500)

        self.assertEqual(result["new_size_gb"], 2000)

    def test_already_at_max_raises(self):
        from app.services.storage_extend import extend_pool_netapp

        pool = self._make_pool(netapp_capacity_gb=2000, auto_extend_max_gb=2000)
        db = MagicMock()

        with self.assertRaises(ValueError) as ctx:
            extend_pool_netapp(pool, db, increment_gb=100)
        self.assertIn("already at max", str(ctx.exception))

    def test_no_provider_raises(self):
        from app.services.storage_extend import extend_pool_netapp

        pool = self._make_pool()
        db = MagicMock()
        db.get.return_value = None

        with self.assertRaises(ValueError) as ctx:
            extend_pool_netapp(pool, db, increment_gb=100)
        self.assertIn("No provider", str(ctx.exception))

    @patch("app.services.storage_pool_service.update_netapp_capacity")
    def test_marks_cooldown_after_extend(self, mock_update):
        from app.services.storage_extend import _last_extend, extend_pool_netapp

        pool = self._make_pool()
        db = MagicMock()
        db.get.return_value = self._mock_provider()

        extend_pool_netapp(pool, db, increment_gb=100)

        self.assertIn("pool:netapp-pool-uuid", _last_extend)


class TestExtendPoolAzureFiles(unittest.TestCase):
    def setUp(self):
        from app.services.storage_extend import _last_extend

        _last_extend.clear()

    def _make_pool(self, **overrides):
        pool = MagicMock()
        pool.id = "azure-pool-uuid"
        pool.name = "test-azure-pool"
        pool.azure_files_capacity_gb = 300
        pool.auto_extend_increment_gb = 100
        pool.auto_extend_max_gb = 1000
        pool.provider_id = "prov-uuid"
        pool.azure_storage_account = "troshkasa"
        pool.azure_file_share_name = "troshka-share"
        for k, v in overrides.items():
            setattr(pool, k, v)
        return pool

    def _mock_provider(self):
        provider = MagicMock()
        provider.get_credentials.return_value = {
            "tenant_id": "t",
            "client_id": "c",
            "client_secret": "s",
            "subscription_id": "sub",
        }
        provider.azure_resource_group = "troshka-rg"
        return provider

    @patch("app.services.storage_pool_service.update_azure_files_capacity")
    def test_success_path(self, mock_update):
        from app.services.storage_extend import extend_pool_azure_files

        pool = self._make_pool()
        db = MagicMock()
        db.get.return_value = self._mock_provider()

        result = extend_pool_azure_files(pool, db, increment_gb=200)

        self.assertEqual(result["old_size_gb"], 300)
        self.assertEqual(result["new_size_gb"], 500)
        self.assertEqual(result["storage_account"], "troshkasa")
        mock_update.assert_called_once_with(
            {
                "tenant_id": "t",
                "client_id": "c",
                "client_secret": "s",
                "subscription_id": "sub",
            },
            "troshka-rg",
            "troshkasa",
            "troshka-share",
            500,
        )
        db.commit.assert_called_once()

    @patch("app.services.storage_pool_service.update_azure_files_capacity")
    def test_uses_pool_increment(self, mock_update):
        from app.services.storage_extend import extend_pool_azure_files

        pool = self._make_pool(auto_extend_increment_gb=50)
        db = MagicMock()
        db.get.return_value = self._mock_provider()

        result = extend_pool_azure_files(pool, db)

        self.assertEqual(result["new_size_gb"], 350)

    @patch("app.services.storage_pool_service.update_azure_files_capacity")
    def test_capped_at_max(self, mock_update):
        from app.services.storage_extend import extend_pool_azure_files

        pool = self._make_pool(azure_files_capacity_gb=950, auto_extend_max_gb=1000)
        db = MagicMock()
        db.get.return_value = self._mock_provider()

        result = extend_pool_azure_files(pool, db, increment_gb=200)

        self.assertEqual(result["new_size_gb"], 1000)

    def test_already_at_max_raises(self):
        from app.services.storage_extend import extend_pool_azure_files

        pool = self._make_pool(azure_files_capacity_gb=1000, auto_extend_max_gb=1000)
        db = MagicMock()

        with self.assertRaises(ValueError) as ctx:
            extend_pool_azure_files(pool, db, increment_gb=100)
        self.assertIn("already at max", str(ctx.exception))

    def test_no_provider_raises(self):
        from app.services.storage_extend import extend_pool_azure_files

        pool = self._make_pool()
        db = MagicMock()
        db.get.return_value = None

        with self.assertRaises(ValueError) as ctx:
            extend_pool_azure_files(pool, db, increment_gb=100)
        self.assertIn("No provider", str(ctx.exception))

    @patch("app.services.storage_pool_service.update_azure_files_capacity")
    def test_marks_cooldown_after_extend(self, mock_update):
        from app.services.storage_extend import _last_extend, extend_pool_azure_files

        pool = self._make_pool()
        db = MagicMock()
        db.get.return_value = self._mock_provider()

        extend_pool_azure_files(pool, db, increment_gb=100)

        self.assertIn("pool:azure-pool-uuid", _last_extend)


if __name__ == "__main__":
    unittest.main()
