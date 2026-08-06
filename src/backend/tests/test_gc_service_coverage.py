"""Tests for uncovered functions in gc_service.py.

Covers:
  - sync_host_capacity
  - _get_pool_host_ids
  - _collect_known_projects_and_domains
  - _collect_bmc_project_ids
  - discover_orphans
  - _find_orphaned_cache
  - clean_orphans
  - _get_existing_bridges
  - _count_total_orphans
  - _reconcile_clean_orphans
  - _reconcile_ocp_routes
  - _reconcile_shared_cache_entries
  - reconcile_host
  - _extract_node_item_ids
  - _collect_referenced_items
  - repair_networks
"""

from unittest.mock import MagicMock, patch

import app.services.gc_service as gc

# ═══════════════════════════════════════════════════════════════════════════
# sync_host_capacity
# ═══════════════════════════════════════════════════════════════════════════


class TestSyncHostCapacity:
    def _make_host(self, used_vcpus=0, used_ram_mb=0):
        h = MagicMock()
        h.id = "host-11112222"
        h.used_vcpus = used_vcpus
        h.used_ram_mb = used_ram_mb
        return h

    def _make_project(self, state, topology):
        p = MagicMock()
        p.state = state
        p.deployed_topology = topology
        p.topology = topology
        return p

    def test_no_projects(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        host = self._make_host(used_vcpus=4, used_ram_mb=8192)
        result = gc.sync_host_capacity(db, host)
        assert host.used_vcpus == 0
        assert host.used_ram_mb == 0
        assert result["new"] == {"used_vcpus": 0, "used_ram_mb": 0}
        assert result["changed"] is True

    def test_active_project_with_vm_nodes(self):
        db = MagicMock()
        topo = {
            "nodes": [
                {"type": "vmNode", "data": {"vcpus": 4, "ram": 8}},
                {"type": "vmNode", "data": {"vcpus": 2, "ram": 4}},
            ]
        }
        proj = self._make_project("active", topo)
        db.query.return_value.filter.return_value.all.return_value = [proj]
        host = self._make_host()
        result = gc.sync_host_capacity(db, host)
        assert host.used_vcpus == 6
        assert host.used_ram_mb == 12288  # (8+4)*1024
        assert result["new"]["used_vcpus"] == 6
        assert result["new"]["used_ram_mb"] == 12288

    def test_project_with_container_nodes(self):
        db = MagicMock()
        topo = {
            "nodes": [
                {"type": "vmNode", "data": {"vcpus": 2, "ram": 4}},
                {"type": "containerNode", "data": {"cpus": 1, "memory": 512}},
            ]
        }
        proj = self._make_project("active", topo)
        db.query.return_value.filter.return_value.all.return_value = [proj]
        host = self._make_host()
        _result = gc.sync_host_capacity(db, host)
        assert host.used_vcpus == 3
        assert host.used_ram_mb == 4608  # 4*1024 + 512

    def test_stopped_project_still_counts(self):
        db = MagicMock()
        topo = {"nodes": [{"type": "vmNode", "data": {"vcpus": 8, "ram": 16}}]}
        proj = self._make_project("stopped", topo)
        db.query.return_value.filter.return_value.all.return_value = [proj]
        host = self._make_host()
        _result = gc.sync_host_capacity(db, host)
        assert host.used_vcpus == 8
        assert host.used_ram_mb == 16384

    def test_unchanged_returns_false(self):
        db = MagicMock()
        topo = {"nodes": [{"type": "vmNode", "data": {"vcpus": 4, "ram": 8}}]}
        proj = self._make_project("active", topo)
        db.query.return_value.filter.return_value.all.return_value = [proj]
        host = self._make_host(used_vcpus=4, used_ram_mb=8192)
        result = gc.sync_host_capacity(db, host)
        assert result["changed"] is False

    def test_node_with_missing_data(self):
        db = MagicMock()
        topo = {
            "nodes": [
                {"type": "vmNode"},  # no data key
                {"type": "networkNode", "data": {}},  # not vm/container
            ]
        }
        proj = self._make_project("active", topo)
        db.query.return_value.filter.return_value.all.return_value = [proj]
        host = self._make_host()
        _result = gc.sync_host_capacity(db, host)
        assert host.used_vcpus == 0
        assert host.used_ram_mb == 0

    def test_uses_deployed_topology_over_topology(self):
        db = MagicMock()
        proj = MagicMock()
        proj.state = "active"
        proj.deployed_topology = {
            "nodes": [{"type": "vmNode", "data": {"vcpus": 10, "ram": 32}}]
        }
        proj.topology = {"nodes": [{"type": "vmNode", "data": {"vcpus": 2, "ram": 4}}]}
        db.query.return_value.filter.return_value.all.return_value = [proj]
        host = self._make_host()
        gc.sync_host_capacity(db, host)
        assert host.used_vcpus == 10
        assert host.used_ram_mb == 32768


# ═══════════════════════════════════════════════════════════════════════════
# _get_pool_host_ids
# ═══════════════════════════════════════════════════════════════════════════


class TestGetPoolHostIds:
    def test_no_storage_pool(self):
        db = MagicMock()
        host = MagicMock()
        host.id = "host-solo"
        host.storage_pool_id = None
        result = gc._get_pool_host_ids(db, host)
        assert result == ["host-solo"]

    def test_with_storage_pool(self):
        db = MagicMock()
        host = MagicMock()
        host.id = "host-pool-1"
        host.storage_pool_id = "pool-abc"
        h1 = MagicMock()
        h1.id = "host-pool-1"
        h2 = MagicMock()
        h2.id = "host-pool-2"
        db.query.return_value.filter.return_value.all.return_value = [h1, h2]
        result = gc._get_pool_host_ids(db, host)
        assert result == ["host-pool-1", "host-pool-2"]


# ═══════════════════════════════════════════════════════════════════════════
# _collect_known_projects_and_domains
# ═══════════════════════════════════════════════════════════════════════════


class TestCollectKnownProjectsAndDomains:
    def test_active_and_stopped_projects(self):
        db = MagicMock()
        p1 = MagicMock()
        p1.id = "aabbccdd-1111-2222-3333-444455556666"
        p1.state = "active"
        p2 = MagicMock()
        p2.id = "eeff0011-2233-4455-6677-8899aabbccdd"
        p2.state = "stopped"
        db.query.return_value.filter.return_value.all.return_value = [p1, p2]
        ids, domains = gc._collect_known_projects_and_domains(db, ["host-1"])
        assert p1.id in ids
        assert p2.id in ids
        assert "troshka-aabbccdd" in domains
        assert "troshka-eeff0011" in domains

    def test_deploying_projects_included(self):
        db = MagicMock()
        p = MagicMock()
        p.id = "deploying1-1111-2222-3333-444455556666"
        p.state = "deploying"
        db.query.return_value.filter.return_value.all.return_value = [p]
        ids, domains = gc._collect_known_projects_and_domains(db, ["host-1"])
        assert p.id in ids
        assert f"troshka-{p.id[:8]}" in domains

    def test_no_projects(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        ids, domains = gc._collect_known_projects_and_domains(db, ["host-1"])
        assert ids == []
        assert domains == []

    def test_draft_project_excluded(self):
        db = MagicMock()
        p = MagicMock()
        p.id = "draft1234-1111-2222-3333-444455556666"
        p.state = "draft"
        db.query.return_value.filter.return_value.all.return_value = [p]
        ids, domains = gc._collect_known_projects_and_domains(db, ["host-1"])
        assert ids == []
        assert domains == []


# ═══════════════════════════════════════════════════════════════════════════
# _collect_bmc_project_ids
# ═══════════════════════════════════════════════════════════════════════════


class TestCollectBmcProjectIds:
    def test_project_with_bmc_network(self):
        db = MagicMock()
        p = MagicMock()
        p.id = "bmc-proj-1111-2222-3333-444455556666"
        p.state = "active"
        p.deployed_topology = {
            "nodes": [
                {"type": "networkNode", "data": {"networkType": "bmc"}},
            ]
        }
        p.topology = None
        db.query.return_value.filter.return_value.all.return_value = [p]
        result = gc._collect_bmc_project_ids(db, ["host-1"])
        assert p.id in result

    def test_project_without_bmc(self):
        db = MagicMock()
        p = MagicMock()
        p.id = "no-bmc-11-2222-3333-444455556666"
        p.state = "active"
        p.deployed_topology = {
            "nodes": [
                {"type": "networkNode", "data": {"networkType": "standard"}},
            ]
        }
        p.topology = None
        db.query.return_value.filter.return_value.all.return_value = [p]
        result = gc._collect_bmc_project_ids(db, ["host-1"])
        assert result == []

    def test_non_active_project_excluded(self):
        db = MagicMock()
        p = MagicMock()
        p.id = "deploying-1111-2222-3333-444455556666"
        p.state = "deploying"
        p.deployed_topology = {
            "nodes": [
                {"type": "networkNode", "data": {"networkType": "bmc"}},
            ]
        }
        p.topology = None
        db.query.return_value.filter.return_value.all.return_value = [p]
        result = gc._collect_bmc_project_ids(db, ["host-1"])
        assert result == []

    def test_empty_topology(self):
        db = MagicMock()
        p = MagicMock()
        p.id = "emptytopo-1111-2222-3333-444455556666"
        p.state = "active"
        p.deployed_topology = None
        p.topology = None
        db.query.return_value.filter.return_value.all.return_value = [p]
        result = gc._collect_bmc_project_ids(db, ["host-1"])
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════
# discover_orphans
# ═══════════════════════════════════════════════════════════════════════════


class TestDiscoverOrphans:
    def test_host_not_reachable_no_ip(self):
        db = MagicMock()
        host = MagicMock()
        host.ip_address = None
        host.agent_status = "connected"
        result = gc.discover_orphans(db, host)
        assert result["error"] == "Host not reachable"

    def test_host_not_reachable_disconnected(self):
        db = MagicMock()
        host = MagicMock()
        host.ip_address = "10.0.0.1"
        host.agent_status = "disconnected"
        result = gc.discover_orphans(db, host)
        assert result["error"] == "Host not reachable"

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="job-disc")
    @patch("app.services.gc_service._collect_bmc_project_ids", return_value=[])
    @patch(
        "app.services.gc_service._collect_known_projects_and_domains",
        return_value=(["proj-1"], ["troshka-proj-1"]),
    )
    @patch("app.services.gc_service._get_pool_host_ids", return_value=["host-1"])
    def test_successful_discovery(
        self, mock_pool, mock_known, mock_bmc, mock_start, mock_wait
    ):
        mock_wait.return_value = {
            "status": "completed",
            "result": {"orphan_dirs": ["/tmp/old"], "orphan_domains": []},
        }
        db = MagicMock()
        host = MagicMock()
        host.ip_address = "10.0.0.1"
        host.agent_status = "connected"
        result = gc.discover_orphans(db, host)
        assert result == {"orphan_dirs": ["/tmp/old"], "orphan_domains": []}

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="job-fail")
    @patch("app.services.gc_service._collect_bmc_project_ids", return_value=[])
    @patch(
        "app.services.gc_service._collect_known_projects_and_domains",
        return_value=([], []),
    )
    @patch("app.services.gc_service._get_pool_host_ids", return_value=["host-1"])
    def test_discovery_job_fails(
        self, mock_pool, mock_known, mock_bmc, mock_start, mock_wait
    ):
        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "timed out"},
        }
        db = MagicMock()
        host = MagicMock()
        host.ip_address = "10.0.0.1"
        host.agent_status = "connected"
        result = gc.discover_orphans(db, host)
        assert result["error"] == "timed out"


# ═══════════════════════════════════════════════════════════════════════════
# _find_orphaned_cache
# ═══════════════════════════════════════════════════════════════════════════


class TestFindOrphanedCache:
    def test_matching_pattern_not_orphaned(self):
        db = MagicMock()
        pattern = MagicMock()
        pattern.id = "pat-111"
        db.query.return_value.all.side_effect = [
            [pattern],  # patterns
            [],  # library items
        ]
        items = [{"path": "/var/lib/troshka/images/pat-111.qcow2"}]
        result = gc._find_orphaned_cache(db, items)
        assert result == []

    def test_matching_library_item_not_orphaned(self):
        db = MagicMock()
        lib = MagicMock()
        lib.id = "lib-222"
        db.query.return_value.all.side_effect = [
            [],  # patterns
            [lib],  # library items
        ]
        items = [{"path": "/cache/lib-222.qcow2"}]
        result = gc._find_orphaned_cache(db, items)
        assert result == []

    def test_no_match_is_orphaned(self):
        db = MagicMock()
        db.query.return_value.all.side_effect = [[], []]
        items = [{"path": "/cache/unknown-id.qcow2"}]
        result = gc._find_orphaned_cache(db, items)
        assert len(result) == 1
        assert "/cache/unknown-id.qcow2" in result

    def test_string_items_handled(self):
        db = MagicMock()
        db.query.return_value.all.side_effect = [[], []]
        items = ["/cache/stale-id.qcow2"]
        result = gc._find_orphaned_cache(db, items)
        assert len(result) == 1

    def test_directory_path_stripped(self):
        db = MagicMock()
        active = MagicMock()
        active.id = "dir-id"
        db.query.return_value.all.side_effect = [
            [active],  # pattern
            [],
        ]
        items = [{"path": "/cache/patterns/dir-id/"}]
        result = gc._find_orphaned_cache(db, items)
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════
# clean_orphans
# ═══════════════════════════════════════════════════════════════════════════


class TestCleanOrphans:
    def test_host_not_reachable(self):
        host = MagicMock()
        host.ip_address = None
        host.agent_status = "disconnected"
        result = gc.clean_orphans(host, {})
        assert result["error"] == "Host not reachable"
        assert result["cleaned"] == 0

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="clean-job")
    def test_successful_cleanup(self, mock_start, mock_wait):
        mock_wait.return_value = {
            "status": "completed",
            "output": ["deleted /tmp/old"],
        }
        host = MagicMock()
        host.ip_address = "10.0.0.1"
        host.agent_status = "connected"
        orphans = {
            "orphan_dirs": ["/tmp/old1", "/tmp/old2"],
            "orphan_domains": ["dom-1"],
            "orphan_containers": [],
            "orphan_bridges": ["br-999"],
            "orphan_namespaces": [],
            "orphaned_bmc_project_ids": [],
        }
        result = gc.clean_orphans(host, orphans)
        assert result["success"] is True
        assert result["cleaned"] == 4  # 2 dirs + 1 domain + 1 bridge

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="clean-job-2")
    @patch(
        "app.services.gc_service._find_orphaned_cache", return_value=["/stale.qcow2"]
    )
    def test_with_db_includes_cache(self, mock_cache, mock_start, mock_wait):
        mock_wait.return_value = {"status": "completed", "output": []}
        host = MagicMock()
        host.ip_address = "10.0.0.1"
        host.agent_status = "connected"
        db = MagicMock()
        orphans = {"cache_items": [{"path": "/old"}]}
        result = gc.clean_orphans(host, orphans, db=db)
        assert result["cache_cleaned"] == 1  # 1 from _find_orphaned_cache

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="clean-j3")
    def test_stale_temps_included(self, mock_start, mock_wait):
        mock_wait.return_value = {"status": "completed", "output": []}
        host = MagicMock()
        host.ip_address = "10.0.0.1"
        host.agent_status = "connected"
        orphans = {"stale_temps": ["/tmp/stale1", "/tmp/stale2"]}
        result = gc.clean_orphans(host, orphans)
        # stale_temps are passed as cache_items to the job
        assert result["cache_cleaned"] == 2


# ═══════════════════════════════════════════════════════════════════════════
# _get_existing_bridges
# ═══════════════════════════════════════════════════════════════════════════


class TestGetExistingBridges:
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="br-job")
    def test_successful(self, mock_start, mock_wait):
        mock_wait.return_value = {
            "status": "completed",
            "result": {"bridges": ["br-100", "br-200"]},
        }
        host = MagicMock()
        result = gc._get_existing_bridges(host)
        assert result == {"br-100", "br-200"}

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="br-job-2")
    def test_failed_job_returns_empty(self, mock_start, mock_wait):
        mock_wait.return_value = {"status": "failed", "result": {}}
        host = MagicMock()
        result = gc._get_existing_bridges(host)
        assert result == set()

    @patch(
        "app.services.troshkad_client.start_job",
        side_effect=__import__(
            "app.services.troshkad_client", fromlist=["TroshkadError"]
        ).TroshkadError("conn err"),
    )
    def test_troshkad_error_returns_empty(self, mock_start):
        host = MagicMock()
        result = gc._get_existing_bridges(host)
        assert result == set()


# ═══════════════════════════════════════════════════════════════════════════
# _count_total_orphans
# ═══════════════════════════════════════════════════════════════════════════


class TestCountTotalOrphans:
    def test_all_categories_populated(self):
        orphans = {
            "orphan_dirs": ["a", "b"],
            "orphan_domains": ["c"],
            "orphan_containers": ["d", "e", "f"],
            "orphan_bridges": ["g"],
            "orphan_namespaces": ["h", "i"],
            "orphaned_bmc_project_ids": ["j"],
        }
        assert gc._count_total_orphans(orphans) == 10

    def test_empty_dict(self):
        assert gc._count_total_orphans({}) == 0

    def test_missing_keys(self):
        orphans = {"orphan_dirs": ["x"]}
        assert gc._count_total_orphans(orphans) == 1

    def test_some_empty(self):
        orphans = {
            "orphan_dirs": [],
            "orphan_domains": ["d1"],
            "orphan_containers": [],
            "orphan_bridges": [],
            "orphan_namespaces": [],
            "orphaned_bmc_project_ids": [],
        }
        assert gc._count_total_orphans(orphans) == 1


# ═══════════════════════════════════════════════════════════════════════════
# _reconcile_clean_orphans
# ═══════════════════════════════════════════════════════════════════════════


class TestReconcileCleanOrphans:
    @patch("app.services.gc_service.clean_orphans")
    @patch("app.services.gc_service._find_orphaned_cache", return_value=[])
    def test_orphans_found_not_dry_run(self, mock_cache, mock_clean):
        mock_clean.return_value = {"cleaned": 3, "cache_cleaned": 0}
        db = MagicMock()
        host = MagicMock()
        orphans = {"orphan_dirs": ["a", "b", "c"]}
        report = {}
        gc._reconcile_clean_orphans(db, host, "host-1234abcd", orphans, False, report)
        mock_clean.assert_called_once()
        assert report["orphans_found"] == 3
        assert report["cleanup"]["cleaned"] == 3

    @patch("app.services.gc_service._find_orphaned_cache", return_value=["/stale"])
    def test_orphans_found_dry_run(self, mock_cache):
        db = MagicMock()
        host = MagicMock()
        orphans = {"orphan_dirs": ["a"]}
        report = {}
        gc._reconcile_clean_orphans(db, host, "host-1234abcd", orphans, True, report)
        assert report["cleanup"]["dry_run"] is True
        assert report["cleanup"]["would_clean"] == 2  # 1 dir + 1 cache

    @patch("app.services.gc_service._find_orphaned_cache", return_value=[])
    def test_no_orphans(self, mock_cache):
        db = MagicMock()
        host = MagicMock()
        orphans = {}
        report = {}
        gc._reconcile_clean_orphans(db, host, "host-1234abcd", orphans, False, report)
        assert report["cleanup"]["cleaned"] == 0
        assert report["orphans_found"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# _reconcile_ocp_routes
# ═══════════════════════════════════════════════════════════════════════════


class TestReconcileOcpRoutes:
    def test_no_provider_id(self):
        db = MagicMock()
        host = MagicMock()
        host.provider_id = None
        report = {}
        gc._reconcile_ocp_routes(db, host, "host-1234abcd", report)
        assert report == {}

    def test_provider_not_ocpvirt(self):
        db = MagicMock()
        host = MagicMock()
        host.provider_id = "prov-1"
        provider = MagicMock()
        provider.type = "ec2"
        db.query.return_value.filter_by.return_value.first.return_value = provider
        report = {}
        gc._reconcile_ocp_routes(db, host, "host-1234abcd", report)
        assert report == {}

    @patch("app.services.gc_service._clean_orphaned_routes")
    @patch("app.services.providers.get_provider_driver")
    def test_ocpvirt_calls_clean(self, mock_driver, mock_clean):
        db = MagicMock()
        host = MagicMock()
        host.provider_id = "prov-ocp"
        provider = MagicMock()
        provider.type = "ocpvirt"
        db.query.return_value.filter_by.return_value.first.return_value = provider
        report = {}
        gc._reconcile_ocp_routes(db, host, "host-1234abcd", report)
        mock_clean.assert_called_once()

    @patch(
        "app.services.providers.get_provider_driver",
        side_effect=Exception("driver fail"),
    )
    def test_exception_handled(self, mock_driver):
        db = MagicMock()
        host = MagicMock()
        host.provider_id = "prov-err"
        provider = MagicMock()
        provider.type = "ocpvirt"
        db.query.return_value.filter_by.return_value.first.return_value = provider
        report = {}
        gc._reconcile_ocp_routes(db, host, "host-1234abcd", report)
        # Should not raise, just log warning

    def test_provider_not_found(self):
        db = MagicMock()
        host = MagicMock()
        host.provider_id = "prov-gone"
        db.query.return_value.filter_by.return_value.first.return_value = None
        report = {}
        gc._reconcile_ocp_routes(db, host, "host-1234abcd", report)
        assert report == {}


# ═══════════════════════════════════════════════════════════════════════════
# _reconcile_shared_cache_entries
# ═══════════════════════════════════════════════════════════════════════════


class TestReconcileSharedCacheEntries:
    def test_no_storage_pool(self):
        db = MagicMock()
        host = MagicMock()
        host.storage_pool_id = None
        report = {}
        gc._reconcile_shared_cache_entries(db, host, "host-1234abcd", report)
        assert report == {}

    @patch("app.models.storage_pool.SharedCacheEntry")
    @patch("app.models.library.LibraryItem")
    @patch("app.models.pattern.Pattern")
    def test_orphaned_entries_deleted(self, mock_pat, mock_lib, mock_sce):
        db = MagicMock()
        host = MagicMock()
        host.storage_pool_id = "pool-1"

        # Active patterns/library
        pat = MagicMock()
        pat.id = "pat-active"
        lib = MagicMock()
        lib.id = "lib-active"

        # Build the chain: db.query(Pattern).all() -> [pat], db.query(LibraryItem).all() -> [lib]
        # db.query(SharedCacheEntry).filter(...).all() -> [orphan_entry]
        orphan_entry = MagicMock()
        orphan_entry.item_id = "orphan-item"

        # Since the function uses multiple db.query calls with different models,
        # we need to handle them in sequence
        _query_mock = MagicMock()

        call_count = {"n": 0}
        _original_query = db.query

        def query_side_effect(model):
            call_count["n"] += 1
            m = MagicMock()
            if call_count["n"] == 1:  # Pattern
                m.all.return_value = [pat]
            elif call_count["n"] == 2:  # LibraryItem
                m.all.return_value = [lib]
            elif call_count["n"] == 3:  # SharedCacheEntry
                m.filter.return_value.all.return_value = [orphan_entry]
            return m

        db.query.side_effect = query_side_effect

        report = {}
        gc._reconcile_shared_cache_entries(db, host, "host-1234abcd", report)
        assert report.get("shared_cache_entries_cleaned") == 1
        db.delete.assert_called_once_with(orphan_entry)
        db.commit.assert_called()

    def test_no_orphaned_entries(self):
        db = MagicMock()
        host = MagicMock()
        host.storage_pool_id = "pool-2"

        call_count = {"n": 0}

        def query_side_effect(model):
            call_count["n"] += 1
            m = MagicMock()
            if call_count["n"] <= 2:  # Pattern and LibraryItem
                m.all.return_value = []
            else:  # SharedCacheEntry
                m.filter.return_value.all.return_value = []
            return m

        db.query.side_effect = query_side_effect
        report = {}
        gc._reconcile_shared_cache_entries(db, host, "host-1234abcd", report)
        assert "shared_cache_entries_cleaned" not in report


# ═══════════════════════════════════════════════════════════════════════════
# reconcile_host
# ═══════════════════════════════════════════════════════════════════════════


class TestReconcileHost:
    @patch("app.core.database.SessionLocal")
    def test_host_not_found(self, mock_sl):
        db = MagicMock()
        mock_sl.return_value = db
        db.query.return_value.filter_by.return_value.first.return_value = None
        result = gc.reconcile_host("host-missing")
        assert result["error"] == "Host not found"

    @patch("app.core.database.SessionLocal")
    def test_projects_deploying_skips_gc(self, mock_sl):
        db = MagicMock()
        mock_sl.return_value = db
        host = MagicMock()
        host.id = "host-deploy"
        host.ip_address = "10.0.0.1"
        db.query.return_value.filter_by.return_value.first.return_value = host
        db.query.return_value.filter.return_value.count.return_value = 2
        result = gc.reconcile_host("host-deploy")
        assert "skipped" in result
        assert "2 project(s) deploying" in result["skipped"]

    @patch("app.services.gc_service.sync_host_capacity")
    @patch("app.core.database.SessionLocal")
    def test_host_not_reachable_skips_orphan(self, mock_sl, mock_sync):
        db = MagicMock()
        mock_sl.return_value = db
        host = MagicMock()
        host.id = "host-noip1234"
        host.ip_address = None
        host.agent_status = "disconnected"
        db.query.return_value.filter_by.return_value.first.return_value = host
        db.query.return_value.filter.return_value.count.return_value = 0
        mock_sync.return_value = {"old": {}, "new": {}, "changed": False}
        result = gc.reconcile_host("host-noip1234")
        assert "error" in result.get("orphans", {})

    @patch("app.services.gc_service._reconcile_shared_cache_entries")
    @patch("app.services.gc_service._reconcile_ocp_routes")
    @patch(
        "app.services.gc_service.clean_s3_orphans",
        return_value={"deleted": 0, "aborted_multipart": 0},
    )
    @patch("app.services.gc_service.repair_networks", return_value={"repaired": 0})
    @patch("app.services.gc_service._reconcile_clean_orphans")
    @patch("app.services.gc_service.discover_orphans")
    @patch("app.services.gc_service.sync_host_capacity")
    @patch("app.core.database.SessionLocal")
    def test_full_flow(
        self,
        mock_sl,
        mock_sync,
        mock_discover,
        mock_reconcile_clean,
        mock_repair,
        mock_s3,
        mock_routes,
        mock_shared,
    ):
        db = MagicMock()
        mock_sl.return_value = db
        host = MagicMock()
        host.id = "host-full1234"
        host.ip_address = "10.0.0.1"
        host.agent_status = "connected"
        host.provider_id = None
        host.storage_pool_id = None
        db.query.return_value.filter_by.return_value.first.return_value = host
        db.query.return_value.filter.return_value.count.return_value = 0
        mock_sync.return_value = {"old": {}, "new": {}, "changed": False}
        mock_discover.return_value = {
            "orphan_dirs": [],
            "orphan_domains": [],
        }

        result = gc.reconcile_host("host-full1234")
        assert result["host_id"] == "host-full1234"
        mock_sync.assert_called()
        mock_discover.assert_called_once()
        mock_reconcile_clean.assert_called_once()
        mock_repair.assert_called_once()
        mock_routes.assert_called_once()

    @patch("app.core.database.SessionLocal")
    def test_exception_returns_error(self, mock_sl):
        db = MagicMock()
        mock_sl.return_value = db
        db.query.return_value.filter_by.return_value.first.side_effect = RuntimeError(
            "db boom"
        )
        result = gc.reconcile_host("host-err12345")
        assert "error" in result

    @patch("app.services.gc_service._reconcile_shared_cache_entries")
    @patch("app.services.gc_service._reconcile_ocp_routes")
    @patch(
        "app.services.gc_service.clean_s3_orphans",
        return_value={"deleted": 0, "aborted_multipart": 0},
    )
    @patch("app.services.gc_service.repair_networks", return_value={"repaired": 0})
    @patch("app.services.gc_service._reconcile_clean_orphans")
    @patch("app.services.gc_service.discover_orphans")
    @patch("app.services.gc_service.sync_host_capacity")
    @patch("app.core.database.SessionLocal")
    def test_discover_error_returns_early(
        self,
        mock_sl,
        mock_sync,
        mock_discover,
        mock_reconcile_clean,
        mock_repair,
        mock_s3,
        mock_routes,
        mock_shared,
    ):
        db = MagicMock()
        mock_sl.return_value = db
        host = MagicMock()
        host.id = "host-disc1234"
        host.ip_address = "10.0.0.1"
        host.agent_status = "connected"
        db.query.return_value.filter_by.return_value.first.return_value = host
        db.query.return_value.filter.return_value.count.return_value = 0
        mock_sync.return_value = {"old": {}, "new": {}, "changed": False}
        mock_discover.return_value = {"error": "discovery failed"}

        result = gc.reconcile_host("host-disc1234")
        assert result["orphans"]["error"] == "discovery failed"
        mock_reconcile_clean.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# _extract_node_item_ids
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractNodeItemIds:
    def test_storage_node_with_library_item(self):
        node = {"type": "storageNode", "data": {"libraryItemId": "lib-1"}}
        assert gc._extract_node_item_ids(node) == ["lib-1"]

    def test_storage_node_with_pattern_disk(self):
        node = {"type": "storageNode", "data": {"patternDiskId": "pat-disk-1"}}
        assert gc._extract_node_item_ids(node) == ["pat-disk-1"]

    def test_storage_node_with_both(self):
        node = {
            "type": "storageNode",
            "data": {"libraryItemId": "lib-1", "patternDiskId": "pat-1"},
        }
        ids = gc._extract_node_item_ids(node)
        assert "lib-1" in ids
        assert "pat-1" in ids
        assert len(ids) == 2

    def test_vm_node_with_pxe_iso(self):
        node = {"type": "vmNode", "data": {"pxeBootIsoId": "iso-1"}}
        assert gc._extract_node_item_ids(node) == ["iso-1"]

    def test_vm_node_without_pxe(self):
        node = {"type": "vmNode", "data": {"vcpus": 4}}
        assert gc._extract_node_item_ids(node) == []

    def test_network_node_empty(self):
        node = {"type": "networkNode", "data": {}}
        assert gc._extract_node_item_ids(node) == []

    def test_container_node_empty(self):
        node = {"type": "containerNode", "data": {}}
        assert gc._extract_node_item_ids(node) == []

    def test_storage_node_no_ids(self):
        node = {"type": "storageNode", "data": {"size_gb": 50}}
        assert gc._extract_node_item_ids(node) == []


# ═══════════════════════════════════════════════════════════════════════════
# _collect_referenced_items
# ═══════════════════════════════════════════════════════════════════════════


class TestCollectReferencedItems:
    def test_projects_with_items(self):
        p1 = MagicMock()
        p1.deployed_topology = {
            "nodes": [
                {"type": "storageNode", "data": {"libraryItemId": "lib-1"}},
                {"type": "vmNode", "data": {"pxeBootIsoId": "iso-1"}},
            ]
        }
        p1.topology = None
        p2 = MagicMock()
        p2.deployed_topology = None
        p2.topology = {
            "nodes": [
                {"type": "storageNode", "data": {"patternDiskId": "pat-1"}},
            ]
        }
        result = gc._collect_referenced_items([p1, p2])
        assert result == {"lib-1", "iso-1", "pat-1"}

    def test_no_items(self):
        p = MagicMock()
        p.deployed_topology = {"nodes": [{"type": "networkNode", "data": {}}]}
        p.topology = None
        result = gc._collect_referenced_items([p])
        assert result == set()

    def test_empty_projects(self):
        result = gc._collect_referenced_items([])
        assert result == set()

    def test_no_topology(self):
        p = MagicMock()
        p.deployed_topology = None
        p.topology = None
        result = gc._collect_referenced_items([p])
        assert result == set()


# ═══════════════════════════════════════════════════════════════════════════
# repair_networks
# ═══════════════════════════════════════════════════════════════════════════


class TestRepairNetworks:
    def test_host_not_reachable(self):
        db = MagicMock()
        host = MagicMock()
        host.ip_address = None
        host.agent_status = "disconnected"
        result = gc.repair_networks(db, host)
        assert result["repaired"] == 0
        assert result["error"] == "Host not reachable"

    def test_no_projects(self):
        db = MagicMock()
        host = MagicMock()
        host.ip_address = "10.0.0.1"
        host.agent_status = "connected"
        db.query.return_value.filter.return_value.all.return_value = []
        result = gc.repair_networks(db, host)
        assert result["repaired"] == 0

    @patch(
        "app.services.deploy_service._setup_networks_via_troshkad", return_value=True
    )
    @patch("app.services.gc_service._get_existing_bridges", return_value=set())
    def test_missing_bridges_repaired(self, mock_bridges, mock_setup):
        db = MagicMock()
        host = MagicMock()
        host.id = "host-repair1234"
        host.ip_address = "10.0.0.1"
        host.agent_status = "connected"
        proj = MagicMock()
        proj.id = "proj-repair-1111-2222-3333-444455556666"
        proj.vni_map = {"net-1": 100}
        proj.deployed_topology = {"nodes": []}
        proj.topology = None
        db.query.return_value.filter.return_value.all.return_value = [proj]
        result = gc.repair_networks(db, host)
        assert result["repaired"] == 1
        mock_setup.assert_called_once()

    @patch("app.services.gc_service._get_existing_bridges", return_value={"br-100"})
    def test_all_bridges_present(self, mock_bridges):
        db = MagicMock()
        host = MagicMock()
        host.id = "host-ok1234567"
        host.ip_address = "10.0.0.1"
        host.agent_status = "connected"
        proj = MagicMock()
        proj.id = "proj-ok-111-2222-3333-444455556666"
        proj.vni_map = {"net-1": 100}
        proj.deployed_topology = {"nodes": []}
        proj.topology = None
        db.query.return_value.filter.return_value.all.return_value = [proj]
        result = gc.repair_networks(db, host)
        assert result["repaired"] == 0

    @patch(
        "app.services.deploy_service._setup_networks_via_troshkad",
        return_value="failed: timeout",
    )
    @patch("app.services.gc_service._get_existing_bridges", return_value=set())
    def test_setup_failure_logged(self, mock_bridges, mock_setup):
        db = MagicMock()
        host = MagicMock()
        host.id = "host-fail1234"
        host.ip_address = "10.0.0.1"
        host.agent_status = "connected"
        proj = MagicMock()
        proj.id = "proj-fail-111-2222-3333-444455556666"
        proj.vni_map = {"net-1": 200}
        proj.deployed_topology = {"nodes": []}
        proj.topology = None
        db.query.return_value.filter.return_value.all.return_value = [proj]
        result = gc.repair_networks(db, host)
        assert result["repaired"] == 0

    @patch("app.services.gc_service._get_existing_bridges", return_value=set())
    def test_project_with_no_vni_map_skipped(self, mock_bridges):
        db = MagicMock()
        host = MagicMock()
        host.id = "host-novni1234"
        host.ip_address = "10.0.0.1"
        host.agent_status = "connected"
        proj = MagicMock()
        proj.id = "proj-novni-111-2222-3333-444455556666"
        proj.vni_map = None
        proj.deployed_topology = {"nodes": []}
        proj.topology = None
        db.query.return_value.filter.return_value.all.return_value = [proj]
        result = gc.repair_networks(db, host)
        assert result["repaired"] == 0
