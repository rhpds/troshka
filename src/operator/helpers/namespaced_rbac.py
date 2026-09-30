"""Per-namespace RoleBindings for provider/operator namespaced ClusterRoles."""

from __future__ import annotations

import logging

from kubernetes import client
from kubernetes.client.exceptions import ApiException

logger = logging.getLogger(__name__)

PROVIDER_NAMESPACED_ROLE = "troshka-provider-namespaced"
OPERATOR_NAMESPACED_ROLE = "troshka-operator-namespaced"
_PROVIDER_NS_BINDING = "troshka-provider-namespaced"
_OPERATOR_NS_BINDING = "troshka-operator-namespaced"
_RBAC_API = "rbac.authorization.k8s.io/v1"


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
    body = _rolebinding_body(
        binding_name=binding_name,
        namespace=namespace,
        cluster_role=cluster_role,
        sa_name=sa_name,
        sa_namespace=sa_namespace,
    )
    try:
        rbac_api.create_namespaced_role_binding(namespace=namespace, body=body)
    except ApiException as e:
        if e.status != 409:
            raise


def ensure_troshka_namespaced_rbac(
    namespace: str, *, operator_namespace: str = "troshka-operator"
) -> None:
    """Grant provider + operator namespaced mutate roles in ``namespace``."""
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
