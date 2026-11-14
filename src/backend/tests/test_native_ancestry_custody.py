"""Current custody preserves exact graph and retirement restrictions."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.schemas.kubevirt_storage import ReadResult
from app.schemas.native_online_enrollment import CurrentAncestryCustody
from app.services import native_ancestry_custody as custody
from app.services.kubevirt_storage_graph import attribute_graph
from app.services.kubevirt_storage_reports import report_inventory
from app.services.native_clone_graph import cleanup_owned_images
from tests.test_native_clone_lineage import chain


def fixture(monkeypatch):
    report, images, managed = chain()
    now = datetime.now(UTC)
    proofs = [
        CurrentAncestryCustody(
            **p.model_dump(
                include=set(CurrentAncestryCustody.model_fields) - {"profile"}
            )
        )
        for p in managed
    ]
    values = {
        i.key: (i.key.image_id, i.parent, set(i.snapshots), set(i.children))
        for i in images
    }
    calls = []

    class Backend:
        def pool_identity(self):
            return ReadResult(status="complete", collected_at=now, value=[])

        def cluster_identity(self):
            return ReadResult(
                status="complete",
                collected_at=now,
                value=proofs[0].binding.image_key.ceph_fsid,
            )

        def verify_clone_intermediate(self, binding, middle, pv):
            calls.append((binding, middle))
            return ReadResult(status="complete", collected_at=now, value=True)

    monkeypatch.setattr(custody, "_metadata", lambda backend, key, pools: values[key])
    monkeypatch.setattr(custody, "_pv", lambda kube, binding: {})
    monkeypatch.setattr(custody, "_sources", lambda kube, pins: [proofs[0].source])
    return SimpleNamespace(
        report=report,
        images=images,
        proofs=proofs,
        values=values,
        backend=Backend(),
        now=now,
        calls=calls,
    )


def test_current_ancestry_read_only_custody_and_accounting(monkeypatch):
    q = fixture(monkeypatch)
    proofs = custody.observe(
        [p.binding for p in q.proofs], None, q.backend, None, q.proofs[0].verified_at
    )
    assert len(q.calls) == 2
    assert all(p.profile == "native-current-ancestry-v1" for p in proofs)
    assert all(not hasattr(p, "original_dv_uid") for p in proofs)
    inventory = report_inventory(q.report)
    graph = attribute_graph(inventory, q.images, clone_lineages=proofs)
    assert [p.allocated_bytes for p in graph.projects] == [30, 50]
    for p in proofs:
        assert cleanup_owned_images(
            inventory, q.images, proofs, p.binding.root.project_id
        ) == {
            p.binding.image_key,
            p.intermediate,
        }


@pytest.mark.parametrize(
    "fault",
    [
        "head_child",
        "middle_child",
        "source_child",
        "source_snapshot",
        "missing_attribution",
    ],
)
def test_current_custody_refuses_foreign_or_unattributed_ancestry(monkeypatch, fault):
    q = fixture(monkeypatch)
    p = q.proofs[0]
    if fault == "missing_attribution":
        q.backend.verify_clone_intermediate = lambda *args: ReadResult(
            status="unsupported", collected_at=q.now
        )
    else:
        key = {
            "head_child": p.binding.image_key,
            "middle_child": p.intermediate,
            "source_child": p.source.image_key,
            "source_snapshot": p.source.image_key,
        }[fault]
        values = list(q.values[key])
        index = 2 if fault == "source_snapshot" else 3
        values[index] = values[index] | {"foreign"}
        q.values[key] = tuple(values)
    with pytest.raises(Exception):
        custody.observe([p.binding for p in q.proofs], None, q.backend, None, q.now)


def test_current_custody_rejects_replaced_metadata(monkeypatch):
    q = fixture(monkeypatch)
    calls = 0

    def changing(backend, key, pools):
        nonlocal calls
        calls += 1
        value = q.values[key]
        return value if calls <= 5 else ("replacement", *value[1:])

    monkeypatch.setattr(custody, "_metadata", changing)
    with pytest.raises(ValueError, match="changed"):
        custody.observe([p.binding for p in q.proofs], None, q.backend, None, q.now)


@pytest.mark.parametrize(
    "fault", [None, "head_trash", "middle_trash", "source_snapshot", "source_owned"]
)
def test_current_custody_requires_exact_private_settlement(monkeypatch, fault):
    from uuid import uuid4

    from app.schemas.kubevirt_storage import RetiringRoot
    from tests.test_native_clone_cleanup import absent

    q = fixture(monkeypatch)
    own, sibling = q.proofs
    private = {own.binding.image_key, own.intermediate}
    retirement = RetiringRoot(
        intent_id=uuid4(),
        operation_id="owner-cleanup",
        root=own.binding.root,
        inventory_digest="a" * 64,
        owned_images=list(private),
        objects_absent=True,
        observed_at=q.now,
    )
    values = []
    for image in q.images:
        value = (
            absent(image, q.now)
            if image.key in private
            else image.model_copy(update={"collected_at": q.now})
        )
        if image.key == own.source.image_key:
            value = value.model_copy(
                update={
                    "children": [sibling.intermediate],
                    "snapshots": [sibling.intermediate_parent],
                }
            )
            if fault == "source_snapshot":
                value = value.model_copy(
                    update={
                        "snapshots": [
                            sibling.intermediate_parent,
                            own.intermediate_parent,
                        ]
                    }
                )
        if (fault == "head_trash" and image.key == own.binding.image_key) or (
            fault == "middle_trash" and image.key == own.intermediate
        ):
            value = image
        values.append(value)
    if fault == "source_owned":
        retirement = retirement.model_copy(
            update={"owned_images": [*private, own.source.image_key]}
        )
    inventory = report_inventory(q.report).model_copy(
        update={"retiring_roots": [retirement]}
    )
    graph = attribute_graph(inventory, values, clone_lineages=q.proofs)
    project = next(
        p for p in graph.projects if p.project_id == own.binding.root.project_id
    )
    assert (project.coverage == "complete") is (fault is None)
    if fault is None:
        assert project.allocated_bytes == 0
        assert graph.platform_pools[0].allocated_bytes == 100


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "journal",
        "driver",
        "head_id",
        "middle_id",
        "middle_name",
        "head_snapshot",
        "source_snapshot",
        "journal_replaced",
    ],
)
def test_independent_csi_journal_and_snapshot_attribution(fault):
    from uuid import uuid4

    _, _, proofs = chain()
    proof = proofs[0]
    token = str(uuid4())
    name = "csi-vol-" + token
    binding = proof.binding.model_copy(
        update={
            "volume_handle": "0001-0007-cluster-000000000000000e-" + token,
        }
    )
    pv = {
        "metadata": {
            "uid": binding.pv_uid,
            "name": "pvc-request",
            "annotations": {"pv.kubernetes.io/provisioned-by": binding.csi_driver},
        },
        "spec": {
            "csi": {"driver": binding.csi_driver, "volumeHandle": binding.volume_handle}
        },
    }
    now = datetime.now(UTC)
    infos = {
        binding.image_key: {
            "id": binding.image_key.image_id,
            "name": name,
            "parent": {
                "pool": "pool",
                "id": proof.intermediate.image_id,
                "snap_id": int(proof.head_parent.snapshot_id),
            },
        },
        proof.intermediate: {
            "id": proof.intermediate.image_id,
            "name": name + "-temp",
            "parent": {
                "pool": "pool",
                "id": proof.source.image_key.image_id,
                "snap_id": int(proof.intermediate_parent.snapshot_id),
            },
        },
    }
    snapshots = {
        proof.intermediate: [
            {
                "id": int(proof.head_parent.snapshot_id),
                "name": name,
                "namespace": {"type": "user"},
            }
        ],
        proof.source.image_key: [
            {
                "id": int(proof.intermediate_parent.snapshot_id),
                "name": str(uuid4()),
                "namespace": {
                    "type": "trash",
                    "original_namespace_type": "user",
                    "original_name": name + "-temp",
                },
            }
        ],
    }
    if fault == "driver":
        pv["spec"]["csi"]["driver"] = "other"
    if fault in {"head_id", "middle_id", "middle_name"}:
        key = binding.image_key if fault == "head_id" else proof.intermediate
        infos[key]["name" if fault == "middle_name" else "id"] = "replacement"
    if fault == "head_snapshot":
        snapshots[proof.intermediate][0]["name"] = "unrelated"
    if fault == "source_snapshot":
        snapshots[proof.source.image_key][0]["namespace"]["original_name"] = "unrelated"

    class Backend:
        calls = 0

        def result(self, value):
            return ReadResult(status="complete", collected_at=now, value=value)

        def resolve_csi(self, driver, handle, *, expected_request, volume_attributes):
            assert expected_request == "pvc-request"
            self.calls += 1
            key = binding.image_key
            if fault == "journal" or (fault == "journal_replaced" and self.calls == 2):
                key = key.model_copy(update={"image_id": "replacement"})
            return self.result(key)

        def image_info(self, key):
            return self.result(infos[key])

        def image_snapshots(self, key):
            return self.result(snapshots[key])

        def pool_identity(self):
            return self.result(
                [{"poolname": "pool", "poolnum": binding.image_key.pool_id}]
            )

    backend = Backend()
    if fault is None:
        custody.verify_csi_intermediate(backend, binding, proof.intermediate, pv)
        assert backend.calls == 2
    else:
        with pytest.raises(ValueError):
            custody.verify_csi_intermediate(backend, binding, proof.intermediate, pv)


def test_mixed_domain_retains_exact_custody_source_without_birth_receipt(monkeypatch):
    from app.services import kubevirt_storage_reports as reports

    q = fixture(monkeypatch)
    source = q.proofs[0].source
    foreign = source.model_copy(
        update={
            "pvc_uid": "foreign",
            "pv_uid": "foreign",
            "image_key": source.image_key.model_copy(update={"image_id": "foreign"}),
        }
    )
    rows = []

    class Database:
        def get(self, model, key):
            return next((r for r in rows if r.key == key), None)

        def add(self, row):
            rows.append(row)

        def flush(self):
            pass

    monkeypatch.setattr(
        reports, "_receipt_cache_identities", lambda db, receipts: set()
    )
    report = q.report.model_copy(update={"cache_bindings": [source, foreign]})
    reports._persist_bindings(
        Database(), report, "domain", q.proofs, ["unrelated-managed-receipt"]
    )
    retained = [r.payload for r in rows if r.root_key == "cache"]
    assert retained == [reports._binding_identity(source)]
