# src/backend/tests/test_hosts_helpers.py
"""Tests for extracted helper functions in app.api.hosts."""
import unittest
from unittest.mock import MagicMock, patch


class TestBuildStorageModeKwargs(unittest.TestCase):
    """Tests for _build_storage_mode_kwargs."""

    def _call(self, host, session, nfs_kwargs):
        from app.api.hosts import _build_storage_mode_kwargs

        return _build_storage_mode_kwargs(host, session, nfs_kwargs)

    def test_local_mode_when_no_nfs_server(self):
        """Empty nfs_kwargs -> local storage mode, no TLS certs."""
        host = MagicMock()
        host.storage_pool_id = None
        host.ip_address = "10.0.0.1"
        session = MagicMock()

        storage_mode, ca_cert, host_cert, host_key = self._call(host, session, {})

        self.assertEqual(storage_mode, "local")
        self.assertEqual(ca_cert, "")
        self.assertEqual(host_cert, "")
        self.assertEqual(host_key, "")

    def test_shared_mode_when_nfs_server_present(self):
        """nfs_server in kwargs -> shared mode."""
        host = MagicMock()
        host.storage_pool_id = None
        host.ip_address = "10.0.0.1"
        session = MagicMock()

        storage_mode, ca_cert, host_cert, host_key = self._call(
            host, session, {"nfs_server": "fs-abc.fsx.us-east-1.amazonaws.com"}
        )

        self.assertEqual(storage_mode, "shared")
        # No pool -> no TLS certs even though shared
        self.assertEqual(ca_cert, "")
        self.assertEqual(host_cert, "")
        self.assertEqual(host_key, "")

    @patch("app.services.storage_pool_service.sign_host_cert")
    def test_shared_mode_with_pool_tls(self, mock_sign):
        """Shared mode + pool with CA -> signs host TLS certs."""
        mock_sign.return_value = ("--HOST-CERT--", "--HOST-KEY--")

        pool = MagicMock()
        pool.ca_cert = "--CA-CERT--"
        pool.ca_key = "--CA-KEY--"

        host = MagicMock()
        host.storage_pool_id = "pool-123"
        host.ip_address = "10.0.0.1"
        host.private_ip = "172.16.0.5"

        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = pool

        storage_mode, ca_cert, host_cert, host_key = self._call(
            host, session, {"nfs_server": "10.0.0.50"}
        )

        self.assertEqual(storage_mode, "shared")
        self.assertEqual(ca_cert, "--CA-CERT--")
        self.assertEqual(host_cert, "--HOST-CERT--")
        self.assertEqual(host_key, "--HOST-KEY--")
        mock_sign.assert_called_once_with(
            "--CA-CERT--", "--CA-KEY--", "10.0.0.1", "172.16.0.5"
        )

    def test_shared_mode_pool_without_ca(self):
        """Shared mode + pool without CA cert -> no TLS certs signed."""
        pool = MagicMock()
        pool.ca_cert = None
        pool.ca_key = None

        host = MagicMock()
        host.storage_pool_id = "pool-123"
        host.ip_address = "10.0.0.1"
        host.private_ip = "172.16.0.5"

        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = pool

        storage_mode, ca_cert, host_cert, host_key = self._call(
            host, session, {"nfs_server": "10.0.0.50"}
        )

        self.assertEqual(storage_mode, "shared")
        self.assertEqual(ca_cert, "")
        self.assertEqual(host_cert, "")
        self.assertEqual(host_key, "")

    def test_shared_mode_no_ip_address(self):
        """Shared mode + pool with CA but no IP -> no TLS certs."""
        host = MagicMock()
        host.storage_pool_id = "pool-123"
        host.ip_address = None

        session = MagicMock()

        storage_mode, ca_cert, host_cert, host_key = self._call(
            host, session, {"nfs_server": "10.0.0.50"}
        )

        self.assertEqual(storage_mode, "shared")
        self.assertEqual(ca_cert, "")
        self.assertEqual(host_cert, "")
        self.assertEqual(host_key, "")


class TestBuildPoolInstallKwargs(unittest.TestCase):
    """Tests for _build_pool_install_kwargs."""

    @patch(
        "app.services.agent_ca_service.get_agent_ca_cert", return_value="--AGENT-CA--"
    )
    @patch(
        "app.services.agent_deployer.get_provider_data_disk", return_value="/dev/sdf"
    )
    @patch("app.services.agent_deployer.get_provider_ssh_port", return_value=22)
    @patch("app.services.agent_deployer.get_provider_ssh_user", return_value="ec2-user")
    def test_host_without_pool(self, mock_user, mock_port, mock_disk, mock_ca):
        """Host with no storage_pool_id -> basic kwargs only."""
        from app.api.hosts import _build_pool_install_kwargs

        host = MagicMock()
        host.storage_pool_id = None
        host.console_domain = None

        session = MagicMock()

        result = _build_pool_install_kwargs(host, session, "ec2")

        self.assertEqual(result["ssh_user"], "ec2-user")
        self.assertEqual(result["ssh_port"], 22)
        self.assertEqual(result["data_disk_device"], "/dev/sdf")
        self.assertFalse(result["vncd_no_tls"])
        self.assertEqual(result["agent_ca_cert"], "--AGENT-CA--")
        self.assertNotIn("storage_mode", result)
        self.assertNotIn("nfs_server", result)
        self.assertNotIn("console_domain", result)

    @patch(
        "app.services.agent_ca_service.get_agent_ca_cert", return_value="--AGENT-CA--"
    )
    @patch(
        "app.services.agent_deployer.get_provider_data_disk", return_value="/dev/sdf"
    )
    @patch("app.services.agent_deployer.get_provider_ssh_port", return_value=22)
    @patch("app.services.agent_deployer.get_provider_ssh_user", return_value="ec2-user")
    def test_host_with_console_domain(self, mock_user, mock_port, mock_disk, mock_ca):
        """Host with console_domain -> includes it in kwargs."""
        from app.api.hosts import _build_pool_install_kwargs

        host = MagicMock()
        host.storage_pool_id = None
        host.console_domain = "i-abc123.console.example.com"

        session = MagicMock()

        result = _build_pool_install_kwargs(host, session, "ec2")

        self.assertEqual(result["console_domain"], "i-abc123.console.example.com")

    @patch(
        "app.services.agent_ca_service.get_agent_ca_cert", return_value="--AGENT-CA--"
    )
    @patch(
        "app.services.agent_deployer.get_provider_data_disk", return_value="/dev/sdf"
    )
    @patch("app.services.agent_deployer.get_provider_ssh_port", return_value=22)
    @patch("app.services.agent_deployer.get_provider_ssh_user", return_value="ec2-user")
    def test_host_in_fsx_pool(self, mock_user, mock_port, mock_disk, mock_ca):
        """Host in shared-fsx pool -> shared mode + FSx NFS params."""
        from app.api.hosts import _build_pool_install_kwargs

        pool = MagicMock()
        pool.mode = "shared-fsx"
        pool.fsx_dns_name = "fs-abc.fsx.us-east-1.amazonaws.com"
        pool.nfs_endpoint = None
        pool.nfs_port = None
        pool.ca_cert = None
        pool.ca_key = None

        host = MagicMock()
        host.storage_pool_id = "pool-fsx-1"
        host.console_domain = None
        host.ip_address = "10.0.0.1"
        host.private_ip = "172.16.0.5"

        session = MagicMock()
        session.get.return_value = pool

        result = _build_pool_install_kwargs(host, session, "ec2")

        self.assertEqual(result["storage_mode"], "shared")
        self.assertEqual(result["nfs_server"], "fs-abc.fsx.us-east-1.amazonaws.com")
        self.assertEqual(result["nfs_path"], "/fsx")
        self.assertNotIn("nfs_port", result)

    @patch(
        "app.services.agent_ca_service.get_agent_ca_cert", return_value="--AGENT-CA--"
    )
    @patch(
        "app.services.agent_deployer.get_provider_data_disk", return_value="/dev/sdf"
    )
    @patch("app.services.agent_deployer.get_provider_ssh_port", return_value=22)
    @patch("app.services.agent_deployer.get_provider_ssh_user", return_value="ec2-user")
    def test_host_in_byo_pool_with_port(self, mock_user, mock_port, mock_disk, mock_ca):
        """Host in shared-byo pool with custom NFS port -> includes nfs_port."""
        from app.api.hosts import _build_pool_install_kwargs

        pool = MagicMock()
        pool.mode = "shared-byo"
        pool.fsx_dns_name = None
        pool.nfs_endpoint = "nfs-server.local:/exports/troshka"
        pool.nfs_port = 2049
        pool.ca_cert = None
        pool.ca_key = None

        host = MagicMock()
        host.storage_pool_id = "pool-byo-1"
        host.console_domain = None
        host.ip_address = "10.0.0.2"
        host.private_ip = None

        session = MagicMock()
        session.get.return_value = pool

        result = _build_pool_install_kwargs(host, session, "ec2")

        self.assertEqual(result["storage_mode"], "shared")
        self.assertEqual(result["nfs_server"], "nfs-server.local")
        self.assertEqual(result["nfs_path"], "/exports/troshka")
        self.assertEqual(result["nfs_port"], 2049)

    @patch(
        "app.services.agent_ca_service.get_agent_ca_cert", return_value="--AGENT-CA--"
    )
    @patch(
        "app.services.agent_deployer.get_provider_data_disk", return_value="/dev/sdf"
    )
    @patch("app.services.agent_deployer.get_provider_ssh_port", return_value=22)
    @patch("app.services.agent_deployer.get_provider_ssh_user", return_value="ec2-user")
    def test_host_in_byo_pool_no_path(self, mock_user, mock_port, mock_disk, mock_ca):
        """shared-byo pool with nfs_endpoint lacking path -> defaults to '/'."""
        from app.api.hosts import _build_pool_install_kwargs

        pool = MagicMock()
        pool.mode = "shared-byo"
        pool.fsx_dns_name = None
        pool.nfs_endpoint = "nfs-server.local"
        pool.nfs_port = None
        pool.ca_cert = None
        pool.ca_key = None

        host = MagicMock()
        host.storage_pool_id = "pool-byo-2"
        host.console_domain = None
        host.ip_address = "10.0.0.3"
        host.private_ip = None

        session = MagicMock()
        session.get.return_value = pool

        result = _build_pool_install_kwargs(host, session, "ec2")

        self.assertEqual(result["nfs_server"], "nfs-server.local")
        self.assertEqual(result["nfs_path"], "/")
        self.assertNotIn("nfs_port", result)

    @patch(
        "app.services.agent_ca_service.get_agent_ca_cert", return_value="--AGENT-CA--"
    )
    @patch(
        "app.services.agent_deployer.get_provider_data_disk", return_value="/dev/sdf"
    )
    @patch("app.services.agent_deployer.get_provider_ssh_port", return_value=22)
    @patch("app.services.agent_deployer.get_provider_ssh_user", return_value="ec2-user")
    def test_host_in_ceph_nfs_pool(self, mock_user, mock_port, mock_disk, mock_ca):
        """Host in shared-ceph-nfs pool -> same NFS endpoint parsing as BYO."""
        from app.api.hosts import _build_pool_install_kwargs

        pool = MagicMock()
        pool.mode = "shared-ceph-nfs"
        pool.fsx_dns_name = None
        pool.nfs_endpoint = "ceph-nfs.local:/cephfs"
        pool.nfs_port = None
        pool.ca_cert = None
        pool.ca_key = None

        host = MagicMock()
        host.storage_pool_id = "pool-ceph-1"
        host.console_domain = None
        host.ip_address = "10.0.0.4"
        host.private_ip = None

        session = MagicMock()
        session.get.return_value = pool

        result = _build_pool_install_kwargs(host, session, "ec2")

        self.assertEqual(result["storage_mode"], "shared")
        self.assertEqual(result["nfs_server"], "ceph-nfs.local")
        self.assertEqual(result["nfs_path"], "/cephfs")

    @patch("app.services.storage_pool_service.sign_host_cert")
    @patch(
        "app.services.agent_ca_service.get_agent_ca_cert", return_value="--AGENT-CA--"
    )
    @patch(
        "app.services.agent_deployer.get_provider_data_disk", return_value="/dev/sdf"
    )
    @patch("app.services.agent_deployer.get_provider_ssh_port", return_value=22)
    @patch("app.services.agent_deployer.get_provider_ssh_user", return_value="ec2-user")
    def test_host_in_pool_with_tls(
        self, mock_user, mock_port, mock_disk, mock_ca, mock_sign
    ):
        """Pool with CA cert/key -> includes TLS certs in kwargs."""
        from app.api.hosts import _build_pool_install_kwargs

        mock_sign.return_value = ("--HOST-CERT--", "--HOST-KEY--")

        pool = MagicMock()
        pool.mode = "shared-fsx"
        pool.fsx_dns_name = "fs-abc.fsx.us-east-1.amazonaws.com"
        pool.nfs_endpoint = None
        pool.nfs_port = None
        pool.ca_cert = "--CA-CERT--"
        pool.ca_key = "--CA-KEY--"

        host = MagicMock()
        host.storage_pool_id = "pool-fsx-tls"
        host.console_domain = None
        host.ip_address = "10.0.0.5"
        host.private_ip = "172.16.0.10"

        session = MagicMock()
        session.get.return_value = pool

        result = _build_pool_install_kwargs(host, session, "ec2")

        self.assertEqual(result["ca_cert"], "--CA-CERT--")
        self.assertEqual(result["host_cert"], "--HOST-CERT--")
        self.assertEqual(result["host_key"], "--HOST-KEY--")
        mock_sign.assert_called_once_with(
            "--CA-CERT--", "--CA-KEY--", "10.0.0.5", "172.16.0.10"
        )

    @patch(
        "app.services.agent_ca_service.get_agent_ca_cert", return_value="--AGENT-CA--"
    )
    @patch(
        "app.services.agent_deployer.get_provider_data_disk",
        return_value="/dev/disk/azure/scsi1/lun0",
    )
    @patch("app.services.agent_deployer.get_provider_ssh_port", return_value=22)
    @patch("app.services.agent_deployer.get_provider_ssh_user", return_value="troshka")
    def test_azure_provider_type(self, mock_user, mock_port, mock_disk, mock_ca):
        """Azure provider -> uses azure SSH user and disk device."""
        from app.api.hosts import _build_pool_install_kwargs

        host = MagicMock()
        host.storage_pool_id = None
        host.console_domain = None

        session = MagicMock()

        result = _build_pool_install_kwargs(host, session, "azure")

        self.assertEqual(result["ssh_user"], "troshka")
        self.assertEqual(result["data_disk_device"], "/dev/disk/azure/scsi1/lun0")
        self.assertFalse(result["vncd_no_tls"])

    @patch(
        "app.services.agent_ca_service.get_agent_ca_cert", return_value="--AGENT-CA--"
    )
    @patch(
        "app.services.agent_deployer.get_provider_data_disk", return_value="/dev/sdb"
    )
    @patch("app.services.agent_deployer.get_provider_ssh_port", return_value=22000)
    @patch(
        "app.services.agent_deployer.get_provider_ssh_user", return_value="cloud-user"
    )
    def test_ocpvirt_provider_type(self, mock_user, mock_port, mock_disk, mock_ca):
        """OCP Virt provider -> vncd_no_tls=True, ocpvirt SSH user/port."""
        from app.api.hosts import _build_pool_install_kwargs

        host = MagicMock()
        host.storage_pool_id = None
        host.console_domain = None

        session = MagicMock()

        result = _build_pool_install_kwargs(host, session, "ocpvirt")

        self.assertEqual(result["ssh_user"], "cloud-user")
        self.assertEqual(result["ssh_port"], 22000)
        self.assertTrue(result["vncd_no_tls"])

    @patch(
        "app.services.agent_ca_service.get_agent_ca_cert", return_value="--AGENT-CA--"
    )
    @patch(
        "app.services.agent_deployer.get_provider_data_disk", return_value="/dev/sdf"
    )
    @patch("app.services.agent_deployer.get_provider_ssh_port", return_value=22)
    @patch("app.services.agent_deployer.get_provider_ssh_user", return_value="ec2-user")
    def test_local_pool_mode(self, mock_user, mock_port, mock_disk, mock_ca):
        """Host in local-mode pool -> no shared storage kwargs."""
        from app.api.hosts import _build_pool_install_kwargs

        pool = MagicMock()
        pool.mode = "local"
        pool.fsx_dns_name = None
        pool.nfs_endpoint = None

        host = MagicMock()
        host.storage_pool_id = "pool-local-1"
        host.console_domain = None

        session = MagicMock()
        session.get.return_value = pool

        result = _build_pool_install_kwargs(host, session, "ec2")

        self.assertNotIn("storage_mode", result)
        self.assertNotIn("nfs_server", result)


class TestVerifyAndUpdateAgentVersion(unittest.TestCase):
    """Tests for _verify_and_update_agent_version."""

    @patch("app.services.troshkad_client.check_health")
    @patch("app.services.troshkad_client.push_update")
    @patch("app.services.troshkad_client.troshkad_request")
    @patch("time.sleep")
    def test_version_matches_no_update(
        self, mock_sleep, mock_request, mock_push, mock_health
    ):
        """Agent version matches source hash -> no update pushed."""
        import hashlib

        from app.api.hosts import _verify_and_update_agent_version

        fake_source_bytes = b'VERSION = "dev"\nprint("hello")\n'
        expected_hash = hashlib.sha256(fake_source_bytes).hexdigest()[:12]

        host = MagicMock()
        host.id = "test-host-12345678"
        host.agent_version = None

        mock_request.return_value = {"version": expected_hash}

        # The function opens the file twice: once "rb" (for hash), once default (for text).
        # mock_open with read_data=bytes returns bytes for .read() in both cases,
        # which works for the rb open. The second open().read() is only reached
        # when versions differ, so for the "match" case we only need the rb open.
        with patch(
            "builtins.open", unittest.mock.mock_open(read_data=fake_source_bytes)
        ):
            _verify_and_update_agent_version(host)

        self.assertEqual(host.agent_version, expected_hash)
        mock_push.assert_not_called()

    @patch("app.services.troshkad_client.check_health")
    @patch("app.services.troshkad_client.push_update")
    @patch("app.services.troshkad_client.troshkad_request")
    @patch("time.sleep")
    def test_version_mismatch_triggers_update(
        self, mock_sleep, mock_request, mock_push, mock_health
    ):
        """Agent version differs from source hash -> pushes update."""
        import hashlib

        from app.api.hosts import _verify_and_update_agent_version

        fake_source_text = 'VERSION = "dev"\nprint("hello")\n'
        fake_source_bytes = fake_source_text.encode()
        expected_hash = hashlib.sha256(fake_source_bytes).hexdigest()[:12]

        host = MagicMock()
        host.id = "test-host-12345678"
        host.agent_version = None

        # Agent reports old version
        mock_request.return_value = {"version": "old-version1"}
        # After update, health returns new version
        mock_health.return_value = {"version": expected_hash}

        # The function opens the file twice:
        #   1. open(path, "rb") -> .read() returns bytes (for hash)
        #   2. open(path).read() -> returns str (for text replacement)
        # Use side_effect to return different mocks for each call.
        call_count = {"n": 0}
        original_mock_open = unittest.mock.mock_open

        def multi_open(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call: open(path, "rb")
                return original_mock_open(read_data=fake_source_bytes)(*args, **kwargs)
            else:
                # Second call: open(path) for text
                return original_mock_open(read_data=fake_source_text)(*args, **kwargs)

        with patch("builtins.open", side_effect=multi_open):
            _verify_and_update_agent_version(host)

        # push_update should have been called
        mock_push.assert_called_once()
        call_args = mock_push.call_args
        self.assertEqual(call_args[0][0], host)  # first arg is host
        self.assertEqual(call_args[0][2], expected_hash)  # third arg is version hash
        # The script should have VERSION stamped (not "dev")
        script_bytes = call_args[0][1]
        self.assertIn(expected_hash.encode(), script_bytes)
        self.assertNotIn(b'VERSION = "dev"', script_bytes)

        # Host version should be updated to the new hash after check_health
        self.assertEqual(host.agent_version, expected_hash)

    @patch("app.services.troshkad_client.check_health")
    @patch("app.services.troshkad_client.push_update")
    @patch("app.services.troshkad_client.troshkad_request")
    @patch("time.sleep")
    def test_health_returns_no_version(
        self, mock_sleep, mock_request, mock_push, mock_health
    ):
        """Health endpoint returns no version -> exits early, no update."""
        from app.api.hosts import _verify_and_update_agent_version

        host = MagicMock()
        host.id = "test-host-12345678"
        host.agent_version = None

        mock_request.return_value = {"status": "ok"}  # no "version" key

        _verify_and_update_agent_version(host)

        mock_push.assert_not_called()

    @patch("app.services.troshkad_client.check_health")
    @patch("app.services.troshkad_client.push_update")
    @patch("app.services.troshkad_client.troshkad_request")
    @patch("time.sleep")
    def test_health_returns_none(
        self, mock_sleep, mock_request, mock_push, mock_health
    ):
        """Health endpoint returns None -> exits early, no update."""
        from app.api.hosts import _verify_and_update_agent_version

        host = MagicMock()
        host.id = "test-host-12345678"
        host.agent_version = None

        mock_request.return_value = None

        _verify_and_update_agent_version(host)

        mock_push.assert_not_called()

    @patch("app.services.troshkad_client.check_health")
    @patch("app.services.troshkad_client.push_update")
    @patch("app.services.troshkad_client.troshkad_request")
    @patch("time.sleep")
    def test_exception_is_caught(
        self, mock_sleep, mock_request, mock_push, mock_health
    ):
        """Exceptions during verify -> caught and logged, no crash."""
        from app.api.hosts import _verify_and_update_agent_version

        host = MagicMock()
        host.id = "test-host-12345678"
        host.agent_version = None

        mock_request.side_effect = ConnectionError("Connection refused")

        # Should not raise
        _verify_and_update_agent_version(host)

        mock_push.assert_not_called()


class TestStoreAgentCredentials(unittest.TestCase):
    """Tests for _store_agent_credentials."""

    def _call(self, h, result):
        from app.api.hosts import _store_agent_credentials

        return _store_agent_credentials(h, result)

    def test_success_with_troshkad_credentials(self):
        """Result has troshkad_credentials -> stores token and fingerprint."""
        h = MagicMock()
        result = {
            "success": True,
            "troshkad_credentials": {
                "token": "tok-abc123",
                "fingerprint": "SHA256:xyz",
            },
        }
        self._call(h, result)
        self.assertEqual(h.agent_token, "tok-abc123")
        self.assertEqual(h.agent_cert_fingerprint, "SHA256:xyz")

    def test_success_with_credentials_fallback(self):
        """No troshkad_credentials -> falls back to credentials key."""
        h = MagicMock()
        result = {
            "success": True,
            "credentials": {
                "token": "tok-fallback",
                "fingerprint": "SHA256:fallback",
            },
        }
        self._call(h, result)
        self.assertEqual(h.agent_token, "tok-fallback")
        self.assertEqual(h.agent_cert_fingerprint, "SHA256:fallback")

    def test_no_success_key(self):
        """Result has no success key -> does not store credentials."""
        h = MagicMock()
        h.agent_token = "original"
        result = {
            "troshkad_credentials": {
                "token": "tok-should-not-store",
                "fingerprint": "SHA256:nope",
            }
        }
        self._call(h, result)
        # agent_token should NOT have been reassigned
        self.assertEqual(h.agent_token, "original")

    def test_success_but_no_token_in_creds(self):
        """success=True but creds dict has no token -> does not store."""
        h = MagicMock()
        h.agent_token = "original"
        result = {
            "success": True,
            "troshkad_credentials": {"fingerprint": "SHA256:only"},
        }
        self._call(h, result)
        self.assertEqual(h.agent_token, "original")

    def test_success_with_empty_creds(self):
        """success=True but no credential dicts at all -> does not store."""
        h = MagicMock()
        h.agent_token = "original"
        result = {"success": True}
        self._call(h, result)
        self.assertEqual(h.agent_token, "original")


class TestGetTroshkadStorage(unittest.TestCase):
    """Tests for _get_troshkad_storage."""

    @patch("app.services.troshkad_client.check_disk_usage")
    def test_returns_none_when_no_disk(self, mock_check):
        """check_disk_usage returns None -> returns None."""
        from app.api.hosts import _get_troshkad_storage

        mock_check.return_value = None
        host = MagicMock()
        self.assertIsNone(_get_troshkad_storage(host))

    @patch("app.services.troshkad_client.check_disk_usage")
    def test_returns_none_on_error(self, mock_check):
        """check_disk_usage returns error dict -> returns None."""
        from app.api.hosts import _get_troshkad_storage

        mock_check.return_value = {"error": "connection refused"}
        host = MagicMock()
        self.assertIsNone(_get_troshkad_storage(host))

    @patch("app.services.troshkad_client.check_disk_usage")
    def test_returns_partitions(self, mock_check):
        """check_disk_usage returns partitions list -> wraps in dict."""
        from app.api.hosts import _get_troshkad_storage

        partitions_data = [
            {"mount": "/", "used_pct": 45.2, "free_gb": 100},
            {"mount": "/data", "used_pct": 10.0, "free_gb": 900},
        ]
        mock_check.return_value = {"partitions": partitions_data}
        host = MagicMock()
        result = _get_troshkad_storage(host)
        self.assertEqual(result, {"partitions": partitions_data})

    @patch("app.services.troshkad_client.check_disk_usage")
    def test_returns_used_pct_with_bytes(self, mock_check):
        """check_disk_usage returns used_pct + bytes -> computes free/total GB."""
        from app.api.hosts import _get_troshkad_storage

        total = 1024**3 * 100  # 100 GB
        free = 1024**3 * 60  # 60 GB
        mock_check.return_value = {
            "used_pct": 40.0,
            "free_bytes": free,
            "total_bytes": total,
        }
        host = MagicMock()
        result = _get_troshkad_storage(host)
        self.assertEqual(result["used_pct"], 40.0)
        self.assertEqual(result["free_gb"], 60.0)
        self.assertEqual(result["total_gb"], 100.0)

    @patch("app.services.troshkad_client.check_disk_usage")
    def test_returns_none_for_empty_dict(self, mock_check):
        """check_disk_usage returns empty dict (no partitions, no used_pct) -> None."""
        from app.api.hosts import _get_troshkad_storage

        mock_check.return_value = {}
        host = MagicMock()
        self.assertIsNone(_get_troshkad_storage(host))


class TestSetupConsoleDns(unittest.TestCase):
    """Tests for _setup_console_dns."""

    @patch("app.services.providers.get_provider_driver")
    @patch("app.services.console_dns.console_domain_for_host")
    def test_success_all_params_present(self, mock_domain, mock_get_drv):
        """All params present -> creates console record, sets console_domain."""
        from app.api.hosts import _setup_console_dns

        mock_domain.return_value = "i-abc.console.example.com"
        drv = MagicMock()
        drv.create_console_record.return_value = "i-abc.console.example.com"
        mock_get_drv.return_value = drv

        h = MagicMock()
        h.instance_id = "i-abc"
        h.ip_address = "10.0.0.1"
        h.provider_id = "prov-1"

        s = MagicMock()
        prov_obj = MagicMock()
        s.get.return_value = prov_obj

        _setup_console_dns(h, s, "console.example.com")

        mock_domain.assert_called_once_with("i-abc", "console.example.com")
        drv.create_console_record.assert_called_once_with(
            prov_obj, h, "i-abc.console.example.com", "10.0.0.1"
        )
        self.assertEqual(h.console_domain, "i-abc.console.example.com")

    def test_missing_instance_id_returns_early(self):
        """No instance_id -> returns immediately without any calls."""
        from app.api.hosts import _setup_console_dns

        h = MagicMock()
        h.instance_id = None
        h.ip_address = "10.0.0.1"
        s = MagicMock()

        _setup_console_dns(h, s, "console.example.com")

        s.get.assert_not_called()

    def test_missing_ip_address_returns_early(self):
        """No ip_address -> returns immediately without any calls."""
        from app.api.hosts import _setup_console_dns

        h = MagicMock()
        h.instance_id = "i-abc"
        h.ip_address = None
        s = MagicMock()

        _setup_console_dns(h, s, "console.example.com")

        s.get.assert_not_called()

    def test_missing_provider_returns_early(self):
        """Provider not found in DB -> returns without creating record."""
        from app.api.hosts import _setup_console_dns

        h = MagicMock()
        h.instance_id = "i-abc"
        h.ip_address = "10.0.0.1"
        h.provider_id = "prov-missing"

        s = MagicMock()
        s.get.return_value = None

        _setup_console_dns(h, s, "console.example.com")

        # Should have tried to get provider but stopped there
        s.get.assert_called_once()

    @patch("app.services.providers.get_provider_driver")
    @patch("app.services.console_dns.console_domain_for_host")
    def test_driver_exception_caught(self, mock_domain, mock_get_drv):
        """Driver exception -> caught, does not raise."""
        from app.api.hosts import _setup_console_dns

        mock_domain.return_value = "i-abc.console.example.com"
        drv = MagicMock()
        drv.create_console_record.side_effect = RuntimeError("AWS error")
        mock_get_drv.return_value = drv

        h = MagicMock()
        h.instance_id = "i-abc"
        h.ip_address = "10.0.0.1"
        h.id = "host-12345678"
        h.provider_id = "prov-1"

        s = MagicMock()
        s.get.return_value = MagicMock()

        # Should not raise
        _setup_console_dns(h, s, "console.example.com")


class TestApplyPoolNfsConfig(unittest.TestCase):
    """Tests for _apply_pool_nfs_config."""

    def _call(self, kwargs, pool):
        from app.api.hosts import _apply_pool_nfs_config

        return _apply_pool_nfs_config(kwargs, pool)

    def test_fsx_pool(self):
        """shared-fsx pool -> sets nfs_server to fsx_dns_name, path to /fsx."""
        pool = MagicMock()
        pool.mode = "shared-fsx"
        pool.fsx_dns_name = "fs-abc.fsx.us-east-1.amazonaws.com"
        kwargs = {}
        self._call(kwargs, pool)
        self.assertEqual(kwargs["nfs_server"], "fs-abc.fsx.us-east-1.amazonaws.com")
        self.assertEqual(kwargs["nfs_path"], "/fsx")

    def test_byo_with_port(self):
        """shared-byo with nfs_port -> parses endpoint and includes port."""
        pool = MagicMock()
        pool.mode = "shared-byo"
        pool.fsx_dns_name = None
        pool.nfs_endpoint = "nfs.local:/exports/data"
        pool.nfs_port = 2049
        kwargs = {}
        self._call(kwargs, pool)
        self.assertEqual(kwargs["nfs_server"], "nfs.local")
        self.assertEqual(kwargs["nfs_path"], "/exports/data")
        self.assertEqual(kwargs["nfs_port"], 2049)

    def test_byo_without_path(self):
        """shared-byo endpoint with no colon path -> defaults to '/'."""
        pool = MagicMock()
        pool.mode = "shared-byo"
        pool.fsx_dns_name = None
        pool.nfs_endpoint = "nfs-server.local"
        pool.nfs_port = None
        kwargs = {}
        self._call(kwargs, pool)
        self.assertEqual(kwargs["nfs_server"], "nfs-server.local")
        self.assertEqual(kwargs["nfs_path"], "/")
        self.assertNotIn("nfs_port", kwargs)

    def test_ceph_nfs(self):
        """shared-ceph-nfs -> same parsing as byo."""
        pool = MagicMock()
        pool.mode = "shared-ceph-nfs"
        pool.fsx_dns_name = None
        pool.nfs_endpoint = "ceph.local:/cephfs/troshka"
        pool.nfs_port = None
        kwargs = {}
        self._call(kwargs, pool)
        self.assertEqual(kwargs["nfs_server"], "ceph.local")
        self.assertEqual(kwargs["nfs_path"], "/cephfs/troshka")

    def test_local_pool_no_changes(self):
        """local pool -> does not modify kwargs."""
        pool = MagicMock()
        pool.mode = "local"
        pool.fsx_dns_name = None
        pool.nfs_endpoint = None
        kwargs = {}
        self._call(kwargs, pool)
        self.assertNotIn("nfs_server", kwargs)
        self.assertNotIn("nfs_path", kwargs)


class TestApplyPoolTlsConfig(unittest.TestCase):
    """Tests for _apply_pool_tls_config."""

    @patch("app.services.storage_pool_service.sign_host_cert")
    def test_success(self, mock_sign):
        """Pool with CA + host with IP -> signs and stores TLS certs."""
        from app.api.hosts import _apply_pool_tls_config

        mock_sign.return_value = ("--HOST-CERT--", "--HOST-KEY--")

        pool = MagicMock()
        pool.ca_cert = "--CA-CERT--"
        pool.ca_key = "--CA-KEY--"

        h = MagicMock()
        h.ip_address = "10.0.0.1"
        h.private_ip = "172.16.0.5"

        kwargs = {}
        _apply_pool_tls_config(kwargs, pool, h)

        self.assertEqual(kwargs["ca_cert"], "--CA-CERT--")
        self.assertEqual(kwargs["host_cert"], "--HOST-CERT--")
        self.assertEqual(kwargs["host_key"], "--HOST-KEY--")
        mock_sign.assert_called_once_with(
            "--CA-CERT--", "--CA-KEY--", "10.0.0.1", "172.16.0.5"
        )

    def test_missing_ca_cert_returns_early(self):
        """Pool without ca_cert -> returns early, no TLS kwargs set."""
        from app.api.hosts import _apply_pool_tls_config

        pool = MagicMock()
        pool.ca_cert = None
        pool.ca_key = "--CA-KEY--"

        h = MagicMock()
        h.ip_address = "10.0.0.1"

        kwargs = {}
        _apply_pool_tls_config(kwargs, pool, h)
        self.assertNotIn("ca_cert", kwargs)

    def test_missing_ip_address_returns_early(self):
        """Host without ip_address -> returns early, no TLS kwargs set."""
        from app.api.hosts import _apply_pool_tls_config

        pool = MagicMock()
        pool.ca_cert = "--CA-CERT--"
        pool.ca_key = "--CA-KEY--"

        h = MagicMock()
        h.ip_address = None

        kwargs = {}
        _apply_pool_tls_config(kwargs, pool, h)
        self.assertNotIn("ca_cert", kwargs)

    @patch("app.services.storage_pool_service.sign_host_cert")
    def test_no_private_ip_uses_empty_string(self, mock_sign):
        """Host without private_ip -> passes empty string as fallback."""
        from app.api.hosts import _apply_pool_tls_config

        mock_sign.return_value = ("--CERT--", "--KEY--")

        pool = MagicMock()
        pool.ca_cert = "--CA--"
        pool.ca_key = "--CAKEY--"

        h = MagicMock()
        h.ip_address = "10.0.0.1"
        h.private_ip = None

        kwargs = {}
        _apply_pool_tls_config(kwargs, pool, h)

        mock_sign.assert_called_once_with("--CA--", "--CAKEY--", "10.0.0.1", "")
        self.assertEqual(kwargs["ca_cert"], "--CA--")


class TestResetStoppedProjectsToDraft(unittest.TestCase):
    """Tests for _reset_stopped_projects_to_draft."""

    def _call(self, db, host):
        from app.api.hosts import _reset_stopped_projects_to_draft

        return _reset_stopped_projects_to_draft(db, host)

    def test_resets_two_stopped_projects(self):
        """Two stopped projects -> both reset to draft, returns 2."""
        p1 = MagicMock()
        p2 = MagicMock()

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [p1, p2]

        host = MagicMock()
        host.id = "host-12345678"

        count = self._call(db, host)

        self.assertEqual(count, 2)
        self.assertEqual(p1.state, "draft")
        self.assertIsNone(p1.host_id)
        self.assertIsNone(p1.deployed_topology)
        self.assertIsNone(p1.deploy_error)
        self.assertIsNone(p1.vni_map)
        self.assertEqual(p2.state, "draft")
        self.assertIsNone(p2.host_id)
        db.flush.assert_called_once()

    def test_no_matching_projects(self):
        """No stopped projects -> returns 0, no flush."""
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []

        host = MagicMock()
        host.id = "host-12345678"

        count = self._call(db, host)

        self.assertEqual(count, 0)
        db.flush.assert_not_called()

    def test_single_error_project(self):
        """One error project -> reset to draft, returns 1."""
        p1 = MagicMock()

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [p1]

        host = MagicMock()
        host.id = "host-12345678"

        count = self._call(db, host)

        self.assertEqual(count, 1)
        self.assertEqual(p1.state, "draft")


class TestCleanupConsoleRecord(unittest.TestCase):
    """Tests for _cleanup_console_record."""

    @patch("app.services.providers.get_provider_driver")
    def test_success(self, mock_get_drv):
        """Host with console_domain + provider -> deletes console record."""
        from app.api.hosts import _cleanup_console_record

        drv = MagicMock()
        mock_get_drv.return_value = drv

        host = MagicMock()
        host.console_domain = "i-abc.console.example.com"
        host.ip_address = "10.0.0.1"
        host.provider_id = "prov-1"
        host.id = "host-12345678"

        prov = MagicMock()
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = prov

        _cleanup_console_record(db, host)

        drv.delete_console_record.assert_called_once_with(
            prov, host, "i-abc.console.example.com", "10.0.0.1"
        )

    def test_missing_console_domain_returns_early(self):
        """No console_domain -> returns immediately."""
        from app.api.hosts import _cleanup_console_record

        host = MagicMock()
        host.console_domain = None
        host.ip_address = "10.0.0.1"

        db = MagicMock()
        _cleanup_console_record(db, host)
        db.query.assert_not_called()

    def test_missing_ip_address_returns_early(self):
        """No ip_address -> returns immediately."""
        from app.api.hosts import _cleanup_console_record

        host = MagicMock()
        host.console_domain = "i-abc.console.example.com"
        host.ip_address = None

        db = MagicMock()
        _cleanup_console_record(db, host)
        db.query.assert_not_called()

    @patch("app.services.providers.get_provider_driver")
    def test_driver_exception_caught(self, mock_get_drv):
        """Driver raises -> exception caught, does not propagate."""
        from app.api.hosts import _cleanup_console_record

        drv = MagicMock()
        drv.delete_console_record.side_effect = RuntimeError("DNS error")
        mock_get_drv.return_value = drv

        host = MagicMock()
        host.console_domain = "i-abc.console.example.com"
        host.ip_address = "10.0.0.1"
        host.provider_id = "prov-1"
        host.id = "host-12345678"

        prov = MagicMock()
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = prov

        # Should not raise
        _cleanup_console_record(db, host)


class TestTerminateHostInstance(unittest.TestCase):
    """Tests for _terminate_host_instance."""

    @patch("app.services.providers.get_provider_driver")
    def test_success_with_provider(self, mock_get_drv):
        """Provider exists -> uses driver to terminate."""
        from app.api.hosts import _terminate_host_instance

        drv = MagicMock()
        mock_get_drv.return_value = drv

        host = MagicMock()
        host.instance_id = "i-abc123"
        host.provider_id = "prov-1"
        host.id = "host-12345678"

        prov = MagicMock()
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = prov

        _terminate_host_instance(db, host, None)

        drv.terminate_host.assert_called_once_with(prov, "i-abc123")

    @patch("app.services.provisioner.terminate_host")
    def test_success_without_provider_fallback(self, mock_terminate):
        """No provider -> falls back to provisioner.terminate_host."""
        from app.api.hosts import _terminate_host_instance

        host = MagicMock()
        host.instance_id = "i-abc123"
        host.provider_id = None
        host.id = "host-12345678"

        db = MagicMock()
        creds = {"aws_access_key_id": "AKIA...", "aws_secret_access_key": "..."}

        _terminate_host_instance(db, host, creds)

        mock_terminate.assert_called_once_with("i-abc123", credentials=creds)

    @patch("app.services.providers.get_provider_driver")
    def test_failure_raises_http_500(self, mock_get_drv):
        """Terminate fails -> sets state to active, raises HTTPException(500)."""
        from fastapi import HTTPException

        from app.api.hosts import _terminate_host_instance

        drv = MagicMock()
        drv.terminate_host.side_effect = RuntimeError("EC2 error")
        mock_get_drv.return_value = drv

        host = MagicMock()
        host.instance_id = "i-abc123"
        host.provider_id = "prov-1"
        host.id = "host-12345678"

        prov = MagicMock()
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = prov

        with self.assertRaises(HTTPException) as ctx:
            _terminate_host_instance(db, host, None)

        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(host.state, "active")
        db.commit.assert_called_once()

    def test_no_instance_id_returns_early(self):
        """No instance_id -> returns immediately without termination."""
        from app.api.hosts import _terminate_host_instance

        host = MagicMock()
        host.instance_id = None

        db = MagicMock()

        _terminate_host_instance(db, host, None)

        db.query.assert_not_called()


class TestDetachInstallIso(unittest.TestCase):
    """Tests for _detach_install_iso."""

    @patch("app.services.troshkad_client.start_job")
    def test_success_calls_start_job_and_detach(self, mock_start_job):
        """Calls troshkad start_job and provider detach_iso."""
        from app.api.hosts import _detach_install_iso

        host = MagicMock()
        host.id = "host-12345678"
        host.provider_id = "prov-1"
        host.instance_id = "i-abc"

        prov = MagicMock()
        prov.type = "ocpvirt"
        drv = MagicMock()

        db_session = MagicMock()
        db_session.get.return_value = prov

        with patch("app.services.providers.get_provider_driver", return_value=drv):
            _detach_install_iso(host, db_session)

        mock_start_job.assert_called_once()
        drv.detach_iso.assert_called_once_with(prov, "i-abc")

    @patch(
        "app.services.troshkad_client.start_job", side_effect=RuntimeError("conn err")
    )
    def test_troshkad_failure_non_fatal(self, mock_start_job):
        """start_job failure is caught and does not propagate."""
        from app.api.hosts import _detach_install_iso

        host = MagicMock()
        host.id = "host-12345678"
        host.provider_id = "prov-1"
        host.instance_id = "i-abc"

        prov = MagicMock()
        db_session = MagicMock()
        db_session.get.return_value = prov

        with patch("app.services.providers.get_provider_driver") as mock_get_drv:
            drv = MagicMock()
            mock_get_drv.return_value = drv
            # Should not raise
            _detach_install_iso(host, db_session)

    def test_no_provider_found(self):
        """Provider not in DB -> detach_iso skipped."""
        from app.api.hosts import _detach_install_iso

        host = MagicMock()
        host.id = "host-12345678"
        host.provider_id = "prov-missing"

        db_session = MagicMock()
        db_session.get.return_value = None

        with patch("app.services.troshkad_client.start_job"):
            # Should not raise
            _detach_install_iso(host, db_session)

    @patch("app.services.troshkad_client.start_job")
    def test_driver_without_detach_iso(self, mock_start_job):
        """Provider driver has no detach_iso -> no error."""
        from app.api.hosts import _detach_install_iso

        host = MagicMock()
        host.id = "host-12345678"
        host.provider_id = "prov-1"
        host.instance_id = "i-abc"

        prov = MagicMock()
        drv = MagicMock(spec=[])  # no detach_iso attribute

        db_session = MagicMock()
        db_session.get.return_value = prov

        with patch("app.services.providers.get_provider_driver", return_value=drv):
            # Should not raise even without detach_iso
            _detach_install_iso(host, db_session)


class TestWaitForRunningInstance(unittest.TestCase):
    """Tests for _wait_for_running_instance."""

    @patch("time.sleep")
    @patch("time.time")
    def test_already_running(self, mock_time, mock_sleep):
        """Instance already running -> returns IP immediately without starting."""
        from app.api.hosts import _wait_for_running_instance

        drv = MagicMock()
        prov = MagicMock()

        # First loop: get_host_status returns "running"
        drv.get_host_status.return_value = {
            "state": "running",
            "public_ip": "1.2.3.4",
        }
        # time.time() for deadline check
        mock_time.side_effect = [0, 1]

        ip, st = _wait_for_running_instance(drv, prov, "host-12345678", "i-abc")

        self.assertEqual(ip, "1.2.3.4")
        self.assertEqual(st["state"], "running")
        # Should NOT call start_host since it's already running
        drv.start_host.assert_not_called()

    @patch("time.sleep")
    @patch("time.time")
    def test_stopped_then_starts(self, mock_time, mock_sleep):
        """Instance stopped -> calls start_host, then polls until running."""
        from app.api.hosts import _wait_for_running_instance

        drv = MagicMock()
        prov = MagicMock()

        # First phase: get_host_status returns "stopped" (breaks first loop)
        # Second phase: first poll returns "pending", second returns "running"
        drv.get_host_status.side_effect = [
            {"state": "stopped"},
            {"state": "pending"},
            {"state": "running", "public_ip": "5.6.7.8"},
        ]
        mock_time.side_effect = [0, 1, 2]

        ip, st = _wait_for_running_instance(drv, prov, "host-12345678", "i-abc")

        self.assertEqual(ip, "5.6.7.8")
        drv.start_host.assert_called_once_with(prov, "i-abc")

    @patch("time.sleep")
    @patch("time.time")
    def test_start_host_failure_still_polls(self, mock_time, mock_sleep):
        """start_host raises -> caught, still polls for running state."""
        from app.api.hosts import _wait_for_running_instance

        drv = MagicMock()
        prov = MagicMock()

        drv.get_host_status.side_effect = [
            {"state": "stopped"},
            {"state": "running", "public_ip": "9.0.1.2"},
        ]
        drv.start_host.side_effect = RuntimeError("API error")
        mock_time.side_effect = [0, 1]

        ip, st = _wait_for_running_instance(drv, prov, "host-12345678", "i-abc")

        self.assertEqual(ip, "9.0.1.2")

    @patch("time.sleep")
    @patch("time.time")
    def test_timeout_returns_none(self, mock_time, mock_sleep):
        """Instance never reaches running -> returns (None, None)."""
        from app.api.hosts import _wait_for_running_instance

        drv = MagicMock()
        prov = MagicMock()

        drv.get_host_status.return_value = {"state": "stopped"}
        # Simulate time passing beyond the 300s deadline
        mock_time.side_effect = [0, 400]

        ip, st = _wait_for_running_instance(drv, prov, "host-12345678", "i-abc")

        self.assertIsNone(ip)
        self.assertIsNone(st)


class TestFinalizeTermination(unittest.TestCase):
    """Tests for _finalize_termination."""

    @patch("time.sleep")
    def test_deletes_keypair_and_host(self, mock_sleep):
        """Key pair name + provider + driver -> deletes key pair and host."""
        from app.api.hosts import _finalize_termination

        s = MagicMock()
        h = MagicMock()
        h.key_pair_name = "troshka-host-abc"
        h.id = "host-12345678"
        prov = MagicMock()
        drv = MagicMock()

        result = _finalize_termination(s, h, prov, drv)

        self.assertTrue(result)
        drv.delete_key_pair.assert_called_once_with(prov, "troshka-host-abc")
        self.assertEqual(h.state, "terminated")
        s.delete.assert_called_once_with(h)
        self.assertEqual(s.commit.call_count, 2)

    @patch("time.sleep")
    def test_no_keypair(self, mock_sleep):
        """No key_pair_name -> skip delete_key_pair, still terminates."""
        from app.api.hosts import _finalize_termination

        s = MagicMock()
        h = MagicMock()
        h.key_pair_name = None
        h.id = "host-12345678"

        result = _finalize_termination(s, h, None, None)

        self.assertTrue(result)
        self.assertEqual(h.state, "terminated")
        s.delete.assert_called_once_with(h)

    @patch("time.sleep")
    def test_delete_key_pair_failure_non_fatal(self, mock_sleep):
        """delete_key_pair raises -> caught, termination proceeds."""
        from app.api.hosts import _finalize_termination

        s = MagicMock()
        h = MagicMock()
        h.key_pair_name = "troshka-host-abc"
        h.id = "host-12345678"
        prov = MagicMock()
        drv = MagicMock()
        drv.delete_key_pair.side_effect = RuntimeError("AWS error")

        result = _finalize_termination(s, h, prov, drv)

        self.assertTrue(result)
        self.assertEqual(h.state, "terminated")
        s.delete.assert_called_once_with(h)


class TestPushVncdUpdate(unittest.TestCase):
    """Tests for _push_vncd_update."""

    @patch("app.services.troshkad_client.troshkad_request")
    def test_success(self, mock_request):
        """vncd file exists -> reads, base64 encodes, sends request."""
        from app.api.hosts import _push_vncd_update

        h = MagicMock()
        h.id = "host-12345678"
        h.provider_id = "prov-1"

        s = MagicMock()
        prov = MagicMock()
        prov.type = "ec2"
        s.query.return_value.filter_by.return_value.first.return_value = prov

        fake_vncd_content = b"#!/usr/bin/env python3\nprint('vncd')\n"

        with patch("os.path.exists", return_value=True), patch(
            "builtins.open", unittest.mock.mock_open(read_data=fake_vncd_content)
        ):
            _push_vncd_update(h, s)

        mock_request.assert_called_once()
        call_args = mock_request.call_args
        self.assertEqual(call_args[0][1], "POST")
        self.assertEqual(call_args[0][2], "/admin/update-vncd")
        body = call_args[1]["body"] if "body" in call_args[1] else call_args[0][3]
        self.assertFalse(body["no_tls"])

    @patch("app.services.troshkad_client.troshkad_request")
    def test_ocpvirt_no_tls(self, mock_request):
        """OCP Virt provider -> no_tls=True."""
        from app.api.hosts import _push_vncd_update

        h = MagicMock()
        h.id = "host-12345678"
        h.provider_id = "prov-1"

        s = MagicMock()
        prov = MagicMock()
        prov.type = "ocpvirt"
        s.query.return_value.filter_by.return_value.first.return_value = prov

        with patch("os.path.exists", return_value=True), patch(
            "builtins.open", unittest.mock.mock_open(read_data=b"vncd content")
        ):
            _push_vncd_update(h, s)

        call_args = mock_request.call_args
        body = call_args[1]["body"] if "body" in call_args[1] else call_args[0][3]
        self.assertTrue(body["no_tls"])

    def test_vncd_file_missing(self):
        """vncd file does not exist -> returns early without error."""
        from app.api.hosts import _push_vncd_update

        h = MagicMock()
        h.id = "host-12345678"
        s = MagicMock()

        with patch("os.path.exists", return_value=False):
            # Should not raise
            _push_vncd_update(h, s)


class TestUpdateConsoleDnsForNewIp(unittest.TestCase):
    """Tests for _update_console_dns_for_new_ip."""

    @patch("app.services.providers.get_provider_driver")
    def test_ip_changed_updates_dns(self, mock_get_drv):
        """New IP differs from old -> updates console DNS."""
        from app.api.hosts import _update_console_dns_for_new_ip

        drv = MagicMock()
        mock_get_drv.return_value = drv

        h = MagicMock()
        h.console_domain = "i-abc.console.example.com"
        h.provider_id = "prov-1"
        h.id = "host-12345678"

        prov = MagicMock()
        s = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = prov

        _update_console_dns_for_new_ip(h, s, "10.0.0.1", "10.0.0.2")

        drv.create_console_record.assert_called_once_with(
            prov, h, "i-abc.console.example.com", "10.0.0.2"
        )

    def test_same_ip_no_update(self):
        """New IP same as old -> no update."""
        from app.api.hosts import _update_console_dns_for_new_ip

        h = MagicMock()
        h.console_domain = "i-abc.console.example.com"
        s = MagicMock()

        _update_console_dns_for_new_ip(h, s, "10.0.0.1", "10.0.0.1")

        s.query.assert_not_called()

    def test_no_console_domain(self):
        """No console_domain -> no update."""
        from app.api.hosts import _update_console_dns_for_new_ip

        h = MagicMock()
        h.console_domain = None
        s = MagicMock()

        _update_console_dns_for_new_ip(h, s, "10.0.0.1", "10.0.0.2")

        s.query.assert_not_called()

    def test_empty_new_ip(self):
        """Empty new_ip -> no update."""
        from app.api.hosts import _update_console_dns_for_new_ip

        h = MagicMock()
        h.console_domain = "i-abc.console.example.com"
        s = MagicMock()

        _update_console_dns_for_new_ip(h, s, "10.0.0.1", "")

        s.query.assert_not_called()

    @patch("app.services.providers.get_provider_driver")
    def test_driver_exception_caught(self, mock_get_drv):
        """Driver raises -> exception caught, does not propagate."""
        from app.api.hosts import _update_console_dns_for_new_ip

        drv = MagicMock()
        drv.create_console_record.side_effect = RuntimeError("DNS error")
        mock_get_drv.return_value = drv

        h = MagicMock()
        h.console_domain = "i-abc.console.example.com"
        h.provider_id = "prov-1"
        h.id = "host-12345678"

        prov = MagicMock()
        s = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = prov

        # Should not raise
        _update_console_dns_for_new_ip(h, s, "10.0.0.1", "10.0.0.2")


class TestGetCephStorage(unittest.TestCase):
    """Tests for _get_ceph_storage."""

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_no_provider_returns_none(self, mock_k8s):
        """No provider in DB -> returns None."""
        from app.api.hosts import _get_ceph_storage

        host = MagicMock()
        host.provider_id = "prov-missing"

        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None

        result = _get_ceph_storage(db, host)
        self.assertIsNone(result)

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_no_toolbox_pod_returns_none(self, mock_k8s):
        """No rook-ceph-tools pod -> returns None."""
        from app.api.hosts import _get_ceph_storage

        host = MagicMock()
        host.provider_id = "prov-1"

        prov = MagicMock()
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = prov

        _, core_api, _ = MagicMock(), MagicMock(), MagicMock()
        core_api.list_namespaced_pod.return_value = MagicMock(items=[])
        mock_k8s.return_value = (MagicMock(), core_api, MagicMock())

        result = _get_ceph_storage(db, host)
        self.assertIsNone(result)


class TestPollAgentAfterUpdate(unittest.TestCase):
    """Tests for _poll_agent_after_update."""

    @patch("time.sleep")
    @patch("app.services.troshkad_client.check_health")
    def test_agent_comes_back_with_new_version(self, mock_health, mock_sleep):
        """Agent goes down then returns with new version -> returns True."""
        from app.api.hosts import _poll_agent_after_update

        h = MagicMock()
        h.id = "host-12345678"
        s = MagicMock()

        # First phase: 2 calls returning health, then None (agent down)
        # Second phase: returns with new version
        mock_health.side_effect = [
            {"version": "old123"},
            None,  # agent went down
            {"version": "new456"},  # agent back with new version
        ]

        with patch("app.api.hosts._push_vncd_update"):
            result = _poll_agent_after_update(h, s, "old123")

        self.assertTrue(result)
        self.assertEqual(h.agent_version, "new456")

    @patch("time.sleep")
    @patch("app.services.troshkad_client.check_health")
    def test_agent_never_comes_back(self, mock_health, mock_sleep):
        """Agent goes down and never returns -> returns False."""
        from app.api.hosts import _poll_agent_after_update

        h = MagicMock()
        h.id = "host-12345678"
        s = MagicMock()

        # Always returns None
        mock_health.return_value = None

        result = _poll_agent_after_update(h, s, "old123")

        self.assertFalse(result)

    @patch("time.sleep")
    @patch("app.services.troshkad_client.check_health")
    def test_vncd_push_failure_non_fatal(self, mock_health, mock_sleep):
        """vncd push fails after agent restart -> logged, still returns True."""
        from app.api.hosts import _poll_agent_after_update

        h = MagicMock()
        h.id = "host-12345678"
        s = MagicMock()

        mock_health.side_effect = [
            None,  # agent down
            {"version": "new456"},  # agent back
        ]

        with patch(
            "app.api.hosts._push_vncd_update", side_effect=RuntimeError("vncd error")
        ):
            result = _poll_agent_after_update(h, s, "old123")

        self.assertTrue(result)


class TestGetExpectedAgentVersion(unittest.TestCase):
    """Tests for get_expected_agent_version endpoint helper."""

    def test_returns_hash(self):
        """Returns a SHA256 hash prefix of the troshkad source."""
        import hashlib

        from app.api.hosts import get_expected_agent_version

        fake_bytes = b"#!/usr/bin/env python3\nVERSION = 'dev'\n"
        expected_hash = hashlib.sha256(fake_bytes).hexdigest()[:12]

        with patch("builtins.open", unittest.mock.mock_open(read_data=fake_bytes)):
            result = get_expected_agent_version()

        self.assertEqual(result["version"], expected_hash)


class TestProvisionRequest(unittest.TestCase):
    """Tests for the ProvisionRequest Pydantic model."""

    def test_defaults(self):
        """All optional fields default to None."""
        from app.api.hosts import ProvisionRequest

        req = ProvisionRequest(provider_id="prov-1")
        self.assertEqual(req.provider_id, "prov-1")
        self.assertIsNone(req.instance_type)
        self.assertIsNone(req.region)
        self.assertIsNone(req.image_id)
        self.assertIsNone(req.storage_pool_id)
        self.assertIsNone(req.disk_gb)

    def test_all_fields(self):
        """All fields populated -> stored correctly."""
        from app.api.hosts import ProvisionRequest

        req = ProvisionRequest(
            provider_id="prov-1",
            instance_type="m5.xlarge",
            region="us-east-1",
            image_id="ami-abc",
            storage_pool_id="pool-1",
            disk_gb=1000,
        )
        self.assertEqual(req.disk_gb, 1000)
        self.assertEqual(req.instance_type, "m5.xlarge")


if __name__ == "__main__":
    unittest.main()
