"""Per-namespace RoleBindings for provider/operator namespaced ClusterRoles."""

from __future__ import annotations

import logging
import os

from kubernetes import client
from kubernetes.client.exceptions import ApiException

logger = logging.getLogger(__name__)

PROVIDER_NAMESPACED_ROLE = "troshka-provider-namespaced"
OPERATOR_NAMESPACED_ROLE = "troshka-operator-namespaced"
_PROVIDER_NS_BINDING = "troshka-provider-namespaced"
_OPERATOR_NS_BINDING = "troshka-operator-namespaced"
_RBAC_API = "rbac.authorization.k8s.io/v1"


class BaseNamespaceGrantsError(RuntimeError):
    """An externally administered base grant could not be verified."""


class BaseNamespaceGrantsModeError(ValueError):
    """Deployment-owned base grants mode is invalid."""


def base_namespace_grants_mode() -> str:
    mode = os.environ.get("TROSHKA_BASE_NAMESPACE_GRANTS_MODE", "managed")
    if mode not in ("managed", "external"):
        raise BaseNamespaceGrantsModeError(
            "TROSHKA_BASE_NAMESPACE_GRANTS_MODE must be managed or external"
        )
    return mode


def _read_binding(rbac_api, namespace: str, name: str) -> dict:
    try:
        resource = rbac_api.read_namespaced_role_binding(name=name, namespace=namespace)
        result = client.ApiClient().sanitize_for_serialization(resource)
        if not isinstance(result, dict):
            raise TypeError("unexpected binding representation")
        return result
    except Exception:
        raise BaseNamespaceGrantsError(
            "Cannot verify externally administered base namespace grants"
        ) from None


def _subjects_match(actual, expected: list) -> bool:
    if (
        not isinstance(actual, list)
        or len(actual) != len(expected)
        or not isinstance(actual[0], dict)
    ):
        return False
    subject = dict(actual[0])
    group = subject.pop("apiGroup", "")
    return group in (None, "") and subject == expected[0]


def _verify_binding(rbac_api, namespace: str, expected: dict) -> None:
    name = expected["metadata"]["name"]
    actual = _read_binding(rbac_api, namespace, name)
    metadata = actual.get("metadata") or {}
    if not (
        isinstance(metadata, dict)
        and metadata.get("name") == name
        and metadata.get("namespace") == namespace
        and not metadata.get("deletionTimestamp")
        and actual.get("roleRef") == expected["roleRef"]
        and _subjects_match(actual.get("subjects"), expected["subjects"])
    ):
        raise BaseNamespaceGrantsError(
            "Externally administered base binding does not match"
        )


def preflight_external_base_namespace_grants(namespace: str) -> None:
    """Validate mode and external grants before create-handler side effects."""
    if base_namespace_grants_mode() == "external":
        ensure_troshka_namespaced_rbac(namespace)


def ensure_managed_base_namespace_grants(namespace: str) -> None:
    """Keep legacy best-effort management at its original handler position."""
    if base_namespace_grants_mode() == "external":
        return
    try:
        ensure_troshka_namespaced_rbac(namespace)
    except (BaseNamespaceGrantsError, BaseNamespaceGrantsModeError):
        raise
    except Exception:
        logger.warning(
            "Failed to ensure namespaced RBAC in %s (continuing)",
            namespace,
            exc_info=True,
        )


def _rolebinding_body(
    *,
    binding_name: str,
    namespace: str,
    cluster_role: str,
    sa_name: str,
    sa_namespace: str,
) -> dict:
    return {
        "apiVersion": _RBAC_API,
        "kind": "RoleBinding",
        "metadata": {"name": binding_name, "namespace": namespace},
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": cluster_role,
        },
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": sa_name,
                "namespace": sa_namespace,
            }
        ],
    }


def _ensure_binding(
    rbac_api,
    namespace: str,
    *,
    binding_name: str,
    cluster_role: str,
    sa_name: str,
    sa_namespace: str,
) -> None:
    mode = base_namespace_grants_mode()
    body = _rolebinding_body(
        binding_name=binding_name,
        namespace=namespace,
        cluster_role=cluster_role,
        sa_name=sa_name,
        sa_namespace=sa_namespace,
    )
    if mode == "external":
        _verify_binding(rbac_api, namespace, body)
        return
    try:
        rbac_api.create_namespaced_role_binding(namespace=namespace, body=body)
    except ApiException as e:
        if e.status != 409:
            raise


def ensure_troshka_namespaced_rbac(
    namespace: str, *, operator_namespace: str = "troshka-operator"
) -> None:
    """Grant provider + operator namespaced mutate roles in ``namespace``."""
    base_namespace_grants_mode()
    rbac_api = client.RbacAuthorizationV1Api()
    _ensure_binding(
        rbac_api,
        namespace,
        binding_name=_PROVIDER_NS_BINDING,
        cluster_role=PROVIDER_NAMESPACED_ROLE,
        sa_name="troshka",
        sa_namespace="troshka",
    )
    _ensure_binding(
        rbac_api,
        namespace,
        binding_name=_OPERATOR_NS_BINDING,
        cluster_role=OPERATOR_NAMESPACED_ROLE,
        sa_name="troshka-operator",
        sa_namespace=operator_namespace,
    )


def ensure_collector_namespace_grants(namespace: str) -> None:
    """Install project-only read access; reject a conflicting existing binding."""
    api = client.RbacAuthorizationV1Api()
    name = "troshka-storage-collector-namespaced"
    body = _rolebinding_body(
        binding_name=name, namespace=namespace, cluster_role=name,
        sa_name="troshka-storage-collector", sa_namespace="troshka",
    )
    if base_namespace_grants_mode() == "external":
        _verify_binding(api, namespace, body)
        return
    try:
        api.create_namespaced_role_binding(namespace=namespace, body=body)
    except ApiException as error:
        if error.status != 409:
            raise
        _verify_binding(api, namespace, body)
