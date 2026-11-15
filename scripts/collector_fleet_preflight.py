"""Read-only collector configuration gate for every configured native host.

This is not accounting or capacity proof. Never enables scheduling or quotas.
The probe runs in the collection worker where protected configuration is mounted;
only identities and status flags leave the application, never credentials.
"""

import argparse
import json
import os
import subprocess
from pathlib import Path

PROBE = '''
import json
from types import SimpleNamespace
from app.core.config import config
from app.core.database import SessionLocal
from app.models.host import Host
from app.models.provider import Provider
from app.models.kubevirt_storage import NativeCollectionDomain, NativeReportingTarget
from app.services.native_collection_config import _managed_inputs
from app.services.native_managed_collector import anchored

result = {"hosts": [], "profiles": [], "blockers": []}
with SessionLocal() as db:
    for host in db.query(Host).filter_by(host_type="kubevirt-cluster").all():
        provider = db.get(Provider, host.provider_id) if host.provider_id else None
        result["hosts"].append({
            "name": provider.name if provider else str(host.id),
            "provider_id": str(provider.id) if provider else None,
        })
    target = db.get(NativeReportingTarget, 1)
    for profile in config.get("native_collection.profiles", []):
        try:
            pins, inventory, backend = _managed_inputs(SimpleNamespace(**profile))
            entry = {
                "domain_id": str(pins.domain_id),
                "providers": [str(p.provider_id) for p in pins.provider_clusters],
                "accepted": pins.initialization_evidence is not None,
                "enrolled": False,
            }
            if str(profile["domain_id"]) != str(pins.domain_id):
                raise ValueError("domain mismatch")
            if target is None or str(target.target_id) != str(profile["target_id"]):
                raise ValueError("reporting target mismatch")
            if db.get(NativeCollectionDomain, str(pins.domain_id)) is not None:
                anchored(db, pins)
                entry["enrolled"] = True
            result["profiles"].append(entry)
        except Exception:  # noqa: BLE001 -- client errors may include credentials
            result["blockers"].append("invalid_or_unanchored_profile")
print(json.dumps(result))
'''


def host_status(host, profiles):
    provider = host.get("provider_id")
    matches = [p for p in profiles if provider and provider in p["providers"]]
    result = {"name": host["name"], "provider_id": provider, "blockers": []}
    if len(matches) != 1:
        result["blockers"] = ["ambiguous_profile" if matches else "missing_profile"]
        return result
    profile = matches[0]
    result["domain_id"] = profile["domain_id"]
    for field, code in (("accepted", "unapproved_profile"), ("enrolled", "missing_enrollment")):
        if profile.get(field) is not True:
            result["blockers"].append(code)
    return result


def evaluate(snapshot):
    hosts = [host_status(h, snapshot["profiles"]) for h in snapshot["hosts"]]
    blockers = list(snapshot.get("blockers", []))
    return {
        "scope": "configuration_only",
        "ready": not blockers and all(not h["blockers"] for h in hosts),
        "hosts": hosts,
        "blockers": blockers,
    }


def inspect(kubeconfig, namespace):
    result = subprocess.run(
        ["oc", f"--kubeconfig={kubeconfig}", "--request-timeout=30s",
         "exec", "-i", "deployment/troshka-collection-worker", "-n", namespace,
         "-c", "worker", "--", "python3", "-"],
        input=PROBE, text=True, capture_output=True, timeout=90, check=True,
    )
    return json.loads(result.stdout)


def check_applications(applications):
    reports = []
    for kubeconfig, namespace in applications:
        try:
            report = evaluate(inspect(kubeconfig, namespace))
        except Exception:  # noqa: BLE001 -- client errors may include credentials
            report = {"ready": False, "hosts": [], "blockers": ["inspection_unavailable"]}
        reports.append({"namespace": namespace, **report})
    return {"ready": bool(reports) and all(r["ready"] for r in reports),
            "scope": "configuration_only", "applications": reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application", nargs=2, action="append", required=True,
                        metavar=("KUBECONFIG", "NAMESPACE"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    report = check_applications(args.application)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
