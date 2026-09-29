"""Tests for Troshka template JSON Schema validation and OpenAPI exposure."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.services.template_schema import (
    TemplateSchemaError,
    load_template_schema,
    validate_template_document,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
_EXAMPLE_TEMPLATES = _REPO_ROOT / "example_templates"


def _template_files() -> list[Path]:
    files = sorted(_BACKEND_TEMPLATES.glob("*.yaml"))
    if _EXAMPLE_TEMPLATES.is_dir():
        files.extend(sorted(_EXAMPLE_TEMPLATES.glob("*.yaml")))
    return files


@pytest.mark.parametrize(
    "path",
    _template_files(),
    ids=lambda p: f"{p.parent.name}/{p.name}",
)
def test_shipped_templates_validate(path: Path):
    doc = yaml.safe_load(path.read_text())
    prepared = validate_template_document(doc)
    assert isinstance(prepared.get("vms"), dict)
    assert isinstance(prepared.get("networks"), dict)


def test_legacy_ocp_mapping_normalized_before_validate():
    doc = {
        "name": "legacy-ocp",
        "networks": {"cluster": {"cidr": "10.0.0.0/24"}},
        "vms": {"cp-0": {"vcpus": 2, "ram_gb": 4, "os": "rhcos"}},
        "ocp": {"name": "ocp", "type": "sno", "ocp_version": "4.22"},
    }
    prepared = validate_template_document(doc)
    assert isinstance(prepared["ocp"], list)
    assert prepared["ocp"][0]["name"] == "ocp"


def test_rejects_unknown_top_level_key():
    doc = {
        "name": "bad",
        "networks": {"mgmt": {"cidr": "10.0.0.0/24"}},
        "vms": {"demo": {"vcpus": 1, "ram_gb": 2}},
        "evil_payload": {"x": 1},
    }
    with pytest.raises(TemplateSchemaError) as ei:
        validate_template_document(doc)
    assert ei.value.errors
    assert any("evil_payload" in e["message"] for e in ei.value.errors)


def test_rejects_missing_vms():
    doc = {"name": "no-vms", "networks": {"mgmt": {"cidr": "10.0.0.0/24"}}}
    with pytest.raises(TemplateSchemaError) as ei:
        validate_template_document(doc)
    assert any("vms" in e["message"].lower() for e in ei.value.errors)


def test_rejects_wrong_vm_types():
    doc = {
        "name": "bad-types",
        "networks": {"mgmt": {"cidr": "10.0.0.0/24"}},
        "vms": {"demo": {"vcpus": "eight", "ram_gb": 2}},
    }
    with pytest.raises(TemplateSchemaError) as ei:
        validate_template_document(doc)
    assert ei.value.errors


def test_rejects_oversized_document(monkeypatch):
    from app.services import template_schema as ts

    monkeypatch.setattr(ts, "_MAX_DOCUMENT_BYTES", 200)
    doc = {
        "name": "huge",
        "description": "x" * 500,
        "networks": {"mgmt": {"cidr": "10.0.0.0/24"}},
        "vms": {"demo": {"vcpus": 1, "ram_gb": 1}},
    }
    with pytest.raises(TemplateSchemaError) as ei:
        validate_template_document(doc)
    assert "maximum size" in str(ei.value).lower()


def test_repo_and_backend_schema_copies_match():
    """Keep repo-root schemas/ and src/backend/schemas/ identical."""
    root = _REPO_ROOT / "schemas" / "troshka-template.schema.json"
    backend = (
        Path(__file__).resolve().parents[1] / "schemas" / "troshka-template.schema.json"
    )
    assert root.is_file() and backend.is_file()
    assert root.read_text() == backend.read_text()


def test_load_template_schema_has_title():
    schema = load_template_schema()
    assert schema.get("title") == "TroshkaTemplate"
    assert "vms" in schema.get("properties", {})


def test_openapi_includes_troshka_template():
    from fastapi.testclient import TestClient

    from app.core.database import get_db
    from app.main import app
    from tests.conftest import get_test_db

    app.dependency_overrides[get_db] = get_test_db
    try:
        client = TestClient(app)
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        body = resp.json()
        assert "TroshkaTemplate" in body.get("components", {}).get("schemas", {})
        # import-template request body should ref TroshkaTemplate
        import_path = body["paths"].get("/api/v1/projects/{project_id}/import-template")
        assert import_path is not None
        schema = import_path["post"]["requestBody"]["content"]["application/json"][
            "schema"
        ]
        assert (
            schema["properties"]["template_yaml"]["$ref"]
            == "#/components/schemas/TroshkaTemplate"
        )
    finally:
        app.dependency_overrides.clear()
        app.openapi_schema = None


def test_templates_schema_endpoint():
    from fastapi.testclient import TestClient

    from app.core.database import get_db
    from app.main import app
    from tests.conftest import get_test_db

    app.dependency_overrides[get_db] = get_test_db
    try:
        client = TestClient(app)
        resp = client.get("/api/v1/templates/schema")
        assert resp.status_code == 200
        assert resp.json().get("title") == "TroshkaTemplate"
    finally:
        app.dependency_overrides.clear()
