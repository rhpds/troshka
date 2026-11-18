"""Offline manifest contracts; API-server CEL/admission needs cluster tests."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft7Validator, FormatChecker, ValidationError


CRD = Path(__file__).resolve().parents[2] / "src/operator/crds/troshkaproject.yaml"


@pytest.fixture
def root_schema():
    manifest = yaml.safe_load(CRD.read_text())
    return next(
        version["schema"]["openAPIV3Schema"]
        for version in manifest["spec"]["versions"]
        if version["name"] == "v1alpha1" and version["served"]
    )


def test_crd_object_schemas_do_not_mix_properties_with_additional_properties(root_schema):
    """JSON Schema accepts this combination, but CRD registration rejects it."""
    pending = [("root", root_schema)]
    while pending:
        path, schema = pending.pop()
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is not False, path
            assert not ("properties" in schema and "additionalProperties" in schema), path
        pending.extend(
            (f"{path}.{name}", child)
            for name, child in schema.get("properties", {}).items()
        )
        if isinstance(schema.get("additionalProperties"), dict):
            pending.append((f"{path}.*", schema["additionalProperties"]))
        if isinstance(schema.get("items"), dict):
            pending.append((f"{path}[]", schema["items"]))


def test_checkpoint_uses_typed_pruned_fields_at_each_schema_boundary(root_schema):
    """Do not bypass checkpoint typing with a preserved, opaque object."""
    schema = root_schema
    for name in ("spec", "originCheckpoint", "publicKey"):
        assert schema.get("x-kubernetes-preserve-unknown-fields") is not True
        assert schema["type"] == "object"
        schema = schema["properties"][name]
    assert schema["type"] == "string"


@pytest.mark.parametrize(
    "change",
    ["valid", "missing-version", "missing-intentId", "missing-publicKey", "version", "uuid", "short-key", "long-key", "key-type"],
)
def test_checkpoint_schema_validates_identity_fields(root_schema, change):
    checkpoint = {
        "version": 1,
        "intentId": "00000000-0000-4000-8000-000000000001",
        "publicKey": "A" * 43 + "=",
    }
    candidate = deepcopy(checkpoint)
    if change.startswith("missing-"):
        candidate.pop(change.removeprefix("missing-"))
    elif change != "valid":
        field, value = {
            "version": ("version", 2),
            "uuid": ("intentId", "not-a-uuid"),
            "short-key": ("publicKey", "A" * 43),
            "long-key": ("publicKey", "A" * 45),
            "key-type": ("publicKey", 44),
        }[change]
        candidate[field] = value
    schema = root_schema["properties"]["spec"]["properties"]["originCheckpoint"]
    validator = Draft7Validator(schema, format_checker=FormatChecker())
    if change == "valid":
        validator.validate(candidate)
    else:
        with pytest.raises(ValidationError):
            validator.validate(candidate)
