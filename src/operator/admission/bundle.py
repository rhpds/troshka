"""Deterministic offline quarantine resources; no cluster or credential clients."""

import base64
import hashlib
import json
import re
import ssl
from dataclasses import asdict

from .bundle_policy import (
    NAME,
    ROLES,
    control_policy,
    lifetime_policy,
    webhook_protection,
)
from .models import ValidationError, dns_name, full_uid, parse_enrollments, require

_PRINCIPAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._:/-]{0,252}\Z")
_IMAGE_PATH = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
_IMAGE = re.compile(
    rf"(?:{_IMAGE_PATH}(?::[0-9]+)?/)?(?:{_IMAGE_PATH}/)*"
    rf"{_IMAGE_PATH}:[A-Za-z0-9_][A-Za-z0-9_.-]{{0,127}}\Z"
)
_MUTABLE_TAGS = {
    "latest",
    "main",
    "master",
    "production",
    "prod",
    "stable",
    "dev",
    "nightly",
    "edge",
}


def _principals(values):
    require(type(values) is list and 1 <= len(values) <= 128, "invalid_principals")
    require(
        all(type(value) is str and _PRINCIPAL.fullmatch(value) for value in values),
        "invalid_principals",
    )
    require(len(values) == len(set(values)), "invalid_principals")
    require(
        not {"system:anonymous", "system:unauthenticated"}.intersection(values),
        "invalid_principals",
    )
    return sorted(values)


def _identities(bootstrap_users, controller_users):
    bootstrap = _principals(bootstrap_users)
    require(
        type(controller_users) is dict and set(controller_users) == set(ROLES),
        "invalid_controllers",
    )
    controllers = {role: _principals(controller_users[role]) for role in ROLES}
    require(
        not set(bootstrap).intersection(
            user for values in controllers.values() for user in values
        ),
        "conflicting_principals",
    )
    return bootstrap, controllers


def _validate_ca(ca_pem):
    require(type(ca_pem) is str and 0 < len(ca_pem) <= 65536, "invalid_ca")
    require(
        "PRIVATE KEY" not in ca_pem
        and ca_pem.strip().startswith("-----BEGIN CERTIFICATE-----"),
        "invalid_ca",
    )
    require(
        re.fullmatch(
            r"(?:-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\s*)+",
            ca_pem.strip(),
        ),
        "invalid_ca",
    )
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=ca_pem)
        require(context.cert_store_stats()["x509_ca"] > 0, "invalid_ca")
    except (ssl.SSLError, ValueError):
        raise ValidationError("invalid_ca") from None


def _validate_settings(namespace, namespace_uid, image, tls_secret, ca_pem, nodes):
    require(
        dns_name(namespace, namespace=True) and full_uid(namespace_uid),
        "invalid_control_namespace",
    )
    require(dns_name(tls_secret), "invalid_tls_secret")
    require(
        type(image) is str and len(image) <= 512 and _IMAGE.fullmatch(image),
        "invalid_image",
    )
    tag = image.rsplit(":", 1)[1]
    require(
        tag.lower() not in _MUTABLE_TAGS and any(char.isdigit() for char in tag),
        "invalid_image",
    )
    require(type(nodes) is list and 1 <= len(nodes) <= 128, "invalid_scheduler_nodes")
    require(
        all(dns_name(node) for node in nodes) and len(nodes) == len(set(nodes)),
        "invalid_scheduler_nodes",
    )
    _validate_ca(ca_pem)


def _resource(kind, name, namespace, *, api="v1", **fields):
    return dict(
        apiVersion=api,
        kind=kind,
        metadata={"name": name, "namespace": namespace},
        **fields,
    )


def _container(image):
    return {
        "name": "admission",
        "image": image,
        "imagePullPolicy": "Always",
        "command": ["python3", "-m", "admission.server"],
        "args": [
            "--config",
            "/etc/quarantine/enrollments.json",
            "--tls-cert",
            "/etc/tls/tls.crt",
            "--tls-key",
            "/etc/tls/tls.key",
        ],
        "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
        "ports": [{"name": "https", "containerPort": 8443}],
        "readinessProbe": {
            "httpGet": {"path": "/readyz", "port": "https", "scheme": "HTTPS"},
            "periodSeconds": 3,
        },
        "livenessProbe": {
            "httpGet": {"path": "/healthz", "port": "https", "scheme": "HTTPS"},
            "periodSeconds": 10,
        },
        "resources": {
            "requests": {"cpu": "100m", "memory": "64Mi"},
            "limits": {"cpu": "500m", "memory": "256Mi"},
        },
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "volumeMounts": [
            {"name": "config", "mountPath": "/etc/quarantine", "readOnly": True},
            {"name": "tls", "mountPath": "/etc/tls", "readOnly": True},
        ],
    }


def _workload(namespace, image, tls_secret, config_name, config_json, nodes):
    labels = {"app.kubernetes.io/name": NAME}
    pod = {
        "serviceAccountName": NAME,
        "automountServiceAccountToken": False,
        "securityContext": {
            "runAsNonRoot": True,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "affinity": {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchFields": [
                                {
                                    "key": "metadata.name",
                                    "operator": "In",
                                    "values": nodes,
                                }
                            ]
                        }
                    ]
                }
            }
        },
        "containers": [_container(image)],
        "volumes": [
            {"name": "config", "configMap": {"name": config_name}},
            {"name": "tls", "secret": {"secretName": tls_secret, "defaultMode": 0o440}},
        ],
    }
    return [
        _resource(
            "ServiceAccount", NAME, namespace, automountServiceAccountToken=False
        ),
        _resource(
            "ConfigMap",
            config_name,
            namespace,
            immutable=True,
            data={"enrollments.json": config_json},
        ),
        _resource(
            "Deployment",
            NAME,
            namespace,
            api="apps/v1",
            spec={
                "replicas": 2,
                "revisionHistoryLimit": 2,
                "selector": {"matchLabels": labels},
                "strategy": {
                    "type": "RollingUpdate",
                    "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
                },
                "template": {"metadata": {"labels": labels}, "spec": pod},
            },
        ),
        _resource(
            "Service",
            NAME,
            namespace,
            spec={
                "selector": labels,
                "ports": [{"name": "https", "port": 443, "targetPort": "https"}],
            },
        ),
        _resource(
            "PodDisruptionBudget",
            NAME,
            namespace,
            api="policy/v1",
            spec={"minAvailable": 1, "selector": {"matchLabels": labels}},
        ),
    ]


def _webhook(namespace, ca_pem, entries):
    return {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingWebhookConfiguration",
        "metadata": {"name": NAME},
        "webhooks": [
            {
                "name": "quarantine.troshka.redhat.com",
                "failurePolicy": "Fail",
                "sideEffects": "None",
                "admissionReviewVersions": ["v1"],
                "matchPolicy": "Equivalent",
                "timeoutSeconds": 3,
                "clientConfig": {
                    "service": {
                        "namespace": namespace,
                        "name": NAME,
                        "path": "/review",
                        "port": 443,
                    },
                    "caBundle": base64.b64encode(ca_pem.encode()).decode(),
                },
                "rules": [
                    {
                        "operations": ["CREATE", "UPDATE", "CONNECT"],
                        "apiGroups": ["*"],
                        "apiVersions": ["*"],
                        "resources": ["*/*"],
                        "scope": "Namespaced",
                    }
                ],
                "matchConditions": [
                    {
                        "name": "exact-enrollment",
                        "expression": "request.namespace in "
                        + json.dumps([entry.namespace_name for entry in entries]),
                    }
                ],
            }
        ],
    }


def render_bundle(
    enrollments,
    *,
    control_namespace,
    control_namespace_uid,
    image,
    tls_secret,
    ca_pem,
    bootstrap_users,
    controller_users,
    scheduler_nodes,
):
    """Return protection, workload and activation stages, without writing/applying.

    A named release tag is required; registry-side immutability and actual image
    contents must be independently verified. No local parser can prove those facts.
    """
    entries = parse_enrollments(
        {"schema_version": 1, "enrollments": [asdict(entry) for entry in enrollments]}
    )
    entries = tuple(sorted(entries, key=lambda entry: entry.namespace_name))
    _validate_settings(
        control_namespace,
        control_namespace_uid,
        image,
        tls_secret,
        ca_pem,
        scheduler_nodes,
    )
    require(
        control_namespace not in {entry.namespace_name for entry in entries},
        "conflicting_namespace",
    )
    require(
        control_namespace_uid not in {entry.namespace_uid for entry in entries},
        "conflicting_namespace",
    )
    bootstrap, controllers = _identities(bootstrap_users, controller_users)
    nodes = sorted(scheduler_nodes)
    config_json = (
        json.dumps(
            {"schema_version": 1, "enrollments": [asdict(entry) for entry in entries]},
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    config_name = NAME + "-" + hashlib.sha256(config_json.encode()).hexdigest()[:16]
    protections = lifetime_policy(
        entries, control_namespace, control_namespace_uid, bootstrap
    )
    protections += control_policy(
        control_namespace, tls_secret, config_name, image, bootstrap, controllers, nodes
    )
    protections += webhook_protection(bootstrap)
    return {
        "00-protections": protections,
        "10-workload": _workload(
            control_namespace, image, tls_secret, config_name, config_json, nodes
        ),
        "20-activation": [_webhook(control_namespace, ca_pem, entries)],
    }
