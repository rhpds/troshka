"""Explicit-target operator CLI never falls back to application DB settings."""

import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.fixture
def cli(monkeypatch):
    directory = Path(__file__).resolve().parents[3] / "scripts"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(
        "native_import_cli", directory / "import_kubevirt_storage.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_import_cli_requires_explicit_target_even_with_application_url(
    cli, monkeypatch, capsys
):
    monkeypatch.delenv("TROSHKA_NATIVE_REPORT_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "TROSHKA_DATABASE__URL", "postgresql://never-use-application-url"
    )
    with pytest.raises(SystemExit) as error:
        cli.main(["--target-id", str(uuid4()), "configure-target"])
    assert error.value.code == 2
    assert "explicit --database-url" in capsys.readouterr().err


def test_operator_files_reject_symlinks_and_writable_inputs(cli, tmp_path):
    source = tmp_path / "report.json"
    source.write_text("{}")
    source.chmod(0o666)
    with pytest.raises(ValueError):
        cli.read_operator_file(source)
    source.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(OSError):
        cli.read_operator_file(link)
    assert cli.read_operator_file(source) == {}


def test_cli_target_uuid_and_import_roundtrip(
    cli, native_case, monkeypatch, tmp_path, capsys
):
    from app.models.kubevirt_storage import NativeStorageSnapshot

    q = native_case
    monkeypatch.setenv(
        "TROSHKA_NATIVE_REPORT_DATABASE_URL", str(q.sessions.kw["bind"].url)
    )
    report_path = tmp_path / "report.json"
    report_path.write_text(q.report.model_dump_json())
    report_path.chmod(0o600)
    args = [
        "--target-id",
        q.target_id,
        "import",
        "--report",
        str(report_path),
        "--enrollment-id",
        q.enrollment_id,
        "--enrollment-digest",
        q.digest,
    ]
    assert cli.main(args) == 0
    with q.sessions() as db:
        assert db.query(NativeStorageSnapshot).count() == 1
    args[1] = str(uuid4())
    assert cli.main(args) == 1
    output = capsys.readouterr()
    assert str(q.sessions.kw["bind"].url) not in output.err
    assert "operation failed" in output.err


def test_cli_prepare_uses_only_requested_project_dependencies(
    cli, native_case, monkeypatch, tmp_path
):
    q = native_case
    monkeypatch.setenv(
        "TROSHKA_NATIVE_REPORT_DATABASE_URL", str(q.sessions.kw["bind"].url)
    )
    output = tmp_path / "enrollment-request.json"
    project_id = str(q.report.project_roots[0].project_id)
    assert (
        cli.main(
            [
                "--target-id",
                q.target_id,
                "prepare",
                "--project-id",
                project_id,
                "--output",
                str(output),
            ]
        )
        == 0
    )
    body = json.loads(output.read_text())
    assert [p["project_id"] for p in body["projects"]] == [project_id]
    assert output.stat().st_mode & 0o777 == 0o600


from tests.test_kubevirt_storage_reports import native_case as native_case_fixture

native_case = native_case_fixture


def test_collector_report_cannot_bootstrap_cli_enrollment(
    cli, native_case, monkeypatch, tmp_path
):
    from app.models.kubevirt_storage import NativeEnrollment

    q = native_case
    monkeypatch.setenv(
        "TROSHKA_NATIVE_REPORT_DATABASE_URL", str(q.sessions.kw["bind"].url)
    )
    path = tmp_path / "not-enrollment.json"
    path.write_text(q.report.model_dump_json())
    path.chmod(0o600)
    assert (
        cli.main(["--target-id", q.target_id, "enroll", "--enrollment", str(path)]) == 1
    )
    with q.sessions() as db:
        assert db.query(NativeEnrollment).count() == 1
