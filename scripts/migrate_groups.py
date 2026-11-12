#!/usr/bin/env python3
"""Explicit-target offline conversion; database credentials never reach output."""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
_backend = _root / "src/backend" if (_root / "src/backend/app").is_dir() else _root
sys.path.insert(0, str(_backend))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-env", required=True, help="Name of an existing environment variable containing the target URL")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Persist the fenced conversion")
    mode.add_argument("--dry-run", action="store_true", help="Rehearse and roll back (default)")
    parser.add_argument("--expected-digest")
    parser.add_argument("--accounting-start", help="Explicit timezone-aware ISO UTC boundary; required for apply")
    parser.add_argument("--writers-fenced", action="store_true", help="Attest all writers, detached monitors and external operations are fenced")
    args = parser.parse_args()
    target = os.environ.get(args.database_url_env)
    if not target:
        parser.error("Target environment variable is unset or empty")
    try:
        start = datetime.fromisoformat(args.accounting_start) if args.accounting_start else None
        if start is not None and start.utcoffset() is None:
            parser.error("Accounting start requires an explicit timezone")
        # Set before importing models/configuration; no implicit application target.
        os.environ["TROSHKA_DATABASE__URL"] = target
        from app.models import QuotaReadiness  # noqa: F401 -- register all tables
        from app.services import group_migration
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url
        from sqlalchemy.orm import Session
        if make_url(target).get_backend_name() != "postgresql":
            parser.error("Operator conversion requires PostgreSQL")
        engine = create_engine(target, hide_parameters=True)
        try:
            with Session(engine) as db:
                result = group_migration.run(db, accounting_start=start, apply=args.apply,
                                             expected_digest=args.expected_digest,
                                             writers_fenced=args.writers_fenced)
            output = {key: value for key, value in result.items() if key != "mapping"}
            output["mode"] = "apply" if args.apply else "dry-run"
            output["mapped_user_count"] = len(result["mapping"])
            output["inventory_digest"] = result["inventory_digest"]
            print(json.dumps(output, sort_keys=True))
        finally:
            engine.dispose()
    except Exception:
        # SQLAlchemy exceptions and URLs may contain credentials/full row values.
        print("Group conversion failed; transaction rolled back. Recheck target, inventory, fence and ambiguity blockers.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
