"""Tests for uncovered kubevirt provider functions.

Covers: _operator_ns, _ensure_s3_secret, _try_existing_cluster_resource,
_handle_create_conflict, _apply_manifest, _deploy_operator,
_poll_and_cleanup_attachments, _cleanup_volume_attachments,
_query_cluster_capacity, _query_ceph_storage_gb,
KubeVirtDriver.delete_console, allocate_eip, release_eip,
update_eip_ports, get_project_status, get_vm_states,
_detect_vnc_state, _parse_vnc_markers, _parse_console_output.
"""

import os

os.environ.setdefault("TROSHKA_DATABASE__URL", "sqlite:///./test.db")

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from app.services.providers.kubevirt import (
    KubeVirtDriver,
    _apply_manifest,
    _cleanup_volume_attachments,
    _detect_vnc_state,
    _handle_create_conflict,
    _operator_ns,
    _parse_console_output,
    _parse_vnc_markers,
    _poll_and_cleanup_attachments,
    _query_ceph_storage_gb,
    _query_cluster_capacity,
    _try_existing_cluster_resource,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(**overrides):
    p = MagicMock()
    p.type = "kubevirt"
    creds = {
        "api_url": "https://api.cluster.example.com:6443",
        "token": "test-token",
        "namespace": "troshka",
        "verify_ssl": False,
    }
    creds.update(overrides)
    p.get_credentials.return_value = creds
    return p


def _make_api_exception(status, reason="Conflict"):
    """Create a real kubernetes ApiException with the given status."""
    return ApiException(status=status, reason=reason)


# ===========================================================================
# _operator_ns
# ===========================================================================


class TestOperatorNs:
    def test_default_namespace(self):
        provider = MagicMock()
        provider.get_credentials.return_value = {}
        assert _operator_ns(provider) == "troshka-operator"

    def test_custom_namespace(self):
        provider = _make_provider(namespace="my-ns")
        assert _operator_ns(provider) == "my-ns"


# ===========================================================================
# _ensure_s3_secret
# ===========================================================================


class TestEnsureS3Secret:
    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_creates_secret_successfully(self, mock_clients):
        core = MagicMock()
        mock_clients.return_value = (MagicMock(), core, MagicMock())
        provider = _make_provider()
        s3 = {
            "access_key_id": "AKID",
            "secret_access_key": "SKEY",
            "region": "us-west-2",
        }
        from app.services.providers.kubevirt import _ensure_s3_secret

        _ensure_s3_secret(provider, "ns-test", s3)
        core.create_namespaced_secret.assert_called_once()
        body = core.create_namespaced_secret.call_args.kwargs["body"]
        assert body.string_data["accessKeyId"] == "AKID"
        assert body.string_data["secretKey"] == "SKEY"
        assert body.string_data["AWS_DEFAULT_REGION"] == "us-west-2"

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_patches_on_already_exists(self, mock_clients):
        core = MagicMock()
        core.create_namespaced_secret.side_effect = Exception("AlreadyExists")
        mock_clients.return_value = (MagicMock(), core, MagicMock())
        provider = _make_provider()
        from app.services.providers.kubevirt import _ensure_s3_secret

        _ensure_s3_secret(
            provider, "ns-test", {"access_key_id": "A", "secret_access_key": "B"}
        )
        core.patch_namespaced_secret.assert_called_once()

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_raises_on_other_error(self, mock_clients):
        core = MagicMock()
        core.create_namespaced_secret.side_effect = RuntimeError("boom")
        mock_clients.return_value = (MagicMock(), core, MagicMock())
        provider = _make_provider()
        from app.services.providers.kubevirt import _ensure_s3_secret

        with pytest.raises(RuntimeError, match="boom"):
            _ensure_s3_secret(provider, "ns-test", {})

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_includes_endpoint_url_when_present(self, mock_clients):
        core = MagicMock()
        mock_clients.return_value = (MagicMock(), core, MagicMock())
        provider = _make_provider()
        s3 = {
            "access_key_id": "A",
            "secret_access_key": "B",
            "endpoint_url": "https://minio.local",
        }
        from app.services.providers.kubevirt import _ensure_s3_secret

        _ensure_s3_secret(provider, "ns-test", s3)
        body = core.create_namespaced_secret.call_args.kwargs["body"]
        assert body.string_data["AWS_ENDPOINT_URL"] == "https://minio.local"


# ===========================================================================
# _try_existing_cluster_resource
# ===========================================================================


class TestTryExistingClusterResource:
    def test_existing_cluster_role_returns_true(self):
        rbac = MagicMock()
        result = _try_existing_cluster_resource("ClusterRole", "my-role", {}, rbac)
        assert result is True
        rbac.read_cluster_role.assert_called_once_with(name="my-role")

    def test_existing_cluster_role_binding_patches_and_returns_true(self):
        rbac = MagicMock()
        body = {"metadata": {"name": "my-binding"}}
        result = _try_existing_cluster_resource(
            "ClusterRoleBinding", "my-binding", body, rbac
        )
        assert result is True
        rbac.patch_cluster_role_binding.assert_called_once_with(
            name="my-binding", body=body
        )

    def test_not_found_returns_false(self):
        rbac = MagicMock()
        exc = _make_api_exception(404, "Not Found")
        rbac.read_cluster_role.side_effect = exc
        result = _try_existing_cluster_resource("ClusterRole", "missing", {}, rbac)
        assert result is False

    def test_other_api_error_raises(self):
        rbac = MagicMock()
        exc = _make_api_exception(403, "Forbidden")
        rbac.read_cluster_role.side_effect = exc
        with pytest.raises(Exception):
            _try_existing_cluster_resource("ClusterRole", "my-role", {}, rbac)


# ===========================================================================
# _handle_create_conflict
# ===========================================================================


class TestHandleCreateConflict:
    def test_deployment_gets_patched(self):
        apps = MagicMock()
        body = {"metadata": {"name": "my-deploy"}}
        _handle_create_conflict("Deployment", "my-deploy", "ns", body, apps)
        apps.patch_namespaced_deployment.assert_called_once_with(
            name="my-deploy", namespace="ns", body=body
        )

    def test_non_deployment_does_not_patch(self):
        apps = MagicMock()
        _handle_create_conflict("Service", "svc", "ns", {}, apps)
        apps.patch_namespaced_deployment.assert_not_called()


# ===========================================================================
# _apply_manifest
# ===========================================================================


class TestApplyManifest:
    def test_creates_namespace(self):
        core = MagicMock()
        rbac = MagicMock()
        apps = MagicMock()
        body = {"kind": "Namespace", "metadata": {"name": "my-ns"}}
        _apply_manifest("Namespace", "my-ns", None, body, core, rbac, apps)
        core.create_namespace.assert_called_once_with(body=body)

    def test_creates_service_account(self):
        core = MagicMock()
        rbac = MagicMock()
        apps = MagicMock()
        body = {"kind": "ServiceAccount", "metadata": {"name": "sa"}}
        _apply_manifest("ServiceAccount", "sa", "ns", body, core, rbac, apps)
        core.create_namespaced_service_account.assert_called_once_with(
            namespace="ns", body=body
        )

    def test_creates_deployment(self):
        core = MagicMock()
        rbac = MagicMock()
        apps = MagicMock()
        body = {"kind": "Deployment", "metadata": {"name": "dep"}}
        _apply_manifest("Deployment", "dep", "ns", body, core, rbac, apps)
        apps.create_namespaced_deployment.assert_called_once_with(
            namespace="ns", body=body
        )

    def test_deployment_conflict_patches(self):
        core = MagicMock()
        rbac = MagicMock()
        apps = MagicMock()
        exc = _make_api_exception(409)
        apps.create_namespaced_deployment.side_effect = exc
        body = {"kind": "Deployment", "metadata": {"name": "dep"}}
        _apply_manifest("Deployment", "dep", "ns", body, core, rbac, apps)
        apps.patch_namespaced_deployment.assert_called_once()

    def test_cluster_role_tries_existing_first(self):
        core = MagicMock()
        rbac = MagicMock()
        apps = MagicMock()
        # read_cluster_role succeeds -> _try_existing returns True -> skip create
        body = {"kind": "ClusterRole", "metadata": {"name": "cr"}}
        _apply_manifest("ClusterRole", "cr", None, body, core, rbac, apps)
        rbac.read_cluster_role.assert_called_once_with(name="cr")
        rbac.create_cluster_role.assert_not_called()

    def test_cluster_role_creates_if_not_found(self):
        core = MagicMock()
        rbac = MagicMock()
        apps = MagicMock()
        exc = _make_api_exception(404, "Not Found")
        rbac.read_cluster_role.side_effect = exc
        body = {"kind": "ClusterRole", "metadata": {"name": "cr"}}
        _apply_manifest("ClusterRole", "cr", None, body, core, rbac, apps)
        rbac.create_cluster_role.assert_called_once_with(body=body)

    def test_non_409_api_error_raises(self):
        core = MagicMock()
        rbac = MagicMock()
        apps = MagicMock()
        exc = _make_api_exception(403, "Forbidden")
        apps.create_namespaced_deployment.side_effect = exc
        with pytest.raises(Exception):
            _apply_manifest("Deployment", "dep", "ns", {}, core, rbac, apps)

    def test_cluster_role_binding_tries_existing_first(self):
        core = MagicMock()
        rbac = MagicMock()
        apps = MagicMock()
        body = {"kind": "ClusterRoleBinding", "metadata": {"name": "crb"}}
        _apply_manifest("ClusterRoleBinding", "crb", None, body, core, rbac, apps)
        rbac.patch_cluster_role_binding.assert_called_once()
        rbac.create_cluster_role_binding.assert_not_called()


# ===========================================================================
# _deploy_operator
# ===========================================================================


class TestDeployOperator:
    @patch("app.services.providers.kubevirt._apply_crds")
    @patch("app.services.providers.kubevirt._apply_manifest")
    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_deploys_all_manifests(self, mock_clients, mock_apply, mock_crds):
        mock_clients.return_value = (MagicMock(), MagicMock(), MagicMock())
        provider = _make_provider()

        manifest_bodies = {
            "namespace.yaml": {
                "kind": "Namespace",
                "metadata": {"name": "troshka-operator"},
            },
            "serviceaccount.yaml": {
                "kind": "ServiceAccount",
                "metadata": {"name": "sa", "namespace": "troshka-operator"},
            },
            "clusterrole.yaml": {"kind": "ClusterRole", "metadata": {"name": "cr"}},
            "clusterrolebinding.yaml": {
                "kind": "ClusterRoleBinding",
                "metadata": {"name": "crb", "namespace": "troshka-operator"},
                "subjects": [{"namespace": "old"}],
            },
            "deployment.yaml": {
                "kind": "Deployment",
                "metadata": {"name": "op", "namespace": "troshka-operator"},
            },
        }

        def mock_open_side_effect(path):
            filename = os.path.basename(path)
            m = MagicMock()
            import copy

            _body = copy.deepcopy(
                manifest_bodies.get(
                    filename, {"kind": "Unknown", "metadata": {"name": "x"}}
                )
            )
            m.__enter__ = lambda s: s
            m.__exit__ = MagicMock(return_value=False)
            m.read = lambda: ""
            # We need yaml.safe_load to return the body
            return m

        import yaml

        with patch("builtins.open", mock_open_side_effect):
            with patch.object(
                yaml,
                "safe_load",
                side_effect=lambda f: manifest_bodies.get(
                    os.path.basename(f.name) if hasattr(f, "name") else "",
                    {"kind": "Unknown", "metadata": {"name": "x"}},
                ),
            ):
                # This approach is fragile; instead, patch at file-read level
                pass

        # Simpler approach: patch yaml.safe_load + open together
        call_count = [0]
        manifest_order_bodies = list(manifest_bodies.values())

        def fake_safe_load(f):
            import copy

            body = copy.deepcopy(manifest_order_bodies[call_count[0]])
            call_count[0] += 1
            return body

        with patch("builtins.open", MagicMock()):
            with patch("yaml.safe_load", side_effect=fake_safe_load):
                from app.services.providers.kubevirt import _deploy_operator

                _deploy_operator(provider)

        assert mock_apply.call_count == 5
        mock_crds.assert_called_once()


# ===========================================================================
# _poll_and_cleanup_attachments
# ===========================================================================


class TestPollAndCleanupAttachments:
    @patch("app.services.providers.kubevirt.time")
    def test_matching_attachments_cleaned(self, mock_time):
        storage = MagicMock()
        va1 = MagicMock()
        va1.spec.source.persistent_volume_name = "pv-1"
        va1.status.attached = False
        va1.metadata.name = "va-1"

        va_list = MagicMock()
        # First call: has matching attachment, second call: empty
        va_list_empty = MagicMock()
        va_list_empty.items = []
        va_list.items = [va1]
        storage.list_volume_attachment.side_effect = [va_list, va_list_empty]

        _poll_and_cleanup_attachments(storage, {"pv-1"})
        storage.delete_volume_attachment.assert_called_once_with(name="va-1")

    @patch("app.services.providers.kubevirt.time")
    def test_no_matching_returns_early(self, mock_time):
        storage = MagicMock()
        va_list = MagicMock()
        va_list.items = []
        storage.list_volume_attachment.return_value = va_list

        _poll_and_cleanup_attachments(storage, {"pv-1"})
        storage.delete_volume_attachment.assert_not_called()
        mock_time.sleep.assert_not_called()


# ===========================================================================
# _cleanup_volume_attachments
# ===========================================================================


class TestCleanupVolumeAttachments:
    @patch("app.services.providers.kubevirt._poll_and_cleanup_attachments")
    @patch("app.services.providers.kubevirt._collect_pv_names")
    def test_pvs_found_polls_and_cleans(self, mock_collect, mock_poll):
        mock_collect.return_value = {"pv-1", "pv-2"}
        core = MagicMock()
        _cleanup_volume_attachments(core, "ns-test")
        mock_poll.assert_called_once()

    @patch("app.services.providers.kubevirt._poll_and_cleanup_attachments")
    @patch("app.services.providers.kubevirt._collect_pv_names")
    def test_no_pvs_skips_cleanup(self, mock_collect, mock_poll):
        mock_collect.return_value = set()
        core = MagicMock()
        _cleanup_volume_attachments(core, "ns-test")
        mock_poll.assert_not_called()

    def test_exception_swallowed(self):
        core = MagicMock()
        # Make the import inside the function fail by raising at pv_names step
        with patch(
            "app.services.providers.kubevirt._collect_pv_names",
            side_effect=RuntimeError("fail"),
        ):
            # Should not raise
            _cleanup_volume_attachments(core, "ns")


# ===========================================================================
# _query_cluster_capacity
# ===========================================================================


class TestQueryClusterCapacity:
    def test_sums_worker_node_resources(self):
        node1 = MagicMock()
        node1.metadata.labels = {"node-role.kubernetes.io/worker": ""}
        node1.spec.unschedulable = False
        node1.spec.taints = []
        node1.status.allocatable = {"cpu": "32", "memory": "131072Mi"}

        node2 = MagicMock()
        node2.metadata.labels = {"node-role.kubernetes.io/worker": ""}
        node2.spec.unschedulable = False
        node2.spec.taints = []
        node2.status.allocatable = {"cpu": "16", "memory": "64Gi"}

        core = MagicMock()
        nodes = MagicMock()
        nodes.items = [node1, node2]
        core.list_node.return_value = nodes

        vcpus, ram = _query_cluster_capacity(core)
        assert vcpus == 48
        assert ram == 131072 + 64 * 1024

    def test_skips_control_plane_nodes(self):
        cp_node = MagicMock()
        cp_node.metadata.labels = {"node-role.kubernetes.io/master": ""}
        cp_node.spec.unschedulable = False
        cp_node.spec.taints = []
        cp_node.status.allocatable = {"cpu": "8", "memory": "32768Mi"}

        core = MagicMock()
        nodes = MagicMock()
        nodes.items = [cp_node]
        core.list_node.return_value = nodes

        vcpus, ram = _query_cluster_capacity(core)
        assert vcpus == 0
        assert ram == 0

    def test_skips_unschedulable_nodes(self):
        node = MagicMock()
        node.metadata.labels = {"node-role.kubernetes.io/worker": ""}
        node.spec.unschedulable = True
        node.spec.taints = []
        node.status.allocatable = {"cpu": "32", "memory": "131072Mi"}

        core = MagicMock()
        nodes = MagicMock()
        nodes.items = [node]
        core.list_node.return_value = nodes

        vcpus, ram = _query_cluster_capacity(core)
        assert vcpus == 0

    def test_skips_nodes_with_noschedule_taint(self):
        taint = MagicMock()
        taint.effect = "NoSchedule"
        node = MagicMock()
        node.metadata.labels = {"node-role.kubernetes.io/worker": ""}
        node.spec.unschedulable = False
        node.spec.taints = [taint]
        node.status.allocatable = {"cpu": "32", "memory": "131072Mi"}

        core = MagicMock()
        nodes = MagicMock()
        nodes.items = [node]
        core.list_node.return_value = nodes

        vcpus, ram = _query_cluster_capacity(core)
        assert vcpus == 0

    def test_handles_ki_memory_suffix(self):
        node = MagicMock()
        node.metadata.labels = {"node-role.kubernetes.io/worker": ""}
        node.spec.unschedulable = False
        node.spec.taints = []
        node.status.allocatable = {"cpu": "4", "memory": "8388608Ki"}

        core = MagicMock()
        nodes = MagicMock()
        nodes.items = [node]
        core.list_node.return_value = nodes

        vcpus, ram = _query_cluster_capacity(core)
        assert vcpus == 4
        assert ram == 8388608 // 1024

    def test_exception_returns_fallback(self):
        core = MagicMock()
        core.list_node.side_effect = RuntimeError("API down")
        vcpus, ram = _query_cluster_capacity(core)
        assert vcpus == 256
        assert ram == 1024 * 1024


# ===========================================================================
# _query_ceph_storage_gb
# ===========================================================================


class TestQueryCephStorageGb:
    def test_parses_ceph_df_output(self):
        import json

        ceph_data = {"stats": {"total_bytes": 5 * 1024**3}}
        core = MagicMock()
        pods = MagicMock()
        pod = MagicMock()
        pod.metadata.name = "rook-ceph-tools-xyz"
        pods.items = [pod]
        core.list_namespaced_pod.return_value = pods

        mock_ws = MagicMock()
        mock_ws.is_open.side_effect = [True, False]
        mock_ws.peek_stdout.return_value = True
        mock_ws.read_stdout.return_value = json.dumps(ceph_data)
        mock_ws.peek_stderr.return_value = False

        # k8s_stream is imported locally: from kubernetes.stream import stream as k8s_stream
        with patch("kubernetes.stream.stream", return_value=mock_ws):
            result = _query_ceph_storage_gb(core)

        assert result == 5

    def test_no_toolbox_pod_returns_zero(self):
        core = MagicMock()
        pods = MagicMock()
        pods.items = []
        core.list_namespaced_pod.return_value = pods

        result = _query_ceph_storage_gb(core)
        assert result == 0

    def test_parse_error_returns_zero(self):
        core = MagicMock()
        pods = MagicMock()
        pod = MagicMock()
        pod.metadata.name = "rook-ceph-tools-xyz"
        pods.items = [pod]
        core.list_namespaced_pod.return_value = pods

        mock_ws = MagicMock()
        mock_ws.is_open.side_effect = [True, False]
        mock_ws.peek_stdout.return_value = True
        mock_ws.read_stdout.return_value = "not json"
        mock_ws.peek_stderr.return_value = False

        with patch("kubernetes.stream.stream", return_value=mock_ws):
            result = _query_ceph_storage_gb(core)

        assert result == 0


# ===========================================================================
# KubeVirtDriver.delete_console
# ===========================================================================


class TestDeleteConsole:
    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_deletes_vnc_services_and_routes(self, mock_clients):
        custom = MagicMock()
        core = MagicMock()
        mock_clients.return_value = (custom, core, MagicMock())

        svc1 = MagicMock()
        svc1.metadata.name = "vnc-svc-1"
        svc2 = MagicMock()
        svc2.metadata.name = "vnc-svc-2"
        svc_list = MagicMock()
        svc_list.items = [svc1, svc2]
        core.list_namespaced_service.return_value = svc_list

        custom.list_namespaced_custom_object.return_value = {
            "items": [
                {"metadata": {"name": "route-1"}},
                {"metadata": {"name": "route-2"}},
            ]
        }

        provider = _make_provider()
        driver = KubeVirtDriver()
        driver.delete_console(provider)

        assert core.delete_namespaced_service.call_count == 2
        assert custom.delete_namespaced_custom_object.call_count == 2

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_handles_exceptions_gracefully(self, mock_clients):
        custom = MagicMock()
        core = MagicMock()
        mock_clients.return_value = (custom, core, MagicMock())
        core.list_namespaced_service.side_effect = RuntimeError("fail")
        custom.list_namespaced_custom_object.side_effect = RuntimeError("fail")

        provider = _make_provider()
        driver = KubeVirtDriver()
        # Should not raise
        driver.delete_console(provider)


# ===========================================================================
# KubeVirtDriver.allocate_eip
# ===========================================================================


class TestAllocateEip:
    @patch("app.services.providers.kubevirt.time")
    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_creates_lb_and_returns_ip(self, mock_clients, mock_time):
        core = MagicMock()
        mock_clients.return_value = (MagicMock(), core, MagicMock())

        svc = MagicMock()
        ingress_entry = MagicMock()
        ingress_entry.ip = "10.0.0.1"
        svc.status.load_balancer.ingress = [ingress_entry]
        core.read_namespaced_service.return_value = svc

        provider = _make_provider()
        driver = KubeVirtDriver()
        result = driver.allocate_eip(
            provider, MagicMock(), "eip-id-1234", project_id="proj-1234"
        )

        core.create_namespaced_service.assert_called_once()
        assert result["public_ip"] == "10.0.0.1"
        assert result["allocation_id"] == "troshka-eip-eip-id-1"

    @patch("app.services.providers.kubevirt.time")
    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_timeout_raises(self, mock_clients, mock_time):
        core = MagicMock()
        mock_clients.return_value = (MagicMock(), core, MagicMock())

        svc = MagicMock()
        svc.status.load_balancer.ingress = None
        core.read_namespaced_service.return_value = svc

        provider = _make_provider()
        driver = KubeVirtDriver()
        with pytest.raises(TimeoutError, match="MetalLB did not assign IP"):
            driver.allocate_eip(
                provider, MagicMock(), "eip-id-1234", project_id="proj-1234"
            )


# ===========================================================================
# KubeVirtDriver.release_eip
# ===========================================================================


class TestReleaseEip:
    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_deletes_lb_service(self, mock_clients):
        core = MagicMock()
        mock_clients.return_value = (MagicMock(), core, MagicMock())

        provider = _make_provider()
        driver = KubeVirtDriver()
        driver.release_eip(provider, "troshka-eip-abc12345")

        core.delete_namespaced_service.assert_called_once_with(
            name="troshka-eip-abc12345", namespace="troshka"
        )

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_uses_explicit_namespace(self, mock_clients):
        core = MagicMock()
        mock_clients.return_value = (MagicMock(), core, MagicMock())

        provider = _make_provider()
        driver = KubeVirtDriver()
        driver.release_eip(provider, "svc-name", namespace="custom-ns")

        core.delete_namespaced_service.assert_called_once_with(
            name="svc-name", namespace="custom-ns"
        )


# ===========================================================================
# KubeVirtDriver.update_eip_ports
# ===========================================================================


class TestUpdateEipPorts:
    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_patches_service_ports(self, mock_clients):
        core = MagicMock()
        mock_clients.return_value = (MagicMock(), core, MagicMock())

        provider = _make_provider()
        driver = KubeVirtDriver()
        ports = [
            {"port": 443, "target_port": 8443, "protocol": "TCP", "name": "https"},
            {"port": 80},
        ]
        driver.update_eip_ports(provider, MagicMock(), "svc-name", ports)

        core.patch_namespaced_service.assert_called_once()
        call_kwargs = core.patch_namespaced_service.call_args.kwargs
        patched_ports = call_kwargs["body"]["spec"]["ports"]
        assert len(patched_ports) == 2
        assert patched_ports[0]["name"] == "https"
        assert patched_ports[0]["port"] == 443
        assert patched_ports[0]["targetPort"] == 8443
        assert patched_ports[1]["name"] == "port-80"
        assert patched_ports[1]["targetPort"] == 80


# ===========================================================================
# KubeVirtDriver.get_project_status
# ===========================================================================


class TestGetProjectStatus:
    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_returns_enriched_status(self, mock_clients):
        custom = MagicMock()
        mock_clients.return_value = (custom, MagicMock(), MagicMock())

        custom.get_namespaced_custom_object.return_value = {
            "status": {"state": "deployed", "vmStates": {"vm1": "running"}}
        }
        # list DVs: project namespace
        custom.list_namespaced_custom_object.side_effect = [
            {"items": []},  # project DVs
        ]

        provider = _make_provider()
        driver = KubeVirtDriver()
        result = driver.get_project_status(provider, "proj-1234-5678")

        assert result["state"] == "deployed"
        assert result["vmStates"] == {"vm1": "running"}
        assert "dataVolumes" in result

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_returns_empty_dict_on_error(self, mock_clients):
        custom = MagicMock()
        mock_clients.return_value = (custom, MagicMock(), MagicMock())
        custom.get_namespaced_custom_object.side_effect = RuntimeError("not found")

        provider = _make_provider()
        driver = KubeVirtDriver()
        result = driver.get_project_status(provider, "proj-1234-5678")
        assert result == {}

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_handles_non_dict_status(self, mock_clients):
        custom = MagicMock()
        mock_clients.return_value = (custom, MagicMock(), MagicMock())
        custom.get_namespaced_custom_object.return_value = {"status": "bad"}
        custom.list_namespaced_custom_object.side_effect = [{"items": []}]

        provider = _make_provider()
        driver = KubeVirtDriver()
        result = driver.get_project_status(provider, "proj-1234-5678")
        assert "dataVolumes" in result

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_includes_cache_dvs_for_golden_refs(self, mock_clients):
        custom = MagicMock()
        mock_clients.return_value = (custom, MagicMock(), MagicMock())

        custom.get_namespaced_custom_object.return_value = {
            "status": {"state": "deploying"}
        }

        project_dv = {
            "metadata": {"name": "clone-dv"},
            "spec": {
                "source": {"pvc": {"namespace": "troshka-cache", "name": "golden-pvc"}}
            },
        }
        cache_dv = {
            "metadata": {"name": "golden-pvc"},
            "spec": {"source": {"s3": {"url": "s3://bucket/key"}}},
        }

        custom.list_namespaced_custom_object.side_effect = [
            {"items": [project_dv]},  # project DVs
            {
                "items": [cache_dv, {"metadata": {"name": "other"}, "spec": {}}]
            },  # cache DVs
        ]

        provider = _make_provider()
        driver = KubeVirtDriver()
        result = driver.get_project_status(provider, "proj-1234-5678")
        # Should include project DV + matching cache DV (not the "other" one)
        assert len(result["dataVolumes"]) == 2


# ===========================================================================
# KubeVirtDriver.get_vm_states
# ===========================================================================


class TestGetVmStates:
    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_returns_vm_states(self, mock_clients):
        custom = MagicMock()
        mock_clients.return_value = (custom, MagicMock(), MagicMock())
        custom.get_namespaced_custom_object.return_value = {
            "status": {"vmStates": {"vm1": "running", "vm2": "stopped"}}
        }
        custom.list_namespaced_custom_object.side_effect = [{"items": []}]

        provider = _make_provider()
        driver = KubeVirtDriver()
        result = driver.get_vm_states(provider, "proj-1234-5678")
        assert result == {"vm1": "running", "vm2": "stopped"}

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_returns_empty_dict_on_error(self, mock_clients):
        custom = MagicMock()
        mock_clients.return_value = (custom, MagicMock(), MagicMock())
        custom.get_namespaced_custom_object.side_effect = RuntimeError("fail")

        provider = _make_provider()
        driver = KubeVirtDriver()
        result = driver.get_vm_states(provider, "proj-1234-5678")
        assert result == {}


# ===========================================================================
# _detect_vnc_state (pure function)
# ===========================================================================


class TestDetectVncState:
    def test_login_prompt(self):
        assert _detect_vnc_state("Welcome\nlogin:") == "login"

    def test_login_prompt_with_space(self):
        assert _detect_vnc_state("host login: ") == "login"

    def test_password_prompt(self):
        assert _detect_vnc_state("some text\nPassword:") == "password"

    def test_password_variant_spelling(self):
        assert _detect_vnc_state("some text\nPassvord:") == "password"

    def test_shell_dollar(self):
        assert _detect_vnc_state("user@host:~$ ") == "shell"

    def test_shell_hash(self):
        assert _detect_vnc_state("root@host:~# ") == "shell"

    def test_shell_bracket(self):
        assert _detect_vnc_state("[root@host ~]$ ") == "shell"

    def test_unknown_text(self):
        assert _detect_vnc_state("Starting systemd services...") == "unknown"

    def test_empty_text(self):
        assert _detect_vnc_state("") == "unknown"

    def test_short_text(self):
        assert _detect_vnc_state("ab") == "unknown"

    def test_tilde_detected_as_shell(self):
        assert _detect_vnc_state("user@host ~ ") == "shell"


# ===========================================================================
# _parse_vnc_markers (pure function)
# ===========================================================================


class TestParseVncMarkers:
    def test_markers_found(self):
        text = "TROSHKA_BEGIN\nhello world\nTROSHKA_EXIT 0"
        output, code = _parse_vnc_markers(text)
        assert output == "hello world"
        assert code == 0

    def test_nonzero_exit_code(self):
        text = "TROSHKA_BEGIN\nerror msg\nTROSHKA_EXIT 1"
        output, code = _parse_vnc_markers(text)
        assert "error msg" in output
        assert code == 1

    def test_no_markers(self):
        text = "just some text output"
        output, code = _parse_vnc_markers(text)
        assert output == "just some text output"
        assert code is None

    def test_missing_exit_marker(self):
        text = "TROSHKA_BEGIN\npartial output"
        output, code = _parse_vnc_markers(text)
        # No TROSHKA_EXIT match, so falls through to plain text path
        assert "partial output" in output
        assert code is None

    def test_multiline_output(self):
        text = "TROSHKA_BEGIN\nline1\nline2\nline3\nTROSHKA_EXIT 42"
        output, code = _parse_vnc_markers(text)
        assert "line1" in output
        assert "line2" in output
        assert "line3" in output
        assert code == 42


# ===========================================================================
# _parse_console_output (pure function)
# ===========================================================================


class TestParseConsoleOutput:
    def test_strips_ansi_codes(self):
        raw = "\x1b[32mhello\x1b[0m"
        body, code = _parse_console_output(raw)
        assert "hello" in body
        assert "\x1b" not in body

    def test_extracts_between_markers(self):
        raw = "TROSHKA_BEGIN\noutput line\nTROSHKA_END 0\n$"
        body, code = _parse_console_output(raw)
        assert body == "output line"
        assert code == 0

    def test_exit_code_parsed(self):
        raw = "TROSHKA_BEGIN\ndata\nTROSHKA_END 127\n"
        body, code = _parse_console_output(raw)
        assert code == 127

    def test_no_markers_returns_full_text(self):
        raw = "just some text"
        body, code = _parse_console_output(raw)
        assert body == "just some text"
        assert code is None

    def test_ansi_with_markers(self):
        raw = "\x1b[1mTROSHKA_BEGIN\x1b[0m\nresult\nTROSHKA_END 0\n"
        body, code = _parse_console_output(raw)
        assert body == "result"
        assert code == 0

    def test_missing_end_marker(self):
        raw = "TROSHKA_BEGIN\npartial"
        body, code = _parse_console_output(raw)
        assert "partial" in body
        assert code is None
