"""Enroll explicitly authorized empty collector domains; preserve existing anchors.

Execute in the collection worker after installing approved protected profiles.
No cluster writes, workload stops, quota changes, or compatibility rewriting.
"""

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


def require_empty_authorization(pins):
    if (pins.get("project_roots") != [] or not pins.get("empty_domain_bootstrap")
            or not pins.get("initialization_evidence")):
        raise ValueError("only explicitly authorized empty domains can be bootstrapped")


def prepared_profiles(db):
    from app.core.config import config
    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeReportingTarget,
    )
    from app.services.native_collection_config import _managed_inputs
    from app.services.native_managed_collector import anchored

    target = db.get(NativeReportingTarget, 1)
    pending, preserved, seen = [], [], set()
    for profile in config.get("native_collection.profiles", []):
        pins, _, _ = _managed_inputs(SimpleNamespace(**profile))
        domain_id = str(pins.domain_id)
        if (domain_id in seen or str(profile["domain_id"]) != domain_id
                or target is None or target.target_id != profile["target_id"]):
            raise ValueError("duplicate domain or profile target mismatch")
        seen.add(domain_id)
        if db.get(NativeCollectionDomain, domain_id) is not None:
            anchored(db, pins)
            preserved.append(domain_id)
        else:
            require_empty_authorization(pins.model_dump(mode="json"))
            pending.append((profile, pins))
    return pending, preserved


def enroll_empty(db, profile, pins):
    from app.schemas.kubevirt_storage_enrollment import VerifiedEnrollment
    from app.services.kubevirt_storage_enrollment import (
        enroll_native_roots,
        prepare_native_enrollment,
    )
    # Reuse the shipped privileged CLI's bounded, read-only verification path.
    sys.path.insert(0, "/opt/app-root/src/scripts")
    from import_kubevirt_storage import _verify

    request = prepare_native_enrollment(db, [], empty_domain_pins=pins)
    db.commit()
    with tempfile.TemporaryDirectory(prefix="collector-bootstrap-") as directory:
        request_path = Path(directory) / "request.json"
        request_path.write_text(request.model_dump_json())
        request_path.chmod(0o600)
        output = Path(directory) / "verified.json"
        args = SimpleNamespace(
            **profile, command="verify", request=str(request_path), output=str(output),
            verification_timeout_seconds=600, command_timeout_seconds=15,
        )
        _verify(db, args)
        manifest = VerifiedEnrollment.model_validate_json(output.read_text())
        enroll_native_roots(db, manifest, expected_target_id=profile["target_id"])
        db.commit()
    return str(pins.domain_id)


def main():
    from app.core.database import SessionLocal

    completed, preserved = [], []
    try:
        with SessionLocal() as db:
            pending, preserved = prepared_profiles(db)
            for profile, pins in pending:
                completed.append(enroll_empty(db, profile, pins))
        result = {"ready": True, "enrolled": completed, "preserved": preserved}
    except Exception:  # noqa: BLE001 -- credentials may appear in client exceptions
        result = {"ready": False, "enrolled": completed, "preserved": preserved,
                  "blocker": "empty_domain_bootstrap_unverified"}
    print(json.dumps(result), flush=True)
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
