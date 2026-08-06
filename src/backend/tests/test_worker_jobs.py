"""Tests for app.workers.jobs — RQ worker job functions."""

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# _reassociate_eips
# ---------------------------------------------------------------------------


class TestReassociateEips:
    @patch("app.services.eip_service.associate_eip")
    def test_reassociates_allocated_eips(self, mock_assoc):
        from app.workers.jobs import _reassociate_eips

        eip = MagicMock()
        eip.canvas_eip_id = "eip-1"
        eip.private_ip = "10.0.0.5"
        eip.public_ip = "1.2.3.4"

        s = MagicMock()
        s.query.return_value.filter_by.return_value.all.return_value = [eip]

        topology = {
            "externalIps": [
                {"id": "eip-1", "ip": "old-ip"},
                {"id": "eip-2", "ip": "other"},
            ]
        }
        host = MagicMock()

        result = _reassociate_eips(s, "proj-1", topology, host)

        mock_assoc.assert_called_once_with(s, eip, host)
        assert result["externalIps"][0]["ip"] == "1.2.3.4"
        assert result["externalIps"][0]["_private_ip"] == "10.0.0.5"
        # Other EIPs unchanged
        assert result["externalIps"][1]["ip"] == "other"
        s.execute.assert_called_once()
        s.commit.assert_called_once()

    @patch("app.services.eip_service.associate_eip")
    def test_no_eips_no_update(self, mock_assoc):
        from app.workers.jobs import _reassociate_eips

        s = MagicMock()
        s.query.return_value.filter_by.return_value.all.return_value = []

        topology = {"externalIps": []}
        result = _reassociate_eips(s, "proj-1", topology, MagicMock())

        mock_assoc.assert_not_called()
        s.execute.assert_not_called()
        assert result == topology

    @patch("app.services.eip_service.associate_eip")
    def test_association_error_logged_but_continues(self, mock_assoc):
        from app.workers.jobs import _reassociate_eips

        mock_assoc.side_effect = RuntimeError("AWS API error")

        eip = MagicMock()
        eip.canvas_eip_id = "eip-1"
        eip.public_ip = "1.2.3.4"

        s = MagicMock()
        s.query.return_value.filter_by.return_value.all.return_value = [eip]

        topology = {"externalIps": [{"id": "eip-1"}]}
        # Should not raise
        _result = _reassociate_eips(s, "proj-1", topology, MagicMock())

        # Still writes topology update since project_eips is non-empty
        s.execute.assert_called_once()


# ---------------------------------------------------------------------------
# job_start_infra_then_vm
# ---------------------------------------------------------------------------


class TestJobStartInfraThenVm:
    @patch("app.services.ws_pubsub.notify_project")
    @patch(
        "app.services.troshkad_client.wait_for_job",
        return_value={"status": "completed"},
    )
    @patch("app.services.troshkad_client.start_job", return_value="job-1")
    @patch("app.services.deploy_service.cache_library_images")
    @patch("app.services.deploy_service._setup_networks_via_troshkad")
    @patch("app.services.deploy_service._get_network_lock")
    @patch("app.workers.jobs._reassociate_eips")
    @patch("app.api.projects._domain_name", return_value="troshka-proj1-vm1")
    @patch("app.core.database.SessionLocal")
    def test_happy_path(
        self,
        mock_session_cls,
        mock_domain,
        mock_reassoc,
        mock_net_lock,
        mock_setup_nets,
        mock_cache,
        mock_start,
        mock_wait,
        mock_notify,
    ):
        from app.workers.jobs import job_start_infra_then_vm

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        proj = MagicMock()
        proj.topology = {"nodes": [], "edges": []}
        proj.vni_map = {"net1": 100}
        host = MagicMock()
        host.id = "h1"

        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            proj,
            host,
        ]
        mock_reassoc.return_value = proj.topology

        job_start_infra_then_vm("proj-1", "h1", "vm-1")

        mock_start.assert_called_once()
        assert proj.state == "active"
        mock_db.commit.assert_called()
        mock_db.close.assert_called_once()

    @patch("app.core.database.SessionLocal")
    def test_missing_project_returns_early(self, mock_session_cls):
        from app.workers.jobs import job_start_infra_then_vm

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        job_start_infra_then_vm("proj-1", "h1", "vm-1")

        mock_db.close.assert_called_once()

    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.troshkad_client.start_job")
    @patch("app.services.deploy_service.cache_library_images")
    @patch("app.workers.jobs._reassociate_eips")
    @patch("app.api.projects._domain_name", return_value="troshka-p-v")
    @patch("app.core.database.SessionLocal")
    def test_troshkad_error_handled(
        self,
        mock_session_cls,
        mock_domain,
        mock_reassoc,
        mock_cache,
        mock_start,
        mock_notify,
    ):
        from app.services.troshkad_client import TroshkadError
        from app.workers.jobs import job_start_infra_then_vm

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        proj = MagicMock()
        proj.topology = {"nodes": []}
        proj.vni_map = {}
        host = MagicMock()
        host.id = "h1"

        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            proj,
            host,
        ]
        mock_reassoc.return_value = proj.topology
        mock_start.side_effect = TroshkadError("agent down")

        job_start_infra_then_vm("proj-1", "h1", "vm-1")

        # Should still set state to active despite TroshkadError on VM start
        assert proj.state == "active"
        mock_db.close.assert_called_once()

    @patch("app.workers.jobs._reassociate_eips")
    @patch("app.core.database.SessionLocal")
    def test_exception_sets_error_state(self, mock_session_cls, mock_reassoc):
        from app.workers.jobs import job_start_infra_then_vm

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        proj = MagicMock()
        proj.topology = {"nodes": []}
        proj.vni_map = {}
        host = MagicMock()
        host.id = "h1"

        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            proj,
            host,
            proj,  # for the except block re-query
        ]
        mock_reassoc.side_effect = RuntimeError("unexpected error")

        job_start_infra_then_vm("proj-1", "h1", "vm-1")

        assert proj.state == "error"
        mock_db.close.assert_called_once()


# ---------------------------------------------------------------------------
# job_cache_and_start_vm
# ---------------------------------------------------------------------------


class TestJobCacheAndStartVm:
    @patch("app.services.ws_pubsub.notify_project")
    @patch(
        "app.services.troshkad_client.wait_for_job",
        return_value={"status": "completed"},
    )
    @patch("app.services.troshkad_client.start_job", return_value="job-1")
    @patch("app.services.deploy_service.cache_library_images")
    @patch("app.api.projects._domain_name", return_value="troshka-p-v")
    @patch("app.core.database.SessionLocal")
    def test_happy_path(
        self,
        mock_session_cls,
        mock_domain,
        mock_cache,
        mock_start,
        mock_wait,
        mock_notify,
    ):
        from app.workers.jobs import job_cache_and_start_vm

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        proj = MagicMock()
        proj.deployed_topology = {"nodes": []}
        proj.topology = None
        host = MagicMock()

        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            proj,
            host,
        ]

        job_cache_and_start_vm("proj-1", "h1", "vm-1")

        mock_cache.assert_called_once()
        mock_start.assert_called_once()
        mock_notify.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("app.services.troshkad_client.start_job")
    @patch("app.services.deploy_service.cache_library_images")
    @patch("app.api.projects._domain_name", return_value="troshka-p-v")
    @patch("app.core.database.SessionLocal")
    def test_troshkad_error_logged(
        self, mock_session_cls, mock_domain, mock_cache, mock_start
    ):
        from app.services.troshkad_client import TroshkadError
        from app.workers.jobs import job_cache_and_start_vm

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        proj = MagicMock()
        proj.deployed_topology = None
        proj.topology = {"nodes": []}
        host = MagicMock()

        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            proj,
            host,
        ]
        mock_start.side_effect = TroshkadError("agent unreachable")

        # Should not raise
        job_cache_and_start_vm("proj-1", "h1", "vm-1")

        mock_db.close.assert_called_once()


# ---------------------------------------------------------------------------
# job_redeploy_bg
# ---------------------------------------------------------------------------


class TestJobRedeployBg:
    @patch("app.services.deploy_service.deploy_project_async")
    @patch("app.services.deploy_service._clear_deploy_cancelled")
    @patch("app.services.placement.place_project")
    @patch("app.core.database.SessionLocal")
    def test_no_destroy_ctx(
        self, mock_session_cls, mock_place, mock_clear, mock_deploy
    ):
        from app.workers.jobs import job_redeploy_bg

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        proj = MagicMock()
        mock_db.get.return_value = proj
        mock_place.return_value = {"host_id": "h1", "vni_map": {"n1": 100}}

        job_redeploy_bg("proj-1", None, "h1")

        mock_place.assert_called_once()
        mock_clear.assert_called_once_with("proj-1")
        mock_deploy.assert_called_once_with("proj-1")
        mock_db.close.assert_called_once()

    @patch("app.services.deploy_service.deploy_project_async")
    @patch("app.services.deploy_service._clear_deploy_cancelled")
    @patch("app.services.placement.place_project")
    @patch("app.services.gc_service.sync_host_capacity")
    @patch("app.services.deploy_service.destroy_project_sync")
    @patch("app.core.database.SessionLocal")
    def test_with_destroy_ctx(
        self,
        mock_session_cls,
        mock_destroy,
        mock_sync,
        mock_place,
        mock_clear,
        mock_deploy,
    ):
        from app.workers.jobs import job_redeploy_bg

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        proj = MagicMock()
        host = MagicMock()
        mock_db.get.return_value = proj
        mock_db.query.return_value.filter_by.return_value.first.return_value = host
        mock_place.return_value = {"host_id": "h1", "vni_map": {"n1": 100}}

        destroy_ctx = {"host_id": "h-old", "project_id": "proj-1"}

        job_redeploy_bg("proj-1", destroy_ctx, "h-old")

        mock_destroy.assert_called_once_with(destroy_ctx, delete_record=False)
        mock_sync.assert_called_once()
        assert proj.host_id is None  # cleared after destroy
        mock_place.assert_called_once()
        mock_deploy.assert_called_once()

    @patch("app.services.placement.place_project")
    @patch("app.core.database.SessionLocal")
    def test_placement_error(self, mock_session_cls, mock_place):
        from app.workers.jobs import job_redeploy_bg

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        proj = MagicMock()
        mock_db.get.return_value = proj
        mock_place.return_value = {"error": "No hosts available"}

        job_redeploy_bg("proj-1", None, None)

        assert proj.state == "error"
        assert proj.deploy_error == "No hosts available"
        mock_db.commit.assert_called()
        mock_db.close.assert_called_once()

    @patch("app.core.database.SessionLocal")
    def test_missing_project_returns_early(self, mock_session_cls):
        from app.workers.jobs import job_redeploy_bg

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        mock_db.get.return_value = None

        job_redeploy_bg("proj-1", None, None)

        mock_db.close.assert_called_once()


# ---------------------------------------------------------------------------
# job_bulk_deploy_projects
# ---------------------------------------------------------------------------


class TestJobBulkDeployProjects:
    @patch("app.api.patterns._bulk_deploy_projects")
    def test_delegates_to_bulk_deploy(self, mock_bulk):
        from app.workers.jobs import job_bulk_deploy_projects

        ids = ["p1", "p2", "p3"]
        job_bulk_deploy_projects(ids)

        mock_bulk.assert_called_once_with(ids)


# ---------------------------------------------------------------------------
# job_clean_pattern_cache
# ---------------------------------------------------------------------------


class TestJobCleanPatternCache:
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="job-1")
    @patch("app.core.database.SessionLocal")
    def test_cleans_cache_on_connected_hosts(
        self, mock_session_cls, mock_start, mock_wait
    ):
        from app.workers.jobs import job_clean_pattern_cache

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        h1 = MagicMock()
        h2 = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [h1, h2]

        job_clean_pattern_cache("pat-1")

        assert mock_start.call_count == 2
        assert mock_wait.call_count == 2
        mock_db.close.assert_called_once()

    @patch("app.services.troshkad_client.start_job")
    @patch("app.core.database.SessionLocal")
    def test_handles_host_errors_gracefully(self, mock_session_cls, mock_start):
        from app.workers.jobs import job_clean_pattern_cache

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        h1 = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [h1]
        mock_start.side_effect = RuntimeError("host unreachable")

        # Should not raise
        job_clean_pattern_cache("pat-1")

        mock_db.close.assert_called_once()

    @patch("app.core.database.SessionLocal")
    def test_no_connected_hosts(self, mock_session_cls):
        from app.workers.jobs import job_clean_pattern_cache

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        mock_db.query.return_value.filter.return_value.all.return_value = []

        job_clean_pattern_cache("pat-1")

        mock_db.close.assert_called_once()


# ---------------------------------------------------------------------------
# job_provision_ocpvirt_host
# ---------------------------------------------------------------------------


class TestJobProvisionOcpvirtHost:
    @patch("app.services.providers.get_provider_driver")
    @patch("app.core.database.SessionLocal")
    def test_successful_provision(self, mock_session_cls, mock_get_drv):
        from app.workers.jobs import job_provision_ocpvirt_host

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        host = MagicMock()
        prov = MagicMock()
        prov.get_credentials.return_value = {"token": "tok-123"}

        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            host,
            prov,
        ]

        drv = MagicMock()
        drv.provision_host.return_value = {
            "instance_id": "i-123",
            "instance_type": "kubevirt-cluster",
            "total_vcpus": 64,
            "total_ram_mb": 256000,
            "public_ip": "10.0.0.1",
            "private_ip": "192.168.0.1",
        }
        mock_get_drv.return_value = drv

        job_provision_ocpvirt_host("prov-1", "h1")

        assert host.instance_id == "i-123"
        assert host.state == "active"
        assert host.agent_status == "connected"
        assert host.total_vcpus == 64
        assert host.agent_token == "tok-123"
        mock_db.commit.assert_called()
        mock_db.close.assert_called_once()

    @patch("app.core.database.SessionLocal")
    def test_missing_host_returns_early(self, mock_session_cls):
        from app.workers.jobs import job_provision_ocpvirt_host

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        job_provision_ocpvirt_host("prov-1", "h1")

        mock_db.close.assert_called_once()

    @patch("app.services.providers.get_provider_driver")
    @patch("app.core.database.SessionLocal")
    def test_provision_error_sets_error_state(self, mock_session_cls, mock_get_drv):
        from app.workers.jobs import job_provision_ocpvirt_host

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        host = MagicMock()
        prov = MagicMock()

        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            host,
            prov,
            host,  # for the except block re-query
        ]

        drv = MagicMock()
        drv.provision_host.side_effect = RuntimeError("API failure")
        mock_get_drv.return_value = drv

        job_provision_ocpvirt_host("prov-1", "h1")

        assert host.state == "error"
        assert host.agent_status == "provision_failed"
        mock_db.close.assert_called_once()


# ---------------------------------------------------------------------------
# job_provision_kubevirt
# ---------------------------------------------------------------------------


class TestJobProvisionKubevirt:
    @patch("app.services.providers.get_provider_driver")
    @patch("app.core.database.SessionLocal")
    def test_successful_provision(self, mock_session_cls, mock_get_drv):
        from app.workers.jobs import job_provision_kubevirt

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        prov = MagicMock()
        host = MagicMock()
        host.id = "h1"
        host.instance_id = "old-id"
        host.total_vcpus = 0
        host.total_ram_mb = 0
        host.ip_address = "old-ip"

        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            prov,
            host,
        ]

        drv = MagicMock()
        drv.provision_host.return_value = {
            "instance_id": "new-id",
            "instance_type": "kubevirt-cluster",
            "total_vcpus": 128,
            "total_ram_mb": 512000,
            "public_ip": "10.0.0.2",
        }
        mock_get_drv.return_value = drv

        job_provision_kubevirt("prov-1")

        assert host.state == "active"
        assert host.agent_status == "connected"
        assert host.instance_id == "new-id"
        assert host.total_vcpus == 128
        mock_db.commit.assert_called()
        mock_db.close.assert_called_once()

    @patch("app.core.database.SessionLocal")
    def test_missing_provider_returns_early(self, mock_session_cls):
        from app.workers.jobs import job_provision_kubevirt

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        job_provision_kubevirt("prov-1")

        mock_db.close.assert_called_once()

    @patch("app.core.database.SessionLocal")
    def test_missing_host_returns_early(self, mock_session_cls):
        from app.workers.jobs import job_provision_kubevirt

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        prov = MagicMock()
        # First query returns prov, second returns None (no host)
        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            prov,
            None,
        ]

        job_provision_kubevirt("prov-1")

        mock_db.close.assert_called_once()

    @patch("app.services.providers.get_provider_driver")
    @patch("app.core.database.SessionLocal")
    def test_provision_error_sets_error_state(self, mock_session_cls, mock_get_drv):
        from app.workers.jobs import job_provision_kubevirt

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        prov = MagicMock()
        host = MagicMock()
        host.id = "h1"

        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            prov,
            host,
            host,  # for the except block re-query
        ]

        drv = MagicMock()
        drv.provision_host.side_effect = RuntimeError("cluster unreachable")
        mock_get_drv.return_value = drv

        job_provision_kubevirt("prov-1")

        assert host.state == "error"
        assert host.agent_status == "provision_failed"
        mock_db.close.assert_called_once()
