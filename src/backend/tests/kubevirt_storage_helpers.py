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
