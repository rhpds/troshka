"""Pinned read transport fixture: remaps never become snapshot authority."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.kubevirt_storage import ImageKey, ReadResult
from app.schemas.native_snapshot_custody import SnapshotJournalProfile
from app.services.kubevirt_storage_transport import PoolConfig, RbdReader, command_argv

UUID = "11111111-1111-4111-8111-111111111111"
SOURCE_UUID = "22222222-2222-4222-8222-222222222222"
HANDLE = "0001-0004-rook-0000000000000001-" + UUID
SOURCE_HANDLE = "0001-0004-rook-0000000000000001-" + SOURCE_UUID
SOURCE = ImageKey(ceph_fsid="fsid", pool_id=1, image_id="source-id")
PROFILE = dict(
    version="ceph-csi-v3.13.0-rbd-snapshot-v1",
    driver="rook.rbd.csi.ceph.com",
    snapshot_class="rbd-snapshot",
    instance_id="default",
    sidecar_version="external-snapshotter-v8.0.1",
)
DOWNSTREAM_PROFILE = dict(
    PROFILE,
    version="odf-4.20-a3db60e1-rbd-snapshot-v1",
    sidecar_version="openshift-snapshotter-fd82be21387d",
)


@pytest.fixture(
    autouse=True, params=[PROFILE, DOWNSTREAM_PROFILE], ids=["upstream", "downstream"]
)
def selected_profile(request, monkeypatch):
    # Synthetic layout fixtures for both approved pairs, never live observations.
    monkeypatch.setitem(globals(), "SELECTED_PROFILE", request.param)


class JournalTransport:
    def __init__(self):
        self.calls = []
        self.fields = {
            "csi.imageid": "snapshot-id",
            "csi.imagename": "csi-snap-" + UUID,
            "csi.snapname": "snapshot-original-uid",
            "csi.source": "csi-vol-" + SOURCE_UUID,
        }
        self.remap = False

    def run(self, operation, **args):
        self.calls.append((operation, args))
        if operation == "fsid":
            value = "fsid"
        elif operation == "pools":
            value = json.dumps([{"poolnum": 1, "poolname": "rbd"}])
        elif operation == "journal":
            value = {
                "csi.imageid": "source-id",
                "csi.imagename": "csi-vol-" + SOURCE_UUID,
                "csi.volname": "pvc-source",
            }[args["journal_key"]]
        elif operation == "snapshot_lookup":
            value = UUID
        elif operation == "snapshot_journal":
            value = self.fields[args["journal_key"]]
            if (
                self.remap
                and len([c for c in self.calls if c[0] == "snapshot_journal"]) > 4
            ):
                value = "remapped"
        elif operation in {"info", "info_id"}:
            source = args["image"] in {"source-id", "csi-vol-" + SOURCE_UUID}
            value = json.dumps(
                {
                    "id": "source-id" if source else "snapshot-id",
                    "name": "csi-vol-" + SOURCE_UUID if source else "csi-snap-" + UUID,
                }
            )
        elif operation == "snapshots":
            value = json.dumps(
                [{"id": 7, "name": "csi-snap-" + UUID, "namespace": {"type": "user"}}]
            )
        else:
            raise AssertionError(operation)
        return ReadResult(
            status="complete", collected_at=datetime.now(UTC), value=value
        )


def reader():
    transport = JournalTransport()
    pool = PoolConfig(pool_id=1, name="rbd", csi_cluster_id="rook")
    return RbdReader(transport, "fsid", [pool], SimpleNamespace()), transport


def resolve(r, **kwargs):
    method = getattr(r, "resolve_csi_snapshot", None)
    assert method is not None, "snapshot handle must use a separate resolver"
    return method(
        SELECTED_PROFILE,
        HANDLE,
        expected_request="snapshot-original-uid",
        source=SOURCE,
        source_handle=SOURCE_HANDLE,
        source_request="pvc-source",
        **kwargs,
    )


def test_resolves_distinct_snapshot_backing_and_immutable_snapshot_id():
    r, t = reader()
    result = resolve(r)
    assert result.status == "complete"
    assert result.value.image.image_id == "snapshot-id"
    assert result.value.snapshot.snapshot_id == "7"
    assert result.value.snapshot.image_key == result.value.image
    assert result.value.image != SOURCE
    assert len([c for c in t.calls if c[0] == "snapshot_journal"]) == 8


@pytest.mark.parametrize(
    "field,value",
    [
        ("csi.source", "other-source"),
        ("csi.imageid", "source-id"),
        ("csi.snapname", "other-request"),
    ],
)
def test_wrong_source_identity_or_request_denies(field, value):
    r, t = reader()
    t.fields[field] = value
    assert resolve(r).status != "complete"


def test_journal_remap_during_observation_denies():
    r, t = reader()
    t.remap = True
    assert resolve(r).status != "complete"


def test_allowlisted_snapshot_reads_have_no_arbitrary_object_or_key():
    assert command_argv(
        "snapshot_journal", pool="rbd", object_uuid=UUID, journal_key="csi.source"
    )[-3:] == ["csi.snap." + UUID, "csi.source", "-"]
    with pytest.raises(ValueError):
        command_argv(
            "snapshot_journal", pool="rbd", object_uuid=UUID, journal_key="secret"
        )
    with pytest.raises(ValueError):
        command_argv(
            "snapshot_lookup",
            pool="rbd",
            instance_id="../../bad",
            request_name="snapshot-a",
        )


def test_profile_pair_preserves_serialization_and_protected_identity():
    from app.services.kubevirt_storage_enrollment import digest

    assert (
        SnapshotJournalProfile.model_validate(PROFILE).model_dump(mode="json")
        == PROFILE
    )
    assert (
        SnapshotJournalProfile.model_validate(DOWNSTREAM_PROFILE).model_dump(
            mode="json"
        )
        == DOWNSTREAM_PROFILE
    )
    assert digest(PROFILE) != digest(DOWNSTREAM_PROFILE)


@pytest.mark.parametrize(
    "version,sidecar",
    [
        (PROFILE["version"], DOWNSTREAM_PROFILE["sidecar_version"]),
        (DOWNSTREAM_PROFILE["version"], PROFILE["sidecar_version"]),
        ("odf-4.20", DOWNSTREAM_PROFILE["sidecar_version"]),
        (DOWNSTREAM_PROFILE["version"], "external-snapshotter-latest"),
    ],
)
def test_unapproved_or_mixed_profile_pairs_deny(version, sidecar):
    with pytest.raises(ValidationError):
        SnapshotJournalProfile.model_validate(
            dict(PROFILE, version=version, sidecar_version=sidecar)
        )


def test_unapproved_pair_cannot_reach_transport():
    r, transport = reader()
    profile = dict(PROFILE, sidecar_version=DOWNSTREAM_PROFILE["sidecar_version"])
    result = r.resolve_csi_snapshot(
        profile,
        HANDLE,
        expected_request="snapshot-original-uid",
        source=SOURCE,
        source_handle=SOURCE_HANDLE,
        source_request="pvc-source",
    )
    assert result.status == "unsupported"
    assert not transport.calls
