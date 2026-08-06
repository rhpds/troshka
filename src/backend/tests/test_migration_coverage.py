"""Tests for uncovered paths in migration_service.py.

Covers:
  - validate_migration edge cases (not found, not active, wrong host, same host, no pool)
  - migrate_project enqueue
  - _migrate_vms_to_target
  - _mark_migration_error
  - _do_migrate_project (success + error paths)
  - _do_evacuate_host
"""

from unittest.mock import MagicMock, patch

from app.services.migration_service import (
    _do_evacuate_host,
    _do_migrate_project,
    _mark_migration_error,
    _migrate_vms_to_target,
    migrate_project,
    validate_migration,
)

# ═══════════════════════════════════════════════════════════════════════════
# validate_migration — edge cases
# ═══════════════════════════════════════════════════════════════════════════


class TestValidateMigrationEdgeCases:
    def test_project_not_found(self):
        db = MagicMock()
        db.get.return_value = None
        errs = validate_migration(db, "proj-1", "src-1", "tgt-1")
        assert any("not found" in e.lower() for e in errs)

    def test_project_not_active(self):
        db = MagicMock()
        proj = MagicMock()
        proj.state = "stopped"
        proj.host_id = "src-1"
        db.get.side_effect = lambda model, id: proj if id == "proj-1" else MagicMock()
        errs = validate_migration(db, "proj-1", "src-1", "tgt-1")
        assert any("active" in e.lower() for e in errs)

    def test_project_not_on_source_host(self):
        db = MagicMock()
        proj = MagicMock()
        proj.state = "active"
        proj.host_id = "other-host"
        src = MagicMock()
        src.storage_pool_id = "pool-1"
        tgt = MagicMock()
        tgt.storage_pool_id = "pool-1"
        tgt.state = "active"
        tgt.agent_status = "connected"
        tgt.used_vcpus = 0
        tgt.used_ram_mb = 0

        def _get(model, id):
            if id == "proj-1":
                return proj
            if id == "src-1":
                return src
            if id == "tgt-1":
                return tgt
            return None

        db.get.side_effect = _get
        errs = validate_migration(db, "proj-1", "src-1", "tgt-1")
        assert any("not on" in e.lower() for e in errs)

    def test_same_host_error(self):
        db = MagicMock()
        proj = MagicMock()
        proj.state = "active"
        proj.host_id = "host-1"
        host = MagicMock()
        host.storage_pool_id = "pool-1"

        def _get(model, id):
            if id == "proj-1":
                return proj
            return host

        db.get.side_effect = _get
        errs = validate_migration(db, "proj-1", "host-1", "host-1")
        assert any("same host" in e.lower() for e in errs)

    def test_no_storage_pool(self):
        db = MagicMock()
        proj = MagicMock()
        proj.state = "active"
        proj.host_id = "src-1"
        src = MagicMock()
        src.storage_pool_id = None
        tgt = MagicMock()
        tgt.storage_pool_id = None

        def _get(model, id):
            if id == "proj-1":
                return proj
            if id == "src-1":
                return src
            if id == "tgt-1":
                return tgt
            return None

        db.get.side_effect = _get
        errs = validate_migration(db, "proj-1", "src-1", "tgt-1")
        assert any("storage pool" in e.lower() for e in errs)

    def test_target_host_not_found(self):
        db = MagicMock()
        proj = MagicMock()
        proj.state = "active"
        proj.host_id = "src-1"
        src = MagicMock()

        def _get(model, id):
            if id == "proj-1":
                return proj
            if id == "src-1":
                return src
            return None

        db.get.side_effect = _get
        errs = validate_migration(db, "proj-1", "src-1", "tgt-1")
        assert any("target" in e.lower() and "not found" in e.lower() for e in errs)


# ═══════════════════════════════════════════════════════════════════════════
# migrate_project
# ═══════════════════════════════════════════════════════════════════════════


class TestMigrateProject:
    @patch("app.core.redis.enqueue_job")
    def test_enqueues_job(self, mock_enqueue):
        migrate_project("proj-1", "src-1", "tgt-1")
        mock_enqueue.assert_called_once()
        args = mock_enqueue.call_args
        assert args[0][1] == "proj-1"
        assert args[0][2] == "src-1"
        assert args[0][3] == "tgt-1"


# ═══════════════════════════════════════════════════════════════════════════
# _migrate_vms_to_target
# ═══════════════════════════════════════════════════════════════════════════


class TestMigrateVmsToTarget:
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="job-1")
    def test_migrates_in_start_order(self, mock_start, mock_wait):
        mock_wait.return_value = {"status": "completed"}
        source = MagicMock()
        target = MagicMock()
        target.private_ip = "10.0.0.2"
        target.ip_address = "1.2.3.4"
        project = MagicMock()
        project.id = "abcdef12-0000-0000-0000-000000000000"
        topology = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode"},
                {"id": "vm-2", "type": "vmNode"},
            ],
            "startOrder": [{"vmId": "vm-2"}, {"vmId": "vm-1"}],
        }
        _migrate_vms_to_target(source, target, project, topology)
        assert mock_start.call_count == 2
        # First call should be vm-2 (start order)
        first_call_params = mock_start.call_args_list[0][0][2]
        assert "vm-2" in first_call_params["domain"]

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="job-1")
    def test_raises_on_failure(self, mock_start, mock_wait):
        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "disk locked"},
        }
        source = MagicMock()
        target = MagicMock()
        target.private_ip = "10.0.0.2"
        project = MagicMock()
        project.id = "aaaaaaaa-0000-0000-0000-000000000000"
        topology = {"nodes": [{"id": "vm-1", "type": "vmNode"}], "startOrder": []}
        try:
            _migrate_vms_to_target(source, target, project, topology)
            assert False, "Should have raised"
        except RuntimeError as e:
            assert "disk locked" in str(e)

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="job-1")
    def test_uses_private_ip(self, mock_start, mock_wait):
        mock_wait.return_value = {"status": "completed"}
        source = MagicMock()
        target = MagicMock()
        target.private_ip = "10.0.0.99"
        target.ip_address = "1.2.3.4"
        project = MagicMock()
        project.id = "bbbbbbbb-0000-0000-0000-000000000000"
        topology = {"nodes": [{"id": "vm-1", "type": "vmNode"}]}
        _migrate_vms_to_target(source, target, project, topology)
        params = mock_start.call_args[0][2]
        assert params["target_host"] == "10.0.0.99"


# ═══════════════════════════════════════════════════════════════════════════
# _mark_migration_error
# ═══════════════════════════════════════════════════════════════════════════


class TestMarkMigrationError:
    def test_marks_error(self):
        db = MagicMock()
        proj = MagicMock()
        db.get.return_value = proj
        _mark_migration_error(db, "proj-1", RuntimeError("test"))
        assert proj.state == "error"
        assert "test" in proj.deploy_error
        db.commit.assert_called()

    def test_project_not_found(self):
        db = MagicMock()
        db.get.return_value = None
        _mark_migration_error(db, "proj-gone", RuntimeError("err"))

    def test_exception_swallowed(self):
        db = MagicMock()
        db.get.side_effect = Exception("db error")
        _mark_migration_error(db, "proj-x", RuntimeError("err"))


# ═══════════════════════════════════════════════════════════════════════════
# _do_migrate_project
# ═══════════════════════════════════════════════════════════════════════════


class TestDoMigrateProject:
    @patch("app.services.migration_service._mark_migration_error")
    @patch("app.services.migration_service._migrate_vms_to_target")
    @patch("app.services.deploy_service._teardown_bmc_via_troshkad")
    @patch("app.services.deploy_service._teardown_networks_via_troshkad")
    @patch("app.services.deploy_service._setup_bmc_via_troshkad")
    @patch("app.services.deploy_service._setup_networks_via_troshkad")
    @patch("app.services.deploy_topology._extract_bmc_config")
    @patch("app.services.migration_service.SessionLocal")
    def test_success(
        self,
        mock_sl,
        mock_bmc_extract,
        mock_net_setup,
        mock_bmc_setup,
        mock_net_teardown,
        mock_bmc_teardown,
        mock_migrate_vms,
        mock_mark_err,
    ):
        mock_db = MagicMock()
        mock_sl.return_value = mock_db
        proj = MagicMock()
        proj.id = "proj-aaaa0000"
        proj.deployed_topology = {"nodes": [{"type": "networkNode"}]}
        proj.topology = None
        proj.vni_map = {"n1": 100}
        source = MagicMock()
        target = MagicMock()

        entity_map = {
            "proj-aaaa0000": proj,
            "src-1": source,
            "tgt-1": target,
        }
        mock_db.get.side_effect = lambda model, eid: entity_map.get(eid)
        mock_bmc_extract.return_value = {"bmc_network": {}}

        _do_migrate_project("proj-aaaa0000", "src-1", "tgt-1")

        assert proj.host_id == "tgt-1"
        assert proj.state == "active"
        mock_net_setup.assert_called_once()
        mock_bmc_setup.assert_called_once()
        mock_migrate_vms.assert_called_once()
        mock_net_teardown.assert_called_once()
        mock_bmc_teardown.assert_called_once()

    @patch("app.services.migration_service._mark_migration_error")
    @patch("app.services.migration_service.SessionLocal")
    def test_missing_entities(self, mock_sl, mock_mark_err):
        mock_db = MagicMock()
        mock_sl.return_value = mock_db
        mock_db.get.return_value = None
        _do_migrate_project("proj-11111111", "src-1", "tgt-1")
        mock_mark_err.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# _do_evacuate_host
# ═══════════════════════════════════════════════════════════════════════════


class TestDoEvacuateHost:
    @patch("app.services.migration_service._do_migrate_project")
    @patch("app.services.migration_service.SessionLocal")
    def test_evacuates_projects(self, mock_sl, mock_migrate):
        mock_db = MagicMock()
        mock_sl.return_value = mock_db
        host = MagicMock()
        host.id = "host-evac1234"
        host.storage_pool_id = "pool-1"
        mock_db.get.side_effect = lambda model, id: (
            host if id == "host-evac1234" else None
        )
        proj = MagicMock()
        proj.id = "proj-111"
        proj.state = "active"
        other_host = MagicMock()
        other_host.id = "host-target"
        mock_db.query.return_value.filter.return_value.all.side_effect = [
            [proj],  # active projects
            [other_host],  # other hosts
        ]
        _do_evacuate_host("host-evac1234")
        mock_migrate.assert_called_once_with("proj-111", "host-evac1234", "host-target")
        assert host.state == "maintenance"

    @patch("app.services.migration_service.SessionLocal")
    def test_no_projects(self, mock_sl):
        mock_db = MagicMock()
        mock_sl.return_value = mock_db
        host = MagicMock()
        host.id = "host-empty123"
        mock_db.get.side_effect = lambda model, id: host
        mock_db.query.return_value.filter.return_value.all.return_value = []
        _do_evacuate_host("host-empty123")

    @patch("app.services.migration_service.SessionLocal")
    def test_host_not_found(self, mock_sl):
        mock_db = MagicMock()
        mock_sl.return_value = mock_db
        mock_db.get.side_effect = lambda model, id: None
        _do_evacuate_host("host-gone1234")

    @patch("app.services.migration_service.SessionLocal")
    def test_no_other_hosts(self, mock_sl):
        mock_db = MagicMock()
        mock_sl.return_value = mock_db
        host = MagicMock()
        host.id = "host-solo1234"
        host.storage_pool_id = "pool-1"
        mock_db.get.side_effect = lambda model, id: host
        proj = MagicMock()
        proj.id = "proj-1"
        mock_db.query.return_value.filter.return_value.all.side_effect = [
            [proj],
            [],  # no other hosts
        ]
        _do_evacuate_host("host-solo1234")
