"""Explicit operator maintenance targets, never quota dispatch authority."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.schemas.kubevirt_storage import Evidence, ProjectRoot, Text

Name = Annotated[
    str,
    Field(min_length=1, max_length=253, pattern=r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$"),
]


class HaltTarget(Evidence):
    kind: Literal[
        "VirtualMachine", "Deployment", "ReplicaSet", "StatefulSet", "Job"
    ] = "VirtualMachine"
    name: Name
    uid: Text
    resource_version: Text


class HaltManifest(Evidence):
    collector_id: Text
    root: ProjectRoot
    targets: list[HaltTarget] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_targets(self):
        if len({(t.kind, t.name) for t in self.targets}) != len(self.targets) or len(
            {t.uid for t in self.targets}
        ) != len(self.targets):
            raise ValueError("duplicate target")
        if not self.root.deployment_generation:
            raise ValueError("positive project generation required")
        return self
