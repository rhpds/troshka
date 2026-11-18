"""Reconciliation tolerates controller bookkeeping, never ownership changes."""

import json
from copy import deepcopy

import pytest

from app.services import native_snapshot_reconciliation as reconciliation


def root():
    progress = dict(
        purpose="update",
        message="waiting",
        success=False,
        failure=False,
        retries=1,
        delayed="first",
        started="original",
    )
    return {
        "metadata": {
            "uid": "root",
            "generation": 1,
            "resourceVersion": "1",
            "managedFields": [],
            "annotations": {
                "troshka.redhat.com/origin-token": "token",
                "kopf.zalando.org/project_update": json.dumps(progress),
            },
        },
        "spec": {"projectId": "project"},
        "status": {
            "kopf": {"progress": {"project_update": progress}},
            "originProducerObjects": {"producer": {"uid": "snapshot"}},
            "originExecutions": {
                "incarnation": {
                    "state": "complete",
                    "executionId": "first",
                    "operator": {"uid": "operator"},
                    "outcome": "failed",
                }
            },
        },
    }


def test_retry_bookkeeping_does_not_invalidate_stable_snapshot_root():
    before = root()
    after = deepcopy(before)
    after["metadata"].update(resourceVersion="2", managedFields=[{"time": "later"}])
    progress = after["status"]["kopf"]["progress"]["project_update"]
    progress.update(retries=2, delayed="later")
    after["metadata"]["annotations"]["kopf.zalando.org/project_update"] = json.dumps(
        progress
    )
    after["status"]["originExecutions"]["incarnation"]["executionId"] = "next"
    assert reconciliation._comparison_root(before) == reconciliation._comparison_root(
        after
    )
    assert before == root()  # Normalization must never alter the live observation.
    assert after["metadata"]["resourceVersion"] == "2"


@pytest.mark.parametrize(
    "path,value",
    [
        (("metadata", "uid"), "replacement"),
        (("metadata", "generation"), 2),
        (("metadata", "deletionTimestamp"), "now"),
        (("metadata", "annotations", "troshka.redhat.com/origin-token"), "other"),
        (
            ("metadata", "annotations", "troshka.redhat.com/origin-checkpoint-ack"),
            "new",
        ),
        (("spec", "projectId"), "other"),
        (("status", "originProducerObjects", "producer", "uid"), "other"),
        (("status", "originCreateEpochs"), {"producer": "new"}),
        (("status", "originExecutions", "incarnation", "state"), "active"),
        (("status", "originExecutions", "incarnation", "outcome"), "returned"),
        (("status", "originExecutions", "incarnation", "operator", "uid"), "other"),
        (("status", "kopf", "progress", "project_update", "success"), True),
        (("status", "kopf", "progress", "project_update", "message"), "different"),
    ],
)
def test_safety_state_changes_still_invalidate_snapshot_root(path, value):
    before = root()
    after = deepcopy(before)
    target = after
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert reconciliation._comparison_root(before) != reconciliation._comparison_root(
        after
    )


def test_active_execution_ids_are_never_normalized():
    before = root()
    before["status"]["originExecutions"]["incarnation"]["state"] = "active"
    after = deepcopy(before)
    after["status"]["originExecutions"]["incarnation"]["executionId"] = "other"
    assert reconciliation._comparison_root(before) != reconciliation._comparison_root(
        after
    )
