import pytest

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


def test_final_graph_recheck_detects_consumer_added_during_later_du():
    class LateConsumer(Rbd):
        late = False
        usage_calls = []

        def image_usage(self, image):
            self.usage_calls.append(image)
            if image == key("child-2"):
                self.late = True
            return super().image_usage(image)

        def image_snapshots(self, image):
            rows = (
                [{"id": 1, "name": "base-snap", "namespace": {"type": "user"}}]
                if image == key("child-1")
                else []
            )
            return ReadResult(status="complete", value=rows, collected_at=NOW)

        def image_children(self, image, snapshot):
            rows = (
                [{"pool": "pool", "pool_namespace": "", "id": "foreign"}]
                if self.late
                else []
            )
            return ReadResult(status="complete", value=rows, collected_at=NOW)

    reader = LateConsumer()
    result = collect_report(
        inventory(), reader, limits=CollectionLimits(), clock=lambda: NOW
    )
    assert result.projects[0].allocated_bytes is None
    assert result.projects[1].allocated_bytes == 0
    assert any(
        b.code == "backend_graph_changed" and b.image_key == key("child-1")
        for b in result.collection_errors
    )
    assert len(reader.usage_calls) == 3
    assert all(i.collected_at == NOW for i in result.images)


@pytest.mark.parametrize("failure", ["changed", "unavailable"])
def test_final_fsid_failure_invalidates_project_and_platform_measurements(failure):
    reader = Rbd()
    reader.image_usage = lambda image: ReadResult(
        status="complete",
        collected_at=NOW,
        value={
            "images": [{"id": image.image_id, "used_size": 7, "provisioned_size": 100}]
        },
    )
    calls = []

    def identity():
        calls.append(True)
        if len(calls) > 1 and failure == "unavailable":
            return ReadResult(status="timeout", collected_at=NOW)
        return ReadResult(
            status="complete",
            value=key("x").ceph_fsid if len(calls) == 1 else "other-fsid",
            collected_at=NOW,
        )

    reader.cluster_identity = identity
    result = collect_report(
        inventory(), reader, limits=CollectionLimits(), clock=lambda: NOW
    )
    assert result.platform_pools[0].allocated_bytes is None
    assert result.platform_pools[0].known_allocated_bytes == 0
    assert all(p.allocated_bytes is None for p in result.projects)
    assert all(p.known_allocated_bytes == 0 for p in result.projects)
    assert all(p.measured_images == 0 for p in result.projects)
    assert all(p.coverage == "unavailable" for p in result.projects)


def test_inventory_recheck_failure_invalidates_platform_total():
    reader = Rbd()
    reader.image_usage = lambda image: ReadResult(
        status="complete",
        collected_at=NOW,
        value={
            "images": [{"id": image.image_id, "used_size": 7, "provisioned_size": 100}]
        },
    )
    reader.recheck_inventory = None
    result = collect_report(
        inventory(), reader, limits=CollectionLimits(), clock=lambda: NOW
    )
    assert result.platform_pools[0].allocated_bytes is None
    assert result.platform_pools[0].known_allocated_bytes == 7
    assert [p.known_allocated_bytes for p in result.projects] == [7, 7]
    assert all(
        p.measured_images == 1 and p.coverage == "partial" for p in result.projects
    )


def test_final_metadata_checks_cover_ids_parents_snapshots_and_unavailable_reads():
    for drift in ("id", "parent", "snapshots", "unavailable"):

        class LateDrift(Rbd):
            late = False
            usage_calls = 0

            def image_usage(self, image):
                self.usage_calls += 1
                if image == key("child-2"):
                    self.late = True
                return super().image_usage(image)

            def image_info(self, image):
                result = super().image_info(image)
                if not self.late or image != key("child-1"):
                    return result
                if drift == "unavailable":
                    return ReadResult(status="timeout", collected_at=NOW)
                value = dict(result.value)
                if drift == "id":
                    value["id"] = "replacement"
                elif drift == "parent":
                    value["parent"] = {
                        "pool": "pool",
                        "pool_namespace": "",
                        "id": "cache",
                        "snap_id": 1,
                    }
                return result.model_copy(update={"value": value})

            def image_snapshots(self, image):
                rows = (
                    [{"id": 1, "name": "new-snap", "namespace": {"type": "user"}}]
                    if self.late and drift == "snapshots" and image == key("child-1")
                    else []
                )
                return ReadResult(status="complete", collected_at=NOW, value=rows)

        reader = LateDrift()
        result = collect_report(
            inventory(), reader, limits=CollectionLimits(), clock=lambda: NOW
        )
        assert result.projects[0].allocated_bytes is None, drift
        assert result.projects[1].allocated_bytes == 0, drift
        assert reader.usage_calls == 3
        assert all(i.collected_at == NOW for i in result.images)
