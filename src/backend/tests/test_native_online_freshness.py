"""Retirement must remain bracketed by fresh CSI journal verification."""

from datetime import timedelta

import pytest

from app.services import native_online_enrollment as online
from tests import test_native_online_enrollment as fixtures
from tests.test_native_managed_resume import FixedDatetime

online_case = fixtures.online_case
cleanup_case = fixtures.cleanup_case
deploy_case = fixtures.deploy_case
isolated_quota_database = fixtures.isolated_quota_database
native_ledger = fixtures.native_ledger
resume_case = fixtures.resume_case


def test_slow_final_bindings_are_not_hidden_from_retirement_clock(
    online_case, monkeypatch
):
    q = online_case
    q.prepared["binding_retirements"] = [{"selected": True}]
    starts = []
    original = online._bindings

    def slow_binding(*args):
        result = original(*args)
        FixedDatetime.current += timedelta(seconds=90)
        return result

    def retirement(*args):
        starts.append(FixedDatetime.current)
        FixedDatetime.current += timedelta(seconds=44)
        return []

    monkeypatch.setattr(online, "_bindings", slow_binding)
    monkeypatch.setattr(online, "_verify_binding_retirements", retirement)
    fixtures.verified(q)
    assert (FixedDatetime.current - starts[0]).total_seconds() == 44 + 90 * len(
        q.online_pins.project_roots
    )


def test_csi_journal_remapping_during_retirement_rejected(online_case, monkeypatch):
    q = online_case
    q.prepared["binding_retirements"] = [{"selected": True}]
    original = q.reader.resolve_csi

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        return result.model_copy(
            update={"value": {**result.value, "image_id": "different-image"}}
        )

    def retirement(*args):
        monkeypatch.setattr(q.reader, "resolve_csi", changed)
        return []

    monkeypatch.setattr(online, "_verify_binding_retirements", retirement)
    with pytest.raises(online.AccountingUnavailable, match="storage binding changed"):
        fixtures.verified(q)


@pytest.mark.parametrize(
    "change",
    [
        "root",
        "topology",
        "namespace",
        "pvc",
        "pv_uid",
        "pv_csi",
        "pv_provisioner",
        "pv_claim",
    ],
)
def test_mutation_during_retirement_rejected(online_case, monkeypatch, change):
    q = online_case
    q.prepared["binding_retirements"] = [{"selected": True}]
    api = next(iter(q.apis.values()))

    def retirement(*args):
        root = next(
            v for (kind, _), v in api.values.items() if kind == "TroshkaProject"
        )
        if change == "root":
            root["metadata"]["uid"] = "changed"
        elif change == "topology":
            root["spec"]["topology"]["changed"] = True
        elif change == "namespace":
            api.values["Namespace", root["metadata"]["namespace"]]["metadata"][
                "uid"
            ] = "changed"
        elif change == "pvc":
            pvc = next(
                v
                for (kind, _), v in api.values.items()
                if kind == "PersistentVolumeClaim"
            )
            pvc["spec"]["accessModes"] = ["changed"]
        else:
            pv = api.values["PersistentVolume", "retained-pv"]
            if change == "pv_uid":
                pv["metadata"]["uid"] = "changed"
            elif change == "pv_csi":
                pv["spec"]["csi"]["volumeHandle"] = "changed"
            elif change == "pv_provisioner":
                pv["metadata"].setdefault("annotations", {})[
                    "pv.kubernetes.io/provisioned-by"
                ] = "changed"
            else:
                pv["spec"]["claimRef"]["uid"] = "changed"
        return []

    monkeypatch.setattr(online, "_verify_binding_retirements", retirement)
    with pytest.raises((ValueError, online.AccountingUnavailable)):
        fixtures.verified(q)


def test_descriptor_brackets_expensive_binding_reads(online_case, monkeypatch):
    q = online_case
    q.prepared["binding_retirements"] = [{"selected": True}]
    original = online._bindings
    calls = 0

    def mutate_binding(*args):
        nonlocal calls
        result = original(*args)
        calls += 1
        if calls == 2 * len(q.online_pins.project_roots) + 1:
            api = next(iter(q.apis.values()))
            api.values["PersistentVolume", "retained-pv"]["spec"][
                "persistentVolumeReclaimPolicy"
            ] = "changed"
        return result

    monkeypatch.setattr(online, "_bindings", mutate_binding)
    monkeypatch.setattr(online, "_verify_binding_retirements", lambda *args: [])
    with pytest.raises(
        online.AccountingUnavailable, match="descriptors changed during CSI"
    ):
        fixtures.verified(q)


def test_without_retirements_keeps_existing_binding_path(online_case, monkeypatch):
    def forbidden(*args):
        raise AssertionError("No retirement descriptor reads expected")

    monkeypatch.setattr(online, "_storage_descriptors", forbidden)
    fixtures.verified(online_case)
