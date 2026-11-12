"""Native issuance protocol foundation; deliberately not wired to runtime writers.

Only a trusted issuer may initialize authorization. A CR is never an authority.
The store must provide durable compare-and-swap with a pinned object identity;
neither Python method visibility nor this library enforces issuer/worker RBAC.
Accepted calls remain ambiguous after a crash, timeout or diagnostic lease expiry.
No method dispatches or retries a workload API request. Callers must use the
immutable operation key from the final execution plan and the recorded target
UID/resourceVersion in the actual request's preconditions, never a fresh lookup.

This protocol cannot stop old credentials or autonomous controllers. Full writer
mediation and controller evidence are later milestones; settled is always false.
"""

from __future__ import annotations

import copy
import json
import re
import time
from dataclasses import asdict, dataclass
from hashlib import sha256
from uuid import UUID

MAX_INT64 = 2**63 - 1
MAX_INTENTS = 64
MAX_CAS_ATTEMPTS = 8


class FenceError(RuntimeError):
    """Fail closed without discarding durable evidence."""


class AuthorizationDenied(FenceError):
    pass


class DuplicateIntent(FenceError):
    """The action was accepted before; reconcile it, never redispatch it."""


class CASConflict(FenceError):
    """A definitive failed CAS; no new state was persisted by this attempt."""


def _integer(value, *, maximum=MAX_INT64, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise FenceError("Invalid bounded integer")
    return value


def _text(value, maximum=253):
    if not isinstance(value, str) or not 0 < len(value) <= maximum:
        raise FenceError("Invalid bounded identity")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value):
        raise FenceError("Invalid identity characters")
    return value


def _namespace(value):
    _text(value, 63)
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", value):
        raise FenceError("Invalid Kubernetes namespace")
    return value


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise FenceError("Invalid execution or request digest")


def _uuid(value):
    _text(value, 36)
    try:
        valid = str(UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise FenceError("Invalid canonical UUID")


def _fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        raise FenceError("Invalid persisted record fields")


@dataclass(frozen=True)
class NativeAuthorization:
    generation: str
    operation_id: str
    execution_digest: str
    desired_growth_state: str
    epoch: int

    def __post_init__(self):
        _uuid(self.generation)
        _uuid(self.operation_id)
        _digest(self.execution_digest)
        _integer(self.epoch)
        if self.desired_growth_state not in ("growing", "fenced"):
            raise FenceError("Invalid desired growth state")


@dataclass(frozen=True)
class ResourceKey:
    operation_key: str
    api_version: str
    kind: str
    namespace: str
    name: str
    action: str
    request_digest: str
    expected_uid: str | None
    expected_resource_version: str | None

    def __post_init__(self):
        for value in (
            self.operation_key,
            self.api_version,
            self.kind,
            self.name,
        ):
            _text(value)
        _namespace(self.namespace)
        _digest(self.request_digest)
        if self.action not in ("create", "patch", "replace", "delete"):
            raise FenceError("Invalid resource action")
        if self.action == "create":
            if (
                self.expected_uid is not None
                or self.expected_resource_version is not None
            ):
                raise FenceError("Creation must not claim an existing target")
        else:
            _text(self.expected_uid, 128)
            _text(self.expected_resource_version, 128)


@dataclass(frozen=True)
class FenceStatus:
    observed_epoch: int | None
    issuance_closed: bool
    unresolved_intents: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def settled(self) -> bool:
        # There is intentionally no bool argument or setter to manufacture proof.
        return False


def empty_record():
    """Initial contents for an externally provisioned, pinned coordination object."""
    return {"version": 1, "authorization": None, "fence_epoch": None, "intents": {}}


def _decode_dataclass(cls, value):
    _fields(value, cls.__dataclass_fields__)
    return cls(**value)


def _intent_id(authorization, resource):
    identity = [
        authorization.generation,
        authorization.operation_id,
        resource.operation_key,
    ]
    return sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def _validate_receipt(receipt, resource):
    if receipt is None:
        return
    _fields(receipt, ("uid", "resource_version"))
    _text(receipt["uid"], 128)
    _text(receipt["resource_version"], 128)
    if resource.expected_uid is not None and resource.expected_uid != receipt["uid"]:
        raise FenceError("Receipt does not match original target UID")


def _validate_intent(key, intent, current):
    _digest(key)
    _fields(
        intent,
        ("authorization", "resource", "accepted_at", "lease_expires_at", "receipt"),
    )
    auth = _decode_dataclass(NativeAuthorization, intent["authorization"])
    resource = _decode_dataclass(ResourceKey, intent["resource"])
    if auth.desired_growth_state != "growing" or _intent_id(auth, resource) != key:
        raise FenceError("Invalid accepted intent identity")
    if (auth.generation, auth.operation_id, auth.execution_digest) != (
        current.generation,
        current.operation_id,
        current.execution_digest,
    ):
        raise FenceError("Accepted intent belongs to another authorization")
    if auth.epoch > current.epoch:
        raise FenceError("Accepted intent epoch exceeds current authority")
    if current.desired_growth_state == "growing" and auth.epoch != current.epoch:
        raise FenceError("Open authority differs from accepted intent epoch")
    start = _integer(intent["accepted_at"])
    end = _integer(intent["lease_expires_at"])
    _integer(end - start, minimum=1, maximum=300)
    _validate_receipt(intent["receipt"], resource)


def validate_record(record):
    """Validate a complete copy before any transition; never repair lost evidence."""
    _fields(record, ("version", "authorization", "fence_epoch", "intents"))
    if type(record["version"]) is not int or record["version"] != 1:
        raise FenceError("Unsupported issuance record version")
    intents = record["intents"]
    if not isinstance(intents, dict) or len(intents) > MAX_INTENTS:
        raise FenceError("Invalid bounded intent inventory")
    auth = record["authorization"]
    epoch = record["fence_epoch"]
    if auth is None:
        if intents or epoch is not None:
            raise FenceError("Evidence without an authority")
        return
    auth = _decode_dataclass(NativeAuthorization, auth)
    if epoch is not None:
        _integer(epoch)
        if auth.desired_growth_state != "fenced" or auth.epoch != epoch:
            raise FenceError("Fence and authority disagree")
    elif auth.desired_growth_state != "growing":
        raise FenceError("Missing fence epoch")
    for key, intent in intents.items():
        _validate_intent(key, intent, auth)


def _reserve_completion_capacity(store, record):
    """Reserve a valid maximum-size completion, never persist synthetic receipts.

    Receipt identities allow only ASCII characters that need no JSON escaping.
    Every unresolved receipt can therefore use the full 128-byte UID/RV bound;
    existing-object UIDs are fixed to their original value. Already recorded
    receipts cannot change. Both mutable epoch fields may reach signed-int64 max.
    The store checks this projection using the exact serializer used by CAS.
    """
    terminal = copy.deepcopy(record)
    terminal["authorization"].update(desired_growth_state="fenced", epoch=MAX_INT64)
    terminal["fence_epoch"] = MAX_INT64
    for intent in terminal["intents"].values():
        if intent["receipt"] is None:
            intent["receipt"] = {
                "uid": intent["resource"]["expected_uid"] or "u" * 128,
                "resource_version": "v" * 128,
            }
    store.ensure_capacity(terminal)


def _transition(store, update, *, reserve_capacity=False):
    """Retry only definitive CAS conflicts; ambiguous store failures propagate."""
    for _ in range(MAX_CAS_ATTEMPTS):
        snapshot = store.read()
        record = copy.deepcopy(snapshot.record)
        validate_record(record)
        result = update(record)
        validate_record(record)
        if reserve_capacity:
            _reserve_completion_capacity(store, record)
        if record == snapshot.record:
            return result
        try:
            store.compare_and_swap(snapshot, record)
            return result
        except CASConflict:
            continue
    raise FenceError("Issuance store contention; no dispatch permitted")


class TrustedIssuer:
    """Trusted backend-only port; NEVER expose this port to workload workers.

    In this foundation the separation is an interface contract, not a deployed
    security boundary. Future mediation must enforce it server-side.
    """

    def __init__(self, store):
        self._store = store

    def issue(self, authorization: NativeAuthorization):
        if not isinstance(authorization, NativeAuthorization):
            raise AuthorizationDenied("Missing typed authorization")
        if authorization.desired_growth_state != "growing":
            raise AuthorizationDenied("New authority must allow growth")

        def update(record):
            previous = record["authorization"]
            if previous is not None and previous != asdict(authorization):
                raise AuthorizationDenied(
                    "Existing authority cannot be replaced or reopened"
                )
            record["authorization"] = asdict(authorization)

        _transition(self._store, update, reserve_capacity=True)


def _require_authorization(record, authorization):
    if not isinstance(authorization, NativeAuthorization):
        raise AuthorizationDenied("Missing typed authorization")
    if record["authorization"] != asdict(authorization):
        raise AuthorizationDenied("Authorization differs from durable authority")
    if (
        authorization.desired_growth_state != "growing"
        or record["fence_epoch"] is not None
    ):
        raise AuthorizationDenied("Issuance already fenced")


def _status(record):
    unresolved = tuple(
        sorted(
            key for key, value in record["intents"].items() if value["receipt"] is None
        )
    )
    closed = record["fence_epoch"] is not None
    blockers = ["controller_quiescence_unproven", "writer_mediation_unproven"]
    if not closed:
        blockers.append("issuance_not_closed")
    if unresolved:
        blockers.append("accepted_intents_unresolved")
    return FenceStatus(record["fence_epoch"], closed, unresolved, tuple(blockers))


class NativeCoordinator:
    """Worker port for acceptance, original-response receipts and fence requests."""

    def __init__(self, store, *, clock=None, lease_seconds=30, max_intents=MAX_INTENTS):
        self._store = store
        self._clock = clock or (lambda: int(time.time()))
        self._lease_seconds = _integer(lease_seconds, minimum=1, maximum=300)
        self._max_intents = _integer(max_intents, minimum=1, maximum=MAX_INTENTS)

    def begin_intent(
        self, resource_key: ResourceKey, authorization: NativeAuthorization
    ) -> str:
        if not isinstance(resource_key, ResourceKey):
            raise FenceError("Missing typed resource identity")

        def update(record):
            _require_authorization(record, authorization)
            key = _intent_id(authorization, resource_key)
            existing = record["intents"].get(key)
            if existing is not None:
                if existing["resource"] != asdict(resource_key):
                    raise FenceError(
                        "Immutable operation key reused with changed request"
                    )
                raise DuplicateIntent(
                    "Intent already accepted; reconciliation required"
                )
            if len(record["intents"]) >= self._max_intents:
                raise FenceError("Intent capacity reached; evidence retained")
            started = _integer(self._clock())
            record["intents"][key] = {
                "authorization": asdict(authorization),
                "resource": asdict(resource_key),
                "accepted_at": started,
                "lease_expires_at": _integer(started + self._lease_seconds),
                "receipt": None,
            }
            return key

        return _transition(self._store, update, reserve_capacity=True)

    def finish_intent(self, intent_id: str, observed_uid: str, resource_version: str):
        """Persist an original request's response, never infer success from a name.

        A caller with an ambiguous response cannot use this as an absence/retry
        escape hatch. Independent reconciliation evidence is a later contract.
        """
        _digest(intent_id)
        receipt = {"uid": observed_uid, "resource_version": resource_version}

        def update(record):
            intent = record["intents"].get(intent_id)
            if intent is None:
                raise FenceError("Unknown accepted intent")
            resource = _decode_dataclass(ResourceKey, intent["resource"])
            _validate_receipt(receipt, resource)
            if intent["receipt"] is not None and intent["receipt"] != receipt:
                raise FenceError("Original receipt cannot be replaced")
            intent["receipt"] = receipt

        _transition(self._store, update)

    def acknowledge_fence(self, epoch: int) -> FenceStatus:
        _integer(epoch)

        def update(record):
            if record["authorization"] is None:
                raise AuthorizationDenied("Cannot acknowledge missing authority")
            auth = record["authorization"]
            if epoch < auth["epoch"]:
                raise FenceError("Fence epoch must never decrease")
            auth.update(epoch=epoch, desired_growth_state="fenced")
            record["fence_epoch"] = epoch
            return _status(record)

        return _transition(self._store, update)

    def status(self) -> FenceStatus:
        record = self._store.read().record
        validate_record(record)
        return _status(record)
