"""Renewal must preserve identity, exact RBAC scope, and crash evidence."""

import base64
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def maintenance(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    return importlib.import_module("collector_maintenance")


def token(exp=10000, sub="system:serviceaccount:troshka:troshka-storage-collector"):
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp, "sub": sub}).encode()).decode().rstrip("=")
    return "header." + body + ".signature"


def test_renewal_uses_actual_expiry_not_requested_duration(maintenance):
    assert maintenance.renewal_due(token(exp=150), now=100, margin=60)
    assert not maintenance.renewal_due(token(exp=161), now=100, margin=60)
    assert maintenance.renewal_due(token(exp=99), now=100, margin=60)


def test_wrong_identity_and_malformed_tokens_fail_closed(maintenance):
    for value in (token(sub="system:serviceaccount:other:admin"), "not-a-token"):
        with pytest.raises(ValueError):
            maintenance.renewal_due(value, now=100, margin=60)


def workload():
    return {"metadata": {"name": "tools", "uid": "deployment"}}


def replica():
    return {"metadata": {"name": "tools-rs", "uid": "replica", "ownerReferences": [
        {"kind": "Deployment", "name": "tools", "uid": "deployment", "controller": True}]}}


def pod():
    return {"metadata": {"name": "tools-new", "uid": "pod-new", "ownerReferences": [
        {"kind": "ReplicaSet", "name": "tools-rs", "uid": "replica", "controller": True}]},
        "spec": {"containers": [{"name": "tools"}]},
        "status": {"phase": "Running", "containerStatuses": [{"name": "tools", "ready": True}],
                   "conditions": [{"type": "Ready", "status": "True"}]}}


def test_replacement_requires_exact_controller_chain(maintenance):
    assert maintenance.select_toolbox(workload(), [replica()], [pod()], "deployment", "tools")["metadata"]["uid"] == "pod-new"
    for field in ("uid", "name"):
        bad = pod()
        bad["metadata"]["ownerReferences"][0][field] = "impostor"
        with pytest.raises(ValueError):
            maintenance.select_toolbox(workload(), [replica()], [bad], "deployment", "tools")


def test_replaced_deployment_and_ambiguous_rollout_are_not_auto_trusted(maintenance):
    with pytest.raises(ValueError):
        maintenance.select_toolbox(workload(), [replica()], [pod()], "different", "tools")
    with pytest.raises(ValueError):
        maintenance.select_toolbox(workload(), [replica()], [pod(), pod()], "deployment", "tools")


def test_unready_or_terminating_toolbox_is_rejected(maintenance):
    value = pod()
    value["metadata"]["deletionTimestamp"] = "now"
    with pytest.raises(ValueError):
        maintenance.select_toolbox(workload(), [replica()], [value], "deployment", "tools")


def test_role_patch_only_retargets_existing_exact_permissions(maintenance):
    role = {"metadata": {"resourceVersion": "42"}, "rules": [
        {"apiGroups": [""], "resources": ["pods"], "resourceNames": ["old"], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["pods/exec"], "resourceNames": ["old"], "verbs": ["create"]}]}
    patch = maintenance.toolbox_role_patch(role, "old", "new")
    assert patch[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "42"}
    assert patch[1]["value"][0]["resourceNames"] == ["new"]
    role["rules"][1]["verbs"].append("delete")
    with pytest.raises(ValueError):
        maintenance.toolbox_role_patch(role, "old", "new")


def test_toolbox_binding_cannot_expand_to_another_identity(maintenance):
    binding = {"roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "troshka-storage-collector-toolbox"},
               "subjects": [{"kind": "ServiceAccount", "name": "troshka-storage-collector", "namespace": "troshka"}]}
    maintenance.verify_toolbox_binding(binding)
    binding["subjects"].append({"kind": "Group", "name": "system:authenticated"})
    with pytest.raises(ValueError, match="subjects"):
        maintenance.verify_toolbox_binding(binding)


def test_partial_attempt_blocks_next_scheduled_run(maintenance, tmp_path):
    (tmp_path / "pending.json").write_text('{"phase":"publish"}')
    with pytest.raises(ValueError, match="unfinished"):
        maintenance.require_no_pending(tmp_path)


def test_busy_job_defers_before_any_mutation(maintenance, monkeypatch, tmp_path):
    prepared = {"entry": {"namespace": "app"}, "changes": [{"key": "cluster"}]}
    monkeypatch.setattr(maintenance, "prepare_application", lambda _: prepared)
    monkeypatch.setattr(maintenance, "jobs_idle", lambda _: False)
    monkeypatch.setattr(maintenance, "publish", lambda *_: pytest.fail("must not publish while busy"))
    result = maintenance.run({"applications": [{}]}, tmp_path, apply=True)
    assert result["status"] == "deferred_jobs_active"
    assert not (tmp_path / "pending.json").exists()


def test_success_requires_rollout_mounted_bytes_and_live_health(maintenance, monkeypatch, tmp_path):
    prepared = {"entry": {"namespace": "app", "kubeconfig": "kc"}, "changes": [{"key": "cluster"}]}
    calls = []
    monkeypatch.setattr(maintenance, "prepare_application", lambda _: prepared)
    monkeypatch.setattr(maintenance, "jobs_idle", lambda _: True)
    monkeypatch.setattr(maintenance, "publish", lambda *_: calls.append("publish") or "version")
    monkeypatch.setattr(maintenance, "wait_consumers", lambda *_: calls.append("rollout"))
    monkeypatch.setattr(maintenance, "verify_mounted_inputs", lambda *_: calls.append("mounted"))
    monkeypatch.setattr(maintenance, "inspect", lambda *_: calls.append("live") or {"profiles": [{"transport": {"blockers": []}}], "blockers": []})
    monkeypatch.setattr(maintenance, "evaluate", lambda _: {"ready": True})
    assert maintenance.run({"applications": [{}]}, tmp_path, apply=True)["status"] == "updated"
    assert calls == ["publish", "rollout", "mounted", "live"]
    assert not (tmp_path / "pending.json").exists()


def test_ambiguous_publication_is_never_retried(maintenance, monkeypatch, tmp_path):
    prepared = {"entry": {"namespace": "app"}, "changes": [{"key": "cluster"}]}
    monkeypatch.setattr(maintenance, "prepare_application", lambda _: prepared)
    monkeypatch.setattr(maintenance, "jobs_idle", lambda _: True)
    def ambiguous(*_):
        raise TimeoutError("response lost")
    monkeypatch.setattr(maintenance, "publish", ambiguous)
    with pytest.raises(TimeoutError):
        maintenance.run({"applications": [{}]}, tmp_path, apply=True)
    assert (tmp_path / "pending.json").exists()
    monkeypatch.setattr(maintenance, "prepare_application", lambda _: pytest.fail("must not retry"))
    with pytest.raises(ValueError, match="unfinished"):
        maintenance.run({"applications": [{}]}, tmp_path, apply=True)


def test_no_change_is_a_read_only_noop(maintenance, monkeypatch, tmp_path):
    from unittest.mock import Mock
    verify = Mock()
    monkeypatch.setattr(maintenance, "verify_mounted_inputs", verify)
    monkeypatch.setattr(maintenance, "prepare_application", lambda _: {"entry": {"namespace": "app"}, "changes": [],
                        "native": {"worker": {"secretName": "secret", "configMapName": "config"}}})  # pragma: allowlist secret
    monkeypatch.setattr(maintenance, "publish", lambda *_: pytest.fail("unexpected mutation"))
    assert maintenance.run({"applications": [{}]}, tmp_path, apply=True)["status"] == "healthy"
    verify.assert_called_once_with({"namespace": "app"}, "secret", "config")


def test_missing_fleet_enrollment_is_not_hidden_by_healthy_credentials(maintenance, monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, "verify_mounted_inputs", lambda *_: None)
    monkeypatch.setattr(maintenance, "prepare_application", lambda _: {"entry": {"namespace": "app"}, "changes": [], "fleet_ready": False,
                        "native": {"worker": {"secretName": "secret", "configMapName": "config"}}})  # pragma: allowlist secret
    assert maintenance.run({"applications": [{}]}, tmp_path, apply=True)["status"] == "fleet_incomplete"


def test_duplicate_application_plan_is_rejected_before_read_or_write(maintenance, monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, "prepare_application", lambda _: pytest.fail("ambiguous plan"))
    with pytest.raises(ValueError, match="duplicate"):
        maintenance.run({"applications": [{"kubeconfig": "kc", "namespace": "app"}] * 2}, tmp_path, apply=True)


def test_consumer_selection_excludes_old_or_terminating_pods(maintenance):
    value = {"metadata": {"labels": {"app": "backend"}}, "spec": {"volumes": [
        {"name": "collection-config", "configMap": {"name": "new"}},
        {"name": "collection-transport", "secret": {"secretName": "new"}}]},  # pragma: allowlist secret
        "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
    assert maintenance.mounted_candidate(value, {"app": "backend"}, "new")
    assert not maintenance.mounted_candidate(value, {"app": "backend"}, "old")
    value["metadata"]["deletionTimestamp"] = "now"
    assert not maintenance.mounted_candidate(value, {"app": "backend"}, "new")


@pytest.fixture
def mounted_fleet(maintenance, monkeypatch):
    import copy

    volumes = [{"name": "collection-config", "configMap": {"name": "config-current"}},
               {"name": "collection-transport", "secret": {"secretName": "secret-current"}}]  # pragma: allowlist secret
    deployment = {"metadata": {"name": "backend", "uid": "deployment", "generation": 3},
                  "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "backend"}},
                           "template": {"spec": {"volumes": copy.deepcopy(volumes),
                                                 "containers": [{"name": "backend"}]}}},
                  "status": {"observedGeneration": 3, "updatedReplicas": 2,
                             "availableReplicas": 2, "readyReplicas": 2, "replicas": 2}}
    pods = [{"metadata": {"name": name, "uid": name, "labels": {"app": "backend"}},
             "spec": {"volumes": copy.deepcopy(volumes)},
             "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}
            for name in ("backend-first", "backend-second")]
    fleet = {"deployment": deployment, "pods": pods, "reads": [], "execs": [], "bad_pod": None}

    def read(_, resource, name=None, namespace=None):
        fleet["reads"].append((resource, name, namespace))
        if resource == "configmap":
            assert name == "config-current"
            return {"data": {"pins.json": '{"pinned":true}'}}
        if resource == "secret":
            assert name == "secret-current"
            return {"data": {"transport": base64.b64encode(b"credential").decode()}}
        if resource == "deployments":
            return {"items": [deployment]}
        assert resource == "pods"
        return {"items": pods}

    def execute(_, *args, payload=None):
        assert args[:2] == ("exec", "-i")
        fleet["execs"].append((args[2], payload))
        return json.dumps({"matched": args[2] != fleet["bad_pod"]})

    monkeypatch.setattr(maintenance, "read", read)
    monkeypatch.setattr(maintenance, "oc", execute)
    fleet["entry"] = {"namespace": "app", "kubeconfig": "kc", "expected_consumers": ["backend"]}
    return fleet


def test_mounted_inputs_checks_every_replica_and_separate_input_names(maintenance, mounted_fleet):
    import hashlib

    maintenance.verify_mounted_inputs(mounted_fleet["entry"], "secret-current", "config-current")
    assert [name for name, _ in mounted_fleet["execs"]] == ["backend-first", "backend-second"]
    for _, code in mounted_fleet["execs"]:
        assert "/etc/troshka/collection/pins.json" in code
        assert "/etc/troshka/collection-transport/transport" in code
        assert hashlib.sha256(b"credential").hexdigest() in code
        assert hashlib.sha256(b'{"pinned":true}').hexdigest() in code


def test_second_replica_with_stale_bytes_blocks_verification(maintenance, mounted_fleet):
    mounted_fleet["bad_pod"] = "backend-second"
    with pytest.raises(ValueError, match="protected files differ"):
        maintenance.verify_mounted_inputs(mounted_fleet["entry"], "secret-current", "config-current")
    assert len(mounted_fleet["execs"]) == 2


def test_mounted_inputs_detects_missing_expected_deployment(maintenance, mounted_fleet):
    mounted_fleet["entry"]["expected_consumers"].append("worker")
    with pytest.raises(ValueError, match="deployment missing"):
        maintenance.verify_mounted_inputs(mounted_fleet["entry"], "secret-current", "config-current")


@pytest.mark.parametrize("stale", ["bytes", "secret", "config", "generation"])
def test_no_change_apply_propagates_stale_consumer_failure(maintenance, mounted_fleet, monkeypatch, tmp_path, stale):
    if stale == "bytes":
        mounted_fleet["bad_pod"] = "backend-second"
    elif stale == "generation":
        mounted_fleet["deployment"]["status"]["observedGeneration"] = 2
    else:
        volumes = mounted_fleet["deployment"]["spec"]["template"]["spec"]["volumes"]
        if stale == "secret":
            volumes[1]["secret"]["secretName"] = "stale"  # pragma: allowlist secret
        else:
            volumes[0]["configMap"]["name"] = "stale"
    prepared = {"entry": mounted_fleet["entry"], "changes": [],
                "native": {"worker": {"secretName": "secret-current", "configMapName": "config-current"}}}  # pragma: allowlist secret
    monkeypatch.setattr(maintenance, "prepare_application", lambda _: prepared)
    monkeypatch.setattr(maintenance, "publish", lambda *_: pytest.fail("no-change run must not publish"))
    with pytest.raises(ValueError, match="protected files differ|rollout is not current"):
        maintenance.run({"applications": [{}]}, tmp_path, apply=True)
    assert ("secret", "secret-current", "app") in mounted_fleet["reads"]
    assert ("configmap", "config-current", "app") in mounted_fleet["reads"]
    assert not (tmp_path / "pending.json").exists()
