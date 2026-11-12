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


@pytest.fixture
def verification_case(cli, native_case, monkeypatch, tmp_path):
    """Real CLI/enrollment/transports; replace only Kubernetes and command I/O."""
    from types import SimpleNamespace

    from kubernetes import client, config

    from app.models.provider import Provider
    from app.schemas.kubevirt_storage import ReadResult
    from app.services.kubevirt_storage_transport import BoundedProcess

    q = native_case
    state = SimpleNamespace(
        cluster="cluster",
        pod_uid="toolbox-uid",
        pool_id=1,
        command_status="complete",
        clients=[],
        commands=[],
        kubeconfigs=[],
        timeouts=[],
        advance=None,
        fail_client=None,
    )
    with q.sessions() as db:
        provider = db.get(Provider, str(q.request.projects[0].provider_id))
        provider.set_credentials(
            {"api_url": "https://provider.invalid", "token": "test-only"}
        )
        db.commit()

    class Api:
        def __init__(self, configuration):
            self.rest_client = SimpleNamespace(
                pool_manager=SimpleNamespace(connection_pool_kw={})
            )
            self.closed = False
            state.clients.append(self)

        def sanitize_for_serialization(self, value):
            return value

        def close(self):
            self.closed = True

    class Core:
        def __init__(self, api):
            if state.fail_client == len(state.clients):
                raise RuntimeError("secret-command-stderr")
            self.api = api

        def read_namespace(self, name, *, _request_timeout):
            state.timeouts.append(_request_timeout)
            if state.advance:
                state.advance()
            if name == "kube-system":
                uid = state.cluster
            elif name == "trusted-cache":
                uid = "cache-ns"
            else:
                uid = next(
                    r.namespace_uid for r in q.pins.project_roots if r.namespace == name
                )
            return {"metadata": {"uid": uid}}

        def read_namespaced_pod(self, name, namespace, *, _request_timeout):
            state.timeouts.append(_request_timeout)
            return {
                "metadata": {"uid": state.pod_uid},
                "spec": {"containers": [{"name": "rook-ceph-tools"}]},
            }

    class Custom:
        def __init__(self, api):
            self.api = api

        def list_namespaced_custom_object(
            self, group, version, namespace, plural, **kwargs
        ):
            state.timeouts.append(kwargs["_request_timeout"])
            root = next(r for r in q.pins.project_roots if r.namespace == namespace)
            return {
                "items": [
                    {
                        "metadata": {"uid": root.project_cr_uid},
                        "spec": {"projectId": str(root.project_id)},
                    }
                ]
            }

    def run(process, argv, **kwargs):
        state.commands.append((argv, kwargs, process.scan_deadline))
        if argv[-1] == "true":
            value = ""
        elif argv[-2:] == ["ceph", "fsid"] or argv[-1] == "fsid":
            value = q.pins.pool_keys[0].ceph_fsid
        else:
            assert argv[-4:] == ["osd", "lspools", "--format", "json"]
            value = json.dumps([{"poolnum": state.pool_id, "poolname": "pool"}])
        return ReadResult(
            status=state.command_status, value=value, collected_at=q.report.completed_at
        )

    monkeypatch.setattr(client, "ApiClient", Api)
    monkeypatch.setattr(client, "CoreV1Api", Core)
    monkeypatch.setattr(client, "CustomObjectsApi", Custom)
    monkeypatch.setattr(
        config, "load_kube_config", lambda **kwargs: state.kubeconfigs.append(kwargs)
    )
    monkeypatch.setattr(BoundedProcess, "run", run)
    monkeypatch.setenv(
        "TROSHKA_NATIVE_REPORT_DATABASE_URL", str(q.sessions.kw["bind"].url)
    )
    state.backend = {
        "ceph_fsid": q.pins.pool_keys[0].ceph_fsid,
        "pools": [{"pool_id": 1, "name": "pool", "csi_cluster_id": "rook"}],
        "mode": "toolbox",
        "kubeconfig": "/private/tmp/operator-kubeconfig",
        "toolbox": {
            "cluster_key": "cluster",
            "namespace": "openshift-storage",
            "pod_name": "rook-ceph-tools-test",
            "pod_uid": "toolbox-uid",
            "container": "rook-ceph-tools",
            "ceph_fsid": q.pins.pool_keys[0].ceph_fsid,
            "allowed_pools": ["pool"],
        },
    }
    state.output = tmp_path / "verified.json"
    state.args = ["--target-id", q.target_id, "verify", "--output", str(state.output)]
    for name, body in (
        ("request", q.request.model_dump(mode="json")),
        ("pins", q.pins.model_dump(mode="json")),
    ):
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps(body))
        path.chmod(0o600)
        state.args += ["--" + name, str(path)]

    def verify(extra=()):
        path = tmp_path / "backends.json"
        path.write_text(json.dumps([state.backend]))
        path.chmod(0o600)
        return cli.main([*state.args, "--backend-config", str(path), *extra])

    state.verify = verify
    state.case = q
    return state


def test_verify_existing_toolbox_produces_manifest_with_bounded_identity_reads(
    verification_case,
):
    from app.models.quota import (
        QuotaAllocation,
        QuotaCharge,
        QuotaReadiness,
        QuotaReservation,
    )

    s = verification_case
    assert s.verify() == 0
    body = json.loads(s.output.read_text())
    assert body["request"]["request_id"] == str(s.case.request.request_id)
    assert body["pins"]["pool_keys"] == s.case.pins.model_dump(mode="json")["pool_keys"]
    assert s.kubeconfigs[0]["config_file"] == "/private/tmp/operator-kubeconfig"
    assert all(client.closed for client in s.clients)
    assert all(
        client.rest_client.pool_manager.connection_pool_kw["retries"] == 0
        for client in s.clients
    )
    assert all(0 < timeout <= 15 for timeout in s.timeouts)
    assert all(deadline is not None for _, _, deadline in s.commands)
    assert {argv[0] for argv, _, _ in s.commands} == {"oc"}
    with s.case.sessions() as db:
        assert db.query(QuotaReservation).count() == 0
        assert db.query(QuotaAllocation).count() == 0
        assert db.query(QuotaCharge).count() == 0
        assert db.get(QuotaReadiness, "baseline").ready is False


@pytest.mark.parametrize("explicit_mode", [False, True])
def test_verify_direct_compatibility(verification_case, explicit_mode):
    s = verification_case
    s.backend = {key: s.backend[key] for key in ("ceph_fsid", "pools")}
    s.backend.update(
        ceph_conf="/private/tmp/ceph.conf", ceph_keyring="/private/tmp/ceph.keyring"
    )
    if explicit_mode:
        s.backend["mode"] = "direct"
    assert s.verify() == 0
    assert s.kubeconfigs == []
    assert {argv[0] for argv, _, _ in s.commands} == {"ceph"}
    assert all(deadline is not None for _, _, deadline in s.commands)
    assert all(client.closed for client in s.clients)


@pytest.mark.parametrize(
    "field,value",
    [
        ("cluster", "other"),
        ("pod_uid", "replacement"),
        ("pool_id", 9),
        ("command_status", "timeout"),
    ],
)
def test_verify_toolbox_rejects_changed_pins_and_timeout(
    verification_case, field, value, capsys
):
    s = verification_case
    setattr(s, field, value)
    assert s.verify() == 1
    assert not s.output.exists()
    assert all(client.closed for client in s.clients)
    assert "operation failed" in capsys.readouterr().err
    if field == "command_status":
        assert len(s.commands) == 1  # Startup timeout probe is never retried.


@pytest.mark.parametrize(
    "change",
    [
        "missing_kubeconfig",
        "relative_kubeconfig",
        "foreign_fsid",
        "outside_allowlist",
        "mixed_credentials",
        "missing_pin",
    ],
)
def test_verify_rejects_ambiguous_backend_configuration(verification_case, change):
    s = verification_case
    if change == "missing_kubeconfig":
        del s.backend["kubeconfig"]
    elif change == "relative_kubeconfig":
        s.backend["kubeconfig"] = "relative-config"
    elif change == "foreign_fsid":
        s.backend["toolbox"]["ceph_fsid"] = str(uuid4())
    elif change == "outside_allowlist":
        s.backend["toolbox"]["allowed_pools"] = ["other"]
    elif change == "mixed_credentials":
        s.backend["ceph_keyring"] = "/private/tmp/other"
    else:
        del s.backend["toolbox"]["namespace"]
    assert s.verify() == 1
    assert not s.output.exists()
    assert not s.commands
    assert all(client.closed for client in s.clients)


def test_verify_toolbox_never_substitutes_operator_credentials_for_provider(
    verification_case,
):
    from app.models.provider import Provider

    s = verification_case
    with s.case.sessions() as db:
        db.get(Provider, str(s.case.request.projects[0].provider_id)).credentials = None
        db.commit()
    assert s.verify() == 1
    assert not s.output.exists()
    assert not s.commands


def test_verify_provider_reads_share_overall_deadline(
    cli, verification_case, monkeypatch
):
    s = verification_case
    clock = [100.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    s.advance = lambda: clock.__setitem__(0, clock[0] + 11)
    s.backend = {key: s.backend[key] for key in ("ceph_fsid", "pools")}
    s.backend.update(
        ceph_conf="/private/tmp/ceph.conf", ceph_keyring="/private/tmp/ceph.keyring"
    )
    assert (
        s.verify(
            ["--verification-timeout-seconds", "10", "--command-timeout-seconds", "3"]
        )
        == 1
    )
    assert not s.output.exists()
    assert s.timeouts == [3]
    assert all(client.closed for client in s.clients)


@pytest.mark.parametrize("client_number", [1, 2])
def test_verify_closes_clients_when_initialization_fails(
    verification_case, client_number, capsys
):
    s = verification_case
    s.fail_client = client_number
    assert s.verify() == 1
    assert len(s.clients) == client_number
    assert all(client.closed for client in s.clients)
    assert not s.output.exists()
    assert "secret-command-stderr" not in capsys.readouterr().err


def test_verify_requires_independently_prepared_request(verification_case):
    from app.models.kubevirt_storage import NativeEnrollmentRequest

    s = verification_case
    with s.case.sessions() as db:
        db.delete(db.get(NativeEnrollmentRequest, str(s.case.request.request_id)))
        db.commit()
    assert s.verify() == 1
    assert not s.output.exists()
    assert not s.clients
    assert not s.commands


def test_verify_large_command_budget_keeps_kubernetes_requests_bounded(
    verification_case,
):
    s = verification_case
    assert s.verify(["--command-timeout-seconds", "3600"]) == 0
    assert all(0 < timeout <= 15 for timeout in s.timeouts)


def test_verify_short_budget_applies_to_both_kubernetes_identities(verification_case):
    s = verification_case
    assert s.verify(["--command-timeout-seconds", "3"]) == 0
    assert all(0 < timeout <= 3 for timeout in s.timeouts)
    assert (
        s.commands[-1][1]["timeout"] == 8
    )  # Remote command plus transport shutdown allowance.


def test_verify_direct_requires_explicit_credentials(verification_case):
    s = verification_case
    s.backend = {key: s.backend[key] for key in ("ceph_fsid", "pools")}
    s.backend["mode"] = "direct"
    assert s.verify() == 1
    assert not s.commands
    assert not s.clients
