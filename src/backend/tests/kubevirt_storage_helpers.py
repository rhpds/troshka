"""Sanitized native storage recordings shared by collector contract tests."""

import json
from pathlib import Path


def report_fixture(name):
    from app.schemas.kubevirt_storage import NativeStorageReport

    return NativeStorageReport.model_validate(
        json.loads(
            (Path(__file__).parent / "fixtures" / "kubevirt_storage" / name).read_text()
        )
    )


def with_retained_snapshot(report, image_id="child-1"):
    """Emulate v1 evidence with snapshot inventory but old complete aggregates."""
    from app.schemas.kubevirt_storage import SnapshotKey

    images = [
        image.model_copy(
            update={"snapshots": [SnapshotKey(image_key=image.key, snapshot_id="123")]}
        )
        if image.key.image_id == image_id
        else image
        for image in report.images
    ]
    return report.model_copy(update={"images": images})
