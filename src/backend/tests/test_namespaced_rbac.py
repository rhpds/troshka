"""Unit tests for per-namespace provider/operator RoleBindings."""

from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from app.services.providers.kubevirt import (
    OPERATOR_NAMESPACED_ROLE,
    PROVIDER_NAMESPACED_ROLE,
    _ensure_namespaced_rolebinding,
    _rolebinding_body,
    ensure_troshka_namespaced_rbac,
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


@pytest.fixture
def grants_mode(monkeypatch):
    from app.core.config import config

    previous = config.get("kubevirt.base_namespace_grants_mode", "managed")

    def select(value):
        config.set("kubevirt.base_namespace_grants_mode", value)

    yield select
    config.set("kubevirt.base_namespace_grants_mode", previous)


def test_external_reads_exact_bindings_only(grants_mode):
    grants_mode("external")
    rbac = MagicMock()
    rbac.read_namespaced_role_binding.side_effect = _existing_binding
    ensure_troshka_namespaced_rbac(rbac, "ns")
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
def test_external_rejects_binding_drift_without_writes(grants_mode, drift):
    grants_mode("external")
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
    with pytest.raises(RuntimeError):
        ensure_troshka_namespaced_rbac(rbac, "ns")
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
def test_external_api_failures_are_bounded_and_stop(grants_mode, error):
    grants_mode("external")
    rbac = MagicMock()
    rbac.read_namespaced_role_binding.side_effect = error
    with pytest.raises(RuntimeError) as exc:
        ensure_troshka_namespaced_rbac(rbac, "ns")
    assert "secret-body" not in str(exc.value)
    assert [c[0] for c in rbac.mock_calls] == ["read_namespaced_role_binding"]


@pytest.mark.parametrize(
    "value",
    ["", None, True, False, 1, "External", " external", "external ", "unknown", [], {}],
)
def test_invalid_mode_fails_before_api_calls(grants_mode, value):
    grants_mode(value)
    rbac = MagicMock()
    with pytest.raises(ValueError):
        ensure_troshka_namespaced_rbac(rbac, "ns")
    assert rbac.mock_calls == []


@pytest.mark.parametrize("group", [None, ""])
def test_external_accepts_core_serviceaccount_group(grants_mode, group):
    grants_mode("external")
    rbac = MagicMock()

    def read(name, namespace):
        binding = _existing_binding(name, namespace)
        binding.subjects[0].api_group = group
        return binding

    rbac.read_namespaced_role_binding.side_effect = read
    ensure_troshka_namespaced_rbac(rbac, "ns")
    assert rbac.read_namespaced_role_binding.call_count == 2
    rbac.create_namespaced_role_binding.assert_not_called()


def test_deployment_env_override_selects_external_reads(monkeypatch):
    from dynaconf import Dynaconf

    from app.services.providers import base_namespace_grants

    monkeypatch.setenv("TROSHKA_KUBEVIRT__BASE_NAMESPACE_GRANTS_MODE", "external")
    settings = Dynaconf(envvar_prefix="TROSHKA", environments=False, load_dotenv=False)
    monkeypatch.setattr(base_namespace_grants, "config", settings)
    rbac = MagicMock()
    rbac.read_namespaced_role_binding.side_effect = _existing_binding
    ensure_troshka_namespaced_rbac(rbac, "ns")
    assert [c[0] for c in rbac.mock_calls] == ["read_namespaced_role_binding"] * 2


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
def test_external_malformed_serialized_binding_stops(grants_mode, body):
    grants_mode("external")
    rbac = MagicMock()
    rbac.read_namespaced_role_binding.return_value = body
    with pytest.raises(RuntimeError):
        ensure_troshka_namespaced_rbac(rbac, "ns")
    assert [c[0] for c in rbac.mock_calls] == ["read_namespaced_role_binding"]


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


def _namespace(name="ns", **kwargs):
    return client.V1Namespace(metadata=client.V1ObjectMeta(name=name, **kwargs))


def test_external_project_namespace_readonly_preserves_metadata(grants_mode):
    from app.services.providers.kubevirt import _ensure_project_namespace

    grants_mode("external")
    events = MagicMock()
    namespace = _namespace(labels={"unrelated": "preserved"})
    events.core.read_namespace.return_value = namespace
    events.rbac.read_namespaced_role_binding.side_effect = _existing_binding
    with patch("kubernetes.client.RbacAuthorizationV1Api", return_value=events.rbac):
        _ensure_project_namespace(events.core, "ns", "project-uuid")
    assert namespace.metadata.labels == {"unrelated": "preserved"}
    assert [c[0] for c in events.mock_calls] == [
        "core.read_namespace",
        "rbac.read_namespaced_role_binding",
        "rbac.read_namespaced_role_binding",
    ]


@pytest.mark.parametrize(
    "failure", ["missing", "forbidden", "timeout", "deleting", "name", "binding"]
)
def test_external_namespace_failure_cannot_create_patch_or_continue(
    grants_mode, failure
):
    from app.services.providers.kubevirt import _ensure_project_namespace

    grants_mode("external")
    events = MagicMock()
    events.core.read_namespace.return_value = _namespace()
    if failure in ("missing", "forbidden", "timeout"):
        events.core.read_namespace.side_effect = {
            "missing": ApiException(status=404),
            "forbidden": ApiException(status=403),
            "timeout": TimeoutError("secret-body"),
        }[failure]
    elif failure == "deleting":
        events.core.read_namespace.return_value = _namespace(
            deletion_timestamp="2026-11-12T00:00:00Z"
        )
    elif failure == "name":
        events.core.read_namespace.return_value = _namespace("wrong")
    events.rbac.read_namespaced_role_binding.side_effect = ApiException(status=404)
    with patch(
        "kubernetes.client.RbacAuthorizationV1Api", return_value=events.rbac
    ), pytest.raises(RuntimeError):
        _ensure_project_namespace(events.core, "ns", "project-uuid")
    assert [c[0] for c in events.mock_calls] == ["core.read_namespace"] + (
        ["rbac.read_namespaced_role_binding"] if failure == "binding" else []
    )


def test_external_provider_bootstrap_only_reads(grants_mode):
    from app.services.providers.kubevirt import _ensure_provider_rbac

    grants_mode("external")
    provider = MagicMock()
    provider.get_credentials.return_value = {"namespace": "custom-operator"}
    events = MagicMock()
    events.core.read_namespace.side_effect = lambda name: _namespace(name)
    events.rbac.read_namespaced_role_binding.side_effect = _existing_binding
    docs = [
        {"kind": kind, "metadata": {"name": "bootstrap"}}
        for kind in ("Namespace", "ServiceAccount", "ClusterRole", "ClusterRoleBinding")
    ]
    with patch(
        "app.services.providers.kubevirt._get_k8s_clients",
        return_value=(None, events.core, None),
    ), patch(
        "kubernetes.client.RbacAuthorizationV1Api", return_value=events.rbac
    ), patch(
        "app.services.providers.kubevirt._load_provider_rbac_docs", return_value=docs
    ), patch(
        "app.services.providers.kubevirt._apply_manifest"
    ) as apply:
        _ensure_provider_rbac(provider)
    apply.assert_not_called()
    assert [c.kwargs["name"] for c in events.core.read_namespace.call_args_list] == [
        "troshka",
        "troshka-cache",
        "custom-operator",
    ]
    assert [c[0] for c in events.mock_calls] == [
        "core.read_namespace",
        "rbac.read_namespaced_role_binding",
        "rbac.read_namespaced_role_binding",
    ] * 3 + ["rbac.read_cluster_role", "rbac.read_cluster_role_binding"]


def test_external_provider_bootstrap_failure_propagates(grants_mode):
    from app.services.providers.kubevirt import _ensure_provider_rbac

    grants_mode("external")
    events = MagicMock()
    events.core.read_namespace.return_value = _namespace("troshka")
    events.rbac.read_namespaced_role_binding.side_effect = ApiException(status=403)
    with patch(
        "app.services.providers.kubevirt._get_k8s_clients",
        return_value=(None, events.core, None),
    ), patch(
        "kubernetes.client.RbacAuthorizationV1Api", return_value=events.rbac
    ), patch(
        "app.services.providers.kubevirt._load_provider_rbac_docs", return_value=[]
    ), patch(
        "app.services.providers.kubevirt._apply_manifest"
    ) as apply, pytest.raises(
        RuntimeError
    ):
        _ensure_provider_rbac(MagicMock())
    apply.assert_not_called()
    assert [c[0] for c in events.mock_calls] == [
        "core.read_namespace",
        "rbac.read_namespaced_role_binding",
    ]


def test_managed_provider_bootstrap_binds_before_serviceaccounts(grants_mode):
    """Fresh clusters 403 on SA create until namespaced RoleBindings exist."""
    from app.services.providers.kubevirt import _ensure_provider_rbac

    grants_mode("managed")
    provider = MagicMock()
    provider.get_credentials.return_value = {"namespace": "troshka-operator"}
    docs = [
        {
            "kind": "Namespace",
            "metadata": {"name": "troshka"},
        },
        {
            "kind": "ServiceAccount",
            "metadata": {"name": "troshka", "namespace": "troshka"},
        },
        {"kind": "ClusterRole", "metadata": {"name": "troshka-provider"}},
        {"kind": "ClusterRoleBinding", "metadata": {"name": "troshka-provider"}},
        {"kind": "SecurityContextConstraints", "metadata": {"name": "skip-me"}},
    ]
    order = []

    def apply(kind, name, ns, body, core_api, apps_api):
        order.append(("apply", kind, name))

    def bind(rbac_api, namespace):
        order.append(("bind", namespace))

    with patch(
        "app.services.providers.kubevirt._get_k8s_clients",
        return_value=(None, MagicMock(), None),
    ), patch(
        "kubernetes.client.RbacAuthorizationV1Api", return_value=MagicMock()
    ), patch(
        "kubernetes.client.AppsV1Api", return_value=MagicMock()
    ), patch(
        "app.services.providers.kubevirt._load_provider_rbac_docs", return_value=docs
    ), patch(
        "app.services.providers.kubevirt._verify_cluster_rbac",
        side_effect=lambda *a, **k: order.append(("verify", a[1], a[2])),
    ), patch(
        "app.services.providers.kubevirt._apply_manifest", side_effect=apply
    ), patch(
        "app.services.providers.kubevirt.ensure_troshka_namespaced_rbac",
        side_effect=bind,
    ):
        _ensure_provider_rbac(provider)

    sa_idx = order.index(("apply", "ServiceAccount", "troshka"))
    assert ("verify", "ClusterRole", "troshka-provider") in order[:sa_idx]
    assert ("bind", "troshka") in order[:sa_idx]
    assert ("bind", "troshka-cache") in order[:sa_idx]
    assert ("bind", "troshka-operator") in order[:sa_idx]


@pytest.mark.parametrize("value", ["external", "invalid", None])
def test_operator_install_rejected_before_bootstrap(grants_mode, value):
    from app.services.providers.kubevirt import _deploy_operator

    grants_mode(value)
    with patch(
        "app.services.providers.kubevirt._ensure_provider_rbac"
    ) as bootstrap, patch(
        "app.services.providers.kubevirt._get_k8s_clients"
    ) as clients, pytest.raises(
        (RuntimeError, ValueError)
    ):
        _deploy_operator(MagicMock())
    bootstrap.assert_not_called()
    clients.assert_not_called()


def test_deploy_project_base_failure_blocks_later_secret_cache_cr_writes(
    grants_mode, monkeypatch
):
    from app.services.providers.kubevirt import KubeVirtDriver

    monkeypatch.setattr(
        "app.services.native_deployment_origin.capture_enabled", lambda: False
    )
    grants_mode("external")
    events = MagicMock()
    events.core.read_namespace.return_value = _namespace("troshka-project")
    events.rbac.read_namespaced_role_binding.side_effect = ApiException(status=403)
    with patch(
        "app.services.providers.kubevirt._ensure_operator_crds", events.crds
    ), patch(
        "app.services.providers.kubevirt._get_k8s_clients",
        return_value=(events.custom, events.core, None),
    ), patch(
        "app.services.providers.kubevirt._project_ns", return_value="troshka-project"
    ), patch(
        "kubernetes.client.RbacAuthorizationV1Api", return_value=events.rbac
    ), patch(
        "app.services.providers.kubevirt._ensure_project_s3_secrets", events.secrets
    ), pytest.raises(
        RuntimeError
    ):
        KubeVirtDriver().deploy_project(MagicMock(), "project-id", {}, {})
    # Earlier CRD reconciliation is separate and remains possible.
    assert [c[0] for c in events.mock_calls] == [
        "crds",
        "core.read_namespace",
        "rbac.read_namespaced_role_binding",
    ]
