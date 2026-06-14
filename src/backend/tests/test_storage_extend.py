# src/backend/tests/test_storage_extend.py
"""Tests for storage auto-extend service."""
import os
import unittest
from unittest.mock import patch, MagicMock

os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test.db"


class TestStorageExtend(unittest.TestCase):

    def setUp(self):
        from app.services.storage_extend import _last_extend
        _last_extend.clear()

    def test_should_extend_host_above_threshold(self):
        from app.services.storage_extend import should_extend_host
        host = MagicMock()
        host.id = "test-host-1234"
        host.auto_extend_enabled = True
        host.auto_extend_threshold_pct = 80
        host.auto_extend_max_gb = 1000
        host.storage_size_gb = 500
        host.storage_warnings = [{"mount": "/var/lib/troshka", "used_pct": 85.0, "level": "warning"}]
        self.assertTrue(should_extend_host(host))

    def test_should_not_extend_host_disabled(self):
        from app.services.storage_extend import should_extend_host
        host = MagicMock()
        host.id = "test-host-1234"
        host.auto_extend_enabled = False
        host.auto_extend_threshold_pct = 80
        host.storage_warnings = [{"mount": "/var/lib/troshka", "used_pct": 85.0, "level": "warning"}]
        self.assertFalse(should_extend_host(host))

    def test_should_not_extend_host_at_max(self):
        from app.services.storage_extend import should_extend_host
        host = MagicMock()
        host.id = "test-host-1234"
        host.auto_extend_enabled = True
        host.auto_extend_threshold_pct = 80
        host.auto_extend_max_gb = 500
        host.storage_size_gb = 500
        host.storage_warnings = [{"mount": "/var/lib/troshka", "used_pct": 85.0, "level": "warning"}]
        self.assertFalse(should_extend_host(host))

    def test_should_extend_pool_above_threshold(self):
        from app.services.storage_extend import should_extend_pool
        pool = MagicMock()
        pool.id = "test-pool-1234"
        pool.auto_extend_enabled = True
        pool.auto_extend_threshold_pct = 80
        pool.auto_extend_max_gb = 1000
        pool.fsx_storage_gb = 256
        pool.mode = "shared-fsx"
        self.assertTrue(should_extend_pool(pool, current_used_pct=85.0))

    def test_should_not_extend_byo_pool(self):
        from app.services.storage_extend import should_extend_pool
        pool = MagicMock()
        pool.id = "test-pool-1234"
        pool.auto_extend_enabled = True
        pool.auto_extend_threshold_pct = 80
        pool.mode = "shared-byo"
        self.assertFalse(should_extend_pool(pool, current_used_pct=90.0))
