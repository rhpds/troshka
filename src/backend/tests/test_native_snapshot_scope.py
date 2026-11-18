"""Unresolved cross-project producers cannot borrow terminal storage authority."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.kubevirt_storage import (
    BackendImageState,
    CurrentImageDependencies,
    RetiringRoot,
    SnapshotKey,
)
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


def current_images(q):
    head = q.facts.bindings[0].image_key
    snapshot = SnapshotKey(image_key=head, snapshot_id="113025")
    q.images[0] = q.images[0].model_copy(
        update={"snapshots": [snapshot], "children": [key("snapshot-child")]}
    )
    q.images[1] = q.images[1].model_copy(update={"parent": None})
    q.images[2] = q.images[2].model_copy(update={"snapshots": [], "children": []})
    q.images.append(
        observation("snapshot-child", 10).model_copy(update={"parent": snapshot})
    )
    child_snapshot = SnapshotKey(image_key=key("snapshot-child"), snapshot_id="113026")
    q.images[-1] = q.images[-1].model_copy(update={"snapshots": [child_snapshot]})
    q.images = [with_dependencies(i) if i.absence is None else i for i in q.images]
    return q.images


def with_dependencies(image):
    dependency = {"overlap_bytes": 10 if image.parent else 0, "parent": image.parent}
    value = CurrentImageDependencies(
        head=dependency,
        snapshots=[
            dict(snapshot=s, dependency=dependency, children=image.children)
            for s in image.snapshots
        ],
    )
    return image.model_copy(update={"current_dependencies": value})


@pytest.mark.parametrize("projected_cache", [False, True])
def test_current_descendants_and_flattened_intermediate_allow_only_terminal(
    scope_case, projected_cache
):
    q = scope_case
    current_images(q)
    if projected_cache:
        q.images[2] = q.images[2].model_copy(update={"source_snapshot_children": []})
    graph = attribute_graph(
        q.inventory,
        q.images,
        snapshot_facts=[q.facts],
        project_cleanup_scopes=[q.scope],
    )
    projects = {p.project_id: p for p in graph.projects}
    assert projects[q.selected.project_id].coverage == "complete"
    assert projects[q.selected.project_id].allocated_bytes == 0
    assert (
        "origin_snapshot_authority_unverified" in projects[q.other.project_id].blockers
    )


def test_current_closure_preserves_live_accounting_hold_and_bounds(scope_case):
    from app.services.native_snapshot_isolation import isolated

    q = scope_case
    current_images(q)
    observations = {i.key: i for i in q.images}
    _, errors = authorize(q.inventory, observations, [q.facts])
    assert any(e.project_id is None for e in errors)
    assert not isolated(
        observations,
        {q.images[0].key},
        key("cache"),
        set(q.scope.owned_images),
        NOW,
        limit=0,
    )


def test_legacy_observation_serialization_does_not_add_optional_evidence():
    image = observation("plain", 10)
    assert "current_dependencies" not in image.model_dump(mode="json")


def test_current_descendant_conflicting_bindings_remain_blocked(scope_case):
    q = scope_case
    current_images(q)
    binding = q.facts.bindings[0].model_copy(
        update={"image_key": key("snapshot-child"), "pvc_uid": "snapshot-owner"}
    )
    q.inventory = q.inventory.model_copy(
        update={
            "volume_bindings": [
                binding,
                binding.model_copy(update={"pvc_uid": "conflicting-owner"}),
            ]
        }
    )
    _, errors = authorize(
        q.inventory,
        {i.key: i for i in q.images},
        [q.facts],
        project_retirements=[q.terminal],
    )
    assert any(e.project_id is None for e in errors)


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "stale",
        "incomplete",
        "no_dependency_evidence",
        "hidden_private_parent",
        "wrong_child_snapshot",
        "duplicate_snapshot",
        "duplicate_child",
        "cycle",
        "missing_parent_snapshot",
        "missing_reverse_child",
        "missing_cache_evidence",
    ],
)
def test_changed_current_closure_rejects_incomplete_or_conflicting_proof(
    scope_case, change
):
    q = scope_case
    current_images(q)
    child = q.images[-1]
    evidence = child.current_dependencies
    if change == "missing":
        q.images.pop()
    elif change == "stale":
        q.images[-1] = child.model_copy(
            update={"collected_at": NOW - timedelta(seconds=1)}
        )
    elif change == "incomplete":
        q.images[-1] = child.model_copy(
            update={"measurement_status": "unavailable", "allocated_bytes": None}
        )
    elif change == "no_dependency_evidence":
        q.images[-1] = child.model_copy(update={"current_dependencies": None})
    elif change in {"hidden_private_parent", "cycle"}:
        parent_key = (
            q.scope.owned_images[0] if change == "hidden_private_parent" else child.key
        )
        row = evidence.snapshots[0].model_copy(
            update={
                "dependency": evidence.head.model_copy(
                    update={
                        "parent": SnapshotKey(image_key=parent_key, snapshot_id="x")
                    }
                )
            }
        )
        q.images[-1] = child.model_copy(
            update={
                "current_dependencies": evidence.model_copy(update={"snapshots": [row]})
            }
        )
    elif change == "wrong_child_snapshot":
        row = evidence.snapshots[0].model_copy(
            update={
                "snapshot": SnapshotKey(image_key=q.images[0].key, snapshot_id="113026")
            }
        )
        q.images[-1] = child.model_copy(
            update={
                "current_dependencies": evidence.model_copy(update={"snapshots": [row]})
            }
        )
    elif change == "duplicate_snapshot":
        q.images[-1] = child.model_copy(
            update={
                "current_dependencies": evidence.model_copy(
                    update={"snapshots": evidence.snapshots * 2}
                )
            }
        )
    elif change == "duplicate_child":
        head = q.images[0]
        row = head.current_dependencies.snapshots[0]
        q.images[0] = head.model_copy(
            update={
                "current_dependencies": head.current_dependencies.model_copy(
                    update={
                        "snapshots": [
                            row.model_copy(update={"children": row.children * 2})
                        ]
                    }
                )
            }
        )
    elif change == "missing_cache_evidence":
        q.images[2] = q.images[2].model_copy(update={"current_dependencies": None})
    else:
        head = q.images[0]
        updates = (
            {"snapshots": [], "children": []}
            if change == "missing_parent_snapshot"
            else {"children": []}
        )
        q.images[0] = with_dependencies(head.model_copy(update=updates))
    graph = attribute_graph(
        q.inventory,
        q.images,
        snapshot_facts=[q.facts],
        project_cleanup_scopes=[q.scope],
    )
    selected = next(p for p in graph.projects if p.project_id == q.selected.project_id)
    assert "origin_snapshot_authority_unverified" in selected.blockers


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


@pytest.mark.parametrize(
    "cleanup,shared_cache", [(False, False), (True, False), (True, True)]
)
def test_scoped_collector_closes_current_cleanup_descendants_only(
    scope_case, cleanup, shared_cache
):
    import json
    import time

    from app.schemas.kubevirt_storage import (
        CollectionLimits,
        ImageObservation,
        ReadResult,
    )
    from app.services.kubevirt_storage_collector import (
        _collect_images,
        _final_graph_check,
    )
    from tests.test_kubevirt_storage_collector import Rbd

    q = scope_case
    current_images(q)
    # Replace snapshot IDs only, preserving the dependency object's head field.
    images = {}
    for item in q.images:
        if item.absence is not None:
            continue
        raw = json.loads(
            item.model_dump_json().replace(
                '"snapshot_id":"head"', '"snapshot_id":"113018"'
            )
        )
        image = ImageObservation.model_validate(raw)
        images[image.key] = image
    if shared_cache:
        boundary = SnapshotKey(image_key=key("cache"), snapshot_id="12001")
        middle = images[q.images[1].key]
        images[middle.key] = with_dependencies(
            middle.model_copy(update={"parent": boundary})
        )
        images[key("cache")] = with_dependencies(
            images[key("cache")].model_copy(
                update={"snapshots": [boundary], "children": [middle.key]}
            )
        )
    child_reads = []

    class Reader(Rbd):
        def image_info(self, image):
            row = images[image]
            parent = row.parent
            value = {"id": image.image_id, "name": image.image_id}
            if parent:
                value["parent"] = {
                    "pool": "pool",
                    "id": parent.image_key.image_id,
                    "snap_id": parent.snapshot_id,
                }
            return ReadResult(status="complete", value=value, collected_at=NOW)

        def image_snapshots(self, image):
            return ReadResult(
                status="complete",
                value=[
                    {
                        "id": int(s.snapshot_id),
                        "name": s.snapshot_id,
                        "namespace": {"type": "user"},
                    }
                    for s in images[image].snapshots
                    + (
                        [SnapshotKey(image_key=image, snapshot_id="12002")]
                        if shared_cache and image == key("cache")
                        else []
                    )
                ],
                collected_at=NOW,
            )

        def image_children(self, image, sid):
            child_reads.append((image, sid))
            assert sid != "12002", "unrelated consumers must never be enumerated"
            row = next(
                s
                for s in images[image].current_dependencies.snapshots
                if s.snapshot.snapshot_id == sid
            )
            return ReadResult(
                status="complete",
                value=[{"pool": "pool", "id": c.image_id} for c in row.children],
                collected_at=NOW,
            )

        def image_dependencies(self, image, limit, selected=None):
            value = images[image].current_dependencies
            if shared_cache and image == key("cache"):
                assert selected == ["12001"]
            return ReadResult(
                status="complete",
                value={
                    "image_id": image.image_id,
                    "head": value.head.model_dump(),
                    "snapshots": [
                        {
                            "snapshot_id": s.snapshot.snapshot_id,
                            "dependency": s.dependency.model_dump(),
                        }
                        for s in value.snapshots
                        if selected is None or s.snapshot.snapshot_id in selected
                    ],
                },
                collected_at=NOW,
            )

    inv = q.inventory.model_copy(
        update={
            "volume_bindings": [],
            "retiring_roots": [],
            "project_scope": q.selected.project_id,
        }
    )
    errors = []
    limits = CollectionLimits()
    reader = Reader()
    collected = _collect_images(
        inv,
        reader,
        [{"poolnum": 1, "poolname": "pool"}],
        "test",
        True,
        limits,
        lambda: NOW,
        errors,
        time.monotonic() + 60,
        snapshot_facts=[q.facts],
        project_cleanup_scopes=[q.scope] if cleanup else [],
    )
    assert (key("snapshot-child") in {i.key for i in collected}) == cleanup
    if cleanup:
        assert all(i.current_dependencies is not None for i in collected)
        if shared_cache:
            from app.services.native_snapshot_isolation import isolated

            observations = {i.key: i for i in collected}
            assert isolated(
                observations,
                {q.images[0].key, q.images[1].key, key("cache")},
                key("cache"),
                set(q.scope.owned_images),
                NOW,
            )
            assert (key("cache"), "12001") in child_reads
            assert (key("cache"), "12002") not in child_reads
            cache = observations[key("cache")]
            assert cache.current_dependencies.cache_boundary == [boundary]
            missing = cache.model_copy(
                update={
                    "current_dependencies": cache.current_dependencies.model_copy(
                        update={"snapshots": []}
                    )
                }
            )
            assert not isolated(
                observations | {cache.key: missing},
                {q.images[0].key},
                key("cache"),
                set(q.scope.owned_images),
                NOW,
            )
            from app.services.native_snapshot_isolation import _complete

            assert not _complete(cache, NOW, boundary=False)
            private_child = cache.model_copy(
                update={"children": cache.children + list(q.scope.owned_images)}
            )
            assert not isolated(
                observations | {cache.key: private_child},
                {q.images[0].key},
                key("cache"),
                set(q.scope.owned_images),
                NOW,
            )
        child = images[key("snapshot-child")]
        value = child.current_dependencies
        images[child.key] = child.model_copy(
            update={
                "current_dependencies": value.model_copy(
                    update={"head": value.head.model_copy(update={"overlap_bytes": 11})}
                )
            }
        )
        checked = _final_graph_check(
            reader,
            collected,
            [{"poolnum": 1, "poolname": "pool"}],
            limits,
            time.monotonic() + 60,
            errors,
            lambda: NOW,
        )
        assert (
            next(i for i in checked if i.key == child.key).measurement_status
            != "complete"
        )


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
