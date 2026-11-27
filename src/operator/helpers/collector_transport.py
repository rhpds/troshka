"""Maintain only an explicitly approved, already enrolled collector transport.

All failures are reduced to fixed reason codes by the caller. Never log API
exceptions here: their bodies can contain TokenRequest or Secret credentials.
"""
import base64
import copy
import json
import math
import os
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

NAMESPACE = "troshka-operator"
CONFIG = "troshka-collector-maintenance"
OUTPUT = "troshka-collector-transport"
STATUS = "troshka-collector-maintenance-status"
ROLE = "troshka-storage-collector-toolbox"
TIMEOUT = (10, 30)
MARGIN = 48 * 3600
MIN_LIFETIME = 96 * 3600


class Rejected(ValueError):
    """Contains only a fixed, non-sensitive reason code."""


def require(condition, reason):
    if not condition:
        raise Rejected(reason)


def controller(value, kind, name, uid):
    refs = [r for r in value["metadata"].get("ownerReferences", []) if r.get("controller")]
    return len(refs) == 1 and all(refs[0].get(k) == v for k, v in
                                 (("kind", kind), ("name", name), ("uid", uid)))


def ready(pod, container):
    status = pod.get("status", {})
    return (not pod["metadata"].get("deletionTimestamp") and status.get("phase") == "Running"
            and any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", []))
            and any(c.get("name") == container and c.get("ready") for c in status.get("containerStatuses", [])))


def select_toolbox(deployment, replicas, pods, expected_uid, container):
    meta = deployment["metadata"]
    require(meta["uid"] == expected_uid and not meta.get("deletionTimestamp"), "deployment_changed")
    owned = [r for r in replicas if not r["metadata"].get("deletionTimestamp")
             and controller(r, "Deployment", meta["name"], meta["uid"])]
    candidates = [p for p in pods if ready(p, container) and any(
        controller(p, "ReplicaSet", r["metadata"]["name"], r["metadata"]["uid"]) for r in owned)]
    require(len(candidates) == 1, "toolbox_unavailable_or_ambiguous")
    return candidates[0]


def rules(pod):
    return [{"apiGroups": [""], "resources": [resource], "resourceNames": [pod], "verbs": [verb]}
            for resource, verb in (("pods", "get"), ("pods/exec", "create"))]


def role_patch(role, allowed, target):
    require(any(role.get("rules") == rules(name) for name in allowed), "role_scope_changed")
    return [{"op": "test", "path": "/metadata/resourceVersion", "value": role["metadata"]["resourceVersion"]},
            {"op": "replace", "path": "/rules", "value": rules(target)}]


def verify_binding(binding):
    require(binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": ROLE},
            "binding_changed")
    subjects = binding.get("subjects", [])
    require(len(subjects) == 1 and subjects[0].get("kind") == "ServiceAccount"
            and subjects[0].get("name") == "troshka-storage-collector"
            and subjects[0].get("namespace") == "troshka"
            and subjects[0].get("apiGroup", "") == "", "binding_changed")


def validate_config(value):
    dep, backend = value["deployment"], value["backend"]
    toolbox = backend["toolbox"]
    require(backend["mode"] == "toolbox" and bool(backend["pools"]), "config_invalid")
    require(toolbox["cluster_key"] == value["cluster_key"]
            and toolbox["namespace"] == dep["namespace"] and toolbox["container"] == dep["container"]
            and toolbox["ceph_fsid"] == backend["ceph_fsid"], "config_invalid")
    require(all(isinstance(dep[k], str) and dep[k] for k in ("namespace", "name", "uid", "container")),
            "config_invalid")


def decode_transport(secret):
    encoded = secret.get("data", {}).get("transport.json")
    return json.loads(base64.b64decode(encoded)) if encoded else None


def same_anchors(envelope, cfg):
    return (envelope.get("version") == 1 and envelope.get("cluster_key") == cfg["cluster_key"]
            and envelope.get("deployment_uid") == cfg["deployment"]["uid"]
            and envelope.get("ceph_fsid") == cfg["backend"]["ceph_fsid"]
            and envelope.get("pools") == cfg["backend"]["pools"])


def token_expiry(token):
    """Inspect API-issued claims, never use unverified JWTs as an authority."""
    try:
        parts = token.split(".")
        require(len(parts) == 3, "token_invalid")
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        expiry = claims["exp"]
        require(type(expiry) in (int, float) and math.isfinite(expiry), "token_invalid")
        require(claims.get("sub") == "system:serviceaccount:troshka:troshka-storage-collector", "token_identity_changed")
        return expiry
    except Rejected:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise Rejected("token_invalid") from None


class KubernetesTransport:
    def __init__(self):
        # Separate configuration, refreshed projected identity on every iteration.
        self.configuration = client.Configuration()
        config.load_incluster_config(client_configuration=self.configuration)
        self.api_client = client.ApiClient(self.configuration)
        self.core = client.CoreV1Api(self.api_client)
        self.apps = client.AppsV1Api(self.api_client)
        self.rbac = client.RbacAuthorizationV1Api(self.api_client)

    def close(self):
        self.api_client.close()

    def call(self, method, *args, **kwargs):
        return self.api_client.sanitize_for_serialization(method(*args, _request_timeout=TIMEOUT, **kwargs))

    def configuration_map(self):
        try:
            return self.call(self.core.read_namespaced_config_map, CONFIG, NAMESPACE)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise

    def inspect(self, cfg):
        dep = cfg["deployment"]
        namespace = dep["namespace"]
        deployment = self.call(self.apps.read_namespaced_deployment, dep["name"], namespace)
        replicas = self.call(self.apps.list_namespaced_replica_set, namespace)["items"]
        pods = self.call(self.core.list_namespaced_pod, namespace)["items"]
        pod = select_toolbox(deployment, replicas, pods, dep["uid"], dep["container"])
        role = self.call(self.rbac.read_namespaced_role, ROLE, namespace)
        verify_binding(self.call(self.rbac.read_namespaced_role_binding, ROLE, namespace))
        return pod, role

    def issue(self):
        result = self.call(self.core.create_namespaced_service_account_token, "troshka-storage-collector", "troshka",
                           {"apiVersion": "authentication.k8s.io/v1", "kind": "TokenRequest",
                            "spec": {"expirationSeconds": 168 * 3600}})
        status = result["status"]
        reported = datetime.fromisoformat(status["expirationTimestamp"].replace("Z", "+00:00")).timestamp()
        expiry = token_expiry(status["token"])
        require(abs(reported - expiry) <= 1, "token_expiry_mismatch")
        require(expiry >= time.time() + MIN_LIFETIME, "token_lifetime_short")
        return status["token"], expiry

    def retarget(self, cfg, patch):
        self.patch("/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/roles/{name}",
                   cfg["deployment"]["namespace"], ROLE, patch, "application/json-patch+json")

    def patch(self, path, namespace, name, body, content_type):
        # Kubernetes 31 generated methods lack _content_type overrides.
        response = self.api_client.call_api(
            path, "PATCH", path_params={"namespace": namespace, "name": name},
            header_params={"Content-Type": content_type, "Accept": "application/json"},
            body=body, auth_settings=["BearerToken"],
            _return_http_data_only=True, _preload_content=False, _request_timeout=TIMEOUT)
        # No response body is needed. Bypass version-specific deserialization
        # arguments and release the raw response on both Kubernetes 31 and 36.
        response.close()
        response.release_conn()

    def verify(self, cfg, pod, token):
        isolated = copy.deepcopy(self.configuration)
        isolated.api_key = {"authorization": "Bearer " + token}
        isolated.api_key_prefix = {}
        isolated.refresh_api_key_hook = None
        with client.ApiClient(isolated) as api_client:
            core = client.CoreV1Api(api_client)
            cluster = self.call(core.read_namespace, "kube-system")
            require(cluster["metadata"]["uid"] == cfg["cluster_key"], "cluster_changed")
            live = self.call(core.read_namespaced_pod, pod["metadata"]["name"], cfg["deployment"]["namespace"])
            require(live["metadata"]["uid"] == pod["metadata"]["uid"], "pod_changed")
            fsid = self.exec(isolated, cfg, pod, ["fsid"]).strip()
            require(fsid == cfg["backend"]["ceph_fsid"], "ceph_changed")
            pools = json.loads(self.exec(isolated, cfg, pod, ["osd", "lspools", "--format", "json"]))
            found = {(p["poolnum"], p["poolname"]) for p in pools}
            require(all((p["pool_id"], p["name"]) in found for p in cfg["backend"]["pools"]), "pools_changed")

    @staticmethod
    def exec(configuration, cfg, pod, command):
        # Python stream ignores the method and always performs WebSocket GET.
        # Use oc's POST/SPDY transport to preserve create-only pod exec scope.
        require(configuration.verify_ssl and bool(configuration.ssl_ca_cert), "tls_config_invalid")
        cluster = {"server": configuration.host, "certificate-authority": configuration.ssl_ca_cert}
        if configuration.tls_server_name:
            cluster["tls-server-name"] = configuration.tls_server_name
        kubeconfig = {"apiVersion": "v1", "kind": "Config", "current-context": "collector",
                      "clusters": [{"name": "collector", "cluster": cluster}],
                      "users": [{"name": "collector", "user": {
                          "token": configuration.api_key["authorization"].removeprefix("Bearer ")}}],
                      "contexts": [{"name": "collector", "context": {"cluster": "collector", "user": "collector"}}]}
        with tempfile.TemporaryDirectory(prefix="collector-exec-") as directory:
            path = Path(directory) / "config"
            path.touch(mode=0o600)
            path.write_text(json.dumps(kubeconfig))
            environment = {**os.environ, "KUBECTL_REMOTE_COMMAND_WEBSOCKETS": "false"}
            result = subprocess.run(
                ["oc", "--kubeconfig=" + str(path), "--request-timeout=30s", "exec",
                 "-n", cfg["deployment"]["namespace"], pod["metadata"]["name"],
                 "-c", cfg["deployment"]["container"], "--", "ceph", "--connect-timeout", "5", *command],
                capture_output=True, text=True, timeout=40, check=False, env=environment)
            require(result.returncode == 0, "ceph_exec_failed")
            return result.stdout

    def read_output(self):
        return self.call(self.core.read_namespaced_secret, OUTPUT, NAMESPACE)

    def publish(self, previous, envelope):
        body = {"metadata": {"resourceVersion": previous["metadata"]["resourceVersion"]},
                "data": {"transport.json": base64.b64encode(json.dumps(envelope).encode()).decode()}}
        self.patch("/api/v1/namespaces/{namespace}/secrets/{name}", NAMESPACE, OUTPUT, body,
                   "application/merge-patch+json")

    def status(self, value):
        previous = self.call(self.core.read_namespaced_config_map, STATUS, NAMESPACE)
        self.patch("/api/v1/namespaces/{namespace}/configmaps/{name}", NAMESPACE, STATUS,
                   {"metadata": {"resourceVersion": previous["metadata"]["resourceVersion"]},
                    "data": {"status.json": json.dumps(value)}}, "application/merge-patch+json")


def reconcile(api, now=None):
    cm = api.configuration_map()
    if cm is None:
        return {"ready": False, "reason": "disabled"}
    cfg = json.loads(cm["data"]["config"])
    validate_config(cfg)
    secret = api.read_output()
    previous = decode_transport(secret)
    if previous:
        require(same_anchors(previous, cfg), "published_anchors_changed")
    pod, role = api.inspect(cfg)
    meta = pod["metadata"]
    allowed = {cfg["backend"]["toolbox"]["pod_name"], meta["name"]}
    if previous:
        allowed.add(previous["pod_name"])
    patch = role_patch(role, allowed, meta["name"])
    current_time = time.time() if now is None else now
    unchanged = (previous and previous["pod_uid"] == meta["uid"] and previous["pod_name"] == meta["name"]
                 and previous["expires_at"] > current_time + MARGIN)
    token, expiry = (previous["token"], previous["expires_at"]) if unchanged else api.issue()
    if role["rules"] != rules(meta["name"]):
        api.retarget(cfg, patch)
    api.verify(cfg, pod, token)
    # Fence config/ownership drift during remote verification before publication.
    require(api.configuration_map() == cm, "config_changed")
    final_pod, final_role = api.inspect(cfg)
    require(final_pod["metadata"]["uid"] == meta["uid"], "pod_changed")
    require(final_role["rules"] == rules(meta["name"]), "role_scope_changed")
    if not unchanged:
        api.publish(secret, {"version": 1, "cluster_key": cfg["cluster_key"],
                    "deployment_uid": cfg["deployment"]["uid"], "pod_name": meta["name"],
                    "pod_uid": meta["uid"], "token": token, "expires_at": expiry,
                    "ceph_fsid": cfg["backend"]["ceph_fsid"], "pools": cfg["backend"]["pools"]})
    return {"ready": True, "reason": "healthy" if unchanged else "updated",
            "observed_pod_uid": meta["uid"], "expires_at": expiry}
