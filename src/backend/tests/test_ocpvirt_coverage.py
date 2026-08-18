"""Additional coverage tests for OCP Virt provider functions.

Covers pure helpers and driver methods not tested in
test_ocpvirt_driver.py or test_ocpvirt_route_access.py.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.services.providers.ocpvirt import (
    OCPVirtDriver,
    _build_cloud_init_userdata,
    _build_root_source,
    _build_vm_disks_and_volumes,
    _cleanup_host_k8s_resources,
    _create_or_get_route,
    _setup_route_dnat,
)


def _make_provider():
    p = MagicMock()
    p.type = "ocpvirt"
    p.get_credentials.return_value = {
        "api_url": "https://api.ocpvdev01.example.com:6443",
        "token": "test-token",
        "namespace": "troshka",
        "verify_ssl": False,
    }
    return p


# ---------------------------------------------------------------------------
# _build_cloud_init_userdata
# ---------------------------------------------------------------------------


class TestBuildCloudInitUserdata:
    def test_standard_host_uses_main_template(self):
        result = _build_cloud_init_userdata("shared", "ssh-rsa AAAA", "host-123")
        assert "ssh-rsa AAAA" in result
        assert "host_id: host-123" in result
        # Standard template installs qemu-kvm, libvirt, etc.
        assert "qemu-kvm" in result
        # Should NOT contain pattern-buffer-only content (no qemu-img-only line)
        assert "qemu-img nfs-utils" not in result

    def test_pattern_buffer_uses_pattern_template(self):
        result = _build_cloud_init_userdata(
            "pattern_buffer", "ssh-rsa BBBB", "host-456"
        )
        assert "ssh-rsa BBBB" in result
        assert "host_id: host-456" in result
        # Pattern buffer template installs minimal packages
        assert "qemu-img nfs-utils" in result
        # Should NOT contain full host packages
        assert "libvirt-client" not in result

    def test_with_nfs_params(self):
        result = _build_cloud_init_userdata(
            "shared",
            "ssh-rsa KEY",
            "host-789",
            nfs_server="10.0.0.5",
            nfs_path="/vol1",
        )
        assert "/var/lib/troshka/shared" in result
        assert "10.0.0.5:/vol1" in result
        assert "nfsvers=4.1,nconnect=16" in result
        assert "virt_use_nfs" in result

    def test_with_nfs_port(self):
        result = _build_cloud_init_userdata(
            "shared",
            "ssh-rsa KEY",
            "host-789",
            nfs_server="10.0.0.5",
            nfs_path="/vol1",
            nfs_port=2049,
        )
        assert "port=2049," in result

    def test_without_nfs_no_mount(self):
        result = _build_cloud_init_userdata("shared", "ssh-rsa KEY", "host-000")
        assert "/var/lib/troshka/shared" not in result
        assert "virt_use_nfs" not in result

    def test_http_repo_when_configured(self):
        result = _build_cloud_init_userdata(
            "shared",
            "ssh-rsa KEY",
            "host-r",
            repo_url="https://repo.example/rhel-10.2",
            repo_user="u",
            repo_pass="p",
        )
        assert "path: /etc/yum.repos.d/troshka-rhel.repo" in result
        assert "baseurl=https://repo.example/rhel-10.2/BaseOS" in result
        assert "baseurl=https://repo.example/rhel-10.2/AppStream" in result
        assert "username=u" in result
        assert "password=p" in result
        assert "REPOEOF" not in result
        assert "/dev/sr0" not in result  # no DVD mount when repo is configured
        assert "qemu-kvm" in result  # still installs the virt stack

    def test_dvd_fallback_when_no_repo(self):
        result = _build_cloud_init_userdata("shared", "ssh-rsa KEY", "host-d")
        assert "/dev/sr0" in result  # falls back to DVD mount
        assert "file:///mnt/iso/BaseOS" in result

    def test_nfs_server_only_no_path(self):
        """NFS server without path should not add mount commands."""
        result = _build_cloud_init_userdata(
            "shared",
            "ssh-rsa KEY",
            "host-000",
            nfs_server="10.0.0.5",
            nfs_path=None,
        )
        assert "/var/lib/troshka/shared" not in result


# ---------------------------------------------------------------------------
# _build_root_source
# ---------------------------------------------------------------------------


class TestBuildRootSource:
    def test_with_url(self):
        result = _build_root_source("https://images.example.com/rhel9.qcow2", "rhel9")
        assert result == {
            "source": {"http": {"url": "https://images.example.com/rhel9.qcow2"}}
        }

    def test_without_url(self):
        result = _build_root_source("", "rhel9")
        assert result == {
            "sourceRef": {
                "kind": "DataSource",
                "name": "rhel9",
                "namespace": "openshift-virtualization-os-images",
            }
        }

    def test_without_url_none(self):
        result = _build_root_source(None, "centos-stream9")
        assert result["sourceRef"]["name"] == "centos-stream9"


# ---------------------------------------------------------------------------
# _build_vm_disks_and_volumes
# ---------------------------------------------------------------------------


class TestBuildVmDisksAndVolumes:
    def test_standard_host(self):
        root_source = {"source": {"blank": {}}}
        dvs, disks, volumes = _build_vm_disks_and_volumes(
            "troshka-host-abc", 500, "shared", None, root_source
        )
        # 2 data volumes: root + data
        assert len(dvs) == 2
        assert dvs[0]["metadata"]["name"] == "troshka-host-abc-root"
        assert dvs[1]["metadata"]["name"] == "troshka-host-abc-data"
        assert "500Gi" in dvs[1]["spec"]["storage"]["resources"]["requests"]["storage"]

        # 3 disks: root, data, cloudinit
        assert len(disks) == 3
        assert disks[0]["name"] == "rootdisk"
        assert disks[1]["name"] == "datadisk"
        assert disks[2]["name"] == "cloudinitdisk"

        # 3 volumes: root, data, cloudinit
        assert len(volumes) == 3

    def test_pattern_buffer_adds_scratch(self):
        root_source = {"source": {"blank": {}}}
        dvs, disks, volumes = _build_vm_disks_and_volumes(
            "troshka-host-xyz", 200, "pattern_buffer", None, root_source
        )
        # 3 data volumes: root + data + scratch
        assert len(dvs) == 3
        assert dvs[2]["metadata"]["name"] == "troshka-host-xyz-scratch"
        assert dvs[2]["spec"]["storage"]["resources"]["requests"]["storage"] == "500Gi"

        # 4 disks: root, data, cloudinit, scratch
        assert len(disks) == 4
        assert disks[3]["name"] == "scratch"

        # 4 volumes
        assert len(volumes) == 4

    def test_with_iso_pvc(self):
        root_source = {"source": {"blank": {}}}
        dvs, disks, volumes = _build_vm_disks_and_volumes(
            "troshka-host-iso", 500, "shared", "rhel-10-dvd-iso", root_source
        )
        # Still 2 data volumes (ISO is PVC reference, not a DV)
        assert len(dvs) == 2

        # 4 disks: root, data, installiso, cloudinit
        assert len(disks) == 4
        iso_disk = [d for d in disks if d.get("name") == "installiso"][0]
        assert "cdrom" in iso_disk
        assert iso_disk["cdrom"]["bus"] == "sata"

        # 4 volumes
        assert len(volumes) == 4
        iso_vol = [v for v in volumes if v.get("name") == "installiso"][0]
        assert iso_vol["persistentVolumeClaim"]["claimName"] == "rhel-10-dvd-iso"

    def test_pattern_buffer_with_iso(self):
        root_source = {"source": {"blank": {}}}
        dvs, disks, volumes = _build_vm_disks_and_volumes(
            "troshka-host-pb", 500, "pattern_buffer", "rhel-dvd", root_source
        )
        # 3 DVs (root, data, scratch), 5 disks (root, data, iso, cloudinit, scratch)
        assert len(dvs) == 3
        assert len(disks) == 5
        assert len(volumes) == 5

    def test_storage_size_formatting(self):
        root_source = {"source": {"blank": {}}}
        dvs, _, _ = _build_vm_disks_and_volumes(
            "host", 1000, "shared", None, root_source
        )
        assert dvs[1]["spec"]["storage"]["resources"]["requests"]["storage"] == "1000Gi"

    def test_root_source_passthrough(self):
        """Root source dict is passed through to first data volume."""
        root_source = {"source": {"http": {"url": "https://example.com/image.qcow2"}}}
        dvs, _, _ = _build_vm_disks_and_volumes(
            "host", 500, "shared", None, root_source
        )
        assert (
            dvs[0]["spec"]["source"]["http"]["url"] == "https://example.com/image.qcow2"
        )


# ---------------------------------------------------------------------------
# _wait_for_vmi_running
# ---------------------------------------------------------------------------


class TestWaitForVmiRunning:
    @patch("app.services.providers.ocpvirt.time")
    def test_vmi_reaches_running(self, mock_time):
        from app.services.providers.ocpvirt import _wait_for_vmi_running

        mock_custom = MagicMock()
        mock_custom.get_namespaced_custom_object.return_value = {
            "status": {
                "phase": "Running",
                "interfaces": [{"ipAddress": "10.128.2.50"}],
            }
        }
        result = _wait_for_vmi_running(mock_custom, "troshka", "host-abc")
        assert result == "10.128.2.50"

    @patch("app.services.providers.ocpvirt.time")
    def test_vmi_running_no_interfaces(self, mock_time):
        from app.services.providers.ocpvirt import _wait_for_vmi_running

        mock_custom = MagicMock()
        mock_custom.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Running", "interfaces": []}
        }
        result = _wait_for_vmi_running(mock_custom, "troshka", "host-abc")
        assert result is None

    @patch("app.services.providers.ocpvirt.time")
    def test_vmi_running_no_interfaces_key(self, mock_time):
        from app.services.providers.ocpvirt import _wait_for_vmi_running

        mock_custom = MagicMock()
        mock_custom.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Running"}
        }
        result = _wait_for_vmi_running(mock_custom, "troshka", "host-abc")
        assert result is None

    @patch("app.services.providers.ocpvirt.time")
    def test_vmi_timeout_raises(self, mock_time):
        from app.services.providers.ocpvirt import _wait_for_vmi_running

        mock_custom = MagicMock()
        mock_custom.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Pending"}
        }
        with pytest.raises(RuntimeError, match="did not reach Running"):
            _wait_for_vmi_running(mock_custom, "troshka", "host-abc")

    @patch("app.services.providers.ocpvirt.time")
    def test_vmi_api_exception_continues(self, mock_time):
        from kubernetes.client import ApiException

        from app.services.providers.ocpvirt import _wait_for_vmi_running

        mock_custom = MagicMock()
        # First call raises, second returns Running
        mock_custom.get_namespaced_custom_object.side_effect = [
            ApiException(status=500, reason="Internal"),
            {
                "status": {
                    "phase": "Running",
                    "interfaces": [{"ipAddress": "10.0.0.1"}],
                }
            },
        ] + [
            {"status": {"phase": "Pending"}}
        ] * 200  # pad to avoid IndexError
        result = _wait_for_vmi_running(mock_custom, "troshka", "host-abc")
        assert result == "10.0.0.1"


# ---------------------------------------------------------------------------
# _wait_for_lb_ip
# ---------------------------------------------------------------------------


class TestWaitForLbIp:
    @patch("app.services.providers.ocpvirt.time")
    def test_ip_assigned(self, mock_time):
        from app.services.providers.ocpvirt import _wait_for_lb_ip

        mock_core = MagicMock()
        lb_svc = MagicMock()
        ingress = MagicMock()
        ingress.ip = "192.168.1.100"
        lb_svc.status.load_balancer.ingress = [ingress]
        mock_core.read_namespaced_service.return_value = lb_svc

        result = _wait_for_lb_ip(mock_core, "troshka-lb-abc", "troshka")
        assert result == "192.168.1.100"

    @patch("app.services.providers.ocpvirt.time")
    def test_no_ingress_returns_none(self, mock_time):
        from app.services.providers.ocpvirt import _wait_for_lb_ip

        mock_core = MagicMock()
        lb_svc = MagicMock()
        lb_svc.status.load_balancer.ingress = None
        mock_core.read_namespaced_service.return_value = lb_svc

        result = _wait_for_lb_ip(mock_core, "troshka-lb-abc", "troshka")
        assert result is None

    @patch("app.services.providers.ocpvirt.time")
    def test_no_lb_status_returns_none(self, mock_time):
        from app.services.providers.ocpvirt import _wait_for_lb_ip

        mock_core = MagicMock()
        lb_svc = MagicMock()
        lb_svc.status.load_balancer = None
        mock_core.read_namespaced_service.return_value = lb_svc

        result = _wait_for_lb_ip(mock_core, "troshka-lb-abc", "troshka")
        assert result is None

    @patch("app.services.providers.ocpvirt.time")
    def test_ingress_no_ip_returns_none(self, mock_time):
        from app.services.providers.ocpvirt import _wait_for_lb_ip

        mock_core = MagicMock()
        lb_svc = MagicMock()
        ingress = MagicMock()
        ingress.ip = None
        lb_svc.status.load_balancer.ingress = [ingress]
        mock_core.read_namespaced_service.return_value = lb_svc

        result = _wait_for_lb_ip(mock_core, "troshka-lb-abc", "troshka")
        assert result is None


# ---------------------------------------------------------------------------
# _cleanup_host_k8s_resources
# ---------------------------------------------------------------------------


class TestCleanupHostK8sResources:
    def test_deletes_all_resources(self):
        mock_custom = MagicMock()
        mock_core = MagicMock()

        # EIP services listing
        eip_svc = MagicMock()
        eip_svc.metadata.name = "troshka-eip-12345678"
        mock_core.list_namespaced_service.return_value = MagicMock(items=[eip_svc])

        _cleanup_host_k8s_resources(
            mock_custom, mock_core, "troshka", "troshka-host-aaaaaaaa"
        )

        # Should delete lb and vncd services
        svc_delete_calls = mock_core.delete_namespaced_service.call_args_list
        deleted_names = [c[0][0] for c in svc_delete_calls]
        assert "troshka-lb-aaaaaaaa" in deleted_names
        assert "troshka-vncd-aaaaaaaa" in deleted_names
        assert "troshka-eip-12345678" in deleted_names

        # Should delete userdata secret
        mock_core.delete_namespaced_secret.assert_called_once_with(
            "troshka-host-aaaaaaaa-userdata", "troshka"
        )

        # Should delete console route
        mock_custom.delete_namespaced_custom_object.assert_called_once()
        route_call = mock_custom.delete_namespaced_custom_object.call_args
        assert route_call[1]["name"] == "troshka-console-aaaaaaaa"

    def test_all_exceptions_silently_caught(self):
        from kubernetes.client import ApiException

        mock_custom = MagicMock()
        mock_core = MagicMock()

        # All operations raise
        mock_core.delete_namespaced_service.side_effect = ApiException(
            status=500, reason="Server"
        )
        mock_core.list_namespaced_service.side_effect = ApiException(
            status=500, reason="Server"
        )
        mock_core.delete_namespaced_secret.side_effect = ApiException(
            status=404, reason="Not Found"
        )
        mock_custom.delete_namespaced_custom_object.side_effect = ApiException(
            status=404, reason="Not Found"
        )

        # Should not raise
        _cleanup_host_k8s_resources(
            mock_custom, mock_core, "troshka", "troshka-host-bbbbbbbb"
        )

    def test_eip_non_eip_services_skipped(self):
        """Only services starting with 'troshka-eip-' are deleted from the EIP list."""
        mock_custom = MagicMock()
        mock_core = MagicMock()

        other_svc = MagicMock()
        other_svc.metadata.name = "some-other-service"
        mock_core.list_namespaced_service.return_value = MagicMock(items=[other_svc])

        _cleanup_host_k8s_resources(
            mock_custom, mock_core, "troshka", "troshka-host-cccccccc"
        )

        # lb + vncd deletes, but NOT the non-eip service
        svc_delete_calls = mock_core.delete_namespaced_service.call_args_list
        deleted_names = [c[0][0] for c in svc_delete_calls]
        assert "some-other-service" not in deleted_names


# ---------------------------------------------------------------------------
# _setup_route_dnat
# ---------------------------------------------------------------------------


class TestSetupRouteDnat:
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_success(self, mock_start, mock_wait):
        mock_start.return_value = "job-123"
        host = MagicMock()

        _setup_route_dnat(host, "a53cbd0d-1111-2222", 30001, "10.0.0.50", 443)

        mock_start.assert_called_once_with(
            host,
            "/networks/add-dnat",
            {
                "namespace": "troshka-a53cbd0d",
                "transit_port": 30001,
                "dst_ip": "10.0.0.50",
                "dst_port": 443,
            },
        )
        mock_wait.assert_called_once_with(host, "job-123", timeout=30)

    @patch("app.services.troshkad_client.start_job")
    def test_exception_caught_and_logged(self, mock_start):
        mock_start.side_effect = ConnectionError("host unreachable")
        host = MagicMock()

        # Should not raise
        _setup_route_dnat(host, "deadbeef-1111-2222", 30002, "10.0.0.60", 80)


# ---------------------------------------------------------------------------
# _create_or_get_route
# ---------------------------------------------------------------------------


class TestCreateOrGetRoute:
    def test_new_route_created(self):
        mock_custom = MagicMock()
        mock_custom.create_namespaced_custom_object.return_value = {
            "spec": {"host": "my-route-troshka.apps.cluster.example.com"}
        }

        result = _create_or_get_route(
            mock_custom, "troshka", {"metadata": {"name": "my-route"}}, "my-route"
        )
        assert result == "my-route-troshka.apps.cluster.example.com"

    def test_route_already_exists_409(self):
        from kubernetes.client import ApiException

        mock_custom = MagicMock()
        mock_custom.create_namespaced_custom_object.side_effect = ApiException(
            status=409, reason="Conflict"
        )
        mock_custom.get_namespaced_custom_object.return_value = {
            "spec": {"host": "existing-route.apps.cluster.example.com"}
        }

        result = _create_or_get_route(
            mock_custom, "troshka", {"metadata": {"name": "my-route"}}, "my-route"
        )
        assert result == "existing-route.apps.cluster.example.com"

    def test_route_409_then_get_fails(self):
        from kubernetes.client import ApiException

        mock_custom = MagicMock()
        mock_custom.create_namespaced_custom_object.side_effect = ApiException(
            status=409, reason="Conflict"
        )
        mock_custom.get_namespaced_custom_object.side_effect = ApiException(
            status=500, reason="Server"
        )

        result = _create_or_get_route(
            mock_custom, "troshka", {"metadata": {"name": "my-route"}}, "my-route"
        )
        assert result == ""

    def test_other_error_propagates(self):
        from kubernetes.client import ApiException

        mock_custom = MagicMock()
        mock_custom.create_namespaced_custom_object.side_effect = ApiException(
            status=403, reason="Forbidden"
        )

        with pytest.raises(ApiException) as exc_info:
            _create_or_get_route(
                mock_custom,
                "troshka",
                {"metadata": {"name": "my-route"}},
                "my-route",
            )
        assert exc_info.value.status == 403


# ---------------------------------------------------------------------------
# OCPVirtDriver.setup_console
# ---------------------------------------------------------------------------


class TestSetupConsole:
    def test_returns_correct_structure(self):
        driver = OCPVirtDriver()
        provider = _make_provider()
        result = driver.setup_console(provider, "console.apps.cluster.example.com")

        assert result == {
            "console_base_domain": "console.apps.cluster.example.com",
            "console_zone_id": None,
            "console_nameservers": None,
        }


# ---------------------------------------------------------------------------
# OCPVirtDriver.create_console_record
# ---------------------------------------------------------------------------


class TestCreateConsoleRecord:
    @patch("app.services.providers.ocpvirt._get_k8s_clients")
    def test_creates_vncd_service_and_route(self, mock_clients):
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        mock_custom.create_namespaced_custom_object.return_value = {
            "spec": {
                "host": "troshka-console-abc12345-troshka.apps.cluster.example.com"
            }
        }

        driver = OCPVirtDriver()
        provider = _make_provider()
        host = MagicMock()
        host.instance_id = "troshka-host-abc12345"

        result = driver.create_console_record(
            provider, host, "console.example.com", "10.0.0.1"
        )

        assert result == "troshka-console-abc12345-troshka.apps.cluster.example.com"

        # Service created with port 8080
        mock_core.create_namespaced_service.assert_called_once()
        svc_body = mock_core.create_namespaced_service.call_args[1]["body"]
        assert svc_body.metadata.name == "troshka-vncd-abc12345"
        assert svc_body.spec.ports[0].port == 8080

        # Route created with edge TLS
        mock_custom.create_namespaced_custom_object.assert_called_once()
        route_body = mock_custom.create_namespaced_custom_object.call_args[1]["body"]
        assert route_body["spec"]["tls"]["termination"] == "edge"
        assert (
            route_body["metadata"]["annotations"]["haproxy.router.openshift.io/timeout"]
            == "3600s"
        )

    @patch("app.services.providers.ocpvirt._get_k8s_clients")
    def test_service_conflict_handled(self, mock_clients):
        from kubernetes.client import ApiException

        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        mock_core.create_namespaced_service.side_effect = ApiException(
            status=409, reason="Conflict"
        )
        mock_custom.create_namespaced_custom_object.return_value = {
            "spec": {"host": "route.apps.example.com"}
        }

        driver = OCPVirtDriver()
        provider = _make_provider()
        host = MagicMock()
        host.instance_id = "troshka-host-def45678"

        # Should not raise
        result = driver.create_console_record(
            provider, host, "console.example.com", "10.0.0.1"
        )
        assert result == "route.apps.example.com"

    @patch("app.services.providers.ocpvirt._get_k8s_clients")
    def test_route_conflict_returns_fallback(self, mock_clients):
        from kubernetes.client import ApiException

        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        mock_custom.create_namespaced_custom_object.side_effect = ApiException(
            status=409, reason="Conflict"
        )

        driver = OCPVirtDriver()
        provider = _make_provider()
        host = MagicMock()
        host.instance_id = "troshka-host-aaa11111"

        result = driver.create_console_record(
            provider, host, "apps.cluster.example.com", "10.0.0.1"
        )
        # Fallback format: {route_name}-{namespace}.{hostname}
        assert "troshka-console-aaa11111" in result
        assert "troshka" in result


# ---------------------------------------------------------------------------
# OCPVirtDriver.delete_console_record
# ---------------------------------------------------------------------------


class TestDeleteConsoleRecord:
    @patch("app.services.providers.ocpvirt._get_k8s_clients")
    def test_deletes_service_and_route(self, mock_clients):
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        driver = OCPVirtDriver()
        provider = _make_provider()
        host = MagicMock()
        host.instance_id = "troshka-host-abc12345"

        driver.delete_console_record(provider, host, "console.example.com", "10.0.0.1")

        mock_core.delete_namespaced_service.assert_called_once_with(
            "troshka-vncd-abc12345", "troshka"
        )
        mock_custom.delete_namespaced_custom_object.assert_called_once()
        route_call = mock_custom.delete_namespaced_custom_object.call_args
        assert route_call[1]["name"] == "troshka-console-abc12345"

    @patch("app.services.providers.ocpvirt._get_k8s_clients")
    def test_handles_404_gracefully(self, mock_clients):
        from kubernetes.client import ApiException

        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        mock_core.delete_namespaced_service.side_effect = ApiException(
            status=404, reason="Not Found"
        )
        mock_custom.delete_namespaced_custom_object.side_effect = ApiException(
            status=404, reason="Not Found"
        )

        driver = OCPVirtDriver()
        provider = _make_provider()
        host = MagicMock()
        host.instance_id = "troshka-host-xyz99999"

        # Should not raise
        driver.delete_console_record(provider, host, "console.example.com", "10.0.0.1")


# ---------------------------------------------------------------------------
# OCPVirtDriver.get_host_powerstate
# ---------------------------------------------------------------------------


class TestGetHostPowerstate:
    @patch("app.services.providers.ocpvirt._get_k8s_clients")
    def test_returns_state_from_status(self, mock_clients):
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        mock_custom.get_namespaced_custom_object.return_value = {
            "status": {
                "phase": "Running",
                "interfaces": [{"ipAddress": "10.0.0.1"}],
            }
        }

        driver = OCPVirtDriver()
        provider = _make_provider()
        result = driver.get_host_powerstate(provider, "troshka-host-test")
        assert result == "running"

    @patch("app.services.providers.ocpvirt._get_k8s_clients")
    def test_returns_unknown_when_status_none(self, mock_clients):
        from kubernetes.client import ApiException

        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        mock_custom.get_namespaced_custom_object.side_effect = ApiException(
            status=404, reason="Not Found"
        )

        driver = OCPVirtDriver()
        provider = _make_provider()
        result = driver.get_host_powerstate(provider, "troshka-host-gone")
        assert result == "unknown"


# ---------------------------------------------------------------------------
# OCPVirtDriver.detach_iso
# ---------------------------------------------------------------------------


class TestDetachIso:
    @patch("app.services.providers.ocpvirt._get_k8s_clients")
    def test_vm_has_iso_removes_it(self, mock_clients):
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        mock_custom.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "devices": {
                                "disks": [
                                    {"disk": {"bus": "virtio"}, "name": "rootdisk"},
                                    {
                                        "cdrom": {"bus": "sata", "readonly": True},
                                        "name": "installiso",
                                    },
                                    {
                                        "disk": {"bus": "virtio"},
                                        "name": "cloudinitdisk",
                                    },
                                ]
                            }
                        },
                        "volumes": [
                            {"dataVolume": {"name": "root"}, "name": "rootdisk"},
                            {
                                "persistentVolumeClaim": {"claimName": "rhel-dvd"},
                                "name": "installiso",
                            },
                            {"cloudInitNoCloud": {}, "name": "cloudinitdisk"},
                        ],
                    }
                }
            }
        }

        driver = OCPVirtDriver()
        provider = _make_provider()
        driver.detach_iso(provider, "troshka-host-abc")

        mock_custom.patch_namespaced_custom_object.assert_called_once()
        patch_body = mock_custom.patch_namespaced_custom_object.call_args[1]["body"]
        patched_disks = patch_body["spec"]["template"]["spec"]["domain"]["devices"][
            "disks"
        ]
        patched_volumes = patch_body["spec"]["template"]["spec"]["volumes"]

        # installiso removed
        assert all(d["name"] != "installiso" for d in patched_disks)
        assert all(v["name"] != "installiso" for v in patched_volumes)
        assert len(patched_disks) == 2
        assert len(patched_volumes) == 2

    @patch("app.services.providers.ocpvirt._get_k8s_clients")
    def test_vm_no_iso_no_patch(self, mock_clients):
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        mock_custom.get_namespaced_custom_object.return_value = {
            "spec": {
                "template": {
                    "spec": {
                        "domain": {
                            "devices": {
                                "disks": [
                                    {"disk": {"bus": "virtio"}, "name": "rootdisk"},
                                    {
                                        "disk": {"bus": "virtio"},
                                        "name": "cloudinitdisk",
                                    },
                                ]
                            }
                        },
                        "volumes": [
                            {"dataVolume": {"name": "root"}, "name": "rootdisk"},
                            {"cloudInitNoCloud": {}, "name": "cloudinitdisk"},
                        ],
                    }
                }
            }
        }

        driver = OCPVirtDriver()
        provider = _make_provider()
        driver.detach_iso(provider, "troshka-host-abc")

        # No patch because no installiso found
        mock_custom.patch_namespaced_custom_object.assert_not_called()


# ---------------------------------------------------------------------------
# OCPVirtDriver.extend_host_storage
# ---------------------------------------------------------------------------


class TestExtendHostStorage:
    @patch("app.services.providers.ocpvirt._get_k8s_clients")
    def test_patches_pvc_with_increased_size(self, mock_clients):
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        driver = OCPVirtDriver()
        provider = _make_provider()
        host = MagicMock()
        host.instance_id = "troshka-host-abc12345"
        host.storage_size_gb = 500
        host.auto_extend_increment_gb = 100
        host.auto_extend_max_gb = None
        db = MagicMock()

        result = driver.extend_host_storage(provider, host, db)

        mock_core.patch_namespaced_persistent_volume_claim.assert_called_once_with(
            "troshka-host-abc12345-root",
            "troshka",
            {"spec": {"resources": {"requests": {"storage": "600Gi"}}}},
        )
        assert result == {"old_size_gb": 500, "new_size_gb": 600}
        assert host.storage_size_gb == 600
        db.commit.assert_called_once()

    @patch("app.services.providers.ocpvirt._get_k8s_clients")
    def test_respects_max_gb_ceiling(self, mock_clients):
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        driver = OCPVirtDriver()
        provider = _make_provider()
        host = MagicMock()
        host.instance_id = "troshka-host-abc12345"
        host.storage_size_gb = 900
        host.auto_extend_increment_gb = 200
        host.auto_extend_max_gb = 1000
        db = MagicMock()

        result = driver.extend_host_storage(provider, host, db)

        # Capped at 1000 instead of 1100
        assert result == {"old_size_gb": 900, "new_size_gb": 1000}
        assert host.storage_size_gb == 1000

    @patch("app.services.providers.ocpvirt._get_k8s_clients")
    def test_already_at_max_raises(self, mock_clients):
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        driver = OCPVirtDriver()
        provider = _make_provider()
        host = MagicMock()
        host.instance_id = "troshka-host-abc12345"
        host.storage_size_gb = 1000
        host.auto_extend_increment_gb = 100
        host.auto_extend_max_gb = 1000
        db = MagicMock()

        with pytest.raises(ValueError, match="already at max"):
            driver.extend_host_storage(provider, host, db)

    @patch("app.services.providers.ocpvirt._get_k8s_clients")
    def test_explicit_increment_gb(self, mock_clients):
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        driver = OCPVirtDriver()
        provider = _make_provider()
        host = MagicMock()
        host.instance_id = "troshka-host-abc12345"
        host.storage_size_gb = 500
        host.auto_extend_increment_gb = 100
        host.auto_extend_max_gb = None
        db = MagicMock()

        result = driver.extend_host_storage(provider, host, db, increment_gb=50)

        assert result == {"old_size_gb": 500, "new_size_gb": 550}
