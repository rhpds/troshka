"""Origin custody retains real storage without inventing an administrator."""

from copy import deepcopy

import pytest

from app.models.project import Project
from app.models.provider import Provider
from app.services import native_deployment_origin as origin
from app.services import native_origin_custody as custody
from app.services.kubevirt_storage_enrollment import digest
from tests import test_native_online_enrollment as online_tests
from tests.test_native_managed_resume import FixedDatetime

cleanup_case = online_tests.cleanup_case
deploy_case = online_tests.deploy_case
isolated_quota_database = online_tests.isolated_quota_database
native_ledger = online_tests.native_ledger
online_case = online_tests.online_case
resume_case = online_tests.resume_case


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "role",
        "role_kind",
        "role_group",
        "subject",
        "namespace",
        "extra_subject",
        "subject_kind",
        "owner",
        "binding_namespace",
        "api_version",
    ],
)
def test_origin_inventory_grant_namespace_ownership(online_case, monkeypatch, mutation):
    from app.services import native_online_enrollment as online
    from app.services import native_origin_support as support
    from app.services.native_managed_power import helpers

    q = online_case
    root = q.report.project_roots[0]
    # Exercise the production list validation as well as bootstrap matching.
    reader = helpers().PowerClient(api=object())
    monkeypatch.setattr(
        reader,
        "request",
        lambda method, kind, namespace: {
            "items": q.reader.rows(kind, namespace),
            "metadata": {},
        },
    )
    monkeypatch.setattr(online, "ReadOnlyManaged", lambda kube: reader)
    grant = next(
        body
        for body in support.render(
            root.namespace, "managed", {}, None, None, "origin-attempt"
        )
        if body["metadata"]["name"] == support.INVENTORY_ROLE
    )
    grant["metadata"]["uid"] = "inventory-grant-uid"
    if mutation == "role":
        grant["roleRef"]["name"] = "cluster-admin"
    elif mutation == "role_kind":
        grant["roleRef"]["kind"] = "Role"
    elif mutation == "role_group":
        grant["roleRef"]["apiGroup"] = "other.invalid"
    elif mutation == "subject":
        grant["subjects"][0]["name"] = "other"
    elif mutation == "namespace":
        grant["subjects"][0]["namespace"] = "other"
    elif mutation == "extra_subject":
        grant["subjects"].append(
            dict(kind="ServiceAccount", name="other", namespace="troshka")
        )
    elif mutation == "subject_kind":
        grant["subjects"][0]["kind"] = "User"
    elif mutation == "owner":
        grant["metadata"]["ownerReferences"] = [
            dict(kind="Secret", name="foreign", uid="foreign", apiVersion="v1")
        ]
    elif mutation == "binding_namespace":
        grant["metadata"]["namespace"] = "foreign"
    elif mutation == "api_version":
        grant["apiVersion"] = "other.invalid/v1"
    q.apis[str(root.project_id)].values["RoleBinding", support.INVENTORY_ROLE] = grant
    if mutation:
        with pytest.raises(helpers().ManagedPowerError):
            online.verify_namespace_ownership(q.reader, root)
    else:
        pins = online.verify_namespace_ownership(q.reader, root)
        assert any(pin["uid"] == "inventory-grant-uid" for pin in pins)


@pytest.fixture
def custody_case(online_case, monkeypatch):
    q = online_case
    q.project_id = q.project_ids[0]
    root = q.report.project_roots[0]
    q.root = root
    api = q.apis[q.project_id]
    live = api.rows("TroshkaProject", root.namespace)[0]
    namespace = api.get("Namespace", None, root.namespace)
    provider_id = str(q.pins.provider_clusters[0].provider_id)
    with q.sessions() as db:
        project = db.get(Project, q.project_id)
        project.state = "deploying"
        db.flush()
        intent = origin.prepare(
            db,
            project,
            db.get(Provider, provider_id),
            dict(
                cluster_uid=root.cluster_key,
                namespace=root.namespace,
                root_name=live["metadata"]["name"],
                namespace_mode="managed",
                cr_spec_digest=digest(live["spec"]),
            ),
        )
        live["metadata"].setdefault("annotations", {}).update(
            origin.annotations(intent)
        )
        namespace["metadata"].setdefault("annotations", {}).update(
            origin.annotations(intent)
        )
        api.values["TroshkaProject", live["metadata"]["name"]] = live
        api.values["Namespace", root.namespace] = namespace
        observed = dict(cluster_uid=root.cluster_key, namespace=namespace, root=live)
        origin.bind_namespace(db, str(intent.id), observed)
        origin.bind_root(db, str(intent.id), observed)
        db.commit()
    q.intent = intent
    monkeypatch.setattr(custody, "admission", lambda _: "admission")
    monkeypatch.setattr(
        custody, "runtime_profile", lambda _: (str(q.target_id), q.pins, "runtime")
    )
    monkeypatch.setattr(custody, "datetime", FixedDatetime)
    return q


def prepare(q, **kwargs):
    with q.sessions() as db:
        value = custody.prepare(
            db, str(q.intent.id), domain_id=str(q.pins.domain_id), **kwargs
        )
        db.commit()
        return value


def publish(q, **kwargs):
    request = prepare(q, **kwargs)
    verified = custody.verify(request, q.readers, q.backend)
    with q.sessions() as db:
        row = custody.apply(db, verified)
        db.commit()
        return row.id, verified


@pytest.fixture
def settled_history(custody_case, monkeypatch):
    from types import SimpleNamespace

    from app.services import quota_cleanup

    q = custody_case
    old = next(r for r in q.pins.project_roots if str(r.project_id) != q.project_id)
    authority = SimpleNamespace(
        root=old,
        phase="finalized",
        settlement_report_id="settlement",
        settled_at=FixedDatetime.current,
    )
    monkeypatch.setattr(quota_cleanup, "native_intents", lambda db: [authority])
    with q.sessions() as db:
        project = db.get(Project, str(old.project_id))
        project.state = "draft"
        project.host_id = None
        db.commit()
    return q, old, authority


def test_settled_hostless_history_allows_successor_and_preserves_history(
    settled_history,
):
    from app.models.kubevirt_storage import NativeEnrollment, NativeProjectRoot

    q, old, _ = settled_history
    with q.sessions() as db:
        history = {r.id: deepcopy(r.payload) for r in db.query(NativeEnrollment)}
        roots = {
            r.key: {
                c.name: deepcopy(getattr(r, c.name))
                for c in NativeProjectRoot.__table__.columns
            }
            for r in db.query(NativeProjectRoot)
        }
    _, verified = publish(q)
    assert old not in verified.prepared.pins.project_roots
    assert verified.prepared.root in verified.prepared.pins.project_roots
    with q.sessions() as db:
        assert all(
            db.get(NativeEnrollment, key).payload == value
            for key, value in history.items()
        )
        for key, fields in roots.items():
            assert all(
                getattr(db.get(NativeProjectRoot, key), name) == value
                for name, value in fields.items()
            )


@pytest.mark.parametrize(
    "change", ["pending", "missing-report", "missing-time", "other-root"]
)
def test_hostless_history_requires_exact_durable_settlement(settled_history, change):
    q, old, authority = settled_history
    if change == "pending":
        authority.phase = "pending"
    elif change == "missing-report":
        authority.settlement_report_id = None
    elif change == "missing-time":
        authority.settled_at = None
    else:
        authority.root = old.model_copy(
            update={"namespace_uid": "different-generation"}
        )
    with pytest.raises(ValueError, match="project has no assigned native provider"):
        prepare(q)


def test_settlement_authority_change_before_publication_is_rejected(settled_history):
    q, _, authority = settled_history
    verified = custody.verify(prepare(q), q.readers, q.backend)
    authority.settlement_report_id = None
    with q.sessions() as db, pytest.raises(ValueError, match="runtime profile changed"):
        custody.apply(db, verified)


def test_settled_root_filter_never_omits_source(custody_case):
    from app.services import kubevirt_storage_enrollment as enrollment

    q = custody_case
    with q.sessions() as db:
        domain, previous = custody._domain(db, str(q.pins.domain_id))
        pins = custody._extended_pins(
            previous,
            domain.enrollment_id,
            q.root,
            retired={enrollment.root_key(q.root)},
        )
    assert pins == previous.pins


def _grow_cache_authority(q, receipt=None):
    """Publish a real verified additive cache enrollment after protected pins."""
    from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment
    from app.schemas.kubevirt_storage_enrollment import CacheRoot, DomainExtension
    from app.services import kubevirt_storage_enrollment as enrollment

    with q.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        previous = enrollment.validated_enrollment(
            db.get(NativeEnrollment, domain.enrollment_id)
        )
        pins = previous.pins.model_copy(
            update={
                "cache_roots": [
                    *previous.pins.cache_roots,
                    CacheRoot(
                        cluster_key="cluster",
                        namespace="second-cache",
                        namespace_uid="second-cache-uid",
                    ),
                ],
                "cache_birth_receipts": [
                    *previous.pins.cache_birth_receipts,
                    *([receipt] if receipt else []),
                ],
            }
        )
        pins = pins.model_copy(
            update={
                "domain_extension": DomainExtension(
                    version="native-domain-extension-v1",
                    previous_enrollment_id=domain.enrollment_id,
                    previous_enrollment_digest=previous.digest,
                    cache_authorization_digest=enrollment.cache_authorization_digest(
                        pins
                    ),
                )
            }
        )
        request = enrollment.prepare_native_enrollment(
            db, [str(r.project_id) for r in pins.project_roots]
        )
        manifest = enrollment.verify_native_enrollment(
            request, q.readers, q.backend, pins=pins
        )
        identity = enrollment.enroll_native_roots(
            db, manifest, expected_target_id=str(q.target_id)
        )
        db.commit()
        return identity, manifest


@pytest.fixture
def growing_cache_case(custody_case, monkeypatch):
    q = custody_case
    original = q.reader.namespace
    monkeypatch.setattr(
        q.reader,
        "namespace",
        lambda name: (
            {"metadata": {"uid": "second-cache-uid"}}
            if name == "second-cache"
            else original(name)
        ),
    )
    return q


def test_origin_custody_uses_authenticated_cache_growth(growing_cache_case):
    q = growing_cache_case
    enrollment_id, manifest = _grow_cache_authority(q)
    row_id, value = publish(q)
    assert str(value.prepared.previous_enrollment_id) == enrollment_id
    assert value.prepared.pins == manifest.pins
    with q.sessions() as db:
        assert (
            custody.retained(db, db.get(Project, q.project_id), require_complete=True)[
                0
            ].id
            == row_id
        )
        uid = value.bindings[0].pvc_uid
        assert (
            custody.prepare_retirement(db, str(q.intent.id), uid).source_kind
            == "origin"
        )


def _cache_birth_receipt(q):
    from app.schemas.kubevirt_storage import InitializationTarget
    from app.services.native_cache_birth import submit_cache_birth, verify_cache_birth
    from tests.test_native_cache_birth import CreationAdapter, observed
    from tests.test_native_cache_birth import prepare as prepare_birth
    from tests.test_native_initialization_evidence import synthetic_profile

    target = InitializationTarget.model_validate(
        synthetic_profile()["target"]
    ).model_copy(
        update={
            **q.pins.pool_keys[0].model_dump(),
            **q.pins.provider_clusters[0].model_dump(),
        }
    )
    operation_id = prepare_birth(q, target=target)
    adapter = CreationAdapter(q)
    submit_cache_birth(q.sessions, operation_id, adapter)
    adapter.observe = observed
    return verify_cache_birth(q.sessions, operation_id, adapter)


def test_origin_enrolls_authenticated_cache_birth_history(growing_cache_case):
    q = growing_cache_case
    reference = _cache_birth_receipt(q)
    _grow_cache_authority(q, reference)
    _, verified = publish(q, mode="enroll")
    assert verified.prepared.pins.cache_birth_receipts == [reference]


def test_origin_holds_tampered_cache_birth_receipt(growing_cache_case):
    from app.models.kubevirt_cache_birth import NativeCacheBirth

    q = growing_cache_case
    reference = _cache_birth_receipt(q)
    _grow_cache_authority(q, reference)
    with q.sessions() as db:
        row = db.get(NativeCacheBirth, str(reference.operation_id))
        row.receipt = {**row.receipt, "pv_uid": "replacement-cache-pv"}
        db.commit()
    with pytest.raises(ValueError, match="cache receipt content identity"):
        prepare(q, mode="enroll")


def test_retained_origin_survives_authenticated_later_cache_growth(growing_cache_case):
    q = growing_cache_case
    row_id, _ = publish(q)
    _grow_cache_authority(q)
    with q.sessions() as db:
        assert (
            custody.retained(db, db.get(Project, q.project_id), require_complete=True)[
                0
            ].id
            == row_id
        )


def test_origin_independently_rechecks_evolved_cache_identity(
    growing_cache_case, monkeypatch
):
    q = growing_cache_case
    _grow_cache_authority(q)
    request = prepare(q)
    original = q.reader.namespace
    monkeypatch.setattr(
        q.reader,
        "namespace",
        lambda name: (
            {"metadata": {"uid": "replacement-cache"}}
            if name == "second-cache"
            else original(name)
        ),
    )
    with pytest.raises(ValueError, match="cache namespace"):
        custody.verify(request, q.readers, q.backend)


def test_origin_rejects_enrollment_racing_prepared_cache_scope(growing_cache_case):
    q = growing_cache_case
    request = prepare(q)
    verified = custody.verify(request, q.readers, q.backend)
    _grow_cache_authority(q)
    with q.sessions() as db, pytest.raises(ValueError, match="predecessor changed"):
        custody.apply(db, verified)


@pytest.mark.parametrize(
    "change",
    [
        "unrelated",
        "target",
        "pool",
        "provider",
        "cache",
        "receipt",
        "initialization",
        "predecessor",
    ],
)
def test_origin_rejects_changed_authenticated_cache_chain(growing_cache_case, change):
    from uuid import uuid4

    from app.models.kubevirt_storage import NativeEnrollment
    from tests.test_native_initialization_evidence import synthetic_profile

    q = growing_cache_case
    enrollment_id, _ = _grow_cache_authority(q)
    with q.sessions() as db:
        row = db.get(NativeEnrollment, enrollment_id)
        raw = deepcopy(row.payload)
        pins = raw["pins"]
        if change == "unrelated":
            pins.pop("domain_extension")
        elif change == "target":
            raw["request"]["target_id"] = str(uuid4())
        elif change == "pool":
            pins["pool_keys"][0]["pool_id"] = 999
        elif change == "provider":
            pins["provider_clusters"][0]["provider_id"] = str(uuid4())
        elif change == "cache":
            pins["cache_roots"][-1]["namespace_uid"] = "changed"
        elif change == "receipt":
            pins["cache_birth_receipts"] = [
                {"operation_id": str(uuid4()), "digest": "a" * 64}
            ]
        elif change == "initialization":
            pins["initialization_evidence"] = synthetic_profile()
            pins["initialization_evidence"]["target"].update(
                pins["pool_keys"][0],
                **pins["provider_clusters"][0],
            )
            pins["domain_extension"]["initialization_authorization_digest"] = digest(
                pins["initialization_evidence"]
            )
        else:
            pins["domain_extension"]["previous_enrollment_digest"] = "a" * 64
        raw["digest"] = digest({k: v for k, v in raw.items() if k != "digest"})
        row.payload, row.digest = raw, raw["digest"]
        db.commit()
    with pytest.raises(
        ValueError, match="anchor|identity|authorization|predecessor|extension"
    ):
        prepare(q)


def test_scoped_retention_while_deploying_reuses_enrollment(custody_case):
    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeEnrollment,
        NativeRetainedBinding,
    )
    from app.models.native_custody import NativeCustodyReceipt
    from app.models.quota import QuotaAllocation, QuotaReservation

    q = custody_case
    uid = q.reader.pvcs(q.root.namespace)[0]["metadata"]["uid"]
    with q.sessions() as db:
        before = db.get(NativeCollectionDomain, str(q.pins.domain_id)).enrollment_id
        count = db.query(NativeEnrollment).count()
    row_id, verified = publish(q, mode="removal", pvc_uids=[uid])
    with q.sessions() as db:
        assert (
            db.get(NativeCollectionDomain, str(q.pins.domain_id)).enrollment_id
            == before
        )
        assert db.query(NativeEnrollment).count() == count
        assert db.query(NativeRetainedBinding).count() >= 1
        assert db.query(NativeCustodyReceipt).count() == 0
        assert (
            db.query(QuotaAllocation).count() == db.query(QuotaReservation).count() == 0
        )
        assert custody.apply(db, verified).id == row_id
        with pytest.raises(ValueError, match="complete custody"):
            custody.retained(db, db.get(Project, q.project_id), require_complete=True)


def test_enrollment_only_does_not_create_storage_or_cleanup_authority(custody_case):
    q = custody_case
    _, verified = publish(q, mode="enroll")
    assert verified.bindings == verified.object_identities == []
    with q.sessions() as db:
        with pytest.raises(ValueError, match="complete custody"):
            custody.retained(db, db.get(Project, q.project_id), require_complete=True)


def test_complete_custody_is_secret_free_and_revalidated(custody_case, monkeypatch):
    q = custody_case
    _, verified = publish(q)
    assert "runtime_topologies" not in verified.model_dump()
    assert "administrator" not in verified.model_dump_json()
    with q.sessions() as db:
        assert custody.retained(
            db, db.get(Project, q.project_id), require_complete=True
        )
    monkeypatch.setattr(
        custody,
        "admission",
        lambda _: (_ for _ in ()).throw(ValueError("admission disabled")),
    )
    with q.sessions() as db:
        assert custody.retained(
            db, db.get(Project, q.project_id), require_complete=True
        )
    with pytest.raises(ValueError, match="admission"):
        prepare(q)


@pytest.mark.parametrize("change", ["cancel", "io", "profile", "owner", "predecessor"])
def test_prepared_publication_rechecks_identity(custody_case, monkeypatch, change):
    from app.models.user import User

    q = custody_case
    request = prepare(q, mode="enroll")
    verified = custody.verify(request, q.readers, q.backend)
    if change == "profile":
        monkeypatch.setattr(
            custody,
            "runtime_profile",
            lambda _: (str(q.target_id), q.pins, "replacement"),
        )
    elif change == "predecessor":
        publish(q, mode="enroll")
    else:
        with q.sessions() as db:
            if change == "cancel":
                origin.cancel(db, q.project_id)
            elif change == "io":
                origin.begin_io(db, str(q.intent.id), "support")
            else:
                owner = User(email="replacement@example.test")
                db.add(owner)
                db.flush()
                db.get(Project, q.project_id).owner_id = owner.id
            db.commit()
    with q.sessions() as db, pytest.raises(
        (ValueError, RuntimeError), match="changed|holds"
    ):
        custody.apply(db, verified)


def test_pending_claim_retained_without_fake_binding(custody_case, monkeypatch):
    q = custody_case
    original = q.reader.pvcs

    def pending(namespace):
        rows = original(namespace)
        for pvc in rows:
            pvc["spec"].pop("volumeName", None)
            pvc["status"] = {"phase": "Pending"}
        return rows

    monkeypatch.setattr(q.reader, "pvcs", pending)
    uid = pending(q.root.namespace)[0]["metadata"]["uid"]
    original_pvs = q.reader.pvs
    monkeypatch.setattr(
        q.reader,
        "pvs",
        lambda: [p for p in original_pvs() if p["spec"]["claimRef"]["uid"] != uid],
    )
    _, verified = publish(q, mode="removal", pvc_uids=[uid])
    assert verified.bindings == []
    assert verified.pending_claims[0].pvc_uid == uid
    with q.sessions() as db:
        _, receipt, _ = custody.retained(db, db.get(Project, q.project_id))
        assert custody.unresolved_claims(receipt)
        with pytest.raises(ValueError, match="prevents storage accounting"):
            custody.resolve_ancestry(db, receipt.verified.enrollment, set())
    with pytest.raises(ValueError, match="unresolved"):
        custody.verify(prepare(q), q.readers, q.backend)


def test_ownerless_foreign_object_rejected(custody_case):
    q = custody_case
    api = q.apis[q.project_id]
    api.values["Secret", "foreign"] = dict(
        apiVersion="v1",
        kind="Secret",
        metadata=dict(
            name="foreign",
            namespace=q.root.namespace,
            uid="foreign-uid",
            resourceVersion="1",
        ),
        data={"password": "never-retain"},
    )
    with pytest.raises(Exception, match="ownership"):
        custody.verify(prepare(q), q.readers, q.backend)


def test_missing_old_binding_never_becomes_complete_custody(custody_case, monkeypatch):
    q = custody_case
    publish(q)
    verified = custody.verify(prepare(q), q.readers, q.backend)
    verified = verified.model_copy(update={"bindings": []})
    verified = verified.model_copy(
        update={"digest": digest(verified.model_dump(mode="json", exclude={"digest"}))}
    )
    with q.sessions() as db, pytest.raises(ValueError, match="missing old binding"):
        custody.apply(db, verified)


def test_origin_admission_defaults_off(monkeypatch):
    from types import SimpleNamespace

    from app.services import native_origin_custody as custody

    monkeypatch.setattr(custody, "config", SimpleNamespace(get=lambda *_: []))
    with pytest.raises(ValueError, match="admission"):
        custody.admission("00000000-0000-0000-0000-000000000001")


def test_pending_claim_with_real_pv_retains_exact_binding(custody_case, monkeypatch):
    q = custody_case
    original = q.reader.pvcs

    def pending(namespace):
        values = original(namespace)
        for pvc in values:
            pvc["spec"].pop("volumeName", None)
            pvc["status"] = {"phase": "Pending"}
        return values

    monkeypatch.setattr(q.reader, "pvcs", pending)
    uid = pending(q.root.namespace)[0]["metadata"]["uid"]
    _, value = publish(q, mode="removal", pvc_uids=[uid])
    assert len(value.bindings) == 1
    assert value.bindings[0].pvc_uid == uid
    assert value.pending_claims == []


def _domain_without_origin(q):
    """Fixture: independently approved domain exists before this new root."""
    from uuid import uuid4

    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeEnrollment,
        NativeProjectRoot,
    )
    from app.services import kubevirt_storage_enrollment as enrollment

    with q.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        previous = enrollment.validated_enrollment(
            db.get(NativeEnrollment, domain.enrollment_id)
        )
        remaining = [
            r for r in previous.pins.project_roots if str(r.project_id) != q.project_id
        ]
        pins = previous.pins.model_copy(update={"project_roots": remaining})
        request = enrollment.prepare_native_enrollment(
            db, [str(r.project_id) for r in remaining]
        )
        manifest = enrollment.verify_native_enrollment(
            request, q.readers, q.backend, pins=pins
        )
        row = NativeEnrollment(
            id=str(uuid4()),
            domain_id=domain.id,
            digest=manifest.digest,
            payload=manifest.model_dump(mode="json"),
        )
        db.add(row)
        domain.enrollment_id = row.id
        db.query(NativeProjectRoot).filter_by(project_id=q.project_id).delete()
        db.commit()
        q.pins = pins
        return row.id, manifest


def test_new_root_extension_preserves_domain_scope_and_conflicts(custody_case):
    from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment
    from app.services import kubevirt_storage_enrollment as enrollment

    q = custody_case
    old_id, old = _domain_without_origin(q)
    with q.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        domain.conflicted = True
        domain.latest_started_at = FixedDatetime.current
        db.commit()
    row_id, value = publish(q, mode="enroll")
    with q.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        assert domain.enrollment_id != old_id
        assert domain.conflicted is True and domain.latest_report_id is None
        assert enrollment.utc(domain.latest_started_at) >= FixedDatetime.current
        current = enrollment.validated_enrollment(
            db.get(NativeEnrollment, domain.enrollment_id)
        )
        assert set(old.pins.project_roots) < set(current.pins.project_roots)
        assert custody._scope(old.pins) == custody._scope(current.pins)
        assert current.pins.domain_extension.root_transition_digest is None
        assert custody.retained(db, db.get(Project, q.project_id))[0].id == row_id


@pytest.mark.parametrize("field", ["target", "pool", "cache", "provider", "absent"])
def test_wrong_independent_scope_holds(custody_case, monkeypatch, field):
    from uuid import uuid4

    from app.models.kubevirt_storage import NativeCollectionDomain
    from app.schemas.kubevirt_storage_enrollment import CacheRoot

    q = custody_case
    pins, target = q.pins, str(q.target_id)
    if field == "target":
        target = str(uuid4())
    elif field == "pool":
        pins = pins.model_copy(
            update={
                "pool_keys": [pins.pool_keys[0].model_copy(update={"pool_id": 999})]
            }
        )
    elif field == "cache":
        pins = pins.model_copy(
            update={
                "cache_roots": [
                    CacheRoot(
                        cluster_key="cluster", namespace="other", namespace_uid="other"
                    )
                ]
            }
        )
    elif field == "provider":
        pins = pins.model_copy(
            update={
                "provider_clusters": [
                    pins.provider_clusters[0].model_copy(
                        update={"provider_id": uuid4()}
                    )
                ]
            }
        )
    else:
        with q.sessions() as db:
            db.delete(db.get(NativeCollectionDomain, str(pins.domain_id)))
            db.commit()
    monkeypatch.setattr(custody, "runtime_profile", lambda _: (target, pins, "runtime"))
    with pytest.raises(ValueError, match="scope|domain"):
        prepare(q)


@pytest.mark.parametrize("field", ["uid", "generation", "spec", "token"])
def test_exact_live_creation_root_required(custody_case, field):
    q = custody_case
    request = prepare(q, mode="enroll")
    root = next(
        v
        for (kind, _), v in q.apis[q.project_id].values.items()
        if kind == "TroshkaProject"
    )
    if field in {"uid", "generation"}:
        root["metadata"][field] = (
            "replacement" if field == "uid" else root["metadata"][field] + 1
        )
    elif field == "spec":
        root["spec"]["unexpected"] = "changed"
    else:
        root["metadata"]["annotations"][origin.TOKEN] = "replacement"
    with pytest.raises(
        ValueError, match="identity|generation|specification|provenance"
    ):
        custody.verify(request, q.readers, q.backend)


def test_csi_journal_remap_during_verification_holds(custody_case, monkeypatch):
    q = custody_case
    original = q.reader.resolve_csi
    calls = 0

    def remapped(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls > 1:
            result = result.model_copy(
                update={"value": {**result.value, "image_id": "remapped"}}
            )
        return result

    monkeypatch.setattr(q.reader, "resolve_csi", remapped)
    with pytest.raises(ValueError, match="journal"):
        custody.verify(prepare(q), q.readers, q.backend)


def test_retained_binding_immutable_remap_rejected(custody_case):
    from app.services.kubevirt_storage_reports import retain_verified_bindings

    q = custody_case
    _, verified = publish(q)
    binding = verified.bindings[0]
    changed = binding.model_copy(
        update={"image_key": binding.image_key.model_copy(update={"image_id": "other"})}
    )
    with q.sessions() as db, pytest.raises(ValueError, match="immutable identity"):
        retain_verified_bindings(db, [changed], str(q.pins.domain_id))


def _retirement_observation(q, monkeypatch, *, survives=False):
    from app.schemas.kubevirt_storage import BackendImageState, ReadResult
    from app.services import native_binding_retirement as retirement

    monkeypatch.setattr(retirement, "datetime", FixedDatetime)
    binding = q.report.volume_bindings[0]
    with q.sessions() as db:
        prepared = custody.prepare_retirement(db, str(q.intent.id), binding.pvc_uid)
        db.commit()
    original_pvs, original_pvcs = q.reader.pvs, q.reader.pvcs
    monkeypatch.setattr(
        q.reader,
        "pvs",
        lambda: [p for p in original_pvs() if p["metadata"]["uid"] != binding.pv_uid],
    )
    monkeypatch.setattr(
        q.reader,
        "pvcs",
        lambda ns: [
            p for p in original_pvcs(ns) if p["metadata"]["uid"] != binding.pvc_uid
        ],
    )
    monkeypatch.setattr(q.reader, "snapshots", lambda ns: [], raising=False)
    monkeypatch.setattr(q.reader, "snapshot_contents", lambda: [], raising=False)
    monkeypatch.setattr(
        q.backend,
        "image_inventory",
        lambda key, **kw: ReadResult(
            status="complete",
            collected_at=FixedDatetime.current,
            value=BackendImageState(
                **key.model_dump(),
                active=survives,
                trash=False,
                collected_at=FixedDatetime.current,
            ),
        ),
        raising=False,
    )
    return retirement.verify(prepared, q.reader, q.backend)


def test_origin_retirement_selected_for_collector_and_revalidates_source(
    custody_case, monkeypatch
):
    from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment
    from app.services import native_binding_retirement as retirement
    from app.services import native_binding_retirement_collection as collection
    from app.services.kubevirt_storage_enrollment import validated_enrollment

    q = custody_case
    publish(q)
    verified = _retirement_observation(q, monkeypatch)
    assert verified.prepared.source_kind == "origin"
    assert "administrator" not in verified.prepared.model_dump()
    with q.sessions() as db:
        reference = custody.apply_retirement(db, verified)
        db.commit()
        assert custody.apply_retirement(db, verified) == reference
        assert retirement.resolve(db, reference) == verified
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        manifest = validated_enrollment(db.get(NativeEnrollment, domain.enrollment_id))
        assert collection.protected_inputs(db, manifest) == [(reference, verified)]
        _, receipt, _ = custody.retained(db, db.get(Project, q.project_id))
        assert custody.active_bindings(db, receipt) == []
        assert receipt.complete is False
    with q.sessions() as db:
        db.get(Project, q.project_id).host_id = None
        db.commit()
    with q.sessions() as db, pytest.raises(ValueError, match="provider"):
        retirement.resolve(db, reference)


def test_surviving_private_head_rejects_origin_retirement(custody_case, monkeypatch):
    q = custody_case
    publish(q)
    with pytest.raises(ValueError, match="private image remains"):
        _retirement_observation(q, monkeypatch, survives=True)


def test_selected_retirement_survives_checkpoint_history_boundary(
    custody_case, monkeypatch
):
    from uuid import uuid4

    from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment
    from app.schemas.kubevirt_storage import BindingRetirementEvidence
    from app.services import native_binding_retirement as retirement
    from app.services import native_binding_retirement_collection as collection
    from app.services.kubevirt_storage_enrollment import validated_enrollment

    q = custody_case
    publish(q)
    verified = _retirement_observation(q, monkeypatch)
    with q.sessions() as db:
        reference = custody.apply_retirement(db, verified)
        db.commit()
    # Retirement publication is already one successor after its source.
    for distance in range(2, 130):
        publish(q, mode="enroll")
        if distance < 127:
            continue
        with q.sessions() as db:
            domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
            manifest = validated_enrollment(
                db.get(NativeEnrollment, domain.enrollment_id)
            )
            assert retirement.resolve(db, reference) == verified
            assert collection.protected_inputs(db, manifest) == [(reference, verified)]
            report = q.report.model_copy(
                update={
                    "binding_retirements": [
                        BindingRetirementEvidence(
                            reference=reference,
                            observation=verified.observation,
                        )
                    ],
                    "retiring_roots": [],
                    "started_at": verified.observation.verified_at,
                    "completed_at": verified.observation.verified_at,
                }
            )
            observations, _ = collection.report_authority(db, manifest, report)
            assert observations == [verified.observation]
            _, receipt, _ = custody.retained(db, db.get(Project, q.project_id))
            assert custody.active_bindings(db, receipt) == []
            if distance >= 128:
                # A different, unselected request cannot borrow the selected
                # retirement's authority to bypass bounded chain validation.
                unselected = verified.prepared.model_copy(
                    update={"request_id": uuid4()}
                )
                with pytest.raises(ValueError, match="ancestry limit"):
                    custody.retirement_source(db, unselected)
                changed = verified.prepared.model_copy(
                    update={"pvc_name": "different-pvc"}
                )
                with pytest.raises(
                    ValueError, match="selected retirement source changed"
                ):
                    custody.retirement_source(db, changed)
                changed = verified.prepared.model_copy(
                    update={"source_custody_digest": "0" * 64}
                )
                with pytest.raises(ValueError, match="retirement source changed"):
                    custody.retirement_source(db, changed)


def test_legacy_retirement_serialization_digest_unchanged():
    from uuid import uuid4

    from app.schemas.native_binding_retirement import PreparedBindingRetirement
    from tests.kubevirt_storage_helpers import report_fixture

    binding = report_fixture("zero-private-parent.json").volume_bindings[0]
    raw = dict(
        request_id=str(uuid4()),
        target_id=str(uuid4()),
        domain_id=str(uuid4()),
        source_custody_id=str(uuid4()),
        source_custody_digest="a" * 64,
        root=binding.root.model_dump(mode="json"),
        binding=binding.model_dump(mode="json"),
        pvc_name="disk",
        ancestry=None,
        administrator="real-reviewer",
        reason="reviewed",
        prepared_at=FixedDatetime.current.isoformat().replace("+00:00", "Z"),
    )
    value = PreparedBindingRetirement.model_validate(raw)
    assert value.model_dump(mode="json") == raw
    assert digest(value) == digest(raw)
    with pytest.raises(ValueError, match="Administrator"):
        PreparedBindingRetirement.model_validate({**raw, "administrator": None})


@pytest.mark.parametrize("duplicate", [False, True])
def test_domain_selection_matches_explicit_provider_cluster(
    custody_case, monkeypatch, duplicate
):
    from uuid import uuid4

    from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment

    q = custody_case
    other_id, enrollment_id = str(uuid4()), str(uuid4())
    with q.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        raw = deepcopy(db.get(NativeEnrollment, domain.enrollment_id).payload)
        raw["pins"]["domain_id"] = other_id
        if not duplicate:
            raw["pins"]["provider_clusters"][0]["provider_id"] = str(uuid4())
        raw["digest"] = digest({k: v for k, v in raw.items() if k != "digest"})
        db.add(
            NativeEnrollment(
                id=enrollment_id, domain_id=other_id, digest=raw["digest"], payload=raw
            )
        )
        db.add(NativeCollectionDomain(id=other_id, enrollment_id=enrollment_id))
        db.commit()
    profiles = [
        {"domain_id": str(q.pins.domain_id), "origin_admission": True},
        {"domain_id": other_id, "origin_admission": True},
    ]
    from types import SimpleNamespace

    monkeypatch.setattr(custody, "config", SimpleNamespace(get=lambda *_: profiles))
    with q.sessions() as db:
        assert custody.config.get("native_collection.profiles", []) == profiles
        if duplicate:
            with pytest.raises(ValueError, match="matching explicit"):
                custody.domain_for(db, q.intent)
        else:
            assert custody.domain_for(db, q.intent) == str(q.pins.domain_id)


def test_delayed_report_cannot_restore_authority_after_publication(custody_case):
    from datetime import timedelta

    from app.models.kubevirt_storage import NativeCollectionDomain
    from app.services.kubevirt_storage_reports import import_report
    from tests.test_kubevirt_storage_reports import revised

    q = custody_case
    delayed = revised(
        q.report,
        started_at=FixedDatetime.current - timedelta(seconds=2),
        completed_at=FixedDatetime.current - timedelta(seconds=1),
    )
    publish(q, mode="enroll")
    with q.sessions() as db:
        import_report(
            db,
            delayed,
            enrollment_id=q.enrollment_id,
            expected_enrollment_digest=q.digest,
            now=FixedDatetime.current,
        )
        db.commit()
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        assert domain.latest_report_id is None


def _postgres(q):
    if q.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL row-lock regression")


def _contender(q, value, waiting, kind):
    import threading

    from sqlalchemy import event

    engine = q.sessions.kw["bind"]
    ident = threading.get_ident()

    def before(_conn, _cursor, statement, _params, _context, _many):
        if (
            threading.get_ident() == ident
            and "FOR UPDATE" in statement
            and kind in statement
        ):
            waiting.set()

    event.listen(engine, "before_cursor_execute", before)
    try:
        with q.sessions() as db:
            try:
                result = custody.apply(db, value)
                db.commit()
                return result.id
            except Exception as exc:
                db.rollback()
                return exc
    finally:
        event.remove(engine, "before_cursor_execute", before)


@pytest.mark.parametrize("change", ["cancel", "owner", "provider", "cleanup"])
def test_postgres_identity_and_cleanup_change_while_publication_waits(
    custody_case, change
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from uuid import uuid4

    from app.models.host import Host
    from app.models.user import User
    from app.services.quota_locks import lock_project

    q = custody_case
    _postgres(q)
    value = custody.verify(prepare(q, mode="enroll"), q.readers, q.backend)
    waiting = Event()
    with ThreadPoolExecutor(max_workers=1) as pool, q.sessions() as holder:
        project, _, _ = lock_project(holder, q.project_id, require_organization=False)
        future = pool.submit(_contender, q, value, waiting, "users")
        assert waiting.wait(5)
        if change == "cancel":
            origin.cancel(holder, q.project_id)
        elif change == "cleanup":
            project.state = "cleanup_pending"
        elif change == "owner":
            owner = User(email=f"racer-{uuid4()}@example.test")
            holder.add(owner)
            holder.flush()
            project.owner_id = owner.id
        else:
            provider = Provider(name=str(uuid4()), type="kubevirt")
            holder.add(provider)
            holder.flush()
            holder.get(Host, project.host_id).provider_id = provider.id
        holder.commit()
        result = future.result(timeout=8)
    assert isinstance(result, ValueError)
    assert "changed" in str(result) or "holds" in str(result)


@pytest.mark.parametrize("ordinary_enrollment", [False, True])
def test_postgres_checkpoint_and_enrollment_compete_on_exact_predecessor(
    custody_case, ordinary_enrollment
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.services import kubevirt_storage_enrollment as enrollment

    q = custody_case
    _postgres(q)
    if ordinary_enrollment:
        _domain_without_origin(q)
    request = prepare(q, mode="enroll")
    value = custody.verify(request, q.readers, q.backend)
    second = custody.verify(prepare(q, mode="enroll"), q.readers, q.backend)
    waiting = Event()
    with ThreadPoolExecutor(max_workers=1) as pool, q.sessions() as holder:
        if ordinary_enrollment:
            enrollment.enroll_native_roots(
                holder, value.enrollment, expected_target_id=str(q.target_id)
            )
        else:
            custody.apply(holder, value)
        future = pool.submit(_contender, q, second, waiting, "native_reporting_targets")
        assert waiting.wait(5)
        holder.commit()
        result = future.result(timeout=8)
    assert isinstance(result, ValueError)
    assert "predecessor changed" in str(result)


def test_origin_custody_migration_installs_immutable_triggers(custody_case):
    import importlib.util
    from pathlib import Path
    from uuid import uuid4

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import DBAPIError

    q = custody_case
    _postgres(q)

    def migration(filename):
        path = Path(__file__).parents[1] / "alembic/versions" / filename
        spec = importlib.util.spec_from_file_location(filename, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    module = migration("f31db95ac602_native_origin_custody.py")
    with q.sessions.kw["bind"].begin() as connection, Operations.context(
        MigrationContext.configure(connection)
    ):
        connection.execute(
            text(
                "CREATE FUNCTION reject_native_origin_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable'; END $$"
            )
        )
        module.downgrade()
        module.upgrade()
        assert (
            inspect(connection)
            .get_columns("native_origin_custody_requests")[0]["type"]
            .__class__.__name__
            == "UUID"
        )
        identity = str(uuid4())
        connection.execute(
            text(
                "INSERT INTO native_origin_custody_requests(id,digest,payload) VALUES (:id,:digest,'{}')"
            ),
            {"id": identity, "digest": "a" * 64},
        )
        for operation in (
            "UPDATE native_origin_custody_requests SET digest='changed'",
            "DELETE FROM native_origin_custody_requests",
        ):
            with pytest.raises(
                DBAPIError, match="immutable"
            ), connection.begin_nested():
                connection.execute(text(operation))
        module.downgrade()
        module.upgrade()


def test_reconfigure_accepts_only_scoped_storage_checkpoint(custody_case):
    q = custody_case
    uid = q.reader.pvcs(q.root.namespace)[0]["metadata"]["uid"]
    with q.sessions() as db:
        project = db.get(Project, q.project_id)
        project.state = "reconfiguring"
        # Use the evidence clock rather than the PostgreSQL container clock.
        project.updated_at = FixedDatetime.current
        db.commit()
    publish(q, mode="removal", pvc_uids=[uid])
    with pytest.raises(ValueError, match="operation state"):
        prepare(q)
    with q.sessions() as db:
        assert (
            custody.prepare_retirement(db, str(q.intent.id), uid).source_kind
            == "origin"
        )


def test_missing_retained_binding_holds_protected_reads(custody_case):
    from app.models.kubevirt_storage import NativeRetainedBinding
    from app.services.kubevirt_storage_reports import binding_key

    q = custody_case
    _, value = publish(q)
    with q.sessions() as db:
        db.delete(db.get(NativeRetainedBinding, binding_key(value.bindings[0])))
        db.commit()
    with q.sessions() as db, pytest.raises(ValueError, match="binding missing"):
        custody.retained(db, db.get(Project, q.project_id))


@pytest.mark.parametrize("retiring", [False, True])
@pytest.mark.parametrize("cache_growth", [False, True])
@pytest.mark.parametrize("wrong_cluster", [False, True])
def test_configured_runtime_constructs_and_closes_independent_readers(
    growing_cache_case,
    monkeypatch,
    retiring,
    cache_growth,
    wrong_cluster,
):
    from types import SimpleNamespace

    from app.services import kubevirt_storage_inventory as inventory_service
    from app.services import native_collection_config as configuration
    from app.services import native_collection_runtime as runtime
    from app.services import native_provider_reader as provider_service

    q = growing_cache_case
    if cache_growth:
        _grow_cache_authority(q)
    if retiring:
        publish(q)
        reference = _retirement_observation(q, monkeypatch).prepared
    else:
        request = prepare(q, mode="enroll")
    args = SimpleNamespace(
        command_timeout_seconds=15,
        verification_timeout_seconds=55,
        kubeconfig="/protected/config",
    )
    inventory = SimpleNamespace(
        capability=None, collector_id=q.pins.collector_ids[0], csi_requests={}
    )
    monkeypatch.setattr(runtime, "profile_for", lambda _: args)
    monkeypatch.setattr(
        configuration,
        "_managed_inputs",
        lambda _: (q.pins, inventory, SimpleNamespace()),
    )
    constructed, closed = [], []

    def backend(*args):
        constructed.append("backend")
        return q.backend

    original_api = q.reader.api

    class Api:
        def __call__(self, namespace):
            return original_api(namespace)

        def close(self):
            closed.append("kube")

    q.reader.api = Api()

    def reader(path, backend, **kwargs):
        assert path == "/protected/config" and backend is q.backend
        assert kwargs["cache_roots"] == [
            r.model_dump(mode="json") for r in q.pins.cache_roots
        ] + (
            [
                dict(
                    cluster_key="cluster",
                    namespace="second-cache",
                    namespace_uid="second-cache-uid",
                )
            ]
            if cache_growth
            else []
        )
        constructed.append("kube")
        return q.reader

    monkeypatch.setattr(configuration, "_backend_reader", backend)
    monkeypatch.setattr(inventory_service, "KubernetesReader", reader)

    def provider_reader(provider, limits, deadline):
        assert str(provider.id) == str(q.pins.provider_clusters[0].provider_id)
        assert limits.command_timeout_seconds == 45
        assert limits.scan_timeout_seconds == 900
        constructed.append("provider")
        return SimpleNamespace(
            api=q.reader.api,
            cluster_identity=(lambda: "wrong-cluster")
            if wrong_cluster
            else q.reader.cluster_identity,
            close=lambda: closed.append("provider"),
        )

    monkeypatch.setattr(provider_service, "ProviderReader", provider_reader)
    if wrong_cluster:
        with pytest.raises(ValueError, match="differs from protected provider cluster"):
            if retiring:
                custody.verify_retirement_configured(reference)
            else:
                custody.verify_configured(request)
        assert closed == ["provider", "kube"]
        return
    if retiring:
        result = custody.verify_retirement_configured(reference)
        assert result.prepared == reference
    else:
        result = custody.verify_configured(request)
        assert result.prepared == request
    assert constructed == ["backend", "kube", "provider"]
    assert closed == ["provider", "kube"]
