"""Troshka template JSON Schema loading and validation."""

from __future__ import annotations

import copy
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

logger = logging.getLogger(__name__)

_SCHEMA_FILENAME = "troshka-template.schema.json"
# Soft cap on serialized document size (defense against oversized import payloads).
_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
_ADDITIONAL_PROPS_RE = re.compile(
    r"Additional properties are not allowed \((.+) (?:was|were) unexpected\)"
)
_QUOTED_KEY_RE = re.compile(r"'([^']+)'")


class TemplateSchemaError(ValueError):
    """Template document failed JSON Schema validation."""

    def __init__(self, message: str, errors: list[dict[str, str]] | None = None):
        super().__init__(message)
        self.errors = errors or []


def _schema_candidates() -> list[Path]:
    """Locations for the canonical schema file (dev tree + container layout)."""
    here = Path(__file__).resolve()
    # app/services → …/src/backend (or /opt/app-root/src in the image)
    backend_root = here.parents[2]
    candidates = [
        backend_root / "schemas" / _SCHEMA_FILENAME,
        Path("/opt/app-root/src/schemas") / _SCHEMA_FILENAME,
        Path("/opt/app-root/schemas") / _SCHEMA_FILENAME,
    ]
    # Monorepo checkout: …/troshka/schemas (parents[4] from app/services)
    if len(here.parents) > 4:
        candidates.append(here.parents[4] / "schemas" / _SCHEMA_FILENAME)
    return candidates


@lru_cache(maxsize=1)
def load_template_schema() -> dict[str, Any]:
    """Load and cache the Troshka template JSON Schema document."""
    for path in _schema_candidates():
        if path.is_file():
            with open(path, encoding="utf-8") as fh:
                schema = json.load(fh)
            logger.debug("Loaded template schema from %s", path)
            return schema
    searched = ", ".join(str(p) for p in _schema_candidates())
    raise FileNotFoundError(
        f"Troshka template schema {_SCHEMA_FILENAME!r} not found. Tried: {searched}"
    )


def _json_pointer(path_parts: list[Any]) -> str:
    """Build a JSON Pointer from a jsonschema absolute_path."""
    if not path_parts:
        return "/"
    escaped = []
    for part in path_parts:
        text = str(part)
        text = text.replace("~", "~0").replace("/", "~1")
        escaped.append(text)
    return "/" + "/".join(escaped)


def _humanize_validation_message(message: str) -> str:
    """Turn jsonschema additionalProperties noise into 'unknown keys: …'."""
    match = _ADDITIONAL_PROPS_RE.search(message)
    if not match:
        return message
    keys = _QUOTED_KEY_RE.findall(match.group(1))
    if not keys:
        return message
    return "unknown keys: " + ", ".join(keys)


def _format_validation_error(err: ValidationError) -> dict[str, str]:
    return {
        "path": _json_pointer(list(err.absolute_path)),
        "message": _humanize_validation_message(err.message),
    }


def _prepare_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy and normalize shapes the schema expects (ocp list)."""
    prepared = copy.deepcopy(doc)
    ocp = prepared.get("ocp")
    if isinstance(ocp, dict):
        prepared["ocp"] = [ocp]
    return prepared


def _document_size_bytes(doc: dict[str, Any]) -> int:
    return len(json.dumps(doc, default=str).encode("utf-8"))


def validate_template_document(doc: Any) -> dict[str, Any]:
    """Validate a template mapping against the Troshka JSON Schema.

    Normalizes legacy ``ocp:`` mappings to a one-element list before validate.
    Returns the prepared (normalized) document on success.

    Raises:
        TemplateSchemaError: on type/schema/size failures.
    """
    if doc is None:
        raise TemplateSchemaError(
            "template_yaml is required",
            errors=[{"path": "/", "message": "template_yaml is required"}],
        )
    if not isinstance(doc, dict):
        raise TemplateSchemaError(
            "template_yaml must be a YAML mapping",
            errors=[{"path": "/", "message": "template_yaml must be a YAML mapping"}],
        )

    size = _document_size_bytes(doc)
    if size > _MAX_DOCUMENT_BYTES:
        raise TemplateSchemaError(
            f"template_yaml exceeds maximum size ({_MAX_DOCUMENT_BYTES} bytes)",
            errors=[
                {
                    "path": "/",
                    "message": (
                        f"document is {size} bytes; max is {_MAX_DOCUMENT_BYTES}"
                    ),
                }
            ],
        )

    prepared = _prepare_document(doc)
    schema = load_template_schema()
    validator = Draft202012Validator(schema)
    errors: list[dict[str, str]] = []
    for err in sorted(validator.iter_errors(prepared), key=lambda e: list(e.path)):
        errors.append(_format_validation_error(err))
    if errors:
        summary = errors[0]["message"]
        if errors[0].get("path") and errors[0]["path"] != "/":
            summary = f"{errors[0]['path']}: {summary}"
        if len(errors) > 1:
            summary = f"{summary} (and {len(errors) - 1} more)"
        raise TemplateSchemaError(
            f"Template schema validation failed: {summary}",
            errors=errors,
        )
    return prepared


def schema_validation_detail(exc: TemplateSchemaError) -> dict[str, Any]:
    """HTTP 400 detail payload for FastAPI."""
    return {
        "message": str(exc),
        "errors": exc.errors,
    }


def slugify_template_name(name: str, *, fallback: str = "template") -> str:
    """Make a human project/pattern name valid for template ``name`` (id)."""
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", (name or "").strip())
    cleaned = cleaned.strip("-._")
    if not cleaned:
        cleaned = fallback
    if not re.match(r"^[a-zA-Z0-9]", cleaned):
        cleaned = f"{fallback}-{cleaned}"
    if not _NAME_PATTERN.match(cleaned):
        cleaned = fallback
    return cleaned[:128]


def apply_export_identity(doc: dict[str, Any], *, title: str, description: str = ""):
    """Set schema-valid ``name`` + human ``display_name`` from a project title."""
    title = (title or "").strip() or "template"
    doc["display_name"] = title[:256]
    doc["name"] = slugify_template_name(title)
    if description:
        doc["description"] = description


def export_has_template_content(doc: dict[str, Any]) -> bool:
    """True when export has schema-required sections worth validating."""
    # Schema requires both vms and networks; skip incomplete/legacy stubs
    # (empty draft, ocpMeta-only) so blank-project export keeps working.
    return bool(doc.get("vms")) and bool(doc.get("networks"))


def ensure_exported_template_valid(doc: dict[str, Any]) -> dict[str, Any]:
    """Validate a fully built export document; raise TemplateSchemaError if invalid.

    Empty or incomplete drafts skip validation so blank-project / ocpMeta-only
    exports keep working.
    """
    if not export_has_template_content(doc):
        return doc
    return validate_template_document(doc)


# Re-export for callers that catch ValidationError directly.
__all__ = [
    "TemplateSchemaError",
    "ValidationError",
    "apply_export_identity",
    "ensure_exported_template_valid",
    "export_has_template_content",
    "load_template_schema",
    "schema_validation_detail",
    "slugify_template_name",
    "validate_template_document",
]
