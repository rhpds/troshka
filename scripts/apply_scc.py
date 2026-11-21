#!/usr/bin/env python3
"""Apply Troshka SCC policy without clearing operator-managed users.

Client-side `oc apply -f scc.yaml` with `users: []` wipes live membership and
stores empty users in last-applied-configuration. This helper merges any live
`users` into each document before apply so policy fields can be updated safely.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a deploy dependency
    yaml = None


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "src/operator/deploy/scc.yaml"


def run(args, timeout=180, stdin=None):
    result = subprocess.run(
        args, input=stdin, text=True, capture_output=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def load_documents(path: Path):
    if yaml is None:
        raise RuntimeError("PyYAML is required (pip install pyyaml)")
    docs = list(yaml.safe_load_all(path.read_text()))
    return [doc for doc in docs if doc and doc.get("kind") == "SecurityContextConstraints"]


def live_users(kubeconfig: str, name: str):
    raw = run(
        [
            "oc",
            f"--kubeconfig={kubeconfig}",
            "--request-timeout=30s",
            "get",
            "scc",
            name,
            "--ignore-not-found",
            "-o",
            "json",
        ]
    )
    if not raw:
        return None
    return list(json.loads(raw).get("users") or [])


def merge_users(doc: dict, existing):
    """Preserve live users; never apply an empty wipe over existing membership."""
    if existing is None:
        # Creating: omit users so the API default stays empty until the operator adds SAs.
        doc = dict(doc)
        doc.pop("users", None)
        return doc
    merged = dict(doc)
    merged["users"] = sorted(set(existing) | set(doc.get("users") or []))
    return merged


def apply_document(kubeconfig: str, doc: dict):
    payload = yaml.safe_dump(doc, sort_keys=False)
    run(
        [
            "oc",
            f"--kubeconfig={kubeconfig}",
            "--request-timeout=30s",
            "apply",
            "-f",
            "-",
        ],
        stdin=payload,
    )


def apply_scc_manifest(kubeconfig: str, manifest: Path):
    applied = []
    for doc in load_documents(manifest):
        name = doc["metadata"]["name"]
        existing = live_users(kubeconfig, name)
        merged = merge_users(doc, existing)
        apply_document(kubeconfig, merged)
        users = merged.get("users") or []
        print(f"{name}: applied policy; preserving {len(users)} users", flush=True)
        applied.append(name)
    return applied


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"SCC YAML (default: {DEFAULT_MANIFEST})",
    )
    args = parser.parse_args(argv)
    if not args.manifest.is_file():
        raise SystemExit(f"manifest not found: {args.manifest}")
    apply_scc_manifest(args.kubeconfig, args.manifest)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.SubprocessError) as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
