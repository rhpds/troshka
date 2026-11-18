"""Unresolved cross-project producers cannot borrow terminal storage authority."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.kubevirt_storage import BackendImageState, RetiringRoot, SnapshotKey
from app.schemas.native_online_enrollment import CurrentAncestryCustody
from app.schemas.native_project_cleanup import CleanupStorageScope, StorageLiabilities
from app.schemas.native_snapshot_custody import OriginProducerIntent
from app.services.kubevirt_storage_enrollment import digest
from app.services.kubevirt_storage_graph import attribute_graph
from app.services.native_project_retirement import verify_project_retirement
from app.services.native_snapshot_graph import authorize
from tests.test_kubevirt_storage_graph import NOW, inventory, key, observation


@pytest.fixture
def scope_case():
    inv = inventory()
    selected, other = inv.project_roots
    source = inv.volume_bindings[1]
    cache = inv.cache_bindings[0]
    head_parent = SnapshotKey(image_key=key("other-intermediate"), snapshot_id="head")
    cache_parent = SnapshotKey(image_key=cache.image_key, snapshot_id="middle")
    lineage = CurrentAncestryCustody(
        binding=source,
        source=cache,
        intermediate=head_parent.image_key,
        head_parent=head_parent,
        intermediate_parent=cache_parent,
        verified_at=NOW,
    )
    producer = OriginProducerIntent(
        id=uuid4(),
        operation_digest="a" * 64,
        kind="VolumeSnapshot",
        name="unresolved",
        role="snapshot",
        spec_digest="b" * 64,
        source_pvc_name="other-pvc",
        source_binding=source,
        snapshot_spec={
            "source": {"persistentVolumeClaimName": "other-pvc"},
            "volumeSnapshotClassName": "rbd",
        },
    )
    facts = SimpleNamespace(
        publication=SimpleNamespace(root=other),
        bindings=[source],
        ancestry=[lineage],
        producers=[producer],
        snapshots=[],
        snapshot_restores=[],
        snapshot_retirements=[],
    )
    liabilities = StorageLiabilities(
        bindings=[inv.volume_bindings[0]],
        private_images=[key("child-1"), key("selected-intermediate")],
        shared_images=[cache.image_key],
    )
    scope = CleanupStorageScope(
        intent_id=uuid4(),
        operation_id="cleanup",
        domain_id=uuid4(),
        root=selected,
        phase="namespace_absent",
        inventory_digest=digest(liabilities.model_dump(mode="json")),
        owned_images=liabilities.private_images,
        bindings=liabilities.bindings,
        teardown_at=NOW,
        liabilities=liabilities,
    )
    retiring = RetiringRoot(
        **scope.model_dump(
            include={
                "intent_id",
                "operation_id",
                "root",
                "inventory_digest",
                "owned_images",
            }
        ),
        objects_absent=True,
        observed_at=NOW,
    )
    inv = inv.model_copy(
        update={"volume_bindings": [source], "retiring_roots": [retiring]}
    )
    images = [
        observation("child-2", 10).model_copy(update={"parent": head_parent}),
        observation("other-intermediate", 10).model_copy(
            update={
                "parent": cache_parent,
                "snapshots": [head_parent],
                "children": [source.image_key],
            }
        ),
        observation("cache", 100).model_copy(
            update={"snapshots": [cache_parent], "children": [head_parent.image_key]}
        ),
    ]
    for private in scope.owned_images:
        images.append(
            observation(private.image_id, 0).model_copy(
                update={
                    "allocated_bytes": 0,
                    "provisioned_bytes": 0,
                    "measurement_semantics_version": "kubevirt-rbd-absence-v1",
                    "absence": BackendImageState(
                        **private.model_dump(),
                        active=False,
                        trash=False,
                        collected_at=NOW,
                    ),
                }
            )
        )
    terminal = verify_project_retirement(scope, inv, images)
    return SimpleNamespace(
        inventory=inv,
        images=images,
        scope=scope,
        terminal=terminal,
        facts=facts,
        selected=selected,
        other=other,
    )


@pytest.mark.parametrize("scoped", [False, True])
def test_unresolved_other_project_with_current_retained_chain_allows_only_verified_retirement(
    scope_case, scoped
):
    q = scope_case
    inv = q.inventory.model_copy(
        update={"project_scope": q.selected.project_id if scoped else None}
    )
    graph = attribute_graph(
        inv, q.images, snapshot_facts=[q.facts], project_cleanup_scopes=[q.scope]
    )
    projects = {p.project_id: p for p in graph.projects}
    assert projects[q.selected.project_id].coverage == "complete"
    assert projects[q.selected.project_id].allocated_bytes == 0
    assert (
        "origin_snapshot_authority_unverified" in projects[q.other.project_id].blockers
    )


@pytest.mark.parametrize(
    "unsafe",
    [
        "unknown_source",
        "wrong_owner",
        "missing_head",
        "missing_middle",
        "private_parent",
        "unknown_snapshot",
        "wrong_chain",
        "shared_missing",
        "unverified_retirement",
        "stale_head",
        "stale_middle",
        "stale_cache",
        "private_cache_child",
        "missing_binding",
        "untrusted_cache",
        "conflicting_owner",
        "conflicting_source_binding",
    ],
)
def test_unknown_or_conflicting_cross_project_dependency_remains_blocked(
    scope_case, unsafe
):
    q = scope_case
    observations = {i.key: i for i in q.images}
    terminal = [q.terminal]
    head = q.facts.bindings[0].image_key
    middle = q.facts.ancestry[0].intermediate
    if unsafe == "unknown_source":
        q.facts.producers[0] = q.facts.producers[0].model_copy(
            update={"source_binding": None}
        )
    elif unsafe == "wrong_owner":
        q.facts.publication.root = q.selected
    elif unsafe in {"missing_head", "missing_middle", "shared_missing"}:
        observations.pop(
            {
                "missing_head": head,
                "missing_middle": middle,
                "shared_missing": key("cache"),
            }[unsafe]
        )
    elif unsafe == "private_parent":
        observations[head] = observations[head].model_copy(
            update={
                "parent": SnapshotKey(
                    image_key=q.scope.owned_images[0], snapshot_id="private"
                )
            }
        )
    elif unsafe == "unknown_snapshot":
        observations[head] = observations[head].model_copy(
            update={"snapshots": [SnapshotKey(image_key=head, snapshot_id="unknown")]}
        )
    elif unsafe == "wrong_chain":
        q.facts.ancestry = []
    elif unsafe.startswith("stale_"):
        subject = {
            "stale_head": head,
            "stale_middle": middle,
            "stale_cache": key("cache"),
        }[unsafe]
        observations[subject] = observations[subject].model_copy(
            update={"collected_at": NOW - timedelta(seconds=1)}
        )
    elif unsafe == "private_cache_child":
        observations[key("cache")] = observations[key("cache")].model_copy(
            update={"children": q.scope.owned_images}
        )
    elif unsafe == "missing_binding":
        q.facts.bindings = []
    elif unsafe == "untrusted_cache":
        q.inventory = q.inventory.model_copy(update={"cache_bindings": []})
    elif unsafe == "conflicting_owner":
        q.facts.bindings.append(
            q.facts.bindings[0].model_copy(update={"root": q.selected})
        )
    elif unsafe == "conflicting_source_binding":
        q.inventory = q.inventory.model_copy(
            update={
                "volume_bindings": [
                    q.facts.bindings[0].model_copy(
                        update={"pvc_uid": "conflicting-pvc"}
                    )
                ]
            }
        )
    else:
        terminal = []
    inv = q.inventory.model_copy(update={"project_scope": q.selected.project_id})
    _, errors = authorize(inv, observations, [q.facts], project_retirements=terminal)
    assert any(
        e.code == "origin_snapshot_authority_unverified" and e.project_id is None
        for e in errors
    )


def test_ordinary_live_accounting_still_blocks_unresolved_other_project(scope_case):
    q = scope_case
    inv = q.inventory.model_copy(update={"project_scope": q.other.project_id})
    _, errors = authorize(inv, {i.key: i for i in q.images}, [q.facts])
    assert any(e.project_id is None for e in errors)


def test_duplicate_source_observation_cannot_prove_independence(scope_case):
    q = scope_case
    duplicate = q.images[0].model_copy(update={"allocated_bytes": 999})
    graph = attribute_graph(
        q.inventory,
        [*q.images, duplicate],
        snapshot_facts=[q.facts],
        project_cleanup_scopes=[q.scope],
    )
    selected = next(p for p in graph.projects if p.project_id == q.selected.project_id)
    assert "origin_snapshot_authority_unverified" in selected.blockers


@pytest.mark.parametrize("cleanup", [False, True])
def test_collector_seeds_unresolved_source_only_for_cleanup_and_follows_existing_parents(
    scope_case, cleanup
):
    import time
    from datetime import UTC, datetime

    from app.schemas.kubevirt_storage import CollectionLimits
    from app.services.kubevirt_storage_collector import _collect_images
    from tests.test_kubevirt_storage_collector import Rbd

    q = scope_case
    visited = []

    class Reader(Rbd):
        def image_info(self, image):
            visited.append(image)
            result = super().image_info(image)
            if image == key("child-2"):
                result.value["parent"] = {
                    "pool": "pool",
                    "id": "other-intermediate",
                    "snap_id": 1,
                }
            elif image == key("other-intermediate"):
                result.value["parent"] = {"pool": "pool", "id": "cache", "snap_id": 2}
            return result

    inv = q.inventory.model_copy(
        update={
            "volume_bindings": [],
            "retiring_roots": [],
            "project_scope": q.selected.project_id,
        }
    )
    errors = []
    images = _collect_images(
        inv,
        Reader(),
        [{"poolnum": 1, "poolname": "pool"}],
        "test",
        True,
        CollectionLimits(),
        lambda: datetime.now(UTC),
        errors,
        time.monotonic() + 10,
        snapshot_facts=[q.facts],
        project_cleanup_scopes=[q.scope] if cleanup else [],
    )
    assert not errors
    assert (key("child-2") in visited) == cleanup
    assert (key("other-intermediate") in {i.key for i in images}) == cleanup
