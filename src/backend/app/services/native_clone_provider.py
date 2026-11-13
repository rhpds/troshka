"""Collector-only original-journal CSI clone observation, with no provider writes."""

from datetime import UTC, datetime
from types import SimpleNamespace

from app.schemas.kubevirt_storage import ImageKey, VolumeBinding
from app.schemas.native_clone_lineage import CloneLineage, CloneObservation
from app.services.native_cache_provider import CacheVerificationAdapter
from app.services.native_cache_verification import (
    _bound_pvc,
    _direct_dv,
    require,
    timestamp,
)


class CloneVerificationAdapter(CacheVerificationAdapter):
    def __init__(self, sessions, kube, backend, source_client, profile):
        super().__init__(kube, backend, source_client, profile)
        self.sessions = sessions

    def _objects(self, namespace, name):
        stub = SimpleNamespace(namespace=namespace, name=name)
        dv, pvc = self._dv(stub), self._pvc(stub)
        pv = self._dict(
            self.kube.core.read_persistent_volume(
                pvc["spec"]["volumeName"], _request_timeout=self.kube._timeout()
            )
        )
        return dv, pvc, pv

    def _image(self, pv):
        csi = pv["spec"]["csi"]
        return ImageKey.model_validate(
            self._value(
                self.backend.resolve_csi(
                    csi["driver"],
                    csi["volumeHandle"],
                    expected_request=pv["metadata"]["name"],
                    volume_attributes=csi.get("volumeAttributes", {}),
                )
            )
        )

    def _binding(self, intent, creation, dv, pvc, pv, image):
        require(
            dv["metadata"]["uid"] == creation.dv_uid
            and dv.get("status", {}).get("phase") == "Succeeded",
            "original clone incomplete or replaced",
        )
        verify_clone_binding(intent, creation, dv, pvc, pv)
        csi = pv["spec"]["csi"]
        return VolumeBinding(
            root=intent.root,
            pvc_uid=pvc["metadata"]["uid"],
            pv_uid=pv["metadata"]["uid"],
            csi_driver=csi["driver"],
            volume_handle=csi["volumeHandle"],
            image_key=image,
        )

    def observe_clone(self, intent, creation):
        from app.models.kubevirt_cache_birth import NativeCacheBirth
        from app.models.quota import QuotaReservation
        from app.services import native_managed_deploy as deploy
        from app.services.kubevirt_storage_collector import _parent
        from app.services.native_cache_birth import validated_receipt

        with self.sessions() as db:
            birth, receipt = validated_receipt(
                db, intent.cache_reference.operation_id, intent.cache_reference.digest
            )
            response = db.get(NativeCacheBirth, str(birth.operation_id)).create_response
            row = (
                db.query(QuotaReservation)
                .filter_by(operation_id=intent.operation_id)
                .one()
            )
            target = row.dispatch_metadata["native_deploy"]
        self._identities(birth)
        source_dv, source_pvc, source_pv = self._objects(birth.namespace, birth.name)
        _direct_dv(birth, response, source_dv)
        _bound_pvc(birth, response, source_pvc, source_pv)
        require(
            (
                source_pvc["metadata"]["uid"],
                source_pv["metadata"]["uid"],
                source_pv["spec"]["csi"]["volumeHandle"],
            )
            == (receipt.pvc_uid, receipt.pv_uid, receipt.volume_handle),
            "sealed source binding replaced",
        )
        require(
            self._image(source_pv) == receipt.image_key, "source CSI identity changed"
        )
        source_info = self._value(self.backend.image_info(receipt.image_key))
        require(
            source_info.get("id") == receipt.image_key.image_id
            and not source_info.get("parent"),
            "sealed source reparented or replaced",
        )
        api = deploy.power.helpers().PowerClient(self.kube.api)
        deploy.observe(api, target, intent.operation_id)
        dv, pvc, pv = self._objects(intent.root.namespace, intent.claim_name)
        require(
            dv["spec"]["source"]
            == {"pvc": {"name": birth.name, "namespace": birth.namespace}},
            "clone source changed",
        )
        image = self._image(pv)
        binding = self._binding(intent, creation, dv, pvc, pv, image)
        pools = self._value(self.backend.pool_identity())
        info = self._value(self.backend.image_info(image))
        require(info.get("id") == image.image_id, "clone image replaced")
        parent = _parent(info, image, pools, self.backend)
        if parent is None:
            raise ValueError("copy fallback is not a COW clone")
        intermediate = parent.image_key
        middle = self._value(self.backend.image_info(intermediate))
        require(
            middle.get("id") == intermediate.image_id, "private intermediate replaced"
        )
        golden = _parent(middle, intermediate, pools, self.backend)
        if golden is None or golden.image_key != receipt.image_key:
            raise ValueError("unsupported clone source ancestry")
        proof = CloneLineage(
            lineage_id=intent.lineage_id,
            operation_id=intent.operation_id,
            claim_name=intent.claim_name,
            domain_id=intent.domain_id,
            target_id=intent.target_id,
            cache_reference=intent.cache_reference,
            binding=binding,
            source=intent.source,
            intermediate=intermediate,
            head_parent=parent,
            intermediate_parent=golden,
            original_dv_uid=creation.dv_uid,
            verified_at=datetime.now(UTC),
        )
        self._identities(birth)
        deploy.observe(api, target, intent.operation_id)
        return CloneObservation(target=intent.target, lineage=proof)


def verify_clone_binding(intent, creation, dv, pvc, pv):
    """New dynamic clone may rebind its PV from CSI's temporary request PVC."""
    from uuid import UUID

    from app.services.kubevirt_storage_enrollment import utc

    meta, spec, pmeta, pspec = pvc["metadata"], pvc["spec"], pv["metadata"], pv["spec"]
    UUID(pmeta["name"].removeprefix("pvc-"))
    require(
        pmeta["name"].startswith("pvc-") and not pmeta.get("deletionTimestamp"),
        "clone PV is not dynamically provisioned",
    )
    require(
        all(
            utc(timestamp(v["metadata"]))
            >= utc(intent.created_at.replace(microsecond=0))
            for v in (dv, pvc, pv)
        )
        and not meta.get("deletionTimestamp")
        and pvc.get("status", {}).get("phase") == "Bound"
        and meta["name"] == intent.claim_name
        and meta["namespace"] == intent.root.namespace
        and spec.get("volumeName") == pmeta["name"]
        and pspec.get("storageClassName") == intent.target.storage_class_name
        and pmeta.get("annotations", {}).get("pv.kubernetes.io/provisioned-by")
        == intent.target.provisioner,
        "new dynamic clone provenance changed",
    )
    claim = pspec.get("claimRef", {})
    require(
        all(claim.get(k) == meta[k] for k in ("uid", "name", "namespace")),
        "clone PV claim changed",
    )
    owners = [r for r in meta.get("ownerReferences", []) if r.get("controller")]
    require(
        len(owners) == 1
        and owners[0].get("kind") == "DataVolume"
        and owners[0].get("uid") == creation.dv_uid
        and pspec.get("csi", {}).get("driver") == intent.target.provisioner,
        "clone ownership or CSI changed",
    )
