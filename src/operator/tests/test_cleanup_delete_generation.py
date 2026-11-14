"""A Kubernetes DELETE generation bump is not a new deployment."""

import json

import pytest
from helpers import managed_power as power

from tests.test_managed_power import resource


def cleanup_root(pending=False):
    root = resource("TroshkaProject", "project", spec={"projectId": "original"})
    value = {
        "version": "native-cleanup-v1",
        "namespace_mode": "managed",
        "root": {"project_cr_uid": root["metadata"]["uid"], "deployment_generation": 1},
        "generation": "allocation",
        "stop_proof": {
            "request_id": "stop",
            "root_spec_digest": power.digest(root["spec"]),
        },
        "objects": [],
        "inventory_digest": power.digest([]),
    }
    stopped = {
        "version": 1,
        "mode": "stopped",
        "requestId": "stop",
        "allocationGeneration": "allocation",
    }
    if pending:
        request = {"payloadDigest": value["stop_proof"].pop("root_spec_digest")}
        journal = {"creates": {}, "generatedClaims": {}}
        value["pending_initialization"] = {"request": request, "journal": journal}
        stopped["pendingInitialization"] = request
        root["status"]["managedPower"] = {
            "state": "off",
            "producerDrained": True,
            "pendingCleanup": power.digest({"request": stopped, "journal": journal}),
        }
    root["metadata"]["annotations"] = {
        power.CLEANUP_ANNOTATION: json.dumps(value),
        power.ANNOTATION: json.dumps(stopped),
    }
    return root, value


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("generation,deleting", [(1, False), (1, True), (2, True)])
def test_original_cleanup_accepts_only_authenticated_delete_transition(
    pending, generation, deleting
):
    root, value = cleanup_root(pending)
    root["metadata"]["generation"] = generation
    if deleting:
        root["metadata"]["deletionTimestamp"] = "2026-11-13T00:00:00Z"
    assert power.cleanup_intent(root) == value


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize(
    "change", ["spec", "uid", "generation", "not_deleting", "missing_digest"]
)
def test_cleanup_rejects_changed_root_even_during_delete(pending, change):
    root, value = cleanup_root(pending)
    root["metadata"].update(generation=2, deletionTimestamp="2026-11-13T00:00:00Z")
    if change == "spec":
        root["spec"]["projectId"] = "changed"
    elif change == "uid":
        root["metadata"]["uid"] = "replacement"
    elif change == "generation":
        root["metadata"]["generation"] = 3
    elif change == "not_deleting":
        del root["metadata"]["deletionTimestamp"]
    elif pending:
        del value["pending_initialization"]["request"]["payloadDigest"]
    else:
        del value["stop_proof"]["root_spec_digest"]
    root["metadata"]["annotations"][power.CLEANUP_ANNOTATION] = json.dumps(value)
    with pytest.raises(power.ManagedPowerError):
        power.cleanup_intent(root)
