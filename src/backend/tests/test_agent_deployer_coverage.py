"""Tests for app/services/agent_deployer.py — provider helpers, SSH wait,
SCP, install-script runner, and the main deploy_agent orchestrator."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app.services.agent_deployer import (
    AgentDeployConfig,
    _run_install_script_via_ssh,
    _scp_file_to_host,
    deploy_agent,
    get_provider_data_disk,
    get_provider_ssh_port,
    get_provider_ssh_user,
    wait_for_ssh,
)

# ── get_provider_ssh_user ──


class TestGetProviderSshUser:
    def test_ec2(self):
        assert get_provider_ssh_user("ec2") == "ec2-user"

    def test_ocpvirt(self):
        assert get_provider_ssh_user("ocpvirt") == "cloud-user"

    def test_gcp(self):
        assert get_provider_ssh_user("gcp") == "troshka"

    def test_azure(self):
        assert get_provider_ssh_user("azure") == "troshka"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown provider type"):
            get_provider_ssh_user("unknown")


# ── get_provider_ssh_port ──


class TestGetProviderSshPort:
    @patch("app.services.agent_deployer.subprocess")  # prevent real import side-effects
    def test_ocpvirt(self, _):
        with patch("app.services.providers.ocpvirt.SSH_LB_PORT", 22000):
            assert get_provider_ssh_port("ocpvirt") == 22000

    def test_ec2(self):
        assert get_provider_ssh_port("ec2") == 22

    def test_gcp(self):
        assert get_provider_ssh_port("gcp") == 22

    def test_azure(self):
        assert get_provider_ssh_port("azure") == 22


# ── get_provider_data_disk ──


class TestGetProviderDataDisk:
    def test_ec2(self):
        assert get_provider_data_disk("ec2") == "sdf"

    def test_ocpvirt(self):
        assert get_provider_data_disk("ocpvirt") == "sdf"

    def test_gcp(self):
        assert get_provider_data_disk("gcp") == "/dev/sdb"

    def test_azure(self):
        assert get_provider_data_disk("azure") == "/dev/disk/azure/scsi1/lun0"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown provider type"):
            get_provider_data_disk("unknown")


# ── AgentDeployConfig ──


class TestAgentDeployConfig:
    def test_defaults(self):
        cfg = AgentDeployConfig()
        assert cfg.api_url == ""
        assert cfg.storage_mode == "local"
        assert cfg.nfs_server == ""
        assert cfg.nfs_path == ""
        assert cfg.nfs_port == 0
        assert cfg.ca_cert == ""
        assert cfg.host_cert == ""
        assert cfg.host_key == ""
        assert cfg.console_domain == ""
        assert cfg.vncd_no_tls is False
        assert cfg.host_type == "shared"
        assert cfg.ssh_port == 22
        assert cfg.ssh_user == "ec2-user"
        assert cfg.data_disk_device == "sdf"
        assert cfg.agent_ca_cert == ""

    def test_custom(self):
        cfg = AgentDeployConfig(
            api_url="https://api.example.com",
            storage_mode="shared",
            nfs_server="10.0.0.1",
            nfs_path="/export",
            nfs_port=2049,
            ca_cert="CERT",
            host_cert="HCERT",
            host_key="HKEY",
            console_domain="console.example.com",
            vncd_no_tls=True,
            host_type="pattern_buffer",
            ssh_port=30022,
            ssh_user="cloud-user",
            data_disk_device="/dev/sdb",
            agent_ca_cert="CACERT",
        )
        assert cfg.api_url == "https://api.example.com"
        assert cfg.storage_mode == "shared"
        assert cfg.nfs_server == "10.0.0.1"
        assert cfg.nfs_path == "/export"
        assert cfg.nfs_port == 2049
        assert cfg.ca_cert == "CERT"
        assert cfg.host_cert == "HCERT"
        assert cfg.host_key == "HKEY"
        assert cfg.console_domain == "console.example.com"
        assert cfg.vncd_no_tls is True
        assert cfg.host_type == "pattern_buffer"
        assert cfg.ssh_port == 30022
        assert cfg.ssh_user == "cloud-user"
        assert cfg.data_disk_device == "/dev/sdb"
        assert cfg.agent_ca_cert == "CACERT"


# ── wait_for_ssh ──


class TestWaitForSsh:
    @patch("app.services.agent_deployer.time.sleep")
    @patch("app.services.agent_deployer.subprocess.run")
    def test_success_first_attempt(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stdout="ssh-ready\n", stderr="")
        result = wait_for_ssh("10.0.0.1", "PRIVATE_KEY")
        assert result is True
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch("app.services.agent_deployer.time.sleep")
    @patch("app.services.agent_deployer.subprocess.run")
    def test_success_after_retry(self, mock_run, mock_sleep):
        fail = MagicMock(returncode=255, stdout="", stderr="Connection refused\n")
        ok = MagicMock(returncode=0, stdout="ssh-ready\n", stderr="")
        mock_run.side_effect = [fail, ok]
        result = wait_for_ssh("10.0.0.1", "KEY")
        assert result is True
        assert mock_run.call_count == 2
        mock_sleep.assert_called_once_with(5)

    @patch("app.services.agent_deployer.time.sleep")
    @patch("app.services.agent_deployer.time.time")
    @patch("app.services.agent_deployer.subprocess.run")
    def test_timeout(self, mock_run, mock_time, mock_sleep):
        # start=0, while-check=0 (enter), elapsed=5, while-check=7 (exit, >6)
        # Extra values guard against Python version differences in call count
        mock_time.side_effect = [0, 0, 5, 5, 7, 7, 99, 99]
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="timeout")
        result = wait_for_ssh("10.0.0.1", "KEY", timeout=6)
        assert result is False

    @patch("app.services.agent_deployer.time.sleep")
    @patch("app.services.agent_deployer.subprocess.run")
    def test_custom_port_and_user(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stdout="ssh-ready\n", stderr="")
        result = wait_for_ssh("10.0.0.2", "KEY", port=30022, ssh_user="cloud-user")
        assert result is True
        args = mock_run.call_args[0][0]
        assert "-p" in args
        assert "30022" in args
        assert "cloud-user@10.0.0.2" in args


# ── _scp_file_to_host ──


class TestScpFileToHost:
    @patch("app.services.agent_deployer.subprocess.run")
    @patch("app.services.agent_deployer.os.path.exists", return_value=True)
    def test_success(self, mock_exists, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        result = _scp_file_to_host(
            "/local/troshkad.py",
            "/opt/troshka/troshkad.py",
            "10.0.0.1",
            "ec2-user",
            ["-o", "StrictHostKeyChecking=no"],
            [],
            [],
        )
        assert result is True
        assert mock_run.call_count == 2  # scp + sudo mv

    @patch("app.services.agent_deployer.os.path.exists", return_value=False)
    def test_file_not_found(self, mock_exists):
        result = _scp_file_to_host(
            "/local/missing.py",
            "/opt/troshka/troshkad.py",
            "10.0.0.1",
            "ec2-user",
            [],
            [],
            [],
        )
        assert result is False

    @patch("app.services.agent_deployer.subprocess.run")
    @patch("app.services.agent_deployer.os.path.exists", return_value=True)
    def test_scp_failure(self, mock_exists, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="Permission denied")
        result = _scp_file_to_host(
            "/local/troshkad.py",
            "/opt/troshka/troshkad.py",
            "10.0.0.1",
            "ec2-user",
            [],
            [],
            [],
        )
        assert result is False

    @patch("app.services.agent_deployer.subprocess.run")
    @patch("app.services.agent_deployer.os.path.exists", return_value=True)
    def test_create_parent_dir(self, mock_exists, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        result = _scp_file_to_host(
            "/local/troshkad.py",
            "/opt/troshka/troshkad.py",
            "10.0.0.1",
            "ec2-user",
            ["-o", "StrictHostKeyChecking=no"],
            ["-p", "30022"],
            ["-P", "30022"],
            create_parent_dir=True,
        )
        assert result is True
        # scp + mkdir + sudo mv
        assert mock_run.call_count == 3


# ── _run_install_script_via_ssh ──


class TestRunInstallScriptViaSsh:
    @patch("app.services.agent_deployer.subprocess.run")
    def test_success_with_credentials(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "Installing...\n"
                "TROSHKAD_TOKEN=abc123\n"
                "TROSHKAD_FINGERPRINT=AA:BB:CC\n"
                "Done\n"
            ),
            stderr="",
        )
        result = _run_install_script_via_ssh(
            "#!/bin/bash\necho hello",
            "10.0.0.1",
            "ec2-user",
            ["-o", "StrictHostKeyChecking=no"],
            [],
            1000.0,
        )
        assert result["success"] is True
        assert result["exit_code"] == 0
        assert result["troshkad_credentials"]["token"] == "abc123"
        assert result["troshkad_credentials"]["fingerprint"] == "AA:BB:CC"

    @patch("app.services.agent_deployer.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="error occurred\n", stderr="fatal\n"
        )
        result = _run_install_script_via_ssh(
            "script", "10.0.0.1", "ec2-user", [], [], 1000.0
        )
        assert result["success"] is False
        assert result["exit_code"] == 1
        assert result["troshkad_credentials"] == {}

    @patch("app.services.agent_deployer.subprocess.run")
    def test_timeout(self, mock_run):
        exc = subprocess.TimeoutExpired(cmd="ssh", timeout=300)
        exc.stdout = b"partial output"
        exc.stderr = b"partial err"
        mock_run.side_effect = exc
        result = _run_install_script_via_ssh(
            "script", "10.0.0.1", "ec2-user", [], [], 1000.0
        )
        assert result["success"] is False
        assert result["exit_code"] == -1
        assert result["troshkad_credentials"] == {}

    @patch("app.services.agent_deployer.subprocess.run")
    def test_timeout_none_output(self, mock_run):
        exc = subprocess.TimeoutExpired(cmd="ssh", timeout=300)
        exc.stdout = None
        exc.stderr = None
        mock_run.side_effect = exc
        result = _run_install_script_via_ssh(
            "script", "10.0.0.1", "ec2-user", [], [], 1000.0
        )
        assert result["success"] is False
        assert result["exit_code"] == -1


# ── deploy_agent ──


class TestDeployAgent:
    @patch("app.services.agent_deployer._run_install_script_via_ssh")
    @patch("app.services.agent_deployer._scp_file_to_host")
    @patch("app.services.agent_deployer.os.path.exists", return_value=False)
    @patch("app.services.agent_deployer.subprocess.run")
    def test_default_config(self, mock_run, mock_exists, mock_scp, mock_install):
        mock_scp.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_install.return_value = {
            "success": True,
            "exit_code": 0,
            "output": "TROSHKAD_TOKEN=tok\nTROSHKAD_FINGERPRINT=fp\n",
            "troshkad_credentials": {"token": "tok", "fingerprint": "fp"},
        }
        with patch("app.core.config.config") as mock_cfg:
            mock_cfg.app = MagicMock(external_url="https://troshka.example.com")
            result = deploy_agent("10.0.0.1", "PRIVATE_KEY", "host-id-123")

        assert result["success"] is True
        assert result["troshkad_credentials"]["token"] == "tok"
        # _scp_file_to_host called twice: troshkad.py and vncd.py
        assert mock_scp.call_count == 2

    @patch("app.services.agent_deployer._run_install_script_via_ssh")
    @patch("app.services.agent_deployer._scp_file_to_host")
    @patch("app.services.agent_deployer.os.path.exists", return_value=False)
    @patch("app.services.agent_deployer.subprocess.run")
    def test_custom_config_shared_storage(
        self, mock_run, mock_exists, mock_scp, mock_install
    ):
        mock_scp.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_install.return_value = {
            "success": True,
            "exit_code": 0,
            "output": "",
            "troshkad_credentials": {},
        }
        cfg = AgentDeployConfig(
            storage_mode="shared",
            nfs_server="10.0.0.99",
            nfs_path="/export/data",
            ca_cert="MY_CA_CERT",
            host_cert="MY_HOST_CERT",
            host_key="MY_HOST_KEY",
            console_domain="console.example.com",
            vncd_no_tls=True,
            host_type="shared",
            ssh_port=30022,
            ssh_user="cloud-user",
            agent_ca_cert="AGENT_CA",
        )
        with patch("app.core.config.config") as mock_cfg:
            mock_cfg.app = MagicMock(external_url="https://troshka.example.com")
            result = deploy_agent("10.0.0.2", "KEY", "host-222", config=cfg)

        assert result["success"] is True
        # Verify the install script received correct substitutions
        script_arg = mock_install.call_args[0][0]
        assert "host-222" in script_arg
        assert "shared" in script_arg
        assert "10.0.0.99" in script_arg
        assert "/export/data" in script_arg
        assert "console.example.com" in script_arg

    @patch("app.services.agent_deployer._run_install_script_via_ssh")
    @patch("app.services.agent_deployer._scp_file_to_host")
    @patch("app.services.agent_deployer.os.path.exists", return_value=False)
    @patch("app.services.agent_deployer.subprocess.run")
    def test_pattern_buffer_host_type(
        self, mock_run, mock_exists, mock_scp, mock_install
    ):
        mock_scp.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_install.return_value = {
            "success": True,
            "exit_code": 0,
            "output": "",
            "troshkad_credentials": {},
        }
        cfg = AgentDeployConfig(host_type="pattern_buffer")
        with patch("app.core.config.config") as mock_cfg:
            mock_cfg.app = MagicMock(external_url="https://troshka.example.com")
            result = deploy_agent("10.0.0.3", "KEY", "host-333", config=cfg)

        assert result["success"] is True
        script_arg = mock_install.call_args[0][0]
        # Pattern buffer script has a distinct marker
        assert "Pattern Buffer Agent Installer" in script_arg

    @patch("app.services.agent_deployer._run_install_script_via_ssh")
    @patch("app.services.agent_deployer._scp_file_to_host")
    @patch("app.services.agent_deployer.os.path.exists", return_value=True)
    @patch("app.services.agent_deployer.subprocess.run")
    def test_copies_fs_monitor_when_exists(
        self, mock_run, mock_exists, mock_scp, mock_install
    ):
        mock_scp.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_install.return_value = {
            "success": True,
            "exit_code": 0,
            "output": "",
            "troshkad_credentials": {},
        }
        with patch("app.core.config.config") as mock_cfg:
            mock_cfg.app = MagicMock(external_url="https://troshka.example.com")
            result = deploy_agent("10.0.0.4", "KEY", "host-444")

        assert result["success"] is True
        # When os.path.exists returns True for fs-monitor, subprocess.run is called
        # for the scp + mv of the monitor script
        assert mock_run.call_count >= 2

    @patch("app.services.agent_deployer._run_install_script_via_ssh")
    @patch("app.services.agent_deployer._scp_file_to_host")
    @patch("app.services.agent_deployer.os.path.exists", return_value=False)
    @patch("app.services.agent_deployer.subprocess.run")
    def test_no_external_url_warns(self, mock_run, mock_exists, mock_scp, mock_install):
        mock_scp.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_install.return_value = {
            "success": True,
            "exit_code": 0,
            "output": "",
            "troshkad_credentials": {},
        }
        cfg = AgentDeployConfig(api_url="")
        with patch("app.core.config.config") as mock_cfg:
            mock_cfg.app = MagicMock(external_url="")
            result = deploy_agent("10.0.0.5", "KEY", "host-555", config=cfg)

        assert result["success"] is True

    @patch("app.services.agent_deployer._run_install_script_via_ssh")
    @patch("app.services.agent_deployer._scp_file_to_host")
    @patch("app.services.agent_deployer.os.path.exists", return_value=False)
    @patch("app.services.agent_deployer.subprocess.run")
    def test_script_placeholders_replaced(
        self, mock_run, mock_exists, mock_scp, mock_install
    ):
        mock_scp.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_install.return_value = {
            "success": True,
            "exit_code": 0,
            "output": "",
            "troshkad_credentials": {},
        }
        cfg = AgentDeployConfig(
            api_url="https://api.test",
            storage_mode="local",
            ssh_user="testuser",
            data_disk_device="nvme1n1",
            vncd_no_tls=False,
        )
        with patch("app.core.config.config") as mock_cfg:
            mock_cfg.app = MagicMock(external_url="https://troshka.example.com")
            deploy_agent("10.0.0.6", "KEY", "host-666", config=cfg)

        script_arg = mock_install.call_args[0][0]
        # All placeholders should be replaced
        assert "{host_id}" not in script_arg
        assert "{api_url}" not in script_arg
        assert "{storage_mode}" not in script_arg
        assert "{ssh_user}" not in script_arg
        assert "{data_disk_device}" not in script_arg
        assert "{vncd_no_tls}" not in script_arg
        assert "{agent_ca_cert_b64}" not in script_arg
        # Actual values present
        assert "host-666" in script_arg
        assert "https://api.test" in script_arg
        assert "testuser" in script_arg
        assert "nvme1n1" in script_arg

    @patch("app.services.agent_deployer._run_install_script_via_ssh")
    @patch("app.services.agent_deployer._scp_file_to_host")
    @patch("app.services.agent_deployer.os.path.exists", return_value=False)
    @patch("app.services.agent_deployer.subprocess.run")
    def test_custom_ssh_port_adds_port_opts(
        self, mock_run, mock_exists, mock_scp, mock_install
    ):
        mock_scp.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_install.return_value = {
            "success": True,
            "exit_code": 0,
            "output": "",
            "troshkad_credentials": {},
        }
        cfg = AgentDeployConfig(ssh_port=30022)
        with patch("app.core.config.config") as mock_cfg:
            mock_cfg.app = MagicMock(external_url="https://troshka.example.com")
            deploy_agent("10.0.0.7", "KEY", "host-777", config=cfg)

        # _scp_file_to_host receives port opts
        scp_call_args = mock_scp.call_args_list[0]
        # scp_port_opts is positional arg index 6
        scp_port_opts = scp_call_args[0][6]
        assert scp_port_opts == ["-P", "30022"]
