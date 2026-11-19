"""Project scans resolve only their exact shared sources, without trusting stale bindings."""

from copy import deepcopy

import pytest

from app.schemas.kubevirt_storage import ReadResult
from app.services.kubevirt_storage_inventory import discover_inventory
from tests.test_kubevirt_storage_graph import NOW, key
from tests.test_native_cache_retirement import setup_cache


def cache_case():
    kube, source = setup_cache()
    unrelated = source.model_copy(
        update={
            "pvc_uid": "other",
            "pv_uid": "other-pv",
            "volume_handle": "other-handle",
            "image_key": key("other"),
        }
    )
    bindings = [source, unrelated]
    pvcs = [
        {
            "metadata": {"uid": b.pvc_uid, "namespace": b.namespace},
            "spec": {"volumeName": b.pv_uid},
            "status": {"phase": "Bound"},
        }
        for b in bindings
    ]
    pvs = [
        {
            "metadata": {"uid": b.pv_uid, "name": b.pv_uid},
            "spec": {
                "claimRef": {"uid": b.pvc_uid, "namespace": b.namespace},
                "csi": {"driver": b.csi_driver, "volumeHandle": b.volume_handle},
            },
        }
        for b in bindings
    ]
    calls = []

    def resolve(driver, handle, **kwargs):
        calls.append(handle)
        return ReadResult(
            status="complete",
            collected_at=NOW,
            value=next(b.image_key for b in bindings if b.volume_handle == handle),
        )

    kube.pvcs = lambda namespace: deepcopy(pvcs)
    kube.pvs = lambda: deepcopy(pvs)
    kube.resolve_csi = resolve
    kube.retained_cache_bindings = bindings
    return kube, source, unrelated, pvcs, pvs, calls


def test_project_scope_skips_unrelated_cache_resolution_on_both_passes():
    kube, source, unrelated, _, _, calls = cache_case()
    kube.project_cache_bindings = [source]
    for _ in range(2):
        result = discover_inventory(kube, [], [])
        assert not result.blockers
        assert result.cache_bindings == [source]
    assert calls == [source.volume_handle] * 2


def test_needed_image_names_skip_unrelated_cache_csi():
    kube, source, unrelated, _, pvs, calls = cache_case()
    for pv, binding in zip(pvs, [source, unrelated]):
        pv["spec"]["csi"]["volumeAttributes"] = {
            "imageName": f"csi-vol-{binding.pvc_uid}"
        }
    kube.retained_cache_bindings = []
    kube.needed_cache_image_names = {f"csi-vol-{source.pvc_uid}"}
    kube.needed_cache_image_keys = {source.image_key}
    result = discover_inventory(kube, [], [])
    assert not result.blockers
    assert result.cache_bindings == [source]
    assert calls == [source.volume_handle]


def test_domain_scan_still_resolves_all_cache_bindings():
    kube, source, unrelated, _, _, calls = cache_case()
    result = discover_inventory(kube, [], [])
    assert not result.blockers
    assert result.cache_bindings == [source, unrelated]
    assert calls == [source.volume_handle, unrelated.volume_handle]


def test_empty_project_scope_does_not_scan_cache():
    kube, _, _, _, _, calls = cache_case()
    kube.project_cache_bindings = []
    result = discover_inventory(kube, [], [])
    assert not result.blockers
    assert not result.cache_bindings
    assert not calls


@pytest.mark.parametrize(
    "changed", ["pv_uid", "handle", "driver", "image", "namespace_uid", "pvc_uid"]
)
def test_scoped_source_identity_changes_are_rejected(changed):
    kube, source, _, pvcs, pvs, _ = cache_case()
    kube.project_cache_bindings = [source]
    if changed == "pv_uid":
        pvs[0]["metadata"]["uid"] = "replacement"
    elif changed == "handle":
        pvs[0]["spec"]["csi"]["volumeHandle"] = "other-handle"
    elif changed == "driver":
        pvs[0]["spec"]["csi"]["driver"] = "replacement"
    elif changed == "image":
        kube.resolve_csi = lambda *a, **kw: ReadResult(
            status="complete", collected_at=NOW, value=key("replacement")
        )
    elif changed == "namespace_uid":
        kube.namespace = lambda name: {"metadata": {"uid": "replacement"}}
    else:
        pvcs[0]["metadata"]["uid"] = "replacement"
        pvs[0]["spec"]["claimRef"]["uid"] = "replacement"
    assert discover_inventory(kube, [], []).blockers


def test_scoped_absent_source_keeps_retained_identity_and_absence_checks():
    kube, source, _, pvcs, pvs, calls = cache_case()
    kube.project_cache_bindings = [source]
    pvcs.pop(0)
    result = discover_inventory(kube, [], [])
    assert result.cache_bindings == [source]
    assert not result.absent_cache_bindings  # PV still exists.
    pvs.pop(0)
    result = discover_inventory(kube, [], [])
    assert result.cache_bindings == result.absent_cache_bindings == [source]
    assert not calls


def test_required_source_outside_enrolled_namespace_is_rejected():
    kube, source, _, _, _, _ = cache_case()
    kube.project_cache_bindings = [source.model_copy(update={"namespace": "foreign"})]
    assert discover_inventory(kube, [], []).blockers


def test_missing_source_cannot_be_invented_from_lineage():
    kube, source, _, pvcs, pvs, _ = cache_case()
    kube.project_cache_bindings = [source]
    kube.retained_cache_bindings = []
    pvcs.pop(0)
    pvs.pop(0)
    result = discover_inventory(kube, [], [])
    assert result.blockers
    assert not result.cache_bindings


def test_second_pass_detects_replaced_source():
    kube, source, _, _, pvs, _ = cache_case()
    kube.project_cache_bindings = [source]
    assert not discover_inventory(kube, [], []).blockers
    pvs[0]["metadata"]["uid"] = "replaced-between-passes"
    assert discover_inventory(kube, [], []).blockers
