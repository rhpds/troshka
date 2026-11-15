"""Collector access must stay scoped and reject replacement identities."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "collector_rbac", SCRIPTS / "deploy_collector_rbac.py"
)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


@pytest.fixture
def domain():
    return {
        "backend": {
            "kubeconfig": "/approved/config",
            "toolbox": {
                "namespace": "storage",
                "pod_name": "tools-exact",
                "pod_uid": "tool-uid",
            },
        },
        "pins": {
            "provider_clusters": [{"cluster_key": "cluster-uid"}],
            "project_roots": [
                {
                    "namespace": "project-a",
                    "namespace_uid": "ns-uid",
                    "project_id": "project-id",
                    "project_cr_uid": "root-uid",
                }
            ],
            "cache_roots": [{"namespace": "cache", "namespace_uid": "cache-uid"}],
        },
    }


def objects():
    return {
        ("namespace", "kube-system"): {"metadata": {"uid": "cluster-uid"}},
        ("namespace", "project-a"): {"metadata": {"uid": "ns-uid"}},
        ("namespace", "cache"): {"metadata": {"uid": "cache-uid"}},
        ("troshkaprojects", "-n", "project-a"): {
            "items": [
                {
                    "apiVersion": "troshka.redhat.com/v1alpha1",
                    "kind": "TroshkaProject",
                    "metadata": {"name": "root", "uid": "root-uid"},
                    "spec": {"projectId": "project-id"},
                }
            ]
        },
        ("pod", "tools-exact", "-n", "storage"): {"metadata": {"uid": "tool-uid"}},
    }


def test_scope_and_project_ownership(monkeypatch, domain):
    live = objects()
    monkeypatch.setattr(collector, "read", lambda kc, *args: live[args])
    manifest = collector.render(domain, collector.validate_domain(domain))["items"]
    for obj in manifest:
        if obj["metadata"].get("namespace") == "project-a":
            assert obj["metadata"]["ownerReferences"][0]["uid"] == "root-uid"
        if obj["metadata"].get("namespace") == "cache":
            assert "ownerReferences" not in obj["metadata"]
            for rule in obj.get("rules", []):
                assert not {"secrets", "configmaps"}.intersection(rule["resources"])
        for rule in obj.get("rules", []):
            assert not {"secrets", "*"}.intersection(rule["resources"])
            if rule["resources"] == ["pods/exec"]:
                assert obj["metadata"]["namespace"] == "storage"
                assert rule["resourceNames"] == ["tools-exact"]
                assert rule["verbs"] == ["create"]
            else:
                assert set(rule["verbs"]) <= {"get", "list"}
    ns_rule = next(x for x in manifest if x["kind"] == "ClusterRole")["rules"][0]
    assert set(ns_rule["resourceNames"]) == {
        "project-a",
        "cache",
        "storage",
        "kube-system",
    }


@pytest.mark.parametrize(
    "key",
    [
        ("namespace", "kube-system"),
        ("namespace", "project-a"),
        ("namespace", "cache"),
        ("pod", "tools-exact", "-n", "storage"),
    ],
)
def test_replacement_rejected(monkeypatch, domain, key):
    live = objects()
    live[key]["metadata"]["uid"] = "replacement"
    monkeypatch.setattr(collector, "read", lambda kc, *args: live[args])
    with pytest.raises(ValueError):
        collector.validate_domain(domain)


def test_revalidation_blocks_all_mutations(monkeypatch, domain, tmp_path):
    (tmp_path / "plan.json").write_text(json.dumps({"domains": [domain]}))
    calls = []
    monkeypatch.setattr(collector, "oc", lambda *args: calls.append(args))

    def reject(_):
        raise ValueError("Toolbox replaced")

    monkeypatch.setattr(collector, "validate_domain", reject)
    with pytest.raises(ValueError, match="Toolbox replaced"):
        collector.apply(tmp_path)
    assert calls == []


def test_duplicate_cluster_scope_rejected(domain, tmp_path):
    with pytest.raises(ValueError, match="one entry per cluster"):
        collector.prepare({"domains": [domain, domain]}, tmp_path)


def test_readiness_access_is_explicit_named_and_read_only(domain):
    assert collector.readiness_objects(domain) == []
    domain["readiness_access"] = {
        "namespace": "openshift-storage",
        "storage_class": "approved-sc",
        "driver": "approved-driver",
        "controller": "approved-controller",
        "node": "approved-node",
        "priority_classes": ["system-node-critical"],
        "service_accounts": ["approved-sa"],
    }
    objects = collector.readiness_objects(domain)
    assert len(objects) == 4
    for obj in objects:
        for rule in obj.get("rules", []):
            assert not {"secrets", "configmaps", "pods/exec"} & set(rule["resources"])
            if "list" in rule["verbs"]:
                assert obj["metadata"]["namespace"] == "openshift-storage"
                assert rule["resources"][0] in {"pods", "replicasets"}
            else:
                assert rule["verbs"] == ["get"]
                assert rule["resourceNames"]
    domain["readiness_access"]["driver"] = "*"
    with pytest.raises(ValueError, match="exact"):
        collector.readiness_objects(domain)


@pytest.mark.parametrize(
    "field,value", [("uid", "replacement"), ("deletionTimestamp", "now")]
)
def test_project_replaced_or_deleting(monkeypatch, domain, field, value):
    live = objects()
    live[("troshkaprojects", "-n", "project-a")]["items"][0]["metadata"][field] = value
    monkeypatch.setattr(collector, "read", lambda kc, *args: live[args])
    with pytest.raises(ValueError):
        collector.validate_domain(domain)
