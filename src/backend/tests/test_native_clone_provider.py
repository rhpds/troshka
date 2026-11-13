"""Concrete clone observer provenance/CSI reads; fixtures are not live proof."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.kubevirt_cache_birth import NativeCacheBirth
from app.models.native_clone_lineage import NativeCloneLineage
from app.schemas.kubevirt_storage import ReadResult
from app.schemas.native_clone_lineage import CloneCreation, CloneIntent
from app.services.native_clone_provider import CloneVerificationAdapter
from tests.test_kubevirt_storage_reports import native_case as native_case_fixture

native_case = native_case_fixture


@pytest.mark.parametrize(
    "change", [None, "copy", "wrong_parent", "source_uid", "old_pv", "claim", "driver"]
)
def test_concrete_clone_observer_requires_new_exact_dynamic_rebind_and_cow(
    native_case, monkeypatch, change
):
    from app.services import native_managed_deploy as deploy
    from app.services.native_cache_birth import validated_receipt
    from tests.test_native_cache_birth import observed
    from tests.test_native_clone_lineage import managed_case

    case = native_case
    identity = managed_case(case)[0]
    with case.sessions() as db:
        row = db.get(NativeCloneLineage, identity)
        intent = CloneIntent.model_validate(row.intent)
        creation = CloneCreation.model_validate(row.creation)
        birth, receipt = validated_receipt(
            db, intent.cache_reference.operation_id, intent.cache_reference.digest
        )
        response = db.get(NativeCacheBirth, str(birth.operation_id)).create_response
    source = observed(birth, response)
    now = datetime.now(UTC).isoformat()
    dv = {
        "metadata": {
            "uid": creation.dv_uid,
            "name": intent.claim_name,
            "namespace": intent.root.namespace,
            "creationTimestamp": now,
        },
        "spec": {
            "source": {"pvc": {"name": birth.name, "namespace": birth.namespace}},
            "pvc": {"resources": {"requests": {"storage": "40Gi"}}},
        },
        "status": {"phase": "Succeeded"},
    }
    pvc_uid, pv_request = str(uuid4()), "pvc-" + str(uuid4())
    pvc = {
        "metadata": {
            "uid": pvc_uid,
            "name": intent.claim_name,
            "namespace": intent.root.namespace,
            "creationTimestamp": now,
            "ownerReferences": [
                {"kind": "DataVolume", "uid": creation.dv_uid, "controller": True}
            ],
        },
        "spec": {"volumeName": pv_request},
        "status": {"phase": "Bound"},
    }
    pv = {
        "metadata": {
            "uid": "new-clone-pv",
            "name": pv_request,
            "creationTimestamp": now,
            "annotations": {
                "pv.kubernetes.io/provisioned-by": intent.target.provisioner
            },
        },
        "spec": {
            "storageClassName": intent.target.storage_class_name,
            "claimRef": {
                "uid": pvc_uid,
                "name": intent.claim_name,
                "namespace": intent.root.namespace,
            },
            "csi": {
                "driver": intent.target.provisioner,
                "volumeHandle": "new-clone-handle",
            },
        },
    }
    head = receipt.image_key.model_copy(update={"image_id": "new-tenant-head"})
    middle = receipt.image_key.model_copy(update={"image_id": "new-private-temp"})
    infos = {
        receipt.image_key: {"id": receipt.image_key.image_id},
        head: {
            "id": head.image_id,
            "parent": {"id": middle.image_id, "pool": "tenant", "snap_id": 10},
        },
        middle: {
            "id": middle.image_id,
            "parent": {
                "id": receipt.image_key.image_id,
                "pool": "tenant",
                "snap_id": 20,
            },
        },
    }
    if change == "copy":
        infos[head].pop("parent")
    elif change == "wrong_parent":
        infos[middle]["parent"]["id"] = "unapproved-source"
    elif change == "source_uid":
        source["pvc"]["metadata"]["uid"] = "replaced"
    elif change == "old_pv":
        pv["metadata"]["creationTimestamp"] = (
            intent.created_at - timedelta(seconds=10)
        ).isoformat()
    elif change == "claim":
        pv["spec"]["claimRef"]["uid"] = "different-final-pvc"
    elif change == "driver":
        pv["spec"]["csi"]["driver"] = "unsupported-driver"
    calls = []

    def result(value):
        return ReadResult(
            status="complete", collected_at=datetime.now(UTC), value=value
        )

    def resolve(driver, handle, **kwargs):
        calls.append((handle, kwargs["expected_request"]))
        return result(receipt.image_key if handle == receipt.volume_handle else head)

    backend = SimpleNamespace(
        resolve_csi=resolve,
        image_info=lambda key: result(infos[key]),
        pool_identity=lambda: result([{"poolname": "tenant", "poolnum": 1}]),
    )
    adapter = CloneVerificationAdapter(
        case.sessions,
        SimpleNamespace(api=object()),
        backend,
        object(),
        SimpleNamespace(),
    )
    monkeypatch.setattr(
        adapter,
        "_identities",
        lambda current: calls.append(
            ("full-installation-and-source", str(current.operation_id))
        ),
    )
    monkeypatch.setattr(
        adapter,
        "_objects",
        lambda namespace, name: deepcopy((source["dv"], source["pvc"], source["pv"]))
        if namespace == birth.namespace
        else deepcopy((dv, pvc, pv)),
    )
    monkeypatch.setattr(deploy.power.helpers(), "PowerClient", lambda api: object())
    monkeypatch.setattr(
        deploy,
        "observe",
        lambda *args: calls.append(("writer-free-full-inventory", intent.operation_id)),
    )
    if change:
        with pytest.raises(ValueError):
            adapter.observe_clone(intent, creation)
    else:
        first, second = adapter.observe_clone(intent, creation), adapter.observe_clone(
            intent, creation
        )
        assert first.lineage.model_dump(
            exclude={"verified_at"}
        ) == second.lineage.model_dump(exclude={"verified_at"})
        assert first.lineage.intermediate == middle
        assert first.lineage.binding.image_key == head
        assert first.lineage.binding.pvc_uid == pvc_uid
        assert pv_request != "pvc-" + pvc_uid
        assert ("new-clone-handle", pv_request) in calls
        assert len([c for c in calls if c[0] == "writer-free-full-inventory"]) == 4
