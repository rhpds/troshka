"""Shared Kubernetes admission/digest shape normalization.

Server-side defaults and JSON number typing routinely diverge from the
exact body Troshka POSTed. Digests and support admission both use these
helpers so float/int and known OpenShift SA projection noise do not
false-fail create/bind/delete.
"""

import re
from copy import deepcopy
from typing import Any, cast

from app.services.kubevirt_storage_enrollment import canonicalize_json_numbers

_KUBE_API_ACCESS = re.compile(r"^kube-api-access-[a-z0-9]{5}$")

_OPENSHIFT_SERVICE_CA = {
    "configMap": {
        "name": "openshift-service-ca.crt",
        "items": [{"key": "service-ca.crt", "path": "service-ca.crt"}],
    }
}


def canonical_equal(left, right):
    """Exact equality after whole-number float→int canonicalize."""
    return canonicalize_json_numbers(left) == canonicalize_json_numbers(right)


def structural_contains(expected, observed):
    """True when ``expected`` is present in ``observed`` after number canonicalize.

    Dicts: every expected key must match (observed may have extras — API defaults).
    Lists: same length, pairwise contains (order matters for K8s list fields).
    Leaves: whole-number floats equal their int form.
    """
    if isinstance(expected, dict):
        return isinstance(observed, dict) and all(
            key in observed and structural_contains(value, observed[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(observed, list)
            and len(expected) == len(observed)
            and all(
                structural_contains(left, right)
                for left, right in zip(expected, observed, strict=True)
            )
        )
    return canonical_equal(expected, observed)


def _token_sources(*, openshift_service_ca: bool):
    sources = [
        {"serviceAccountToken": {"path": "token"}},
        {
            "configMap": {
                "name": "kube-root-ca.crt",
                "items": [{"key": "ca.crt", "path": "ca.crt"}],
            }
        },
        {
            "downwardAPI": {
                "items": [
                    {
                        "path": "namespace",
                        "fieldRef": {
                            "apiVersion": "v1",
                            "fieldPath": "metadata.namespace",
                        },
                    }
                ]
            }
        },
    ]
    if openshift_service_ca:
        sources.append(deepcopy(_OPENSHIFT_SERVICE_CA))
    return sources


def is_bound_service_account_volume(volume: Any) -> bool:
    """Recognize Kubernetes and exact OpenShift kube-api-access projections."""
    if not isinstance(volume, dict):
        return False
    raw = canonicalize_json_numbers(deepcopy(volume))
    if not isinstance(raw, dict):
        return False
    value = cast(dict[str, Any], raw)
    name = value.pop("name", "")
    if not isinstance(name, str) or _KUBE_API_ACCESS.fullmatch(name) is None:
        return False
    projected_raw = value.get("projected", {})
    if not isinstance(projected_raw, dict):
        return False
    projected = cast(dict[str, Any], projected_raw)
    if projected.get("defaultMode", 420) != 420:
        return False
    projected.pop("defaultMode", None)
    sources_raw = projected.get("sources", [])
    # Vanilla K8s: 3 sources. OpenShift adds openshift-service-ca.crt as a 4th.
    if not isinstance(sources_raw, list) or len(sources_raw) not in {3, 4}:
        return False
    sources = cast(list[Any], sources_raw)
    first = sources[0]
    if not isinstance(first, dict):
        return False
    token_raw = first.get("serviceAccountToken", {})
    if not isinstance(token_raw, dict):
        return False
    token = cast(dict[str, Any], token_raw)
    expiration = token.pop("expirationSeconds", 3607)
    if not (
        isinstance(expiration, int)
        and not isinstance(expiration, bool)
        and expiration > 0
    ):
        return False
    expected = {
        "projected": {"sources": _token_sources(openshift_service_ca=len(sources) == 4)}
    }
    return value == expected


def service_account_mount(volume_name):
    return {
        "name": volume_name,
        "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount",
        "readOnly": True,
    }
