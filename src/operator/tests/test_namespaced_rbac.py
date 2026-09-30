"""Tests for helpers.namespaced_rbac."""

from unittest.mock import MagicMock, patch

from kubernetes.client.exceptions import ApiException

from helpers.namespaced_rbac import (
    OPERATOR_NAMESPACED_ROLE,
    PROVIDER_NAMESPACED_ROLE,
    ensure_troshka_namespaced_rbac,
)


def test_ensure_creates_both_bindings():
    rbac = MagicMock()
    with patch(
        "helpers.namespaced_rbac.client.RbacAuthorizationV1Api", return_value=rbac
    ):
        ensure_troshka_namespaced_rbac("troshka-abc12345")
    assert rbac.create_namespaced_role_binding.call_count == 2
    bodies = [
        c.kwargs["body"] for c in rbac.create_namespaced_role_binding.call_args_list
    ]
    roles = {b["roleRef"]["name"] for b in bodies}
    assert roles == {PROVIDER_NAMESPACED_ROLE, OPERATOR_NAMESPACED_ROLE}


def test_ensure_ignores_existing_bindings():
    rbac = MagicMock()
    rbac.create_namespaced_role_binding.side_effect = ApiException(status=409)
    with patch(
        "helpers.namespaced_rbac.client.RbacAuthorizationV1Api", return_value=rbac
    ):
        ensure_troshka_namespaced_rbac("troshka-abc12345")  # must not raise
