import os

import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "templates")


def test_load_base_template():
    from app.services.template_loader import load_template

    tmpl = load_template("ocp-cluster", templates_dir=TEMPLATES_DIR)
    assert tmpl["name"] == "ocp-cluster"
    assert "parameters" in tmpl
    assert "control_count" in tmpl["parameters"]


def test_load_preset_template():
    from app.services.template_loader import load_template

    tmpl = load_template("ocp-compact", templates_dir=TEMPLATES_DIR)
    assert tmpl["name"] == "ocp-compact"
    assert tmpl["extends"] == "ocp-cluster"


def test_resolve_preset_parameters():
    from app.services.template_loader import resolve_template

    resolved = resolve_template(
        "ocp-compact", overrides={}, templates_dir=TEMPLATES_DIR
    )
    assert resolved["control_count"] == 3
    assert resolved["control_schedulable"] is True
    assert resolved["worker_count"] == 0
    assert "parameters" in resolved


def test_resolve_with_overrides():
    from app.services.template_loader import resolve_template

    resolved = resolve_template(
        "ocp-compact",
        overrides={"worker_count": 2, "control_ram_gb": 32},
        templates_dir=TEMPLATES_DIR,
    )
    assert resolved["worker_count"] == 2
    assert resolved["control_ram_gb"] == 32


def test_resolve_rejects_unknown_override():
    from app.services.template_loader import resolve_template

    with pytest.raises(ValueError, match="Unknown parameter"):
        resolve_template(
            "ocp-compact", overrides={"fake_param": 99}, templates_dir=TEMPLATES_DIR
        )


def test_resolve_rejects_below_minimum():
    from app.services.template_loader import resolve_template

    with pytest.raises(ValueError, match="below minimum"):
        resolve_template(
            "ocp-compact", overrides={"control_vcpus": 1}, templates_dir=TEMPLATES_DIR
        )


def test_validate_version():
    from app.services.template_loader import resolve_template

    resolved = resolve_template(
        "ocp-compact", overrides={}, version="4.16", templates_dir=TEMPLATES_DIR
    )
    assert resolved["version"] == "4.16"


def test_validate_version_rejects_invalid():
    from app.services.template_loader import resolve_template

    with pytest.raises(ValueError, match="not available"):
        resolve_template(
            "ocp-compact", overrides={}, version="3.11", templates_dir=TEMPLATES_DIR
        )


def test_load_nonexistent_template():
    from app.services.template_loader import load_template

    with pytest.raises(FileNotFoundError):
        load_template("nonexistent", templates_dir=TEMPLATES_DIR)
