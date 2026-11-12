from copy import deepcopy

import pytest
import yaml

from app.schemas.kubevirt_storage import ReadResult
from app.services.kubevirt_storage_inventory import discover_inventory
from tests.test_kubevirt_storage_graph import NOW, key, root


class Kube:
    cache_roots = []
    collector_id = "test"

    def cluster_identity(self):
        return "cluster"

    def namespace(self, name):
        return {"metadata": {"uid": "ns-1"}}

    def projects(self, namespace):
        return [
            {
                "metadata": {"uid": "cr-1"},
                "spec": {"projectId": str(root(1).project_id)},
            }
        ]

    def pvcs(self, namespace):
        return [
            {
                "metadata": {"uid": "pvc-1", "namespace": namespace},
                "spec": {"volumeName": "pv"},
                "status": {"phase": "Bound", "capacity": {"storage": "1Gi"}},
            }
        ]

    def pvs(self):
        return [
            {
                "metadata": {"name": "pv", "uid": "pv-1"},
                "spec": {
                    "claimRef": {"uid": "pvc-1", "namespace": "project-1"},
                    "csi": {"driver": "driver", "volumeHandle": "handle"},
                },
            }
        ]

    def snapshots(self, namespace):
        return []

    def snapshot_contents(self):
        return []

    def resolve_csi(self, driver, handle, **kwargs):
        return ReadResult(status="complete", collected_at=NOW, value=key("child-1"))


def test_all_namespace_pvcs_are_included_without_topology_or_owner():
    inventory = discover_inventory(Kube(), [], [root(1)])
    assert len(inventory.volume_bindings) == 1
    assert inventory.volume_bindings[0].provisioned_bytes == 1024**3
    assert not inventory.blockers


def test_changed_claim_uid_and_pending_are_blockers():
    kube = Kube()
    kube.pvs = lambda: [
        {
            "metadata": {"name": "pv", "uid": "pv-1"},
            "spec": {"claimRef": {"uid": "wrong"}},
        }
    ]
    assert (
        discover_inventory(kube, [], [root(1)]).blockers[0].code
        == "claim_identity_mismatch"
    )
    kube.pvcs = lambda ns: [
        {
            "metadata": {"uid": "pending", "namespace": ns},
            "spec": {},
            "status": {"phase": "Pending"},
        }
    ]
    assert discover_inventory(kube, [], [root(1)]).blockers[0].code == "pending_pvc"


def test_full_uuid_proof_rejects_equal_prefix_collision():
    kube = Kube()
    kube.projects = lambda ns: [
        {
            "metadata": {"uid": "cr-1"},
            "spec": {"projectId": "00000000-9999-4000-8000-000000000001"},
        }
    ]
    inventory = discover_inventory(kube, [], [root(1)])
    assert not inventory.volume_bindings
    assert inventory.blockers


def test_unrelated_namespace_pvc_is_never_bound():
    kube = Kube()
    original = kube.pvcs

    def wrong(ns):
        result = deepcopy(original(ns))
        result[0]["metadata"]["namespace"] = "foreign"
        return result

    kube.pvcs = wrong
    assert not discover_inventory(kube, [], [root(1)]).volume_bindings


def snapshot_kube():
    kube = Kube()
    kube.snapshots = lambda ns: [
        {
            "metadata": {"uid": "snapshot-uid", "name": "retained"},
            "spec": {"source": {"persistentVolumeClaimName": "deleted-source-pvc"}},
            "status": {"readyToUse": True, "boundVolumeSnapshotContentName": "content"},
        }
    ]
    kube.snapshot_contents = lambda: [
        {
            "metadata": {"name": "content", "uid": "content-uid"},
            "spec": {
                "driver": "unknown.csi",
                "volumeSnapshotRef": {
                    "uid": "snapshot-uid",
                    "name": "retained",
                    "namespace": "project-1",
                },
                "deletionPolicy": "Retain",
                "source": {"volumeHandle": "missing-volume-handle"},
            },
            "status": {"snapshotHandle": "unresolved-snapshot-handle"},
        }
    ]
    return kube


def test_ready_snapshot_without_source_pvc_is_explicitly_unresolved():
    from app.schemas.kubevirt_storage import CollectionLimits
    from app.services.kubevirt_storage_collector import collect_report
    from tests.test_kubevirt_storage_collector import Rbd

    kube = snapshot_kube()
    discovered = discover_inventory(kube, [], [root(1)])
    assert any(b.code == "csi_snapshot_backend_unresolved" for b in discovered.blockers)
    assert discovered.snapshot_evidence[0].source_pvc_name == "deleted-source-pvc"
    reader = Rbd()
    reader.recheck_inventory = lambda: discover_inventory(kube, [], [root(1)])
    result = collect_report(
        discovered,
        reader,
        limits=CollectionLimits(),
        clock=lambda: discovered.collected_at,
    )
    assert result.projects[0].allocated_bytes is None
    assert result.snapshot_evidence == discovered.snapshot_evidence


def test_snapshot_handle_and_content_replacements_change_inventory_digest():
    from app.services.kubevirt_storage_inventory import inventory_digest

    kube = snapshot_kube()
    original = discover_inventory(kube, [], [root(1)])
    contents = kube.snapshot_contents()
    kube.snapshot_contents = lambda: contents
    contents[0]["status"]["snapshotHandle"] = "replacement-handle"
    assert inventory_digest(
        discover_inventory(kube, [], [root(1)])
    ) != inventory_digest(original)
    contents[0]["status"]["snapshotHandle"] = "unresolved-snapshot-handle"
    contents[0]["metadata"]["uid"] = "replacement-content-uid"
    assert inventory_digest(
        discover_inventory(kube, [], [root(1)])
    ) != inventory_digest(original)


def test_deleted_snapshot_content_is_uncertain_evidence_without_promoting_ownership():
    from app.schemas.kubevirt_storage import CollectionLimits
    from app.services.kubevirt_storage_collector import collect_report
    from tests.test_kubevirt_storage_collector import Rbd

    kube = snapshot_kube()
    kube.snapshots = lambda ns: []
    discovered = discover_inventory(kube, [], [root(1)])
    assert len(discovered.snapshot_evidence) == 1
    evidence = discovered.snapshot_evidence[0]
    assert evidence.snapshot_uid is None
    assert evidence.reference_snapshot_uid == "snapshot-uid"
    assert evidence.reference_namespace == "project-1"
    assert evidence.reference_snapshot_name == "retained"
    assert evidence.deletion_policy == "Retain"
    assert evidence.content_uid == "content-uid"
    assert not discovered.retained_bindings
    assert [b.image_key for b in discovered.volume_bindings] == [key("child-1")]
    assert "snapshot_identity_unresolved" in {b.code for b in discovered.blockers}
    reader = Rbd()
    reader.recheck_inventory = lambda: discover_inventory(kube, [], [root(1)])
    result = collect_report(
        discovered,
        reader,
        limits=CollectionLimits(),
        clock=lambda: discovered.collected_at,
    )
    assert result.projects[0].allocated_bytes is None
    assert result.projects[0].coverage == "partial"
    assert result.projects[0].measured_images == 1


@pytest.mark.parametrize(
    "section,field,replacement",
    [
        ("metadata", "uid", "replacement-content"),
        ("status", "snapshotHandle", "replacement-handle"),
        ("reference", "uid", "replacement-snapshot"),
        ("reference", "name", "replacement-name"),
        ("reference", "namespace", "unrelated-namespace"),
        ("source", "volumeHandle", "replacement-volume"),
        ("source", "snapshotHandle", "replacement-source-snapshot"),
        ("spec", "deletionPolicy", "Delete"),
    ],
)
def test_orphan_content_mutations_change_digest_and_final_recheck(
    section, field, replacement
):
    from app.schemas.kubevirt_storage import CollectionLimits
    from app.services.kubevirt_storage_collector import collect_report
    from tests.test_kubevirt_storage_collector import Rbd

    kube = snapshot_kube()
    kube.snapshots = lambda ns: []
    contents = kube.snapshot_contents()
    kube.snapshot_contents = lambda: contents
    before = discover_inventory(kube, [], [root(1)])
    target = contents[0]
    if section in {"reference", "source"}:
        target = target["spec"].setdefault(
            "volumeSnapshotRef" if section == "reference" else "source", {}
        )
    else:
        target = target[section]
    target[field] = replacement
    after = discover_inventory(kube, [], [root(1)])
    assert after.inventory_digest != before.inventory_digest
    reader = Rbd()
    reader.recheck_inventory = lambda: after
    report = collect_report(
        before, reader, limits=CollectionLimits(), clock=lambda: after.collected_at
    )
    assert "inventory_changed" in {b.code for b in report.collection_errors}


def test_same_snapshot_name_new_uid_does_not_rebind_old_content():
    kube = snapshot_kube()
    snapshots = kube.snapshots("project-1")
    snapshots[0]["metadata"]["uid"] = "new-snapshot-uid"
    kube.snapshots = lambda ns: snapshots
    discovered = discover_inventory(kube, [], [root(1)])
    evidence = discovered.snapshot_evidence[0]
    assert evidence.snapshot_uid == "new-snapshot-uid"
    assert evidence.reference_snapshot_uid == "snapshot-uid"
    assert "snapshot_identity_unresolved" in {b.code for b in discovered.blockers}


def test_replacement_snapshot_with_new_content_keeps_old_orphan_separate():
    kube = snapshot_kube()
    snapshots, contents = kube.snapshots("project-1"), kube.snapshot_contents()
    snapshots[0]["metadata"]["uid"] = "new-snapshot-uid"
    snapshots[0]["status"]["boundVolumeSnapshotContentName"] = "new-content"
    new_content = deepcopy(contents[0])
    new_content["metadata"] = {"name": "new-content", "uid": "new-content-uid"}
    new_content["spec"]["volumeSnapshotRef"]["uid"] = "new-snapshot-uid"
    kube.snapshots = lambda ns: snapshots
    kube.snapshot_contents = lambda: [*contents, new_content]
    discovered = discover_inventory(kube, [], [root(1)])
    assert len(discovered.snapshot_evidence) == 2
    evidence = {e.content_uid: e for e in discovered.snapshot_evidence}
    assert evidence["content-uid"].snapshot_uid is None
    assert evidence["content-uid"].reference_snapshot_uid == "snapshot-uid"
    assert evidence["new-content-uid"].snapshot_uid == "new-snapshot-uid"
    assert {b.code for b in discovered.blockers} == {
        "snapshot_identity_unresolved",
        "csi_snapshot_backend_unresolved",
    }


def test_unrelated_orphan_content_does_not_establish_project_evidence():
    kube = snapshot_kube()
    kube.snapshots = lambda ns: []
    contents = kube.snapshot_contents()
    contents[0]["spec"]["volumeSnapshotRef"]["namespace"] = "unrelated"
    kube.snapshot_contents = lambda: contents
    discovered = discover_inventory(kube, [], [root(1)])
    assert discovered.snapshot_evidence == []
    assert discovered.blockers == []


def test_orphan_content_removal_changes_digest():
    kube = snapshot_kube()
    kube.snapshots = lambda ns: []
    before = discover_inventory(kube, [], [root(1)])
    kube.snapshot_contents = lambda: []
    assert (
        discover_inventory(kube, [], [root(1)]).inventory_digest
        != before.inventory_digest
    )


def test_legacy_snapshot_evidence_serialization_and_digest_are_preserved():
    import hashlib
    import json

    from app.schemas.kubevirt_storage import CSISnapshotEvidence, NativeInventory
    from app.services.kubevirt_storage_inventory import inventory_digest

    legacy = {
        "root": root(1).model_dump(mode="json"),
        "snapshot_uid": "old-uid",
        "snapshot_name": "old-name",
        "content_name": None,
        "content_uid": None,
        "source_pvc_name": None,
        "csi_driver": None,
        "ready_to_use": False,
        "snapshot_handle_sha256": None,
        "source_snapshot_handle_sha256": None,
        "source_volume_handle_sha256": None,
    }
    evidence = CSISnapshotEvidence.model_validate(legacy)
    assert evidence.model_dump(mode="json") == legacy
    inventory = NativeInventory(
        project_roots=[root(1)], snapshot_evidence=[evidence], collected_at=NOW
    )
    normalized = {
        "project_roots": [legacy["root"]],
        "volume_bindings": [],
        "cache_bindings": [],
        "retained_bindings": [],
        "blockers": [],
        "snapshot_evidence": [legacy],
    }
    expected = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert inventory_digest(inventory) == expected


def static_kubeconfig(tmp_path, user=None):
    """A local-only operator file; no real credential or API endpoint."""
    path = tmp_path / "operator-kubeconfig"
    body = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "operator",
        "clusters": [
            {"name": "storage", "cluster": {"server": "https://storage.invalid"}}
        ],
        "contexts": [
            {"name": "operator", "context": {"cluster": "storage", "user": "operator"}}
        ],
        "users": [{"name": "operator", "user": user or {"token": "test-only-token"}}],
    }
    path.write_text(yaml.safe_dump(body))
    path.chmod(0o600)
    return path, body


@pytest.mark.parametrize("auth", ["exec", "auth-provider"])
@pytest.mark.parametrize("active", [True, False])
def test_native_reader_rejects_auth_helpers_before_loader_or_api(
    tmp_path, monkeypatch, capsys, caplog, auth, active
):
    import subprocess

    from kubernetes import client

    from app.services.kubevirt_storage_inventory import KubernetesReader

    path, body = static_kubeconfig(tmp_path)
    helper = (
        {
            "command": "forbidden-auth-helper",
            "apiVersion": "client.authentication.k8s.io/v1beta1",
        }
        if auth == "exec"
        else {"name": "gcp", "config": {"cmd-path": "forbidden-auth-helper"}}
    )
    if active:
        body["users"][0]["user"] = {auth: helper}
    else:
        body["users"].append({"name": "unused", "user": {auth: helper}})
    path.write_text(yaml.safe_dump(body))
    launches, apis = [], []

    def forbidden_process(*args, **kwargs):
        launches.append(args)
        raise RuntimeError("private-helper-diagnostic")

    def forbidden_api(*args, **kwargs):
        apis.append(args)
        raise RuntimeError("API must not be initialized")

    monkeypatch.setattr(subprocess, "Popen", forbidden_process)
    monkeypatch.setattr(client, "ApiClient", forbidden_api)
    with pytest.raises(ValueError, match="static"):
        KubernetesReader(str(path), None, collector_id="test", cache_roots=[])
    assert launches == []
    assert apis == []
    assert "private-helper-diagnostic" not in capsys.readouterr().err
    assert "private-helper-diagnostic" not in caplog.text


@pytest.mark.parametrize("auth", ["token", "client-certificate"])
def test_native_reader_keeps_static_credentials_and_relative_certificate_paths(
    tmp_path, auth
):
    from app.services.kubevirt_storage_inventory import KubernetesReader

    user = {"token": "test-only-token"}
    if auth == "client-certificate":
        (tmp_path / "client.crt").write_text("test certificate fixture")
        (tmp_path / "client.key").write_text("test key fixture")
        user = {"client-certificate": "client.crt", "client-key": "client.key"}
    path, _ = static_kubeconfig(tmp_path, user)
    reader = KubernetesReader(str(path), None, collector_id="test", cache_roots=[])
    try:
        assert reader.api.configuration.host == "https://storage.invalid"
        if auth == "token":
            assert (
                reader.api.configuration.api_key["BearerToken"]
                == "Bearer test-only-token"
            )
        else:
            assert reader.api.configuration.cert_file == str(tmp_path / "client.crt")
            assert reader.api.configuration.key_file == str(tmp_path / "client.key")
    finally:
        reader.api.close()


def test_native_reader_uses_same_validated_data_without_reopening_file(
    tmp_path, monkeypatch
):
    import subprocess

    from app.services.kubevirt_storage_inventory import (
        KubernetesReader,
        read_static_kubeconfig,
    )

    path, body = static_kubeconfig(tmp_path)
    parsed = read_static_kubeconfig(path)
    body["users"][0]["user"] = {"exec": {"command": "forbidden-auth-helper"}}
    path.write_text(yaml.safe_dump(body))
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("must not reopen config"),
    )
    reader = KubernetesReader(
        str(path), None, collector_id="test", cache_roots=[], kubeconfig_data=parsed
    )
    try:
        assert (
            reader.api.configuration.api_key["BearerToken"] == "Bearer test-only-token"
        )
    finally:
        reader.api.close()
