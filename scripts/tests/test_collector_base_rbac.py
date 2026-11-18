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


def test_snapshot_class_verifier_read_is_get_only_on_the_collector_account():
    items = json.loads((ROOT / "infra/storage-collector-base-rbac.yaml").read_text())["items"]
    role = next(o for o in items if o["kind"] == "ClusterRole" and o["metadata"]["name"] == "troshka-storage-collector-base")
    rules = [r for r in role["rules"] if "volumesnapshotclasses" in r["resources"]]
    assert rules == [{"apiGroups": ["snapshot.storage.k8s.io"], "resources": ["volumesnapshotclasses"], "verbs": ["get"]}]
    binding = next(o for o in items if o["kind"] == "ClusterRoleBinding" and o["roleRef"]["name"] == role["metadata"]["name"])
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "troshka-storage-collector", "namespace": "troshka"}]


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
