"""Independent object-chain checks for a newly created direct import."""

from datetime import UTC, datetime

from app.schemas.kubevirt_cache_birth import (
    CacheBirthReceipt,
    validate_import_annotations,
)
from app.schemas.kubevirt_storage import ImageKey
from app.services.kubevirt_storage_enrollment import digest


def require(condition, message):
    if not condition:
        raise ValueError(message)


def timestamp(meta):
    return datetime.fromisoformat(meta["creationTimestamp"].replace("Z", "+00:00"))


def _direct_dv(intent, response, dv):
    meta = dv["metadata"]
    validate_import_annotations(meta)
    require(
        meta.get("uid") == response["metadata"]["uid"]
        and meta.get("name") == intent.name
        and meta.get("namespace") == intent.namespace
        and not meta.get("deletionTimestamp")
        and dv.get("spec") == intent.spec
        and dv.get("status", {}).get("phase") == "Succeeded",
        "direct import incomplete or replaced",
    )


def _bound_pvc(intent, response, pvc, pv):
    meta, spec = pvc["metadata"], pvc["spec"]
    require(
        meta.get("name") == intent.name
        and meta.get("namespace") == intent.namespace
        and not meta.get("deletionTimestamp")
        and pvc.get("status", {}).get("phase") == "Bound",
        "cache PVC incomplete or replaced",
    )
    owners = [r for r in meta.get("ownerReferences", []) if r.get("controller")]
    require(
        len(owners) == 1
        and owners[0].get("kind") == "DataVolume"
        and owners[0].get("uid") == response["metadata"]["uid"],
        "cache PVC not owned by original DV",
    )
    require(
        not any(k in spec for k in ("dataSource", "dataSourceRef", "selector")),
        "adopted PVC",
    )
    require(
        all(spec.get(k) == v for k, v in intent.spec["pvc"].items()),
        "PVC provisioning changed",
    )
    if getattr(intent, "managed_authority", None) is not None:
        from kubernetes.utils.quantity import parse_quantity

        require(
            parse_quantity(pvc["status"].get("capacity", {}).get("storage", "0"))
            == parse_quantity(intent.spec["pvc"]["resources"]["requests"]["storage"]),
            "actual managed source capacity changed",
        )
    pmeta, pspec = pv["metadata"], pv["spec"]
    require(
        spec.get("volumeName") == pmeta.get("name")
        and not pmeta.get("deletionTimestamp")
        and pmeta.get("name") == "pvc-" + meta["uid"]
        and pspec.get("storageClassName") == intent.target.storage_class_name,
        "cache PV identity changed",
    )
    claim = pspec.get("claimRef", {})
    require(
        claim.get("uid") == meta.get("uid")
        and claim.get("name") == intent.name
        and claim.get("namespace") == intent.namespace,
        "PV claim binding changed",
    )
    csi = pspec.get("csi", {})
    require(
        csi.get("driver") == intent.target.provisioner
        and pmeta.get("annotations", {}).get("pv.kubernetes.io/provisioned-by")
        == intent.target.provisioner,
        "fresh CSI provisioning evidence missing",
    )


def _writer_free(intent, pods):
    # Even terminal Pods are rejected until removed: their spec still references
    # the source and cannot be reused as a writable helper under this contract.
    for pod in pods:
        volumes = pod.get("spec", {}).get("volumes", [])
        require(
            not any(
                v.get("persistentVolumeClaim", {}).get("claimName") == intent.name
                for v in volumes
            ),
            "cache import writer remains",
        )


def verify_observation(intent, response, value):
    dv, pvc, pv = value["dv"], value["pvc"], value["pv"]
    _direct_dv(intent, response, dv)
    _bound_pvc(intent, response, pvc, pv)
    _writer_free(intent, value["pods"])
    image = ImageKey.model_validate(value["image_key"])
    require(
        (image.ceph_fsid, image.pool_id, image.rbd_namespace)
        == (
            intent.target.ceph_fsid,
            intent.target.pool_id,
            intent.target.rbd_namespace,
        ),
        "backend image outside protected target",
    )
    require(
        value["image_info"].get("id") == image.image_id
        and not value["image_info"].get("parent")
        and value["snapshots"] == [],
        "unsupported initial backend ancestry",
    )
    times = [timestamp(o["metadata"]) for o in (dv, pvc, pv)]
    # Kubernetes creationTimestamp has second precision. This is a consistency
    # check; no-adoption and CSI creation semantics establish freshness.
    require(
        all(t >= intent.created_at.replace(microsecond=0) for t in times),
        "preexisting storage object",
    )
    csi = pv["spec"]["csi"]
    return CacheBirthReceipt(
        operation_id=intent.operation_id,
        intent_digest=digest(intent),
        dv_uid=dv["metadata"]["uid"],
        pvc_uid=pvc["metadata"]["uid"],
        pv_uid=pv["metadata"]["uid"],
        csi_driver=csi["driver"],
        volume_handle=csi["volumeHandle"],
        image_key=image,
        original_snapshots=[],
        dv_created_at=times[0],
        pvc_created_at=times[1],
        pv_created_at=times[2],
        verified_at=datetime.now(UTC),
        verification_digest=digest(value),
    )


def stable_receipt(receipt):
    return receipt.model_dump(
        mode="json", exclude={"verified_at", "verification_digest"}
    )
