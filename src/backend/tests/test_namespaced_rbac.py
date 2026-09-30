"""Unit tests for per-namespace provider/operator RoleBindings."""

from unittest.mock import MagicMock, patch

from kubernetes.client.exceptions import ApiException

from app.services.providers.kubevirt import (
    OPERATOR_NAMESPACED_ROLE,
    PROVIDER_NAMESPACED_ROLE,
    _ensure_namespaced_rolebinding,
    ensure_troshka_namespaced_rbac,
)


def test_ensure_namespaced_rolebinding_creates_binding():
    rbac = MagicMock()
    _ensure_namespaced_rolebinding(
        rbac,
        "troshka-abc12345",
        binding_name="troshka-provider-namespaced",
        cluster_role=PROVIDER_NAMESPACED_ROLE,
        sa_name="troshka",
        sa_namespace="troshka",
    )
    rbac.create_namespaced_role_binding.assert_called_once()
    kw = rbac.create_namespaced_role_binding.call_args.kwargs
    assert kw["namespace"] == "troshka-abc12345"
    body = kw["body"]
    assert body["roleRef"]["name"] == PROVIDER_NAMESPACED_ROLE
    assert body["subjects"][0]["name"] == "troshka"


def test_ensure_namespaced_rolebinding_ignores_409():
    rbac = MagicMock()
    rbac.create_namespaced_role_binding.side_effect = ApiException(status=409)
    _ensure_namespaced_rolebinding(
        rbac,
        "ns",
        binding_name="b",
        cluster_role="r",
        sa_name="sa",
        sa_namespace="sa-ns",
    )


def test_ensure_troshka_namespaced_rbac_binds_provider_and_operator():
    rbac = MagicMock()
    with patch(
        "app.services.providers.kubevirt._ensure_namespaced_rolebinding"
    ) as ensure:
        ensure_troshka_namespaced_rbac(rbac, "troshka-deadbeef")
    assert ensure.call_count == 2
    roles = {c.kwargs["cluster_role"] for c in ensure.call_args_list}
    assert roles == {PROVIDER_NAMESPACED_ROLE, OPERATOR_NAMESPACED_ROLE}
