"""Full namespace discovery with immutable Kubernetes and CSI binding proof."""

import hashlib
import json
import os
import stat
import time
from datetime import UTC, datetime

import yaml
from kubernetes.utils.quantity import parse_quantity

from app.schemas.kubevirt_storage import (
    Blocker,
    CacheBinding,
    CSISnapshotEvidence,
    NativeInventory,
    VolumeBinding,
)


class BindingError(ValueError):
    pass


def inventory_digest(inventory):
    fields = (
        "project_roots",
        "volume_bindings",
        "cache_bindings",
        "retained_bindings",
        "blockers",
    )
    if inventory.snapshot_evidence:
        fields += ("snapshot_evidence",)
    data = {
        name: sorted(
            (v.model_dump(mode="json") for v in getattr(inventory, name)),
            key=lambda v: json.dumps(v, sort_keys=True),
        )
        for name in fields
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _proof_root(kube, root, retained):
    namespace = kube.namespace(root.namespace)
    if namespace.get("metadata", {}).get("uid") != root.namespace_uid:
        return "namespace_identity_mismatch"
    projects = kube.projects(root.namespace)
    exact = [
        p
        for p in projects
        if p.get("spec", {}).get("projectId") == str(root.project_id)
        and p.get("metadata", {}).get("uid") == root.project_cr_uid
    ]
    if not projects and any(b.root == root for b in retained):
        return None
    if len(exact) != 1 or len(projects) != 1:
        return "project_identity_mismatch"
    if (
        root.deployment_generation is not None
        and exact[0].get("metadata", {}).get("generation") != root.deployment_generation
    ):
        return "project_generation_mismatch"
    return None


def _pvc_binding(kube, pvc, pv_by_name, namespace):
    meta, spec = pvc.get("metadata", {}), pvc.get("spec", {})
    if meta.get("namespace") != namespace:
        raise BindingError("pvc_namespace_mismatch")
    if not spec.get("volumeName") or pvc.get("status", {}).get("phase") != "Bound":
        raise BindingError("pending_pvc")
    pv = pv_by_name.get(spec["volumeName"], {})
    claim = pv.get("spec", {}).get("claimRef", {})
    if claim.get("uid") != meta.get("uid") or claim.get("namespace") != namespace:
        raise BindingError("claim_identity_mismatch")
    csi = pv.get("spec", {}).get("csi", {})
    pv_meta = pv.get("metadata", {})
    request = getattr(kube, "csi_requests", {}).get(pv_meta.get("uid"))
    if request is None and pv_meta.get("annotations", {}).get(
        "pv.kubernetes.io/provisioned-by"
    ) == csi.get("driver"):
        request = pv_meta.get("name")
    resolved = kube.resolve_csi(
        csi.get("driver", ""),
        csi.get("volumeHandle", ""),
        expected_request=request,
        volume_attributes=csi.get("volumeAttributes", {}),
    )
    if resolved.status != "complete":
        raise ValueError("csi_identity_" + resolved.status)
    result = dict(
        pvc_uid=meta["uid"],
        pv_uid=pv["metadata"]["uid"],
        csi_driver=csi["driver"],
        volume_handle=csi["volumeHandle"],
        image_key=resolved.value,
    )
    quantity = pvc.get("status", {}).get("capacity", {}).get("storage")
    size = parse_quantity(quantity) if quantity is not None else None
    if size is not None and (size < 0 or size != int(size) or size > 2**63 - 1):
        raise ValueError("invalid_provisioned_bytes")
    return result, int(size) if size is not None else None


def _handle_digest(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("invalid CSI handle")
    return hashlib.sha256(value.encode()).hexdigest()


def _snapshot_record(root, snapshot, content, content_name):
    spec = content.get("spec", {})
    source, ref = spec.get("source", {}), spec.get("volumeSnapshotRef", {})
    return CSISnapshotEvidence(
        root=root,
        snapshot_uid=snapshot.get("metadata", {}).get("uid"),
        snapshot_name=snapshot.get("metadata", {}).get("name"),
        content_name=content_name,
        content_uid=content.get("metadata", {}).get("uid"),
        source_pvc_name=snapshot.get("spec", {})
        .get("source", {})
        .get("persistentVolumeClaimName"),
        csi_driver=content.get("spec", {}).get("driver"),
        ready_to_use=snapshot.get("status", {}).get("readyToUse") is True,
        snapshot_handle_sha256=_handle_digest(
            content.get("status", {}).get("snapshotHandle")
        ),
        source_snapshot_handle_sha256=_handle_digest(source.get("snapshotHandle")),
        source_volume_handle_sha256=_handle_digest(source.get("volumeHandle")),
        reference_snapshot_uid=ref.get("uid"),
        reference_snapshot_name=ref.get("name"),
        reference_namespace=ref.get("namespace"),
        deletion_policy=spec.get("deletionPolicy"),
    )


def _snapshot_discovery(kube, root, contents):
    records, errors, referenced = [], [], set()
    for snapshot in kube.snapshots(root.namespace):
        status, meta = snapshot.get("status", {}), snapshot.get("metadata", {})
        content_name = status.get("boundVolumeSnapshotContentName")
        content = contents.get(content_name, {})
        referenced.add(content_name)
        records.append(_snapshot_record(root, snapshot, content, content_name))
        ref = content.get("spec", {}).get("volumeSnapshotRef", {})
        code = "csi_snapshot_backend_unresolved"
        if (
            not status.get("readyToUse")
            or ref.get("uid") != meta.get("uid")
            or ref.get("namespace") != root.namespace
        ):
            code = "snapshot_identity_unresolved"
        # Ready references are insufficient without a supported backend snapshot resolver.
        errors.append(Blocker(code=code, project_id=root.project_id))
    for name, content in contents.items():
        ref = content.get("spec", {}).get("volumeSnapshotRef", {})
        if name in referenced or ref.get("namespace") != root.namespace:
            continue
        # The name selects relevant uncertainty, not ownership of a namespace
        # generation or a backend image. Keep live and reference UIDs separate.
        records.append(_snapshot_record(root, {}, content, name))
        errors.append(
            Blocker(code="snapshot_identity_unresolved", project_id=root.project_id)
        )
    return records, errors


def _expired(kube):
    deadline = getattr(kube, "scan_deadline", None)
    return deadline is not None and time.monotonic() >= deadline


def _project_pvcs(kube, root, pvs):
    bindings, blockers = [], []
    for pvc in kube.pvcs(root.namespace):
        if _expired(kube):
            blockers.append(Blocker(code="scan_limit"))
            break
        try:
            fields, size = _pvc_binding(kube, pvc, pvs, root.namespace)
            owners = [
                o["uid"] for o in pvc.get("metadata", {}).get("ownerReferences", [])
            ]
            bindings.append(
                VolumeBinding(
                    root=root,
                    **fields,
                    provisioned_bytes=size,
                    owner_reference_uids=owners,
                )
            )
        except BindingError as error:
            blockers.append(Blocker(code=error.args[0], project_id=root.project_id))
        except (ValueError, KeyError, TypeError):
            blockers.append(
                Blocker(code="pvc_binding_unresolved", project_id=root.project_id)
            )
    return bindings, blockers


def _project_discovery(kube, root, retained, cluster, pvs, contents):
    try:
        problem = (
            "cluster_identity_mismatch"
            if root.cluster_key != cluster
            else _proof_root(kube, root, retained)
        )
        if problem:
            return [], [], [Blocker(code=problem, project_id=root.project_id)]
        snapshots, snapshot_errors = _snapshot_discovery(kube, root, contents)
        bindings, blockers = _project_pvcs(kube, root, pvs)
        return bindings, snapshots, blockers + snapshot_errors
    except Exception:
        return (
            [],
            [],
            [Blocker(code="project_discovery_unavailable", project_id=root.project_id)],
        )


def _retained_cache(kube, enrolled, cache):
    result = []
    current = {b.pvc_uid for b in cache}
    for previous in getattr(kube, "retained_cache_bindings", []):
        identity = {
            n: getattr(previous, n)
            for n in ("cluster_key", "namespace", "namespace_uid")
        }
        if identity == enrolled and previous.pvc_uid not in current:
            result.append(previous)
    return result


def _cache_discovery(kube, cluster, pvs):
    cache, blockers = [], []
    for enrolled in getattr(kube, "cache_roots", []):
        if _expired(kube):
            blockers.append(Blocker(code="scan_limit"))
            break
        try:
            namespace_uid = kube.namespace(enrolled["namespace"])["metadata"]["uid"]
            if (
                cluster != enrolled["cluster_key"]
                or namespace_uid != enrolled["namespace_uid"]
            ):
                raise ValueError("cache identity")
            records = _cache_pvcs(kube, enrolled, pvs)
            cache.extend(records + _retained_cache(kube, enrolled, records))
        except Exception:
            blockers.append(Blocker(code="cache_discovery_unavailable"))
    return cache, blockers


def _cache_pvcs(kube, enrolled, pvs):
    records = []
    for pvc in kube.pvcs(enrolled["namespace"]):
        if _expired(kube):
            raise TimeoutError("scan limit")
        fields, _ = _pvc_binding(kube, pvc, pvs, enrolled["namespace"])
        records.append(CacheBinding(**enrolled, **fields))
    return records


def _retained_conflicts(retained, roots, bindings):
    blockers = []
    for old in retained:
        changed = any(
            b.pvc_uid == old.pvc_uid
            and (
                b.image_key != old.image_key
                or b.pv_uid != old.pv_uid
                or b.volume_handle != old.volume_handle
            )
            for b in bindings
        )
        if old.root not in roots or changed:
            blockers.append(
                Blocker(
                    code="retained_identity_conflict", project_id=old.root.project_id
                )
            )
    return blockers


def discover_inventory(kube_reader, retained_bindings, roots):
    """Discover full enrolled namespaces; retained records contain identity only."""
    bindings, snapshots, blockers, roots = [], [], [], list(roots)
    try:
        cluster = kube_reader.cluster_identity()
        pvs = {p["metadata"]["name"]: p for p in kube_reader.pvs()}
        contents = {c["metadata"]["name"]: c for c in kube_reader.snapshot_contents()}
    except Exception:
        return NativeInventory(
            project_roots=roots,
            retained_bindings=retained_bindings,
            blockers=[Blocker(code="kubernetes_discovery_unavailable")],
            collected_at=datetime.now(UTC),
        )
    seen_projects, seen_namespaces = set(), set()
    for root in roots:
        if _expired(kube_reader):
            blockers.append(Blocker(code="scan_limit"))
            break
        namespace_key = (root.cluster_key, root.namespace_uid)
        if root.project_id in seen_projects or namespace_key in seen_namespaces:
            blockers.append(
                Blocker(code="duplicate_project_root", project_id=root.project_id)
            )
            continue
        seen_projects.add(root.project_id)
        seen_namespaces.add(namespace_key)
        found, snapshot_records, problems = _project_discovery(
            kube_reader, root, retained_bindings, cluster, pvs, contents
        )
        bindings.extend(found)
        snapshots.extend(snapshot_records)
        blockers.extend(problems)
    cache, cache_errors = _cache_discovery(kube_reader, cluster, pvs)
    blockers.extend(
        cache_errors + _retained_conflicts(retained_bindings, roots, bindings)
    )
    result = NativeInventory(
        collector_id=getattr(kube_reader, "collector_id", "standalone"),
        project_roots=roots,
        volume_bindings=bindings,
        cache_bindings=cache,
        retained_bindings=retained_bindings,
        snapshot_evidence=snapshots,
        blockers=blockers,
        collected_at=getattr(kube_reader, "scan_started_at", datetime.now(UTC)),
    )
    return result.model_copy(update={"inventory_digest": inventory_digest(result)})


def _static_kubeconfig(data):
    if not isinstance(data, dict) or not isinstance(data.get("users"), list):
        raise ValueError("static operator kubeconfig required")
    for entry in data["users"]:
        user = entry.get("user") if isinstance(entry, dict) else None
        if not isinstance(user, dict) or {"exec", "auth-provider"}.intersection(user):
            raise ValueError("static operator kubeconfig required")
    return data


def read_static_kubeconfig(path):
    """Reject executable/refreshing auth before any Kubernetes loader or process.

    The operator must keep this private file unchanged for subsequent oc reads.
    The Python loader receives these same parsed bytes rather than reopening it.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
        ):
            raise ValueError("operator-owned regular kubeconfig required")
        raw = stream.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("kubeconfig exceeds bound")
    try:
        return _static_kubeconfig(yaml.safe_load(raw))
    except yaml.YAMLError:
        raise ValueError("invalid static operator kubeconfig") from None


class KubernetesReader:
    """Standalone Kubernetes get/list adapter; never imported by application routes."""

    def __init__(
        self,
        kubeconfig,
        rbd_reader,
        *,
        collector_id,
        cache_roots,
        scan_deadline=None,
        csi_requests=None,
        max_objects=10000,
        kubeconfig_data=None,
    ):
        from kubernetes import client
        from kubernetes.config.kube_config import KubeConfigLoader

        configuration = client.Configuration()
        data = (
            read_static_kubeconfig(kubeconfig)
            if kubeconfig_data is None
            else _static_kubeconfig(kubeconfig_data)
        )
        KubeConfigLoader(
            config_dict=data,
            config_base_path=os.path.dirname(os.path.abspath(kubeconfig)),
        ).load_and_set(configuration)
        self.api = client.ApiClient(configuration)
        # Configure urllib3's supported pool option before the first request.
        # Kubernetes' generated Configuration incorrectly infers retries as None.
        self.api.rest_client.pool_manager.connection_pool_kw["retries"] = 0
        self.core = client.CoreV1Api(self.api)
        self.custom = client.CustomObjectsApi(self.api)
        self.rbd_reader, self.collector_id, self.cache_roots = (
            rbd_reader,
            collector_id,
            cache_roots,
        )
        self.scan_deadline, self.csi_requests = scan_deadline, csi_requests or {}
        self.max_objects = max_objects

    def _timeout(self):
        remaining = self.scan_deadline - time.monotonic() if self.scan_deadline else 15
        if remaining <= 0:
            raise TimeoutError("scan limit")
        return min(15, remaining)

    def _dict(self, result):
        serialized = self.api.sanitize_for_serialization(result)
        if not isinstance(serialized, dict):
            raise ValueError("invalid Kubernetes response")
        return serialized

    def _items(self, result):
        if len(result.get("items", [])) > self.max_objects or result.get(
            "metadata", {}
        ).get("continue"):
            raise ValueError("discovery object limit")
        return result["items"]

    def cluster_identity(self):
        return self.namespace("kube-system")["metadata"]["uid"]

    def namespace(self, name):
        return self._dict(
            self.core.read_namespace(name, _request_timeout=self._timeout())
        )

    def pod(self, namespace, name):
        return self._dict(
            self.core.read_namespaced_pod(
                name, namespace, _request_timeout=self._timeout()
            )
        )

    def pvs(self):
        return self._items(
            self._dict(
                self.core.list_persistent_volume(
                    limit=self.max_objects + 1, _request_timeout=self._timeout()
                )
            )
        )

    def pvcs(self, namespace):
        return self._items(
            self._dict(
                self.core.list_namespaced_persistent_volume_claim(
                    namespace,
                    limit=self.max_objects + 1,
                    _request_timeout=self._timeout(),
                )
            )
        )

    def projects(self, namespace):
        return self._items(
            self.custom.list_namespaced_custom_object(
                "troshka.redhat.com",
                "v1alpha1",
                namespace,
                "troshkaprojects",
                limit=self.max_objects + 1,
                _request_timeout=self._timeout(),
            )
        )

    def snapshots(self, namespace):
        return self._items(
            self.custom.list_namespaced_custom_object(
                "snapshot.storage.k8s.io",
                "v1",
                namespace,
                "volumesnapshots",
                limit=self.max_objects + 1,
                _request_timeout=self._timeout(),
            )
        )

    def snapshot_contents(self):
        return self._items(
            self.custom.list_cluster_custom_object(
                "snapshot.storage.k8s.io",
                "v1",
                "volumesnapshotcontents",
                limit=self.max_objects + 1,
                _request_timeout=self._timeout(),
            )
        )

    def resolve_csi(
        self, driver, handle, *, expected_request=None, volume_attributes=None
    ):
        return self.rbd_reader.resolve_csi(
            driver,
            handle,
            expected_request=expected_request,
            volume_attributes=volume_attributes,
        )
