"""Exact read-only absence of previously resolved snapshot allocation debt."""

from datetime import UTC, datetime
from typing import Any, cast

from app.schemas.native_snapshot_custody import SnapshotRetirement
from app.services.kubevirt_storage_collector import _call, _snapshot_rows
from app.services.kubevirt_storage_enrollment import digest
from app.services.native_snapshot_custody import (
    _api,
    _find,
    _source,
    require,
    retired_ids,
)


def _objects(prepared, proof, kube):
    api = _api(kube)
    kinds = ("VolumeSnapshot", "PersistentVolumeClaim", "Pod", "Job")
    rows = {kind: api.rows(kind, prepared.root.namespace) for kind in kinds}
    rows["VolumeSnapshotContent"] = api.rows("VolumeSnapshotContent", None)
    require(
        all(isinstance(v, list) and len(v) <= 10000 for v in rows.values()),
        "Snapshot inventory incomplete",
    )
    require(
        not any(
            v["metadata"].get("name") == proof.snapshot_name
            or v["metadata"].get("uid") == proof.snapshot_uid
            for v in rows["VolumeSnapshot"]
        ),
        "Snapshot original object remains or was replaced",
    )
    require(
        not any(
            v["metadata"].get("name") == proof.content_name
            or v["metadata"].get("uid") == proof.content_uid
            or v.get("status", {}).get("snapshotHandle") == proof.handle
            or v.get("spec", {}).get("volumeSnapshotRef", {}).get("uid")
            == proof.snapshot_uid
            for v in rows["VolumeSnapshotContent"]
        ),
        "Snapshot original content remains",
    )
    operation = next(
        p.operation_digest for p in prepared.producers if p.id == proof.producer_id
    )
    helpers = [
        p
        for p in prepared.producers
        if p.operation_digest == operation and p.kind != "VolumeSnapshot"
    ]
    require(
        not any(
            v["metadata"].get("name") == p.name or v["metadata"].get("uid") == p.uid
            for p in helpers
            for v in rows[p.kind]
        ),
        "Snapshot operation helper remains",
    )
    return digest(
        {
            k: sorted(
                [
                    dict(metadata=v["metadata"], specDigest=digest(v.get("spec", {})))
                    for v in values
                ],
                key=digest,
            )
            for k, values in rows.items()
        }
    )


def _source_survives(prepared, proof, kube, backend):
    from app.services.kubevirt_storage_collector import _parent

    intent = next(p for p in prepared.producers if p.id == proof.producer_id)
    source, _ = _source(prepared, intent, [proof.source], kube)
    info = cast(dict[str, Any], _call(backend.image_info, source.image_key).value)
    require(
        info.get("id") == source.image_key.image_id, "Snapshot source image changed"
    )
    pools = _call(backend.pool_identity).value
    require(
        str(_call(backend.cluster_identity).value).strip()
        == source.image_key.ceph_fsid,
        "Snapshot backend cluster changed",
    )
    require(
        _parent(info, source.image_key, pools, backend) == proof.source_parent,
        "Snapshot source ancestry changed",
    )
    snapshots, _ = _snapshot_rows(
        _call(backend.image_snapshots, source.image_key).value, source.image_key
    )
    require(
        proof.parent is None or proof.parent not in snapshots,
        "Snapshot source dependency remains",
    )


def observe(prepared, facts, kube, backend):
    from app.services.native_snapshot_observation import absent_image

    retired = list(facts.snapshot_retirements)
    for proof in facts.snapshots:
        if proof.producer_id in retired_ids(facts):
            continue
        if (
            _find(
                _api(kube),
                "VolumeSnapshot",
                prepared.root.namespace,
                proof.snapshot_name,
            )
            is not None
        ):
            continue
        before = _objects(prepared, proof, kube)
        _source_survives(prepared, proof, kube, backend)
        absence = absent_image(backend, proof.image)
        _source_survives(prepared, proof, kube, backend)
        after = _objects(prepared, proof, kube)
        retired.append(
            SnapshotRetirement(
                producer_id=proof.producer_id,
                image_absence=absence,
                kubernetes_digest=before,
                kubernetes_final_digest=after,
                verified_at=datetime.now(UTC),
            )
        )
    return facts.model_copy(update={"snapshot_retirements": retired})
