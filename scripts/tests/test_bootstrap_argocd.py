"""Argo bootstrap protects stored credentials and existing workloads."""

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def bootstrap():
    path = Path(__file__).resolve().parents[1] / "bootstrap_argocd.py"
    spec = importlib.util.spec_from_file_location("bootstrap_argocd", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_application_uses_named_revision_and_manual_nonpruning_sync(bootstrap):
    app = bootstrap.application("dev", "troshka-dev", "v2026.11.14-214005", {}, [])
    assert app["spec"]["source"]["targetRevision"] == "v2026.11.14-214005"
    assert app["spec"]["source"]["helm"]["releaseName"] == "dev"
    assert "automated" not in app["spec"]["syncPolicy"]
    assert "finalizers" not in app["metadata"]
    assert "RespectIgnoreDifferences=true" in app["spec"]["syncPolicy"]["syncOptions"]


@pytest.mark.parametrize("revision", ["latest", "production", "deadbeef", "vbad ref"])
def test_mutable_or_malformed_revision_is_rejected(bootstrap, revision):
    with pytest.raises(ValueError):
        bootstrap.application("dev", "troshka-dev", revision, {}, [])


def test_credentials_never_enter_application_values(bootstrap):
    with pytest.raises(ValueError, match="credential"):
        bootstrap.application("dev", "troshka-dev", "v1", {"secrets": {"encryptionKey": "private"}}, [])


def test_dev_can_follow_main(bootstrap):
    assert bootstrap.application("dev", "troshka-dev", "main", {}, [])["spec"]["source"]["targetRevision"] == "main"


def test_dev_latest_does_not_replace_database_images(bootstrap):
    values = {"postgres": {"image": "postgres:16"}}
    bootstrap.dev_images(values)
    assert values["backend"]["image"] == {"repository": "quay.io/redhat-gpte/troshka-backend", "tag": "latest"}
    assert values["frontend"]["image"]["tag"] == "latest"
    assert values["postgres"]["image"] == "postgres:16"


def test_image_updater_targets_only_requested_application(bootstrap):
    updater = bootstrap.image_updater("troshka-dev", "openshift-gitops")
    refs = updater["spec"]["applicationRefs"]
    assert len(refs) == 1 and refs[0]["namePattern"] == "troshka-dev"
    assert all(i["imageName"].endswith(":latest") for i in refs[0]["images"])
    assert all(i["commonUpdateSettings"]["updateStrategy"] == "digest" for i in refs[0]["images"])


def test_scheduler_settings_come_from_backend_not_worker(bootstrap, monkeypatch):
    def live(kc, item):
        if item["kind"] == "Deployment":
            return {"spec": {"template": {"spec": {"volumes": [{"name": "config", "configMap": {"name": "scheduler"}}]}}}}
        assert item["metadata"]["name"] == "scheduler"
        return {"data": {"config.yaml": "native_collection:\n  enabled: true\n  domain_interval_seconds: 60\n"}}
    monkeypatch.setattr(bootstrap, "live_resource", live)
    native = {"enabled": False, "domainIntervalSeconds": 20, "profiles": ["protected-profile"]}
    bootstrap.hydrate_scheduler(native, "dev", "troshka-dev")
    assert native == {"enabled": True, "domainIntervalSeconds": 60, "profiles": ["protected-profile"]}


def test_adoption_preserves_disabled_quota_environment_override(bootstrap, monkeypatch):
    monkeypatch.setattr(bootstrap, "live_resource", lambda *args: {
        "spec": {"template": {"spec": {"containers": [{"env": [{"name": "TROSHKA_QUOTAS__ENABLED", "value": "false"}]}]}}}})
    values = {"quotas": {"enabled": True}}
    bootstrap.hydrate_quota_override(values, "dev", "troshka-dev")
    assert values["quotas"]["enabled"] is False


def test_adoption_rejects_conflicting_quota_environment_overrides(bootstrap, monkeypatch):
    def resource(kc, item):
        value = "true" if item["metadata"]["name"] == "troshka-worker" else "false"
        return {"spec": {"template": {"spec": {"containers": [{"env": [{"name": "TROSHKA_QUOTAS__ENABLED", "value": value}]}]}}}}
    monkeypatch.setattr(bootstrap, "live_resource", resource)
    with pytest.raises(ValueError, match="conflicting"):
        bootstrap.hydrate_quota_override({}, "dev", "troshka-dev")


def test_verification_does_not_report_old_success_while_new_sync_is_pending(bootstrap, monkeypatch, tmp_path):
    (tmp_path / "application.json").write_text("{}")
    (tmp_path / "plan.json").write_text(json.dumps({"kubeconfig": "dev"}))
    status = {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}, "operationState": {"phase": "Succeeded"}}
    responses = [{"operation": {"sync": {}}, "status": status}, {"status": status}]
    monkeypatch.setattr(bootstrap, "live_resource", lambda *args: responses.pop(0))
    class Waiter:
        def wait(self, seconds):
            pass
    monkeypatch.setattr(bootstrap.threading, "Event", Waiter)
    bootstrap.verify(tmp_path)
    assert not responses


def test_application_lookup_is_unambiguous_on_openshift(bootstrap, monkeypatch):
    calls = []
    monkeypatch.setattr(bootstrap, "oc", lambda *args: calls.append(args) or "{}")
    bootstrap.live_resource("dev", {"kind": "Application", "metadata": {"name": "troshka-dev"}})
    assert calls[0][2] == "applications.argoproj.io"


def test_secret_and_oauth_identity_are_protected_during_sync(bootstrap):
    app = bootstrap.application("dev", "troshka-dev", "v1", {}, [])
    rules = app["spec"]["ignoreDifferences"]
    assert any(r["kind"] == "Secret" and r["name"] == "troshka-secrets"
               and r["jsonPointers"] == ["/data", "/stringData"] for r in rules)
    assert any(r["kind"] == "OAuthClient" and r["jsonPointers"] == ["/secret"] for r in rules)


def test_adoption_protects_only_explicit_existing_workloads(bootstrap):
    resource = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "troshka-worker", "namespace": "troshka-dev"}}
    app = bootstrap.application("dev", "troshka-dev", "v1", {}, [resource])
    guards = [r for r in app["spec"]["ignoreDifferences"] if r["kind"] == "Deployment"]
    assert guards == [{"group": "apps", "kind": "Deployment", "name": "troshka-worker",
                       "namespace": "troshka-dev", "jsonPointers": ["/spec/template", "/spec/replicas"]}]


def test_selective_adoption_sync_excludes_migration_hooks_and_pruning(bootstrap):
    resources = [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "troshka-backend", "namespace": "troshka-dev"}},
        {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "troshka-migrate-1", "annotations": {"argocd.argoproj.io/hook": "PostSync"}}},
    ]
    operation = bootstrap.sync_operation("v1", resources)
    assert operation["sync"]["prune"] is False
    assert operation["sync"]["resources"] == [{"group": "apps", "kind": "Deployment", "name": "troshka-backend", "namespace": "troshka-dev"}]
