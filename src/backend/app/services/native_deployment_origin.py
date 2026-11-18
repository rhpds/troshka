"""Generic native creation, independent of quota admission.

Mutators acquire canonical identity locks; their caller commits before provider I/O.
An unresolved handoff is retained even after cancellation. It is never evidence of
provider absence. Receipts contain digests/identities, never a second launch bundle.
"""

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

if TYPE_CHECKING:
    from rq.job import Job as RQJob  # noqa: F401 -- quoted cast

from app.models.host import Host
from app.models.native_deployment_origin import (
    NativeDeploymentNamespaceReceipt,
    NativeDeploymentOriginActive,
    NativeDeploymentOriginIntent,
    NativeDeploymentOriginLaunch,
    NativeDeploymentRootReceipt,
)
from app.schemas.native_deployment_origin import (
    LaunchDescriptor,
    NamespaceReceipt,
    OriginIntent,
    RootReceipt,
)
from app.schemas.quota import AccountingUnavailable
from app.services.kubevirt_storage_enrollment import digest
from app.services.quota_locks import lock_project

TOKEN = "troshka.redhat.com/origin-token"
INTENT = "troshka.redhat.com/origin-intent"
ATTEMPT = "troshka.redhat.com/origin-attempt"
PROJECT = "troshka.redhat.com/project-id"


@dataclass(frozen=True)
class _HandoffSnapshot:
    phase: str | None
    token: str | None
    job_id: str | None
    cancelled: bool
    project_state: str


def require(value, message):
    if not value:
        raise AccountingUnavailable(message)


def capture_enabled():
    from app.core.config import config

    return config.get("native_origin.capture_enabled", False) is True


def _validated(row, schema):
    value = schema.model_validate(row.payload)
    require(
        digest(value.model_dump(mode="json")) == row.digest,
        "Native origin immutable payload changed",
    )
    return value


def active(db, project_id):
    pointer = (
        db.query(NativeDeploymentOriginActive)
        .filter_by(project_id=project_id)
        .populate_existing()
        .first()
    )
    if pointer is None:
        return None
    row = db.get(NativeDeploymentOriginIntent, pointer.intent_id)
    require(row is not None, "Native origin intent missing")
    return _validated(row, OriginIntent)


def receipts(db, intent_id):
    result = []
    for model, schema in (
        (NativeDeploymentNamespaceReceipt, NamespaceReceipt),
        (NativeDeploymentRootReceipt, RootReceipt),
    ):
        row = db.query(model).filter_by(intent_id=intent_id).populate_existing().first()
        result.append(_validated(row, schema) if row else None)
    return tuple(result)


def _identity(db, project_id):
    project, owner, organization = lock_project(
        db, project_id, require_organization=False
    )
    host = (
        db.query(Host).filter_by(id=project.host_id).populate_existing().first()
        if project.host_id
        else None
    )
    require(
        host is not None and host.host_type == "kubevirt-cluster",
        "Native origin requires selected native host",
    )
    if TYPE_CHECKING:
        assert host is not None and host.host_type == "kubevirt-cluster"
    require(
        project.provider_id in (None, host.provider_id),
        "Native origin provider identity changed",
    )
    identity: dict[str, Any] = dict(
        project_id=project.id,
        owner_id=owner.id,
        organization_id=organization.id if organization else None,
        host_id=host.id,
        provider_id=host.provider_id,
    )
    return project, identity


def prepare(db, project, provider, descriptor):
    from app.models.provider import Provider

    provider_id = provider.id
    project, identity = _identity(db, project.id)
    provider = (
        db.query(Provider).filter_by(id=provider_id).populate_existing().one_or_none()
    )
    require(project.state == "deploying", "Native origin requires active deployment")
    require(
        provider is not None
        and identity["provider_id"] == provider.id
        and provider.type == "kubevirt",
        "Native origin provider changed",
    )
    supplied = dict(descriptor)
    origin_id = supplied.pop("origin_id", None)
    operation_token = supplied.pop("operation_token", None)
    attempt_id = supplied.pop("attempt_id", None)
    launch = LaunchDescriptor.model_validate(supplied)
    existing = active(db, project.id)
    if existing:
        _locked(db, str(existing.id))
        expected = launch.model_dump(mode="json")
        actual = {key: existing.model_dump(mode="json")[key] for key in expected}
        require(expected == actual, "Native origin intent descriptor changed")
        require(
            operation_token in (None, str(existing.operation_token)),
            "Native origin intent token changed",
        )
        require(
            attempt_id in (None, str(existing.attempt_id)),
            "Native origin intent attempt changed",
        )
        require(
            origin_id in (None, str(existing.id)), "Native origin intent ID changed"
        )
        return existing
    intent = OriginIntent(
        **launch.model_dump(),
        **identity,
        id=origin_id or uuid4(),
        attempt_id=attempt_id or uuid4(),
        operation_token=operation_token or uuid4(),
    )
    payload = intent.model_dump(mode="json")
    db.add(
        NativeDeploymentOriginIntent(
            id=str(intent.id),
            project_id=project.id,
            attempt_id=str(intent.attempt_id),
            digest=digest(payload),
            payload=payload,
        )
    )
    db.flush()
    db.add(
        NativeDeploymentOriginActive(
            project_id=project.id, intent_id=str(intent.id), cancelled=False
        )
    )
    db.flush()
    return intent


def _locked(db, intent_id, *, allow_cancelled=False):
    row = db.get(NativeDeploymentOriginIntent, intent_id)
    require(row is not None, "Native origin intent missing")
    intent = _validated(row, OriginIntent)
    project, identity = _identity(db, str(intent.project_id))
    pointer = (
        db.query(NativeDeploymentOriginActive)
        .filter_by(project_id=project.id)
        .populate_existing()
        .one_or_none()
    )
    require(
        pointer is not None and pointer.intent_id == intent_id,
        "Native origin active attempt changed",
    )
    if TYPE_CHECKING:
        assert pointer is not None
    require(
        all(str(getattr(intent, key)) == str(value) for key, value in identity.items()),
        "Native origin ownership or provider changed",
    )
    require(
        allow_cancelled or (not pointer.cancelled and project.state == "deploying"),
        "Native origin attempt cancelled or no longer deploying",
    )
    return intent, cast(NativeDeploymentOriginActive, pointer)


def begin_io(db, intent_id, phase):
    from rq import get_current_job

    from app.services import native_origin_writers as writers

    intent, pointer = _locked(db, intent_id)
    require(pointer.io_phase is None, "Native origin has unresolved provider handoff")
    writer = writers.register_current(db, intent_id)
    pointer.io_phase = phase
    pointer.io_token = str(uuid4())
    job = get_current_job()
    pointer.io_job_id = job.id if job else None
    if phase == "root":
        writers.submitted_root(db, intent_id, writer, pointer.io_token)
    db.flush()
    return pointer.io_token


def finish_io(db, intent_id, io_token):
    _, pointer = _locked(db, intent_id)
    require(
        pointer.io_token == io_token and io_token is not None,
        "Native origin provider handoff owner changed",
    )
    pointer.io_phase = None
    pointer.io_token = None
    pointer.io_job_id = None
    db.flush()


def cancel(db, project_id):
    from app.services.quota_locks import lock_retained_project

    lock_retained_project(db, project_id)
    pointer = (
        db.query(NativeDeploymentOriginActive)
        .filter_by(project_id=project_id)
        .populate_existing()
        .first()
    )
    if pointer:
        pointer.cancelled = True
        from app.services.native_origin_descendants import close_origin

        close_origin(db, pointer.intent_id)
        db.flush()
    return pointer


def annotations(intent):
    return {
        TOKEN: str(intent.operation_token),
        INTENT: str(intent.id),
        ATTEMPT: str(intent.attempt_id),
        PROJECT: str(intent.project_id),
    }


def _namespace(intent, observed):
    require(
        observed.get("cluster_uid") == intent.cluster_uid,
        "Native origin cluster changed",
    )
    meta = observed["namespace"]["metadata"]
    require(
        meta.get("name") == intent.namespace
        and meta.get("uid")
        and not meta.get("deletionTimestamp"),
        "Native origin namespace unavailable",
    )
    if intent.namespace_mode == "managed":
        require(
            all(
                meta.get("annotations", {}).get(k) == v
                for k, v in annotations(intent).items()
            ),
            "Native origin namespace token changed",
        )
    return NamespaceReceipt(
        intent_id=intent.id,
        cluster_uid=intent.cluster_uid,
        namespace=intent.namespace,
        namespace_uid=meta["uid"],
        operation_token=intent.operation_token,
        namespace_delete_authority=intent.namespace_mode == "managed",
    )


def _insert_receipt(db, model, intent_id, value):
    payload = value.model_dump(mode="json")
    existing = (
        db.query(model).filter_by(intent_id=intent_id).populate_existing().first()
    )
    if existing:
        require(
            existing.payload == payload and existing.digest == digest(payload),
            "Native origin receipt identity changed",
        )
    else:
        db.add(
            model(
                id=str(uuid4()),
                intent_id=intent_id,
                payload=payload,
                digest=digest(payload),
            )
        )
    db.flush()
    return value


def bind_namespace(db, intent_id, observed, *, record_cancelled=False, io_token=None):
    intent, pointer = _locked(db, intent_id, allow_cancelled=record_cancelled)
    if pointer.io_phase == "namespace":
        require(
            io_token is not None and io_token == pointer.io_token,
            "Native origin namespace handoff owner changed",
        )
    receipt = _insert_receipt(
        db, NativeDeploymentNamespaceReceipt, intent_id, _namespace(intent, observed)
    )
    if pointer.io_phase == "namespace":
        pointer.io_phase = None
        pointer.io_token = None
        pointer.io_job_id = None
    return receipt


def bind_root(db, intent_id, observed, *, record_cancelled=False, io_token=None):
    intent, pointer = _locked(db, intent_id, allow_cancelled=record_cancelled)
    if pointer.io_phase == "root":
        require(
            io_token is not None and io_token == pointer.io_token,
            "Native origin root handoff owner changed",
        )
    ns = _namespace(intent, observed)
    retained_ns, _ = receipts(db, intent_id)
    require(retained_ns == ns, "Native origin namespace receipt changed")
    root = observed["root"]
    meta = root["metadata"]
    require(
        meta.get("name") == intent.root_name
        and meta.get("namespace") == intent.namespace
        and meta.get("uid")
        and not meta.get("deletionTimestamp"),
        "Native origin root identity changed",
    )
    require(
        all(
            meta.get("annotations", {}).get(k) == v
            for k, v in annotations(intent).items()
        ),
        "Native origin root token changed",
    )
    require(
        root.get("spec", {}).get("projectId") == str(intent.project_id)
        and digest(root.get("spec")) == intent.cr_spec_digest,
        "Native origin root spec changed",
    )
    value = RootReceipt(
        **ns.model_dump(),
        root_name=intent.root_name,
        root_uid=meta["uid"],
        generation=meta.get("generation", 0),
        cr_spec_digest=intent.cr_spec_digest,
    )
    receipt = _insert_receipt(db, NativeDeploymentRootReceipt, intent_id, value)
    if pointer.io_phase == "root":
        pointer.io_phase = None
        pointer.io_token = None
        pointer.io_job_id = None
    return receipt


def require_idle(project_id, original_job_id=None):
    from rq import get_current_job

    from app.core import redis

    require(redis.is_redis_available(), "Native origin job tracking unavailable")
    try:
        reference = redis.get_redis().get(f"job:project:{project_id}")
    except Exception as exc:
        raise AccountingUnavailable("Native origin job tracking unavailable") from exc
    current = get_current_job()
    own_recovery = (
        current
        and current.id != original_job_id
        and current.meta.get("project_id") == project_id
    )
    require(
        not reference or (own_recovery and reference == cast("RQJob", current).id),
        "Native origin project has in-flight work",
    )
    require(
        original_job_id is not None,
        "Native origin original worker tracking unavailable",
    )
    if TYPE_CHECKING:
        assert original_job_id is not None
    try:
        from rq.job import Job

        status = Job.fetch(
            original_job_id, connection=redis.get_redis_raw()
        ).get_status(refresh=True)
    except Exception as exc:
        raise AccountingUnavailable(
            "Native origin original worker tracking unavailable"
        ) from exc
    require(
        status in {"finished", "failed", "stopped", "canceled"},
        "Native origin original writer is still active",
    )


def _handoff_snapshot(db, intent, *, allow_cancelled=False):
    from app.models.project import Project

    _, pointer = _locked(db, str(intent.id), allow_cancelled=allow_cancelled)
    snapshot = _HandoffSnapshot(
        phase=pointer.io_phase,
        token=pointer.io_token,
        job_id=pointer.io_job_id,
        cancelled=pointer.cancelled,
        project_state=db.get(Project, str(intent.project_id)).state,
    )
    return snapshot, pointer


def recover_handoff(db, intent):
    """Check remote tracking unlocked, then recheck the exact durable handoff."""
    snapshot, _ = _handoff_snapshot(db, intent, allow_cancelled=True)
    db.commit()
    if snapshot.phase:
        require(
            snapshot.phase != "support",
            "Native origin support handoff requires exact support recovery",
        )
        require_idle(str(intent.project_id), snapshot.job_id)
    current, _ = _handoff_snapshot(db, intent, allow_cancelled=True)
    require(current == snapshot, "Native origin recovery handoff changed")
    db.commit()
    return snapshot.phase, snapshot.token


def retry_handoff(db, intent, phase, token):
    """Reissue an absent original create only after independent worker-idle proof."""
    if phase is None:
        return
    snapshot, _ = _handoff_snapshot(db, intent)
    require(
        snapshot.phase == phase
        and snapshot.token == token
        and phase in {"namespace", "root"},
        "Native origin retry handoff changed",
    )
    db.commit()
    require_idle(str(intent.project_id), snapshot.job_id)
    current, pointer = _handoff_snapshot(db, intent)
    require(current == snapshot, "Native origin retry handoff changed")
    pointer.io_phase, pointer.io_token = None, None
    pointer.io_job_id = None
    db.commit()


def retain_launch(db, intent, launch):
    from app.core.encryption import encrypt

    require(
        digest(launch["project_cr"]["spec"]) == intent.cr_spec_digest,
        "Native origin launch spec changed",
    )
    existing = db.get(NativeDeploymentOriginLaunch, str(intent.id))
    if existing is None:
        db.add(
            NativeDeploymentOriginLaunch(
                id=str(intent.id),
                encrypted_payload=encrypt(json.dumps(launch, sort_keys=True)),
            )
        )
        db.flush()


def load_launch(db, intent):
    from app.core.encryption import decrypt
    from app.services.providers.kubevirt import _origin_support_intents

    row = db.get(NativeDeploymentOriginLaunch, str(intent.id))
    require(row is not None, "Native origin protected launch inputs unavailable")
    try:
        launch = json.loads(decrypt(row.encrypted_payload))
    except (ValueError, TypeError) as exc:
        raise AccountingUnavailable(
            "Native origin protected launch cannot be decrypted"
        ) from exc
    require(
        digest(launch["project_cr"]["spec"]) == intent.cr_spec_digest,
        "Native origin protected launch changed",
    )
    meta = launch["project_cr"]["metadata"]
    require(
        meta.get("name") == intent.root_name
        and meta.get("namespace") == intent.namespace,
        "Native origin protected launch identity changed",
    )
    expected = [entry.model_dump(mode="json") for entry in intent.support_intents]
    if "support_bodies" in launch:
        from app.services.native_origin_support import manifest

        actual = manifest(launch["support_bodies"])
    else:
        actual = _origin_support_intents(
            launch["s3_config"], launch["cluster_s3"], launch["central_s3"]
        )
    require(actual == expected, "Native origin protected support inputs changed")
    return launch


def recover(api, intent, retained_receipts):
    """Read-only exact recovery; caller binds using fresh canonical locks."""
    cluster = api.get("Namespace", None, "kube-system")["metadata"]["uid"]
    require(cluster == intent.cluster_uid, "Native origin cluster changed")
    ns = api.get("Namespace", None, intent.namespace)
    if ns is None:
        require(not any(retained_receipts), "Native origin bound namespace disappeared")
        require(
            api.get("Namespace", None, "kube-system")["metadata"]["uid"] == cluster,
            "Native origin recovery cluster bracket changed",
        )
        return None
    observed = dict(cluster_uid=cluster, namespace=ns)
    value = _namespace(intent, observed)
    require(
        retained_receipts[0] in (None, value), "Native origin namespace UID changed"
    )
    root = api.get("TroshkaProject", intent.namespace, intent.root_name)
    if root is None:
        require(retained_receipts[1] is None, "Native origin bound root disappeared")
        require(
            api.get("Namespace", None, intent.namespace)["metadata"]["uid"]
            == value.namespace_uid
            and api.get("Namespace", None, "kube-system")["metadata"]["uid"] == cluster,
            "Native origin recovery identity bracket changed",
        )
        return observed
    observed["root"] = root
    # Pure comparison mirrors binding, without publishing any authority.
    meta = root["metadata"]
    require(
        meta.get("namespace") == intent.namespace
        and meta.get("name") == intent.root_name
        and meta.get("uid")
        and not meta.get("deletionTimestamp"),
        "Native origin root changed",
    )
    require(
        all(
            meta.get("annotations", {}).get(k) == v
            for k, v in annotations(intent).items()
        ),
        "Native origin root token changed",
    )
    require(
        digest(root.get("spec")) == intent.cr_spec_digest,
        "Native origin root spec changed",
    )
    if retained_receipts[1]:
        require(
            meta["uid"] == retained_receipts[1].root_uid
            and meta.get("generation") == retained_receipts[1].generation,
            "Native origin root UID/generation changed",
        )
    require(
        api.get("Namespace", None, intent.namespace)["metadata"]["uid"]
        == value.namespace_uid
        and api.get("Namespace", None, "kube-system")["metadata"]["uid"] == cluster,
        "Native origin recovery identity bracket changed",
    )
    return observed


def reconcile_cancelled(db, api, intent):
    """Retain an exact late POST result, without allowing any continuation.

    An absent object cannot settle ambiguous I/O. A support write cannot be
    settled by finding the namespace/root and remains a cleanup hold.
    """
    phase, token = recover_handoff(db, intent)
    retained = receipts(db, str(intent.id))
    db.commit()
    observed = recover(api, intent, retained)
    if observed is None:
        return None
    bind_namespace(
        db,
        str(intent.id),
        observed,
        record_cancelled=True,
        io_token=token if phase == "namespace" else None,
    )
    if "root" in observed:
        bind_root(
            db,
            str(intent.id),
            observed,
            record_cancelled=True,
            io_token=token if phase == "root" else None,
        )
    db.commit()
    return observed
