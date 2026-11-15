"""Empty hosts and unavailable inspection cannot disappear from release gates."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def fleet():
    path = Path(__file__).resolve().parents[1] / "collector_fleet_preflight.py"
    spec = importlib.util.spec_from_file_location("collector_fleet_preflight", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot():
    return {
        "hosts": [{"name": "empty-host", "provider_id": "provider"}],
        "profiles": [{"domain_id": "domain", "providers": ["provider"],
                      "accepted": True, "enrolled": True}],
    }


def test_empty_configured_host_requires_profile(fleet):
    value = snapshot()
    value["profiles"] = []
    report = fleet.evaluate(value)
    assert report["ready"] is False
    assert report["hosts"][0]["blockers"] == ["missing_profile"]


@pytest.mark.parametrize("field,code", [
    ("accepted", "unapproved_profile"), ("enrolled", "missing_enrollment"),
])
def test_missing_evidence_is_a_blocker(fleet, field, code):
    value = snapshot()
    value["profiles"][0][field] = False
    assert code in fleet.evaluate(value)["hosts"][0]["blockers"]


def test_duplicate_profiles_cannot_choose_an_arbitrary_domain(fleet):
    value = snapshot()
    value["profiles"] *= 2
    assert fleet.evaluate(value)["hosts"][0]["blockers"] == ["ambiguous_profile"]


def test_valid_configuration_is_not_accounting_verification(fleet):
    report = fleet.evaluate(snapshot())
    assert report["ready"] is True
    assert report["scope"] == "configuration_only"


def test_failure_is_reported_without_leaking_command_output(fleet, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private-token")
    monkeypatch.setattr(fleet, "inspect", fail)
    report = fleet.check_applications([("config", "app")])
    assert report["ready"] is False
    assert report["applications"][0]["blockers"] == ["inspection_unavailable"]
    assert "private-token" not in json.dumps(report)


def test_all_applications_are_reported_even_after_failure(fleet, monkeypatch):
    def inspect(config, namespace):
        if namespace == "first":
            raise RuntimeError("unreachable")
        return snapshot()
    monkeypatch.setattr(fleet, "inspect", inspect)
    report = fleet.check_applications([("config", "first"), ("config", "second")])
    assert len(report["applications"]) == 2
    assert report["applications"][1]["ready"] is True
    assert report["ready"] is False


def test_failed_fleet_preflight_prevents_push_and_promotion(tmp_path):
    calls = tmp_path / "calls"
    for name, body in {
        "git": 'echo "git $*" >> "$CALLS"\ncase "$1" in status) exit 0;; rev-parse) echo revision; exit 0;; *) exit 97;; esac',
        "python3": 'echo "python3 $*" >> "$CALLS"\nexit 1',
    }.items():
        path = tmp_path / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o700)
    script = Path(__file__).resolve().parents[1] / "deploy-full.sh"
    result = subprocess.run(
        ["bash", str(script), "--tag", "vtest"], text=True, capture_output=True, check=False,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
             "CALLS": str(calls)}, timeout=10,
    )
    assert result.returncode != 0
    commands = calls.read_text()
    assert "collector_fleet_preflight.py" in commands
    assert "git tag" not in commands
    assert "git push" not in commands
    assert "deploy_release.py images" not in commands
