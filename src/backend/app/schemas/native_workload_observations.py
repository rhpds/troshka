"""Standalone workload evidence; deliberately cannot authorize quota release."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.kubevirt_storage import Evidence, ProjectRoot, Text


class WorkloadResource(Evidence):
    name: Text
    uid: Text
    resource_version: Text
    owner_uids: list[Text] = Field(default_factory=list, max_length=128)
    deleting: bool
    phase: Literal[
        "Pending",
        "Running",
        "Succeeded",
        "Failed",
        "Unknown",
        "Bound",
        "Lost",
        "Unset",
        "Other",
    ]
    paused: bool = False
    compute_state: Literal[
        "potentially_active", "terminal_observed", "unknown", "not_runtime"
    ]
    desired_running: bool | None = None
    desired_replicas: int | None = Field(default=None, ge=0)


class WorkloadCollection(Evidence):
    kind: Text
    started_at: datetime
    finished_at: datetime
    status: Literal["complete", "invalid_output", "unavailable", "timeout"]
    list_resource_version: Text | None = None
    resources: list[WorkloadResource] = Field(default_factory=list, max_length=10000)


class WorkloadObservation(Evidence):
    schema_version: Literal[1] = 1
    collector_id: Text
    root: ProjectRoot
    started_at: datetime
    finished_at: datetime
    inventory_complete: bool
    collections: list[WorkloadCollection] = Field(default_factory=list, max_length=16)
    blockers: list[Text] = Field(default_factory=list, max_length=32)
    observation_only: Literal[True] = True
    atomic_snapshot: Literal[False] = False
    settlement_verified: Literal[False] = False
    capacity_release_allowed: Literal[False] = False
    storage_absence_verified: Literal[False] = False
    admitted_plan_verified: Literal[False] = False
