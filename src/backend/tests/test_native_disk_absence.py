"""A broken unrelated header must not hide the target's directory/trash state."""

import json
from datetime import UTC, datetime

import pytest

from app.schemas.kubevirt_storage import ImageKey, ReadResult
from app.services.kubevirt_storage_transport import (
    CapabilityProfile,
    PoolConfig,
    RbdReader,
    command_argv,
)

NOW = datetime.now(UTC)
KEY = ImageKey(ceph_fsid="fsid", pool_id=14, image_id="target")


class Transport:
    def __init__(self, *, active=False, trash=False, failure=None, changing=False):
        self.active, self.trash = active, trash
        self.failure, self.changing = failure, changing
        self.round = 0

    def run(self, operation, **kwargs):
        if operation == self.failure:
            return ReadResult(status="unavailable", collected_at=NOW)
        if operation == "directory_keys":
            self.round += 1
            value = "id_broken\nname_unreadable\n"
            if self.active or (self.changing and self.round > 1):
                value += "id_target\nname_original\n"
        elif operation == "trash_all":
            value = json.dumps(
                [{"id": "target", "source": "REMOVING"}] if self.trash else []
            )
        elif operation == "fsid":
            value = "fsid"
        elif operation == "pools":
            value = json.dumps([{"poolname": "pool", "poolnum": 14}])
        else:
            return ReadResult(status="unavailable", collected_at=NOW)
        return ReadResult(status="complete", value=value, collected_at=NOW)


def reader(**kwargs):
    return RbdReader(
        Transport(**kwargs),
        "fsid",
        [PoolConfig(pool_id=14, name="pool", csi_cluster_id="rook")],
        CapabilityProfile(command_version="rbd version 19.2.1"),
    )


@pytest.mark.parametrize("active,trash", [(False, False), (True, False), (False, True)])
def test_target_state_ignores_unreadable_unrelated_header(active, trash):
    result = reader(active=active, trash=trash).image_inventory(KEY, max_objects=10)
    assert result.status == "complete"
    assert result.value.image_id == "target"
    assert result.value.active is active
    assert result.value.trash is trash


@pytest.mark.parametrize("failure", ["directory_keys", "trash_all", "fsid", "pools"])
def test_incomplete_read_never_proves_absence(failure):
    assert (
        reader(failure=failure).image_inventory(KEY, max_objects=10).status
        != "complete"
    )


def test_target_reappearing_between_reads_blocks_absence():
    assert (
        reader(changing=True).image_inventory(KEY, max_objects=10).status != "complete"
    )


def test_directory_command_is_read_only_and_fixed_to_rbd_directory():
    assert command_argv("directory_keys", pool="pool") == [
        "rados",
        "--pool",
        "pool",
        "--namespace",
        "",
        "listomapkeys",
        "rbd_directory",
    ]


def test_removal_command_is_one_immutable_id_never_pool_purge():
    assert command_argv("trash_remove", pool="pool", image="target") == [
        "rbd",
        "trash",
        "rm",
        "target",
        "--pool",
        "pool",
        "--namespace",
        "",
        "--force",
    ]
    for image in [None, "", "--all", "target;purge"]:
        with pytest.raises(ValueError):
            command_argv("trash_remove", pool="pool", image=image)
    with pytest.raises(ValueError):
        command_argv("purge", pool="pool")


@pytest.mark.parametrize(
    "active,trash,image",
    [(True, False, "target"), (False, True, "target"), (False, False, "foreign")],
)
def test_present_or_wrong_target_cannot_be_recorded_as_zero(active, trash, image):
    from app.schemas.kubevirt_storage import BackendImageState, ImageObservation

    with pytest.raises(ValueError, match="absence proof"):
        ImageObservation(
            key=KEY,
            collected_at=NOW,
            allocated_bytes=0,
            provisioned_bytes=0,
            measurement_status="complete",
            command_version="rbd version 19.2.1",
            measurement_semantics_version="kubevirt-rbd-absence-v1",
            absence=BackendImageState(
                **{**KEY.model_dump(), "image_id": image},
                active=active,
                trash=trash,
                collected_at=NOW,
            ),
        )
