"""Explicit-target operator CLI never falls back to application DB settings."""

import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest


def test_reconciliation_cli_requires_review_and_explicit_authority(
    cli, monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from app import services

    calls = []
    prepared = {"request": {"request_id": str(uuid4())}}
    review = {"review_digest": "a" * 64}
    service = SimpleNamespace(
        prepare=lambda db, ids, **kw: calls.append(("prepare", ids, kw)) or prepared,
        review=lambda db, value: calls.append(("review", value)) or review,
        apply=lambda db, value, **kw: calls.append(("apply", value, kw))
        or ["successor"],
    )
    monkeypatch.setattr(
        services, "native_custody_reconciliation", service, raising=False
    )
    target, project = str(uuid4()), str(uuid4())
    output = tmp_path / "prepared.json"
    parser = cli._parser()
    args = parser.parse_args(
        [
            "--target-id",
            target,
            "prepare-reconciliation",
            "--project-id",
            project,
            "--cleanup-authority-project-id",
            project,
            "--reason",
            "Reviewed ownership",
            "--output",
            str(output),
        ]
    )
    cli._reconciliation_dispatch(None, args)
    assert json.loads(output.read_text()) == prepared
    assert output.stat().st_mode & 0o777 == 0o600
    assert calls[-1] == (
        "prepare",
        [project],
        {
            "cleanup_authority_project_ids": [project],
            "reason": "Reviewed ownership",
        },
    )
    reviewed = tmp_path / "review.json"
    args = parser.parse_args(
        [
            "--target-id",
            target,
            "review-reconciliation",
            "--enrollment",
            str(output),
            "--output",
            str(reviewed),
        ]
    )
    cli._reconciliation_dispatch(None, args)
    assert json.loads(reviewed.read_text()) == review
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--target-id",
                target,
                "apply-reconciliation",
                "--enrollment",
                str(output),
            ]
        )
    args = parser.parse_args(
        [
            "--target-id",
            target,
            "apply-reconciliation",
            "--enrollment",
            str(output),
            "--review-digest",
            "a" * 64,
            "--cleanup-authority-project-id",
            project,
        ]
    )
    assert cli._reconciliation_dispatch(None, args) == ["successor"]
    assert calls[-1] == (
        "apply",
        prepared,
        {
            "expected_target_id": target,
            "cleanup_authority_project_ids": [project],
            "review_digest": "a" * 64,
        },
    )


@pytest.mark.parametrize("command", ["prepare-reconciliation", "apply-reconciliation"])
def test_reconciliation_cli_passes_only_explicit_retirement_selection(
    cli, monkeypatch, tmp_path, command
):
    from types import SimpleNamespace

    from app import services

    calls = []
    prepared = {"request": {"request_id": str(uuid4())}}
    service = SimpleNamespace(
        prepare=lambda db, ids, **kw: calls.append(kw) or prepared,
        apply=lambda db, value, **kw: calls.append(kw) or [],
    )
    monkeypatch.setattr(
        services, "native_custody_reconciliation", service, raising=False
    )
    selection = [{"receipt_id": str(uuid4()), "digest": "b" * 64}]
    refs = tmp_path / "retirements.json"
    refs.write_text(json.dumps(selection))
    refs.chmod(0o600)
    manifest = tmp_path / "verified.json"
    manifest.write_text(json.dumps(prepared))
    manifest.chmod(0o600)
    argv = ["--target-id", str(uuid4()), command, "--binding-retirements", str(refs)]
    if command == "prepare-reconciliation":
        argv += [
            "--project-id",
            str(uuid4()),
            "--reason",
            "Reviewed absent old disks",
            "--output",
            str(tmp_path / "out.json"),
        ]
    else:
        argv += ["--enrollment", str(manifest), "--review-digest", "a" * 64]
    cli._reconciliation_dispatch(None, cli._parser().parse_args(argv))
    assert calls[-1]["binding_retirements"] == selection


def test_binding_retirement_prepare_writes_private_request(cli, monkeypatch, tmp_path):
    import native_binding_retirement_cli as retirement_cli

    calls = []
    prepared = {"request_id": str(uuid4())}
    monkeypatch.setattr(
        retirement_cli.retirement,
        "prepare",
        lambda *args, **kw: calls.append((args, kw)) or prepared,
    )
    output = tmp_path / "retirement.json"
    target, project = str(uuid4()), str(uuid4())
    args = cli._parser().parse_args(
        [
            "--target-id",
            target,
            "prepare-binding-retirement",
            "--project-id",
            project,
            "--pvc-uid",
            "old-pvc",
            "--administrator",
            "operator",
            "--reason",
            "Old disk absent",
            "--output",
            str(output),
        ]
    )
    assert retirement_cli.dispatch(None, args) == prepared["request_id"]
    assert calls == [
        (
            (None, project, "old-pvc"),
            {"administrator": "operator", "reason": "Old disk absent", "root": None},
        )
    ]
    assert json.loads(output.read_text()) == prepared
    assert output.stat().st_mode & 0o777 == 0o600


def test_binding_retirement_wrong_target_never_reaches_provider_or_apply(
    cli, monkeypatch
):
    from types import SimpleNamespace

    import native_binding_retirement_cli as retirement_cli

    def forbidden(*args, **kwargs):
        raise AssertionError("retirement must not run against an unselected target")

    monkeypatch.setattr(retirement_cli, "dispatch", forbidden)
    for command in retirement_cli.COMMANDS:
        args = SimpleNamespace(command=command, target_id=str(uuid4()))
        with pytest.raises(ValueError, match="target UUID"):
            cli._dispatch(
                SimpleNamespace(get=lambda *a: SimpleNamespace(target_id=str(uuid4()))),
                args,
            )


def test_binding_retirement_apply_requires_exact_review_digest(cli):
    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            [
                "--target-id",
                str(uuid4()),
                "apply-binding-retirement",
                "--evidence",
                "proof.json",
                "--output",
                "reference.json",
            ]
        )


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


def test_online_cli_requires_explicit_attestation(cli):
    with pytest.raises(SystemExit) as error:
        cli._parser().parse_args(
            [
                "--target-id",
                str(uuid4()),
                "verify-online",
                "--request",
                "request.json",
                "--pins",
                "pins.json",
                "--backend-config",
                "backend.json",
                "--output",
                "receipt.json",
            ]
        )
    assert error.value.code == 2


def test_online_reader_rejects_wrong_cluster_and_closes_transport(cli, monkeypatch):
    from contextlib import ExitStack
    from types import SimpleNamespace

    closed = []
    reader = SimpleNamespace(
        api=SimpleNamespace(close=lambda: closed.append(True)),
        cluster_identity=lambda: "unrelated-cluster",
    )
    monkeypatch.setattr(cli, "KubernetesReader", lambda *args, **kwargs: reader)
    provider = SimpleNamespace(cluster_identity=lambda: "configured-cluster")
    with pytest.raises(ValueError, match="configured provider cluster"):
        with ExitStack() as cleanup:
            cli._online_readers(
                SimpleNamespace(kubeconfig="protected-kubeconfig"),
                {"provider": provider},
                {"backend": object()},
                SimpleNamespace(collector_ids=["collector"], cache_roots=[]),
                SimpleNamespace(max_objects=100),
                123,
                cleanup,
            )
    assert closed == [True]


def test_online_custody_uses_provider_transport_not_collector(cli, monkeypatch):
    from contextlib import ExitStack
    from types import SimpleNamespace

    from app.services.native_online_enrollment import ReadOnlyManaged

    collector_api = SimpleNamespace(close=lambda: None)
    provider_api = object()
    kube = SimpleNamespace(api=collector_api, cluster_identity=lambda: "cluster")
    provider = SimpleNamespace(api=provider_api, cluster_identity=lambda: "cluster")
    monkeypatch.setattr(cli, "KubernetesReader", lambda *args, **kwargs: kube)
    with ExitStack() as cleanup:
        readers = cli._online_readers(
            SimpleNamespace(kubeconfig="protected-kubeconfig"),
            {"provider": provider},
            {"backend": object()},
            SimpleNamespace(collector_ids=["collector"], cache_roots=[]),
            SimpleNamespace(max_objects=100),
            123,
            cleanup,
        )
        assert readers["provider"].api is collector_api
        assert ReadOnlyManaged(readers["provider"])._reader.api is provider_api


@pytest.mark.parametrize("accounting_only", [False, True])
def test_online_cli_private_prepare_and_explicit_target_apply(
    cli, native_case, monkeypatch, tmp_path, accounting_only
):
    from types import SimpleNamespace

    from app import services

    q = native_case
    monkeypatch.setenv(
        "TROSHKA_NATIVE_REPORT_DATABASE_URL", str(q.sessions.kw["bind"].url)
    )
    project_id = str(q.report.project_roots[0].project_id)
    prepared = {
        "request": q.request.model_dump(mode="json"),
        "topologies": {project_id: {"nodes": []}},
    }
    calls = []

    def prepare(db, ids, *, accounting_only_project_ids):
        calls.append(("prepare", ids, accounting_only_project_ids))
        return prepared

    def apply(db, receipt, *, expected_target_id, preserve_existing=False):
        calls.append(("apply", receipt, expected_target_id, preserve_existing))
        return ["custody-receipt"]

    monkeypatch.setattr(
        services,
        "native_online_enrollment",
        SimpleNamespace(prepare=prepare, apply=apply),
        raising=False,
    )
    output = tmp_path / "prepared.json"
    assert (
        cli.main(
            [
                "--target-id",
                q.target_id,
                "prepare-online",
                "--project-id",
                project_id,
                "--output",
                str(output),
                *(
                    ["--accounting-only-project-id", project_id]
                    if accounting_only
                    else []
                ),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text()) == prepared
    assert output.stat().st_mode & 0o777 == 0o600
    assert calls == [("prepare", [project_id], [project_id] if accounting_only else [])]
    # A different database target fails before touching the custody service.
    assert (
        cli.main(
            ["--target-id", str(uuid4()), "enroll-online", "--enrollment", str(output)]
        )
        == 1
    )
    assert len(calls) == 1
    assert (
        cli.main(
            ["--target-id", q.target_id, "enroll-online", "--enrollment", str(output)]
        )
        == 0
    )
    assert calls[-1] == ("apply", prepared, q.target_id, False)
    assert (
        cli.main(
            [
                "--target-id",
                q.target_id,
                "enroll-online",
                "--enrollment",
                str(output),
                "--preserve-existing",
            ]
        )
        == 0
    )
    assert calls[-1] == ("apply", prepared, q.target_id, True)


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

    from kubernetes import client

    from app.models.provider import Provider
    from app.schemas.kubevirt_storage import ReadResult
    from app.services.kubevirt_storage_transport import BoundedProcess
    from tests.test_kubevirt_storage_inventory import static_kubeconfig

    q = native_case
    state = SimpleNamespace(
        cluster="cluster",
        pod_uid="toolbox-uid",
        pool_id=1,
        command_status="complete",
        clients=[],
        commands=[],
        configurations=[],
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
            state.configurations.append(configuration)
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
                        "metadata": {
                            "uid": root.project_cr_uid,
                            "generation": root.deployment_generation,
                        },
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
    monkeypatch.setattr(BoundedProcess, "run", run)
    monkeypatch.setenv(
        "TROSHKA_NATIVE_REPORT_DATABASE_URL", str(q.sessions.kw["bind"].url)
    )
    state.kubeconfig_path, state.kubeconfig_body = static_kubeconfig(tmp_path)
    state.backend = {
        "ceph_fsid": q.pins.pool_keys[0].ceph_fsid,
        "pools": [{"pool_id": 1, "name": "pool", "csi_cluster_id": "rook"}],
        "mode": "toolbox",
        "kubeconfig": str(state.kubeconfig_path),
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
    assert s.configurations[1].host == "https://storage.invalid"
    assert s.configurations[1].api_key["BearerToken"] == "Bearer test-only-token"
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
    assert len(s.clients) == 1
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


def test_protected_pins_carry_explicit_initialization_authorization(
    verification_case, cli, tmp_path
):
    from app.models.kubevirt_storage import NativeEnrollment
    from app.models.quota import QuotaReservation
    from app.services.kubevirt_storage_enrollment import digest
    from tests.test_native_initialization_evidence import synthetic_profile

    s = verification_case
    pins = s.case.pins.model_dump(mode="json")
    profile = synthetic_profile()
    profile["target"].update(
        provider_id=str(s.case.request.projects[0].provider_id),
        cluster_key="cluster",
        ceph_fsid=s.case.pins.pool_keys[0].ceph_fsid,
    )
    pins["initialization_evidence"] = profile
    pins["domain_extension"] = {
        "version": "native-domain-extension-v1",
        "previous_enrollment_id": s.case.enrollment_id,
        "previous_enrollment_digest": s.case.digest,
        "initialization_authorization_digest": digest(profile),
    }
    pins_path = tmp_path / "pins.json"
    pins_path.write_text(json.dumps(pins))
    pins_path.chmod(0o600)
    assert s.verify() == 0
    body = json.loads(s.output.read_text())
    assert body["pins"]["domain_extension"] == pins["domain_extension"]
    assert body["pins"]["initialization_evidence"] == profile
    assert (
        cli.main(
            ["--target-id", s.case.target_id, "enroll", "--enrollment", str(s.output)]
        )
        == 0
    )
    with s.case.sessions() as db:
        assert db.query(NativeEnrollment).count() == 2
        assert db.query(QuotaReservation).count() == 0


def test_protected_enroll_rejects_omitted_domain_extension(
    verification_case, cli, tmp_path
):
    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeEnrollment,
        NativeProjectRoot,
    )
    from tests.test_quota_native_storage import extension_manifest

    s, q = verification_case, verification_case.case
    q.project_ids = [str(root.project_id) for root in q.report.project_roots]
    with q.sessions() as db:
        manifest, _ = extension_manifest(db, q)
        db.commit()
    s.case.pins = manifest.pins
    (tmp_path / "request.json").write_text(manifest.request.model_dump_json())
    body = manifest.pins.model_dump(mode="json")
    body.pop("domain_extension")
    (tmp_path / "pins.json").write_text(json.dumps(body))
    assert s.verify() == 0
    assert (
        cli.main(["--target-id", q.target_id, "enroll", "--enrollment", str(s.output)])
        == 1
    )
    with q.sessions() as db:
        assert (
            db.get(NativeCollectionDomain, str(q.pins.domain_id)).enrollment_id
            == q.enrollment_id
        )
        assert db.query(NativeEnrollment).count() == 1
        assert db.query(NativeProjectRoot).count() == 2


def test_verify_direct_requires_explicit_credentials(verification_case):
    s = verification_case
    s.backend = {key: s.backend[key] for key in ("ceph_fsid", "pools")}
    s.backend["mode"] = "direct"
    assert s.verify() == 1
    assert not s.commands
    assert not s.clients


def test_verify_explicit_collector_transport_uses_verified_tls(verification_case):
    s = verification_case
    assert s.verify(["--kubeconfig", str(s.kubeconfig_path)]) == 0
    assert s.configurations[0].host == "https://storage.invalid"
    assert s.configurations[0].verify_ssl is True
    assert all(client.closed for client in s.clients)


def test_verify_explicit_collector_transport_still_rejects_wrong_cluster(
    verification_case,
):
    s = verification_case
    s.cluster = "unrelated-cluster"
    assert s.verify(["--kubeconfig", str(s.kubeconfig_path)]) == 1
    assert not s.output.exists()
    assert all(client.closed for client in s.clients)


def test_verify_collector_transport_rejects_multiple_providers(
    cli, verification_case, monkeypatch
):
    s = verification_case
    monkeypatch.setattr(cli, "requested_providers", lambda request: {"one", "two"})
    assert s.verify(["--kubeconfig", str(s.kubeconfig_path)]) == 1
    assert not s.clients
    assert not s.output.exists()


def test_verify_collector_transport_rejects_insecure_tls(verification_case):
    import yaml

    s = verification_case
    s.kubeconfig_body["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
    s.kubeconfig_path.write_text(yaml.safe_dump(s.kubeconfig_body))
    assert s.verify(["--kubeconfig", str(s.kubeconfig_path)]) == 1
    assert not s.clients
    assert not s.output.exists()


@pytest.mark.parametrize("auth", ["exec", "auth-provider"])
def test_verify_rejects_auth_helpers_before_any_api_or_command(
    verification_case, auth, capsys
):
    import yaml

    s = verification_case
    s.kubeconfig_body["users"][0]["user"] = {
        auth: {"command": "private-helper-command", "name": "gcp"}
    }
    s.kubeconfig_path.write_text(yaml.safe_dump(s.kubeconfig_body))
    assert s.verify() == 1
    assert not s.clients
    assert not s.commands
    assert not s.output.exists()
    assert "private-helper-command" not in capsys.readouterr().err
