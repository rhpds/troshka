"""Library item name resolution helpers (exact name + | alternates)."""

from __future__ import annotations


class OptionalLibraryRef:
    """Sentinel: optional library_item_name matched nothing — omit the ref."""


# Returned when library_item_name ends with an empty | alternate
# (e.g. "RHEL DVD|") and no named alternate matched.
OPTIONAL_LIBRARY_REF = OptionalLibraryRef()


def split_library_item_names(item_name: str | None) -> tuple[list[str], bool]:
    """Split ``A|B|`` into names ``[\"A\", \"B\"]`` and optional=True.

    An empty segment (leading, middle, or trailing ``|``) marks the ref optional:
    if no named alternate exists in the library, skip instead of erroring.
    """
    if item_name is None:
        return [], False
    text = str(item_name)
    if not text.strip() and "|" not in text:
        return [], False
    parts = [p.strip() for p in text.split("|")]
    optional = any(p == "" for p in parts)
    names = [p for p in parts if p]
    return names, optional
