"""Tests for extracted helpers in workers/jobs.py."""

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# _reassociate_eips
# ---------------------------------------------------------------------------
class TestReassociateEips:
    @patch("app.services.eip_service.associate_eip")
    def test_no_eips_returns_topology_unchanged(self, mock_assoc):
        from app.workers.jobs import _reassociate_eips

        s = MagicMock()
        s.query.return_value.filter_by.return_value.all.return_value = []
        topology = {"externalIps": [], "nodes": []}
        result = _reassociate_eips(s, "proj-1", topology, MagicMock())
        assert result is topology
        mock_assoc.assert_not_called()
        # Should not commit when no EIPs
        s.execute.assert_not_called()

    @patch("app.services.eip_service.associate_eip")
    def test_reassociates_and_updates_topology(self, mock_assoc):
        from app.workers.jobs import _reassociate_eips

        eip = MagicMock()
        eip.canvas_eip_id = "eip-canvas-1"
        eip.private_ip = "10.0.0.5"
        eip.public_ip = "54.1.2.3"

        s = MagicMock()
        s.query.return_value.filter_by.return_value.all.return_value = [eip]

        topology = {
            "externalIps": [
                {"id": "eip-canvas-1", "ip": "old-ip", "_private_ip": "old-priv"},
                {"id": "eip-canvas-2", "ip": "other"},
            ],
            "nodes": [],
        }
        host = MagicMock()
        result = _reassociate_eips(s, "proj-1", topology, host)

        mock_assoc.assert_called_once_with(s, eip, host)
        # First EIP should be updated
        assert result["externalIps"][0]["ip"] == "54.1.2.3"
        assert result["externalIps"][0]["_private_ip"] == "10.0.0.5"
        # Second EIP should be unchanged
        assert result["externalIps"][1]["ip"] == "other"
        # Should persist to DB
        s.execute.assert_called_once()
        s.commit.assert_called_once()

    @patch("app.services.eip_service.associate_eip")
    def test_handles_eip_association_failure(self, mock_assoc):
        from app.workers.jobs import _reassociate_eips

        eip1 = MagicMock()
        eip1.canvas_eip_id = "eip-1"
        eip1.private_ip = "10.0.0.5"
        eip1.public_ip = "54.1.2.3"

        eip2 = MagicMock()
        eip2.canvas_eip_id = "eip-2"
        eip2.private_ip = "10.0.0.6"
        eip2.public_ip = "54.1.2.4"

        # First EIP succeeds, second fails
        mock_assoc.side_effect = [None, RuntimeError("AWS error")]

        s = MagicMock()
        s.query.return_value.filter_by.return_value.all.return_value = [eip1, eip2]

        topology = {
            "externalIps": [
                {"id": "eip-1", "ip": "old"},
                {"id": "eip-2", "ip": "old2"},
            ]
        }
        host = MagicMock()
        result = _reassociate_eips(s, "proj-1", topology, host)
        # First EIP updated, second left alone (error swallowed)
        assert result["externalIps"][0]["ip"] == "54.1.2.3"
        assert result["externalIps"][1]["ip"] == "old2"
        # Should still persist (both EIPs exist in query result)
        s.execute.assert_called_once()

    @patch("app.services.eip_service.associate_eip")
    def test_topology_without_external_ips_key(self, mock_assoc):
        from app.workers.jobs import _reassociate_eips

        eip = MagicMock()
        eip.canvas_eip_id = "eip-1"
        eip.private_ip = "10.0.0.5"
        eip.public_ip = "54.1.2.3"

        s = MagicMock()
        s.query.return_value.filter_by.return_value.all.return_value = [eip]

        topology = {"nodes": []}
        host = MagicMock()
        # Should not crash when externalIps key is missing
        _reassociate_eips(s, "proj-1", topology, host)
        mock_assoc.assert_called_once()
        s.execute.assert_called_once()


# ---------------------------------------------------------------------------
# job_start_infra_then_vm
# ---------------------------------------------------------------------------
class TestJobStartInfraThenVm:
    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="job-vm-start")
    @patch("app.services.deploy_service._get_network_lock")
    @patch("app.services.deploy_service._setup_networks_via_troshkad")
    @patch("app.services.deploy_service.cache_library_images")
    @patch("app.api.projects._domain_name", return_value="troshka-proj1234-vm123456")
    @patch("app.services.eip_service.associate_eip")
    @patch("app.core.database.SessionLocal")
    def test_happy_path(
        self,
        mock_session_cls,
        mock_assoc_eip,
        mock_domain,
        mock_cache,
        mock_nets,
        mock_lock,
        mock_start,
        mock_wait,
        mock_notify,
    ):
        from app.workers.jobs import job_start_infra_then_vm

        proj = MagicMock()
        proj.id = "proj-1234"
        proj.topology = {"nodes": [], "edges": []}
        proj.vni_map = {"net1": 100}
        proj.state = "stopped"

        host = MagicMock()
        host.id = "host-1"

        s = MagicMock()
        mock_session_cls.return_value = s
        s.query.return_value.filter_by.return_value.first.side_effect = [proj, host]
        # _reassociate_eips queries EIPs
        s.query.return_value.filter_by.return_value.all.return_value = []
        s.refresh = MagicMock()

        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        job_start_infra_then_vm("proj-1234", "host-1", "vm-target")

        mock_cache.assert_called_once()
        mock_nets.assert_called_once()
        mock_start.assert_called_once()
        assert proj.state == "active"
        s.commit.assert_called()

    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="job-1")
    @patch("app.services.deploy_service._get_network_lock")
    @patch("app.services.deploy_service._setup_networks_via_troshkad")
    @patch("app.services.deploy_service.cache_library_images")
    @patch("app.api.projects._domain_name", return_value="troshka-p-v")
    @patch("app.services.eip_service.associate_eip")
    @patch("app.core.database.SessionLocal")
    def test_skips_networks_when_no_vni_map(
        self,
        mock_session_cls,
        mock_assoc_eip,
        mock_domain,
        mock_cache,
        mock_nets,
        mock_lock,
        mock_start,
        mock_wait,
        mock_notify,
    ):
        from app.workers.jobs import job_start_infra_then_vm

        proj = MagicMock()
        proj.id = "proj-2"
        proj.topology = {"nodes": []}
        proj.vni_map = {}
        proj.state = "stopped"

        host = MagicMock()
        host.id = "host-2"

        s = MagicMock()
        mock_session_cls.return_value = s
        s.query.return_value.filter_by.return_value.first.side_effect = [proj, host]
        s.query.return_value.filter_by.return_value.all.return_value = []
        s.refresh = MagicMock()

        job_start_infra_then_vm("proj-2", "host-2", "vm-2")

        mock_nets.assert_not_called()
        mock_lock.assert_not_called()

    @patch("app.core.database.SessionLocal")
    def test_returns_early_when_project_not_found(self, mock_session_cls):
        from app.workers.jobs import job_start_infra_then_vm

        s = MagicMock()
        mock_session_cls.return_value = s
        s.query.return_value.filter_by.return_value.first.return_value = None

        # Should not raise
        job_start_infra_then_vm("missing", "host-1", "vm-1")
        s.close.assert_called_once()

    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.troshkad_client.start_job")
    @patch("app.services.deploy_service._get_network_lock")
    @patch("app.services.deploy_service._setup_networks_via_troshkad")
    @patch("app.services.deploy_service.cache_library_images")
    @patch("app.api.projects._domain_name", return_value="troshka-p-v")
    @patch("app.services.eip_service.associate_eip")
    @patch("app.core.database.SessionLocal")
    def test_handles_vm_start_troshkad_error(
        self,
        mock_session_cls,
        mock_assoc_eip,
        mock_domain,
        mock_cache,
        mock_nets,
        mock_lock,
        mock_start,
        mock_notify,
    ):
        from app.services.troshkad_client import TroshkadError
        from app.workers.jobs import job_start_infra_then_vm

        proj = MagicMock()
        proj.id = "proj-3"
        proj.topology = {"nodes": []}
        proj.vni_map = {}
        proj.state = "stopped"

        host = MagicMock()
        host.id = "host-3"

        s = MagicMock()
        mock_session_cls.return_value = s
        s.query.return_value.filter_by.return_value.first.side_effect = [proj, host]
        s.query.return_value.filter_by.return_value.all.return_value = []
        s.refresh = MagicMock()
        mock_start.side_effect = TroshkadError("agent down")

        # Should not raise -- the TroshkadError is caught
        job_start_infra_then_vm("proj-3", "host-3", "vm-3")
        # Project should still be set to active (infra started, VM error is non-fatal)
        assert proj.state == "active"

    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.eip_service.associate_eip")
    @patch("app.core.database.SessionLocal")
    def test_sets_error_on_unexpected_exception(
        self, mock_session_cls, mock_assoc_eip, mock_notify
    ):
        from app.workers.jobs import job_start_infra_then_vm

        proj = MagicMock()
        proj.id = "proj-4"
        proj.topology = {"nodes": []}
        proj.vni_map = {}
        proj.state = "stopped"

        host = MagicMock()
        host.id = "host-4"

        s = MagicMock()
        mock_session_cls.return_value = s
        # First query returns proj+host, then on exception recovery returns proj again
        s.query.return_value.filter_by.return_value.first.side_effect = [
            proj,
            host,
            proj,  # re-query in exception handler
        ]
        # _reassociate_eips queries EIPs -- make it blow up
        s.query.return_value.filter_by.return_value.all.side_effect = RuntimeError(
            "unexpected boom"
        )

        job_start_infra_then_vm("proj-4", "host-4", "vm-4")

        assert proj.state == "error"
        s.close.assert_called_once()
