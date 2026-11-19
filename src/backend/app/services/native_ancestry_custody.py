"""Read-only current ancestry authority retained by administrator custody.

This module never invents original creation, admission, or cache birth history.
The normal graph checks remain responsible for current mutable edge validation.
"""

from collections import defaultdict
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from app.schemas.kubevirt_storage import CollectionLimits
from app.schemas.native_online_enrollment import CurrentAncestryCustody
from app.services.kubevirt_storage_collector import (
    _call,
    _graph_metadata,
    _source_metadata,
)
from app.services.kubevirt_storage_enrollment import root_key

if TYPE_CHECKING:
    from app.services.native_custody_reconciliation import EffectiveCustody
    from app.services.native_online_enrollment import RetainedCustody


def verify_csi_intermediate(backend, binding, intermediate, pv):
    """Joint current journal, parent-ID, and CSI snapshot-name verification."""
    from app.schemas.kubevirt_storage import ImageKey
    from app.services.kubevirt_storage_collector import _parent
    from app.services.kubevirt_storage_transport import decode_csi_handle

    csi = pv["spec"]["csi"]
    _require(
        pv["metadata"]["uid"] == binding.pv_uid
        and csi["driver"] == binding.csi_driver
        and csi["volumeHandle"] == binding.volume_handle
        and pv["metadata"].get("annotations", {}).get("pv.kubernetes.io/provisioned-by")
        == binding.csi_driver,
        "Custody dynamic CSI identity changed",
    )
    resolve = partial(
        backend.resolve_csi,
        binding.csi_driver,
        binding.volume_handle,
        expected_request=pv["metadata"]["name"],
        volume_attributes=csi.get("volumeAttributes", {}),
    )
    _require(
        ImageKey.model_validate(_call(resolve).value) == binding.image_key,
        "Custody CSI journal head changed",
    )
    _, _, object_uuid = decode_csi_handle(binding.volume_handle)
    expected_head = "csi-vol-" + object_uuid
    head = _call(backend.image_info, binding.image_key).value
    middle = _call(backend.image_info, intermediate).value
    pools = _call(backend.pool_identity).value
    if not isinstance(head, dict) or not isinstance(middle, dict):
        raise ValueError("Custody CSI image metadata unavailable")
    head_parent = _parent(head, binding.image_key, pools, backend)
    source_parent = _parent(middle, intermediate, pools, backend)
    if head_parent is None or source_parent is None:
        raise ValueError("Custody CSI temporary image identity changed")
    _require(
        head.get("id") == binding.image_key.image_id
        and head.get("name") == expected_head
        and head_parent.image_key == intermediate
        and middle.get("id") == intermediate.image_id
        and middle.get("name") == expected_head + "-temp",
        "Custody CSI temporary image identity changed",
    )
    _snapshot_name(backend, intermediate, head_parent, expected_head)
    _snapshot_name(
        backend, source_parent.image_key, source_parent, expected_head + "-temp"
    )
    _require(
        ImageKey.model_validate(_call(resolve).value) == binding.image_key,
        "Custody CSI journal changed during verification",
    )


def _snapshot_name(backend, image, snapshot, expected_name):
    from app.services.kubevirt_storage_collector import _snapshot_rows

    raw = _call(backend.image_snapshots, image).value
    if not isinstance(raw, list):
        raise ValueError("Custody CSI snapshot metadata unavailable")
    identities, _ = _snapshot_rows(raw, image)
    matches = [r for key, r in zip(identities, raw) if key == snapshot]
    _require(len(matches) == 1, "Custody CSI snapshot identity changed")
    row = matches[0]
    namespace = row["namespace"]
    name = (
        namespace.get("original_name") if namespace["type"] == "trash" else row["name"]
    )
    _require(name == expected_name, "Custody CSI snapshot request changed")


def _require(value, message):
    if not value:
        raise ValueError(message)


def _metadata(backend, key, pools):
    result = _graph_metadata(
        backend, SimpleNamespace(key=key), pools, CollectionLimits()
    )
    _require(result[0] == key.image_id, "Custody image replaced")
    return result


def _pv(kube, binding):
    rows = [p for p in kube.pvs() if p["metadata"]["uid"] == binding.pv_uid]
    _require(len(rows) == 1, "Custody PV identity missing or ambiguous")
    pv = rows[0]
    claim = pv.get("spec", {}).get("claimRef", {})
    _require(
        claim.get("uid") == binding.pvc_uid
        and claim.get("namespace") == binding.root.namespace
        and not pv["metadata"].get("deletionTimestamp"),
        "Custody PV claim changed",
    )
    return pv


def _sources(kube, pins):
    from app.services.kubevirt_storage_inventory import _cache_pvcs

    pvs = {p["metadata"]["name"]: p for p in kube.pvs()}
    result = []
    for root in pins.cache_roots:
        _require(
            kube.cluster_identity() == root.cluster_key, "Custody cache cluster changed"
        )
        namespace = kube.namespace(root.namespace)
        _require(
            namespace["metadata"]["uid"] == root.namespace_uid
            and not namespace["metadata"].get("deletionTimestamp"),
            "Custody cache namespace changed",
        )
        result.extend(_cache_pvcs(kube, root.model_dump(mode="json"), pvs))
    _require(
        len({b.image_key for b in result}) == len(result),
        "Ambiguous cache source binding",
    )
    return result


def _scope_needed_sources(kube, backend, needed):
    """Limit cache CSI to exact clone parents already identified via Ceph."""
    if kube is None or getattr(kube, "project_cache_bindings", None) is not None:
        return
    names = set()
    for key in needed:
        info = _call(backend.image_info, key).value
        name = info.get("name") if isinstance(info, dict) else None
        _require(
            isinstance(info, dict)
            and info.get("id") == key.image_id
            and isinstance(name, str)
            and bool(name),
            "Custody source image name unavailable",
        )
        assert isinstance(name, str)
        names.add(name)
    kube.needed_cache_image_keys = set(needed)
    kube.needed_cache_image_names = names


def observe(bindings, kube, backend, pins, verified_at, *, snapshot_edges=None):
    """Pin supported exact source -> intermediate -> bound-head current chains."""
    pools = _call(backend.pool_identity).value
    _require(
        _call(backend.cluster_identity).value == bindings[0].image_key.ceph_fsid,
        "Custody backend changed",
    )
    proofs, observed, pending = [], {}, []
    for binding in bindings:
        head = binding.image_key
        observed[head] = _metadata(backend, head, pools)
        _, parent, snaps, children = observed[head]
        edges = (snapshot_edges or {}).get(head, {})
        _require(
            snaps == set(edges) and children == set(edges.values()),
            "Custody head has foreign snapshots or children",
        )
        if parent is None:
            continue
        middle = parent.image_key
        observed[middle] = _metadata(backend, middle, pools)
        _, source_parent, middle_snaps, middle_children = observed[middle]
        _require(
            source_parent is not None
            and middle_snaps == {parent}
            and middle_children == {head},
            "Custody intermediate is not exclusive",
        )
        assert source_parent is not None
        pending.append((binding, parent, middle, source_parent))
    caches = []
    if pending:
        _scope_needed_sources(
            kube, backend, {source.image_key for *_, source in pending}
        )
        caches = _sources(kube, pins)
    for binding, parent, middle, source_parent in pending:
        sources = [b for b in caches if b.image_key == source_parent.image_key]
        _require(
            len(sources) == 1, "Custody source is not an independently bound cache"
        )
        _require(
            middle not in {b.image_key for b in bindings + caches},
            "Custody intermediate overlaps another bound image",
        )
        # Requires independent backend CSI attribution, never a name-only guess.
        _require(
            _call(
                backend.verify_clone_intermediate, binding, middle, _pv(kube, binding)
            ).value
            is True,
            "Custody intermediate CSI attribution unavailable",
        )
        proofs.append(
            CurrentAncestryCustody(
                binding=binding,
                source=sources[0],
                intermediate=middle,
                head_parent=parent,
                intermediate_parent=source_parent,
                verified_at=verified_at,
            )
        )
    sources = _validate_sources(proofs, backend, pools)
    _require(
        all(_metadata(backend, key, pools) == value for key, value in observed.items()),
        "Custody ancestry changed during verification",
    )
    _require(
        sources == _validate_sources(proofs, backend, pools),
        "Custody source ancestry changed during verification",
    )
    if caches is not None:
        _require(caches == _sources(kube, pins), "Custody source binding changed")
    return proofs


def _validate_sources(proofs, backend, pools):
    snapshots, expected = defaultdict(set), {}
    for proof in proofs:
        snapshots[proof.source.image_key].add(proof.intermediate_parent)
        _require(proof.intermediate_parent not in expected, "Shared source snapshot")
        expected[proof.intermediate_parent] = proof.intermediate
    _require(
        len({p.intermediate for p in proofs}) == len(proofs),
        "Shared private intermediate",
    )
    observed = {}
    for key, selected in snapshots.items():
        observed[key] = _source_metadata(
            backend, key, selected, pools, CollectionLimits()
        )
        _, parent, entries = observed[key]
        _require(
            parent is None
            and all(
                e.present and set(e.children) == {expected[e.snapshot]} for e in entries
            ),
            "Custody source has unverified edges",
        )
    return observed


def resolve(db, manifest, retired):
    """Load only protected DB custody, never caller-supplied collector proof data."""
    from app.models.native_custody import NativeCustodyReceipt
    from app.models.project import Project
    from app.services.native_custody_reconciliation import effective
    from app.services.native_online_enrollment import retained

    result = []
    roots = {root_key(r) for r in manifest.pins.project_roots}
    for row in db.query(NativeCustodyReceipt):
        raw = row.payload["receipt"]["enrollment"]
        if raw["pins"]["domain_id"] != str(manifest.pins.domain_id):
            continue
        project = db.get(Project, row.project_id)
        # A successor can introduce the first ancestry proof. Resolve it before
        # deciding that the project has nothing to account for.
        _, receipt = cast("EffectiveCustody", effective(db, row.project_id))
        own = [
            p
            for p in receipt.ancestry
            if str(p.binding.root.project_id) == row.project_id
            and root_key(p.binding.root) not in retired
        ]
        if not own:
            continue
        _require(project is not None, "Custody project missing before settlement")
        if TYPE_CHECKING:
            assert project is not None
        # Proven disk ancestry belongs to the attested root and immutable image
        # identities, not the editable canvas. Keep receipt/owner/provider checks;
        # current storage graph validation still checks the original bindings.
        _, receipt, _ = cast(
            "RetainedCustody", retained(db, project, require_topology=False)
        )
        own = [
            p
            for p in receipt.ancestry
            if str(p.binding.root.project_id) == row.project_id
            and root_key(p.binding.root) not in retired
        ]
        _require(
            receipt.enrollment.request.target_id == manifest.request.target_id,
            "Custody target changed",
        )
        for proof in own:
            _require(
                root_key(proof.binding.root) in roots
                and any(
                    r.cluster_key == proof.source.cluster_key
                    and r.namespace == proof.source.namespace
                    and r.namespace_uid == proof.source.namespace_uid
                    for r in manifest.pins.cache_roots
                ),
                "Custody ancestry scope changed",
            )
            result.append(proof)
    from app.services.native_origin_custody import resolve_ancestry

    return result + resolve_ancestry(db, manifest, retired)
