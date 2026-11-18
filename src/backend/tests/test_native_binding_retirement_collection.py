"""Retiring one binding preserves its private liability and shared source edges."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.kubevirt_storage import BindingRetirementObservation
from app.services.kubevirt_storage_graph import attribute_graph
from app.services.native_clone_graph import cleanup_owned_images
from tests.test_native_clone_cleanup import absent
from tests.test_native_clone_lineage import source_evidence
from tests.test_native_project_clone_graph import scoped_chain


def retired_chain():
    inventory, images, proofs = scoped_chain()
    own, sibling = proofs
    now = datetime.now(UTC)
    private = {own.binding.image_key, own.intermediate}
    images = [
        (
            absent(i, now)
            if i.key in private
            else i.model_copy(
                update={
                    "collected_at": now,
                    "children": [sibling.intermediate],
                    "snapshots": [sibling.intermediate_parent],
                }
            )
        )
        for i in images
    ]
    images[-1] = source_evidence(images[-1], proofs)
    retirement = BindingRetirementObservation(
        request_id=uuid4(),
        root=own.binding.root,
        binding=own.binding,
        private_images=list(private),
        objects_absent=True,
        image_absence=[i.absence for i in images if i.key in private],
        kubernetes_digest="a" * 64,
        kubernetes_final_digest="a" * 64,
        verified_at=now,
    )
    return inventory, images, proofs, retirement


def test_exact_binding_retirement_keeps_head_and_intermediate_owned():
    inventory, images, proofs, retirement = retired_chain()
    result = attribute_graph(
        inventory, images, clone_lineages=proofs, binding_retirements=[retirement]
    )
    assert result.projects[0].allocated_bytes == 0
    assert result.projects[0].required_images == 2
    assert cleanup_owned_images(
        inventory, images, proofs, inventory.project_scope, [retirement]
    ) == set(retirement.private_images)
    assert not inventory.retiring_roots


@pytest.mark.parametrize(
    "fault",
    ["untrusted", "missing_middle", "source_snapshot", "source_child", "resurrection"],
)
def test_retired_binding_cannot_forget_liability_or_stale_source_edge(fault):
    inventory, images, proofs, retirement = retired_chain()
    trusted = [retirement]
    if fault == "untrusted":
        from app.schemas.kubevirt_storage import (
            BindingRetirementEvidence,
            BindingRetirementReference,
        )

        inventory = inventory.model_copy(
            update={
                "binding_retirements": [
                    BindingRetirementEvidence(
                        reference=BindingRetirementReference(
                            receipt_id=uuid4(), digest="a" * 64
                        ),
                        observation=retirement,
                    )
                ]
            }
        )
        trusted = []
    elif fault == "missing_middle":
        images = [i for i in images if i.key != proofs[0].intermediate]
    elif fault == "resurrection":
        images[0] = images[0].model_copy(update={"absence": None, "allocated_bytes": 1})
    else:
        source = images[-1]
        field, value = (
            ("snapshots", proofs[0].intermediate_parent)
            if fault == "source_snapshot"
            else ("children", proofs[0].intermediate)
        )
        images[-1] = source.model_copy(update={field: getattr(source, field) + [value]})
    result = attribute_graph(
        inventory, images, clone_lineages=proofs, binding_retirements=trusted
    )
    assert result.projects[0].allocated_bytes is None


@pytest.mark.parametrize(
    "fault", [None, "missing", "duplicate", "unselected", "stale", "unpinned"]
)
def test_import_requires_exact_selected_current_scan_retirement(monkeypatch, fault):
    from datetime import timedelta

    from app.schemas.kubevirt_storage import (
        BindingRetirementEvidence,
        BindingRetirementReference,
    )
    from app.services import native_binding_retirement_collection as collection

    inventory, images, proofs, observation = retired_chain()
    reference = BindingRetirementReference(receipt_id=uuid4(), digest="a" * 64)
    prepared = SimpleNamespace(
        root=observation.root, binding=observation.binding, ancestry=proofs[0]
    )
    monkeypatch.setattr(
        collection,
        "protected_inputs",
        lambda *a, **kw: [(reference, SimpleNamespace(prepared=prepared))],
    )
    # Observation identity/schema validation belongs to the retirement proof service;
    # this test isolates selection, scan time and pin validation at report import.
    monkeypatch.setattr(
        "app.services.native_binding_retirement.validate_observation", lambda *a: None
    )
    evidence = BindingRetirementEvidence(reference=reference, observation=observation)
    supplied = [evidence]
    if fault == "missing":
        supplied = []
    elif fault == "duplicate":
        supplied *= 2
    elif fault == "unselected":
        supplied = [
            evidence.model_copy(
                update={
                    "reference": reference.model_copy(update={"receipt_id": uuid4()})
                }
            )
        ]
    report = SimpleNamespace(
        project_scope=observation.root.project_id,
        binding_retirements=supplied,
        retiring_roots=[],
        started_at=observation.verified_at
        + (timedelta(seconds=1) if fault == "stale" else timedelta()),
        completed_at=observation.verified_at + timedelta(seconds=5),
    )
    manifest = SimpleNamespace(
        pins=SimpleNamespace(
            project_roots=[] if fault == "unpinned" else [observation.root]
        )
    )
    if fault is None:
        assert collection.report_authority(None, manifest, report) == (
            [observation],
            [proofs[0]],
        )
    else:
        with pytest.raises(ValueError):
            collection.report_authority(None, manifest, report)


def test_collection_reverifies_protected_retirements_with_current_pins(monkeypatch):
    from app.schemas.kubevirt_storage import BindingRetirementReference
    from app.services import native_binding_retirement_collection as collection

    inventory, images, proofs, observation = retired_chain()
    prepared = SimpleNamespace(
        root=observation.root, binding=observation.binding, ancestry=proofs[0]
    )
    reference = BindingRetirementReference(receipt_id=uuid4(), digest="a" * 64)
    values = [(reference, SimpleNamespace(prepared=prepared))]
    calls = []

    def verify_many(prepared_values, kube, backend, *, roots):
        calls.append((prepared_values, roots))
        if len(calls) > 1:
            raise ValueError("Retired image resurrected during scan")
        return [SimpleNamespace(observation=observation)]

    monkeypatch.setattr(
        "app.services.native_binding_retirement.verify_many", verify_many
    )
    result = collection.scan_evidence(
        values, inventory, object(), object(), [observation.root]
    )
    assert result[0].reference == reference
    assert calls == [([prepared], {str(observation.root.project_id): observation.root})]
    with pytest.raises(ValueError, match="resurrected"):
        collection.scan_evidence(
            values, inventory, object(), object(), [observation.root]
        )
    assert collection.scan_evidence([], inventory, object(), object(), []) == []


def test_unrelated_scoped_project_does_not_require_sibling_retirement_evidence():
    from app.services.native_binding_retirement_collection import _scoped_values

    inventory, images, proofs, observation = retired_chain()
    value = SimpleNamespace(
        prepared=SimpleNamespace(root=observation.root, ancestry=proofs[0])
    )
    selected = [(object(), value)]
    unrelated_project = uuid4()
    assert _scoped_values(selected, unrelated_project, []) == []
    assert _scoped_values(selected, observation.root.project_id, []) == selected
    # A sibling sharing the same immutable source still needs the old edge proof.
    assert (
        _scoped_values(selected, proofs[1].binding.root.project_id, [proofs[1]])
        == selected
    )
