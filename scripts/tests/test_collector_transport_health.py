import importlib
import json
from pathlib import Path

import pytest
from test_collector_maintenance import token


@pytest.fixture
def health(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    return importlib.import_module("collector_transport_health")


def config(value):
    return {"current-context": "collector", "contexts": [{"name": "collector", "context": {"user": "collector", "cluster": "target"}}],
            "users": [{"name": "collector", "user": {"token": value}}],
            "clusters": [{"name": "target", "cluster": {"server": "https://cluster", "certificate-authority-data": "ca"}}]}


def test_expired_credentials_are_not_healthy_configuration(health):
    result = health.credential_health(config(token(exp=99)), now=100, margin=60)
    assert result["blockers"] == ["collector_token_expired"]
    assert "signature" not in json.dumps(result)


def test_expiry_margin_and_wrong_identity(health):
    assert health.credential_health(config(token(exp=159)), now=100, margin=60)["blockers"] == ["collector_token_expiring"]
    assert health.credential_health(config(token(exp=1000, sub="admin")), now=100, margin=60)["blockers"] == ["collector_identity_invalid"]


def test_public_ca_system_trust_is_valid_without_embedded_ca(health):
    value = config(token(exp=1000))
    del value["clusters"][0]["cluster"]["certificate-authority-data"]
    assert health.credential_health(value, now=100, margin=60)["blockers"] == []
    value["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
    assert health.credential_health(value, now=100, margin=60)["blockers"]


def test_dynamic_credentials_are_rejected_without_execution(health):
    value = config(token())
    value["users"][0]["user"] = {"exec": {"command": "malicious"}}
    assert health.credential_health(value, now=100, margin=60)["blockers"] == ["collector_identity_invalid"]


@pytest.mark.parametrize("status,code", [(401, "collector_unauthorized"), (403, "collector_forbidden"), (404, "collector_toolbox_missing")])
def test_api_failures_use_safe_actionable_codes(health, status, code):
    error = type("Error", (Exception,), {"status": status})("secret-token")
    assert health.access_error(error) == code


@pytest.mark.parametrize("changed,blocker", [(False, []), (True, ["collector_toolbox_replaced"])])
def test_live_probe_checks_uid_and_closes_client(health, monkeypatch, tmp_path, changed, blocker):
    import sys
    from types import SimpleNamespace as NS

    path = tmp_path / "collector.json"
    path.write_text(json.dumps(config(token(exp=10**10))))
    closed = []
    api = NS(close=lambda: closed.append(True))
    core = NS(read_namespace=lambda *a, **kw: NS(metadata=NS(uid="cluster")),
              read_namespaced_pod=lambda *a, **kw: NS(metadata=NS(uid="other" if changed else "pinned", deletion_timestamp=None),
                status=NS(phase="Running", container_statuses=[NS(name="tools", ready=True)])))
    monkeypatch.setitem(sys.modules, "kubernetes", NS(client=NS(CoreV1Api=lambda _: core),
                        config=NS(new_client_from_config_dict=lambda _: api)))
    backend = NS(kubeconfig=str(path), toolbox=NS(cluster_key="cluster", pod_name="pod", pod_uid="pinned",
                                                namespace="storage", container="tools"))
    assert health.transport_health(backend)["blockers"] == blocker
    assert closed == [True]
