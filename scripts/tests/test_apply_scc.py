"""apply_scc must never wipe live SCC users on apply."""

import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "apply_scc.py"


@pytest.fixture
def apply_scc():
    spec = importlib.util.spec_from_file_location("apply_scc", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_merge_users_preserves_live_membership(apply_scc):
    doc = {"metadata": {"name": "troshka-gateway"}, "users": []}
    merged = apply_scc.merge_users(
        doc, ["system:serviceaccount:troshka-abc:troshka-bmc"]
    )
    assert merged["users"] == ["system:serviceaccount:troshka-abc:troshka-bmc"]


def test_merge_users_unions_manifest_and_live(apply_scc):
    doc = {
        "metadata": {"name": "troshka-gateway"},
        "users": ["system:serviceaccount:troshka:troshka"],
    }
    merged = apply_scc.merge_users(
        doc, ["system:serviceaccount:troshka-abc:troshka-network"]
    )
    assert set(merged["users"]) == {
        "system:serviceaccount:troshka:troshka",
        "system:serviceaccount:troshka-abc:troshka-network",
    }


def test_merge_users_omits_users_on_create(apply_scc):
    doc = {"metadata": {"name": "troshka-gateway"}, "users": []}
    merged = apply_scc.merge_users(doc, None)
    assert "users" not in merged
