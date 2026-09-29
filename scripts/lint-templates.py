#!/usr/bin/env python3
"""Lint Troshka template YAML files against the TroshkaTemplate JSON Schema.

Usage (from repo root)::

    ./scripts/lint-templates.py
    ./scripts/lint-templates.py path/to/template.yaml example_templates/
    ./scripts/lint-templates.py --quiet src/backend/templates

Exit codes: 0 = all ok, 1 = validation errors, 2 = usage / I/O errors.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import yaml  # noqa: E402

from app.services.template_schema import (  # noqa: E402
    TemplateSchemaError,
    validate_template_document,
)

_DEFAULT_GLOBS = (
    "src/backend/templates/*.yaml",
    "example_templates/*.yaml",
)


def _collect_files(paths: list[str]) -> list[Path]:
    """Expand files/dirs/globs into a sorted unique list of YAML paths."""
    found: set[Path] = set()
    for raw in paths:
        p = Path(raw)
        if any(ch in raw for ch in "*?[]"):
            for match in sorted(_REPO_ROOT.glob(raw)):
                if match.is_file() and match.suffix in (".yaml", ".yml"):
                    found.add(match.resolve())
            continue
        path = p if p.is_absolute() else (_REPO_ROOT / p).resolve()
        if path.is_file():
            if path.suffix not in (".yaml", ".yml"):
                raise SystemExit(f"not a YAML file: {path}")
            found.add(path)
        elif path.is_dir():
            found.update(sorted(path.glob("*.yaml")))
            found.update(sorted(path.glob("*.yml")))
        else:
            raise SystemExit(f"path not found: {raw}")
    return sorted(found)


def lint_file(path: Path) -> list[dict[str, str]]:
    """Validate one template file. Returns a list of error dicts (empty = ok)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [{"path": "/", "message": f"cannot read file: {exc}"}]
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [{"path": "/", "message": f"YAML parse error: {exc}"}]
    try:
        validate_template_document(doc)
    except TemplateSchemaError as exc:
        return list(exc.errors) or [{"path": "/", "message": str(exc)}]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate Troshka template YAML against the JSON Schema.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files, directories, or globs (default: shipped + example templates)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only print failures",
    )
    args = parser.parse_args(argv)

    targets = args.paths or list(_DEFAULT_GLOBS)
    try:
        files = _collect_files(targets)
    except SystemExit as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not files:
        print("error: no YAML files matched", file=sys.stderr)
        return 2

    failed = 0
    for path in files:
        try:
            rel = path.relative_to(_REPO_ROOT)
        except ValueError:
            rel = path
        errors = lint_file(path)
        if errors:
            failed += 1
            print(f"FAIL {rel}")
            for err in errors:
                print(f"  {err.get('path', '/')}: {err.get('message', '')}")
        elif not args.quiet:
            print(f"OK   {rel}")

    if failed:
        print(f"\n{failed} of {len(files)} template(s) failed schema validation")
        return 1
    if not args.quiet:
        print(f"\n{len(files)} template(s) ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
