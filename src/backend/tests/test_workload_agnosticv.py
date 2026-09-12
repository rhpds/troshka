# src/backend/tests/test_workload_agnosticv.py
import json
from unittest.mock import patch

import pytest

from app.services.workloads import agnosticv

# --- pure: path -> catalog id (ci-inspector _resolve_binder_crd_name vectors) ---


@pytest.mark.parametrize(
    "path,default_stage,expected",
    [
        (
            "agd_v2/ocp-cluster-cnv-pools/event",
            "prod",
            ("agd-v2.ocp-cluster-cnv-pools", "event"),
        ),
        (
            "agd_v2/ocp-cluster-cnv-pools",
            "prod",
            ("agd-v2.ocp-cluster-cnv-pools", "prod"),
        ),
        ("agd_v2/my_component/event", "prod", ("agd-v2.my-component", "event")),
        ("AgD_V2/My-Component/Event", "prod", ("agd-v2.my-component", "event")),
        ("my-component", "dev", ("my-component", "dev")),
    ],
)
def test_path_to_catalog_id(path, default_stage, expected):
    assert agnosticv.path_to_catalog_id(path, default_stage) == expected


def test_build_catalog_index_maps_id_stage_to_path():
    paths = [
        "agd_v2/aap-multiinstance-workshop/prod.yaml",
        "agd_v2/aap-multiinstance-workshop/dev.yaml",
    ]
    with patch.object(agnosticv, "list_item_paths", return_value=paths):
        index = agnosticv.build_catalog_index("/root")
    assert (
        index["agd-v2.aap-multiinstance-workshop.prod"]
        == "agd_v2/aap-multiinstance-workshop/prod.yaml"
    )
    assert (
        index["agd-v2.aap-multiinstance-workshop.dev"]
        == "agd_v2/aap-multiinstance-workshop/dev.yaml"
    )


def test_resolve_path_found_and_missing():
    paths = ["agd_v2/mcp-with-openshift/prod.yaml"]
    with patch.object(agnosticv, "list_item_paths", return_value=paths):
        assert (
            agnosticv.resolve_path("/root", "agd-v2.mcp-with-openshift.prod")
            == "agd_v2/mcp-with-openshift/prod.yaml"
        )
        with pytest.raises(KeyError):
            agnosticv.resolve_path("/root", "agd-v2.nope.prod")


# --- subprocess wrappers (mocked) ---


def test_list_item_paths_parses_json(monkeypatch):
    class _CP:
        stdout = json.dumps(["a/prod.yaml", "b/dev.yaml"])

    calls = {}

    def fake_run(args, **kw):
        calls["args"] = args
        return _CP()

    monkeypatch.setattr(agnosticv.subprocess, "run", fake_run)
    out = agnosticv.list_item_paths("/root")
    assert out == ["a/prod.yaml", "b/dev.yaml"]
    assert "--list" in calls["args"] and "--git=false" in calls["args"]
    assert "/root" in calls["args"]


def test_merge_path_parses_json(monkeypatch):
    class _CP:
        stdout = json.dumps({"foo": "bar", "__meta__": {}})

    def fake_run(args, **kw):
        assert "--merge" in args
        return _CP()

    monkeypatch.setattr(agnosticv.subprocess, "run", fake_run)
    assert agnosticv.merge_path("/root", "x/prod.yaml") == {
        "foo": "bar",
        "__meta__": {},
    }


# --- vault decrypt over merged structure ---


def test_decrypt_vault_strings_walks_structure(monkeypatch):
    monkeypatch.setattr(
        agnosticv,
        "decrypt_vault",
        lambda text, pw: "PLAIN" if text.startswith("$ANSIBLE_VAULT") else text,
    )
    monkeypatch.setattr(
        agnosticv,
        "is_vault",
        lambda t: isinstance(t, str) and t.startswith("$ANSIBLE_VAULT"),
    )
    data = {
        "a": "$ANSIBLE_VAULT;1.1;AES256\nxx",
        "b": ["plain", "$ANSIBLE_VAULT;1.1;AES256\nyy"],
        "c": 3,
    }
    out = agnosticv.decrypt_vault_strings(data, "pw")
    assert out == {"a": "PLAIN", "b": ["plain", "PLAIN"], "c": 3}


# --- resolved item extraction ---


def test_to_resolved_item_extracts_deployer_fields():
    merged = {
        "requirements_content": {"collections": [{"name": "agnosticd.core_workloads"}]},
        "__meta__": {
            "deployer": {
                "scm_ref": "v1.2.3",
                "execution_environment": {"image": "quay.io/agnosticd/ee-multicloud:x"},
            }
        },
    }
    item = agnosticv.to_resolved_item(merged)
    assert item.ee_image == "quay.io/agnosticd/ee-multicloud:x"
    assert item.scm_ref == "v1.2.3"
    assert item.requirements_content == {
        "collections": [{"name": "agnosticd.core_workloads"}]
    }
    assert item.extra_vars is merged


def test_to_resolved_item_missing_deployer_is_none():
    item = agnosticv.to_resolved_item({"foo": "bar"})
    assert (
        item.ee_image is None
        and item.scm_ref is None
        and item.requirements_content is None
    )


import shutil
import subprocess as _sp


@pytest.mark.skipif(shutil.which("agnosticv") is None, reason="agnosticv binary absent")
def test_real_agnosticv_merge_roundtrip(tmp_path):
    # Minimal agnosticv tree: root common.yaml + one overlay item with prod.yaml.
    root = tmp_path
    (root / "common.yaml").write_text("root_var: from_root\n")
    config_content = """---
root_dir: .
account_dir: agd_v2
related_files: []
"""
    (root / ".agnosticv.yaml").write_text(config_content)
    item = root / "agd_v2" / "sample-item"
    item.mkdir(parents=True)
    (root / "agd_v2" / "account.yaml").write_text("account_var: acct\n")
    (item / "common.yaml").write_text("item_var: base\n")
    (item / "prod.yaml").write_text("item_var: prod_override\n")

    # agnosticv requires a git repository to find the root
    _sp.run(["git", "init"], cwd=root, check=True, capture_output=True)
    _sp.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    _sp.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)

    paths = agnosticv.list_item_paths(str(root))
    assert "agd_v2/sample-item/prod.yaml" in paths
    rel = agnosticv.resolve_path(str(root), "agd-v2.sample-item.prod")
    merged = agnosticv.merge_path(str(root), rel)
    assert merged["root_var"] == "from_root"
    assert merged["item_var"] == "prod_override"  # stage overrides base
