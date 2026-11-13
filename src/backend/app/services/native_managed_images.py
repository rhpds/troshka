"""Frozen authorized central images for the existing native deployment operation."""

import math
from copy import deepcopy
from uuid import uuid4

from app.models.library import LibraryItem
from app.models.provider import Provider
from app.schemas.kubevirt_cache_birth import CacheSource
from app.schemas.quota import AccountingUnavailable
from app.services.kubevirt_storage_enrollment import digest
from app.services.library_access import usable_items

GIB = 1024**3


def source_client(db, policy):
    import boto3
    from botocore.config import Config

    provider = db.get(Provider, str(policy.provider_id))
    if provider is None or provider.type != "s3_readonly" or provider.state != "active":
        raise AccountingUnavailable("Approved central source provider unavailable")
    creds = provider.get_credentials()
    if creds.get("bucket", "troshka-gold-images") != policy.bucket:
        raise AccountingUnavailable("Approved central source bucket changed")
    return boto3.client(
        "s3",
        endpoint_url=creds.get("endpoint_url"),
        region_name=creds.get("region") or provider.default_region or "us-east-1",
        aws_access_key_id=creds.get("access_key_id", ""),
        aws_secret_access_key=creds.get("secret_access_key", ""),
        config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 0}),
    )


def authorized_item(db, owner, item_id, policy):
    item = (
        usable_items(db, owner)
        .filter(LibraryItem.id == item_id)
        .populate_existing()
        .one_or_none()
    )
    if (
        item is None
        or item.source != "central"
        or item.format != "qcow2"
        or item.type != "disk"
        or item.state != "ready"
        or not item.s3_key
        or not item.s3_key.startswith(policy.key_prefix)
        or item.source_provider_id not in (None, str(policy.provider_id))
    ):
        raise AccountingUnavailable("Authorized ready central QCOW2 disk required")
    return item


def freeze_source(db, owner, data, profile, cache):
    from app.services.deploy_service import (
        _qcow2_virtual_size_from_s3,
        _resolve_library_disk,
    )
    from app.services.native_managed_deploy import helpers

    policy = profile.source_policy
    item = authorized_item(db, owner, data["libraryItemId"], policy)
    requested_gib = data.get("size", data.get("sizeGb", 20))
    client = source_client(db, policy)
    try:
        before = client.head_object(Bucket=policy.bucket, Key=item.s3_key)
        virtual = _qcow2_virtual_size_from_s3(client, policy.bucket, item.s3_key, {})
        if virtual <= 0:
            raise AccountingUnavailable("Measured QCOW2 virtual size required")
        source = CacheSource(
            endpoint=client.meta.endpoint_url,
            bucket=policy.bucket,
            key=item.s3_key,
            etag=before["ETag"],
            size_bytes=before["ContentLength"],
            last_modified=before["LastModified"],
            version_id=before.get("VersionId"),
            content_sha256=before.get("Metadata", {}).get("sha256"),
            immutability_policy="trusted-admin-frozen-source-v1",
        )
        _resolve_library_disk(data, db, None, "", {}, client, policy.bucket, {})
        data["size"] = requested_gib
        from app.services.native_cache_provider import CacheReads

        CacheReads(None, client, profile)._source(
            type("Source", (), {"source": source})()
        )
    finally:
        client.close()
    source_gib = math.ceil(virtual / GIB)
    source_request = max(source_gib + 2, math.ceil(source_gib * 1.15))
    identity = uuid4()
    request = helpers().clone_manifest(
        "pending",
        "pending",
        {
            "name": "managed-cache-" + identity.hex,
            "namespace": cache.namespace,
            "sourceCapacityGiB": source_request,
        },
        requested_gib,
        profile.target.storage_class_name,
    )["spec"]["pvc"]["resources"]["requests"]["storage"]
    return {
        "operation_id": str(identity),
        "name": "managed-cache-" + identity.hex,
        "namespace": cache.namespace,
        "namespace_uid": cache.namespace_uid,
        "library_item_id": item.id,
        "source": source.model_dump(mode="json"),
        "virtual_size_bytes": virtual,
        "sourceCapacityGiB": source_request,
        "source_storage_request": f"{source_request}Gi",
        "storage_request": request,
    }


def freeze(db, project, owner, profile, manifest):
    from app.services.deploy_service import _prepare_topology_library_refs
    from app.services.native_managed_deploy import helpers

    topology = deepcopy(project.topology)
    if profile.operation != "managed-cache-clone":
        return topology, {}
    helpers().validate_topology(topology, clone=True)
    _prepare_topology_library_refs(topology, db, project)
    caches = [
        r
        for r in manifest.pins.cache_roots
        if r.cluster_key == profile.target.cluster_key
    ]
    if len(caches) != 1:
        raise AccountingUnavailable("One protected cache namespace required")
    sources = {}
    for node in topology.get("nodes", []):
        data = node.get("data", {})
        if node["type"] == "storageNode" and data.get("source") == "library":
            sources[node["id"]] = freeze_source(db, owner, data, profile, caches[0])
    if not sources:
        raise AccountingUnavailable("Managed clone requires central image storage")
    return topology, sources


def validate_sources(db, owner, target, profile):
    sources = target.get("sources", {})
    if digest(sources) != target.get("sources_digest", digest({})):
        raise AccountingUnavailable("Frozen managed source plan changed")
    for value in sources.values():
        item = authorized_item(
            db, owner, value["library_item_id"], profile.source_policy
        )
        if item.s3_key != value["source"]["key"]:
            raise AccountingUnavailable("Authorized library source changed")


def managed_authority(db, operation_id, disk_id, *, fresh=True):
    from app.models.quota import QuotaReservation
    from app.services import native_initialization
    from app.services import native_managed_deploy as deploy

    deploy.admission.lock_operations(db, [operation_id])
    row = (
        db.query(QuotaReservation)
        .filter_by(operation_id=operation_id)
        .populate_existing()
        .one()
    )
    project, _, _ = deploy.lock_project(db, row.project_id)
    target, profile = deploy.validate(db, project, row, physical=False)
    if row.state not in {
        "pending",
        "dispatched",
        "reconciling",
    } or not row.dispatch_metadata.get("claim"):
        raise AccountingUnavailable("Claimed admitted cache operation required")
    if fresh and target["phase"] != "bound":
        raise AccountingUnavailable("Bound managed cache preparation required")
    if fresh:
        domain, manifest, _, observation = native_initialization.current(
            db,
            target["provider_id"],
            deploy.operations.now(),
            domain_id=target["domain_id"],
        )
        observed = observation.model_dump(mode="json")
    else:
        domain, manifest, _ = native_initialization.enrollment(
            db, target["provider_id"], domain_id=target["domain_id"]
        )
        observed = target["admission_observation"]
    source = target["sources"][disk_id]
    if not any(
        r.namespace == source["namespace"]
        and r.namespace_uid == source["namespace_uid"]
        and r.cluster_key == profile.target.cluster_key
        for r in manifest.pins.cache_roots
    ):
        raise AccountingUnavailable("Frozen source cache namespace authority changed")
    authority = {
        "operation_id": operation_id,
        "disk_id": disk_id,
        "payload_digest": target["payload_digest"],
        "sources_digest": target["sources_digest"],
        "profile_digest": target["profile_digest"],
        "owner_id": target["owner_id"],
        "organization_id": target["organization_id"],
        "enrollment_id": domain.enrollment_id,
        "initialization_observation": observed,
    }
    return target, profile, source, manifest, authority


def prepare_sources(sessions, operation_id, target, profile, provider):
    """One bounded worker dispatch. Independent collector supplies receipts later."""
    import time

    from app.models.kubevirt_cache_birth import NativeCacheBirth
    from app.schemas.kubevirt_storage import CollectionLimits
    from app.services.native_cache_birth import (
        prepare_managed_cache,
        submit_cache_birth,
    )
    from app.services.native_cache_provider import CacheCreationAdapter
    from app.services.native_provider_reader import ProviderReader

    ready = True
    for disk_id, source in target.get("sources", {}).items():
        identity = prepare_managed_cache(sessions, operation_id, disk_id)
        with sessions() as db:
            row = db.get(NativeCacheBirth, identity)
            state = row.state
            client = (
                source_client(db, profile.source_policy)
                if state == "prepared"
                else None
            )
        if state == "prepared":
            assert client is not None
            kube = ProviderReader(provider, CollectionLimits(), time.monotonic() + 45)
            try:
                submit_cache_birth(
                    sessions, identity, CacheCreationAdapter(kube, client, profile)
                )
            finally:
                kube.close()
                client.close()
        ready = ready and state == "verified"
    return ready


def prepare_clones(db, row, target):
    from app.models.kubevirt_cache_birth import NativeCacheBirth
    from app.services.native_clone_lineage import prepare_clone
    from app.services.native_managed_deploy import helpers

    root = {
        **deepcopy(target["payload"]),
        "metadata": {
            **target["payload"]["metadata"],
            "uid": target["root"]["project_cr_uid"],
        },
    }
    ids = {}
    for value in helpers().render(root, target["initialization_storage_class"]):
        if value["kind"] != "DataVolume":
            continue
        name = value["spec"]["source"]["pvc"]["name"]
        source = next(v for v in target["sources"].values() if v["name"] == name)
        birth = db.get(NativeCacheBirth, source["operation_id"])
        reference = {"operation_id": birth.id, "digest": birth.receipt_digest}
        ids[value["metadata"]["name"]] = prepare_clone(
            db,
            operation_id=row.operation_id,
            claim_name=value["metadata"]["name"],
            cache_reference=reference,
        )
    return ids
