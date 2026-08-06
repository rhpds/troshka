"""Tests for app.services.snapshot_service — VM snapshot disk capture."""

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# _find_connected_disk_nodes
# ---------------------------------------------------------------------------


class TestFindConnectedDiskNodes:
    def test_finds_connected_disks(self):
        from app.services.snapshot_service import _find_connected_disk_nodes

        topology = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {}},
                {"id": "s1", "type": "storageNode", "data": {"size": 50}},
                {"id": "s2", "type": "storageNode", "data": {"size": 100}},
                {"id": "s3", "type": "storageNode", "data": {"size": 20}},
            ],
            "edges": [
                {"source": "vm1", "target": "s1"},
                {"source": "vm1", "target": "s2"},
                # s3 is not connected to vm1
                {"source": "vm2", "target": "s3"},
            ],
        }
        result = _find_connected_disk_nodes(topology, "vm1")
        assert len(result) == 2
        ids = [n["id"] for n in result]
        assert "s1" in ids
        assert "s2" in ids

    def test_no_connected_disks(self):
        from app.services.snapshot_service import _find_connected_disk_nodes

        topology = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {}},
                {"id": "s1", "type": "storageNode", "data": {}},
            ],
            "edges": [{"source": "vm2", "target": "s1"}],
        }
        result = _find_connected_disk_nodes(topology, "vm1")
        assert result == []

    def test_reverse_edge_direction(self):
        from app.services.snapshot_service import _find_connected_disk_nodes

        topology = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {}},
                {"id": "s1", "type": "storageNode", "data": {}},
            ],
            "edges": [
                {"source": "s1", "target": "vm1"},  # reversed direction
            ],
        }
        result = _find_connected_disk_nodes(topology, "vm1")
        assert len(result) == 1
        assert result[0]["id"] == "s1"

    def test_empty_topology(self):
        from app.services.snapshot_service import _find_connected_disk_nodes

        result = _find_connected_disk_nodes({}, "vm1")
        assert result == []

    def test_ignores_non_storage_nodes(self):
        from app.services.snapshot_service import _find_connected_disk_nodes

        topology = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {}},
                {"id": "n1", "type": "networkNode", "data": {}},
            ],
            "edges": [{"source": "vm1", "target": "n1"}],
        }
        result = _find_connected_disk_nodes(topology, "vm1")
        assert result == []


# ---------------------------------------------------------------------------
# _upload_single_disk
# ---------------------------------------------------------------------------


class TestUploadSingleDisk:
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="job-1")
    @patch(
        "app.services.s3_storage._get_s3_config",
        return_value={
            "access_key_id": "ak",
            "secret_access_key": "sk",
            "region": "us-east-1",
            "endpoint_url": "",
        },
    )
    @patch("app.services.s3_storage._bucket", return_value="test-bucket")
    def test_successful_upload(self, mock_bucket, mock_config, mock_start, mock_wait):
        from app.services.snapshot_service import _upload_single_disk

        mock_wait.return_value = {
            "status": "completed",
            "result": {"size_bytes": 5000},
        }

        host = MagicMock()
        disk_node = {"id": "disk-1", "data": {"format": "qcow2", "size": 50}}
        db = MagicMock()

        result = _upload_single_disk(host, "dom-1", 0, disk_node, "lib-item-1", db)

        assert result is True
        db.add.assert_called_once()
        db.commit.assert_called_once()
        mock_start.assert_called_once()

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="job-1")
    @patch(
        "app.services.s3_storage._get_s3_config",
        return_value={
            "access_key_id": "",
            "secret_access_key": "",
            "region": "us-east-1",
            "endpoint_url": "",
        },
    )
    @patch("app.services.s3_storage._bucket", return_value="test-bucket")
    def test_failed_job_returns_false(
        self, mock_bucket, mock_config, mock_start, mock_wait
    ):
        from app.services.snapshot_service import _upload_single_disk

        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "disk not found"},
        }

        host = MagicMock()
        disk_node = {"id": "disk-1", "data": {"format": "qcow2"}}
        db = MagicMock()

        result = _upload_single_disk(host, "dom-1", 0, disk_node, "lib-item-1", db)

        assert result is False
        db.add.assert_not_called()

    def test_iso_disk_skipped(self):
        from app.services.snapshot_service import _upload_single_disk

        host = MagicMock()
        disk_node = {"id": "disk-1", "data": {"format": "iso"}}
        db = MagicMock()

        result = _upload_single_disk(host, "dom-1", 0, disk_node, "lib-item-1", db)

        assert result is True
        db.add.assert_not_called()

    @patch("app.services.troshkad_client.start_job")
    @patch(
        "app.services.s3_storage._get_s3_config",
        return_value={
            "access_key_id": "",
            "secret_access_key": "",
            "region": "us-east-1",
            "endpoint_url": "",
        },
    )
    @patch("app.services.s3_storage._bucket", return_value="test-bucket")
    def test_troshkad_error_returns_false(self, mock_bucket, mock_config, mock_start):
        from app.services.snapshot_service import _upload_single_disk
        from app.services.troshkad_client import TroshkadError

        mock_start.side_effect = TroshkadError("connection refused")

        host = MagicMock()
        disk_node = {"id": "disk-1", "data": {"format": "qcow2"}}
        db = MagicMock()

        result = _upload_single_disk(host, "dom-1", 0, disk_node, "lib-item-1", db)

        assert result is False

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="job-1")
    @patch(
        "app.services.s3_storage._get_s3_config",
        return_value={
            "access_key_id": "",
            "secret_access_key": "",
            "region": "",
            "endpoint_url": "",
        },
    )
    @patch("app.services.s3_storage._bucket", return_value="bucket")
    def test_default_format_qcow2(
        self, mock_bucket, mock_config, mock_start, mock_wait
    ):
        from app.services.snapshot_service import _upload_single_disk

        mock_wait.return_value = {
            "status": "completed",
            "result": {"size_bytes": 3000},
        }

        host = MagicMock()
        disk_node = {"id": "disk-1", "data": {}}  # no format specified
        db = MagicMock()

        result = _upload_single_disk(host, "dom-1", 0, disk_node, "lib-1", db)
        assert result is True

        # Check the LibraryItemDisk was created with qcow2
        added_record = db.add.call_args[0][0]
        assert added_record.format == "qcow2"


# ---------------------------------------------------------------------------
# _save_snapshot_metadata
# ---------------------------------------------------------------------------


class TestSaveSnapshotMetadata:
    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._bucket", return_value="bucket")
    def test_saves_metadata_to_s3(self, mock_bucket, mock_client_fn):
        from app.services.snapshot_service import _save_snapshot_metadata

        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client

        item = MagicMock()
        item.name = "test-snap"
        item.type = "snapshot"
        item.format = "qcow2"
        item.size_bytes = 1000
        item.os_variant = "rhel9"
        item.vm_config = {"vcpus": 4}
        item.tags = ["test"]

        disk = MagicMock()
        disk.s3_key = "snapshots/123/d1.qcow2"
        disk.format = "qcow2"
        disk.size_bytes = 1000
        disk.virtual_size_bytes = 5000
        disk.boot_order = 0
        item.item_disks = [disk]

        _save_snapshot_metadata(item, "lib-123")

        mock_client.put_object.assert_called_once()
        call_kwargs = mock_client.put_object.call_args[1]
        assert call_kwargs["Key"] == "snapshots/lib-123/metadata.json"
        assert call_kwargs["ContentType"] == "application/json"

    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._bucket", return_value="bucket")
    def test_handles_s3_error_gracefully(self, mock_bucket, mock_client_fn):
        from app.services.snapshot_service import _save_snapshot_metadata

        mock_client = MagicMock()
        mock_client.put_object.side_effect = RuntimeError("S3 down")
        mock_client_fn.return_value = mock_client

        item = MagicMock()
        item.name = "test"
        item.type = "snapshot"
        item.format = "qcow2"
        item.size_bytes = 0
        item.os_variant = None
        item.vm_config = None
        item.tags = None
        item.item_disks = []

        # Should not raise
        _save_snapshot_metadata(item, "lib-123")


# ---------------------------------------------------------------------------
# capture_vm_disks (top-level function with DB session management)
# ---------------------------------------------------------------------------


class TestCaptureVmDisks:
    @patch("app.services.snapshot_service._capture_vm_disks_inner")
    @patch("app.core.database.SessionLocal")
    def test_calls_inner_and_closes_session(self, mock_session_cls, mock_inner):
        from app.services.snapshot_service import capture_vm_disks

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        capture_vm_disks("lib-1", "proj-1", "vm-1")

        mock_inner.assert_called_once_with(mock_db, "lib-1", "proj-1", "vm-1")
        mock_db.close.assert_called_once()

    @patch("app.services.snapshot_service._capture_vm_disks_inner")
    @patch("app.core.database.SessionLocal")
    def test_marks_error_on_exception(self, mock_session_cls, mock_inner):
        from app.services.snapshot_service import capture_vm_disks

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        mock_inner.side_effect = RuntimeError("boom")

        mock_item = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_item

        capture_vm_disks("lib-1", "proj-1", "vm-1")

        assert mock_item.state == "error"
        mock_db.commit.assert_called()
        mock_db.close.assert_called_once()


# ---------------------------------------------------------------------------
# _capture_vm_disks_inner
# ---------------------------------------------------------------------------


class TestCaptureVmDisksInner:
    def test_returns_early_if_item_not_found(self):
        from app.services.snapshot_service import _capture_vm_disks_inner

        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None

        # Should not raise
        _capture_vm_disks_inner(db, "lib-1", "proj-1", "vm-1")

    def test_marks_error_if_no_host(self):
        from app.services.snapshot_service import _capture_vm_disks_inner

        db = MagicMock()
        item = MagicMock()
        project = MagicMock()
        project.host_id = "h1"

        call_count = [0]
        responses = [item, project, None]

        def query_side_effect(model):
            q = MagicMock()
            idx = call_count[0]
            call_count[0] += 1
            q.filter_by.return_value.first.return_value = (
                responses[idx] if idx < len(responses) else None
            )
            return q

        db.query.side_effect = query_side_effect

        _capture_vm_disks_inner(db, "lib-1", "proj-1", "vm-1")

        assert item.state == "error"
        db.commit.assert_called()

    def test_marks_available_if_no_disks(self):
        from app.services.snapshot_service import _capture_vm_disks_inner

        db = MagicMock()
        item = MagicMock()
        project = MagicMock()
        project.host_id = "h1"
        project.deployed_topology = {
            "nodes": [{"id": "vm-1", "type": "vmNode", "data": {}}],
            "edges": [],
        }
        project.topology = None
        host = MagicMock()
        host.ip_address = "10.0.0.1"

        call_count = [0]
        responses = [item, project, host]

        def query_side_effect(model):
            q = MagicMock()
            idx = call_count[0]
            call_count[0] += 1
            q.filter_by.return_value.first.return_value = (
                responses[idx] if idx < len(responses) else None
            )
            return q

        db.query.side_effect = query_side_effect

        _capture_vm_disks_inner(db, "lib-1", "proj-1", "vm-1")

        assert item.state == "available"
        db.commit.assert_called()

    @patch("app.services.snapshot_service._save_snapshot_metadata")
    @patch("app.services.snapshot_service._upload_single_disk", return_value=True)
    def test_successful_capture(self, mock_upload, mock_save_meta):
        from app.services.snapshot_service import _capture_vm_disks_inner

        db = MagicMock()
        item = MagicMock()
        item.item_disks = []
        project = MagicMock()
        project.host_id = "h1"
        project.deployed_topology = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {}},
                {"id": "s1", "type": "storageNode", "data": {"format": "qcow2"}},
            ],
            "edges": [{"source": "vm-1", "target": "s1"}],
        }
        project.topology = None
        host = MagicMock()
        host.ip_address = "10.0.0.1"

        call_count = [0]
        responses = [item, project, host]

        def query_side_effect(model):
            q = MagicMock()
            idx = call_count[0]
            call_count[0] += 1
            q.filter_by.return_value.first.return_value = (
                responses[idx] if idx < len(responses) else None
            )
            return q

        db.query.side_effect = query_side_effect

        _capture_vm_disks_inner(db, "lib-1", "proj-1", "vm-1")

        assert item.state == "ready"
        mock_upload.assert_called_once()
        mock_save_meta.assert_called_once()

    @patch("app.services.snapshot_service._upload_single_disk", return_value=False)
    def test_marks_error_on_upload_failure(self, mock_upload):
        from app.services.snapshot_service import _capture_vm_disks_inner

        db = MagicMock()
        item = MagicMock()
        project = MagicMock()
        project.host_id = "h1"
        project.deployed_topology = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {}},
                {"id": "s1", "type": "storageNode", "data": {"format": "qcow2"}},
            ],
            "edges": [{"source": "vm-1", "target": "s1"}],
        }
        project.topology = None
        host = MagicMock()
        host.ip_address = "10.0.0.1"

        call_count = [0]
        responses = [item, project, host]

        def query_side_effect(model):
            q = MagicMock()
            idx = call_count[0]
            call_count[0] += 1
            q.filter_by.return_value.first.return_value = (
                responses[idx] if idx < len(responses) else None
            )
            return q

        db.query.side_effect = query_side_effect

        _capture_vm_disks_inner(db, "lib-1", "proj-1", "vm-1")

        assert item.state == "error"
        db.commit.assert_called()
