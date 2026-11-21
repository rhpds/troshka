"""Tests for gitignored deploy host resolution."""

import importlib.util
from pathlib import Path

import pytest
import yaml

PATH = Path(__file__).resolve().parents[1] / "deploy_hosts.py"


@pytest.fixture
def hosts():
    spec = importlib.util.spec_from_file_location("deploy_hosts", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_expands_short_names(hosts, tmp_path):
    config = tmp_path / "deploy-hosts.local.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "kubeconfig_dir": str(tmp_path / "secrets"),
                "domain_suffix": "infra.demo.redhat.com",
                "production": "ocpv-infra01.dal12",
                "dev": "ocpvdev01.dal13",
                "operators": ["ocpv01.dfw3", "ocpv06.dal10"],
            }
        )
    )
    resolved = hosts.resolve_hosts(config)
    assert resolved["production"].endswith(
        "ocpv-infra01.dal12.infra.demo.redhat.com.kubeconfig"
    )
    assert resolved["operators"][0].endswith(
        "ocpv01.dfw3.infra.demo.redhat.com.kubeconfig"
    )


def test_accepts_absolute_kubeconfig(hosts, tmp_path):
    kc = tmp_path / "custom.kubeconfig"
    kc.write_text("x")
    config = tmp_path / "deploy-hosts.local.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "kubeconfig_dir": str(tmp_path),
                "domain_suffix": "example.com",
                "production": str(kc),
                "dev": "dev.cluster",
                "operators": [str(kc)],
            }
        )
    )
    resolved = hosts.resolve_hosts(config)
    assert resolved["production"] == str(kc.resolve())
