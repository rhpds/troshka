from app.schemas.kubevirt_storage import (
    CollectionLimits,
    NativeStorageReport,
    ReadResult,
)
from app.services.kubevirt_storage_collector import collect_report
from app.services.kubevirt_storage_transport import CapabilityProfile
from tests.test_kubevirt_storage_graph import NOW, inventory, key


class Rbd:
    capability = CapabilityProfile(
        command_version="rbd version test",
        merge_snapshots_validated=True,
        validation_reference="synthetic-test",
    )

    def cluster_identity(self):
        return ReadResult(status="complete", value=key("x").ceph_fsid, collected_at=NOW)

    def command_version(self):
        return ReadResult(status="complete", value="rbd version test", collected_at=NOW)

    def pool_identity(self):
        return ReadResult(
            status="complete",
            value=[{"poolnum": 1, "poolname": "pool"}],
            collected_at=NOW,
        )

    def image_info(self, image):
        return ReadResult(
            status="complete",
            value={"id": image.image_id, "name": image.image_id, "size": 100},
            collected_at=NOW,
        )

    def image_usage(self, image):
        return ReadResult(
            status="complete",
            value={
                "images": [
                    {"id": image.image_id, "used_size": 0, "provisioned_size": 100}
                ]
            },
            collected_at=NOW,
        )

    def image_snapshots(self, image):
        return ReadResult(status="complete", value=[], collected_at=NOW)

    def image_children(self, image, snapshot):
        return ReadResult(status="complete", value=[], collected_at=NOW)

    def trash_inventory(self, image):
        return ReadResult(status="complete", value=[], collected_at=NOW)

    def recheck_inventory(self):
        return inventory()


def test_zero_complete_report_requires_recheck_and_validated_capability():
    result = collect_report(
        inventory(), Rbd(), limits=CollectionLimits(), clock=lambda: NOW
    )
    assert [p.allocated_bytes for p in result.projects] == [0, 0]
    assert result.enforcement_eligible is False
    assert NativeStorageReport.model_validate_json(result.model_dump_json()) == result


def test_unvalidated_semantics_never_complete():
    reader = Rbd()
    reader.capability = CapabilityProfile(command_version="rbd version test")
    result = collect_report(
        inventory(), reader, limits=CollectionLimits(), clock=lambda: NOW
    )
    assert all(p.allocated_bytes is None for p in result.projects)


def test_identity_replacement_during_measurement_is_partial():
    reader = Rbd()
    calls = {}
    original = reader.image_info

    def changed(image):
        calls[image] = calls.get(image, 0) + 1
        result = original(image)
        if calls[image] > 1:
            return result.model_copy(
                update={"value": {"id": "replacement", "size": 100}}
            )
        return result

    reader.image_info = changed
    result = collect_report(
        inventory(), reader, limits=CollectionLimits(), clock=lambda: NOW
    )
    assert all(i.measurement_status == "identity_changed" for i in result.images)


def test_inventory_not_rechecked_is_partial():
    reader = Rbd()
    reader.recheck_inventory = None
    result = collect_report(
        inventory(), reader, limits=CollectionLimits(), clock=lambda: NOW
    )
    assert result.collection_errors
    assert all(p.allocated_bytes is None for p in result.projects)


def test_future_observation_rejected():
    import pytest

    with pytest.raises(ValueError):
        ReadResult(status="complete", collected_at=NOW.replace(year=2099), value=0)


def test_collection_never_opens_application_database(monkeypatch):
    from app.core import database

    def forbidden(*args, **kwargs):
        raise AssertionError("storage observation opened application DB")

    monkeypatch.setattr(database, "SessionLocal", forbidden)
    result = collect_report(
        inventory(), Rbd(), limits=CollectionLimits(), clock=lambda: NOW
    )
    assert result.projects[0].allocated_bytes == 0


def test_atomic_output_is_private_and_roundtrips(tmp_path):
    import runpy
    from pathlib import Path

    module = runpy.run_path(
        str(
            Path(__file__).resolve().parents[3]
            / "scripts"
            / "collect_kubevirt_storage.py"
        )
    )
    path = tmp_path / "report.json"
    report = collect_report(
        inventory(), Rbd(), limits=CollectionLimits(), clock=lambda: NOW
    )
    module["atomic_report"](path, report)
    assert path.stat().st_mode & 0o777 == 0o600
    assert NativeStorageReport.model_validate_json(path.read_text()) == report
