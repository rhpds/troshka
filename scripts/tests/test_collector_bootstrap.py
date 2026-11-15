"""Deployment bootstrap may create empty domains, never replace project enrollment."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def bootstrap():
    path = Path(__file__).resolve().parents[1] / "collector_bootstrap.py"
    spec = importlib.util.spec_from_file_location("collector_bootstrap", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("pins", [
    {"project_roots": [{"project_id": "existing"}], "empty_domain_bootstrap": {}},
    {"project_roots": [], "empty_domain_bootstrap": None},
    {"project_roots": [], "empty_domain_bootstrap": {}, "initialization_evidence": None},
])
def test_existing_or_unapproved_roots_cannot_be_bootstrapped(bootstrap, pins):
    with pytest.raises(ValueError):
        bootstrap.require_empty_authorization(pins)


def test_explicit_empty_authorization_is_required(bootstrap):
    bootstrap.require_empty_authorization({
        "project_roots": [], "empty_domain_bootstrap": {"version": "approved"},
        "initialization_evidence": {"profile": "approved"},
    })
