"""Operator source must preserve protected identity and never trust provider TLS defaults."""

import base64
import copy
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def source(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    return importlib.import_module("collector_operator_source")


def token(exp=9999999999, sub="system:serviceaccount:troshka:troshka-storage-collector"):
    claims = base64.urlsafe_b64encode(json.dumps({"sub": sub, "exp": exp}).encode()).decode().rstrip("=")
    return "header." + claims + ".signature"


@pytest.fixture
def case():
    config = {"current-context": "c", "contexts": [{"name": "c", "context": {"cluster": "c", "user": "u"}}],
              "clusters": [{"name": "c", "cluster": {"server": "https://storage", "certificate-authority-data": "Y2E="}}],
              "users": [{"name": "u", "user": {"token": token()}}]}
    backend = {"ceph_fsid": "fsid", "pools": [{"pool_id": 1, "name": "pool"}],
               "toolbox": {"cluster_key": "cluster", "namespace": "storage", "container": "tools",
                           "ceph_fsid": "fsid", "pod_name": "tools-old", "pod_uid": "old"}}
    trusted = {"toolbox": {"namespace": "storage", "container": "tools", "deployment": "tools", "deployment_uid": "dep"}}
    pins = {"provider_clusters": [{"provider_id": "provider", "cluster_key": "cluster"}],
            "pool_keys": [{"ceph_fsid": "fsid", "pool_id": 1}]}
    envelope = {"version": 1, "cluster_key": "cluster", "deployment_uid": "dep", "pod_name": "tools-old",
                "pod_uid": "old", "token": token(), "expires_at": 9999999999, "ceph_fsid": "fsid",
                "pools": backend["pools"]}
    return config, backend, trusted, pins, envelope


@pytest.mark.parametrize("field,value", [("version", True), ("version", 2), ("cluster_key", "replaced"),
    ("deployment_uid", "replaced"), ("ceph_fsid", "other"), ("pools", []), ("pod_uid", ""),
    ("expires_at", 123), ("token", "malformed"), ("token", token(sub="admin"))])
def test_rejects_malformed_or_replaced_anchors(source, case, field, value):
    _, backend, trusted, pins, envelope = case
    envelope[field] = value
    with pytest.raises(ValueError):
        source.validate_envelope(envelope, trusted, backend, pins, now=100)


def test_requires_more_than_48_hours_actual_lifetime(source, case):
    _, backend, trusted, pins, envelope = case
    for exp in (99, 100 + 48 * 3600):
        envelope.update(token=token(exp), expires_at=exp)
        with pytest.raises(ValueError):
            source.validate_envelope(envelope, trusted, backend, pins, now=100)


def test_provider_tls_uses_only_protected_server_and_ca(source, case):
    config = case[0]
    result = source.provider_config(config, {"api_url": "https://storage", "token": "provider", "verify_ssl": False})
    assert result["clusters"][0]["cluster"] == config["clusters"][0]["cluster"]
    assert result["users"][0]["user"] == {"token": "provider"}
    assert config["users"][0]["user"]["token"] != "provider"
    with pytest.raises(ValueError):
        source.provider_config(config, {"api_url": "https://impostor", "token": "provider"})


@pytest.mark.parametrize("replacement", [False, True])
def test_prepare_unchanged_or_new_envelope(source, case, monkeypatch, replacement):
    config, backend, trusted, pins, envelope = case
    original = copy.deepcopy((config, backend))
    if replacement:
        envelope.update(pod_name="tools-new", pod_uid="new", token=token(9999999998), expires_at=9999999998)
    monkeypatch.setattr(source, "fetch_envelope", lambda *_: copy.deepcopy(envelope))
    verified = []
    monkeypatch.setattr(source, "verify", lambda change: verified.append(change))
    change, changed = source.prepare(trusted, [backend], pins, config, "transport", "backend")
    assert changed is replacement
    assert len(verified) == 1
    encoded, backend_text = source.renew(change)
    published = json.loads(base64.b64decode(encoded))
    assert published["clusters"] == config["clusters"]
    assert published["users"][0]["user"]["token"] == envelope["token"]
    assert json.loads(backend_text)[0]["toolbox"]["pod_uid"] == envelope["pod_uid"]
    assert (config, backend) == original
    envelope["pod_uid"] = "changed-after-prepare"
    with pytest.raises(ValueError, match="changed during preparation"):
        source.renew(change)


def test_verify_checks_live_cluster_and_readiness(source, case, monkeypatch):
    maintenance = importlib.import_module("collector_maintenance")
    config, backend, trusted, pins, envelope = case
    monkeypatch.setattr(source, "fetch_envelope", lambda *_: envelope)
    pod = {"metadata": {"uid": "old"}, "status": {"phase": "Running", "conditions": [
        {"type": "Ready", "status": "True"}], "containerStatuses": [{"name": "tools", "ready": True}]}}
    cluster = {"metadata": {"uid": "cluster"}}
    monkeypatch.setattr(maintenance, "read", lambda _, resource, *args: cluster if resource == "namespace" else pod)
    checked = []
    monkeypatch.setattr(maintenance, "verify_storage", lambda *args: checked.append(True))
    source.prepare(trusted, [backend], pins, config, "transport", "backend")
    assert checked == [True]
    cluster["metadata"]["uid"] = "replaced"
    with pytest.raises(ValueError, match="cluster replaced"):
        source.prepare(trusted, [backend], pins, config, "transport", "backend")
    cluster["metadata"]["uid"] = "cluster"
    pod["status"]["conditions"] = []
    with pytest.raises(ValueError, match="not ready"):
        source.prepare(trusted, [backend], pins, config, "transport", "backend")
