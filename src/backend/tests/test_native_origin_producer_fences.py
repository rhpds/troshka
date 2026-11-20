"""Producer acknowledgements are exact committed storage receipts, never claims."""

from copy import deepcopy

import pytest

from tests import test_native_origin_custody as custody_tests

cleanup_case = custody_tests.cleanup_case
deploy_case = custody_tests.deploy_case
isolated_quota_database = custody_tests.isolated_quota_database
native_ledger = custody_tests.native_ledger
online_case = custody_tests.online_case
resume_case = custody_tests.resume_case


@pytest.fixture
def checkpoint_case(online_case, monkeypatch):
    from app.services import native_deployment_origin as origin
    from app.services import native_origin_checkpoints as checkpoints
    from app.services import native_origin_custody as custody
    from app.services.kubevirt_storage_enrollment import digest_cr_spec

    original = origin.prepare
    api = online_case.apis[online_case.project_ids[0]]
    root = api.rows("TroshkaProject", None)[0]
    with online_case.sessions() as db:
        identity = checkpoints.initialize(db, root["spec"])
        db.commit()
    api.values["TroshkaProject", root["metadata"]["name"]] = root

    def prepare(db, project, provider, descriptor):
        descriptor.update(
            origin_id=identity, cr_spec_digest=digest_cr_spec(root["spec"])
        )
        return original(db, project, provider, descriptor)

    monkeypatch.setattr(origin, "prepare", prepare)
    q = custody_tests.custody_case.__wrapped__(online_case, monkeypatch)
    monkeypatch.setattr(custody, "domain_for", lambda db, intent: str(q.pins.domain_id))
    monkeypatch.setattr(
        custody,
        "verify_configured",
        lambda value: custody.verify(value, q.readers, q.backend),
    )
    api = q.apis[q.project_id]
    root = api.rows("TroshkaProject", None)[0]
    pvc = api.rows("PersistentVolumeClaim", None)[0]
    q.request = checkpoints.protocol().build_request(
        api, root, q.root.namespace, pvc, "delete-PersistentVolumeClaim"
    )
    root["metadata"].setdefault("annotations", {})[checkpoints.REQUEST] = __import__(
        "json"
    ).dumps(q.request)
    api.values["TroshkaProject", root["metadata"]["name"]] = root
    q.checkpoint_api = api
    return q


def test_signed_acknowledgement_rejects_forged_or_changed_request():
    from app.services import native_origin_checkpoints as checkpoints

    public, secret = checkpoints.generate_key()
    request = {
        "requestId": "request",
        "intentId": "intent",
        "objects": [{"uid": "old-pvc"}],
    }
    ack = checkpoints.sign(secret, request, "receipt", "receipt-digest", "predecessor")
    assert checkpoints.verify_signature(public, request, ack)
    changed = deepcopy(request)
    changed["objects"][0]["uid"] = "replacement-pvc"
    assert not checkpoints.verify_signature(public, changed, ack)
    ack["receiptId"] = "forged"
    assert not checkpoints.verify_signature(public, request, ack)


def test_checkpoint_key_decryption_failure_holds():
    from app.schemas.quota import AccountingUnavailable
    from app.services import native_origin_checkpoints as checkpoints

    with pytest.raises(AccountingUnavailable, match="key"):
        checkpoints.sign(
            "invalid-ciphertext", {"requestId": "x"}, "receipt", "digest", None
        )


def test_committed_receipt_replay_and_changed_request_id(checkpoint_case):
    from app.models.native_origin_checkpoint import (
        NativeOriginCheckpointKey,
        NativeOriginProducerReceipt,
    )
    from app.models.native_origin_custody import NativeOriginCustodyReceipt
    from app.schemas.quota import AccountingUnavailable
    from app.services import native_origin_checkpoints as checkpoints

    q = checkpoint_case
    ack = checkpoints.service_request(
        q.sessions, q.checkpoint_api, str(q.intent.id), q.request
    )
    with q.sessions() as db:
        row = db.get(NativeOriginProducerReceipt, q.request["requestId"])
        assert db.get(NativeOriginCustodyReceipt, row.custody_receipt_id) is not None
        public = db.get(NativeOriginCheckpointKey, str(q.intent.id)).public_key
    assert checkpoints.verify_signature(public, q.request, ack)
    assert (
        checkpoints.service_request(
            q.sessions, q.checkpoint_api, str(q.intent.id), q.request
        )
        == ack
    )
    changed = deepcopy(q.request)
    changed["reason"] = "different"
    with pytest.raises(AccountingUnavailable, match="conflict"):
        checkpoints.service_request(
            q.sessions, q.checkpoint_api, str(q.intent.id), changed
        )


def test_cancel_during_verification_never_acknowledges(checkpoint_case, monkeypatch):
    from app.models.native_origin_checkpoint import NativeOriginProducerReceipt
    from app.schemas.quota import AccountingUnavailable
    from app.services import native_deployment_origin as origin
    from app.services import native_origin_checkpoints as checkpoints
    from app.services import native_origin_custody as custody

    q = checkpoint_case
    verify = custody.verify_configured

    def cancelling(request):
        result = verify(request)
        with q.sessions() as db:
            origin.cancel(db, q.project_id)
            db.commit()
        return result

    monkeypatch.setattr(custody, "verify_configured", cancelling)
    with pytest.raises((AccountingUnavailable, ValueError)):
        checkpoints.service_request(
            q.sessions, q.checkpoint_api, str(q.intent.id), q.request
        )
    with q.sessions() as db:
        assert db.query(NativeOriginProducerReceipt).count() == 0


def test_observer_does_not_replace_producer_job_tracking(monkeypatch):
    from app.core import redis
    from app.services import native_origin_checkpoints as checkpoints

    def forbidden(*args, **kwargs):
        raise AssertionError("observer must not enqueue a producer job")

    monkeypatch.setattr(redis, "enqueue_job", forbidden)
    called = []
    monkeypatch.setattr(
        checkpoints, "observe_project", lambda value: called.append(value)
    )
    future = checkpoints.schedule("project-test")
    future.result(timeout=5)
    assert called == ["project-test"]


def test_absent_legacy_child_is_idempotent_but_origin_absence_holds():
    from unittest.mock import MagicMock

    from kubernetes.client.exceptions import ApiException

    from app.schemas.quota import AccountingUnavailable
    from app.services import native_origin_checkpoints as checkpoints

    custom, api = MagicMock(), MagicMock()
    custom.get_namespaced_custom_object.side_effect = ApiException(status=404)
    api.rows.return_value = [{"spec": {}}]
    assert checkpoints._find_child(custom, api, "ns", "vm") is None
    api.rows.return_value = [{"spec": {"originCheckpoint": {}}}]
    with pytest.raises(AccountingUnavailable, match="absent child"):
        checkpoints._find_child(custom, api, "ns", "vm")


@pytest.mark.parametrize("state", ["active", "stopped", "error", "reconfiguring"])
def test_supported_lifecycle_signs_and_replays_but_cancel_holds(checkpoint_case, state):
    from app.models.project import Project
    from app.services import native_deployment_origin as origin
    from app.services import native_origin_checkpoints as checkpoints
    from tests.test_native_managed_resume import FixedDatetime

    q = checkpoint_case
    with q.sessions() as db:
        project = db.get(Project, q.project_id)
        project.state, project.updated_at = state, FixedDatetime.current
        db.commit()
    ack = checkpoints.service_request(
        q.sessions, q.checkpoint_api, str(q.intent.id), q.request
    )
    assert (
        checkpoints.service_request(
            q.sessions, q.checkpoint_api, str(q.intent.id), q.request
        )
        == ack
    )
    with q.sessions() as db:
        origin.cancel(db, q.project_id)
        db.commit()
    with pytest.raises(ValueError, match="cancellation"):
        checkpoints.service_request(
            q.sessions, q.checkpoint_api, str(q.intent.id), q.request
        )


@pytest.mark.parametrize("gate", ["io_phase", "io_token", "destroying"])
def test_signing_replay_retains_io_and_lifecycle_gates(checkpoint_case, gate):
    from app.models.native_deployment_origin import NativeDeploymentOriginActive
    from app.models.project import Project
    from app.services import native_origin_checkpoints as checkpoints
    from tests.test_native_managed_resume import FixedDatetime

    q = checkpoint_case
    checkpoints.service_request(
        q.sessions, q.checkpoint_api, str(q.intent.id), q.request
    )
    with q.sessions() as db:
        if gate == "destroying":
            project = db.get(Project, q.project_id)
            project.state, project.updated_at = gate, FixedDatetime.current
        else:
            value = (
                "support"
                if gate == "io_phase"
                else "00000000-0000-0000-0000-000000000001"
            )
            setattr(db.get(NativeDeploymentOriginActive, q.project_id), gate, value)
        db.commit()
    with pytest.raises(ValueError):
        checkpoints.service_request(
            q.sessions, q.checkpoint_api, str(q.intent.id), q.request
        )


def test_replacement_child_marker_requires_original_live_root(monkeypatch):
    from unittest.mock import MagicMock

    from app.services import native_origin_checkpoints as checkpoints
    from app.services.native_managed_power import helpers

    module = helpers()
    root = {
        "kind": "TroshkaProject",
        "metadata": {"name": "project", "uid": "original"},
        "spec": {"originCheckpoint": {"intentId": "intent"}},
    }
    body = {
        "kind": "TroshkaVM",
        "metadata": {
            "name": "child",
            "ownerReferences": [
                {"kind": "TroshkaProject", "name": "project", "uid": "original"}
            ],
        },
        "spec": {"disks": []},
    }
    api = MagicMock()
    api.get.return_value = root
    monkeypatch.setattr(module, "PowerClient", lambda *args: api)
    checkpoints.mark_replacement_child(MagicMock(), "external-ns", body)
    assert body["spec"] == {"disks": [], checkpoints.protocol().PARTICIPATION: "intent"}
    root["metadata"]["uid"] = "replacement"
    with pytest.raises(ValueError, match="changed"):
        checkpoints.mark_replacement_child(MagicMock(), "external-ns", body)


def test_commit_before_sign_recovers_after_signing_crash(checkpoint_case, monkeypatch):
    from app.models.native_origin_checkpoint import NativeOriginProducerReceipt
    from app.services import native_origin_checkpoints as checkpoints

    q = checkpoint_case
    signer = checkpoints.sign

    def crash(*args):
        with q.sessions() as db:
            assert (
                db.get(NativeOriginProducerReceipt, q.request["requestId"]) is not None
            )
        raise RuntimeError("publication interrupted")

    monkeypatch.setattr(checkpoints, "sign", crash)
    with pytest.raises(RuntimeError, match="interrupted"):
        checkpoints.service_request(
            q.sessions, q.checkpoint_api, str(q.intent.id), q.request
        )
    monkeypatch.setattr(checkpoints, "sign", signer)
    ack = checkpoints.service_request(
        q.sessions, q.checkpoint_api, str(q.intent.id), q.request
    )
    assert ack["receiptId"]
    with q.sessions() as db:
        assert db.query(NativeOriginProducerReceipt).count() == 1


@pytest.mark.parametrize("change", ["root_uid", "key", "child_uid", "resource_version"])
def test_live_identity_change_never_signs(checkpoint_case, change):
    from app.models.native_origin_checkpoint import NativeOriginProducerReceipt
    from app.schemas.quota import AccountingUnavailable
    from app.services import native_origin_checkpoints as checkpoints

    q = checkpoint_case
    root = q.checkpoint_api.values["TroshkaProject", q.intent.root_name]
    target = q.checkpoint_api.values[
        q.request["target"]["kind"], q.request["target"]["name"]
    ]
    if change == "root_uid":
        root["metadata"]["uid"] = "replacement"
    elif change == "key":
        root["spec"]["originCheckpoint"]["publicKey"] = "forged"
    elif change == "child_uid":
        target["metadata"]["uid"] = "replacement"
    else:
        target["metadata"]["resourceVersion"] = "changed"
    with pytest.raises(AccountingUnavailable):
        checkpoints.service_request(
            q.sessions, q.checkpoint_api, str(q.intent.id), q.request
        )
    with q.sessions() as db:
        assert db.query(NativeOriginProducerReceipt).count() == 0


def test_unresolved_claim_is_retained_but_never_acknowledged(
    checkpoint_case, monkeypatch
):
    from app.models.native_origin_checkpoint import NativeOriginProducerReceipt
    from app.models.project import Project
    from app.schemas.quota import AccountingUnavailable
    from app.services import native_origin_checkpoints as checkpoints
    from app.services import native_origin_custody as custody

    q = checkpoint_case
    original = q.reader.pvcs
    uid = q.request["target"]["uid"]

    def pending(namespace):
        rows = original(namespace)
        for pvc in rows:
            pvc["spec"].pop("volumeName", None)
            pvc["status"] = {"phase": "Pending"}
        return rows

    pvs = q.reader.pvs
    monkeypatch.setattr(q.reader, "pvcs", pending)
    monkeypatch.setattr(
        q.reader,
        "pvs",
        lambda: [p for p in pvs() if p["spec"]["claimRef"]["uid"] != uid],
    )
    with pytest.raises(AccountingUnavailable, match="unresolved"):
        checkpoints.service_request(
            q.sessions, q.checkpoint_api, str(q.intent.id), q.request
        )
    with q.sessions() as db:
        assert db.query(NativeOriginProducerReceipt).count() == 0
        _, receipt, _ = custody.retained(db, db.get(Project, q.project_id))
        assert custody.unresolved_claims(receipt)[0].pvc_uid == uid


def test_postgres_same_request_publishers_commit_one_receipt(
    checkpoint_case, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock

    from app.models.native_origin_checkpoint import NativeOriginProducerReceipt
    from app.services import native_origin_checkpoints as checkpoints
    from app.services import native_origin_custody as custody

    q = checkpoint_case
    custody_tests._postgres(q)
    barrier, lock = Barrier(2), Lock()
    original = custody.verify_configured

    def verify(request):
        with lock:
            result = original(request)
        barrier.wait(timeout=10)
        return result

    def publish():
        try:
            return checkpoints.service_request(
                q.sessions, q.checkpoint_api, str(q.intent.id), q.request
            )
        except ValueError as error:
            return error

    monkeypatch.setattr(custody, "verify_configured", verify)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            f.result(timeout=30) for f in [pool.submit(publish), pool.submit(publish)]
        ]
    assert sum(isinstance(value, dict) for value in results) == 1
    with q.sessions() as db:
        assert db.query(NativeOriginProducerReceipt).count() == 1


def test_postgres_checkpoint_migration_retains_immutable_keys(checkpoint_case):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import UUID, inspect, text
    from sqlalchemy.exc import DBAPIError

    from app.models.native_origin_checkpoint import NativeOriginCheckpointKey

    q = checkpoint_case
    custody_tests._postgres(q)
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/a62f19cb0e47_native_origin_producer.py"
    )
    spec = importlib.util.spec_from_file_location("checkpoint_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = q.sessions.kw["bind"]
    with engine.begin() as connection, Operations.context(
        MigrationContext.configure(connection)
    ):
        # Fixture metadata lacks triggers. Exercise the deployed DDL itself.
        connection.execute(
            text(
                "CREATE OR REPLACE FUNCTION reject_native_origin_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable'; END $$"
            )
        )
        migration.downgrade()
        migration.upgrade()
        columns = inspect(connection).get_columns("native_origin_checkpoint_keys")
        assert isinstance(
            next(c for c in columns if c["name"] == "intent_id")["type"], UUID
        )
        connection.execute(
            NativeOriginCheckpointKey.__table__.insert().values(
                intent_id=str(q.intent.id),
                public_key="public",
                encrypted_private_key="ciphertext",
            )
        )
        for sql in (
            "UPDATE native_origin_checkpoint_keys SET public_key='bad'",
            "DELETE FROM native_origin_checkpoint_keys",
        ):
            with pytest.raises(
                DBAPIError, match="immutable"
            ), connection.begin_nested():
                connection.execute(text(sql))
