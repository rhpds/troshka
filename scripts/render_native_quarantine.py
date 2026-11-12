#!/usr/bin/env python3
"""Offline only: explicit inputs, new private output directory, no apply."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/operator"))
from admission.bundle import render_bundle
from admission.models import (
    MAX_BODY_BYTES,
    ValidationError,
    decode_document,
    load_enrollments,
    require,
)


def _read(path):
    with Path(path).open("rb") as stream:
        content = stream.read(MAX_BODY_BYTES + 1)
    require(len(content) <= MAX_BODY_BYTES, "document_limit")
    return content


def write_bundle(output, stages):
    """Refuse existing destinations, including symlinks; files are exclusive 0600."""
    output = Path(output).absolute()
    require(output.name not in {"", ".", ".."}, "invalid_output")
    require(
        not any(parent.is_symlink() for parent in (output, *output.parents)),
        "invalid_output",
    )
    files = {
        stage + ".yaml": yaml.safe_dump_all(docs, sort_keys=True).encode()
        for stage, docs in stages.items()
    }
    manifest = {
        "schema_version": 1,
        "files": {
            name: hashlib.sha256(data).hexdigest()
            for name, data in sorted(files.items())
        },
    }
    files["manifest.json"] = (
        json.dumps(manifest, sort_keys=True, indent=2) + "\n"
    ).encode()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, data in files.items():
        descriptor = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in (
        "enrollments",
        "output",
        "control-namespace",
        "control-namespace-uid",
        "image",
        "tls-secret",
        "ca-file",
        "bootstrap-users-file",
        "controller-users-file",
        "scheduler-nodes-file",
    ):
        parser.add_argument("--" + flag, required=True)
    args = parser.parse_args(argv)
    try:
        stages = render_bundle(
            load_enrollments(args.enrollments),
            control_namespace=args.control_namespace,
            control_namespace_uid=args.control_namespace_uid,
            image=args.image,
            tls_secret=args.tls_secret,
            ca_pem=_read(args.ca_file).decode("ascii"),
            bootstrap_users=decode_document(_read(args.bootstrap_users_file)),
            controller_users=decode_document(_read(args.controller_users_file)),
            scheduler_nodes=decode_document(_read(args.scheduler_nodes_file)),
        )
        write_bundle(args.output, stages)
    except (ValidationError, OSError, UnicodeError):
        parser.exit(
            2, "quarantine render failed: invalid input or unavailable output\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
