"""Credential-safe, read-only transport checks; also embedded in fleet probes."""

import base64
import json
import math
import time

COLLECTOR_SUBJECT = "system:serviceaccount:troshka:troshka-storage-collector"
RENEW_MARGIN = 48 * 3600


def token_expiry(token):
    """Decode expiry for scheduling only; live TLS/API checks establish trust."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "==="))
        expiry = claims["exp"]
        if (claims["sub"] != COLLECTOR_SUBJECT or isinstance(expiry, bool)
                or not isinstance(expiry, (int, float)) or not math.isfinite(expiry)):
            raise ValueError
        return expiry
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("collector_identity_invalid") from None


def selected_credentials(config):
    def unique(items, name, field):
        matches = [row[field] for row in items if row["name"] == name]
        if len(matches) != 1:
            raise ValueError("ambiguous collector configuration")
        return matches[0]

    context = unique(config["contexts"], config["current-context"], "context")
    user = unique(config["users"], context["user"], "user")
    cluster = unique(config["clusters"], context["cluster"], "cluster")
    if set(user) != {"token"} or not isinstance(user["token"], str):
        raise ValueError("static collector token required")
    if (not cluster.get("server", "").startswith("https://")
            or cluster.get("insecure-skip-tls-verify") or cluster.get("proxy-url")
            or cluster.get("certificate-authority")):
        raise ValueError("verified TLS with system trust or embedded CA required")
    return user, cluster


def credential_health(config, *, now=None, margin=RENEW_MARGIN):
    try:
        user, _ = selected_credentials(config)
        expiry = token_expiry(user["token"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return {"blockers": ["collector_identity_invalid"]}
    remaining = expiry - (time.time() if now is None else now)
    code = "collector_token_expired" if remaining <= 0 else "collector_token_expiring"
    return {"expires_at": expiry, "blockers": [code] if remaining <= margin else []}


def access_error(error):
    return {401: "collector_unauthorized", 403: "collector_forbidden",
            404: "collector_toolbox_missing"}.get(getattr(error, "status", None),
                                                 "collector_transport_unavailable")


def transport_health(backend):
    """Never execute kubeconfig plugins or return provider exception text."""
    import yaml
    from kubernetes import client, config

    api = None
    try:
        with open(backend.kubeconfig) as stream:
            data = yaml.safe_load(stream)
        result = credential_health(data)
        if result["blockers"]:
            return result
        api = config.new_client_from_config_dict(data)
        core = client.CoreV1Api(api)
        toolbox = backend.toolbox
        identity = core.read_namespace("kube-system", _request_timeout=(5, 10))
        if identity.metadata.uid != toolbox.cluster_key:
            return {**result, "blockers": ["collector_cluster_changed"]}
        pod = core.read_namespaced_pod(toolbox.pod_name, toolbox.namespace, _request_timeout=(5, 10))
        if pod.metadata.uid != toolbox.pod_uid:
            result["blockers"] = ["collector_toolbox_replaced"]
        elif (pod.metadata.deletion_timestamp or pod.status.phase != "Running"
              or not any(c.name == toolbox.container and c.ready for c in pod.status.container_statuses or [])):
            result["blockers"] = ["collector_toolbox_not_ready"]
        return result
    except Exception as error:  # noqa: BLE001 -- never expose credential-bearing client text
        return {"blockers": [access_error(error)]}
    finally:
        if api is not None:
            api.close()
