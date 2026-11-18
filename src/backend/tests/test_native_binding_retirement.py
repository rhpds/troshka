"""Binding absence requires original Kubernetes and private backend identities."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.schemas.kubevirt_storage import BackendImageState, ReadResult
from tests.kubevirt_storage_helpers import report_fixture

NOW = datetime.now(UTC)


@pytest.fixture
def retirement_case():
    from app.schemas.native_binding_retirement import PreparedBindingRetirement

    report = report_fixture("zero-private-parent.json")
    binding = report.volume_bindings[0]
    root = binding.root.model_copy(update={"deployment_generation": 1})
    prepared = PreparedBindingRetirement(
        request_id=uuid4(),
        target_id=uuid4(),
        domain_id=uuid4(),
        source_custody_id=uuid4(),
        source_custody_digest="a" * 64,
        root=root,
        binding=binding,
        pvc_name="original-disk",
        ancestry=None,
        administrator="reviewer",
        reason="Replaced disk independently confirmed absent",
        prepared_at=NOW,
    )

    class Kube:
        pv_rows, pvc_rows, snapshot_rows, content_rows = [], [], [], []
        generation = 1

        def cluster_identity(self):
            return root.cluster_key

        def namespace(self, _):
            return {"metadata": {"uid": root.namespace_uid}}

        def projects(self, _):
            return [
                {
                    "metadata": {
                        "uid": root.project_cr_uid,
                        "generation": self.generation,
                    },
                    "spec": {"projectId": str(root.project_id)},
                }
            ]

        def pvs(self):
            return deepcopy(self.pv_rows)

        def pvcs(self, namespace):
            return deepcopy(self.pvc_rows)

        def snapshots(self, namespace):
            return deepcopy(self.snapshot_rows)

        def snapshot_contents(self):
            return deepcopy(self.content_rows)

    class Backend:
        active = False
        trash = False
        status = "complete"

        def cluster_identity(self):
            return ReadResult(
                status="complete", value=binding.image_key.ceph_fsid, collected_at=NOW
            )

        def pool_identity(self):
            return ReadResult(
                status="complete",
                value=[{"poolnum": binding.image_key.pool_id}],
                collected_at=NOW,
            )

        def image_inventory(self, key, *, max_objects):
            return ReadResult(
                status=self.status,
                value=BackendImageState(
                    **key.model_dump(),
                    active=self.active,
                    trash=self.trash,
                    collected_at=datetime.now(UTC),
                ),
                collected_at=datetime.now(UTC),
            )

    return prepared, Kube(), Backend()


def test_verified_retirement_preserves_binding_and_never_retires_whole_root(
    retirement_case,
):
    from app.services.native_binding_retirement import verify

    prepared, kube, backend = retirement_case
    proof = verify(prepared, kube, backend)
    assert proof.observation.binding == prepared.binding
    assert proof.observation.private_images == [prepared.binding.image_key]
    assert proof.observation.objects_absent is True
    assert proof.observation.image_absence[0].active is False


@pytest.mark.parametrize(
    "remaining",
    [
        "pvc",
        "pv",
        "handle",
        "snapshot",
        "content",
        "static_snapshot",
        "active",
        "trash",
        "unavailable",
        "root_drift",
    ],
)
def test_any_original_resource_or_uncertainty_blocks_retirement(
    retirement_case, remaining
):
    from app.services.native_binding_retirement import verify

    prepared, kube, backend = retirement_case
    binding = prepared.binding
    if remaining == "pvc":
        kube.pvc_rows = [{"metadata": {"uid": binding.pvc_uid}}]
    elif remaining == "pv":
        kube.pv_rows = [{"metadata": {"uid": binding.pv_uid}, "spec": {}}]
    elif remaining == "handle":
        kube.pv_rows = [
            {
                "metadata": {"uid": "replacement"},
                "spec": {
                    "csi": {
                        "driver": binding.csi_driver,
                        "volumeHandle": binding.volume_handle,
                    }
                },
            }
        ]
    elif remaining == "snapshot":
        kube.snapshot_rows = [
            {
                "metadata": {"namespace": prepared.root.namespace},
                "spec": {"source": {"persistentVolumeClaimName": prepared.pvc_name}},
            }
        ]
    elif remaining in {"content", "static_snapshot"}:
        source = (
            {"volumeHandle": binding.volume_handle}
            if remaining == "content"
            else {"snapshotHandle": "unresolved"}
        )
        kube.content_rows = [{"spec": {"driver": binding.csi_driver, "source": source}}]
    elif remaining in {"active", "trash"}:
        setattr(backend, remaining, True)
    elif remaining == "unavailable":
        backend.status = "unavailable"
    else:
        kube.generation = 2
    with pytest.raises(ValueError):
        verify(prepared, kube, backend)


@pytest.mark.parametrize("reference", ["uid", "handle"])
def test_second_inventory_catches_racing_reappearance(retirement_case, reference):
    from app.services.native_binding_retirement import verify

    prepared, kube, backend = retirement_case
    original = backend.image_inventory

    def reappear(key, **kwargs):
        result = original(key, **kwargs)
        kube.pv_rows = [{"metadata": {"uid": prepared.binding.pv_uid}, "spec": {}}]
        if reference == "handle":
            kube.pv_rows = [
                {
                    "metadata": {"uid": "other-pv"},
                    "spec": {
                        "csi": {
                            "driver": prepared.binding.csi_driver,
                            "volumeHandle": prepared.binding.volume_handle,
                        }
                    },
                }
            ]
        return result

    backend.image_inventory = reappear
    with pytest.raises(ValueError):
        verify(prepared, kube, backend)


def test_unrelated_pv_insertion_retains_both_inventory_digests(retirement_case):
    from app.services.native_binding_retirement import verify

    prepared, kube, backend = retirement_case
    original = backend.image_inventory

    def insert_unrelated(key, **kwargs):
        result = original(key, **kwargs)
        kube.pv_rows = [{"metadata": {"uid": "unrelated-pv"}, "spec": {}}]
        return result

    backend.image_inventory = insert_unrelated
    observed = verify(prepared, kube, backend).observation
    assert observed.kubernetes_digest != observed.kubernetes_final_digest


def test_batch_uses_complete_shared_inventory_and_retains_observation_times(
    retirement_case,
):
    from app.services.native_binding_retirement import verify_many

    prepared, kube, backend = retirement_case
    individual = backend.image_inventory

    def batch(keys, *, max_objects):
        return ReadResult(
            status="complete",
            value=[individual(k, max_objects=max_objects).value for k in keys],
            collected_at=datetime.now(UTC),
        )

    backend.images_inventory = batch

    def forbidden(*args, **kwargs):
        raise AssertionError("batch reader must not repeat directory reads per image")

    backend.image_inventory = forbidden
    second = prepared.model_copy(
        update={
            "request_id": uuid4(),
            "binding": prepared.binding.model_copy(
                update={
                    "pvc_uid": "old-second",
                    "pv_uid": "old-second-pv",
                    "image_key": prepared.binding.image_key.model_copy(
                        update={"image_id": "old-second-image"}
                    ),
                }
            ),
        }
    )
    proofs = verify_many([prepared, second], kube, backend)
    assert [p.observation.private_images for p in proofs] == [
        [prepared.binding.image_key],
        [second.binding.image_key],
    ]
    assert all(
        p.observation.image_absence[0].collected_at >= p.observation.verified_at
        for p in proofs
    )


@pytest.mark.parametrize("remaining", ["none", "intermediate", "source_snapshot"])
def test_clone_retirement_requires_private_intermediate_and_source_edge_absent(
    retirement_case, remaining
):
    from app.schemas.kubevirt_storage import CacheBinding, SnapshotKey
    from app.schemas.native_online_enrollment import CurrentAncestryCustody
    from app.services.native_binding_retirement import verify

    prepared, kube, backend = retirement_case
    head = prepared.binding.image_key
    middle, source_key = (
        head.model_copy(update={"image_id": "private-middle"}),
        head.model_copy(update={"image_id": "shared-source"}),
    )
    source = CacheBinding(
        cluster_key=prepared.root.cluster_key,
        namespace="cache",
        namespace_uid="cache-uid",
        pvc_uid="cache-pvc",
        pv_uid="cache-pv",
        csi_driver=prepared.binding.csi_driver,
        volume_handle="cache-handle",
        image_key=source_key,
    )
    ancestry = CurrentAncestryCustody(
        binding=prepared.binding,
        source=source,
        intermediate=middle,
        head_parent=SnapshotKey(image_key=middle, snapshot_id="12"),
        intermediate_parent=SnapshotKey(image_key=source_key, snapshot_id="34"),
        verified_at=NOW,
    )
    prepared = prepared.model_copy(update={"ancestry": ancestry})
    individual = backend.image_inventory

    def inventory(key, **kwargs):
        result = individual(key, **kwargs)
        if key == middle and remaining == "intermediate":
            return result.model_copy(
                update={"value": result.value.model_copy(update={"active": True})}
            )
        return result

    backend.image_inventory = inventory
    backend.image_snapshots = lambda _: ReadResult(
        status="complete",
        value=[{"id": 34, "name": "old", "namespace": {"type": "user"}}]
        if remaining == "source_snapshot"
        else [],
        collected_at=datetime.now(UTC),
    )
    if remaining == "none":
        proof = verify(prepared, kube, backend)
        assert proof.observation.private_images == [head, middle]
        assert source_key not in proof.observation.private_images
    else:
        with pytest.raises(ValueError):
            verify(prepared, kube, backend)


@pytest.mark.parametrize(
    "mode", ["absent", "trash", "changed", "failed", "unrelated_churn"]
)
def test_transport_batch_shares_two_complete_pool_rounds(mode):
    from app.schemas.kubevirt_storage import ImageKey
    from app.services.kubevirt_storage_transport import (
        CapabilityProfile,
        PoolConfig,
        RbdReader,
    )

    class Transport:
        directories = 0

        def run(self, operation, **kwargs):
            values = {
                "fsid": "fsid",
                "pools": [{"poolname": "pool", "poolnum": 1}],
                "directory_keys": "id_other\nname_other",
                "trash_all": [],
            }
            if operation == "directory_keys":
                self.directories += 1
                if mode == "failed":
                    return ReadResult(status="unavailable", collected_at=NOW)
                if mode == "changed" and self.directories == 2:
                    values[operation] = "id_original-a\nname_old"
                if mode == "unrelated_churn" and self.directories == 2:
                    values[operation] = "id_other-new\nname_unrelated"
            if mode == "trash":
                values["trash_all"] = [{"id": "original-b"}]
            value = values[operation]
            return ReadResult(
                status="complete",
                value=value
                if operation in {"fsid", "directory_keys"}
                else json.dumps(value),
                collected_at=NOW,
            )

    transport = Transport()
    reader = RbdReader(
        transport,
        "fsid",
        [PoolConfig(pool_id=1, name="pool", csi_cluster_id="rook")],
        CapabilityProfile(command_version="rbd version 19.2.1"),
    )
    keys = [
        ImageKey(ceph_fsid="fsid", pool_id=1, image_id="original-" + suffix)
        for suffix in ["a", "b"]
    ]
    result = reader.images_inventory(keys, max_objects=10)
    if mode in {"changed", "failed"}:
        assert result.status != "complete"
    else:
        assert result.status == "complete"
        assert transport.directories == 2
        assert [p.active for p in result.value] == [False, False]
        assert [p.trash for p in result.value] == [False, mode == "trash"]
        assert all(p.collected_at == NOW for p in result.value)


def test_cluster_pv_reference_in_another_namespace_blocks_retirement(retirement_case):
    from app.services.native_binding_retirement import verify

    prepared, kube, backend = retirement_case
    kube.pv_rows = [
        {
            "metadata": {"uid": "different-pv"},
            "spec": {
                "claimRef": {
                    "uid": prepared.binding.pvc_uid,
                    "namespace": "another-namespace",
                },
            },
        }
    ]
    with pytest.raises(ValueError, match="original PV or CSI"):
        verify(prepared, kube, backend)


def test_malformed_or_duplicate_inventory_cannot_prove_absence(retirement_case):
    from app.services.native_binding_retirement import verify

    prepared, kube, backend = retirement_case
    kube.pv_rows = [{"spec": {}}]
    with pytest.raises(ValueError, match="inventory"):
        verify(prepared, kube, backend)


def test_later_current_generation_is_observed_without_rewriting_prepared(
    retirement_case,
):
    from app.services.native_binding_retirement import verify

    prepared, kube, backend = retirement_case
    kube.generation = 2
    root = prepared.root.model_copy(update={"deployment_generation": 2})
    value = verify(prepared, kube, backend, root=root)
    assert value.prepared.root.deployment_generation == 1
    assert value.observation.root.deployment_generation == 2
