"""
Pattern export/import service.

Export streams a tar archive containing topology, metadata, and disk images
directly from S3 — no temp files. Import accepts a tar upload, creates
Pattern + PatternDisk records, and streams disk files to local S3.
"""

import json
import logging
import tarfile

logger = logging.getLogger(__name__)

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB


def _tar_end_marker():
    return b"\0" * 1024


def estimate_export_size(pattern, disks) -> int:
    """Precompute tar archive size for Content-Length header."""
    size = 0
    for name, data_size in _manifest_entries(pattern, disks):
        size += 512  # tar header
        size += data_size
        if data_size % 512:
            size += 512 - (data_size % 512)  # padding
    size += 1024  # end-of-archive marker
    return size


def _manifest_entries(pattern, disks):
    """Yield (name, size) for each entry in the tar archive."""
    topo_bytes = json.dumps(pattern.topology, indent=2).encode()
    yield ("topology.json", len(topo_bytes))

    meta = _build_metadata(pattern, disks)
    meta_bytes = json.dumps(meta, indent=2).encode()
    yield ("metadata.json", len(meta_bytes))

    for disk in disks:
        ext = disk.format or "qcow2"
        yield (f"disks/{disk.id}.{ext}", disk.size_bytes)


def _build_metadata(pattern, disks):
    return {
        "name": pattern.name,
        "description": pattern.description,
        "visibility": pattern.visibility,
        "tags": pattern.tags,
        "clock_target": str(pattern.clock_target) if pattern.clock_target else None,
        "total_size_bytes": pattern.total_size_bytes,
        "disks": [
            {
                "id": d.id,
                "source_disk_id": d.source_disk_id,
                "source_vm_id": d.source_vm_id,
                "s3_key": d.s3_key,
                "format": d.format,
                "size_bytes": d.size_bytes,
                "virtual_size_bytes": d.virtual_size_bytes,
                "checksum_sha256": d.checksum_sha256,
            }
            for d in disks
        ],
    }


def stream_pattern_export(pattern_id: str, db):
    """Stream a tar archive of the pattern's topology + disk images from S3."""
    from app.models.pattern import Pattern, PatternDisk
    from app.services import s3_storage

    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        return
    disks = (
        db.query(PatternDisk).filter_by(pattern_id=pattern_id, state="available").all()
    )

    client = s3_storage._get_s3_client()
    bucket = s3_storage._bucket()

    topo_bytes = json.dumps(pattern.topology, indent=2).encode()
    yield from _yield_tar_entry("topology.json", topo_bytes)

    meta = _build_metadata(pattern, disks)
    meta_bytes = json.dumps(meta, indent=2).encode()
    yield from _yield_tar_entry("metadata.json", meta_bytes)

    for disk in disks:
        ext = disk.format or "qcow2"
        name = f"disks/{disk.id}.{ext}"
        header = _make_tar_header(name, disk.size_bytes)
        yield header

        response = client.get_object(Bucket=bucket, Key=disk.s3_key)
        body = response["Body"]
        remaining = disk.size_bytes
        while remaining > 0:
            chunk = body.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            yield chunk
            remaining -= len(chunk)

        pad = disk.size_bytes % 512
        if pad:
            yield b"\0" * (512 - pad)

    yield _tar_end_marker()


def _yield_tar_entry(name: str, data: bytes):
    """Yield tar header + data + padding for an in-memory entry."""
    yield _make_tar_header(name, len(data))
    yield data
    pad = len(data) % 512
    if pad:
        yield b"\0" * (512 - pad)


def _make_tar_header(name: str, size: int) -> bytes:
    """Build a POSIX ustar tar header."""
    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mtime = 0
    info.mode = 0o644
    info.type = tarfile.REGTYPE
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    return info.tobuf(tarfile.GNU_FORMAT)


def import_pattern_from_tar(
    pattern_id: str, tar_stream, owner_id: str, name: str | None
):
    """Import a pattern from a tar archive. Runs in background thread."""
    from app.core.database import SessionLocal
    from app.models.pattern import Pattern, PatternDisk

    db = SessionLocal()
    try:
        pattern = db.query(Pattern).filter_by(id=pattern_id).first()
        if not pattern:
            return

        tf = tarfile.open(fileobj=tar_stream, mode="r|")
        topology = None
        metadata = None
        disk_map = {}

        for member in tf:
            if member.name == "topology.json":
                topology = json.loads(tf.extractfile(member).read())
            elif member.name == "metadata.json":
                metadata = json.loads(tf.extractfile(member).read())
            elif member.name.startswith("disks/") and member.size > 0:
                disk_id = member.name.split("/")[-1].rsplit(".", 1)[0]
                fmt = member.name.rsplit(".", 1)[-1] if "." in member.name else "qcow2"
                s3_key = f"patterns/{pattern_id}/{disk_id}.{fmt}"

                _upload_tar_member_to_s3(tf.extractfile(member), s3_key, member.size)

                disk_map[disk_id] = {
                    "s3_key": s3_key,
                    "format": fmt,
                    "size_bytes": member.size,
                }

        if not topology:
            pattern.state = "error"
            db.commit()
            logger.error("Import %s: no topology.json in archive", pattern_id)
            return

        from app.api.patterns import _remap_topology

        new_topo = _remap_topology(topology)
        pattern.topology = new_topo
        if name:
            pattern.name = name
        elif metadata and metadata.get("name"):
            pattern.name = metadata["name"]
        pattern.description = metadata.get("description") if metadata else None
        pattern.tags = metadata.get("tags") if metadata else None
        pattern.visibility = "private"

        if metadata and metadata.get("clock_target"):
            from dateutil.parser import parse as parse_dt

            try:
                pattern.clock_target = parse_dt(metadata["clock_target"])
            except Exception:
                pass

        total_size = 0
        meta_disks = {d["id"]: d for d in (metadata or {}).get("disks", [])}

        for old_id, info in disk_map.items():
            md = meta_disks.get(old_id, {})
            pd = PatternDisk(
                pattern_id=pattern_id,
                source_disk_id=md.get("source_disk_id", old_id),
                source_vm_id=md.get("source_vm_id", ""),
                s3_key=info["s3_key"],
                format=info["format"],
                size_bytes=info["size_bytes"],
                virtual_size_bytes=md.get("virtual_size_bytes", 0),
                checksum_sha256=md.get("checksum_sha256"),
                state="available",
            )
            db.add(pd)
            total_size += info["size_bytes"]

            _update_topology_disk_refs(new_topo, old_id, pd.id, pattern_id)

        pattern.total_size_bytes = total_size
        pattern.state = "available"
        db.commit()
        logger.info(
            "Pattern %s imported: %d disks, %d bytes",
            pattern_id,
            len(disk_map),
            total_size,
        )

    except Exception:
        logger.exception("Pattern import %s failed", pattern_id)
        try:
            pattern = db.query(Pattern).filter_by(id=pattern_id).first()
            if pattern:
                pattern.state = "error"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


def _upload_tar_member_to_s3(fileobj, s3_key: str, size: int):
    """Stream a tar member to S3 via multipart upload."""
    from app.services import s3_storage

    client = s3_storage._get_s3_client()
    bucket = s3_storage._bucket()

    if size < 100 * 1024 * 1024:
        client.upload_fileobj(fileobj, bucket, s3_key)
        return

    mpu = client.create_multipart_upload(Bucket=bucket, Key=s3_key)
    upload_id = mpu["UploadId"]
    parts = []
    part_num = 1
    try:
        while True:
            chunk = fileobj.read(64 * 1024 * 1024)  # 64 MiB parts
            if not chunk:
                break
            resp = client.upload_part(
                Bucket=bucket,
                Key=s3_key,
                UploadId=upload_id,
                PartNumber=part_num,
                Body=chunk,
            )
            parts.append({"PartNumber": part_num, "ETag": resp["ETag"]})
            part_num += 1

        client.complete_multipart_upload(
            Bucket=bucket,
            Key=s3_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        client.abort_multipart_upload(Bucket=bucket, Key=s3_key, UploadId=upload_id)
        raise


def _update_topology_disk_refs(
    topology: dict, old_disk_id: str, new_disk_id: str, pattern_id: str
):
    """Update storage nodes in topology to reference the new pattern disk."""
    for node in topology.get("nodes", []):
        if node.get("type") != "storageNode":
            continue
        data = node.get("data", {})
        if data.get("patternDiskId") == old_disk_id:
            data["patternDiskId"] = new_disk_id
            data["patternId"] = pattern_id
