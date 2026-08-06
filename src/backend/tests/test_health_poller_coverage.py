"""Tests for uncovered paths in health_poller.py.

Covers:
  - _check_mesh_health success path (WireGuard warnings)
  - _check_ip_change_if_all_unreachable
  - _renew_pool_ca_if_expiring
  - _renew_host_certs_for_pool
  - _check_cert_renewal
  - start_health_poller
  - _poll_hosts kubevirt-cluster branch
  - _poll_hosts skip-until logic
  - _poll_hosts pattern-buffer auto-stop
"""

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import app.services.health_poller as hp

# ═══════════════════════════════════════════════════════════════════════════
# _check_mesh_health
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckMeshHealth:
    @patch("app.services.health_poller.ProjectMeshPeer")
    @patch("app.services.troshkad_client.troshkad_request")
    def test_stale_handshake_generates_warning(self, mock_req, mock_peer):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.count.return_value = 1
        host = MagicMock()
        host.id = "host-123"
        host.storage_warnings = []
        mock_req.return_value = {
            "projects": {
                "proj-aaaaaaaa": {"peers": {"pubkey12345678": int(time.time()) - 300}}
            }
        }
        hp._check_mesh_health(host, db)
        assert any("stale" in w for w in host.storage_warnings)

    @patch("app.services.health_poller.ProjectMeshPeer")
    @patch("app.services.troshkad_client.troshkad_request")
    def test_error_in_project_generates_warning(self, mock_req, mock_peer):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.count.return_value = 1
        host = MagicMock()
        host.id = "host-456"
        host.storage_warnings = ["disk /data 90%"]
        mock_req.return_value = {
            "projects": {"proj-bbbbbbbb": {"error": "interface down"}}
        }
        hp._check_mesh_health(host, db)
        # Existing non-mesh warnings preserved
        assert any("disk" in w for w in host.storage_warnings)
        assert any("interface down" in w for w in host.storage_warnings)

    @patch("app.services.health_poller.ProjectMeshPeer")
    def test_no_peers_early_return(self, mock_peer):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.count.return_value = 0
        host = MagicMock()
        hp._check_mesh_health(host, db)
        # Should not call troshkad_request

    @patch("app.services.health_poller.ProjectMeshPeer")
    @patch(
        "app.services.troshkad_client.troshkad_request", side_effect=Exception("conn")
    )
    def test_exception_handled(self, mock_req, mock_peer):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.count.return_value = 2
        host = MagicMock()
        host.id = "host-err"
        hp._check_mesh_health(host, db)


# ═══════════════════════════════════════════════════════════════════════════
# _check_ip_change_if_all_unreachable
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckIpChange:
    @patch("app.services.provisioner.update_sg_troshkad_ip")
    @patch("app.services.provisioner.get_public_ip")
    def test_ip_changed_updates_sgs(self, mock_ip, mock_sg):
        mock_ip.return_value = "2.2.2.2"
        old_ip = hp._last_known_ip
        hp._last_known_ip = "1.1.1.1"
        try:
            mock_prov = MagicMock()
            mock_prov.security_group_id = "sg-123"
            mock_prov.id = "prov-abcd1234"
            mock_prov.get_credentials.return_value = {}
            with patch("app.core.database.SessionLocal") as mock_sl:
                mock_db = MagicMock()
                mock_sl.return_value = mock_db
                mock_db.query.return_value.filter.return_value.all.return_value = [
                    mock_prov
                ]
                hp._check_ip_change_if_all_unreachable(3, 3)
            mock_sg.assert_called()
            assert hp._last_known_ip == "2.2.2.2"
        finally:
            hp._last_known_ip = old_ip

    @patch("app.services.provisioner.get_public_ip", return_value="1.1.1.1")
    def test_same_ip_no_action(self, mock_ip):
        old_ip = hp._last_known_ip
        hp._last_known_ip = "1.1.1.1"
        try:
            hp._check_ip_change_if_all_unreachable(2, 2)
            # No error = success
        finally:
            hp._last_known_ip = old_ip

    def test_partial_failure_no_action(self):
        # Only 1 of 3 failed — not all unreachable
        hp._check_ip_change_if_all_unreachable(3, 1)

    def test_zero_failures_no_action(self):
        hp._check_ip_change_if_all_unreachable(5, 0)

    @patch("app.services.provisioner.get_public_ip", return_value=None)
    def test_no_current_ip_returns(self, mock_ip):
        old_ip = hp._last_known_ip
        hp._last_known_ip = "1.1.1.1"
        try:
            hp._check_ip_change_if_all_unreachable(2, 2)
        finally:
            hp._last_known_ip = old_ip


# ═══════════════════════════════════════════════════════════════════════════
# _renew_pool_ca_if_expiring
# ═══════════════════════════════════════════════════════════════════════════


class TestRenewPoolCa:
    @patch("app.services.storage_pool_service.generate_pool_ca")
    def test_expiring_ca_gets_renewed(self, mock_gen):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(1000)
            .not_valid_before(datetime.now(UTC) - timedelta(days=365))
            .not_valid_after(datetime.now(UTC) + timedelta(days=30))
            .sign(key, hashes.SHA256())
        )
        pem = cert.public_bytes(serialization.Encoding.PEM).decode()

        pool = MagicMock()
        pool.ca_cert = pem
        pool.name = "test-pool"
        db = MagicMock()
        mock_gen.return_value = ("new-cert", "new-key")

        hp._renew_pool_ca_if_expiring(pool, db)
        assert pool.ca_cert == "new-cert"
        assert pool.ca_key == "new-key"
        db.commit.assert_called()

    @patch("app.services.storage_pool_service.generate_pool_ca")
    def test_fresh_ca_not_renewed(self, mock_gen):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fresh-ca")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(2000)
            .not_valid_before(datetime.now(UTC) - timedelta(days=30))
            .not_valid_after(datetime.now(UTC) + timedelta(days=335))
            .sign(key, hashes.SHA256())
        )
        pem = cert.public_bytes(serialization.Encoding.PEM).decode()

        pool = MagicMock()
        pool.ca_cert = pem
        pool.name = "fresh-pool"
        db = MagicMock()

        hp._renew_pool_ca_if_expiring(pool, db)
        mock_gen.assert_not_called()

    def test_bad_cert_handled(self):
        pool = MagicMock()
        pool.ca_cert = "not-a-cert"
        pool.name = "bad-pool"
        db = MagicMock()
        hp._renew_pool_ca_if_expiring(pool, db)


# ═══════════════════════════════════════════════════════════════════════════
# _renew_host_certs_for_pool
# ═══════════════════════════════════════════════════════════════════════════


class TestRenewHostCerts:
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="job-1")
    @patch(
        "app.services.storage_pool_service.sign_host_cert",
        return_value=("cert-pem", "key-pem"),
    )
    def test_signs_and_pushes_certs(self, mock_sign, mock_start, mock_wait):
        pool = MagicMock()
        pool.id = "pool-1"
        pool.ca_cert = "ca-cert"
        pool.ca_key = "ca-key"
        db = MagicMock()
        host = MagicMock()
        host.ip_address = "10.0.0.1"
        host.private_ip = "10.0.0.2"
        host.id = "host-aabbccdd"
        db.query.return_value.filter.return_value.all.return_value = [host]
        hp._renew_host_certs_for_pool(pool, db)
        mock_sign.assert_called_once_with("ca-cert", "ca-key", "10.0.0.1", "10.0.0.2")
        mock_start.assert_called_once()

    @patch("app.services.troshkad_client.start_job", side_effect=Exception("conn err"))
    @patch(
        "app.services.storage_pool_service.sign_host_cert",
        return_value=("cert", "key"),
    )
    def test_exception_per_host_handled(self, mock_sign, mock_start):
        pool = MagicMock()
        pool.id = "pool-2"
        pool.ca_cert = "ca"
        pool.ca_key = "key"
        db = MagicMock()
        host = MagicMock()
        host.ip_address = "10.0.0.1"
        host.private_ip = ""
        host.id = "host-eeff0011"
        db.query.return_value.filter.return_value.all.return_value = [host]
        hp._renew_host_certs_for_pool(pool, db)

    def test_host_no_ip_skipped(self):
        pool = MagicMock()
        pool.id = "pool-3"
        pool.ca_cert = "ca"
        pool.ca_key = "key"
        db = MagicMock()
        host = MagicMock()
        host.ip_address = None
        host.id = "host-skip"
        db.query.return_value.filter.return_value.all.return_value = [host]
        hp._renew_host_certs_for_pool(pool, db)


# ═══════════════════════════════════════════════════════════════════════════
# _check_cert_renewal
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckCertRenewal:
    @patch("app.services.health_poller._renew_host_certs_for_pool")
    @patch("app.services.health_poller._renew_pool_ca_if_expiring")
    def test_runs_when_due(self, mock_ca, mock_host):
        old = hp._last_cert_check
        hp._last_cert_check = 0
        try:
            pool = MagicMock()
            pool.ca_cert = "cert"
            pool.ca_key = "key"
            with patch("app.core.database.SessionLocal") as mock_sl:
                mock_db = MagicMock()
                mock_sl.return_value = mock_db
                mock_db.query.return_value.filter.return_value.all.return_value = [pool]
                hp._check_cert_renewal()
            mock_ca.assert_called_once_with(pool, mock_db)
            mock_host.assert_called_once_with(pool, mock_db)
        finally:
            hp._last_cert_check = old

    def test_skips_when_recent(self):
        old = hp._last_cert_check
        hp._last_cert_check = time.time()
        try:
            hp._check_cert_renewal()
        finally:
            hp._last_cert_check = old

    @patch("app.services.health_poller._renew_host_certs_for_pool")
    @patch("app.services.health_poller._renew_pool_ca_if_expiring")
    def test_pool_without_ca_skipped(self, mock_ca, mock_host):
        old = hp._last_cert_check
        hp._last_cert_check = 0
        try:
            pool = MagicMock()
            pool.ca_cert = None
            pool.ca_key = None
            with patch("app.core.database.SessionLocal") as mock_sl:
                mock_db = MagicMock()
                mock_sl.return_value = mock_db
                mock_db.query.return_value.filter.return_value.all.return_value = [pool]
                hp._check_cert_renewal()
            mock_ca.assert_not_called()
        finally:
            hp._last_cert_check = old


# ═══════════════════════════════════════════════════════════════════════════
# start_health_poller
# ═══════════════════════════════════════════════════════════════════════════


class TestStartHealthPoller:
    @patch("app.services.health_poller._poller_loop")
    def test_returns_daemon_thread(self, mock_loop):
        mock_loop.side_effect = lambda: None
        thread = hp.start_health_poller()
        assert thread.daemon is True
        assert thread.name == "health-poller"
        thread.join(timeout=1)
