"""New projects give the collector scoped reads with exact cleanup identity."""

from unittest.mock import MagicMock, patch

import pytest
from helpers import managed_power
from helpers import namespaced_rbac as rbac
from kubernetes.client.exceptions import ApiException


def test_collector_binding_matches_cleanup_contract():
    api = MagicMock()
    with patch.object(rbac.client, "RbacAuthorizationV1Api", return_value=api):
        rbac.ensure_collector_namespace_grants("troshka-test")
    body = api.create_namespaced_role_binding.call_args.kwargs["body"]
    expected = managed_power.cleanup_grant_body(body["metadata"]["name"], "troshka-test")
    assert expected is not None
    assert body["roleRef"] == expected["roleRef"]
    assert body["subjects"] == expected["subjects"]
    assert body["subjects"][0]["name"] == "troshka-storage-collector"


def test_existing_collector_binding_is_verified_not_adopted():
    api = MagicMock()
    api.create_namespaced_role_binding.side_effect = ApiException(status=409)
    api.read_namespaced_role_binding.return_value = {
        "metadata": {"name": "troshka-storage-collector-namespaced", "namespace": "ns"},
        "roleRef": {"kind": "ClusterRole", "name": "wrong"}, "subjects": [],
    }
    with (
        patch.object(rbac.client, "RbacAuthorizationV1Api", return_value=api),
        pytest.raises(rbac.BaseNamespaceGrantsError),
    ):
        rbac.ensure_collector_namespace_grants("ns")
