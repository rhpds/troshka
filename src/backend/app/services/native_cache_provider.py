"""Bounded provider I/O for the protected direct-import worker entrypoints.

Uses the existing independent Kubernetes/ODF readers and a configured S3 client.
It creates exactly one DV. It never creates namespaces/secrets, adopts objects,
deletes a mismatch, retries a write, or accepts report-supplied verification.
"""

from datetime import UTC, datetime

from app.schemas.kubevirt_cache_birth import USE_POPULATOR, CacheSource
from app.services.native_cache_verification import require
from app.services.native_initialization import supported_blank_storage


class CacheProviderAdapter:
    def __init__(self, kube, backend, source_client, installation_profile):
        self.kube, self.backend = kube, backend
        self.source_client, self.profile = source_client, installation_profile
        # Kubernetes generated clients can otherwise retry ambiguous POSTs.
        kube.api.rest_client.pool_manager.connection_pool_kw["retries"] = 0

    def _value(self, result):
        require(result.status == "complete", "incomplete independent backend read")
        require(
            0 <= (datetime.now(UTC) - result.collected_at).total_seconds() <= 60,
            "stale independent backend read",
        )
        return result.value

    def _dict(self, value):
        return self.kube.api.sanitize_for_serialization(value)

    def _source(self, intent):
        source = intent.source
        require(
            self.source_client.meta.endpoint_url.rstrip("/")
            == source.endpoint.rstrip("/"),
            "S4 endpoint changed",
        )
        head = self.source_client.head_object(Bucket=source.bucket, Key=source.key)
        current = CacheSource(
            endpoint=source.endpoint,
            bucket=source.bucket,
            key=source.key,
            etag=head["ETag"],
            size_bytes=head["ContentLength"],
            last_modified=head["LastModified"],
            version_id=head.get("VersionId"),
            content_sha256=head.get("Metadata", {}).get("sha256"),
            immutability_policy=source.immutability_policy,
        )
        require(current == source, "frozen S4 source identity changed")

    def _identities(self, intent):
        from app.services.native_initialization_observer import ProductionObserver

        require(
            self.profile.target == intent.target, "independent installation pin changed"
        )
        target = intent.target
        require(
            target.provisioner == "openshift-storage.rbd.csi.ceph.com"
            and target.csi_installation is not None
            and target.storage_class_options is not None
            and target.ceph_version.startswith("ceph version 19.2.1")
            and supported_blank_storage(target),
            "unsupported fresh provisioning profile",
        )
        observed = ProductionObserver().observe(self.profile, self.kube, self.backend)
        require(observed.target == target, "installed CSI/storage class changed")
        require(
            self.kube.cluster_identity() == target.cluster_key
            and self.kube.namespace(intent.namespace)["metadata"]["uid"]
            == intent.namespace_uid,
            "cache namespace or cluster changed",
        )
        self._source(intent)

    def _dv(self, intent):
        return self.kube.custom.get_namespaced_custom_object(
            "cdi.kubevirt.io",
            "v1beta1",
            intent.namespace,
            "datavolumes",
            intent.name,
            _request_timeout=self.kube._timeout(),
        )

    def _pvc(self, intent):
        return self._dict(
            self.kube.core.read_namespaced_persistent_volume_claim(
                intent.name, intent.namespace, _request_timeout=self.kube._timeout()
            )
        )

    def preflight(self, intent):
        from kubernetes.client.exceptions import ApiException

        self._identities(intent)
        for read in (self._dv, self._pvc):
            try:
                read(intent)
            except ApiException as error:
                if error.status != 404:
                    raise
            else:
                raise ValueError("preexisting managed cache target; no adoption")

    def create(self, intent):
        return self.kube.custom.create_namespaced_custom_object(
            "cdi.kubevirt.io",
            "v1beta1",
            intent.namespace,
            "datavolumes",
            {
                "apiVersion": "cdi.kubevirt.io/v1beta1",
                "kind": "DataVolume",
                "metadata": {
                    "name": intent.name,
                    "namespace": intent.namespace,
                    "annotations": {USE_POPULATOR: "false"},
                },
                "spec": intent.spec,
            },
            _request_timeout=self.kube._timeout(),
        )

    def _pods(self, namespace):
        value = self._dict(
            self.kube.core.list_namespaced_pod(
                namespace, limit=10001, _request_timeout=self.kube._timeout()
            )
        )
        require(
            not value.get("metadata", {}).get("continue")
            and len(value["items"]) <= 10000,
            "cache writer inventory incomplete",
        )
        return value["items"]

    def observe(self, intent, response):
        self._identities(intent)
        dv, pvc = self._dv(intent), self._pvc(intent)
        pv = self._dict(
            self.kube.core.read_persistent_volume(
                pvc["spec"]["volumeName"], _request_timeout=self.kube._timeout()
            )
        )
        csi = pv["spec"]["csi"]
        # Existing public CSI journal resolution binds this exact PV request name
        # and volume handle to immutable image ID; omitted parent fields alone
        # never establish birth authority.
        image = self._value(
            self.backend.resolve_csi(
                csi["driver"],
                csi["volumeHandle"],
                expected_request=pv["metadata"]["name"],
                volume_attributes=csi.get("volumeAttributes", {}),
            )
        )
        info = self._value(self.backend.image_info(image))
        snapshots = self._value(self.backend.image_snapshots(image))
        result = {
            "dv": dv,
            "pvc": pvc,
            "pv": pv,
            "pods": self._pods(intent.namespace),
            "image_key": image.model_dump(mode="json"),
            "image_info": info,
            "snapshots": snapshots,
        }
        self._identities(intent)
        return result
