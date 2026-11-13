"""Tests for helpers.namespaced_rbac."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes import client

from kubernetes.client.exceptions import ApiException

from helpers.namespaced_rbac import (
    OPERATOR_NAMESPACED_ROLE,
    PROVIDER_NAMESPACED_ROLE,
    ensure_troshka_namespaced_rbac,
    _rolebinding_body,
)


def _existing_binding(name, namespace):
    provider = name == "troshka-provider-namespaced"
    body = _rolebinding_body(
        binding_name=name,
        namespace=namespace,
        cluster_role=name,
        sa_name="troshka" if provider else "troshka-operator",
        sa_namespace="troshka" if provider else "troshka-operator",
    )
    return client.ApiClient()._ApiClient__deserialize(body, "V1RoleBinding")


def test_external_reads_exact_bindings_only(monkeypatch):
    monkeypatch.setenv("TROSHKA_BASE_NAMESPACE_GRANTS_MODE", "external")
    rbac = MagicMock()
    rbac.read_namespaced_role_binding.side_effect = _existing_binding
    with patch(
        "helpers.namespaced_rbac.client.RbacAuthorizationV1Api", return_value=rbac
    ):
        ensure_troshka_namespaced_rbac("ns")
    assert [c[0] for c in rbac.mock_calls] == [
        "read_namespaced_role_binding",
        "read_namespaced_role_binding",
    ]
    assert [c.kwargs for c in rbac.mock_calls] == [
        {"name": "troshka-provider-namespaced", "namespace": "ns"},
        {"name": "troshka-operator-namespaced", "namespace": "ns"},
    ]


@pytest.mark.parametrize(
    "drift",
    [
        "name",
        "namespace",
        "deleting",
        "role-group",
        "role-kind",
        "role-name",
        "missing-subjects",
        "extra-subject",
        "duplicate-subject",
        "subject-kind",
        "subject-name",
        "subject-namespace",
        "subject-group",
    ],
)
def test_external_rejects_binding_drift_without_writes(monkeypatch, drift):
    monkeypatch.setenv("TROSHKA_BASE_NAMESPACE_GRANTS_MODE", "external")
    rbac = MagicMock()
    binding = _existing_binding("troshka-provider-namespaced", "ns")
    if drift in ("name", "namespace"):
        setattr(binding.metadata, drift, "wrong")
    elif drift == "deleting":
        binding.metadata.deletion_timestamp = "2026-11-12T00:00:00Z"
    elif drift.startswith("role-"):
        setattr(
            binding.role_ref, {"group": "api_group"}.get(drift[5:], drift[5:]), "wrong"
        )
    elif drift == "missing-subjects":
        binding.subjects = None
    elif drift == "extra-subject":
        binding.subjects.append(client.RbacV1Subject(kind="User", name="attacker"))
    elif drift == "duplicate-subject":
        binding.subjects.append(binding.subjects[0])
    else:
        setattr(
            binding.subjects[0],
            {"group": "api_group"}.get(drift[8:], drift[8:]),
            "wrong",
        )
    rbac.read_namespaced_role_binding.return_value = binding
    with patch(
        "helpers.namespaced_rbac.client.RbacAuthorizationV1Api", return_value=rbac
    ), pytest.raises(RuntimeError):
        ensure_troshka_namespaced_rbac("ns")
    assert [c[0] for c in rbac.mock_calls] == ["read_namespaced_role_binding"]


@pytest.mark.parametrize(
    "error",
    [
        ApiException(status=403, reason="secret-body"),
        ApiException(status=404),
        ApiException(status=500),
        TimeoutError("secret-body"),
    ],
)
def test_external_api_failures_are_bounded_and_stop(monkeypatch, error):
    monkeypatch.setenv("TROSHKA_BASE_NAMESPACE_GRANTS_MODE", "external")
    rbac = MagicMock()
    rbac.read_namespaced_role_binding.side_effect = error
    with patch(
        "helpers.namespaced_rbac.client.RbacAuthorizationV1Api", return_value=rbac
    ), pytest.raises(RuntimeError) as exc:
        ensure_troshka_namespaced_rbac("ns")
    assert "secret-body" not in str(exc.value)
    assert [c[0] for c in rbac.mock_calls] == ["read_namespaced_role_binding"]


@pytest.mark.parametrize(
    "value",
    ["", "null", "True", "False", "1", "External", " external", "external ", "unknown"],
)
def test_invalid_mode_fails_before_api_calls(monkeypatch, value):
    monkeypatch.setenv("TROSHKA_BASE_NAMESPACE_GRANTS_MODE", value)
    with patch(
        "helpers.namespaced_rbac.client.RbacAuthorizationV1Api"
    ) as api, pytest.raises(ValueError):
        ensure_troshka_namespaced_rbac("ns")
    api.assert_not_called()


@pytest.mark.parametrize("group", [None, ""])
def test_external_accepts_core_serviceaccount_group(monkeypatch, group):
    monkeypatch.setenv("TROSHKA_BASE_NAMESPACE_GRANTS_MODE", "external")
    rbac = MagicMock()

    def read(name, namespace):
        binding = _existing_binding(name, namespace)
        binding.subjects[0].api_group = group
        return binding

    rbac.read_namespaced_role_binding.side_effect = read
    with patch(
        "helpers.namespaced_rbac.client.RbacAuthorizationV1Api", return_value=rbac
    ):
        ensure_troshka_namespaced_rbac("ns")
    assert rbac.read_namespaced_role_binding.call_count == 2
    rbac.create_namespaced_role_binding.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"metadata": []},
        {
            "metadata": {"name": "troshka-provider-namespaced", "namespace": "ns"},
            "subjects": ["unexpected"],
        },
    ],
)
def test_external_malformed_serialized_binding_stops(monkeypatch, body):
    monkeypatch.setenv("TROSHKA_BASE_NAMESPACE_GRANTS_MODE", "external")
    rbac = MagicMock()
    rbac.read_namespaced_role_binding.return_value = body
    with patch(
        "helpers.namespaced_rbac.client.RbacAuthorizationV1Api", return_value=rbac
    ), pytest.raises(RuntimeError):
        ensure_troshka_namespaced_rbac("ns")
    assert [c[0] for c in rbac.mock_calls] == ["read_namespaced_role_binding"]


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


def _create_handler(module, name):
    import importlib
    import kopf

    handlers = importlib.import_module(module)
    for c in kopf.on.create.return_value.call_args_list:
        fn = c.args[0]
        if getattr(fn, "__name__", None) == name:
            return handlers, fn
    raise AssertionError(f"Missing handler {name}")


@pytest.mark.parametrize("handler", ["project", "vm"])
@pytest.mark.parametrize("mode", ["external", "invalid"])
def test_create_preflight_failure_prevents_status_and_writes(
    monkeypatch, handler, mode
):
    import asyncio

    module, fn = _create_handler(f"handlers.{handler}", f"{handler}_create")
    monkeypatch.setenv("TROSHKA_BASE_NAMESPACE_GRANTS_MODE", mode)
    events = MagicMock()
    events.rbac.read_namespaced_role_binding.side_effect = ApiException(status=403)
    patch_obj = MagicMock()
    patch_obj.status = {}
    kwargs = dict(
        meta={}, namespace="ns", name="test", body={"spec": {}}, patch=patch_obj
    )
    if handler == "project":
        kwargs["spec"] = {"action": "capture"}
    first_operation = (
        "_handle_capture" if handler == "project" else "_ensure_cpu_host_model"
    )
    events.next_operation.side_effect = RuntimeError("unexpected subsequent operation")
    with patch(
        "helpers.namespaced_rbac.client.RbacAuthorizationV1Api",
        return_value=events.rbac,
    ), patch.object(module.client, "CoreV1Api", events.core), patch.object(
        module.client, "CustomObjectsApi", events.custom
    ), patch.object(
        module, first_operation, events.next_operation
    ):
        with pytest.raises((RuntimeError, ValueError)):
            asyncio.run(fn(**kwargs))
    assert patch_obj.status == {}
    assert [c[0] for c in events.mock_calls] == (
        ["rbac.read_namespaced_role_binding"] if mode == "external" else []
    )


def test_external_vm_success_still_reaches_supplementary_exec_grant(monkeypatch):
    import asyncio

    module, fn = _create_handler("handlers.vm", "vm_create")
    monkeypatch.setenv("TROSHKA_BASE_NAMESPACE_GRANTS_MODE", "external")
    events = MagicMock()
    events.rbac.read_namespaced_role_binding.side_effect = _existing_binding
    events.cpu.return_value = {"spec": {}}
    events.exec_grant.side_effect = RuntimeError("stop at supplementary grant")
    patch_obj = MagicMock()
    patch_obj.status = {}
    with patch(
        "helpers.namespaced_rbac.client.RbacAuthorizationV1Api",
        return_value=events.rbac,
    ), patch.object(module, "_ensure_cpu_host_model", events.cpu), patch.object(
        module, "_ensure_provider_exec_rbac", events.exec_grant
    ), patch.object(
        module.client, "CoreV1Api"
    ), patch.object(
        module.client, "CustomObjectsApi"
    ), pytest.raises(
        RuntimeError, match="stop at supplementary grant"
    ):
        asyncio.run(
            fn(meta={}, namespace="ns", name="test", body={"spec": {}}, patch=patch_obj)
        )
    assert [c[0] for c in events.mock_calls] == [
        "rbac.read_namespaced_role_binding",
        "rbac.read_namespaced_role_binding",
        "cpu",
        "exec_grant",
    ]
    assert patch_obj.status == {"state": "Creating"}


@pytest.mark.parametrize("mode", ["managed", "external"])
def test_project_capture_continues_after_valid_external_or_failed_managed_grants(
    monkeypatch, mode
):
    import asyncio

    module, fn = _create_handler("handlers.project", "project_create")
    monkeypatch.setenv("TROSHKA_BASE_NAMESPACE_GRANTS_MODE", mode)
    events = MagicMock()
    events.capture = AsyncMock()
    events.rbac.read_namespaced_role_binding.side_effect = _existing_binding
    events.rbac.create_namespaced_role_binding.side_effect = ApiException(status=403)
    with patch(
        "helpers.namespaced_rbac.client.RbacAuthorizationV1Api",
        return_value=events.rbac,
    ), patch.object(module, "_handle_capture", events.capture):
        asyncio.run(
            fn(
                spec={"action": "capture"},
                meta={},
                namespace="ns",
                name="test",
                body={},
                patch=MagicMock(),
            )
        )
    assert [c[0] for c in events.mock_calls] == (
        ["rbac.read_namespaced_role_binding"] * 2
        if mode == "external"
        else ["rbac.create_namespaced_role_binding"]
    ) + ["capture"]
