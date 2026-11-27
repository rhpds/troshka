import base64
import copy
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from helpers import collector_transport as ct

# Capture before the autouse network guard; the integration test below replaces
# the HTTP request boundary instead, exercising the real client's signature.
_REAL_CALL_API = ct.client.ApiClient.call_api


def owner(kind, name, uid):
    return {"kind": kind, "name": name, "uid": uid, "controller": True}


def token(expiry, subject="system:serviceaccount:troshka:troshka-storage-collector"):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry, "sub": subject}).encode()).decode().rstrip("=")
    return "header." + payload + ".signature"


def identities():
    deployment = {"metadata": {"name": "tools", "uid": "deployment-uid"}}
    replica = {"metadata": {"name": "rs", "uid": "rs-uid", "ownerReferences": [owner("Deployment", "tools", "deployment-uid")]}}
    pod = {"metadata": {"name": "tools-new", "uid": "pod-uid", "ownerReferences": [owner("ReplicaSet", "rs", "rs-uid")]},
           "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                      "containerStatuses": [{"name": "tools", "ready": True}]}}
    return deployment, replica, pod


def configuration():
    return {"cluster_key": "cluster-uid", "deployment": {"namespace": "openshift-storage", "name": "tools",
            "uid": "deployment-uid", "container": "tools"},
            "backend": {"mode": "toolbox", "ceph_fsid": "fsid", "pools": [{"pool_id": 1, "name": "rbd"}],
                        "toolbox": {"namespace": "openshift-storage", "container": "tools", "pod_name": "tools-old",
                                    "pod_uid": "old-uid", "cluster_key": "cluster-uid", "ceph_fsid": "fsid"}}}


class FakeAPI:
    def __init__(self):
        self.cm = {"metadata": {"resourceVersion": "1"}, "data": {"config": json.dumps(configuration())}}
        self.secret = {"metadata": {"resourceVersion": "1"}, "data": {}}
        self.pod = identities()[2]
        self.role = {"metadata": {"resourceVersion": "2"}, "rules": ct.rules("tools-old")}
        self.issues = 0
        self.patches = 0
        self.publications = 0
        self.failure = None

    def configuration_map(self):
        return copy.deepcopy(self.cm)

    def read_output(self):
        return copy.deepcopy(self.secret)

    def inspect(self, cfg):
        return self.pod, self.role

    def issue(self):
        self.issues += 1
        return "private-token", 1000 + 168 * 3600

    def retarget(self, cfg, patch):
        self.patches += 1
        self.role["rules"] = patch[-1]["value"]

    def verify(self, cfg, pod, token):
        assert token == "private-token"
        if self.failure:
            raise ct.Rejected(self.failure)

    def publish(self, previous, envelope):
        assert previous["metadata"]["resourceVersion"] == self.secret["metadata"]["resourceVersion"]
        self.publications += 1
        self.secret["data"]["transport.json"] = base64.b64encode(json.dumps(envelope).encode()).decode()


def test_disabled_does_not_touch_credentials():
    api = MagicMock()
    api.configuration_map.return_value = None
    assert ct.reconcile(api) == {"ready": False, "reason": "disabled"}
    api.issue.assert_not_called()
    api.read_output.assert_not_called()


def test_initial_publish_then_no_change_cycle():
    api = FakeAPI()
    status = ct.reconcile(api, now=1000)
    assert status["reason"] == "updated"
    assert "token" not in json.dumps(status)
    assert ct.decode_transport(api.secret)["pod_uid"] == "pod-uid"
    assert ct.reconcile(api, now=1001)["reason"] == "healthy"
    assert (api.issues, api.patches, api.publications) == (1, 1, 1)


def test_expiry_renewal_at_margin():
    api = FakeAPI()
    ct.reconcile(api, now=1000)
    ct.reconcile(api, now=1000 + 120 * 3600)
    assert api.issues == api.publications == 2


@pytest.mark.parametrize("reason", ["cluster_changed", "ceph_changed", "pools_changed", "pod_changed"])
def test_failure_no_publication_then_restart_recovers_retargeted_role(reason):
    api = FakeAPI()
    api.failure = reason
    with pytest.raises(ct.Rejected, match=reason):
        ct.reconcile(api, now=1000)
    assert api.patches == 1 and api.publications == 0
    api.failure = None
    assert ct.reconcile(api, now=1000)["ready"]
    assert api.patches == 1 and api.publications == 1


@pytest.mark.parametrize("mutation", ["deployment", "impostor", "ambiguous", "not_ready", "terminating"])
def test_rejects_unapproved_controller_chain(mutation):
    deployment, replica, pod = identities()
    pods = [pod]
    if mutation == "deployment":
        deployment["metadata"]["uid"] = "replaced"
    elif mutation == "impostor":
        pod["metadata"]["ownerReferences"][0]["uid"] = "other-rs"
    elif mutation == "ambiguous":
        pods.append(copy.deepcopy(pod))
    elif mutation == "not_ready":
        pod["status"]["containerStatuses"][0]["ready"] = False
    else:
        pod["metadata"]["deletionTimestamp"] = "now"
    with pytest.raises(ct.Rejected):
        ct.select_toolbox(deployment, [replica], pods, "deployment-uid", "tools")


def test_exact_owner_succeeds():
    deployment, replica, pod = identities()
    assert ct.select_toolbox(deployment, [replica], [pod], "deployment-uid", "tools") == pod


def test_role_drift_blocks_before_token_issuance():
    api = FakeAPI()
    api.role["rules"][1]["verbs"].append("get")
    with pytest.raises(ct.Rejected, match="role_scope_changed"):
        ct.reconcile(api, now=1000)
    assert api.issues == api.patches == api.publications == 0


def test_binding_requires_exact_collector_subject():
    binding = {"roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": ct.ROLE},
               "subjects": [{"kind": "ServiceAccount", "name": "troshka-storage-collector", "namespace": "troshka"}]}
    ct.verify_binding(binding)
    binding["subjects"].append(copy.deepcopy(binding["subjects"][0]))
    with pytest.raises(ct.Rejected, match="binding_changed"):
        ct.verify_binding(binding)


def test_changed_published_anchors_fail_closed():
    api = FakeAPI()
    ct.reconcile(api, now=1000)
    cfg = configuration()
    cfg["deployment"]["uid"] = "new-deployment"
    api.cm["data"]["config"] = json.dumps(cfg)
    with pytest.raises(ct.Rejected, match="published_anchors_changed"):
        ct.reconcile(api, now=1000)
    assert api.publications == 1


def test_configuration_change_during_verification_prevents_publish():
    api = FakeAPI()
    api.verify = lambda *args: api.cm["metadata"].update(resourceVersion="2")
    with pytest.raises(ct.Rejected, match="config_changed"):
        ct.reconcile(api, now=1000)
    assert api.publications == 0


@pytest.mark.parametrize("hours,accepted", [(95, False), (96, True), (168, True)])
def test_token_request_actual_expiration(monkeypatch, hours, accepted):
    api = object.__new__(ct.KubernetesTransport)
    api.core = MagicMock()
    issued = token(1000 + hours * 3600)
    api.call = MagicMock(return_value={"status": {"token": issued, "expirationTimestamp":
        datetime.fromtimestamp(1000 + hours * 3600, timezone.utc).isoformat()}})
    monkeypatch.setattr(ct.time, "time", lambda: 1000)
    if accepted:
        assert api.issue() == (issued, 1000 + hours * 3600)
    else:
        with pytest.raises(ct.Rejected, match="token_lifetime_short"):
            api.issue()
    assert api.call.call_args.args[3]["spec"]["expirationSeconds"] == 168 * 3600


@pytest.mark.parametrize("issued,reason", [(token(1000, "other"), "token_identity_changed"),
                                        (token(float("inf")), "token_invalid"),
                                        (token(True), "token_invalid"), ("malformed", "token_invalid")])
def test_token_claims_reject_invalid_identity_and_expiry(issued, reason):
    with pytest.raises(ct.Rejected, match=reason):
        ct.token_expiry(issued)


def test_token_request_mismatched_expiry_rejected(monkeypatch):
    api = object.__new__(ct.KubernetesTransport)
    api.core = MagicMock()
    api.call = MagicMock(return_value={"status": {"token": token(1000 + 168 * 3600),
        "expirationTimestamp": datetime.fromtimestamp(1000 + 169 * 3600, timezone.utc).isoformat()}})
    monkeypatch.setattr(ct.time, "time", lambda: 1000)
    with pytest.raises(ct.Rejected, match="token_expiry_mismatch"):
        api.issue()


def test_collector_verification_uses_isolated_token(monkeypatch):
    api = object.__new__(ct.KubernetesTransport)
    api.configuration = ct.client.Configuration()
    api.configuration.api_key = {"authorization": "operator-token"}
    api.configuration.refresh_api_key_hook = MagicMock()
    api.api_client = ct.client.ApiClient(api.configuration)
    core = MagicMock()
    core.read_namespace.return_value = {"metadata": {"uid": "cluster-uid"}}
    core.read_namespaced_pod.return_value = identities()[2]
    captured = []
    monkeypatch.setattr(ct.client, "CoreV1Api", lambda c: captured.append(c.configuration) or core)
    api.exec = MagicMock(side_effect=["fsid", '[{"poolnum":1,"poolname":"rbd"}]'])
    api.verify(configuration(), identities()[2], "private-token")
    assert captured[0].api_key == {"authorization": "Bearer private-token"}
    assert captured[0].refresh_api_key_hook is None
    assert api.configuration.api_key == {"authorization": "operator-token"}
    assert all(c.args[0].api_key == {"authorization": "Bearer private-token"} for c in api.exec.call_args_list)


def test_exec_uses_collector_only_private_config_and_post_spdy(monkeypatch):
    from pathlib import Path

    config = ct.client.Configuration()
    config.host = "https://approved-api.example"
    config.ssl_ca_cert = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    config.api_key = {"authorization": "Bearer private-token"}
    config.cert_file = "/operator-client-cert"
    config.key_file = "/operator-client-key"
    observed = []

    def run(args, **kwargs):
        assert "private-token" not in json.dumps(args)
        assert kwargs["env"]["KUBECTL_REMOTE_COMMAND_WEBSOCKETS"] == "false"
        assert kwargs["timeout"] == 40
        path = Path(args[1].removeprefix("--kubeconfig="))
        observed.append(path)
        assert path.stat().st_mode & 0o777 == 0o600
        kubeconfig = json.loads(path.read_text())
        assert kubeconfig["users"] == [{"name": "collector", "user": {"token": "private-token"}}]
        assert "operator-client" not in json.dumps(kubeconfig)
        assert kubeconfig["clusters"][0]["cluster"] == {
            "server": config.host, "certificate-authority": config.ssl_ca_cert}
        assert args[-4:] == ["ceph", "--connect-timeout", "5", "fsid"]
        return MagicMock(returncode=0, stdout="fsid")

    monkeypatch.setattr(ct.subprocess, "run", run)
    assert ct.KubernetesTransport.exec(config, configuration(), identities()[2], ["fsid"]) == "fsid"
    assert not observed[0].exists()


def test_publish_uses_resource_version_compare_and_swap():
    api = object.__new__(ct.KubernetesTransport)
    api.api_client = MagicMock()
    api.publish({"metadata": {"resourceVersion": "42"}}, {"token": "private-token"})
    kwargs = api.api_client.call_api.call_args.kwargs
    assert kwargs["body"]["metadata"] == {"resourceVersion": "42"}
    assert kwargs["header_params"]["Content-Type"] == "application/merge-patch+json"
    assert kwargs["_request_timeout"] == ct.TIMEOUT


def test_role_patch_uses_explicit_json_patch_and_timeout():
    api = object.__new__(ct.KubernetesTransport)
    api.api_client = MagicMock()
    role = {"metadata": {"resourceVersion": "9"}, "rules": ct.rules("old")}
    patch = ct.role_patch(role, {"old"}, "new")
    api.retarget(configuration(), patch)
    kwargs = api.api_client.call_api.call_args.kwargs
    assert kwargs["body"][0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "9"}
    assert kwargs["header_params"]["Content-Type"] == "application/json-patch+json"
    assert kwargs["_request_timeout"] == ct.TIMEOUT


def test_patch_works_with_installed_api_client_request_boundary(monkeypatch):
    api = object.__new__(ct.KubernetesTransport)
    configuration = ct.client.Configuration(host="https://unit-test.invalid")
    configuration.api_key = {"authorization": "Bearer unit-test-token"}
    api.api_client = ct.client.ApiClient(configuration)
    monkeypatch.setattr(api.api_client, "call_api", _REAL_CALL_API.__get__(api.api_client))
    request = MagicMock(return_value=MagicMock(status=200, data=b"{}"))
    monkeypatch.setattr(api.api_client, "request", request)
    api.publish({"metadata": {"resourceVersion": "42"}}, {"version": 1})
    assert request.call_args.args == (
        "PATCH", "https://unit-test.invalid/api/v1/namespaces/troshka-operator/secrets/troshka-collector-transport")
    assert request.call_args.kwargs["headers"]["Content-Type"] == "application/merge-patch+json"
    assert request.call_args.kwargs["body"]["metadata"]["resourceVersion"] == "42"
    request.return_value.close.assert_called_once()
    request.return_value.release_conn.assert_called_once()
    api.api_client.close()


@pytest.mark.parametrize("changed,reason", [("cluster", "cluster_changed"), ("pod", "pod_changed"),
                                          ("fsid", "ceph_changed"), ("pool", "pools_changed")])
def test_live_storage_identity_checks_reject_mismatch(monkeypatch, changed, reason):
    api = object.__new__(ct.KubernetesTransport)
    api.configuration = ct.client.Configuration()
    api.api_client = ct.client.ApiClient(api.configuration)
    core = MagicMock()
    core.read_namespace.return_value = {"metadata": {"uid": "wrong" if changed == "cluster" else "cluster-uid"}}
    pod = identities()[2]
    live = copy.deepcopy(pod)
    if changed == "pod":
        live["metadata"]["uid"] = "wrong"
    core.read_namespaced_pod.return_value = live
    monkeypatch.setattr(ct.client, "CoreV1Api", lambda c: core)
    api.exec = MagicMock(side_effect=["wrong" if changed == "fsid" else "fsid",
                                     json.dumps([{"poolnum": 2 if changed == "pool" else 1, "poolname": "rbd"}])])
    with pytest.raises(ct.Rejected, match=reason):
        api.verify(configuration(), pod, "private-token")


def test_role_accepts_last_emitted_pod_but_never_arbitrary_pod():
    api = FakeAPI()
    ct.reconcile(api, now=1000)
    api.pod = copy.deepcopy(api.pod)
    api.pod["metadata"].update(name="tools-next", uid="next-uid")
    assert ct.reconcile(api, now=1001)["reason"] == "updated"
    assert api.publications == 2
    api.role["rules"] = ct.rules("unrelated-pod")
    with pytest.raises(ct.Rejected, match="role_scope_changed"):
        ct.reconcile(api, now=1002)


def test_new_rbac_has_only_named_escalation_and_no_exec():
    from pathlib import Path

    import yaml

    path = Path(__file__).parents[1] / "deploy" / "collector-maintenance.yaml"
    docs = list(yaml.safe_load_all(path.read_text()))
    roles = [doc for doc in docs if doc["kind"] in {"Role", "ClusterRole"}]
    for role in roles:
        for rule in role["rules"]:
            assert "pods/exec" not in rule["resources"]
            assert "*" not in rule["verbs"]
            if "escalate" in rule["verbs"]:
                assert rule["resourceNames"] == [ct.ROLE]
            if "serviceaccounts/token" in rule["resources"]:
                assert rule["resourceNames"] == ["troshka-storage-collector"]
    reader = next(d for d in roles if d["metadata"]["name"] == "troshka-collector-transport-reader")
    assert reader["rules"] == [{"apiGroups": [""], "resources": ["secrets"],
                                "resourceNames": [ct.OUTPUT], "verbs": ["get"]}]


def test_refresh_failure_status_has_heartbeat_without_credential_details(monkeypatch):
    from handlers import collector_transport as handler

    api = MagicMock()
    stop = MagicMock()
    stop.is_set.side_effect = [False, True]
    monkeypatch.setattr(handler, "_stop", stop)
    monkeypatch.setattr(handler, "KubernetesTransport", lambda: api)
    monkeypatch.setattr(handler, "reconcile", MagicMock(side_effect=RuntimeError("private-token")))
    monkeypatch.setattr(handler.time, "time", lambda: 1234)
    handler.refresh_loop()
    api.status.assert_called_once_with({"ready": False, "reason": "verification_failed", "observed_at": 1234})
    api.close.assert_called_once()
    stop.wait.assert_called_once_with(300)
