"""Reconciliation retains original evidence and requires reviewed, fresh custody."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.models.native_custody import NativeCustodyReceipt
from app.models.project import Project
from app.schemas.quota import AccountingUnavailable
from app.services import native_online_enrollment as online
from tests import test_native_online_enrollment as fixtures

online_case = fixtures.online_case
cleanup_case = fixtures.cleanup_case
deploy_case = fixtures.deploy_case
isolated_quota_database = fixtures.isolated_quota_database
native_ledger = fixtures.native_ledger
resume_case = fixtures.resume_case
verified = fixtures.verified


@pytest.fixture(autouse=True)
def idle_queue(monkeypatch):
    from app.core import redis

    monkeypatch.setattr(redis, "is_redis_available", lambda: True)
    monkeypatch.setattr(
        redis, "get_redis", lambda: SimpleNamespace(get=lambda key: None)
    )


def prepared_transition(q):
    from app.services import native_custody_reconciliation as reconcile

    selected = [q.project_ids[0]]
    with q.sessions() as db:
        q.prepared = online.prepare(
            db, q.project_ids, accounting_only_project_ids=selected
        )
        db.commit()
    original = verified(q)
    with q.sessions() as db:
        online.apply(db, original, expected_target_id=q.target_id)
        db.commit()
        originals = {r.id: deepcopy(r.payload) for r in db.query(NativeCustodyReceipt)}
        q.prepared = reconcile.prepare(
            db,
            q.project_ids,
            cleanup_authority_project_ids=selected,
            reason="Reviewed repaired helpers",
        )
        db.commit()
    fresh = verified(q)
    with q.sessions() as db:
        review = reconcile.review(db, fresh)
    return reconcile, selected, originals, fresh, review


def test_explicit_promotion_is_append_only_and_read_only(online_case):
    q = online_case
    before = {k: deepcopy(v.values) for k, v in q.apis.items()}
    reconcile, selected, originals, fresh, review = prepared_transition(q)
    with q.sessions() as db:
        ids = reconcile.apply(
            db,
            fresh,
            expected_target_id=q.target_id,
            cleanup_authority_project_ids=selected,
            review_digest=review["digest"],
        )
        db.commit()
        assert {r.id: r.payload for r in db.query(NativeCustodyReceipt)} == originals
        row, receipt, _ = online.retained(db, db.get(Project, selected[0]))
        assert row.id in ids and row.id not in originals
        online._require_cleanup_authority(receipt, selected[0])
        assert len(set(ids) - set(originals)) == 1
        with pytest.raises(AccountingUnavailable, match="predecessor"):
            reconcile.apply(
                db,
                fresh,
                expected_target_id=q.target_id,
                cleanup_authority_project_ids=selected,
                review_digest=review["digest"],
            )
    assert before == {k: v.values for k, v in q.apis.items()}
    assert all(not api.calls for api in q.apis.values())


@pytest.mark.parametrize("failure", ["implicit", "digest", "stale", "anchor"])
def test_reconciliation_rejects_unreviewed_or_unsafe_apply(
    online_case, monkeypatch, failure
):
    q = online_case
    reconcile, selected, _, fresh, review = prepared_transition(q)
    if failure == "stale":
        from tests.test_native_managed_resume import FixedDatetime

        class Later(FixedDatetime):
            @classmethod
            def now(cls, tz=None):
                return FixedDatetime.current + timedelta(seconds=61)

        monkeypatch.setattr(online, "datetime", Later)
    with q.sessions() as db:
        if failure == "anchor":
            sibling = next(p for p in q.project_ids if p not in selected)
            online.cleanup_anchor(db, db.get(Project, sibling), [])
            db.commit()
        with pytest.raises(AccountingUnavailable):
            reconcile.apply(
                db,
                fresh,
                expected_target_id=q.target_id,
                cleanup_authority_project_ids=[] if failure == "implicit" else selected,
                review_digest="0" * 64 if failure == "digest" else review["digest"],
            )


@pytest.mark.parametrize(
    "field", ["identity", "root", "bindings", "ancestry", "target", "domain"]
)
def test_immutable_evidence_is_never_retargeted(online_case, field):
    reconcile, selected, _, _, review = prepared_transition(online_case)
    transition = deepcopy(review["review"]["projects"][selected[0]])
    transition["after"][field] = {"changed": True}
    with pytest.raises(AccountingUnavailable, match="changed"):
        reconcile._transition(transition, selected[0], selected)


@pytest.mark.parametrize(
    "tamper", ["predecessor", "receipt", "review", "enrollment", "bound"]
)
def test_successor_chain_is_validated_on_every_read(online_case, monkeypatch, tamper):
    from app.models.native_custody import NativeCustodySuccessor

    q = online_case
    reconcile, selected, _, fresh, review = prepared_transition(q)
    with q.sessions() as db:
        ids = reconcile.apply(
            db,
            fresh,
            expected_target_id=q.target_id,
            cleanup_authority_project_ids=selected,
            review_digest=review["digest"],
        )
        db.commit()
        row = db.get(NativeCustodySuccessor, ids[0])
        if tamper == "predecessor":
            row.predecessor_digest = "0" * 64
        elif tamper == "receipt":
            row.digest = "0" * 64
        elif tamper == "bound":
            monkeypatch.setattr(reconcile, "MAX_SUCCESSORS", 0)
        else:
            value = deepcopy(row.payload)
            value[
                "enrollment_id" if tamper == "enrollment" else "review_digest"
            ] = "changed"
            row.payload = value
        db.flush()
        with pytest.raises(AccountingUnavailable):
            online.retained(db, db.get(Project, selected[0]))


@pytest.mark.parametrize("attribute", ["owner_id", "provider_id"])
def test_changed_database_identity_rejects_promotion(online_case, attribute):
    from app.models.provider import Provider
    from app.models.user import User

    q = online_case
    reconcile, selected, _, fresh, review = prepared_transition(q)
    with q.sessions() as db:
        replacement = None
        if attribute == "owner_id":
            user = User(email="new-owner@test.invalid")
            db.add(user)
            db.flush()
            replacement = user.id
        else:
            provider = Provider(name="other-provider", type="kubevirt")
            db.add(provider)
            db.flush()
            replacement = provider.id
        setattr(db.get(Project, selected[0]), attribute, replacement)
        db.commit()
        with pytest.raises((AccountingUnavailable, ValueError)):
            reconcile.apply(
                db,
                fresh,
                expected_target_id=q.target_id,
                cleanup_authority_project_ids=selected,
                review_digest=review["digest"],
            )


def test_accounting_only_verification_cannot_promote(online_case):
    reconcile, selected, _, _, review = prepared_transition(online_case)
    transition = deepcopy(review["review"]["projects"][selected[0]])
    transition["after"]["authority"] = "accounting_only"
    with pytest.raises(AccountingUnavailable, match="Accounting-only"):
        reconcile._transition(transition, selected[0], selected)


@pytest.mark.parametrize("tracking", ["unavailable", "active", "error"])
def test_unknown_or_active_tracking_blocks_apply(online_case, monkeypatch, tracking):
    from app.core import redis

    q = online_case
    reconcile, selected, _, fresh, review = prepared_transition(q)
    if tracking == "unavailable":
        monkeypatch.setattr(redis, "is_redis_available", lambda: False)
    elif tracking == "active":
        monkeypatch.setattr(
            redis, "get_redis", lambda: SimpleNamespace(get=lambda key: "job-id")
        )
    else:

        def unavailable():
            raise ConnectionError("unavailable")

        monkeypatch.setattr(redis, "get_redis", unavailable)
    with q.sessions() as db, pytest.raises(AccountingUnavailable):
        reconcile.apply(
            db,
            fresh,
            expected_target_id=q.target_id,
            cleanup_authority_project_ids=selected,
            review_digest=review["digest"],
        )


def test_replaced_helper_requires_fresh_full_verification_and_exact_review(online_case):
    q = online_case
    reconcile, selected, _, _, old_review = prepared_transition(q)
    helper = next(
        v for (kind, _), v in q.apis[selected[0]].values.items() if kind == "Deployment"
    )
    helper["metadata"]["uid"] += "-replacement"
    fresh = verified(q)
    with q.sessions() as db:
        review = reconcile.review(db, fresh)
        assert review["digest"] != old_review["digest"]
        with pytest.raises(AccountingUnavailable, match="review digest"):
            reconcile.apply(
                db,
                fresh,
                expected_target_id=q.target_id,
                cleanup_authority_project_ids=selected,
                review_digest=old_review["digest"],
            )
        reconcile.apply(
            db,
            fresh,
            expected_target_id=q.target_id,
            cleanup_authority_project_ids=selected,
            review_digest=review["digest"],
        )
        db.commit()
        _, receipt, _ = online.retained(db, db.get(Project, selected[0]))
        assert any(
            p["uid"] == helper["metadata"]["uid"]
            for p in receipt.object_identities[selected[0]]
        )


def add_bound_volume(q, project_id, monkeypatch):
    api = q.apis[project_id]
    pvc = deepcopy(api.values["PersistentVolumeClaim", "retained-disk"])
    pvc["metadata"].update(name="added-disk", uid="added-pvc-uid")
    pvc["spec"]["volumeName"] = "added-pv"
    pv = deepcopy(api.values["PersistentVolume", "retained-pv"])
    pv["metadata"].update(name="added-pv", uid="added-pv-uid")
    pv["spec"]["claimRef"] = {
        k: pvc["metadata"][k] for k in ("name", "namespace", "uid")
    }
    pv["spec"]["csi"]["volumeHandle"] = "added-volume-handle"
    api.values["PersistentVolumeClaim", "added-disk"] = pvc
    api.values["PersistentVolume", "added-pv"] = pv
    source = next(
        b for b in q.report.volume_bindings if str(b.root.project_id) == project_id
    )
    added = source.model_copy(
        update={
            "pvc_uid": "added-pvc-uid",
            "pv_uid": "added-pv-uid",
            "volume_handle": "added-volume-handle",
            "image_key": source.image_key.model_copy(
                update={"image_id": "added-image"}
            ),
        }
    )
    q.report = q.report.model_copy(
        update={"volume_bindings": [*q.report.volume_bindings, added]}
    )
    original_pvs, original_pvcs = q.reader.pvs, q.reader.pvcs
    monkeypatch.setattr(q.reader, "pvs", lambda: [*original_pvs(), deepcopy(pv)])

    def pvcs(namespace):
        result = original_pvcs(namespace)
        for value in result:
            if value["metadata"]["uid"] == "added-pvc-uid":
                value["spec"]["volumeName"] = "added-pv"
        return result

    monkeypatch.setattr(q.reader, "pvcs", pvcs)


@pytest.mark.parametrize("add_storage", [False, True])
@pytest.mark.parametrize("authorization", ["valid", "missing", "wrong"])
def test_reviewed_generation_transition_retains_original_storage(
    online_case, monkeypatch, add_storage, authorization
):
    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeEnrollment,
        NativeProjectAttempt,
    )
    from app.schemas.kubevirt_storage_enrollment import DomainExtension

    q = online_case
    reconcile, selected, _, _, _ = prepared_transition(q)
    roots = [
        r.model_copy(update={"deployment_generation": r.deployment_generation + 1})
        if str(r.project_id) == selected[0]
        else r
        for r in q.online_pins.project_roots
    ]
    changed = q.online_pins.model_copy(update={"project_roots": roots})
    with q.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        previous = db.get(NativeEnrollment, domain.enrollment_id)
        extension = DomainExtension(
            version="native-domain-extension-v1",
            previous_enrollment_id=previous.id,
            previous_enrollment_digest=previous.digest,
            root_transition_digest=online.enrollment.root_transition_digest(
                q.online_pins, changed
            ),
        )
        if authorization != "valid":
            extension = extension.model_copy(
                update={
                    "root_transition_digest": None
                    if authorization == "missing"
                    else "0" * 64
                }
            )
    q.online_pins = changed.model_copy(update={"domain_extension": extension})
    root = next(
        v
        for (kind, _), v in q.apis[selected[0]].values.items()
        if kind == "TroshkaProject"
    )
    root["metadata"]["generation"] += 1
    root["status"]["observedGeneration"] = root["metadata"]["generation"]
    if add_storage:
        add_bound_volume(q, selected[0], monkeypatch)
    fresh = verified(q)
    with q.sessions() as db:
        if authorization != "valid":
            with pytest.raises((AccountingUnavailable, ValueError)):
                reconcile.review(db, fresh)
            return
        review = reconcile.review(db, fresh)
        additions = review["review"]["projects"][selected[0]]["storage_transition"][
            "added_bindings"
        ]
        assert len(additions) == int(add_storage)
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        # Sibling observation cursors still refer to the old enrollment until
        # retirement invalidation validates their exact current root identity.
        for pinned in q.online_pins.project_roots:
            cursor = db.get(NativeProjectAttempt, (domain.id, str(pinned.project_id)))
            if cursor is None:
                db.add(
                    NativeProjectAttempt(
                        domain_id=domain.id,
                        project_id=str(pinned.project_id),
                        root_key=online.enrollment.root_key(pinned),
                        enrollment_id=previous.id,
                    )
                )
        db.flush()
        from tests.test_native_managed_resume import FixedDatetime

        future_watermark = FixedDatetime.current + timedelta(seconds=120)
        domain.latest_started_at, domain.conflicted = future_watermark, True
        for cursor in db.query(NativeProjectAttempt).filter_by(domain_id=domain.id):
            cursor.latest_started_at, cursor.conflicted = future_watermark, True
        prior_observations = reconcile._retirement_observation_state(db, domain.id)
        reconcile.apply(
            db,
            fresh,
            expected_target_id=q.target_id,
            cleanup_authority_project_ids=selected,
            review_digest=review["digest"],
        )
        db.commit()
        row, receipt, retained_root = online.retained(db, db.get(Project, selected[0]))
        assert retained_root.deployment_generation == root["metadata"]["generation"]
        assert row.payload["enrollment_id"] != previous.id
        assert len(receipt.bindings) == len(fresh["bindings"])
        current_domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        cursors = (
            db.query(NativeProjectAttempt).filter_by(domain_id=current_domain.id).all()
        )
        assert any(
            c.enrollment_id == previous.id
            for c in cursors
            if c.project_id != selected[0]
        )
        identities = {c.project_id: c.root_key for c in cursors}
        reconcile._invalidate_retirement_observations(
            db, current_domain.id, prior_observations
        )
        assert all(c.enrollment_id == current_domain.enrollment_id for c in cursors)
        assert {c.project_id: c.root_key for c in cursors} == identities
        assert all(
            c.conflicted
            and online.enrollment.utc(c.latest_started_at) == future_watermark
            for c in cursors
        )
        assert (
            current_domain.conflicted
            and online.enrollment.utc(current_domain.latest_started_at)
            == future_watermark
        )
        # Historical custody binds its own exact enrollment, not today's domain cursor.
        db.get(
            NativeCollectionDomain, str(q.pins.domain_id)
        ).enrollment_id = previous.id
        db.flush()
        assert online.retained(db, db.get(Project, selected[0]))[0].id == row.id
        payload = deepcopy(row.payload)
        payload["enrollment_digest"] = "0" * 64
        row.payload = payload
        db.flush()
        with pytest.raises(AccountingUnavailable, match="manifest changed"):
            online.retained(db, db.get(Project, selected[0]))


@pytest.mark.parametrize("drop_old", [False, True])
def test_additive_storage_requires_every_old_binding(
    online_case, monkeypatch, drop_old
):
    q = online_case
    reconcile, selected, _, _, _ = prepared_transition(q)
    add_bound_volume(q, selected[0], monkeypatch)
    fresh = verified(q)
    if drop_old:
        fresh["bindings"] = [
            b
            for b in fresh["bindings"]
            if b["root"]["project_id"] != selected[0] or b["pvc_uid"] == "added-pvc-uid"
        ]
        fresh["digest"] = reconcile.digest(
            {k: v for k, v in fresh.items() if k != "digest"}
        )
    with q.sessions() as db:
        if drop_old:
            with pytest.raises(AccountingUnavailable, match="cannot drop"):
                reconcile.review(db, fresh)
            return
        review = reconcile.review(db, fresh)
        reconcile.apply(
            db,
            fresh,
            expected_target_id=q.target_id,
            cleanup_authority_project_ids=selected,
            review_digest=review["digest"],
        )
        db.commit()
        _, receipt, _ = online.retained(db, db.get(Project, selected[0]))
        assert (
            len([b for b in receipt.bindings if str(b.root.project_id) == selected[0]])
            == 2
        )


def test_ancestry_resolver_loads_first_proof_from_effective_successor(
    online_case, monkeypatch
):
    from app.services import native_ancestry_custody as ancestry

    q = online_case
    reconcile, selected, _, fresh, _ = prepared_transition(q)
    receipt = online._validated(fresh)
    root = next(
        r
        for r in receipt.enrollment.pins.project_roots
        if str(r.project_id) == selected[0]
    )
    cache = receipt.enrollment.pins.cache_roots[0]
    proof = SimpleNamespace(binding=SimpleNamespace(root=root), source=cache)
    effective = reconcile.effective
    retained = online.retained

    def with_successor(db, project_id):
        row, value = effective(db, project_id)
        if project_id == selected[0]:
            value = value.model_copy(update={"ancestry": [proof]})
        return row, value

    def retained_successor(db, project, **kwargs):
        row, _, original_root = retained(db, project, **kwargs)
        return row, with_successor(db, project.id)[1], original_root

    monkeypatch.setattr(reconcile, "effective", with_successor)
    monkeypatch.setattr(online, "retained", retained_successor)
    with q.sessions() as db:
        assert ancestry.resolve(db, receipt.enrollment, set()) == [proof]


def replace_bound_volume(q, project_id, monkeypatch):
    from app.schemas.kubevirt_storage import ReadResult
    from tests.test_native_managed_resume import FixedDatetime

    add_bound_volume(q, project_id, monkeypatch)
    api = q.apis[project_id]
    api.values.pop(("PersistentVolumeClaim", "retained-disk"))
    api.values.pop(("PersistentVolume", "retained-pv"))

    def pvs():
        result = []
        for index, current in enumerate(q.apis.values()):
            for (kind, name), value in current.values.items():
                if kind == "PersistentVolume":
                    item = deepcopy(value)
                    if name == "retained-pv":
                        item["metadata"]["name"] = f"retained-pv-{index}"
                    result.append(item)
        return result

    def pvcs(namespace):
        current = q.reader.api(namespace)
        result = deepcopy(current.rows("PersistentVolumeClaim", namespace))
        for value in result:
            if value["metadata"]["name"] == "retained-disk":
                value["spec"][
                    "volumeName"
                ] = f"retained-pv-{list(q.apis.values()).index(current)}"
        return result

    monkeypatch.setattr(q.reader, "pvs", pvs)
    monkeypatch.setattr(q.reader, "pvcs", pvcs)
    monkeypatch.setattr(
        q.reader,
        "all_pvcs",
        lambda: [
            v for a in q.apis.values() for v in a.rows("PersistentVolumeClaim", None)
        ],
        raising=False,
    )
    monkeypatch.setattr(q.reader, "all_snapshots", lambda: [], raising=False)
    monkeypatch.setattr(q.reader, "snapshots", lambda namespace: [], raising=False)
    monkeypatch.setattr(q.reader, "snapshot_contents", lambda: [], raising=False)

    def absent(key, **kwargs):
        return ReadResult(
            status="complete",
            collected_at=FixedDatetime.current,
            value={
                **key.model_dump(mode="json"),
                "active": False,
                "trash": False,
                "collected_at": FixedDatetime.current,
            },
        )

    monkeypatch.setattr(q.backend, "image_inventory", absent, raising=False)


@pytest.mark.parametrize(
    "timing", ["fresh", "aged", "before_prepare", "after_final", "image_after_final"]
)
def test_retirement_replacement_refreshes_absence_and_preserves_history(
    online_case, monkeypatch, timing
):
    from app.services import native_binding_retirement as retirement
    from tests.test_native_managed_resume import FixedDatetime

    q = online_case
    reconcile, selected, originals, _, _ = prepared_transition(q)
    monkeypatch.setattr(retirement, "datetime", FixedDatetime)
    with q.sessions() as db:
        _, source, _ = online.retained(db, db.get(Project, selected[0]))
        binding = next(
            b for b in source.bindings if str(b.root.project_id) == selected[0]
        )
        prepared = retirement.prepare(
            db,
            selected[0],
            binding.pvc_uid,
            administrator="reviewer",
            reason="Old disk independently absent",
        )
        db.commit()
    replace_bound_volume(q, selected[0], monkeypatch)
    absent = retirement.verify(prepared, q.reader, q.backend)
    with q.sessions() as db:
        reference = retirement.apply(
            db, absent, review_digest=retirement.review(absent)["digest"]
        )
        db.commit()
        q.prepared = reconcile.prepare(
            db,
            q.project_ids,
            cleanup_authority_project_ids=selected,
            reason="Reviewed replacement storage",
            binding_retirements=[reference],
        )
        db.commit()
    # The immutable reference remains valid while online verification refreshes
    # its absence observations instead of pretending old observations are fresh.
    FixedDatetime.current += timedelta(seconds=120)
    fresh = verified(q)
    if timing != "fresh":
        observation = fresh["binding_retirements"][0]["observation"]
        offset = {
            "aged": -120,
            "before_prepare": -121,
            "after_final": 1,
            "image_after_final": 0,
        }[timing]
        observed = FixedDatetime.current + timedelta(seconds=offset)
        observation["verified_at"] = observed.isoformat()
        for image in observation["image_absence"]:
            image["collected_at"] = (
                observed + timedelta(seconds=int(timing == "image_after_final"))
            ).isoformat()
        from app.schemas.native_online_enrollment import VerifiedCustody

        fresh["digest"] = reconcile.digest(
            VerifiedCustody.model_validate(fresh).model_dump(
                mode="json", exclude={"digest"}
            )
        )
    with q.sessions() as db:
        review = reconcile.review(db, fresh)
        if timing in {"before_prepare", "after_final", "image_after_final"}:
            with pytest.raises(
                AccountingUnavailable, match="outside the verified preparation interval"
            ):
                reconcile.apply(
                    db,
                    fresh,
                    expected_target_id=q.target_id,
                    binding_retirements=[reference],
                    cleanup_authority_project_ids=selected,
                    review_digest=review["digest"],
                )
            return
        from app.models.kubevirt_storage import (
            NativeCollectionDomain,
            NativeProjectAttempt,
            NativeProjectRoot,
            NativeStorageSnapshot,
        )

        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        old_report = db.query(NativeStorageSnapshot).first()
        assert old_report is not None and old_report.accepted
        domain.latest_report_id, domain.latest_started_at = (
            old_report.id,
            old_report.started_at,
        )
        for root in online._validated(fresh).enrollment.pins.project_roots:
            cursor = db.get(NativeProjectAttempt, (domain.id, str(root.project_id)))
            if cursor is None:
                cursor = NativeProjectAttempt(
                    domain_id=domain.id,
                    project_id=str(root.project_id),
                    root_key=online.enrollment.root_key(root),
                    enrollment_id=domain.enrollment_id,
                )
                db.add(cursor)
            cursor.latest_report_id, cursor.latest_started_at = (
                old_report.id,
                old_report.started_at,
            )
        db.flush()
        with pytest.raises(AccountingUnavailable, match="selection changed"):
            reconcile.apply(
                db,
                fresh,
                expected_target_id=q.target_id,
                cleanup_authority_project_ids=selected,
                review_digest=review["digest"],
            )
        reconcile.apply(
            db,
            fresh,
            expected_target_id=q.target_id,
            binding_retirements=[reference],
            cleanup_authority_project_ids=selected,
            review_digest=review["digest"],
        )
        db.commit()
        assert domain.latest_report_id is None
        assert online.enrollment.utc(domain.latest_started_at) == FixedDatetime.current
        assert all(
            c.latest_report_id is None
            and online.enrollment.utc(c.latest_started_at) == FixedDatetime.current
            for c in db.query(NativeProjectAttempt).filter_by(domain_id=domain.id)
        )
        assert db.get(NativeStorageSnapshot, old_report.id).accepted
        from app.services.quota_native import (
            NativeObservationRefreshRequired,
            _measurement_report,
        )

        roots = db.query(NativeProjectRoot).filter_by(domain_id=domain.id).all()
        with pytest.raises(NativeObservationRefreshRequired):
            _measurement_report(
                db,
                domain,
                roots,
                online._validated(fresh).enrollment,
                FixedDatetime.current,
                project_id=selected[0],
            )
        assert {r.id: r.payload for r in db.query(NativeCustodyReceipt)} == originals
        _, receipt, _ = online.retained(db, db.get(Project, selected[0]))
        own = [b for b in receipt.bindings if str(b.root.project_id) == selected[0]]
        assert len(own) == 1 and own[0].pvc_uid == "added-pvc-uid"
        assert reconcile.retirement_references(db, q.pins.domain_id) == [
            reference.model_dump(mode="json")
        ]
        from app.services.native_binding_retirement_collection import protected_inputs

        protected = protected_inputs(db, receipt.enrollment)
        assert len(protected) == 1
        assert protected[0][0] == reference
        assert protected[0][1].prepared == absent.prepared
        if timing == "aged":
            original_inventory = q.backend.image_inventory

            def reappeared(key, **kwargs):
                result = original_inventory(key, **kwargs)
                return result.model_copy(
                    update={"value": {**result.value, "active": True}}
                )

            with monkeypatch.context() as changes:
                changes.setattr(q.backend, "image_inventory", reappeared)
                with pytest.raises(ValueError, match="private image remains"):
                    retirement.verify(protected[0][1].prepared, q.reader, q.backend)
            # Historical metadata is not a fresh deletion authorization.
            with pytest.raises(NativeObservationRefreshRequired):
                _measurement_report(
                    db,
                    domain,
                    roots,
                    receipt.enrollment,
                    FixedDatetime.current,
                    project_id=selected[0],
                )
        q.prepared = reconcile.prepare(
            db, q.project_ids, reason="Reviewed later helper replacement"
        )
        db.commit()
    helper = next(
        v for (kind, _), v in q.apis[selected[0]].values.items() if kind == "Deployment"
    )
    helper["metadata"]["uid"] += "-later"
    later = verified(q)
    with q.sessions() as db:
        review = reconcile.review(db, later)
        reconcile.apply(
            db,
            later,
            expected_target_id=q.target_id,
            cleanup_authority_project_ids=[],
            review_digest=review["digest"],
        )
        db.commit()
        assert reconcile.retirement_references(db, q.pins.domain_id) == [
            reference.model_dump(mode="json")
        ]


@pytest.mark.parametrize("watermark_offset", [-120, 120])
def test_retirement_invalidation_preserves_conflicts_history_and_other_domains(
    online_case, watermark_offset
):
    from uuid import uuid4

    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeProjectAttempt,
        NativeStorageSnapshot,
    )
    from app.services.native_custody_reconciliation import (
        _invalidate_retirement_observations,
    )
    from tests.test_native_managed_resume import FixedDatetime

    q = online_case
    with q.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        report = db.query(NativeStorageSnapshot).first()
        watermark = FixedDatetime.current + timedelta(seconds=watermark_offset)
        domain.latest_report_id, domain.latest_started_at = report.id, watermark
        domain.conflicted = True
        sibling = NativeCollectionDomain(
            id=str(uuid4()),
            enrollment_id=domain.enrollment_id,
            latest_report_id=report.id,
            latest_started_at=report.started_at,
            conflicted=True,
        )
        db.add(sibling)
        for owner in (domain, sibling):
            cursor = NativeProjectAttempt(
                domain_id=owner.id,
                project_id=str(uuid4()),
                root_key="a" * 64,
                enrollment_id=domain.enrollment_id,
                latest_report_id=report.id,
                latest_started_at=watermark,
                conflicted=True,
            )
            db.add(cursor)
        db.flush()
        _invalidate_retirement_observations(db, domain.id)
        assert domain.conflicted and domain.latest_report_id is None
        assert online.enrollment.utc(domain.latest_started_at) == max(
            watermark, FixedDatetime.current
        )
        assert sibling.conflicted and sibling.latest_report_id == report.id
        own = (
            db.query(NativeProjectAttempt)
            .filter_by(domain_id=domain.id, conflicted=True)
            .one()
        )
        other = db.query(NativeProjectAttempt).filter_by(domain_id=sibling.id).one()
        assert own.conflicted and own.latest_report_id is None
        assert online.enrollment.utc(own.latest_started_at) == max(
            watermark, FixedDatetime.current
        )
        assert other.conflicted and other.latest_report_id == report.id
        assert db.get(NativeStorageSnapshot, report.id) is report


def test_retirement_state_refreshes_cached_rows_under_lock(online_case):
    from app.models.kubevirt_storage import NativeCollectionDomain, NativeProjectAttempt
    from app.services import native_custody_reconciliation as reconcile
    from tests.test_native_managed_resume import FixedDatetime

    q = online_case
    with q.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        root = q.pins.project_roots[0]
        cursor = NativeProjectAttempt(
            domain_id=domain.id,
            project_id=str(root.project_id),
            root_key=online.enrollment.root_key(root),
            enrollment_id=domain.enrollment_id,
            conflicted=False,
        )
        db.add(cursor)
        db.flush()
        future = FixedDatetime.current + timedelta(seconds=120)
        # A database update must supersede rows already cached by review.
        for model in (NativeCollectionDomain, NativeProjectAttempt):
            db.query(model).update(
                {model.conflicted: True, model.latest_started_at: future},
                synchronize_session=False,
            )
        assert not domain.conflicted and not cursor.conflicted
        state = reconcile._retirement_observation_state(db, domain.id)
        assert all(
            conflicted and online.enrollment.utc(started) == future
            for started, conflicted in state.values()
        )
        reconcile._invalidate_retirement_observations(db, domain.id, state)
        assert domain.conflicted and cursor.conflicted
        assert online.enrollment.utc(domain.latest_started_at) == future
        assert online.enrollment.utc(cursor.latest_started_at) == future
