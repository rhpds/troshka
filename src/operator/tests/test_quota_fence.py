"""Executable CAS faults; the fake API models persisted Kubernetes object state."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace

import pytest
from helpers.quota_fence import (
    MAX_INT64,
    AuthorizationDenied,
    DuplicateIntent,
    FenceError,
    NativeAuthorization,
    NativeCoordinator,
    ResourceKey,
    TrustedIssuer,
    empty_record,
)
from helpers.quota_fence_store import KubernetesCASStore, StoreIdentityError
from kubernetes.client.exceptions import ApiException


class FakeAPI:
    def __init__(self):
        self.uid = "coordination-object-uid"
        self.version = 1
        self.record = empty_record()
        self.lock = Lock()  # Only models the API server's atomic JSON Patch.
        self.before_patch = None
        self.timeout_after_write = False
        self.patches = []

    def read_namespaced_config_map(self, name, namespace, **kwargs):
        with self.lock:
            return SimpleNamespace(
                metadata=SimpleNamespace(
                    uid=self.uid, resource_version=str(self.version)
                ),
                data={"record": json.dumps(self.record)},
            )

    def patch_namespaced_config_map(self, name, namespace, body, **kwargs):
        if self.before_patch:
            hook, self.before_patch = self.before_patch, None
            hook()
        with self.lock:
            self.patches.append(copy.deepcopy(body))
            expected = [self.uid, str(self.version)]
            if [body[0]["value"], body[1]["value"]] != expected:
                raise ApiException(status=409)
            assert body[0]["path"] == "/metadata/uid"
            assert body[1]["path"] == "/metadata/resourceVersion"
            assert kwargs["_content_type"] == "application/json-patch+json"
            self.record = json.loads(body[2]["value"])
            self.version += 1
            if self.timeout_after_write:
                self.timeout_after_write = False
                raise TimeoutError("response lost after persistence")


def store(api):
    return KubernetesCASStore(
        api, namespace="coordination", name="generation", uid="coordination-object-uid"
    )


def authorization(**changes):
    values = {
        "generation": "12345678-1234-4234-8234-123456789abc",
        "operation_id": "87654321-4321-4321-8321-cba987654321",
        "execution_digest": "a" * 64,
        "desired_growth_state": "growing",
        "epoch": 1,
    }
    return NativeAuthorization(**{**values, **changes})


def resource(**changes):
    values = {
        "operation_key": "vm-create",
        "api_version": "kubevirt.io/v1",
        "kind": "VirtualMachine",
        "namespace": "project-namespace",
        "name": "vm-one",
        "action": "create",
        "request_digest": "b" * 64,
        "expected_uid": None,
        "expected_resource_version": None,
    }
    return ResourceKey(**{**values, **changes})


@pytest.fixture
def initialized():
    api = FakeAPI()
    TrustedIssuer(store(api)).issue(authorization())
    return api, NativeCoordinator(store(api), clock=lambda: 100)


def test_paused_accepted_writer_fence_and_restart(initialized):
    api, coordinator = initialized
    accepted, release = Event(), Event()
    dispatched = []

    def writer():
        intent = coordinator.begin_intent(resource(), authorization())
        accepted.set()
        assert release.wait(5)
        dispatched.append(intent)
        coordinator.finish_intent(intent, "created-vm-uid", "75")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        try:
            assert accepted.wait(5)
            restarted = NativeCoordinator(store(api), clock=lambda: 1000)
            fence = restarted.acknowledge_fence(2)
            assert fence.issuance_closed and not fence.settled
            assert len(fence.unresolved_intents) == 1
            with pytest.raises(AuthorizationDenied):
                restarted.begin_intent(
                    resource(operation_key="second"), authorization()
                )
            with pytest.raises(FenceError):
                restarted.begin_intent(resource(), authorization())
            assert restarted.status().unresolved_intents == fence.unresolved_intents
        finally:
            release.set()
        future.result(timeout=5)
    status = NativeCoordinator(store(api)).status()
    assert len(dispatched) == 1
    assert not status.unresolved_intents
    assert status.issuance_closed and not status.settled
    assert "controller_quiescence_unproven" in status.blockers


def test_two_actors_conflicting_cas_keep_both_intents(initialized):
    api, first = initialized
    second = NativeCoordinator(store(api))
    other_ids = []
    api.before_patch = lambda: other_ids.append(
        second.begin_intent(
            resource(operation_key="other", name="vm-two"), authorization()
        )
    )
    first_id = first.begin_intent(resource(), authorization())
    assert set(first.status().unresolved_intents) == {first_id, other_ids[0]}
    assert len(api.patches) == 4  # Issue + second write + first conflict + retry.


def test_fence_wins_acceptance_cas_race(initialized):
    api, coordinator = initialized
    api.before_patch = lambda: NativeCoordinator(store(api)).acknowledge_fence(2)
    with pytest.raises(AuthorizationDenied):
        coordinator.begin_intent(resource(), authorization())
    assert coordinator.status().unresolved_intents == ()


def test_timeout_after_acceptance_never_replays_even_after_expiry(initialized):
    api, coordinator = initialized
    api.timeout_after_write = True
    with pytest.raises(TimeoutError):
        coordinator.begin_intent(resource(), authorization())
    accepted = copy.deepcopy(api.record)
    restarted = NativeCoordinator(store(api), clock=lambda: 100000)
    with pytest.raises(DuplicateIntent):
        restarted.begin_intent(resource(), authorization())
    assert api.record == accepted
    assert len(restarted.status().unresolved_intents) == 1


def test_duplicate_after_receipt_does_not_dispatch(initialized):
    _, coordinator = initialized
    intent = coordinator.begin_intent(resource(), authorization())
    coordinator.finish_intent(intent, "vm-uid", "77")
    coordinator.finish_intent(intent, "vm-uid", "77")
    with pytest.raises(DuplicateIntent):
        coordinator.begin_intent(resource(), authorization())
    with pytest.raises(FenceError):
        coordinator.finish_intent(intent, "replacement-uid", "78")


def test_existing_target_receipt_binds_original_identity(initialized):
    api, coordinator = initialized
    key = resource(
        action="patch", expected_uid="original-uid", expected_resource_version="12"
    )
    intent = coordinator.begin_intent(key, authorization())
    stored = api.record["intents"][intent]["resource"]
    assert stored["expected_uid"] == "original-uid"
    assert stored["expected_resource_version"] == "12"
    with pytest.raises(FenceError):
        coordinator.finish_intent(intent, "replacement-uid", "13")
    assert coordinator.status().unresolved_intents == (intent,)
    coordinator.finish_intent(intent, "original-uid", "13")


def test_reusing_operation_key_with_changed_request_is_rejected(initialized):
    api, coordinator = initialized
    coordinator.begin_intent(resource(), authorization())
    original = copy.deepcopy(api.record)
    with pytest.raises(FenceError):
        coordinator.begin_intent(resource(request_digest="c" * 64), authorization())
    assert api.record == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("epoch", 0),
        ("execution_digest", "d" * 64),
        ("operation_id", "00000000-1234-4234-8234-123456789abc"),
    ],
)
def test_stale_or_forged_authorization_denied(initialized, field, value):
    api, coordinator = initialized
    with pytest.raises(AuthorizationDenied):
        coordinator.begin_intent(resource(), authorization(**{field: value}))
    assert not api.record["intents"]


def test_missing_authority_and_issuer_cannot_replace_generation():
    api = FakeAPI()
    coordinator = NativeCoordinator(store(api))
    with pytest.raises(AuthorizationDenied):
        coordinator.begin_intent(resource(), authorization())
    issuer = TrustedIssuer(store(api))
    issuer.issue(authorization())
    issuer.issue(authorization())
    with pytest.raises(AuthorizationDenied):
        issuer.issue(authorization(generation="00000000-1234-4234-8234-123456789abc"))
    coordinator.acknowledge_fence(2)
    with pytest.raises(AuthorizationDenied):
        issuer.issue(authorization())


def test_monotonic_fence_does_not_reopen(initialized):
    _, coordinator = initialized
    coordinator.acknowledge_fence(3)
    coordinator.acknowledge_fence(3)
    with pytest.raises(FenceError):
        coordinator.acknowledge_fence(2)
    assert coordinator.status().observed_epoch == 3
    assert not coordinator.status().settled


@pytest.mark.parametrize("epoch", [-1, True, 1.5, "2", 2**63])
def test_invalid_epoch_values(initialized, epoch):
    api, coordinator = initialized
    before = copy.deepcopy(api.record)
    with pytest.raises(FenceError):
        coordinator.acknowledge_fence(epoch)
    with pytest.raises(FenceError):
        authorization(epoch=epoch)
    assert api.record == before


def test_replaced_store_and_deleted_store_fail_closed(initialized):
    api, coordinator = initialized
    api.uid = "replacement"
    with pytest.raises(StoreIdentityError):
        coordinator.begin_intent(resource(), authorization())
    api.read_namespaced_config_map = lambda **kwargs: (_ for _ in ()).throw(
        ApiException(status=404)
    )
    with pytest.raises(StoreIdentityError):
        coordinator.status()


def test_store_replacement_between_read_and_write_fails_closed(initialized):
    api, coordinator = initialized
    api.before_patch = lambda: setattr(api, "uid", "replacement")
    with pytest.raises(StoreIdentityError):
        coordinator.begin_intent(resource(), authorization())
    assert not api.record["intents"]


def test_intent_count_bound_keeps_prior_evidence(initialized):
    api, _ = initialized
    coordinator = NativeCoordinator(store(api), max_intents=1)
    coordinator.begin_intent(resource(), authorization())
    before = copy.deepcopy(api.record)
    with pytest.raises(FenceError):
        coordinator.begin_intent(resource(operation_key="two"), authorization())
    assert api.record == before
    assert coordinator.acknowledge_fence(2).issuance_closed


@pytest.mark.parametrize(
    "mutation",
    [
        lambda record: record.update(epoch=0),
        lambda record: record["authorization"].update(epoch=True),
        lambda record: record.update(intents=[]),
        lambda record: record.update(fence_epoch=2**63),
    ],
)
def test_malformed_persisted_state_is_not_overwritten(initialized, mutation):
    api, coordinator = initialized
    mutation(api.record)
    before = copy.deepcopy(api.record)
    with pytest.raises(FenceError):
        coordinator.acknowledge_fence(2)
    assert api.record == before


def test_store_size_limit_rejects_without_write(initialized):
    api, _ = initialized
    bounded = KubernetesCASStore(
        api, namespace="coordination", name="generation", uid=api.uid, max_bytes=512
    )
    coordinator = NativeCoordinator(bounded)
    before = copy.deepcopy(api.record)
    with pytest.raises(FenceError):
        coordinator.begin_intent(resource(), authorization())
    assert api.record == before


@pytest.mark.parametrize("stage", ["fence", "receipt"])
def test_timeout_after_terminal_state_write_survives_restart(initialized, stage):
    api, coordinator = initialized
    intent = coordinator.begin_intent(resource(), authorization())
    api.timeout_after_write = True
    with pytest.raises(TimeoutError):
        if stage == "fence":
            coordinator.acknowledge_fence(2)
        else:
            coordinator.finish_intent(intent, "vm-uid", "77")
    restarted = NativeCoordinator(store(api))
    before = len(api.patches)
    if stage == "fence":
        assert restarted.acknowledge_fence(2).issuance_closed
        assert restarted.status().unresolved_intents == (intent,)
    else:
        restarted.finish_intent(intent, "vm-uid", "77")
        assert restarted.status().unresolved_intents == ()
    assert len(api.patches) == before
    assert not restarted.status().settled


def test_external_request_timeout_does_not_clear_intent(initialized):
    api, coordinator = initialized
    intent = coordinator.begin_intent(resource(), authorization())
    external_calls = []

    def ambiguous_create():
        external_calls.append("persisted-vm-uid")
        raise TimeoutError("workload API response lost")

    with pytest.raises(TimeoutError):
        ambiguous_create()
    restarted = NativeCoordinator(store(api), clock=lambda: 100000)
    with pytest.raises(DuplicateIntent):
        restarted.begin_intent(resource(), authorization())
    assert external_calls == ["persisted-vm-uid"]
    assert restarted.acknowledge_fence(2).unresolved_intents == (intent,)


def test_conflict_budget_is_bounded_without_acceptance(initialized):
    api, coordinator = initialized
    before = copy.deepcopy(api.record)
    conflicts = []

    def reject(**kwargs):
        conflicts.append(1)
        raise ApiException(status=409)

    api.patch_namespaced_config_map = reject
    with pytest.raises(FenceError, match="contention"):
        coordinator.begin_intent(resource(), authorization())
    assert len(conflicts) == 8
    assert api.record == before


@pytest.mark.parametrize("http_status", [403, 422, 500, 504])
def test_other_api_errors_never_retry_and_do_not_expose_raw_body(
    initialized, http_status
):
    api, coordinator = initialized
    attempts = []

    def reject(**kwargs):
        attempts.append(1)
        raise ApiException(status=http_status, reason="sensitive-workload-payload")

    api.patch_namespaced_config_map = reject
    with pytest.raises(FenceError) as error:
        coordinator.begin_intent(resource(), authorization())
    assert len(attempts) == 1
    assert "sensitive-workload-payload" not in str(error.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", "not-a-uuid"),
        ("operation_id", "x" * 10000),
        ("execution_digest", "A" * 64),
        ("desired_growth_state", "running"),
    ],
)
def test_invalid_authority_identity(field, value):
    with pytest.raises(FenceError):
        authorization(**{field: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_uid": "wrong-existing-uid"},
        {"action": "patch"},
        {"action": "patch", "expected_uid": "old-uid", "expected_resource_version": 12},
        {"operation_key": "x" * 254},
        {"name": ""},
        {"request_digest": "x" * 64},
    ],
)
def test_invalid_resource_identity(changes):
    with pytest.raises(FenceError):
        resource(**changes)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lease_seconds": 0},
        {"lease_seconds": 301},
        {"max_intents": 65},
        {"max_intents": True},
    ],
)
def test_invalid_coordinator_bounds(initialized, kwargs):
    api, _ = initialized
    with pytest.raises(FenceError):
        NativeCoordinator(store(api), **kwargs)


def test_clock_overflow_does_not_erase_evidence(initialized):
    api, _ = initialized
    before = copy.deepcopy(api.record)
    coordinator = NativeCoordinator(store(api), clock=lambda: 2**63 - 1)
    with pytest.raises(FenceError):
        coordinator.begin_intent(resource(), authorization())
    assert api.record == before


@pytest.mark.parametrize(
    "encoded",
    ['{"version":1,"version":1}', "[]", "null", "{", '"' + ("x" * 131072) + '"'],
)
def test_invalid_or_oversized_persisted_json(initialized, encoded):
    api, coordinator = initialized
    original_read = api.read_namespaced_config_map

    def read(**kwargs):
        obj = original_read(**kwargs)
        obj.data["record"] = encoded
        return obj

    api.read_namespaced_config_map = read
    with pytest.raises(FenceError):
        coordinator.status()


def test_persisted_unfenced_authority_cannot_disagree_with_accepted_epoch(initialized):
    api, coordinator = initialized
    coordinator.begin_intent(resource(), authorization())
    api.record["authorization"]["epoch"] = 2
    with pytest.raises(FenceError):
        coordinator.status()


@pytest.mark.parametrize(
    "namespace,name",
    [
        ("wrong/path", "generation"),
        ("n" * 64, "generation"),
        ("coordination", "../generation"),
    ],
)
def test_store_requires_valid_pinned_kubernetes_names(namespace, name):
    with pytest.raises(FenceError):
        KubernetesCASStore(
            FakeAPI(), namespace=namespace, name=name, uid="original-uid"
        )


class ExactBytesAPI(FakeAPI):
    """Capacity probes read the exact string written by the real CAS adapter."""

    def __init__(self):
        super().__init__()
        self.wire_record = json.dumps(self.record, separators=(",", ":"))

    def read_namespaced_config_map(self, **kwargs):
        obj = super().read_namespaced_config_map(**kwargs)
        obj.data["record"] = self.wire_record
        return obj

    def patch_namespaced_config_map(self, **kwargs):
        super().patch_namespaced_config_map(**kwargs)
        self.wire_record = kwargs["body"][2]["value"]


def bounded_coordinator(max_bytes):
    api = ExactBytesAPI()
    bounded = KubernetesCASStore(
        api,
        namespace="coordination",
        name="generation",
        uid=api.uid,
        max_bytes=max_bytes,
    )
    TrustedIssuer(bounded).issue(authorization())
    return api, NativeCoordinator(bounded, clock=lambda: 100)


def test_small_budget_cannot_grant_unfinishable_intent():
    api, coordinator = bounded_coordinator(974)
    before = api.wire_record
    with pytest.raises(FenceError, match="storage bound"):
        coordinator.begin_intent(resource(), authorization())
    assert api.wire_record == before
    assert coordinator.acknowledge_fence(MAX_INT64).issuance_closed


def test_conflicting_acceptance_rechecks_completion_capacity():
    api, first = bounded_coordinator(1400)
    second_store = KubernetesCASStore(
        api, namespace="coordination", name="generation", uid=api.uid, max_bytes=1400
    )
    second = NativeCoordinator(second_store, clock=lambda: 100)
    accepted = []
    api.before_patch = lambda: accepted.append(
        second.begin_intent(
            resource(operation_key="other", name="vm-two"), authorization()
        )
    )
    with pytest.raises(FenceError, match="storage bound"):
        first.begin_intent(resource(), authorization())
    assert first.status().unresolved_intents == tuple(accepted)
    first.acknowledge_fence(MAX_INT64)
    first.finish_intent(accepted[0], "u" * 128, "v" * 128)
    assert len(api.record["intents"]) == 1
    assert not first.status().unresolved_intents


def test_exact_byte_boundary_reserves_all_receipt_and_epoch_growth():
    # All allowed punctuation is ASCII and requires no escaping in JSON strings.
    uid = ("u._:/-" * 22)[:128]
    version = ("v._:/-" * 22)[:128]
    probe, probe_coordinator = bounded_coordinator(4096)
    intent = probe_coordinator.begin_intent(resource(), authorization())
    probe_coordinator.finish_intent(intent, uid, version)
    probe_coordinator.acknowledge_fence(MAX_INT64)
    terminal_bytes = len(probe.wire_record.encode("utf-8"))

    short_api, short_coordinator = bounded_coordinator(terminal_bytes - 1)
    before = short_api.wire_record
    with pytest.raises(FenceError, match="storage bound"):
        short_coordinator.begin_intent(resource(), authorization())
    assert short_api.wire_record == before

    api, coordinator = bounded_coordinator(terminal_bytes)
    intent = coordinator.begin_intent(resource(), authorization())
    coordinator.acknowledge_fence(1000)
    coordinator.finish_intent(intent, uid, version)
    coordinator.acknowledge_fence(MAX_INT64)
    assert len(api.wire_record.encode("utf-8")) == terminal_bytes
    assert coordinator.status().unresolved_intents == ()
    assert intent in api.record["intents"]


def test_default_budget_every_granted_large_key_can_finish_and_fence():
    api, coordinator = bounded_coordinator(128 * 1024)
    intents = []
    # Long original identities and legal namespace reach the real byte bound
    # before the count bound. Every grant must leave room for all final receipts.
    for index in range(64):
        key = resource(
            operation_key=f"{index:03d}" + "k" * 250,
            api_version="a" * 253,
            kind="k" * 253,
            namespace="n" * 63,
            name="v" * 253,
            action="patch",
            expected_uid="u" * 128,
            expected_resource_version="v" * 128,
        )
        before = api.wire_record
        try:
            intents.append(coordinator.begin_intent(key, authorization()))
        except FenceError:
            assert api.wire_record == before
            break
    assert len(intents) > 1
    assert set(api.record["intents"]) == set(intents)
    for intent in intents:
        coordinator.finish_intent(intent, "u" * 128, "v" * 128)
    coordinator.acknowledge_fence(MAX_INT64)
    assert len(intents) < 64
    assert set(api.record["intents"]) == set(intents)
    assert not coordinator.status().unresolved_intents
    assert len(api.wire_record.encode("utf-8")) <= 128 * 1024


def test_authority_issue_also_reserves_future_fence_bytes():
    probe, _ = bounded_coordinator(4096)
    open_bytes = len(probe.wire_record.encode("utf-8"))
    api = ExactBytesAPI()
    bounded = KubernetesCASStore(
        api,
        namespace="coordination",
        name="generation",
        uid=api.uid,
        max_bytes=open_bytes,
    )
    with pytest.raises(FenceError, match="storage bound"):
        TrustedIssuer(bounded).issue(authorization())
    assert api.record["authorization"] is None


@pytest.mark.parametrize(
    "namespace",
    ["wrong/path", "UPPERCASE", "n" * 64, "ns.example", "trailing-", "-leading"],
)
def test_workload_namespace_is_a_dns_label(namespace):
    with pytest.raises(FenceError):
        resource(namespace=namespace)


@pytest.mark.parametrize("namespace", ["wrong/path", "UPPERCASE", "n" * 64])
def test_invalid_persisted_workload_namespace_is_not_repaired(initialized, namespace):
    api, coordinator = initialized
    intent = coordinator.begin_intent(resource(), authorization())
    api.record["intents"][intent]["resource"]["namespace"] = namespace
    before = copy.deepcopy(api.record)
    with pytest.raises(FenceError):
        coordinator.acknowledge_fence(2)
    assert api.record == before


@pytest.mark.parametrize("character", ['"', "\\", "\n", "é"])
def test_receipt_alphabet_excludes_json_escaping_and_non_ascii(initialized, character):
    api, coordinator = initialized
    intent = coordinator.begin_intent(resource(), authorization())
    before = copy.deepcopy(api.record)
    for uid, version in [("u" * 127 + character, "v"), ("u", "v" * 127 + character)]:
        with pytest.raises(FenceError):
            coordinator.finish_intent(intent, uid, version)
    assert api.record == before
