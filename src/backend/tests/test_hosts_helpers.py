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

        session = MagicMock()

        # The function opens the file twice: once "rb" (for hash), once default (for text).
        # mock_open with read_data=bytes returns bytes for .read() in both cases,
        # which works for the rb open. The second open().read() is only reached
        # when versions differ, so for the "match" case we only need the rb open.
        with patch(
            "builtins.open", unittest.mock.mock_open(read_data=fake_source_bytes)
        ):
            _verify_and_update_agent_version(host, session)

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

        session = MagicMock()

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
            _verify_and_update_agent_version(host, session)

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

        session = MagicMock()

        _verify_and_update_agent_version(host, session)

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

        session = MagicMock()

        _verify_and_update_agent_version(host, session)

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

        session = MagicMock()

        # Should not raise
        _verify_and_update_agent_version(host, session)

        mock_push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
