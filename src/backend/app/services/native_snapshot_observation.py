"""Independent complete metadata reads for retained snapshot allocation graphs."""

from datetime import UTC, datetime
from functools import partial
from typing import Any, cast

from app.schemas.kubevirt_storage import BackendImageState, ImageKey
from app.services.kubevirt_storage_collector import (
    _call,
    _children,
    _parent,
    _snapshot_rows,
)
from app.services.native_snapshot_custody import _api, _find, require


def absent_image(backend, key):
    started = datetime.now(UTC)
    value = _call(partial(backend.image_inventory, key, max_objects=10000)).value
    state = BackendImageState.model_validate(value)
    require(
        started <= state.collected_at <= datetime.now(UTC),
        "Snapshot image absence stale",
    )
    require(
        state.model_dump(include=set(ImageKey.model_fields)) == key.model_dump()
        and state.excludes(key),
        "Snapshot image remains active or in trash",
    )
    return state


def _absent_restores(facts, kube, backend):
    absent = set()
    api = _api(kube)
    names = {p.id: p.name for p in facts.producers}
    for proof in facts.snapshot_restores:
        binding = proof.binding
        live = _find(
            api,
            "PersistentVolumeClaim",
            binding.root.namespace,
            names[proof.producer_id],
        )
        if live is not None:
            require(
                live["metadata"]["uid"] == binding.pvc_uid,
                "Snapshot restore original PVC changed",
            )
            continue
        absent_image(backend, binding.image_key)
        absent.add(binding.pvc_uid)
    return absent


def _metadata(backend, key, pools):
    info = cast(dict[str, Any], _call(backend.image_info, key).value)
    require(info.get("id") == key.image_id, "Snapshot graph image identity changed")
    parent = _parent(info, key, pools, backend)
    snapshots, _ = _snapshot_rows(_call(backend.image_snapshots, key).value, key)
    children = _children(backend, key, snapshots, pools, [], 10000)
    require(
        len(snapshots) == len(set(snapshots)) and len(children) == len(set(children)),
        "Snapshot graph duplicate identity",
    )
    return parent, set(snapshots), set(children)


def verify_graph(facts, kube, backend):
    from app.services.native_snapshot_graph import _expected

    absent = _absent_restores(facts, kube, backend)
    parents, _, snapshots, children, valid = _expected([facts], absent)
    require(valid, "Snapshot graph conflicting parent authority")
    pools = _call(backend.pool_identity).value
    for key, parent in parents.items():
        expected = parent, snapshots[key], children[key]
        require(
            _metadata(backend, key, pools) == expected,
            "Snapshot graph unresolved snapshot or child edge",
        )
        require(
            _metadata(backend, key, pools) == expected,
            "Snapshot graph changed during verification",
        )
