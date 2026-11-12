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
                "volumeSnapshotRef": {"uid": "snapshot-uid", "namespace": "project-1"},
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
