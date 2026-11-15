"""Fleet baseline grants no credential reads, storage writes, or exec."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_collector_baseline_is_read_only_and_namespace_scoped():
    items = json.loads((ROOT / "infra/storage-collector-base-rbac.yaml").read_text())[
        "items"
    ]
    assert any(o["kind"] == "ServiceAccount" for o in items)
    for obj in items:
        for rule in obj.get("rules", []):
            assert set(rule["verbs"]) <= {"get", "list"}
            assert not {"secrets", "configmaps", "pods/exec", "*"} & set(
                rule["resources"]
            )
            if rule["resources"] in (["pods"], ["replicasets"]):
                assert obj["kind"] == "Role"
                assert obj["metadata"]["namespace"] == "openshift-storage"
    names = {o["metadata"]["name"] for o in items}
    assert "troshka-storage-collector-base" in names


def test_full_deployment_includes_collector_baseline():
    script = (ROOT / "scripts/deploy-full.sh").read_text()
    body = script.split("apply_rbac() {", 1)[1].split("\n}\n", 1)[0]
    assert "oc apply -f infra/storage-collector-base-rbac.yaml" in body
    assert "oc apply -f infra/storage-collector-project-rbac.yaml" in body


def test_project_role_matches_existing_scoped_inventory_permissions():
    role = json.loads((ROOT / "infra/storage-collector-project-rbac.yaml").read_text())
    scoped = json.loads((ROOT / "infra/storage-collector-rbac.yaml").read_text())
    assert role["kind"] == "ClusterRole"
    assert role["rules"] == scoped["rules"]
    for rule in role["rules"]:
        assert not {"secrets", "*"}.intersection(rule["resources"])
    baseline = json.loads((ROOT / "infra/storage-collector-base-rbac.yaml").read_text())
    assert not any(
        obj.get("roleRef", {}).get("name") == role["metadata"]["name"]
        for obj in baseline["items"]
    )
