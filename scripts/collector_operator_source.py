"""Consume operator credentials using provider identity and protected collector TLS.

Provider credentials never leave the short-lived API client or enter publication
evidence. Envelope contents are private and must never appear in diagnostics.
"""

import base64
import copy
import json
import time

from collector_provision import require
from collector_transport_health import RENEW_MARGIN, selected_credentials, token_expiry


def provider_credentials(provider_id):
    from app.core.database import SessionLocal
    from app.models.provider import Provider

    with SessionLocal() as session:
        provider = session.get(Provider, str(provider_id))
        require(provider is not None and provider.type == "kubevirt", "pinned provider unavailable")
        return provider.get_credentials()


def provider_config(config, credentials):
    _, cluster = selected_credentials(config)
    require(credentials.get("api_url") == cluster["server"], "provider server differs from protected server")
    require(isinstance(credentials.get("token"), str) and bool(credentials["token"]), "provider token unavailable")
    # Build a minimal static configuration: never copy provider TLS defaults or plugins.
    tls = {k: copy.deepcopy(cluster[k]) for k in
           ("server", "certificate-authority-data", "tls-server-name") if k in cluster}
    return {"apiVersion": "v1", "kind": "Config", "current-context": "operator-source",
            "clusters": [{"name": "storage", "cluster": tls}],
            "users": [{"name": "provider", "user": {"token": credentials["token"]}}],
            "contexts": [{"name": "operator-source", "context": {"cluster": "storage", "user": "provider"}}]}


def fetch_envelope(config, pins):
    from kubernetes import client
    from kubernetes import config as kube_config

    require(len(pins["provider_clusters"]) == 1, "one pinned provider required")
    credentials = provider_credentials(pins["provider_clusters"][0]["provider_id"])
    api = kube_config.new_client_from_config_dict(provider_config(config, credentials))
    try:
        secret = client.CoreV1Api(api).read_namespaced_secret(
            "troshka-collector-transport", "troshka-operator", _request_timeout=(5, 10))
        require(set(secret.data or {}) == {"transport.json"}, "operator transport secret malformed")
        raw = base64.b64decode(secret.data["transport.json"], validate=True)
        require(len(raw) <= 65536, "operator transport too large")
        return json.loads(raw)
    finally:
        api.close()


def validate_envelope(envelope, source, backend, pins, *, now=None):
    require(isinstance(envelope, dict), "operator transport malformed")
    fields = {"version", "cluster_key", "deployment_uid", "pod_name", "pod_uid",
              "token", "expires_at", "ceph_fsid", "pools"}
    require(set(envelope) == fields and type(envelope["version"]) is int
            and envelope["version"] == 1, "operator transport version or fields invalid")
    trusted, toolbox = source["toolbox"], backend["toolbox"]
    require(len(pins["provider_clusters"]) == 1, "one pinned provider required")
    require(envelope["cluster_key"] == toolbox["cluster_key"]
            == pins["provider_clusters"][0]["cluster_key"], "operator cluster differs from protected pins")
    require(envelope["deployment_uid"] == trusted["deployment_uid"]
            and bool(trusted["deployment_uid"]), "operator Deployment replaced")
    require(trusted["namespace"] == toolbox["namespace"] and trusted["container"] == toolbox["container"],
            "operator scope differs from protected toolbox")
    require(envelope["ceph_fsid"] == toolbox["ceph_fsid"] == backend["ceph_fsid"], "operator Ceph identity changed")
    require(envelope["pools"] == backend["pools"], "operator pool identities changed")
    configured = {(backend["ceph_fsid"], p["pool_id"]) for p in backend["pools"]}
    pinned = {(p["ceph_fsid"], p["pool_id"]) for p in pins["pool_keys"]}
    require(configured == pinned and bool(pinned), "operator pools differ from protected pins")
    require(all(isinstance(envelope[k], str) and bool(envelope[k]) for k in ("pod_name", "pod_uid", "token")),
            "operator transport identity missing")
    expiry = token_expiry(envelope["token"])
    require(type(envelope["expires_at"]) in (int, float) and expiry == envelope["expires_at"]
            and expiry > (time.time() if now is None else now) + RENEW_MARGIN,
            "operator token expiry invalid or renewal due")


def prepare(source, backends, pins, config, key, backend_key):
    envelope = fetch_envelope(config, pins)
    validate_envelope(envelope, source, backends[0], pins)
    toolbox = backends[0]["toolbox"]
    changed = (envelope["pod_name"], envelope["pod_uid"]) != (toolbox["pod_name"], toolbox["pod_uid"])
    change = {"mode": "operator", "source": source, "backends": backends, "pins": pins,
              "config": config, "key": key, "backend_key": backend_key, "envelope": envelope,
              "pod": {"metadata": {"name": envelope["pod_name"], "uid": envelope["pod_uid"]}},
              "toolbox_changed": changed}
    verify(change)
    return change, changed or envelope["token"] != selected_credentials(config)[0]["token"]


def publication(change):
    config, backends = copy.deepcopy(change["config"]), copy.deepcopy(change["backends"])
    envelope = change["envelope"]
    selected_credentials(config)[0]["token"] = envelope["token"]
    backends[0]["toolbox"].update(pod_name=envelope["pod_name"], pod_uid=envelope["pod_uid"])
    return config, backends


def verify(change):
    from collector_maintenance import verify_current_transport

    config, backends = publication(change)
    verify_current_transport({**change, "config": config, "backends": backends})


def recheck(change):
    envelope = fetch_envelope(change["config"], change["pins"])
    validate_envelope(envelope, change["source"], change["backends"][0], change["pins"])
    require(envelope == change["envelope"], "operator transport changed during preparation")


def renew(change):
    recheck(change)
    verify(change)
    config, backends = publication(change)
    return base64.b64encode(json.dumps(config).encode()).decode(), json.dumps(backends, indent=2)
