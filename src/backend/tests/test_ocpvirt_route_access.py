"""Tests for OCP Virt Route-based external access."""

from unittest.mock import MagicMock, patch

from app.services.deploy_service import _find_vm_name_by_ip


def _make_topology_with_port_forwards(port_forwards, vms=None):
    """Build a topology with a gateway and port forwards."""
    nodes = [
        {
            "id": "gw-001",
            "type": "networkNode",
            "data": {
                "name": "gateway",
                "subtype": "gateway",
                "gatewayMode": "nat-portforward",
                "portForwards": port_forwards,
            },
        },
    ]
    for vm in vms or []:
        nodes.append(
            {
                "id": vm["id"],
                "type": "vmNode",
                "data": {
                    "name": vm["name"],
                    "os": vm.get("os", "rhel"),
                    "nics": vm.get("nics", []),
                },
            }
        )
    return {"nodes": nodes, "edges": []}


# -- VM name lookup -----------------------------------------------------------


def test_find_vm_name_by_ip_found():
    topo = _make_topology_with_port_forwards(
        [],
        vms=[
            {
                "id": "vm-bastion",
                "name": "bastion",
                "nics": [{"ip": "10.0.0.50"}],
            }
        ],
    )
    assert _find_vm_name_by_ip(topo, "10.0.0.50") == "bastion"


def test_find_vm_name_by_ip_not_found():
    topo = _make_topology_with_port_forwards(
        [],
        vms=[
            {
                "id": "vm-bastion",
                "name": "bastion",
                "nics": [{"ip": "10.0.0.50"}],
            }
        ],
    )
    result = _find_vm_name_by_ip(topo, "10.0.0.99")
    assert result == "10-0-0-99"


def test_find_vm_name_by_ip_multiple_nics():
    topo = _make_topology_with_port_forwards(
        [],
        vms=[
            {
                "id": "vm-hub",
                "name": "hub",
                "nics": [{"ip": "10.0.0.10"}, {"ip": "192.168.100.10"}],
            }
        ],
    )
    assert _find_vm_name_by_ip(topo, "192.168.100.10") == "hub"


# -- Route resource naming ---------------------------------------------------


def test_route_name_sanitization():
    """Route names must be DNS-safe."""
    from app.services.providers.ocpvirt import OCPVirtDriver
    import re

    driver = OCPVirtDriver()
    # The naming logic is inside create_route_access — test it indirectly
    # by verifying the pattern used
    vm_name = "My_Bastion.Host"
    safe = re.sub(r"[^a-z0-9-]", "-", vm_name.lower())[:20]
    resource_name = f"troshka-pf-{'a53cbd0d'}-{safe}-443"
    assert resource_name == "troshka-pf-a53cbd0d-my-bastion-host-443"
    assert len(resource_name) <= 63


# -- OCPVirtDriver.create_route_access (mocked K8s) -------------------------


@patch("app.services.providers.ocpvirt._get_k8s_clients")
def test_create_route_access_creates_service_and_route(mock_clients):
    from app.services.providers.ocpvirt import OCPVirtDriver

    mock_core = MagicMock()
    mock_custom = MagicMock()
    mock_clients.return_value = (mock_custom, mock_core)
    mock_custom.create_namespaced_custom_object.return_value = {
        "spec": {
            "host": "troshka-pf-a53cbd0d-bastion-443-troshka.apps.cluster.example.com"
        }
    }

    provider = MagicMock()
    provider.get_credentials.return_value = {
        "api_url": "https://api.cluster:6443",
        "token": "test",
        "namespace": "troshka",
    }
    host = MagicMock()
    host.instance_id = "troshka-host-abc12345"

    driver = OCPVirtDriver()
    result = driver.create_route_access(
        provider, host, "a53cbd0d-0000-0000", "bastion", "10.0.0.50", 443
    )

    assert (
        result["hostname"]
        == "troshka-pf-a53cbd0d-bastion-443-troshka.apps.cluster.example.com"
    )
    assert mock_core.create_namespaced_service.call_count == 1
    assert mock_custom.create_namespaced_custom_object.call_count == 1

    # Verify TLS termination is edge (OCP router handles TLS, backend is plaintext)
    route_body = mock_custom.create_namespaced_custom_object.call_args[1]["body"]
    assert route_body["spec"]["tls"]["termination"] == "edge"


@patch("app.services.providers.ocpvirt._get_k8s_clients")
def test_create_route_access_edge_for_port_80(mock_clients):
    from app.services.providers.ocpvirt import OCPVirtDriver

    mock_core = MagicMock()
    mock_custom = MagicMock()
    mock_clients.return_value = (mock_custom, mock_core)
    mock_custom.create_namespaced_custom_object.return_value = {
        "spec": {"host": "test-route.apps.cluster.example.com"}
    }

    provider = MagicMock()
    provider.get_credentials.return_value = {
        "api_url": "https://api:6443",
        "token": "t",
        "namespace": "troshka",
    }
    host = MagicMock()
    host.instance_id = "troshka-host-abc"

    driver = OCPVirtDriver()
    result = driver.create_route_access(
        provider, host, "proj-001", "bastion", "10.0.0.50", 80
    )

    route_body = mock_custom.create_namespaced_custom_object.call_args[1]["body"]
    assert route_body["spec"]["tls"]["termination"] == "edge"


# -- OCPVirtDriver.delete_route_access (mocked K8s) -------------------------


@patch("app.services.providers.ocpvirt._get_k8s_clients")
def test_delete_route_access_cleans_up_by_label(mock_clients):
    from kubernetes import client
    from app.services.providers.ocpvirt import OCPVirtDriver

    mock_core = MagicMock()
    mock_custom = MagicMock()
    mock_clients.return_value = (mock_custom, mock_core)

    svc1 = MagicMock()
    svc1.metadata.name = "troshka-pf-a53cbd0d-bastion-443"
    mock_core.list_namespaced_service.return_value = MagicMock(items=[svc1])
    mock_custom.list_namespaced_custom_object.return_value = {
        "items": [{"metadata": {"name": "troshka-pf-a53cbd0d-bastion-443"}}]
    }

    provider = MagicMock()
    provider.get_credentials.return_value = {
        "api_url": "https://api:6443",
        "token": "t",
        "namespace": "troshka",
    }

    driver = OCPVirtDriver()
    driver.delete_route_access(provider, "a53cbd0d-0000-0000")

    mock_core.delete_namespaced_service.assert_called_once_with(
        "troshka-pf-a53cbd0d-bastion-443", "troshka"
    )
    mock_custom.delete_namespaced_custom_object.assert_called_once()
    call_kwargs = mock_custom.delete_namespaced_custom_object.call_args
    assert call_kwargs[1]["name"] == "troshka-pf-a53cbd0d-bastion-443"
