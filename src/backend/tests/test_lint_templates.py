"""Tests for scripts/lint-templates.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "lint-templates.py"


def _load_lint_module():
    spec = importlib.util.spec_from_file_location("lint_templates_script", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Avoid polluting sys.modules permanently under a fixed name.
    sys.modules["lint_templates_script"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def lint_mod():
    return _load_lint_module()


def test_lint_shipped_example_ok(lint_mod):
    path = _REPO_ROOT / "src" / "backend" / "templates" / "example.yaml"
    assert lint_mod.lint_file(path) == []


def test_lint_rejects_unknown_key(lint_mod, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: bad\n"
        "networks:\n  mgmt:\n    cidr: 10.0.0.0/24\n"
        "vms:\n  demo:\n    vcpus: 1\n    ram_gb: 1\n"
        "not_a_real_field: true\n",
        encoding="utf-8",
    )
    errors = lint_mod.lint_file(bad)
    assert errors
    assert any("not_a_real_field" in e["message"] for e in errors)


def test_lint_rejects_invalid_yaml(lint_mod, tmp_path):
    bad = tmp_path / "broken.yaml"
    bad.write_text("vms: [\n", encoding="utf-8")
    errors = lint_mod.lint_file(bad)
    assert errors
    assert "YAML parse error" in errors[0]["message"]


def test_main_defaults_exit_zero(lint_mod, capsys):
    code = lint_mod.main([])
    assert code == 0
    out = capsys.readouterr().out
    assert "template(s) ok" in out


def test_main_fails_on_bad_file(lint_mod, tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\nnetworks: {}\nvms: {}\n", encoding="utf-8")
    code = lint_mod.main([str(bad)])
    assert code == 1
    err_out = capsys.readouterr().out
    assert "FAIL" in err_out
