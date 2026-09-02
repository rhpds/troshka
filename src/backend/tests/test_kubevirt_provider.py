from unittest.mock import MagicMock, patch

from app.services.providers import get_provider_driver


def _make_provider(provider_type="kubevirt"):
    p = MagicMock()
    p.type = provider_type
    p.get_credentials.return_value = {
        "api_url": "https://api.cluster.example.com:6443",
        "token": "test-token",
        "namespace": "troshka",
        "verify_ssl": False,
    }
    return p


def test_get_provider_driver_returns_kubevirt():
    provider = _make_provider()
    driver = get_provider_driver(provider)
    from app.services.providers.kubevirt import KubeVirtDriver

    assert isinstance(driver, KubeVirtDriver)


def test_provision_host_returns_cluster_info():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    mock_node = MagicMock()
    mock_node.metadata.labels = {"node-role.kubernetes.io/worker": ""}
    mock_node.spec.unschedulable = False
    mock_node.spec.taints = []
    mock_node.status.allocatable = {"cpu": "64", "memory": "262144Mi"}
    mock_nodes = MagicMock()
    mock_nodes.items = [mock_node]

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_core.list_node.return_value = mock_nodes
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        result = driver.provision_host(
            provider, "test-host-id", "kubevirt-cluster", 1000
        )

    assert result["host_id"] == "test-host-id"
    assert result["instance_type"] == "kubevirt-cluster"
    assert result["total_vcpus"] == 64
    assert result["total_ram_mb"] == 262144


def test_get_host_status_returns_running():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        result = driver.get_host_status(
            provider, "https://api.cluster.example.com:6443"
        )

    assert result is not None
    assert result["state"] == "running"


def test_get_host_powerstate_always_running():
    provider = _make_provider()
    driver = get_provider_driver(provider)
    assert driver.get_host_powerstate(provider, "any") == "running"


def test_deploy_project_creates_namespace_and_cr():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        result = driver.deploy_project(
            provider,
            "12345678-1234-1234-1234-123456789abc",
            {"nodes": [], "edges": []},
            {"bucket": "test", "endpoint": "s3.amazonaws.com", "region": "us-east-1"},
        )

    assert result == "project-12345678"
    project_ns_call = mock_core.create_namespace.call_args_list[0]
    ns_body = project_ns_call.kwargs["body"]
    assert ns_body.metadata.name == "troshka-12345678"
    assert ns_body.metadata.labels["mutatevirtualmachines.kubemacpool.io"] == "ignore"
    mock_custom.create_namespaced_custom_object.assert_called_once()
    call_args = mock_custom.create_namespaced_custom_object.call_args
    assert call_args.kwargs["namespace"] == "troshka-12345678"
    assert call_args.kwargs["body"]["spec"]["action"] == "deploy"


def test_destroy_project_deletes_cr_and_namespace():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with (
        patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients,
        patch("app.services.providers.kubevirt.time.sleep"),
    ):
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_custom.list_namespaced_custom_object.return_value = {"items": []}
        empty_list = MagicMock()
        empty_list.items = []
        mock_core.list_namespaced_pod.return_value = empty_list
        mock_core.list_namespaced_persistent_volume_claim.return_value = empty_list
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        driver.destroy_project(provider, "12345678-1234-1234-1234-123456789abc")

    mock_custom.delete_namespaced_custom_object.assert_called_once()
    mock_core.delete_namespace.assert_called_once_with(name="troshka-12345678")


def test_setup_console_returns_config():
    provider = _make_provider()
    driver = get_provider_driver(provider)
    result = driver.setup_console(provider, "console.example.com")
    assert result["console_base_domain"] == "console.example.com"


def test_create_console_record_creates_service_and_route():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        host = MagicMock()
        driver.create_console_record(
            provider, host, "vm1.console.example.com", "10.0.0.1"
        )

    mock_core.create_namespaced_service.assert_called_once()
    mock_custom.create_namespaced_custom_object.assert_called_once()
    route_call = mock_custom.create_namespaced_custom_object.call_args
    assert route_call.kwargs["body"]["spec"]["host"] == "vm1.console.example.com"


def test_delete_console_record_cleans_up():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        host = MagicMock()
        driver.delete_console_record(
            provider, host, "vm1.console.example.com", "10.0.0.1"
        )

    mock_core.delete_namespaced_service.assert_called_once()
    mock_custom.delete_namespaced_custom_object.assert_called_once()


def test_create_route_access_creates_service_and_route():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_custom.create_namespaced_custom_object.return_value = {
            "spec": {"host": "bastion-443.apps.cluster.example.com"}
        }
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        host = MagicMock()
        result = driver.create_route_access(
            provider, host, "proj-1234-5678", "bastion", "10.0.0.10", 443
        )

    assert result["hostname"] == "bastion-443.apps.cluster.example.com"
    mock_core.create_namespaced_service.assert_called_once()


def test_create_route_access_edge_for_port_443():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_custom.create_namespaced_custom_object.return_value = {
            "spec": {"host": "showroom-443.apps.cluster.example.com"}
        }
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        host = MagicMock()
        result = driver.create_route_access(
            provider, host, "proj-1234-5678", "showroom", "10.0.0.5", 443
        )

    assert result["hostname"] == "showroom-443.apps.cluster.example.com"
    route_body = mock_custom.create_namespaced_custom_object.call_args[1]["body"]
    assert route_body["spec"]["tls"]["termination"] == "edge"
    assert route_body["spec"]["port"]["targetPort"] == 1443
    svc_body = mock_core.create_namespaced_service.call_args[1]["body"]
    assert svc_body["spec"]["ports"][0]["port"] == 1443
    assert svc_body["spec"]["ports"][0]["targetPort"] == 1443


def test_create_route_access_edge_for_port_80():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_custom.create_namespaced_custom_object.return_value = {
            "spec": {"host": "showroom-80.apps.cluster.example.com"}
        }
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        host = MagicMock()
        result = driver.create_route_access(
            provider, host, "proj-1234-5678", "showroom", "10.0.0.5", 80
        )

    assert result["hostname"] == "showroom-80.apps.cluster.example.com"
    route_body = mock_custom.create_namespaced_custom_object.call_args[1]["body"]
    assert route_body["spec"]["tls"]["termination"] == "edge"
    assert route_body["spec"]["port"]["targetPort"] == 1080
    svc_body = mock_core.create_namespaced_service.call_args[1]["body"]
    assert svc_body["spec"]["ports"][0]["port"] == 1080
    assert svc_body["spec"]["ports"][0]["targetPort"] == 1080


def test_delete_route_access_cleans_up_by_label():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    mock_svc = MagicMock()
    mock_svc.metadata.name = "rt-bastion-443"
    mock_route = {"metadata": {"name": "rt-bastion-443"}}

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_core.list_namespaced_service.return_value.items = [mock_svc]
        mock_custom.list_namespaced_custom_object.return_value = {"items": [mock_route]}
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        driver.delete_route_access(provider, "proj-1234-5678")

    mock_core.delete_namespaced_service.assert_called_once()
    mock_custom.delete_namespaced_custom_object.assert_called_once()


def test_resize_and_extend_are_noops():
    provider = _make_provider()
    driver = get_provider_driver(provider)
    assert driver.resize_host(provider, "any", "any") == {}
    assert driver.extend_host_storage(provider, MagicMock(), MagicMock()) == {}


def test_start_stop_host_are_noops():
    provider = _make_provider()
    driver = get_provider_driver(provider)
    driver.start_host(provider, "any")
    driver.stop_host(provider, "any")


# ---------------------------------------------------------------------------
# Task 8b (Plan 4b): create_ops_pod live k8s create (mocked client)
# ---------------------------------------------------------------------------


def _ops_pod_manifests():
    return (
        {"kind": "Pod", "metadata": {"name": "troshka-abcdef12-ops"}},
        {"kind": "Secret", "metadata": {"name": "troshka-abcdef12-ops-config"}},
    )


def test_create_ops_pod_creates_secret_then_pod():
    from app.services.providers.kubevirt import create_ops_pod

    provider = _make_provider()
    pod, secret = _ops_pod_manifests()
    with (
        patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients,
        patch(
            "app.services.providers.kubevirt._project_ns",
            return_value="troshka-abcdef12",
        ),
    ):
        core = MagicMock()
        mock_clients.return_value = (MagicMock(), core, MagicMock())
        ns = create_ops_pod(provider, "abcdef12-0000", pod, secret)

    assert ns == "troshka-abcdef12"
    core.create_namespaced_secret.assert_called_once_with(
        namespace="troshka-abcdef12", body=secret
    )
    core.create_namespaced_pod.assert_called_once_with(
        namespace="troshka-abcdef12", body=pod
    )


def test_create_ops_pod_replaces_existing_pod():
    from app.services.providers.kubevirt import create_ops_pod

    provider = _make_provider()
    pod, secret = _ops_pod_manifests()
    with (
        patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients,
        patch(
            "app.services.providers.kubevirt._project_ns",
            return_value="troshka-abcdef12",
        ),
    ):
        core = MagicMock()
        # Secret + Pod already exist on the first create attempt.
        core.create_namespaced_secret.side_effect = Exception("AlreadyExists")
        core.create_namespaced_pod.side_effect = [Exception("AlreadyExists"), None]
        mock_clients.return_value = (MagicMock(), core, MagicMock())
        create_ops_pod(provider, "abcdef12-0000", pod, secret)

    core.replace_namespaced_secret.assert_called_once()
    # Delete with grace_period_seconds=0 so the terminating window is short.
    core.delete_namespaced_pod.assert_called_once_with(
        name="troshka-abcdef12-ops",
        namespace="troshka-abcdef12",
        grace_period_seconds=0,
    )
    assert core.create_namespaced_pod.call_count == 2


def test_create_ops_pod_retries_recreate_through_terminating_window():
    # After delete, the old pod is still Terminating so the recreate hits
    # AlreadyExists once more; _apply_ops_pod must retry (not raise) and succeed.
    from app.services.providers.kubevirt import create_ops_pod

    provider = _make_provider()
    pod, secret = _ops_pod_manifests()
    with (
        patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients,
        patch(
            "app.services.providers.kubevirt._project_ns",
            return_value="troshka-abcdef12",
        ),
        patch("app.services.providers.kubevirt.time.sleep"),
    ):
        core = MagicMock()
        # initial create → AlreadyExists; recreate → AlreadyExists once, then OK.
        core.create_namespaced_pod.side_effect = [
            Exception("AlreadyExists"),
            Exception("AlreadyExists"),
            None,
        ]
        mock_clients.return_value = (MagicMock(), core, MagicMock())
        # Must not raise.
        create_ops_pod(provider, "abcdef12-0000", pod, secret)

    core.delete_namespaced_pod.assert_called_once_with(
        name="troshka-abcdef12-ops",
        namespace="troshka-abcdef12",
        grace_period_seconds=0,
    )
    assert core.create_namespaced_pod.call_count == 3
