"""Tests for extracted helper functions in app.api.hosts."""

import os

os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test_host_svc_helpers.db"

import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# _store_agent_credentials
# ---------------------------------------------------------------------------


class TestStoreAgentCredentials(unittest.TestCase):
    def _call(self, h, result):
        from app.api.hosts import _store_agent_credentials

        return _store_agent_credentials(h, result)

    def test_stores_token_and_fingerprint(self):
        h = MagicMock()
        h.id = "host-12345678"
        result = {
            "success": True,
            "troshkad_credentials": {
                "token": "tok-abc123",
                "fingerprint": "SHA256:xyz",
            },
        }

        self._call(h, result)

        assert h.agent_token == "tok-abc123"
        assert h.agent_cert_fingerprint == "SHA256:xyz"

    def test_credentials_key_fallback(self):
        h = MagicMock()
        h.id = "host-12345678"
        result = {
            "success": True,
            "credentials": {"token": "tok-fallback", "fingerprint": "fp-2"},
        }

        self._call(h, result)

        assert h.agent_token == "tok-fallback"
        assert h.agent_cert_fingerprint == "fp-2"

    def test_troshkad_credentials_takes_priority(self):
        h = MagicMock()
        h.id = "host-12345678"
        result = {
            "success": True,
            "troshkad_credentials": {"token": "tok-primary"},
            "credentials": {"token": "tok-fallback"},
        }

        self._call(h, result)

        assert h.agent_token == "tok-primary"

    def test_no_token_skips_storage(self):
        h = MagicMock()
        h.id = "host-12345678"
        sentinel_token = object()
        h.agent_token = sentinel_token
        result = {
            "success": True,
            "troshkad_credentials": {"fingerprint": "fp-only"},
        }

        self._call(h, result)

        # agent_token should NOT have been reassigned
        assert h.agent_token is sentinel_token

    def test_failed_result_skips_entirely(self):
        h = MagicMock()
        h.id = "host-12345678"
        sentinel_token = object()
        h.agent_token = sentinel_token
        result = {
            "success": False,
            "troshkad_credentials": {"token": "should-not-store"},
        }

        self._call(h, result)

        assert h.agent_token is sentinel_token

    def test_missing_success_key_skips(self):
        h = MagicMock()
        h.id = "host-12345678"
        sentinel_token = object()
        h.agent_token = sentinel_token
        result = {"troshkad_credentials": {"token": "nope"}}

        self._call(h, result)

        assert h.agent_token is sentinel_token

    def test_empty_credentials_dict_skips(self):
        h = MagicMock()
        h.id = "host-12345678"
        sentinel_token = object()
        h.agent_token = sentinel_token
        result = {"success": True}

        self._call(h, result)

        # No credentials dict at all -> creds = {}, no token -> skip
        assert h.agent_token is sentinel_token

    def test_fingerprint_defaults_to_empty_string(self):
        h = MagicMock()
        h.id = "host-12345678"
        result = {
            "success": True,
            "troshkad_credentials": {"token": "tok-nofp"},
        }

        self._call(h, result)

        assert h.agent_token == "tok-nofp"
        assert h.agent_cert_fingerprint == ""


# ---------------------------------------------------------------------------
# _get_ceph_storage
# ---------------------------------------------------------------------------


class TestGetCephStorage(unittest.TestCase):
    def _call(self, db, host):
        from app.api.hosts import _get_ceph_storage

        return _get_ceph_storage(db, host)

    def test_no_provider_returns_none(self):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        host = MagicMock()
        host.provider_id = "prov-1"

        result = self._call(db, host)
        assert result is None

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_no_toolbox_pod_returns_none(self, mock_k8s):
        db = MagicMock()
        provider = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = provider

        core_api = MagicMock()
        toolbox_pods = MagicMock()
        toolbox_pods.items = []
        core_api.list_namespaced_pod.return_value = toolbox_pods
        mock_k8s.return_value = (MagicMock(), core_api, MagicMock())

        host = MagicMock()
        host.provider_id = "prov-1"

        result = self._call(db, host)
        assert result is None

    @patch("kubernetes.stream.stream")
    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_successful_query(self, mock_k8s, mock_stream):
        import json

        db = MagicMock()
        provider = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = provider

        core_api = MagicMock()
        pod = MagicMock()
        pod.metadata.name = "rook-ceph-tools-abc"
        toolbox_pods = MagicMock()
        toolbox_pods.items = [pod]
        core_api.list_namespaced_pod.return_value = toolbox_pods
        mock_k8s.return_value = (MagicMock(), core_api, MagicMock())

        ceph_data = {
            "stats": {
                "total_bytes": 1000000000000,  # ~931 GB
                "total_used_bytes": 500000000000,  # ~465 GB
            }
        }
        resp = MagicMock()
        resp.is_open.side_effect = [True, False]
        resp.peek_stdout.return_value = True
        resp.read_stdout.return_value = json.dumps(ceph_data)
        resp.peek_stderr.return_value = False
        mock_stream.return_value = resp

        host = MagicMock()
        host.provider_id = "prov-1"

        result = self._call(db, host)

        assert result is not None
        assert result["used_pct"] == 50.0
        assert result["total_gb"] == round(1000000000000 / (1024**3), 1)
        assert result["free_gb"] == round(500000000000 / (1024**3), 1)


# ---------------------------------------------------------------------------
# _build_storage_mode_kwargs
# ---------------------------------------------------------------------------


class TestBuildStorageModeKwargs(unittest.TestCase):
    def _call(self, h, s, nfs_kwargs):
        from app.api.hosts import _build_storage_mode_kwargs

        return _build_storage_mode_kwargs(h, s, nfs_kwargs)

    def test_local_mode_when_no_nfs(self):
        h = MagicMock()
        s = MagicMock()
        result = self._call(h, s, {})

        assert result == ("local", "", "", "")

    def test_local_mode_when_nfs_server_empty(self):
        h = MagicMock()
        s = MagicMock()
        result = self._call(h, s, {"nfs_server": ""})

        assert result == ("local", "", "", "")

    def test_shared_but_no_storage_pool_id(self):
        h = MagicMock()
        h.storage_pool_id = None
        h.ip_address = "10.0.0.1"
        s = MagicMock()

        mode, ca, cert, key = self._call(h, s, {"nfs_server": "10.0.1.1"})

        assert mode == "shared"
        assert ca == ""
        assert cert == ""
        assert key == ""

    def test_shared_but_no_ip_address(self):
        h = MagicMock()
        h.storage_pool_id = "pool-1"
        h.ip_address = None
        s = MagicMock()

        mode, ca, cert, key = self._call(h, s, {"nfs_server": "10.0.1.1"})

        assert mode == "shared"
        assert ca == ""

    @patch("app.services.storage_pool_service.sign_host_cert")
    def test_shared_pool_no_ca_cert(self, mock_sign):
        h = MagicMock()
        h.storage_pool_id = "pool-1"
        h.ip_address = "10.0.0.5"
        s = MagicMock()

        pool = MagicMock()
        pool.ca_cert = None
        pool.ca_key = "some-key"
        s.query.return_value.filter_by.return_value.first.return_value = pool

        mode, ca, cert, key = self._call(h, s, {"nfs_server": "10.0.1.1"})

        assert mode == "shared"
        assert ca == ""
        mock_sign.assert_not_called()

    @patch("app.services.storage_pool_service.sign_host_cert")
    def test_shared_with_full_certs(self, mock_sign):
        mock_sign.return_value = ("host-cert-pem", "host-key-pem")

        h = MagicMock()
        h.storage_pool_id = "pool-1"
        h.ip_address = "10.0.0.5"
        h.private_ip = "172.16.0.5"
        s = MagicMock()

        pool = MagicMock()
        pool.ca_cert = "ca-cert-pem"
        pool.ca_key = "ca-key-pem"
        s.query.return_value.filter_by.return_value.first.return_value = pool

        mode, ca, cert, key = self._call(h, s, {"nfs_server": "10.0.1.1"})

        assert mode == "shared"
        assert ca == "ca-cert-pem"
        assert cert == "host-cert-pem"
        assert key == "host-key-pem"
        mock_sign.assert_called_once_with(
            "ca-cert-pem", "ca-key-pem", "10.0.0.5", "172.16.0.5"
        )

    @patch("app.services.storage_pool_service.sign_host_cert")
    def test_shared_no_private_ip_passes_empty_string(self, mock_sign):
        mock_sign.return_value = ("cert", "key")

        h = MagicMock()
        h.storage_pool_id = "pool-1"
        h.ip_address = "10.0.0.5"
        h.private_ip = None
        s = MagicMock()

        pool = MagicMock()
        pool.ca_cert = "ca"
        pool.ca_key = "ca-key"
        s.query.return_value.filter_by.return_value.first.return_value = pool

        self._call(h, s, {"nfs_server": "10.0.1.1"})

        # private_ip should be passed as "" when None
        mock_sign.assert_called_once_with("ca", "ca-key", "10.0.0.5", "")

    def test_shared_no_pool_in_db(self):
        h = MagicMock()
        h.storage_pool_id = "pool-nonexistent"
        h.ip_address = "10.0.0.5"
        s = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = None

        mode, ca, cert, key = self._call(h, s, {"nfs_server": "10.0.1.1"})

        assert mode == "shared"
        assert ca == ""
        assert cert == ""
        assert key == ""


# ---------------------------------------------------------------------------
# _poll_agent_after_update
# ---------------------------------------------------------------------------


class TestPollAgentAfterUpdate(unittest.TestCase):
    def _call(self, h, s, old_version):
        from app.api.hosts import _poll_agent_after_update

        return _poll_agent_after_update(h, s, old_version)

    @patch("app.api.hosts._push_vncd_update")
    @patch("app.services.troshkad_client.check_health")
    @patch("time.sleep")
    def test_successful_update_with_version_change(
        self, _mock_sleep, mock_health, mock_vncd
    ):
        h = MagicMock()
        h.id = "host-123456789"
        s = MagicMock()

        # Drain: first call returns health (still up), second returns None (down)
        # Comeback: third call returns new health
        mock_health.side_effect = [
            {"version": "old-ver"},
            None,  # agent went down
            {"version": "new-abc123"},  # agent came back
        ]

        result = self._call(h, s, "old-ver")

        assert result is True
        assert h.agent_version == "new-abc123"
        s.commit.assert_called_once()
        mock_vncd.assert_called_once_with(h, s)

    @patch("app.api.hosts._push_vncd_update")
    @patch("app.services.troshkad_client.check_health")
    @patch("time.sleep")
    def test_timeout_returns_false(self, _mock_sleep, mock_health, mock_vncd):
        h = MagicMock()
        h.id = "host-123456789"
        s = MagicMock()

        # Agent never comes back
        mock_health.return_value = None

        result = self._call(h, s, "old-ver")

        assert result is False
        s.commit.assert_not_called()
        mock_vncd.assert_not_called()

    @patch("app.api.hosts._push_vncd_update")
    @patch("app.services.troshkad_client.check_health")
    @patch("time.sleep")
    def test_same_version_still_succeeds(self, _mock_sleep, mock_health, mock_vncd):
        h = MagicMock()
        h.id = "host-123456789"
        s = MagicMock()

        mock_health.side_effect = [
            None,  # agent went down immediately
            {"version": "same-ver"},  # came back with same version
        ]

        result = self._call(h, s, "same-ver")

        assert result is True
        assert h.agent_version == "same-ver"
        s.commit.assert_called_once()
        mock_vncd.assert_called_once()

    @patch("app.api.hosts._push_vncd_update")
    @patch("app.services.troshkad_client.check_health")
    @patch("time.sleep")
    def test_vncd_update_failure_non_fatal(self, _mock_sleep, mock_health, mock_vncd):
        h = MagicMock()
        h.id = "host-123456789"
        s = MagicMock()

        mock_health.side_effect = [None, {"version": "v2"}]
        mock_vncd.side_effect = Exception("vncd push failed")

        result = self._call(h, s, "v1")

        # Should still return True despite vncd failure
        assert result is True
        assert h.agent_version == "v2"


# ---------------------------------------------------------------------------
# _setup_console_dns
# ---------------------------------------------------------------------------


class TestSetupConsoleDns(unittest.TestCase):
    def _call(self, h, s, provider_console_domain):
        from app.api.hosts import _setup_console_dns

        return _setup_console_dns(h, s, provider_console_domain)

    def test_missing_instance_id_returns_early(self):
        h = MagicMock()
        h.instance_id = None
        h.ip_address = "10.0.0.1"
        s = MagicMock()

        self._call(h, s, "console.example.com")

        s.get.assert_not_called()

    def test_missing_ip_address_returns_early(self):
        h = MagicMock()
        h.instance_id = "i-abc123"
        h.ip_address = None
        s = MagicMock()

        self._call(h, s, "console.example.com")

        s.get.assert_not_called()

    def test_missing_domain_returns_early(self):
        h = MagicMock()
        h.instance_id = "i-abc123"
        h.ip_address = "10.0.0.1"
        s = MagicMock()

        self._call(h, s, None)

        s.get.assert_not_called()

    def test_no_provider_returns_early(self):
        h = MagicMock()
        h.instance_id = "i-abc123"
        h.ip_address = "10.0.0.1"
        h.provider_id = "prov-1"
        s = MagicMock()
        s.get.return_value = None

        self._call(h, s, "console.example.com")

        # Should not attempt DNS setup
        assert h.console_domain != "i-abc123.console.example.com"

    @patch("app.services.providers.get_provider_driver")
    @patch(
        "app.services.console_dns.console_domain_for_host",
        return_value="i-abc123.console.example.com",
    )
    def test_successful_dns_setup(self, _mock_domain, mock_driver):
        drv = MagicMock()
        drv.create_console_record.return_value = "i-abc123.console.example.com"
        mock_driver.return_value = drv

        h = MagicMock()
        h.instance_id = "i-abc123"
        h.ip_address = "10.0.0.1"
        h.provider_id = "prov-1"

        prov_obj = MagicMock()
        s = MagicMock()
        s.get.return_value = prov_obj

        self._call(h, s, "console.example.com")

        assert h.console_domain == "i-abc123.console.example.com"
        drv.create_console_record.assert_called_once_with(
            prov_obj, h, "i-abc123.console.example.com", "10.0.0.1"
        )

    @patch("app.services.providers.get_provider_driver")
    @patch(
        "app.services.console_dns.console_domain_for_host",
        return_value="i-abc123.console.example.com",
    )
    def test_driver_returns_none_uses_fqdn(self, _mock_domain, mock_driver):
        drv = MagicMock()
        drv.create_console_record.return_value = None
        mock_driver.return_value = drv

        h = MagicMock()
        h.instance_id = "i-abc123"
        h.ip_address = "10.0.0.1"
        h.provider_id = "prov-1"

        s = MagicMock()
        s.get.return_value = MagicMock()

        self._call(h, s, "console.example.com")

        assert h.console_domain == "i-abc123.console.example.com"

    @patch("app.services.providers.get_provider_driver")
    @patch(
        "app.services.console_dns.console_domain_for_host",
        return_value="i-abc123.console.example.com",
    )
    def test_driver_exception_caught(self, _mock_domain, mock_driver):
        mock_driver.side_effect = Exception("AWS Route53 error")

        h = MagicMock()
        h.instance_id = "i-abc123"
        h.ip_address = "10.0.0.1"
        h.provider_id = "prov-1"
        h.id = "host-12345678"
        sentinel = object()
        h.console_domain = sentinel

        s = MagicMock()
        s.get.return_value = MagicMock()

        # Should not raise
        self._call(h, s, "console.example.com")

        # console_domain should NOT have been set (exception was caught)
        assert h.console_domain is sentinel


if __name__ == "__main__":
    unittest.main()
