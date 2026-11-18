"""Bounded snapshot reads for explicitly pinned RBD journal/build pairs.

The tested profile restricts journal and image pool to the same configured pool,
default csi-snap image names and the sidecar's full snapshot UID request name.
Configuration is an explicit layout selection, never live-version discovery.
Accepting a profile does not qualify its installation or enable collection.
"""

from uuid import UUID

from app.schemas.kubevirt_storage import ImageKey, ReadResult
from app.schemas.native_snapshot_custody import (
    SnapshotBackendIdentity,
    SnapshotJournalProfile,
)
from app.services.kubevirt_storage_transport import _token, decode_csi_handle, utcnow


def _require(condition):
    if not condition:
        raise ValueError("Snapshot journal identity changed or unsupported")


def _value(reader, operation, **kwargs):
    result = reader._read(operation, False, **kwargs)
    _require(result.status == "complete")
    return _token(result.value)


def _journal(reader, pool, profile, expected_request, object_uuid):
    args = dict(pool=pool.name, namespace=pool.rbd_namespace)
    resolved = _value(
        reader,
        "snapshot_lookup",
        instance_id=profile.instance_id,
        request_name=expected_request,
        **args,
    )
    _require(str(UUID(resolved)) == object_uuid)
    return {
        field: _value(
            reader,
            "snapshot_journal",
            object_uuid=object_uuid,
            journal_key=field,
            **args,
        )
        for field in ("csi.imageid", "csi.imagename", "csi.snapname", "csi.source")
    }


def _source(reader, profile, source, source_handle, source_request):
    result = reader.resolve_csi(
        profile.driver, source_handle, expected_request=source_request
    )
    _require(
        result.status == "complete" and ImageKey.model_validate(result.value) == source
    )
    info = reader.image_info(source)
    _require(info.status == "complete" and info.value.get("id") == source.image_id)
    return _token(info.value["name"])


def _backend_snapshot(reader, pool, object_uuid, values):
    from app.services.kubevirt_storage_collector import _snapshot_rows

    key = ImageKey(
        ceph_fsid=reader.ceph_fsid,
        pool_id=pool.pool_id,
        rbd_namespace=pool.rbd_namespace,
        image_id=values["csi.imageid"],
    )
    info = reader._read(
        "info",
        pool=pool.name,
        namespace=pool.rbd_namespace,
        image=values["csi.imagename"],
    )
    _require(
        info.status == "complete"
        and info.value.get("id") == key.image_id
        and info.value.get("name") == "csi-snap-" + object_uuid
    )
    reader.names[key] = values["csi.imagename"]
    snapshots = reader.image_snapshots(key)
    _require(snapshots.status == "complete" and isinstance(snapshots.value, list))
    identities, _ = _snapshot_rows(snapshots.value, key)
    matches = [
        identity
        for identity, raw in zip(identities, snapshots.value)
        if raw.get("name") == "csi-snap-" + object_uuid
        and raw["namespace"]["type"] == "user"
    ]
    _require(len(matches) == 1)
    return key, matches[0]


def resolve(
    reader, profile, handle, *, expected_request, source, source_handle, source_request
):
    try:
        profile = SnapshotJournalProfile.model_validate(profile)
        cluster, pool_id, object_uuid = decode_csi_handle(handle)
        pool = reader._csi_pool(profile.driver, cluster, pool_id, expected_request, {})
        _require(
            pool.journal_pool in (None, pool.name) and reader._pool(source) == pool
        )
        _require(reader._verify_binding_target().status == "complete")
        source_name = _source(reader, profile, source, source_handle, source_request)
        values = _journal(reader, pool, profile, expected_request, object_uuid)
        _require(
            values["csi.snapname"] == expected_request
            and values["csi.source"] == source_name
        )
        key, snapshot = _backend_snapshot(reader, pool, object_uuid, values)
        _require(key != source and values["csi.imagename"] == "csi-snap-" + object_uuid)
        _require(
            values == _journal(reader, pool, profile, expected_request, object_uuid)
        )
        _require(
            source_name
            == _source(reader, profile, source, source_handle, source_request)
        )
        return ReadResult(
            status="complete",
            collected_at=utcnow(),
            value=SnapshotBackendIdentity(image=key, snapshot=snapshot, handle=handle),
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        return ReadResult(status="unsupported", collected_at=utcnow())
