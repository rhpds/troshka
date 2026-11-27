"""Empty hosts and unavailable inspection cannot disappear from release gates."""

import importlib.util
import json
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
                      "accepted": True, "enrolled": True, "transport": {"blockers": []}}],
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


def test_expired_or_missing_transport_cannot_pass_fleet_gate(fleet):
    value = snapshot()
    value["profiles"][0]["transport"] = {"blockers": ["collector_token_expired"]}
    assert fleet.evaluate(value)["hosts"][0]["blockers"] == ["collector_token_expired"]
    del value["profiles"][0]["transport"]
    assert not fleet.evaluate(value)["ready"]


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
