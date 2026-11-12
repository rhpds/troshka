"""Immutable enrollment and bounded JSON parsing, without cluster clients."""

import json
import re
from dataclasses import dataclass, fields
from pathlib import Path
from uuid import UUID

MAX_BODY_BYTES = 1024 * 1024
MAX_DEPTH = 32
MAX_VALUES = 20000
MAX_TEXT = 65536
MAX_ENROLLMENTS = 32
_DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\Z")


class ValidationError(ValueError):
    """A bounded public reason code; never carries request content."""


def require(condition, code="invalid_document"):
    if not condition:
        raise ValidationError(code)


def full_uid(value):
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def dns_name(value, *, namespace=False):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > (63 if namespace else 253)
    ):
        return False
    return all(_DNS_LABEL.fullmatch(part) for part in value.split(".")) and (
        not namespace or "." not in value
    )


def validate_tree(value):
    """Bound direct in-memory calls as well as decoded HTTP/config documents."""
    pending = [(value, 0)]
    count, text_size = 0, 0
    while pending:
        item, depth = pending.pop()
        count += 1
        require(count <= MAX_VALUES and depth <= MAX_DEPTH, "document_limit")
        children = _children(item)
        text_size += _text_size(item)
        require(text_size <= MAX_BODY_BYTES, "document_limit")
        require(len(children) + count + len(pending) <= MAX_VALUES, "document_limit")
        pending.extend((child, depth + 1) for child in children)


def _text_size(item):
    if type(item) is dict:
        return sum(len(key) for key in item)
    return len(item) if type(item) is str else 0


def _children(item):
    if type(item) is dict:
        require(
            all(type(key) is str and len(key) <= MAX_TEXT for key in item),
            "invalid_document",
        )
        return item.values()
    if type(item) is list:
        return item
    if type(item) is str:
        require(len(item) <= MAX_TEXT, "document_limit")
    elif type(item) is int:
        require(-(2**63) <= item < 2**63, "document_limit")
    else:
        require(item is None or type(item) is bool, "invalid_document")
    return ()


def _check_json_depth(raw):
    depth, quoted, escaped = 0, False, False
    for char in raw:
        if quoted:
            quoted, escaped = _quoted_state(char, escaped)
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
            require(depth <= MAX_DEPTH, "document_limit")
        elif char in (93, 125):
            depth -= 1


def _quoted_state(char, escaped):
    if escaped:
        return True, False
    if char == 92:
        return True, True
    return char != 34, False


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_key")
        result[key] = value
    return result


def _unsupported_number(_value):
    raise ValidationError("invalid_number")


def decode_document(raw):
    require(isinstance(raw, bytes) and len(raw) <= MAX_BODY_BYTES, "document_limit")
    _check_json_depth(raw)
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_pairs,
            parse_float=_unsupported_number,
            parse_constant=_unsupported_number,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ValidationError("invalid_json") from None
    validate_tree(value)
    return value


@dataclass(frozen=True, slots=True)
class Enrollment:
    mode: str
    cluster_key: str
    enrollment_id: str
    namespace_name: str
    namespace_uid: str
    project_id: str
    project_cr_name: str
    project_cr_uid: str


def _parse_entry(value):
    require(
        type(value) is dict
        and set(value) == {field.name for field in fields(Enrollment)},
        "invalid_enrollment",
    )
    require(value["mode"] == "quarantine", "invalid_enrollment")
    require(
        dns_name(value["namespace_name"], namespace=True)
        and dns_name(value["project_cr_name"]),
        "invalid_enrollment",
    )
    for key in (
        "cluster_key",
        "enrollment_id",
        "namespace_uid",
        "project_id",
        "project_cr_uid",
    ):
        require(full_uid(value[key]), "invalid_enrollment")
    return Enrollment(**value)


def parse_enrollments(value):
    validate_tree(value)
    require(
        type(value) is dict and set(value) == {"schema_version", "enrollments"},
        "invalid_enrollment",
    )
    require(
        type(value["schema_version"]) is int and value["schema_version"] == 1,
        "invalid_enrollment",
    )
    rows = value["enrollments"]
    require(
        type(rows) is list and 1 <= len(rows) <= MAX_ENROLLMENTS, "invalid_enrollment"
    )
    entries = tuple(_parse_entry(row) for row in rows)
    require(
        len({entry.cluster_key for entry in entries}) == 1, "conflicting_enrollment"
    )
    for key in (
        "enrollment_id",
        "namespace_name",
        "namespace_uid",
        "project_id",
        "project_cr_name",
        "project_cr_uid",
    ):
        require(
            len({getattr(entry, key) for entry in entries}) == len(entries),
            "conflicting_enrollment",
        )
    return entries


def load_enrollments(path):
    try:
        with Path(path).open("rb") as stream:
            return parse_enrollments(decode_document(stream.read(MAX_BODY_BYTES + 1)))
    except OSError:
        raise ValidationError("config_unavailable") from None
