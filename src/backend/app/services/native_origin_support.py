"""Bounded support writes and exact read-only recovery from protected launch bodies."""

import base64
import hashlib
from typing import TYPE_CHECKING

from app.models.native_origin_namespace import (
    NativeOriginSupportHandoff,
    NativeOriginSupportReceipt,
)
from app.services import native_deployment_origin as origin
from app.services import native_origin_writers as writers
from app.services.kubevirt_storage_enrollment import digest

INVENTORY_ROLE = "troshka-origin-inventory"
MAX_ITEMS = 6
ATTEMPT = "troshka.redhat.com/support-origin"
ITEM = "troshka.redhat.com/support-item"


def secret_data(config):
    value = {
        "accessKeyId": config.get("access_key_id", ""),
        "secretKey": config.get("secret_access_key", ""),
        "AWS_ACCESS_KEY_ID": config.get("access_key_id", ""),
        "AWS_SECRET_ACCESS_KEY": config.get("secret_access_key", ""),
        "AWS_DEFAULT_REGION": config.get("region", "us-east-1"),
    }
    if config.get("endpoint_url"):
        value["AWS_ENDPOINT_URL"] = config["endpoint_url"]
    return value


def render(namespace, mode, s3_config, cluster_s3, central_s3, origin_id=None):
    from app.services.providers.kubevirt import _obc_s3_dict, _rolebinding_body

    grants = [
        ("troshka-provider-namespaced", "troshka", "troshka"),
        ("troshka-operator-namespaced", "troshka-operator", "troshka-operator"),
    ]
    if mode == "managed":
        grants.append((INVENTORY_ROLE, "troshka", "troshka"))
    bodies = [
        _rolebinding_body(
            binding_name=name,
            namespace=namespace,
            cluster_role=name,
            sa_name=sa,
            sa_namespace=sa_ns,
        )
        for name, sa, sa_ns in grants
    ]
    secrets = [("s3-credentials", s3_config)]
    if cluster_s3:
        secrets.append(("s3-obc-credentials", _obc_s3_dict(cluster_s3)))
    if central_s3:
        secrets.append(("s3-central-credentials", central_s3))
    bodies += [
        dict(
            apiVersion="v1",
            kind="Secret",
            metadata=dict(name=name, namespace=namespace),
            type="Opaque",
            stringData=secret_data(config),
        )
        for name, config in secrets
    ]
    if origin_id:
        for body in bodies:
            if mode == "managed" or body["kind"] != "RoleBinding":
                body["metadata"]["annotations"] = {
                    ATTEMPT: str(origin_id),
                    ITEM: digest(manifest([body])[0]),
                }
    return bodies


def _content(value):
    if value["kind"] == "RoleBinding":
        subjects = [
            {k: v for k, v in s.items() if not (k == "apiGroup" and v == "")}
            for s in value.get("subjects", [])
        ]
        return dict(roleRef=value.get("roleRef"), subjects=sorted(subjects, key=digest))
    origin.require(value["kind"] == "Secret", "Native origin support kind unsupported")
    try:
        data = {
            k: base64.b64decode(v, validate=True)
            for k, v in value.get("data", {}).items()
        }
        data.update(
            {k: v.encode("utf-8") for k, v in value.get("stringData", {}).items()}
        )
    except (ValueError, TypeError, AttributeError):
        raise origin.AccountingUnavailable(
            "Native origin support encoding invalid"
        ) from None
    return dict(
        type=value.get("type") or "Opaque",
        data={k: hashlib.sha256(v).hexdigest() for k, v in data.items()},
    )


def manifest(bodies):
    origin.require(
        0 < len(bodies) <= MAX_ITEMS, "Native origin support manifest unavailable"
    )
    result = [
        dict(kind=b["kind"], name=b["metadata"]["name"], digest=digest(_content(b)))
        for b in bodies
    ]
    origin.require(
        len({(v["kind"], v["name"]) for v in result}) == len(result),
        "Native origin duplicate support",
    )
    return result


def original_bodies(db, intent):
    bodies = origin.load_launch(db, intent).get("support_bodies")
    origin.require(
        isinstance(bodies, list), "Native origin original support manifests unavailable"
    )
    origin.require(
        manifest(bodies) == [v.model_dump(mode="json") for v in intent.support_intents],
        "Native origin original support manifest changed",
    )
    for body in bodies:
        origin.require(
            body["metadata"]["namespace"] == intent.namespace,
            "Native origin support namespace changed",
        )
        if intent.namespace_mode == "managed":
            origin.require(
                body["metadata"].get("annotations")
                == {ATTEMPT: str(intent.id), ITEM: digest(manifest([body])[0])},
                "Native origin original support provenance unavailable",
            )
    if intent.namespace_mode != "managed":
        origin.require(
            all(b["metadata"]["name"] != INVENTORY_ROLE for b in bodies),
            "External inventory binding denied",
        )
    return bodies


def readback(expected, observed):
    meta = observed.get("metadata", {})
    origin.require(
        all(meta.get(k) == expected["metadata"][k] for k in ("name", "namespace"))
        and meta.get("uid")
        and not meta.get("deletionTimestamp")
        and not meta.get("ownerReferences"),
        "Native origin support identity changed",
    )
    origin.require(
        observed.get("kind") == expected["kind"]
        and observed.get("apiVersion") == expected["apiVersion"]
        and _content(observed) == _content(expected),
        "Native origin support content changed",
    )
    origin.require(
        all(
            meta.get("annotations", {}).get(k) == v
            for k, v in expected["metadata"].get("annotations", {}).items()
        ),
        "Native origin support provenance changed",
    )
    return dict(
        kind=expected["kind"],
        name=meta["name"],
        uid=meta["uid"],
        digest=digest(_content(observed)),
    )


def _rows(db, intent_id):
    rows = (
        db.query(NativeOriginSupportHandoff)
        .filter_by(intent_id=intent_id)
        .order_by(NativeOriginSupportHandoff.id)
        .all()
    )
    origin.require(len(rows) <= 100, "Native origin support history limit")
    for row in rows:
        writers._checked(row)
    return rows


def begin(db, intent, body=None):
    token = origin.begin_io(db, str(intent.id), "support" if body else "shared-support")
    writer = writers.register_current(db, str(intent.id))
    item = (
        manifest([body])[0]
        if body
        else dict(kind="SharedCache", name="s3", digest="unowned")
    )
    payload = dict(
        intent_id=str(intent.id),
        writer_id=writer.id,
        writer_digest=writer.digest,
        scope="project" if body else "shared",
        namespace=intent.namespace if body else "troshka-cache",
        item=item,
    )
    db.add(
        NativeOriginSupportHandoff(
            id=token, intent_id=str(intent.id), digest=digest(payload), payload=payload
        )
    )
    db.commit()
    return token


def _receipt(db, row, observed):
    payload = dict(handoff_id=row.id, handoff_digest=row.digest, observed=observed)
    existing = db.get(NativeOriginSupportReceipt, row.id)
    if existing:
        origin.require(
            writers._checked(existing) == payload,
            "Native origin support receipt changed",
        )
    else:
        db.add(
            NativeOriginSupportReceipt(
                handoff_id=row.id, digest=digest(payload), payload=payload
            )
        )
    db.flush()


def finish(db, intent, token, observed):
    _, pointer = origin._locked(db, str(intent.id), allow_cancelled=True)
    origin.require(pointer.io_token == token, "Native origin support handoff changed")
    row = db.get(NativeOriginSupportHandoff, token)
    origin.require(row is not None, "Native origin support handoff unavailable")
    writers._checked(row)
    _receipt(db, row, observed)
    pointer.io_phase, pointer.io_token, pointer.io_job_id = None, None, None
    db.commit()


def _prior_receipt(db, intent, body, token):
    item = manifest([body])[0]
    known = None
    for row in _rows(db, str(intent.id)):
        if row.id == token or row.payload["item"] != item:
            continue
        receipt = db.get(NativeOriginSupportReceipt, row.id)
        if receipt is None:
            continue
        payload = writers._checked(receipt)
        origin.require(
            payload["handoff_id"] == row.id and payload["handoff_digest"] == row.digest,
            "Native origin support receipt handoff changed",
        )
        observed = payload["observed"]
        if observed is not None:
            origin.require(
                known is None or known == observed,
                "Native origin original support UID changed",
            )
            known = observed
    return known


def write(db, api, intent, body):
    # Every support boundary is a fresh cancellation check before dispatch.
    token = begin(db, intent, body)
    known = _prior_receipt(db, intent, body, token)
    bracket(api, intent, origin.receipts(db, str(intent.id))[0])
    db.commit()
    if known is not None or (
        intent.namespace_mode == "external" and body["kind"] == "RoleBinding"
    ):
        observed = api.get(body["kind"], intent.namespace, body["metadata"]["name"])
        origin.require(
            observed is not None, "Native origin external support binding unavailable"
        )
    else:
        # Never turn a failed create into ownership by name/content. An ambiguous
        # dispatch can recover later only from the protected attempt/item marker.
        created = api.post(body["kind"], body)
        value = readback(body, created)
        _, pointer = origin._locked(db, str(intent.id), allow_cancelled=True)
        origin.require(
            pointer.io_token == token, "Native origin support handoff changed"
        )
        _receipt(db, db.get(NativeOriginSupportHandoff, token), value)
        db.commit()  # Pin the successful POST UID before any separate GET.
        observed = api.get(body["kind"], intent.namespace, body["metadata"]["name"])
        origin.require(
            observed is not None, "Native origin support readback unavailable"
        )
    value = readback(body, observed)
    origin.require(
        known is None or value == known, "Native origin original support UID changed"
    )
    bracket(api, intent, origin.receipts(db, str(intent.id))[0])
    finish(db, intent, token, value)


def bracket(api, intent, receipt, *, terminating=False):
    origin.require(
        receipt is not None
        and receipt.intent_id == intent.id
        and receipt.cluster_uid == intent.cluster_uid
        and receipt.namespace == intent.namespace
        and receipt.operation_token == intent.operation_token
        and receipt.namespace_delete_authority == (intent.namespace_mode == "managed"),
        "Native origin original namespace receipt changed",
    )
    if TYPE_CHECKING:
        assert receipt is not None
    cluster = api.get("Namespace", None, "kube-system")
    origin.require(
        cluster and cluster["metadata"].get("uid") == intent.cluster_uid,
        "Native origin cluster changed",
    )
    ns = api.get("Namespace", None, intent.namespace)
    origin.require(ns is not None, "Native origin namespace unavailable")
    meta = ns["metadata"]
    origin.require(
        meta.get("uid") == receipt.namespace_uid
        and meta.get("name") == intent.namespace,
        "Native origin namespace UID changed",
    )
    origin.require(
        terminating or not meta.get("deletionTimestamp"),
        "Native origin namespace terminating",
    )
    if intent.namespace_mode == "managed":
        origin.require(
            all(
                meta.get("annotations", {}).get(k) == v
                for k, v in origin.annotations(intent).items()
            ),
            "Native origin namespace token changed",
        )
    return ns


def snapshot(db, intent):
    from app.services.quota_cleanup import fence

    fence(db, str(intent.project_id))
    _, pointer = origin._locked(db, str(intent.id), allow_cancelled=True)
    origin.require(
        pointer.cancelled, "Native origin support recovery requires cancellation"
    )
    rows = writers.registrations(db, str(intent.id))
    origin.require(rows, "Native origin original writer history unavailable")
    terminal = {r.id: digest(writers._terminal(db, r)) for r in rows}
    return dict(
        writers=terminal,
        handoffs=[(r.id, r.digest) for r in _rows(db, str(intent.id))],
        phase=pointer.io_phase,
        token=pointer.io_token,
    )


def reconcile(db, api, intent):
    """Read-only cancellation recovery; never repair absent project support."""
    before = snapshot(db, intent)
    bodies = original_bodies(db, intent)
    ns, _ = origin.receipts(db, str(intent.id))
    handoffs = [(r.id, r.payload) for r in _rows(db, str(intent.id))]
    db.commit()
    bracket(api, intent, ns)
    observed = {}
    for body in bodies:
        key = (body["kind"], body["metadata"]["name"])
        value = api.get(key[0], intent.namespace, key[1])
        observed[key] = readback(body, value) if value is not None else None
    bracket(api, intent, ns)
    origin.require(
        snapshot(db, intent) == before, "Native origin support recovery handoff changed"
    )
    started = {
        (p["item"]["kind"], p["item"]["name"])
        for _, p in handoffs
        if p["scope"] == "project"
    }
    origin.require(
        all(value is None or key in started for key, value in observed.items()),
        "Native origin support has no original write handoff",
    )
    for identity, payload in handoffs:
        row = db.get(NativeOriginSupportHandoff, identity)
        value = (
            observed[(payload["item"]["kind"], payload["item"]["name"])]
            if payload["scope"] == "project"
            else None
        )
        existing = db.get(NativeOriginSupportReceipt, identity)
        if existing is None:
            _receipt(db, row, value)
        else:
            original = writers._checked(existing)
            if value is not None:
                origin.require(
                    original["observed"] == value,
                    "Native origin original support UID changed",
                )
    _, pointer = origin._locked(db, str(intent.id), allow_cancelled=True)
    if pointer.io_phase in {"support", "shared-support"}:
        origin.require(
            pointer.io_token in {key for key, _ in handoffs},
            "Native origin unresolved support handoff",
        )
        pointer.io_phase, pointer.io_token, pointer.io_job_id = None, None, None
    db.commit()
    return [v for v in observed.values() if v is not None]
