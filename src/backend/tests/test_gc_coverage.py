"""Tests for uncovered paths in gc_service.py.

Covers:
  - _recover_mesh_peers
  - _reconnect_project_taps
  - _restore_project_bmc (no-BMC skip)
  - recover_host_services (empty projects, busy projects, exception)
  - clean_s3_orphans
  - _clean_orphaned_routes
"""

from unittest.mock import MagicMock, patch

import app.services.gc_service as gc

# ═══════════════════════════════════════════════════════════════════════════
# _recover_mesh_peers
# ═══════════════════════════════════════════════════════════════════════════


class TestRecoverMeshPeers:
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="job-1")
    @patch("app.services.mesh_service.get_peer_config_for_host")
    def test_success(self, mock_config, mock_start, mock_wait):
        mock_config.return_value = {"wg_interface": "wg0"}
        db = MagicMock()
        host = MagicMock()
        peer = MagicMock()
        peer.project_id = "proj-aabbccdd-1111"
        gc._recover_mesh_peers(db, host, "host-1", [peer])
        mock_start.assert_called_once()
        mock_wait.assert_called_once()

    @patch(
        "app.services.mesh_service.get_peer_config_for_host",
        side_effect=Exception("err"),
    )
    def test_exception_per_peer(self, mock_config):
        db = MagicMock()
        host = MagicMock()
        peer = MagicMock()
        peer.project_id = "proj-eeff0011-2222"
        gc._recover_mesh_peers(db, host, "host-1", [peer])

    @patch("app.services.mesh_service.get_peer_config_for_host")
    def test_multiple_peers(self, mock_config):
        mock_config.return_value = {"wg_interface": "wg0"}
        db = MagicMock()
        host = MagicMock()
        peers = [
            MagicMock(project_id="proj-1111111100000000"),
            MagicMock(project_id="proj-2222222200000000"),
        ]
        with patch("app.services.troshkad_client.start_job", return_value="j"), patch(
            "app.services.troshkad_client.wait_for_job"
        ):
            gc._recover_mesh_peers(db, host, "host-1", peers)
        assert mock_config.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# _reconnect_project_taps
# ═══════════════════════════════════════════════════════════════════════════


class TestReconnectProjectTaps:
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="j-1")
    def test_reconnects_running_vms(self, mock_start, mock_wait):
        mock_wait.return_value = {"result": {"reconnected": 2}}
        proj = MagicMock()
        proj.id = "abcdef12-3456-7890-abcd-ef1234567890"
        vm_states = {
            "troshka-abcdef12-vm111111": "running",
            "troshka-abcdef12-vm222222": "stopped",
            "troshka-00000000-vm333333": "running",
        }
        gc._reconnect_project_taps(MagicMock(), [proj], vm_states)
        mock_start.assert_called_once()

    def test_no_running_vms_skipped(self):
        proj = MagicMock()
        proj.id = "bbbbbbbb-0000-0000-0000-000000000000"
        vm_states = {"troshka-bbbbbbbb-vm1": "stopped"}
        gc._reconnect_project_taps(MagicMock(), [proj], vm_states)

    @patch("app.services.troshkad_client.start_job", side_effect=Exception("conn"))
    def test_exception_handled(self, mock_start):
        proj = MagicMock()
        proj.id = "cccccccc-0000-0000-0000-000000000000"
        vm_states = {"troshka-cccccccc-vm1": "running"}
        gc._reconnect_project_taps(MagicMock(), [proj], vm_states)


# ═══════════════════════════════════════════════════════════════════════════
# _restore_project_bmc
# ═══════════════════════════════════════════════════════════════════════════


class TestRestoreProjectBmc:
    @patch("app.services.deploy_service._setup_bmc_via_troshkad")
    @patch("app.services.deploy_topology._extract_bmc_config", return_value=None)
    def test_no_bmc_config_skipped(self, mock_extract, mock_setup):
        proj = MagicMock()
        proj.id = "dddddddd-0000-0000-0000-000000000000"
        proj.deployed_topology = {"nodes": []}
        proj.topology = None
        result = gc._restore_project_bmc(MagicMock(), [proj])
        assert result == 0
        mock_setup.assert_not_called()

    @patch("app.services.deploy_service._setup_bmc_via_troshkad")
    @patch("app.services.deploy_topology._extract_bmc_config")
    def test_bmc_config_restored(self, mock_extract, mock_setup):
        mock_extract.return_value = {"bmc_network": {}, "vms": []}
        proj = MagicMock()
        proj.id = "eeeeeeee-0000-0000-0000-000000000000"
        proj.deployed_topology = {"nodes": []}
        proj.topology = None
        result = gc._restore_project_bmc(MagicMock(), [proj])
        assert result == 1

    @patch(
        "app.services.deploy_service._setup_bmc_via_troshkad",
        side_effect=Exception("err"),
    )
    @patch(
        "app.services.deploy_topology._extract_bmc_config",
        return_value={"bmc_network": {}},
    )
    def test_bmc_exception_handled(self, mock_extract, mock_setup):
        proj = MagicMock()
        proj.id = "ffffffff-0000-0000-0000-000000000000"
        proj.deployed_topology = {"nodes": []}
        proj.topology = None
        result = gc._restore_project_bmc(MagicMock(), [proj])
        assert result == 0


# ═══════════════════════════════════════════════════════════════════════════
# recover_host_services
# ═══════════════════════════════════════════════════════════════════════════


class TestRecoverHostServices:
    def _setup_mocks(self, projects=None, busy=False):
        mock_db = MagicMock()
        host = MagicMock()
        host.id = "host-aaaabbbb"
        host.agent_status = "connected"
        mock_db.query.return_value.filter_by.return_value.first.return_value = host

        if projects is None:
            projects = []
        mock_db.query.return_value.filter.return_value.all.return_value = projects
        return mock_db, host

    @patch("app.core.database.SessionLocal")
    def test_empty_projects_early_return(self, mock_sl):
        gc._recovering_hosts.discard("host-aaaabbbb")
        mock_db, _ = self._setup_mocks(projects=[])
        mock_sl.return_value = mock_db
        gc.recover_host_services("host-aaaabbbb")
        assert "host-aaaabbbb" not in gc._recovering_hosts

    @patch("app.core.database.SessionLocal")
    def test_busy_projects_deferred(self, mock_sl):
        gc._recovering_hosts.discard("host-bbbbcccc")
        mock_db = MagicMock()
        host = MagicMock()
        host.id = "host-bbbbcccc"
        host.agent_status = "connected"
        mock_db.query.return_value.filter_by.return_value.first.return_value = host
        proj = MagicMock()
        proj.state = "deploying"
        mock_db.query.return_value.filter.return_value.all.side_effect = [
            [proj],  # first call: projects
            [],  # mesh peers
        ]
        mock_sl.return_value = mock_db
        gc.recover_host_services("host-bbbbcccc")
        assert "host-bbbbcccc" not in gc._recovering_hosts

    def test_already_recovering_skipped(self):
        gc._recovering_hosts.add("host-dedupe")
        gc.recover_host_services("host-dedupe")
        gc._recovering_hosts.discard("host-dedupe")

    @patch("app.core.database.SessionLocal")
    def test_exception_cleans_up(self, mock_sl):
        gc._recovering_hosts.discard("host-except")
        mock_db = MagicMock()
        host = MagicMock()
        host.id = "host-except"
        host.agent_status = "connected"
        mock_db.query.return_value.filter_by.return_value.first.return_value = host
        proj = MagicMock()
        proj.state = "active"
        mock_db.query.return_value.filter.return_value.all.side_effect = [
            [proj],
            [],  # mesh peers
        ]
        mock_sl.return_value = mock_db
        with patch(
            "app.services.gc_service.repair_networks", side_effect=RuntimeError("boom")
        ), patch("app.services.gc_service._recover_mesh_peers"):
            gc.recover_host_services("host-except")
        assert "host-except" not in gc._recovering_hosts


# ═══════════════════════════════════════════════════════════════════════════
# clean_s3_orphans
# ═══════════════════════════════════════════════════════════════════════════


class TestCleanS3Orphans:
    @patch("app.services.s3_storage.owner_params", return_value={})
    @patch("app.services.s3_storage._bucket", return_value="test-bucket")
    @patch("app.services.s3_storage._get_s3_config")
    @patch("boto3.client")
    def test_deletes_orphaned_patterns(
        self, mock_boto, mock_config, mock_bucket, mock_op
    ):
        mock_config.return_value = {"region": "us-east-1"}
        s3 = MagicMock()
        mock_boto.return_value = s3
        paginator = MagicMock()
        s3.get_paginator.return_value = paginator

        # patterns/ scan: one orphan
        paginator.paginate.side_effect = [
            # patterns/ with delimiter
            [{"CommonPrefixes": [{"Prefix": "patterns/orphan-id/"}]}],
            # patterns/orphan-id/ contents
            [{"Contents": [{"Key": "patterns/orphan-id/disk.qcow2", "Size": 1024}]}],
            # snapshots/ scan
            [{"CommonPrefixes": []}],
            # library/ scan
            [{"CommonPrefixes": []}],
        ]
        s3.list_multipart_uploads.return_value = {"Uploads": []}

        db = MagicMock()

        db.query.return_value.all.side_effect = [
            [],  # no active patterns
            [],  # no active library items
        ]
        result = gc.clean_s3_orphans(db)
        assert result["deleted"] == 1
        s3.delete_objects.assert_called_once()

    @patch("app.services.s3_storage.owner_params", return_value={})
    @patch("app.services.s3_storage._bucket", return_value="test-bucket")
    @patch("app.services.s3_storage._get_s3_config")
    @patch("boto3.client")
    def test_dry_run_no_deletes(self, mock_boto, mock_config, mock_bucket, mock_op):
        mock_config.return_value = {"region": "us-east-1"}
        s3 = MagicMock()
        mock_boto.return_value = s3
        paginator = MagicMock()
        s3.get_paginator.return_value = paginator
        paginator.paginate.side_effect = [
            [{"CommonPrefixes": [{"Prefix": "patterns/orphan/"}]}],
            [{"Contents": [{"Key": "patterns/orphan/f.qcow2", "Size": 100}]}],
            [{"CommonPrefixes": []}],
            [{"CommonPrefixes": []}],
        ]
        s3.list_multipart_uploads.return_value = {"Uploads": []}
        db = MagicMock()
        db.query.return_value.all.side_effect = [[], []]
        result = gc.clean_s3_orphans(db, dry_run=True)
        assert result["deleted"] == 0
        s3.delete_objects.assert_not_called()

    @patch("app.services.s3_storage.owner_params", return_value={})
    @patch("app.services.s3_storage._bucket", return_value="test-bucket")
    @patch("app.services.s3_storage._get_s3_config")
    @patch("boto3.client")
    def test_aborts_stale_multipart(self, mock_boto, mock_config, mock_bucket, mock_op):
        mock_config.return_value = {"region": "us-east-1"}
        s3 = MagicMock()
        mock_boto.return_value = s3
        paginator = MagicMock()
        s3.get_paginator.return_value = paginator
        paginator.paginate.side_effect = [
            [{"CommonPrefixes": []}],
            [{"CommonPrefixes": []}],
            [{"CommonPrefixes": []}],
        ]
        s3.list_multipart_uploads.return_value = {
            "Uploads": [{"Key": "patterns/orphan-mp/disk.qcow2", "UploadId": "up-1"}]
        }
        db = MagicMock()
        db.query.return_value.all.side_effect = [[], []]
        result = gc.clean_s3_orphans(db)
        assert result["aborted_multipart"] == 1
        s3.abort_multipart_upload.assert_called_once()

    def test_s3_not_configured(self):
        db = MagicMock()
        with patch(
            "app.services.s3_storage._get_s3_config",
            side_effect=Exception("no config"),
        ):
            result = gc.clean_s3_orphans(db)
        assert "error" in result


# ═══════════════════════════════════════════════════════════════════════════
# _clean_orphaned_routes
# ═══════════════════════════════════════════════════════════════════════════


class TestCleanOrphanedRoutes:
    @patch("app.services.providers.ocpvirt._get_k8s_clients")
    def test_deletes_orphaned_routes(self, mock_clients):
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        svc = MagicMock()
        svc.metadata.labels = {"troshka/project-id": "deadbeef"}
        svc.metadata.name = "route-dead"
        mock_core.list_namespaced_service.return_value.items = [svc]

        db = MagicMock()
        db.query.return_value.filter.return_value = []  # no active projects

        provider = MagicMock()
        provider.get_credentials.return_value = {"namespace": "troshka"}
        report = {}

        gc._clean_orphaned_routes(db, MagicMock(), provider, report)
        mock_core.delete_namespaced_service.assert_called_once_with(
            "route-dead", "troshka"
        )
        assert report["routes_cleaned"] == 1

    @patch(
        "app.services.providers.ocpvirt._get_k8s_clients",
        side_effect=Exception("no k8s"),
    )
    def test_k8s_unavailable(self, mock_clients):
        provider = MagicMock()
        provider.get_credentials.return_value = {"namespace": "troshka"}
        gc._clean_orphaned_routes(MagicMock(), MagicMock(), provider, {})
