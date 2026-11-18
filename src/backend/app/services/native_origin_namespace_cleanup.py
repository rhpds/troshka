"""Complete bounded namespace inventory and the root-never-submitted cleanup branch."""


import json
import re
import time
from copy import deepcopy
from urllib.parse import quote
from uuid import uuid4

from app.models.native_origin_namespace import (
    NativeOriginNamespaceCleanup,
    NativeOriginNamespaceEvidence,
)
from app.schemas.native_origin_namespace import (
    InventoryObject,
    NamespaceCleanup,
    NamespaceInventory,
)
from app.services import native_deployment_origin as origin
from app.services import native_origin_support as support
from app.services import native_origin_writers as writers
from app.services.kubevirt_storage_enrollment import digest

MAX_RESOURCES = 2000
MAX_REQUESTS = 5000
MAX_OBJECTS = 1000
RBAC = "rbac.authorization.k8s.io/v1"
LEGACY_RBAC = "authorization.openshift.io/v1"


class InventoryClient:
    """JSON only, bounded response/request/time budget, no mutation retries."""

    def __init__(self, client):
        self.client = client
        self.deadline = time.monotonic() + 55
        self.requests = 0
        self.response_bytes = 0

    def path(
        self,
        path,
        query=(),
        *,
        method="GET",
        body=None,
        absent=False,
        content_type="application/json",
    ):
        from urllib3 import Timeout

        self.requests += 1
        remaining = self.deadline - time.monotonic()
        origin.require(
            remaining > 0 and self.requests <= MAX_REQUESTS,
            "Native origin inventory budget exceeded",
        )
        headers = {
            **self.client.default_headers,
            "Accept": "application/json",
            "Content-Type": content_type,
        }
        query = list(query)
        self.client.update_params_for_auth(headers, query, ["BearerToken"])
        options = dict(
            fields=query if body is None else None,
            body=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            preload_content=False,
            redirect=False,
            retries=False,
            timeout=Timeout(total=min(5, remaining)),
        )
        url = self.client.configuration.host + path
        if method == "GET":
            from app.services.native_origin_inventory_transport import read

            status, raw = read(
                self.client.rest_client.pool_manager,
                url,
                deadline=min(self.deadline, time.monotonic() + 5),
                **options,
            )
        else:
            status, raw = self._mutation(method, url, options)
        if absent and status == 404:
            return None
        origin.require(200 <= status < 300, "Native origin inventory API unavailable")
        origin.require(
            len(raw) <= 4 * 1024 * 1024, "Native origin inventory response limit"
        )
        self.response_bytes += len(raw)
        origin.require(
            self.response_bytes <= 32 * 1024 * 1024,
            "Native origin inventory aggregate response limit",
        )
        value = json.loads(raw)
        origin.require(
            isinstance(value, dict), "Native origin inventory response invalid"
        )
        return value

    def _mutation(self, method, url, options):
        # DELETE stays synchronous and is never put into a cancellable helper.
        # Its durable intent/ambiguous-result protocol owns mutation recovery.
        response = self.client.rest_client.pool_manager.request(method, url, **options)
        try:
            return response.status, response.read(4 * 1024 * 1024 + 1)
        finally:
            response.close()

    def get(self, kind, namespace, name):
        routes = {
            "Namespace": ("/api/v1", "namespaces"),
            "Secret": ("/api/v1", "secrets"),
            "RoleBinding": ("/apis/rbac.authorization.k8s.io/v1", "rolebindings"),
        }
        origin.require(kind in routes, "Native origin inventory resource unsupported")
        prefix, resource = routes[kind]
        scope = "/namespaces/" + quote(namespace, safe="") if namespace else ""
        return self.path(
            prefix + scope + "/" + resource + "/" + quote(name, safe=""), absent=True
        )

    def delete_namespace(self, name, uid, resource_version):
        return self.path(
            "/api/v1/namespaces/" + quote(name, safe=""),
            method="DELETE",
            body={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": uid, "resourceVersion": resource_version},
            },
            absent=True,
        )


def _versions(api):
    core, groups = api.path("/api"), api.path("/apis")
    origin.require(
        core.get("kind") == "APIVersions"
        and isinstance(core.get("versions"), list)
        and core["versions"],
        "Native origin core discovery incomplete",
    )
    origin.require(
        groups.get("kind") == "APIGroupList" and isinstance(groups.get("groups"), list),
        "Native origin group discovery incomplete",
    )
    versions = [("", v, "/api/" + quote(v, safe="")) for v in core["versions"]]
    for group in groups["groups"]:
        served = group.get("versions")
        name = group.get("name")
        origin.require(
            name and isinstance(served, list) and served,
            "Native origin group versions incomplete",
        )
        preferred = group.get("preferredVersion", {}).get("groupVersion")
        for entry in sorted(served, key=lambda v: v.get("groupVersion") != preferred):
            gv = entry.get("groupVersion")
            origin.require(
                gv == name + "/" + entry.get("version", ""),
                "Native origin discovery group changed",
            )
            versions.append(
                (
                    name,
                    gv,
                    "/apis/"
                    + quote(name, safe="")
                    + "/"
                    + quote(entry["version"], safe=""),
                )
            )
    origin.require(
        len(versions) <= MAX_RESOURCES and len(set(versions)) == len(versions),
        "Native origin discovery versions limit or duplicates",
    )
    return versions


def discover(api):
    resources = {}
    for group, version, path in _versions(api):
        listing = api.path(path)
        origin.require(
            listing.get("kind") == "APIResourceList"
            and listing.get("groupVersion") == version
            and isinstance(listing.get("resources"), list),
            "Native origin discovery incomplete",
        )
        for resource in listing["resources"]:
            name, kind = resource.get("name"), resource.get("kind")
            origin.require(
                isinstance(name, str)
                and isinstance(kind, str)
                and isinstance(resource.get("verbs"), list)
                and isinstance(resource.get("namespaced"), bool),
                "Native origin discovery resource invalid",
            )
            if (
                not resource["namespaced"]
                or "/" in name
                or "list" not in resource["verbs"]
            ):
                continue
            origin.require(
                re.fullmatch(r"[a-z][a-z0-9-]*", name),
                "Native origin discovery resource name invalid",
            )
            resources.setdefault(
                (group, name),
                dict(path=path, resource=name, kind=kind, version=version),
            )
    origin.require(
        0 < len(resources) <= MAX_RESOURCES, "Native origin discovery resource limit"
    )
    return [resources[key] for key in sorted(resources)]


def _list_pages(api, namespace, resource):
    path = (
        resource["path"]
        + "/namespaces/"
        + quote(namespace, safe="")
        + "/"
        + resource["resource"]
    )
    rows, seen_tokens, token, version = [], set(), "", None
    for _ in range(100):
        query = [("limit", "100")]
        if token:
            query.append(("continue", token))
        page = api.path(path, query)
        meta = page.get("metadata", {})
        origin.require(
            isinstance(page.get("items"), list) and meta.get("resourceVersion"),
            "Native origin inventory page incomplete",
        )
        version = version or meta["resourceVersion"]
        origin.require(
            meta["resourceVersion"] == version,
            "Native origin inventory page version changed",
        )
        rows.extend(page["items"])
        origin.require(len(rows) <= MAX_OBJECTS, "Native origin inventory object limit")
        token = meta.get("continue", "")
        if not token:
            return rows
        origin.require(
            isinstance(token, str) and token not in seen_tokens,
            "Native origin inventory pagination incomplete",
        )
        seen_tokens.add(token)
    raise origin.AccountingUnavailable("Native origin inventory page limit")


def inventory_rows(api, namespace, resources):
    rows, seen = [], set()
    for resource in resources:
        for value in _list_pages(api, namespace, resource):
            meta = value.get("metadata", {})
            origin.require(
                meta.get("namespace") == namespace
                and meta.get("uid")
                and meta.get("name"),
                "Native origin inventory namespace or identity changed",
            )
            value.setdefault("kind", resource["kind"])
            value.setdefault("apiVersion", resource["version"])
            if value["kind"] == "RoleBinding" and value["apiVersion"] in {
                RBAC,
                LEGACY_RBAC,
            }:
                origin.require(
                    resource["resource"] == "rolebindings"
                    and resource["path"] == "/apis/" + value["apiVersion"],
                    "Native origin RoleBinding alias resource invalid",
                )
            identity = (resource["path"], resource["resource"], meta["uid"])
            origin.require(
                value["kind"] == resource["kind"]
                and value["apiVersion"] == resource["version"]
                and identity not in seen,
                "Native origin inventory duplicate or invalid identity",
            )
            seen.add(identity)
            rows.append(value)
            origin.require(
                len(rows) <= MAX_OBJECTS, "Native origin inventory object limit"
            )
    return _merge_rolebinding_alias(rows, resources)


def _legacy_subjects(value):
    subjects, users, groups = [], [], []
    kinds: dict[str | None, str] = {
        "User": "User",
        "SystemUser": "User",
        "Group": "Group",
        "SystemGroup": "Group",
        "ServiceAccount": "ServiceAccount",
    }
    for subject in value.get("subjects") or []:
        clean = {k: v for k, v in subject.items() if v != ""}
        kind = kinds.get(clean.get("kind"))
        origin.require(
            kind and clean.get("name") and set(clean) <= {"kind", "name", "namespace"},
            "Native origin RoleBinding alias subject invalid",
        )
        item = dict(kind=kind, name=clean["name"])
        if kind == "ServiceAccount":
            item["namespace"] = clean.get("namespace", "")
            users.append(
                f"system:serviceaccount:{item['namespace'] or value['metadata']['namespace']}:{item['name']}"
            )
        else:
            origin.require(
                not clean.get("namespace"),
                "Native origin RoleBinding alias subject namespace invalid",
            )
            item["apiGroup"] = "rbac.authorization.k8s.io"
            (users if kind == "User" else groups).append(item["name"])
        subjects.append(item)
    origin.require(
        (value.get("userNames") or []) == users
        and (value.get("groupNames") or []) == groups,
        "Native origin RoleBinding alias subject mirrors changed",
    )
    return subjects


def _validate_rolebinding_alias(canonical, legacy):
    # Pinned OpenShift RoleBindingFromRBAC conversion contract: retain complete
    # metadata, translate only autoupdate, and validate the generated mirrors.
    meta = deepcopy(canonical["metadata"])
    annotations = meta.get("annotations", {})
    auto = annotations.get("rbac.authorization.kubernetes.io/autoupdate")
    if auto in {"true", "false"}:
        annotations.pop("rbac.authorization.kubernetes.io/autoupdate")
        annotations["openshift.io/reconcile-protect"] = (
            "false" if auto == "true" else "true"
        )
    origin.require(
        meta == legacy["metadata"], "Native origin RoleBinding alias metadata changed"
    )
    ref = {k: v for k, v in legacy.get("roleRef", {}).items() if v != ""}
    origin.require(
        ref.get("name")
        and set(ref) <= {"name", "namespace"}
        and ref.get("namespace", meta["namespace"]) == meta["namespace"],
        "Native origin RoleBinding alias role reference invalid",
    )
    normalized = dict(
        kind="RoleBinding",
        roleRef=dict(
            apiGroup="rbac.authorization.k8s.io",
            kind="Role" if ref.get("namespace") else "ClusterRole",
            name=ref["name"],
        ),
        subjects=_legacy_subjects(legacy),
    )
    origin.require(
        set(legacy)
        <= {
            "apiVersion",
            "kind",
            "metadata",
            "roleRef",
            "subjects",
            "userNames",
            "groupNames",
        }
        and support._content(normalized) == support._content(canonical),
        "Native origin RoleBinding alias semantics changed",
    )


def _merge_rolebinding_alias(rows, resources):
    aliases = {
        r["version"]
        for r in resources
        if r["resource"] == "rolebindings"
        and r["kind"] == "RoleBinding"
        and r["version"] in {RBAC, LEGACY_RBAC}
        and r["path"] == "/apis/" + r["version"]
    }
    canonical = {
        v["metadata"]["uid"]: v
        for v in rows
        if v["apiVersion"] == RBAC and v["kind"] == "RoleBinding"
    }
    legacy = {
        v["metadata"]["uid"]: v
        for v in rows
        if v["apiVersion"] == LEGACY_RBAC and v["kind"] == "RoleBinding"
    }
    if LEGACY_RBAC in aliases:
        origin.require(
            RBAC in aliases and set(canonical) == set(legacy),
            "Native origin RoleBinding alias inventory changed",
        )
        for uid, value in legacy.items():
            _validate_rolebinding_alias(canonical[uid], value)
    retained = [
        v
        for v in rows
        if not (
            LEGACY_RBAC in aliases
            and v["apiVersion"] == LEGACY_RBAC
            and v["kind"] == "RoleBinding"
        )
    ]
    origin.require(
        len({v["metadata"]["uid"] for v in retained}) == len(retained),
        "Native origin inventory duplicate or invalid identity",
    )
    return retained


def _object_digest(value):
    # Never retain arbitrary bodies (particularly Secret data) as evidence.
    clean = {k: v for k, v in value.items() if k not in {"metadata", "status"}}
    clean["metadata"] = {
        k: v
        for k, v in value["metadata"].items()
        if k not in {"resourceVersion", "managedFields"}
    }
    return digest(clean)


def _bootstrap(value, by_uid):
    from app.services.native_managed_power import helpers

    # Root-aware grants and arbitrary known-looking objects are not admitted.
    kind, name = value["kind"], value["metadata"]["name"]
    allowed = {
        "ServiceAccount": {"default", "builder", "deployer"},
        "ConfigMap": {
            "kube-root-ca.crt",
            "openshift-service-ca.crt",
            "odh-kserve-custom-ca-bundle",
        },
        "RoleBinding": {
            "system:deployers",
            "system:image-builders",
            "system:image-pullers",
        },
    }
    if kind != "Secret" and name not in allowed.get(kind, set()):
        return False
    expected_version = "rbac.authorization.k8s.io/v1" if kind == "RoleBinding" else "v1"
    return (
        value["apiVersion"] == expected_version
        and helpers().cleanup_bootstrap(value, by_uid) is not None
    )


def seal(api, intent, namespace_receipt, support_receipts, writer_proofs):
    support.bracket(api, intent, namespace_receipt)
    resources = discover(api)
    rows = inventory_rows(api, intent.namespace, resources)
    expected = {(v["kind"], v["name"]): v for v in support_receipts}
    by_uid = {v["metadata"]["uid"]: v for v in rows}
    objects = []
    for value in rows:
        key = value["kind"], value["metadata"]["name"]
        exact = expected.get(key)
        if exact is not None:
            origin.require(
                value["metadata"]["uid"] == exact["uid"]
                and digest(support._content(value)) == exact["digest"],
                "Native origin sealed support changed",
            )
        else:
            origin.require(
                _bootstrap(value, by_uid),
                "Native origin unknown or workload object in namespace",
            )
        objects.append(
            InventoryObject(
                api_version=value["apiVersion"],
                kind=key[0],
                name=key[1],
                uid=value["metadata"]["uid"],
                digest=_object_digest(value),
            )
        )
    present = {(v.kind, v.name) for v in objects}
    origin.require(
        set(expected) <= present, "Native origin support disappeared during inventory"
    )
    origin.require(
        discover(api) == resources, "Native origin discovery changed during inventory"
    )
    support.bracket(api, intent, namespace_receipt)
    return NamespaceInventory(
        namespace=namespace_receipt,
        discovery_digest=digest(resources),
        objects=tuple(sorted(objects, key=lambda v: (v.api_version, v.kind, v.name))),
        support_digest=digest(support_receipts),
        writers_digest=digest(writer_proofs),
    )


def _load(row):
    origin.require(
        row is not None and row.digest == digest(row.payload),
        "Native origin namespace cleanup changed",
    )
    return NamespaceCleanup.model_validate(row.payload)


def _save(db, work, **updates):
    value = NamespaceCleanup.model_validate({**work.model_dump(), **updates})
    row = db.get(NativeOriginNamespaceCleanup, str(value.id))
    row.payload = value.model_dump(mode="json")
    row.digest = digest(row.payload)
    db.flush()
    return value


def _evidence(db, work, phase, payload):
    row = (
        db.query(NativeOriginNamespaceEvidence)
        .filter_by(cleanup_id=str(work.id), phase=phase)
        .one_or_none()
    )
    if row:
        origin.require(
            writers._checked(row) == payload, "Native origin namespace evidence changed"
        )
    else:
        db.add(
            NativeOriginNamespaceEvidence(
                id=str(uuid4()),
                cleanup_id=str(work.id),
                phase=phase,
                digest=digest(payload),
                payload=payload,
            )
        )
    db.flush()


def _evidence_matches(db, work, phase, payload):
    rows = (
        db.query(NativeOriginNamespaceEvidence).filter_by(cleanup_id=str(work.id)).all()
    )
    matching = [
        row for row in rows if row.phase == phase or row.phase.startswith(phase + ":")
    ]
    origin.require(
        any(writers._checked(row) == payload for row in matching),
        "Native origin protected namespace evidence unavailable",
    )


def _delete_payload(work, rv):
    return dict(
        namespace_uid=work.inventory.namespace.namespace_uid,
        resource_version=rv,
        inventory_digest=digest(work.inventory.model_dump(mode="json")),
    )


def _absence_payload(work):
    return dict(
        version="namespace-absence-v1",
        cleanup_id=str(work.id),
        intent_id=str(work.intent_id),
        inventory_digest=digest(work.inventory.model_dump(mode="json")),
        namespace=work.inventory.namespace.model_dump(mode="json"),
        root_never_submitted=True,
    )


def _verify_delete_evidence(db, work):
    origin.require(
        work.inventory is not None and work.delete_resource_version,
        "Native origin delete evidence unavailable",
    )
    _evidence_matches(db, work, "sealed", work.inventory.model_dump(mode="json"))
    _evidence_matches(
        db,
        work,
        "namespace_delete_requested",
        _delete_payload(work, work.delete_resource_version),
    )


def _eligible(db, intent, project):
    from app.models.native_deployment_origin import NativeDeploymentOriginActive
    from app.models.native_origin_checkpoint import NativeOriginProducerReceipt
    from app.models.native_origin_custody import NativeOriginCustodyReceipt
    from app.models.quota import QuotaReservation
    from app.services import quota_cleanup

    origin.require(
        intent.namespace_mode == "managed", "External namespace deletion is ineligible"
    )
    origin.require(
        not writers.root_submitted(db, str(intent.id)),
        "Native origin root was submitted; namespace-only cleanup denied",
    )
    pointer = db.get(NativeDeploymentOriginActive, project.id)
    origin.require(
        pointer is not None and pointer.io_phase != "root",
        "Native origin original root POST unresolved",
    )
    origin.require(
        not db.query(NativeOriginProducerReceipt)
        .filter_by(intent_id=str(intent.id))
        .first()
        and not db.query(NativeOriginCustodyReceipt)
        .filter_by(intent_id=str(intent.id))
        .first(),
        "Native origin producer or storage liability remains",
    )
    origin.require(
        not db.query(QuotaReservation).filter_by(project_id=project.id).first(),
        "Native origin namespace cleanup cannot settle storage reservations",
    )
    quota_cleanup._require_no_unclaimed_assets(db, project)
    origin.require(
        not any(
            (topology or {}).get("_dns_records")
            for topology in (project.topology, project.deployed_topology)
        ),
        "Native origin retained DNS records require separate verified cleanup",
    )


def _locked(db, cleanup_id):
    from app.models.project import Project
    from app.services import quota_cleanup

    work = _load(db.get(NativeOriginNamespaceCleanup, cleanup_id))
    quota_cleanup.fence(db, str(work.project_id))
    intent, pointer = origin._locked(db, str(work.intent_id), allow_cancelled=True)
    work = _load(
        db.query(NativeOriginNamespaceCleanup)
        .filter_by(id=cleanup_id)
        .populate_existing()
        .one()
    )
    origin.require(
        pointer.cancelled, "Native origin namespace cleanup cancellation changed"
    )
    project = db.get(Project, str(work.project_id))
    _eligible(db, intent, project)
    return project, intent, work


def request(db, project, mode):
    """Dispatch before any root/domain/reservation prerequisites; retain cancellation."""
    from app.core.redis import enqueue_job
    from app.services import quota_cleanup

    intent = origin.active(db, project.id)
    if intent is None or writers.root_submitted(db, str(intent.id)):
        return None
    quota_cleanup.fence(db, project.id)
    intent, pointer = origin._locked(db, str(intent.id), allow_cancelled=True)
    _eligible(db, intent, project)
    pointer.cancelled = True
    row = (
        db.query(NativeOriginNamespaceCleanup)
        .filter_by(intent_id=str(intent.id))
        .one_or_none()
    )
    if row is None:
        work = NamespaceCleanup(
            id=uuid4(), intent_id=intent.id, project_id=intent.project_id, mode=mode
        )
        payload = work.model_dump(mode="json")
        row = NativeOriginNamespaceCleanup(
            id=str(work.id),
            intent_id=str(intent.id),
            project_id=project.id,
            digest=digest(payload),
            payload=payload,
        )
        db.add(row)
    work = _load(row)
    origin.require(work.mode == mode, "Native origin namespace cleanup mode changed")
    db.commit()
    enqueue_job(reconcile, str(work.id))
    return dict(
        status="cleanup_pending", id=project.id, mode=mode, cleanup_id=str(work.id)
    )


def _prepare_observation(db, cleanup_id):
    _, intent, work = _locked(db, cleanup_id)
    before = support.snapshot(db, intent)
    namespace, _ = origin.receipts(db, str(intent.id))
    if work.inventory is not None:
        origin.require(
            namespace is not None
            and namespace == work.inventory.namespace
            and namespace.namespace_delete_authority,
            "Native origin original namespace receipt unavailable or changed",
        )
    db.commit()
    return intent, work, before, namespace


def _recover_namespace(db, api, intent, before, receipt):
    if receipt is not None:
        return receipt
    origin.require(
        before["phase"] == "namespace" and before["token"],
        "Native origin namespace birth evidence unavailable",
    )
    ns = api.get("Namespace", None, intent.namespace)
    cluster = api.get("Namespace", None, "kube-system")
    origin.require(ns is not None, "Native origin original namespace POST unresolved")
    observed = dict(namespace=ns, cluster_uid=cluster["metadata"]["uid"])
    origin.require(
        support.snapshot(db, intent) == before,
        "Native origin namespace recovery handoff changed",
    )
    receipt = origin.bind_namespace(
        db, str(intent.id), observed, record_cancelled=True, io_token=before["token"]
    )
    db.commit()
    return receipt


def _observe_absence(api, intent, receipt):
    cluster = api.get("Namespace", None, "kube-system")
    origin.require(
        cluster and cluster["metadata"]["uid"] == intent.cluster_uid,
        "Native origin cluster changed",
    )
    ns = api.get("Namespace", None, intent.namespace)
    if ns is not None:
        support.bracket(api, intent, receipt, terminating=True)
        return False
    cluster = api.get("Namespace", None, "kube-system")
    origin.require(
        cluster and cluster["metadata"]["uid"] == intent.cluster_uid,
        "Native origin absence cluster changed",
    )
    return True


def advance(db, api, cleanup_id):
    intent, work, before, receipt = _prepare_observation(db, cleanup_id)
    if work.phase in {"absent", "finalized"}:
        return work
    if work.phase == "namespace_delete_requested":
        if _observe_absence(api, intent, receipt):
            _, _, current = _locked(db, cleanup_id)
            origin.require(
                current == work and support.snapshot(db, intent) == before,
                "Native origin absence handoff changed",
            )
            _verify_delete_evidence(db, work)
            _evidence(db, work, "absent", _absence_payload(work))
            work = _save(db, work, phase="absent", last_error=None)
            db.commit()
            return work
        # Ambiguous DELETE: observe original UID first. A terminating namespace
        # remains pending; a live original can be retried only after full reseal.
        ns = support.bracket(api, intent, receipt, terminating=True)
        if ns["metadata"].get("deletionTimestamp"):
            return work
    receipt = _recover_namespace(db, api, intent, before, receipt)
    observed_support = support.reconcile(db, api, intent)
    before = support.snapshot(db, intent)
    origin.require(
        before["phase"] is None and before["token"] is None,
        "Native origin unresolved provider handoff",
    )
    db.commit()
    inventory = seal(api, intent, receipt, observed_support, before["writers"])
    _, _, current = _locked(db, cleanup_id)
    origin.require(
        current == work and support.snapshot(db, intent) == before,
        "Native origin inventory handoff changed",
    )
    if work.inventory is not None:
        _evidence_matches(db, work, "sealed", work.inventory.model_dump(mode="json"))
        if work.inventory != inventory:
            _allow_late_bootstrap(work, inventory)
            _evidence(
                db,
                work,
                "sealed:" + digest(inventory.model_dump(mode="json"))[:32],
                inventory.model_dump(mode="json"),
            )
            work = _save(db, work, inventory=inventory)
    else:
        _evidence(db, work, "sealed", inventory.model_dump(mode="json"))
        work = _save(db, work, phase="sealed", inventory=inventory)
    db.commit()
    # Reread all original identities after seal publication, outside locks.
    origin.require(
        seal(api, intent, receipt, observed_support, before["writers"]) == inventory,
        "Native origin inventory changed before namespace deletion",
    )
    ns = support.bracket(api, intent, receipt)
    rv = ns["metadata"].get("resourceVersion")
    origin.require(rv, "Native origin namespace resourceVersion unavailable")
    _, _, current = _locked(db, cleanup_id)
    origin.require(
        current == work and support.snapshot(db, intent) == before,
        "Native origin delete handoff changed",
    )
    delete_payload = _delete_payload(work, rv)
    phase = (
        "namespace_delete_requested"
        if work.phase != "namespace_delete_requested"
        else "namespace_delete_requested:" + digest(delete_payload)[:12]
    )
    _evidence(db, work, phase, delete_payload)
    work = _save(
        db, work, phase="namespace_delete_requested", delete_resource_version=rv
    )
    db.commit()
    api.delete_namespace(intent.namespace, receipt.namespace_uid, rv)
    return work


def _allow_late_bootstrap(work, inventory):
    old = work.inventory
    old_objects = {value.uid: value for value in old.objects}
    new_objects = {value.uid: value for value in inventory.objects}
    origin.require(
        work.phase == "sealed"
        and old.namespace == inventory.namespace
        and old.support_digest == inventory.support_digest
        and old.writers_digest == inventory.writers_digest
        and all(new_objects.get(uid) == value for uid, value in old_objects.items()),
        "Native origin sealed inventory changed; cleanup held",
    )
    # Additional objects have just passed the complete inventory bootstrap
    # validator. Pin them in a new immutable seal; never replace an original UID.


def finish(db, cleanup_id):
    from app.models.native_deployment_origin import NativeDeploymentOriginActive
    from app.services.quota_cleanup import _finish_project_record

    project, _, work = _locked(db, cleanup_id)
    origin.require(
        work.phase == "absent", "Native origin verified namespace absence required"
    )
    evidence = (
        db.query(NativeOriginNamespaceEvidence)
        .filter_by(cleanup_id=cleanup_id, phase="absent")
        .one_or_none()
    )
    origin.require(
        evidence is not None, "Native origin namespace absence receipt missing"
    )
    _verify_delete_evidence(db, work)
    origin.require(
        writers._checked(evidence) == _absence_payload(work),
        "Native origin namespace absence receipt changed",
    )
    _save(db, work, phase="finalized")
    pointer = db.get(NativeDeploymentOriginActive, str(work.project_id))
    db.delete(pointer)
    _finish_project_record(db, project, work.mode)


def reconcile(cleanup_id):
    from app.core.database import SessionLocal
    from app.models.provider import Provider
    from app.services.providers.kubevirt import _get_k8s_clients

    with SessionLocal() as db:
        if _load(db.get(NativeOriginNamespaceCleanup, cleanup_id)).phase == "finalized":
            return
        _, intent, work = _locked(db, cleanup_id)
        provider = db.get(Provider, str(intent.provider_id))
        db.commit()
        _, _, client = _get_k8s_clients(provider)
        try:
            work = advance(db, InventoryClient(client), cleanup_id)
            if work.phase == "namespace_delete_requested":
                work = advance(db, InventoryClient(client), cleanup_id)
            if work.phase == "absent":
                finish(db, cleanup_id)
        except Exception:
            db.rollback()
            _, _, work = _locked(db, cleanup_id)
            _save(
                db,
                work,
                last_error="Original namespace or complete inventory evidence unavailable; retry cleanup",
            )
            db.commit()
            raise
        finally:
            client.close()
