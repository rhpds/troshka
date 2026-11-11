from copy import deepcopy

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
