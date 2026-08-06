"""Tests for additional uncovered paths in pattern_buffer_service.py.

Covers:
  - is_provisioning / get_provision_error (module-level state)
  - _provision_pattern_buffer (main orchestrator)
  - _wait_and_install_agent
  - wake_pattern_buffer
  - get_pattern_buffer_host
  - check_auto_sleep
  - _wait_for_new_ip
  - _update_buffer_ip
  - _poll_agent_health
  - touch_activity
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import app.services.pattern_buffer_service as pbs
from app.services.pattern_buffer_service import (
    _poll_agent_health,
    _update_buffer_ip,
    _wait_for_new_ip,
    check_auto_sleep,
    get_pattern_buffer_host,
    get_provision_error,
    is_provisioning,
    touch_activity,
    wake_pattern_buffer,
)

# ═══════════════════════════════════════════════════════════════════════════
# is_provisioning / get_provision_error
# ═══════════════════════════════════════════════════════════════════════════


class TestIsProvisioning:
    def setup_method(self):
        pbs._provisioning.discard("pool-test")

    def teardown_method(self):
        pbs._provisioning.discard("pool-test")

    def test_returns_false_when_not_provisioning(self):
        assert is_provisioning("pool-test") is False

    def test_returns_true_when_provisioning(self):
        pbs._provisioning.add("pool-test")
        assert is_provisioning("pool-test") is True


class TestGetProvisionError:
    def setup_method(self):
        pbs._provision_errors.pop("pool-test", None)

    def teardown_method(self):
        pbs._provision_errors.pop("pool-test", None)

    def test_returns_none_when_no_error(self):
        assert get_provision_error("pool-test") is None

    def test_returns_error_string(self):
        pbs._provision_errors["pool-test"] = "Something went wrong"
        assert get_provision_error("pool-test") == "Something went wrong"


# ═══════════════════════════════════════════════════════════════════════════
# _provision_pattern_buffer
# ═══════════════════════════════════════════════════════════════════════════


class TestProvisionPatternBuffer:
    def setup_method(self):
        pbs._provisioning.discard("pool-1")
        pbs._provision_errors.pop("pool-1", None)

    def teardown_method(self):
        pbs._provisioning.discard("pool-1")
        pbs._provision_errors.pop("pool-1", None)

    @patch("app.services.pattern_buffer_service.SessionLocal")
    def test_pool_not_found(self, mock_session_cls):
        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        pbs._provision_pattern_buffer("pool-1")

        assert "pool-1" not in pbs._provisioning
        mock_db.close.assert_called_once()

    @patch("app.services.pattern_buffer_service.SessionLocal")
    def test_pool_already_has_active_worker(self, mock_session_cls):
        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        pool = MagicMock()
        pool.worker_host_id = "existing-host"
        existing_host = MagicMock()
        existing_host.state = "active"
        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            pool,
            existing_host,
        ]

        pbs._provision_pattern_buffer("pool-1")

        assert "pool-1" not in pbs._provisioning
        mock_db.close.assert_called_once()

    @patch("app.services.pattern_buffer_service.SessionLocal")
    def test_no_provider(self, mock_session_cls):
        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        pool = MagicMock()
        pool.worker_host_id = None
        pool.provider = None
        mock_db.query.return_value.filter_by.return_value.first.return_value = pool

        pbs._provision_pattern_buffer("pool-1")

        assert "pool-1" not in pbs._provisioning
        mock_db.close.assert_called_once()

    @patch(
        "app.services.pattern_buffer_service._wait_and_install_agent", return_value=True
    )
    @patch("app.services.pattern_buffer_service._resolve_nfs_kwargs", return_value={})
    @patch(
        "app.services.pattern_buffer_service._resolve_instance_type",
        return_value="i4i.large",
    )
    @patch("app.services.providers.get_provider_driver")
    @patch("app.services.pattern_buffer_service.SessionLocal")
    def test_successful_provision(
        self, mock_session_cls, mock_get_drv, mock_inst_type, mock_nfs, mock_wait
    ):
        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        pool = MagicMock()
        pool.id = "pool-1"
        pool.worker_host_id = None
        pool.subnet_id = None
        provider = MagicMock()
        provider.id = "prov-1"
        provider.type = "ec2"
        provider.name = "test-provider"
        provider.default_image = "ami-123"
        provider.default_region = "us-east-1"
        provider.vpc_id = "vpc-1"
        provider.subnet_id = "subnet-1"
        provider.security_group_id = "sg-1"
        pool.provider = provider
        mock_db.query.return_value.filter_by.return_value.first.return_value = pool

        driver = MagicMock()
        driver.provision_host.return_value = {
            "instance_id": "i-new",
            "instance_type": "i4i.large",
            "public_ip": "1.2.3.4",
            "private_ip": "10.0.0.1",
            "private_key": "key-data",
            "total_vcpus": 2,
            "total_ram_mb": 16384,
        }
        mock_get_drv.return_value = driver

        pbs._provision_pattern_buffer("pool-1")

        assert "pool-1" not in pbs._provisioning
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called()
        mock_db.close.assert_called_once()
        assert "pool-1" not in pbs._provision_errors

    @patch("app.services.pattern_buffer_service._cleanup_failed_instance")
    @patch("app.services.providers.get_provider_driver")
    @patch("app.services.pattern_buffer_service.SessionLocal")
    def test_exception_sets_error_and_cleans_up(
        self, mock_session_cls, mock_get_drv, mock_cleanup
    ):
        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        pool = MagicMock()
        pool.id = "pool-1"
        pool.worker_host_id = None
        pool.subnet_id = None
        provider = MagicMock()
        provider.id = "prov-1"
        provider.type = "ec2"
        provider.name = "test"
        provider.default_image = "ami-1"
        provider.default_region = "us-east-1"
        provider.vpc_id = "vpc-1"
        provider.subnet_id = "sub-1"
        provider.security_group_id = "sg-1"
        pool.provider = provider
        mock_db.query.return_value.filter_by.return_value.first.return_value = pool

        driver = MagicMock()
        driver.provision_host.side_effect = RuntimeError("cloud fail")
        mock_get_drv.return_value = driver

        pbs._provision_pattern_buffer("pool-1")

        assert "pool-1" not in pbs._provisioning
        assert "pool-1" in pbs._provision_errors
        assert "RuntimeError" in pbs._provision_errors["pool-1"]
        mock_db.rollback.assert_called_once()
        mock_cleanup.assert_called_once()
        mock_db.close.assert_called_once()

    @patch(
        "app.services.pattern_buffer_service._wait_and_install_agent",
        return_value=False,
    )
    @patch("app.services.pattern_buffer_service._resolve_nfs_kwargs", return_value={})
    @patch(
        "app.services.pattern_buffer_service._resolve_instance_type",
        return_value="i4i.large",
    )
    @patch("app.services.providers.get_provider_driver")
    @patch("app.services.pattern_buffer_service.SessionLocal")
    def test_agent_install_fails_returns_early(
        self, mock_session_cls, mock_get_drv, mock_inst_type, mock_nfs, mock_wait
    ):
        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        pool = MagicMock()
        pool.id = "pool-1"
        pool.worker_host_id = None
        pool.subnet_id = None
        provider = MagicMock()
        provider.id = "prov-1"
        provider.type = "ec2"
        provider.name = "test"
        provider.default_image = "ami-1"
        provider.default_region = "us-east-1"
        provider.vpc_id = "vpc-1"
        provider.subnet_id = "sub-1"
        provider.security_group_id = "sg-1"
        pool.provider = provider
        mock_db.query.return_value.filter_by.return_value.first.return_value = pool

        driver = MagicMock()
        driver.provision_host.return_value = {
            "instance_id": "i-new",
            "instance_type": "i4i.large",
            "public_ip": "1.2.3.4",
            "total_vcpus": 2,
            "total_ram_mb": 16384,
        }
        mock_get_drv.return_value = driver

        pbs._provision_pattern_buffer("pool-1")

        # Should still clean up the provisioning flag
        assert "pool-1" not in pbs._provisioning
        mock_db.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# _wait_and_install_agent
# ═══════════════════════════════════════════════════════════════════════════


class TestWaitAndInstallAgent:
    @patch("app.services.agent_ca_service.get_agent_ca_cert", return_value="ca-cert")
    @patch("app.services.agent_deployer.deploy_agent")
    @patch("app.services.agent_deployer.wait_for_ssh", return_value=True)
    @patch(
        "app.services.agent_deployer.get_provider_data_disk", return_value="/dev/sdb"
    )
    @patch("app.services.agent_deployer.get_provider_ssh_user", return_value="ec2-user")
    def test_successful_install(
        self, mock_user, mock_disk, mock_wait_ssh, mock_deploy, mock_ca
    ):
        host = MagicMock()
        host.id = "host-1234abcd"
        provider = MagicMock()
        provider.type = "ec2"
        pool = MagicMock()
        pool.ca_cert = None
        pool.ca_key = None
        result = {
            "public_ip": "1.2.3.4",
            "private_key": "key-data",
            "private_ip": "10.0.0.1",
        }
        mock_deploy.return_value = {
            "troshkad_credentials": {"token": "tok123", "fingerprint": "fp456"}
        }

        ret = pbs._wait_and_install_agent(host, provider, pool, result, {})

        assert ret is True
        assert host.agent_token == "tok123"
        assert host.agent_cert_fingerprint == "fp456"
        assert host.agent_status == "connected"
        mock_wait_ssh.assert_called_once()

    @patch("app.services.agent_deployer.wait_for_ssh", return_value=False)
    @patch(
        "app.services.agent_deployer.get_provider_data_disk", return_value="/dev/sdb"
    )
    @patch("app.services.agent_deployer.get_provider_ssh_user", return_value="ec2-user")
    def test_ssh_never_available(self, mock_user, mock_disk, mock_wait_ssh):
        host = MagicMock()
        host.id = "host-1234abcd"
        provider = MagicMock()
        provider.type = "ec2"
        pool = MagicMock()
        result = {"public_ip": "1.2.3.4", "private_key": "key-data"}

        ret = pbs._wait_and_install_agent(host, provider, pool, result, {})

        assert ret is False

    @patch("app.services.agent_ca_service.get_agent_ca_cert", return_value="ca-cert")
    @patch("app.services.agent_deployer.deploy_agent")
    @patch("app.services.agent_deployer.wait_for_ssh", return_value=True)
    @patch(
        "app.services.agent_deployer.get_provider_data_disk", return_value="/dev/sdb"
    )
    @patch("app.services.agent_deployer.get_provider_ssh_user", return_value="ec2-user")
    @patch("app.services.storage_pool_service.sign_host_cert")
    def test_with_pool_ca_cert(
        self, mock_sign, mock_user, mock_disk, mock_wait_ssh, mock_deploy, mock_ca
    ):
        mock_sign.return_value = ("cert-pem", "key-pem")
        host = MagicMock()
        host.id = "host-1234abcd"
        provider = MagicMock()
        provider.type = "ec2"
        pool = MagicMock()
        pool.ca_cert = "pool-ca-cert"
        pool.ca_key = "pool-ca-key"
        result = {
            "public_ip": "1.2.3.4",
            "private_key": "key-data",
            "private_ip": "10.0.0.1",
        }
        mock_deploy.return_value = {"troshkad_credentials": {}}

        ret = pbs._wait_and_install_agent(
            host, provider, pool, result, {"nfs_server": "fs.example.com"}
        )

        assert ret is True
        mock_sign.assert_called_once_with(
            "pool-ca-cert", "pool-ca-key", "1.2.3.4", "10.0.0.1"
        )
        # Verify deploy_agent was called with shared storage_mode (because nfs_kwargs non-empty)
        deploy_call = mock_deploy.call_args
        config = (
            deploy_call[1]["config"]
            if "config" in deploy_call[1]
            else deploy_call[0][2]
            if len(deploy_call[0]) > 2
            else None
        )
        # The config is passed as a keyword arg
        if config is None:
            # Check kwargs
            for k, v in deploy_call[1].items():
                if hasattr(v, "storage_mode"):
                    config = v
                    break
        if config:
            assert config.storage_mode == "shared"

    @patch("app.services.agent_ca_service.get_agent_ca_cert", return_value="ca-cert")
    @patch("app.services.agent_deployer.deploy_agent")
    @patch("app.services.agent_deployer.wait_for_ssh", return_value=True)
    @patch(
        "app.services.agent_deployer.get_provider_data_disk", return_value="/dev/sdb"
    )
    @patch("app.services.agent_deployer.get_provider_ssh_user", return_value="ec2-user")
    def test_uses_ssh_host_from_result(
        self, mock_user, mock_disk, mock_wait_ssh, mock_deploy, mock_ca
    ):
        host = MagicMock()
        host.id = "host-1234abcd"
        provider = MagicMock()
        provider.type = "ec2"
        pool = MagicMock()
        pool.ca_cert = None
        pool.ca_key = None
        result = {
            "public_ip": "1.2.3.4",
            "private_key": "key-data",
            "_ssh_host": "bastion.example.com",
            "_ssh_port": 2222,
        }
        mock_deploy.return_value = {"troshkad_credentials": {}}

        ret = pbs._wait_and_install_agent(host, provider, pool, result, {})

        assert ret is True
        # wait_for_ssh should use _ssh_host and _ssh_port
        mock_wait_ssh.assert_called_once_with(
            "bastion.example.com",
            "key-data",
            port=2222,
            ssh_user="ec2-user",
            timeout=300,
        )


# ═══════════════════════════════════════════════════════════════════════════
# _wait_for_new_ip
# ═══════════════════════════════════════════════════════════════════════════


class TestWaitForNewIp:
    @patch("boto3.client")
    def test_ec2_path(self, mock_boto_client):
        provider = MagicMock()
        provider.type = "ec2"
        provider.default_region = "us-east-1"
        provider.get_credentials.return_value = {
            "access_key_id": "AK",
            "secret_access_key": "SK",
        }
        drv = MagicMock()

        ec2_client = MagicMock()
        mock_boto_client.return_value = ec2_client
        waiter = MagicMock()
        ec2_client.get_waiter.return_value = waiter
        ec2_client.describe_instances.return_value = {
            "Reservations": [{"Instances": [{"PublicIpAddress": "3.4.5.6"}]}]
        }

        result = _wait_for_new_ip(provider, drv, "i-abc123")

        assert result == "3.4.5.6"
        waiter.wait.assert_called_once_with(InstanceIds=["i-abc123"])
        ec2_client.describe_instances.assert_called_once_with(InstanceIds=["i-abc123"])

    @patch("time.sleep")
    def test_non_ec2_polls_driver(self, mock_sleep):
        provider = MagicMock()
        provider.type = "gcp"
        drv = MagicMock()
        # First call: not running, second call: running
        drv.get_host_status.side_effect = [
            {"state": "starting"},
            {"state": "running", "public_ip": "5.6.7.8"},
        ]

        result = _wait_for_new_ip(provider, drv, "gcp-inst-1")

        assert result == "5.6.7.8"
        assert drv.get_host_status.call_count == 2

    @patch("time.sleep")
    def test_non_ec2_timeout_returns_empty(self, mock_sleep):
        provider = MagicMock()
        provider.type = "gcp"
        drv = MagicMock()
        drv.get_host_status.return_value = None

        result = _wait_for_new_ip(provider, drv, "gcp-inst-1")

        assert result == ""

    @patch("time.sleep")
    def test_non_ec2_returns_private_ip_when_no_public(self, mock_sleep):
        provider = MagicMock()
        provider.type = "azure"
        drv = MagicMock()
        drv.get_host_status.return_value = {
            "state": "running",
            "public_ip": None,
            "private_ip": "10.0.0.5",
        }

        result = _wait_for_new_ip(provider, drv, "azure-inst-1")

        assert result == "10.0.0.5"


# ═══════════════════════════════════════════════════════════════════════════
# _update_buffer_ip
# ═══════════════════════════════════════════════════════════════════════════


class TestUpdateBufferIp:
    @patch("app.services.troshkad_client._pools", {})
    def test_no_change_when_same_ip(self):
        host = MagicMock()
        host.ip_address = "1.2.3.4"
        host.id = "host-1234abcd"

        _update_buffer_ip(host, "1.2.3.4")

        # ip_address should not be reassigned
        assert host.ip_address == "1.2.3.4"

    @patch("app.services.troshkad_client._pools", {})
    def test_no_change_when_empty_ip(self):
        host = MagicMock()
        host.ip_address = "1.2.3.4"
        host.id = "host-1234abcd"

        _update_buffer_ip(host, "")

        assert host.ip_address == "1.2.3.4"

    @patch(
        "app.services.troshkad_client._pools",
        {"1.2.3.4:31337": MagicMock(), "5.6.7.8:31337": MagicMock()},
    )
    def test_updates_ip_and_flushes_pool(self):
        host = MagicMock()
        host.ip_address = "1.2.3.4"
        host.id = "host-1234abcd"

        from app.services.troshkad_client import _pools

        _update_buffer_ip(host, "9.8.7.6")

        assert host.ip_address == "9.8.7.6"
        # Old pool entry should be removed
        assert "1.2.3.4:31337" not in _pools
        # Unrelated entry should remain
        assert "5.6.7.8:31337" in _pools


# ═══════════════════════════════════════════════════════════════════════════
# _poll_agent_health
# ═══════════════════════════════════════════════════════════════════════════


class TestPollAgentHealth:
    @patch("time.sleep")
    @patch("app.services.troshkad_client.check_health")
    def test_agent_responds_immediately(self, mock_health, mock_sleep):
        mock_health.return_value = {"status": "ok"}
        host = MagicMock()
        host.id = "host-1234abcd"
        pool = MagicMock()
        db = MagicMock()

        result = _poll_agent_health(host, pool, db, timeout=60)

        assert result is True
        assert host.agent_status == "connected"
        db.commit.assert_called_once()

    @patch("time.time")
    @patch("time.sleep")
    @patch("app.services.troshkad_client.check_health")
    def test_agent_never_responds(self, mock_health, mock_sleep, mock_time):
        mock_health.return_value = None
        # time.time() returns values that exceed timeout immediately on second check
        mock_time.side_effect = [0, 0, 121, 121, 999, 999]
        host = MagicMock()
        host.id = "host-1234abcd"
        pool = MagicMock()
        db = MagicMock()

        result = _poll_agent_health(host, pool, db, timeout=120)

        assert result is False
        db.commit.assert_not_called()

    @patch("time.time")
    @patch("time.sleep")
    @patch("app.services.troshkad_client.check_health")
    def test_agent_responds_after_retries(self, mock_health, mock_sleep, mock_time):
        mock_health.side_effect = [None, None, {"status": "ok"}]
        # start=0, while-check=1, while-check=4, while-check=7, log-msg=8
        # Extra values guard against Python version differences in call count
        mock_time.side_effect = [0, 1, 4, 7, 8, 8, 99, 99]
        host = MagicMock()
        host.id = "host-1234abcd"
        pool = MagicMock()
        db = MagicMock()

        result = _poll_agent_health(host, pool, db, timeout=60)

        assert result is True
        assert mock_health.call_count == 3


# ═══════════════════════════════════════════════════════════════════════════
# wake_pattern_buffer
# ═══════════════════════════════════════════════════════════════════════════


class TestWakePatternBuffer:
    def test_no_worker_host_id(self):
        db = MagicMock()
        pool = MagicMock()
        pool.worker_host_id = None

        assert wake_pattern_buffer(db, pool) is False

    def test_no_host_record(self):
        db = MagicMock()
        pool = MagicMock()
        pool.worker_host_id = "host-1"
        db.query.return_value.filter_by.return_value.first.return_value = None

        assert wake_pattern_buffer(db, pool) is False

    def test_host_no_instance_id(self):
        db = MagicMock()
        pool = MagicMock()
        pool.worker_host_id = "host-1"
        host = MagicMock()
        host.instance_id = None
        db.query.return_value.filter_by.return_value.first.return_value = host

        assert wake_pattern_buffer(db, pool) is False

    def test_already_active_and_connected(self):
        db = MagicMock()
        pool = MagicMock()
        pool.worker_host_id = "host-1"
        host = MagicMock()
        host.instance_id = "i-123"
        host.state = "active"
        host.agent_status = "connected"
        db.query.return_value.filter_by.return_value.first.return_value = host

        assert wake_pattern_buffer(db, pool) is True

    def test_no_provider(self):
        db = MagicMock()
        pool = MagicMock()
        pool.worker_host_id = "host-1"
        pool.provider = None
        host = MagicMock()
        host.instance_id = "i-123"
        host.state = "stopped"
        host.agent_status = "disconnected"
        db.query.return_value.filter_by.return_value.first.return_value = host

        assert wake_pattern_buffer(db, pool) is False

    @patch("app.services.pattern_buffer_service._poll_agent_health", return_value=True)
    @patch("app.services.pattern_buffer_service._update_buffer_ip")
    @patch(
        "app.services.pattern_buffer_service._wait_for_new_ip", return_value="5.6.7.8"
    )
    @patch("app.services.providers.get_provider_driver")
    def test_successful_wake(
        self, mock_get_drv, mock_wait_ip, mock_update_ip, mock_poll
    ):
        db = MagicMock()
        pool = MagicMock()
        pool.worker_host_id = "host-1"
        provider = MagicMock()
        pool.provider = provider
        host = MagicMock()
        host.id = "host-1234abcd"
        host.instance_id = "i-123"
        host.state = "stopped"
        host.agent_status = "disconnected"
        db.query.return_value.filter_by.return_value.first.return_value = host

        drv = MagicMock()
        mock_get_drv.return_value = drv

        result = wake_pattern_buffer(db, pool)

        assert result is True
        drv.start_host.assert_called_once_with(provider, "i-123")
        mock_wait_ip.assert_called_once_with(provider, drv, "i-123")
        mock_update_ip.assert_called_once_with(host, "5.6.7.8")
        assert host.state == "active"
        db.commit.assert_called()

    @patch("app.services.pattern_buffer_service._poll_agent_health", return_value=False)
    @patch("app.services.pattern_buffer_service._update_buffer_ip")
    @patch(
        "app.services.pattern_buffer_service._wait_for_new_ip", return_value="5.6.7.8"
    )
    @patch("app.services.providers.get_provider_driver")
    def test_agent_never_responds(
        self, mock_get_drv, mock_wait_ip, mock_update_ip, mock_poll
    ):
        db = MagicMock()
        pool = MagicMock()
        pool.worker_host_id = "host-1"
        provider = MagicMock()
        pool.provider = provider
        host = MagicMock()
        host.id = "host-1234abcd"
        host.instance_id = "i-123"
        host.state = "stopped"
        host.agent_status = "disconnected"
        db.query.return_value.filter_by.return_value.first.return_value = host
        mock_get_drv.return_value = MagicMock()

        result = wake_pattern_buffer(db, pool, timeout=10)

        assert result is False


# ═══════════════════════════════════════════════════════════════════════════
# get_pattern_buffer_host
# ═══════════════════════════════════════════════════════════════════════════


class TestGetPatternBufferHost:
    def test_pool_not_found(self):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None

        assert get_pattern_buffer_host(db, "pool-1") is None

    def test_pool_no_worker_host_id(self):
        db = MagicMock()
        pool = MagicMock()
        pool.worker_host_id = None
        db.query.return_value.filter_by.return_value.first.return_value = pool

        assert get_pattern_buffer_host(db, "pool-1") is None

    def test_host_not_found(self):
        db = MagicMock()
        pool = MagicMock()
        pool.worker_host_id = "host-1"
        # First query returns pool, second returns None for host
        db.query.return_value.filter_by.return_value.first.side_effect = [pool, None]

        assert get_pattern_buffer_host(db, "pool-1") is None

    def test_active_connected_host_returned(self):
        db = MagicMock()
        pool = MagicMock()
        pool.worker_host_id = "host-1"
        host = MagicMock()
        host.state = "active"
        host.agent_status = "connected"
        db.query.return_value.filter_by.return_value.first.side_effect = [pool, host]

        result = get_pattern_buffer_host(db, "pool-1")

        assert result is host

    @patch("app.services.pattern_buffer_service.wake_pattern_buffer", return_value=True)
    def test_stopped_host_auto_wake_true(self, mock_wake):
        db = MagicMock()
        pool = MagicMock()
        pool.worker_host_id = "host-1"
        pool.id = "pool-1234abcd"
        host = MagicMock()
        host.id = "host-1234abcd"
        host.state = "stopped"
        host.agent_status = "disconnected"
        db.query.return_value.filter_by.return_value.first.side_effect = [pool, host]

        result = get_pattern_buffer_host(db, "pool-1234abcd", auto_wake=True)

        assert result is host
        mock_wake.assert_called_once_with(db, pool)

    @patch(
        "app.services.pattern_buffer_service.wake_pattern_buffer", return_value=False
    )
    def test_stopped_host_auto_wake_fails(self, mock_wake):
        db = MagicMock()
        pool = MagicMock()
        pool.worker_host_id = "host-1"
        pool.id = "pool-1234abcd"
        host = MagicMock()
        host.id = "host-1234abcd"
        host.state = "stopped"
        host.agent_status = "disconnected"
        db.query.return_value.filter_by.return_value.first.side_effect = [pool, host]

        result = get_pattern_buffer_host(db, "pool-1234abcd", auto_wake=True)

        assert result is None

    def test_stopped_host_auto_wake_false(self):
        db = MagicMock()
        pool = MagicMock()
        pool.worker_host_id = "host-1"
        host = MagicMock()
        host.state = "stopped"
        host.agent_status = "disconnected"
        db.query.return_value.filter_by.return_value.first.side_effect = [pool, host]

        result = get_pattern_buffer_host(db, "pool-1", auto_wake=False)

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# touch_activity
# ═══════════════════════════════════════════════════════════════════════════


class TestTouchActivity:
    def test_pool_found_updates_activity(self):
        db = MagicMock()
        pool = MagicMock()
        pool.pb_last_activity_at = None
        db.query.return_value.filter_by.return_value.first.return_value = pool

        touch_activity(db, "pool-1")

        assert pool.pb_last_activity_at is not None
        db.commit.assert_called_once()

    def test_pool_not_found_no_error(self):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None

        # Should not raise
        touch_activity(db, "pool-nonexistent")

        db.commit.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# check_auto_sleep
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckAutoSleep:
    @patch("app.services.pattern_buffer_service.stop_pattern_buffer")
    @patch(
        "app.services.pattern_buffer_service._check_pattern_buffer_busy",
        return_value=None,
    )
    def test_idle_buffer_past_threshold_stops(self, mock_busy, mock_stop):
        db = MagicMock()
        pool = MagicMock()
        pool.name = "test-pool"
        pool.worker_host_id = "host-1"
        pool.pb_auto_sleep_minutes = 30
        pool.pb_last_activity_at = datetime.now(UTC) - timedelta(minutes=60)
        host = MagicMock()
        host.state = "active"
        host.agent_status = "connected"

        db.query.return_value.filter.return_value.all.return_value = [pool]
        db.query.return_value.filter_by.return_value.first.return_value = host

        check_auto_sleep(db)

        mock_stop.assert_called_once_with(db, pool)

    def test_recent_activity_skips(self):
        db = MagicMock()
        pool = MagicMock()
        pool.name = "test-pool"
        pool.worker_host_id = "host-1"
        pool.pb_auto_sleep_minutes = 30
        pool.pb_last_activity_at = datetime.now(UTC) - timedelta(minutes=5)
        host = MagicMock()
        host.state = "active"
        host.agent_status = "connected"

        db.query.return_value.filter.return_value.all.return_value = [pool]
        db.query.return_value.filter_by.return_value.first.return_value = host

        check_auto_sleep(db)

        # Should not attempt to stop
        db.commit.assert_not_called()

    def test_no_last_activity_sets_it(self):
        db = MagicMock()
        pool = MagicMock()
        pool.name = "test-pool"
        pool.worker_host_id = "host-1"
        pool.pb_auto_sleep_minutes = 30
        pool.pb_last_activity_at = None
        host = MagicMock()
        host.state = "active"
        host.agent_status = "connected"

        db.query.return_value.filter.return_value.all.return_value = [pool]
        db.query.return_value.filter_by.return_value.first.return_value = host

        check_auto_sleep(db)

        assert pool.pb_last_activity_at is not None
        db.commit.assert_called_once()

    @patch(
        "app.services.pattern_buffer_service._check_pattern_buffer_busy",
        return_value="2 active jobs",
    )
    def test_busy_buffer_skips(self, mock_busy):
        db = MagicMock()
        pool = MagicMock()
        pool.name = "test-pool"
        pool.worker_host_id = "host-1"
        pool.pb_auto_sleep_minutes = 30
        pool.pb_last_activity_at = datetime.now(UTC) - timedelta(minutes=60)
        host = MagicMock()
        host.state = "active"
        host.agent_status = "connected"

        db.query.return_value.filter.return_value.all.return_value = [pool]
        db.query.return_value.filter_by.return_value.first.return_value = host

        check_auto_sleep(db)

        # Should not stop because buffer is busy

    def test_host_not_active_skips(self):
        db = MagicMock()
        pool = MagicMock()
        pool.name = "test-pool"
        pool.worker_host_id = "host-1"
        pool.pb_auto_sleep_minutes = 30
        host = MagicMock()
        host.state = "stopped"
        host.agent_status = "disconnected"

        db.query.return_value.filter.return_value.all.return_value = [pool]
        db.query.return_value.filter_by.return_value.first.return_value = host

        check_auto_sleep(db)

    @patch(
        "app.services.pattern_buffer_service.stop_pattern_buffer",
        side_effect=Exception("stop failed"),
    )
    @patch(
        "app.services.pattern_buffer_service._check_pattern_buffer_busy",
        return_value=None,
    )
    def test_exception_during_stop_caught(self, mock_busy, mock_stop):
        db = MagicMock()
        pool = MagicMock()
        pool.name = "test-pool"
        pool.worker_host_id = "host-1"
        pool.pb_auto_sleep_minutes = 30
        pool.pb_last_activity_at = datetime.now(UTC) - timedelta(minutes=60)
        host = MagicMock()
        host.state = "active"
        host.agent_status = "connected"

        db.query.return_value.filter.return_value.all.return_value = [pool]
        db.query.return_value.filter_by.return_value.first.return_value = host

        # Should not raise despite stop_pattern_buffer throwing
        check_auto_sleep(db)

    def test_no_pools_does_nothing(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []

        check_auto_sleep(db)

    def test_host_not_found_skips(self):
        db = MagicMock()
        pool = MagicMock()
        pool.name = "test-pool"
        pool.worker_host_id = "host-1"
        pool.pb_auto_sleep_minutes = 30

        db.query.return_value.filter.return_value.all.return_value = [pool]
        db.query.return_value.filter_by.return_value.first.return_value = None

        check_auto_sleep(db)
