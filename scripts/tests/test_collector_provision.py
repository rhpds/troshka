"""Collector provisioning preserves existing settings and limits credential scope."""

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture
def provision(monkeypatch):
    folder = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location("collector_provision", folder / "collector_provision.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_profile_merge_preserves_existing_domain(provision):
    old = [{"domain_id": "existing", "pins": "unchanged"}]
    merged = provision.merge_profiles(old, [{"domain_id": "new"}])
    assert merged == [{"domain_id": "existing", "pins": "unchanged"}, {"domain_id": "new"}]
    assert old == [{"domain_id": "existing", "pins": "unchanged"}]
    with pytest.raises(ValueError, match="existing"):
        provision.merge_profiles(old, [{"domain_id": "existing", "pins": "replacement"}])


@pytest.mark.parametrize("cluster", [
    {"server": "http://cluster"},
    {"server": "https://cluster", "insecure-skip-tls-verify": True},
    {"server": "https://cluster", "proxy-url": "http://unreviewed-proxy"},
])
def test_unsafe_transport_cannot_receive_a_bearer_token(provision, cluster):
    with pytest.raises(ValueError):
        provision.collector_transport(cluster, "secret-token")


def test_transport_contains_only_collector_identity(provision):
    value = provision.collector_transport({"server": "https://cluster", "certificate-authority-data": "ca"}, "token")
    assert value["users"] == [{"name": "collector", "user": {"token": "token"}}]
    assert value["clusters"][0]["cluster"]["certificate-authority-data"] == "ca"


def test_settings_patch_preserves_quota_and_scheduling(provision):
    app = {"metadata": {"resourceVersion": "123"}, "spec": {"source": {"helm": {
        "valuesObject": {"nativeCollection": {"enabled": False, "jobTimeoutSeconds": 900,
            "worker": {"replicas": 2, "cliImage": "cli:v1"}}, "quotas": {"enabled": False}}
    }}}}
    patch = provision.settings_patch(app, [{"domain_id": "new"}], "new-config", "new-secret")
    assert patch[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "123"}
    value = patch[1]["value"]
    assert value["enabled"] is False
    assert value["jobTimeoutSeconds"] == 900
    assert value["worker"]["replicas"] == 2
    assert value["worker"]["cliImage"] == "cli:v1"
    assert app["spec"]["source"]["helm"]["valuesObject"]["quotas"] == {"enabled": False}


def test_helm_parameter_override_cannot_silently_defeat_provisioning(provision):
    app = {"spec": {"source": {"helm": {"valuesObject": {"nativeCollection": {}},
        "parameters": [{"name": "nativeCollection.worker.secretName", "value": "other"}]}}}}
    with pytest.raises(ValueError):
        provision.native_settings(app)


def test_rollout_does_not_count_old_ready_pods_as_new_ready(provision):
    deployment = {"metadata": {"generation": 2}, "spec": {"replicas": 2, "template": {"spec": {"volumes": [
        {"name": "collection-config", "configMap": {"name": "new"}},
        {"name": "collection-transport", "secret": {"secretName": "new"}}]}}},  # pragma: allowlist secret
        "status": {"observedGeneration": 2, "updatedReplicas": 2, "availableReplicas": 2,
                   "readyReplicas": 2, "replicas": 3}}
    assert not provision.consumer_ready(deployment, "new", allow_terminating=True)
    deployment["status"].update(availableReplicas=3, readyReplicas=3)
    assert provision.consumer_ready(deployment, "new", allow_terminating=True)
    assert not provision.consumer_ready(deployment, "new")
