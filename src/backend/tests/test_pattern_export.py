"""Tests for app.services.pattern_export — tar-based pattern export/import."""

import io
import json
import tarfile
import uuid
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import TestSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_pattern(
    name="test-pat",
    description="A test pattern",
    visibility="private",
    tags=None,
    clock_target=None,
    total_size_bytes=1024,
    topology=None,
):
    p = MagicMock()
    p.name = name
    p.description = description
    p.visibility = visibility
    p.tags = tags or ["tag1"]
    p.clock_target = clock_target
    p.total_size_bytes = total_size_bytes
    p.topology = topology or {"nodes": [], "edges": []}
    return p


def _mock_disk(
    disk_id=None,
    source_disk_id="sd-1",
    source_vm_id="svm-1",
    s3_key="patterns/abc/disk1.qcow2",
    fmt="qcow2",
    size_bytes=1000,
    virtual_size_bytes=10000,
    checksum_sha256="abc123",
):
    d = MagicMock()
    d.id = disk_id or str(uuid.uuid4())
    d.source_disk_id = source_disk_id
    d.source_vm_id = source_vm_id
    d.s3_key = s3_key
    d.format = fmt
    d.size_bytes = size_bytes
    d.virtual_size_bytes = virtual_size_bytes
    d.checksum_sha256 = checksum_sha256
    return d


# ---------------------------------------------------------------------------
# _tar_end_marker
# ---------------------------------------------------------------------------


class TestTarEndMarker:
    def test_returns_1024_zero_bytes(self):
        from app.services.pattern_export import _tar_end_marker

        marker = _tar_end_marker()
        assert marker == b"\0" * 1024
        assert len(marker) == 1024


# ---------------------------------------------------------------------------
# _build_metadata
# ---------------------------------------------------------------------------


class TestBuildMetadata:
    def test_basic_metadata(self):
        from app.services.pattern_export import _build_metadata

        p = _mock_pattern()
        d1 = _mock_disk(disk_id="d1")
        result = _build_metadata(p, [d1])

        assert result["name"] == "test-pat"
        assert result["description"] == "A test pattern"
        assert result["visibility"] == "private"
        assert result["tags"] == ["tag1"]
        assert result["clock_target"] is None
        assert result["total_size_bytes"] == 1024
        assert len(result["disks"]) == 1
        assert result["disks"][0]["id"] == "d1"
        assert result["disks"][0]["format"] == "qcow2"
        assert result["disks"][0]["size_bytes"] == 1000

    def test_clock_target_included(self):
        from datetime import datetime

        from app.services.pattern_export import _build_metadata

        dt = datetime(2025, 1, 15, 0, 0, 0)
        p = _mock_pattern(clock_target=dt)
        result = _build_metadata(p, [])

        assert result["clock_target"] == str(dt)

    def test_no_disks(self):
        from app.services.pattern_export import _build_metadata

        p = _mock_pattern()
        result = _build_metadata(p, [])
        assert result["disks"] == []

    def test_multiple_disks(self):
        from app.services.pattern_export import _build_metadata

        p = _mock_pattern()
        disks = [_mock_disk(disk_id=f"d{i}") for i in range(3)]
        result = _build_metadata(p, disks)
        assert len(result["disks"]) == 3


# ---------------------------------------------------------------------------
# _manifest_entries
# ---------------------------------------------------------------------------


class TestManifestEntries:
    def test_yields_topology_metadata_and_disks(self):
        from app.services.pattern_export import _manifest_entries

        p = _mock_pattern()
        d1 = _mock_disk(disk_id="d1", size_bytes=500)
        d2 = _mock_disk(disk_id="d2", size_bytes=700, fmt="raw")

        entries = list(_manifest_entries(p, [d1, d2]))
        names = [e[0] for e in entries]

        assert names[0] == "topology.json"
        assert names[1] == "metadata.json"
        assert names[2] == "disks/d1.qcow2"
        assert names[3] == "disks/d2.raw"
        assert entries[2][1] == 500
        assert entries[3][1] == 700

    def test_disk_with_no_format_defaults_qcow2(self):
        from app.services.pattern_export import _manifest_entries

        p = _mock_pattern()
        d = _mock_disk(disk_id="d1")
        d.format = None
        entries = list(_manifest_entries(p, [d]))
        assert entries[2][0] == "disks/d1.qcow2"


# ---------------------------------------------------------------------------
# estimate_export_size
# ---------------------------------------------------------------------------


class TestEstimateExportSize:
    def test_includes_headers_data_padding_and_end_marker(self):
        from app.services.pattern_export import estimate_export_size

        p = _mock_pattern()
        d = _mock_disk(size_bytes=1000)
        size = estimate_export_size(p, [d])

        # Should include: tar headers (512 each), data, padding, and 1024 end marker
        assert size > 0
        assert size >= 1024  # at minimum the end marker

    def test_no_disks(self):
        from app.services.pattern_export import estimate_export_size

        p = _mock_pattern()
        size = estimate_export_size(p, [])
        # topology.json header+data+pad + metadata.json header+data+pad + end marker
        assert size >= 1024

    def test_size_includes_padding(self):
        from app.services.pattern_export import estimate_export_size

        p = _mock_pattern()
        # Pick a size that doesn't align to 512 to exercise padding
        d = _mock_disk(size_bytes=511)
        size = estimate_export_size(p, [d])
        assert size > 0


# ---------------------------------------------------------------------------
# _make_tar_header
# ---------------------------------------------------------------------------


class TestMakeTarHeader:
    def test_returns_512_byte_header(self):
        from app.services.pattern_export import _make_tar_header

        header = _make_tar_header("test.txt", 100)
        assert len(header) == 512

    def test_header_contains_filename(self):
        from app.services.pattern_export import _make_tar_header

        header = _make_tar_header("myfile.qcow2", 500)
        assert b"myfile.qcow2" in header

    def test_header_parseable_as_tarinfo(self):
        from app.services.pattern_export import _make_tar_header

        header = _make_tar_header("disks/abc.qcow2", 12345)
        info = tarfile.TarInfo.frombuf(header, tarfile.ENCODING, "surrogateescape")
        assert info.name == "disks/abc.qcow2"
        assert info.size == 12345
        assert info.mode == 0o644


# ---------------------------------------------------------------------------
# _yield_tar_entry
# ---------------------------------------------------------------------------


class TestYieldTarEntry:
    def test_yields_header_data_and_padding(self):
        from app.services.pattern_export import _yield_tar_entry

        data = b"Hello world"  # 11 bytes, needs padding to 512
        parts = list(_yield_tar_entry("test.txt", data))

        # Should yield: header, data, padding
        assert len(parts) == 3
        assert len(parts[0]) == 512  # header
        assert parts[1] == data
        # padding to fill 512 block: 512 - 11 = 501
        assert len(parts[2]) == 512 - 11

    def test_no_padding_when_aligned(self):
        from app.services.pattern_export import _yield_tar_entry

        data = b"x" * 512  # exactly 512 bytes, no padding needed
        parts = list(_yield_tar_entry("test.bin", data))
        assert len(parts) == 2  # header + data, no padding


# ---------------------------------------------------------------------------
# _extract_json_member
# ---------------------------------------------------------------------------


class TestExtractJsonMember:
    def test_extracts_json(self):
        from app.services.pattern_export import _extract_json_member

        payload = {"key": "value"}
        buf = io.BytesIO(json.dumps(payload).encode())
        tf = MagicMock()
        tf.extractfile.return_value = buf
        member = MagicMock()

        result = _extract_json_member(tf, member)
        assert result == payload

    def test_returns_none_when_no_file(self):
        from app.services.pattern_export import _extract_json_member

        tf = MagicMock()
        tf.extractfile.return_value = None
        member = MagicMock()

        result = _extract_json_member(tf, member)
        assert result is None


# ---------------------------------------------------------------------------
# _extract_disk_member
# ---------------------------------------------------------------------------


class TestExtractDiskMember:
    @patch("app.services.pattern_export._upload_tar_member_to_s3")
    def test_extracts_disk_id_and_format(self, mock_upload):
        from app.services.pattern_export import _extract_disk_member

        tf = MagicMock()
        tf.extractfile.return_value = io.BytesIO(b"disk data")
        member = MagicMock()
        member.name = "disks/abc-123.qcow2"
        member.size = 100

        disk_id, info = _extract_disk_member(tf, member, "pat-1")

        assert disk_id == "abc-123"
        assert info["format"] == "qcow2"
        assert info["s3_key"] == "patterns/pat-1/abc-123.qcow2"
        assert info["size_bytes"] == 100
        mock_upload.assert_called_once()

    @patch("app.services.pattern_export._upload_tar_member_to_s3")
    def test_raw_format(self, mock_upload):
        from app.services.pattern_export import _extract_disk_member

        tf = MagicMock()
        tf.extractfile.return_value = io.BytesIO(b"disk data")
        member = MagicMock()
        member.name = "disks/disk-id.raw"
        member.size = 200

        disk_id, info = _extract_disk_member(tf, member, "pat-2")
        assert disk_id == "disk-id"
        assert info["format"] == "raw"


# ---------------------------------------------------------------------------
# _update_topology_disk_refs
# ---------------------------------------------------------------------------


class TestUpdateTopologyDiskRefs:
    def test_updates_matching_storage_nodes(self):
        from app.services.pattern_export import _update_topology_disk_refs

        topo = {
            "nodes": [
                {
                    "id": "s1",
                    "type": "storageNode",
                    "data": {"patternDiskId": "old-disk-1"},
                },
                {
                    "id": "s2",
                    "type": "storageNode",
                    "data": {"patternDiskId": "other-disk"},
                },
                {"id": "v1", "type": "vmNode", "data": {}},
            ]
        }
        _update_topology_disk_refs(topo, "old-disk-1", "new-disk-1", "new-pat-1")

        assert topo["nodes"][0]["data"]["patternDiskId"] == "new-disk-1"
        assert topo["nodes"][0]["data"]["patternId"] == "new-pat-1"
        # Other nodes unchanged
        assert topo["nodes"][1]["data"]["patternDiskId"] == "other-disk"

    def test_no_storage_nodes(self):
        from app.services.pattern_export import _update_topology_disk_refs

        topo = {"nodes": [{"id": "v1", "type": "vmNode", "data": {}}]}
        _update_topology_disk_refs(topo, "old", "new", "pat")
        assert topo["nodes"][0]["type"] == "vmNode"

    def test_empty_topology(self):
        from app.services.pattern_export import _update_topology_disk_refs

        topo = {}
        _update_topology_disk_refs(topo, "old", "new", "pat")
        # No error


# ---------------------------------------------------------------------------
# _apply_pattern_metadata
# ---------------------------------------------------------------------------


class TestApplyPatternMetadata:
    def test_applies_name_from_parameter(self):
        from app.services.pattern_export import _apply_pattern_metadata

        pattern = MagicMock()
        metadata = {"name": "meta-name", "description": "desc", "tags": ["t1"]}
        _apply_pattern_metadata(pattern, metadata, "override-name")
        assert pattern.name == "override-name"

    def test_applies_name_from_metadata_when_no_name(self):
        from app.services.pattern_export import _apply_pattern_metadata

        pattern = MagicMock()
        metadata = {"name": "meta-name", "description": "desc"}
        _apply_pattern_metadata(pattern, metadata, None)
        assert pattern.name == "meta-name"

    def test_none_metadata(self):
        from app.services.pattern_export import _apply_pattern_metadata

        pattern = MagicMock()
        _apply_pattern_metadata(pattern, None, "forced-name")
        assert pattern.name == "forced-name"
        assert pattern.description is None
        assert pattern.tags is None

    def test_visibility_always_private(self):
        from app.services.pattern_export import _apply_pattern_metadata

        pattern = MagicMock()
        metadata = {"name": "n", "visibility": "public"}
        _apply_pattern_metadata(pattern, metadata, None)
        assert pattern.visibility == "private"

    def test_clock_target_parsed(self):
        from app.services.pattern_export import _apply_pattern_metadata

        pattern = MagicMock()
        metadata = {"name": "n", "clock_target": "2025-01-15T00:00:00Z"}
        _apply_pattern_metadata(pattern, metadata, None)
        assert pattern.clock_target is not None

    def test_invalid_clock_target_ignored(self):
        from app.services.pattern_export import _apply_pattern_metadata

        pattern = MagicMock()
        metadata = {"name": "n", "clock_target": "not-a-date"}
        _apply_pattern_metadata(pattern, metadata, None)
        # Should not raise

    def test_no_clock_target_in_metadata(self):
        from app.services.pattern_export import _apply_pattern_metadata

        pattern = MagicMock()
        metadata = {"name": "n"}
        _apply_pattern_metadata(pattern, metadata, None)
        # clock_target should not be set (no assignment expected)


# ---------------------------------------------------------------------------
# _parse_tar_entries
# ---------------------------------------------------------------------------


class TestParseTarEntries:
    @patch("app.services.pattern_export._upload_tar_member_to_s3")
    def test_parses_all_entry_types(self, mock_upload):
        from app.services.pattern_export import _parse_tar_entries

        topo = {"nodes": [], "edges": []}
        meta = {"name": "test", "disks": []}

        member_topo = MagicMock()
        member_topo.name = "topology.json"
        member_topo.size = 100

        member_meta = MagicMock()
        member_meta.name = "metadata.json"
        member_meta.size = 50

        member_disk = MagicMock()
        member_disk.name = "disks/d1.qcow2"
        member_disk.size = 500

        tf = MagicMock()
        tf.__iter__ = MagicMock(
            return_value=iter([member_topo, member_meta, member_disk])
        )

        def extract_side_effect(member):
            if member.name == "topology.json":
                return io.BytesIO(json.dumps(topo).encode())
            elif member.name == "metadata.json":
                return io.BytesIO(json.dumps(meta).encode())
            elif member.name.startswith("disks/"):
                return io.BytesIO(b"fake disk data")
            return None

        tf.extractfile = extract_side_effect

        topology, metadata, disk_map = _parse_tar_entries(tf, "pat-1")

        assert topology == topo
        assert metadata == meta
        assert "d1" in disk_map
        assert disk_map["d1"]["format"] == "qcow2"

    @patch("app.services.pattern_export._upload_tar_member_to_s3")
    def test_skips_zero_size_disk_entries(self, mock_upload):
        from app.services.pattern_export import _parse_tar_entries

        member_disk = MagicMock()
        member_disk.name = "disks/d1.qcow2"
        member_disk.size = 0  # zero-size, should be skipped

        tf = MagicMock()
        tf.__iter__ = MagicMock(return_value=iter([member_disk]))

        topology, metadata, disk_map = _parse_tar_entries(tf, "pat-1")
        assert topology is None
        assert metadata is None
        assert len(disk_map) == 0


# ---------------------------------------------------------------------------
# _mark_pattern_error
# ---------------------------------------------------------------------------


class TestMarkPatternError:
    def test_marks_pattern_as_error(self):
        from app.models.pattern import Pattern
        from app.models.user import User
        from app.services.pattern_export import _mark_pattern_error

        db = TestSession()
        try:
            user = User(email="markpat@test.com", display_name="Mark Test", role="user")
            db.add(user)
            db.commit()
            db.refresh(user)

            pat = Pattern(
                name="error-test",
                owner_id=user.id,
                topology={"nodes": [], "edges": []},
                state="creating",
            )
            db.add(pat)
            db.commit()
            db.refresh(pat)

            _mark_pattern_error(db, pat.id)

            db.refresh(pat)
            assert pat.state == "error"
        finally:
            db.close()

    def test_nonexistent_pattern_no_error(self):
        from app.services.pattern_export import _mark_pattern_error

        db = TestSession()
        try:
            _mark_pattern_error(db, "nonexistent-id")
            # Should not raise
        finally:
            db.close()


# ---------------------------------------------------------------------------
# stream_pattern_export
# ---------------------------------------------------------------------------


class TestStreamPatternExport:
    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._bucket", return_value="test-bucket")
    def test_returns_none_for_missing_pattern(self, mock_bucket, mock_client):
        from app.services.pattern_export import stream_pattern_export

        db = TestSession()
        try:
            gen = stream_pattern_export("nonexistent-id", db)
            # Generator should return immediately (yield nothing)
            assert gen is None or list(gen) == []
        finally:
            db.close()

    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._bucket", return_value="test-bucket")
    def test_streams_pattern_with_disks(self, mock_bucket, mock_s3_client):
        from app.models.pattern import Pattern, PatternDisk
        from app.models.user import User
        from app.services.pattern_export import stream_pattern_export

        db = TestSession()
        try:
            user = User(
                email="stream-export@test.com",
                display_name="Stream Test",
                role="user",
            )
            db.add(user)
            db.commit()
            db.refresh(user)

            pat = Pattern(
                name="stream-test",
                owner_id=user.id,
                topology={"nodes": [], "edges": []},
                state="available",
                total_size_bytes=100,
            )
            db.add(pat)
            db.commit()
            db.refresh(pat)

            disk = PatternDisk(
                pattern_id=pat.id,
                source_disk_id="sd1",
                source_vm_id="svm1",
                s3_key="patterns/abc/disk1.qcow2",
                format="qcow2",
                size_bytes=512,
                virtual_size_bytes=1024,
                state="available",
            )
            db.add(disk)
            db.commit()

            # Mock S3 client
            mock_body = MagicMock()
            mock_body.read.side_effect = [b"x" * 512, b""]
            mock_client_obj = MagicMock()
            mock_client_obj.get_object.return_value = {"Body": mock_body}
            mock_s3_client.return_value = mock_client_obj

            chunks = list(stream_pattern_export(pat.id, db))
            assert len(chunks) > 0

            # Last chunk should be the end marker
            assert chunks[-1] == b"\0" * 1024
        finally:
            db.close()


# ---------------------------------------------------------------------------
# _upload_tar_member_to_s3
# ---------------------------------------------------------------------------


class TestUploadTarMemberToS3:
    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._bucket", return_value="bucket")
    def test_small_file_uses_upload_fileobj(self, mock_bucket, mock_client_fn):
        from app.services.pattern_export import _upload_tar_member_to_s3

        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client

        fileobj = io.BytesIO(b"small data")
        _upload_tar_member_to_s3(fileobj, "key", 10)

        mock_client.upload_fileobj.assert_called_once()

    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._bucket", return_value="bucket")
    def test_large_file_uses_multipart(self, mock_bucket, mock_client_fn):
        from app.services.pattern_export import _upload_tar_member_to_s3

        mock_client = MagicMock()
        mock_client.create_multipart_upload.return_value = {"UploadId": "up-1"}
        mock_client.upload_part.return_value = {"ETag": "etag-1"}
        mock_client_fn.return_value = mock_client

        # 101 MiB -- exceeds 100 MiB threshold
        large_size = 101 * 1024 * 1024
        data = b"x" * (64 * 1024 * 1024)  # one 64 MiB chunk
        fileobj = MagicMock()
        fileobj.read.side_effect = [data, b""]

        _upload_tar_member_to_s3(fileobj, "key", large_size)

        mock_client.create_multipart_upload.assert_called_once()
        mock_client.complete_multipart_upload.assert_called_once()

    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._bucket", return_value="bucket")
    def test_multipart_upload_aborts_on_error(self, mock_bucket, mock_client_fn):
        from app.services.pattern_export import _upload_tar_member_to_s3

        mock_client = MagicMock()
        mock_client.create_multipart_upload.return_value = {"UploadId": "up-1"}
        mock_client.upload_part.side_effect = RuntimeError("S3 failure")
        mock_client_fn.return_value = mock_client

        large_size = 101 * 1024 * 1024
        fileobj = MagicMock()
        fileobj.read.return_value = b"x" * 1024

        with pytest.raises(RuntimeError):
            _upload_tar_member_to_s3(fileobj, "key", large_size)

        mock_client.abort_multipart_upload.assert_called_once()


# ---------------------------------------------------------------------------
# _create_pattern_disks
# ---------------------------------------------------------------------------


class TestCreatePatternDisks:
    def test_creates_disk_records_and_returns_total_size(self):
        from app.models.pattern import Pattern
        from app.models.user import User
        from app.services.pattern_export import _create_pattern_disks

        db = TestSession()
        try:
            user = User(
                email="create-disks@test.com",
                display_name="Create Disks",
                role="user",
            )
            db.add(user)
            db.commit()
            db.refresh(user)

            pat = Pattern(
                name="disks-test",
                owner_id=user.id,
                topology={"nodes": [], "edges": []},
            )
            db.add(pat)
            db.commit()
            db.refresh(pat)

            disk_map = {
                "old-d1": {
                    "s3_key": "patterns/p/d1.qcow2",
                    "format": "qcow2",
                    "size_bytes": 500,
                },
                "old-d2": {
                    "s3_key": "patterns/p/d2.qcow2",
                    "format": "qcow2",
                    "size_bytes": 700,
                },
            }
            metadata = {
                "disks": [
                    {
                        "id": "old-d1",
                        "source_disk_id": "src-1",
                        "source_vm_id": "vm-1",
                        "virtual_size_bytes": 10000,
                        "checksum_sha256": "sha1",
                    }
                ]
            }
            new_topo = {"nodes": [], "edges": []}

            total = _create_pattern_disks(db, pat.id, disk_map, metadata, new_topo)
            db.commit()

            assert total == 1200
        finally:
            db.close()

    def test_no_metadata(self):
        from app.models.pattern import Pattern
        from app.models.user import User
        from app.services.pattern_export import _create_pattern_disks

        db = TestSession()
        try:
            user = User(email="no-meta@test.com", display_name="No Meta", role="user")
            db.add(user)
            db.commit()
            db.refresh(user)

            pat = Pattern(
                name="no-meta-test",
                owner_id=user.id,
                topology={"nodes": [], "edges": []},
            )
            db.add(pat)
            db.commit()
            db.refresh(pat)

            disk_map = {
                "d1": {
                    "s3_key": "patterns/p/d1.qcow2",
                    "format": "qcow2",
                    "size_bytes": 300,
                }
            }
            new_topo = {"nodes": [], "edges": []}

            total = _create_pattern_disks(db, pat.id, disk_map, None, new_topo)
            db.commit()
            assert total == 300
        finally:
            db.close()
