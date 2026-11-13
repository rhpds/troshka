"""Read-only verification for explicitly attaching a preinstalled operator.

Registration does not establish support for measured storage or managed quotas.
"""

from app.services.providers import kubevirt

_CRDS = ("troshkaprojects", "troshkavms", "troshkanetworks", "troshkancephs")
_READ_TIMEOUT = 10


def _operator_image(containers):
    matches = [c.image for c in containers or [] if c.name == "operator"]
    if len(matches) != 1:
        raise ValueError("Operator container missing or ambiguous")
    return matches[0]


def _verify_deployment(deployment, expected_image):
    status = deployment.status
    replicas = deployment.spec.replicas
    if deployment.metadata.deletion_timestamp or not replicas or replicas < 1:
        raise ValueError("Operator deployment inactive")
    if (
        not deployment.metadata.generation
        or (status.observed_generation or 0) < deployment.metadata.generation
    ):
        raise ValueError("Operator generation not observed")
    counts = (
        status.ready_replicas,
        status.available_replicas,
        status.updated_replicas,
        status.replicas,
    )
    if any(count != replicas for count in counts):
        raise ValueError("Operator rollout not ready")
    if not any(
        c.type == "Available" and c.status == "True" for c in status.conditions or []
    ):
        raise ValueError("Operator deployment unavailable")
    if _operator_image(deployment.spec.template.spec.containers) != expected_image:
        raise ValueError("Operator deployment image mismatch")


def _verify_pod(pod, deployment, apps_api, namespace, expected_image):
    if pod.metadata.deletion_timestamp or pod.status.phase != "Running":
        raise ValueError("Operator pod inactive")
    owners = [
        r
        for r in pod.metadata.owner_references or []
        if r.kind == "ReplicaSet" and r.controller
    ]
    if len(owners) != 1:
        raise ValueError("Operator pod ownership ambiguous")
    rs = apps_api.read_namespaced_replica_set(
        owners[0].name, namespace, _request_timeout=_READ_TIMEOUT
    )
    if not any(
        r.kind == "Deployment" and r.controller and r.uid == deployment.metadata.uid
        for r in rs.metadata.owner_references or []
    ):
        raise ValueError("Operator pod belongs to another deployment")
    if _operator_image(pod.spec.containers) != expected_image:
        raise ValueError("Operator pod image mismatch")
    statuses = [s for s in pod.status.container_statuses or [] if s.name == "operator"]
    if (
        len(statuses) != 1
        or not statuses[0].ready
        or statuses[0].image != expected_image
    ):
        raise ValueError("Running operator container not ready at expected image")


def _verify_pods(deployment, core_api, apps_api, namespace, expected_image):
    selector = deployment.spec.selector
    if not selector.match_labels or selector.match_expressions:
        raise ValueError("Unsupported operator selector")
    labels = ",".join(
        f"{key}={value}" for key, value in sorted(selector.match_labels.items())
    )
    pods = core_api.list_namespaced_pod(
        namespace=namespace, label_selector=labels, _request_timeout=_READ_TIMEOUT
    )
    if len(pods.items) != deployment.spec.replicas:
        raise ValueError("Operator pod count does not match deployment")
    for pod in pods.items:
        _verify_pod(pod, deployment, apps_api, namespace, expected_image)


def _verify_crds(ext_api):
    for plural in _CRDS:
        crd = ext_api.read_custom_resource_definition(
            f"{plural}.troshka.redhat.com", _request_timeout=_READ_TIMEOUT
        )
        if crd.metadata.deletion_timestamp or not any(
            c.type == "Established" and c.status == "True"
            for c in crd.status.conditions or []
        ):
            raise ValueError("Required Troshka CRD not established")


def verify_existing_operator(provider, expected_image):
    """Return discovered capacity; propagate any failed/unavailable observation."""
    from kubernetes import client

    custom_api, core_api, api_client = kubevirt._get_k8s_clients(provider)
    try:
        namespace = kubevirt._operator_ns(provider)
        apps_api = client.AppsV1Api(api_client)
        deployment = apps_api.read_namespaced_deployment(
            name="troshka-operator", namespace=namespace, _request_timeout=_READ_TIMEOUT
        )
        _verify_deployment(deployment, expected_image)
        _verify_pods(deployment, core_api, apps_api, namespace, expected_image)
        _verify_crds(client.ApiextensionsV1Api(api_client))
        vcpus, ram_mb = kubevirt._query_cluster_capacity(core_api, strict=True)
        storage_gb = kubevirt._query_ceph_storage_gb(core_api, strict=True)
        max_eips = kubevirt._query_metallb_capacity(custom_api, strict=True)
        if min(vcpus, ram_mb, storage_gb) <= 0 or max_eips < 0:
            raise ValueError("Cluster capacity unavailable")
        return {
            "total_vcpus": vcpus,
            "total_ram_mb": ram_mb,
            "storage_size_gb": storage_gb,
            "max_eips": max_eips,
        }
    finally:
        # All wrappers share the supplied client; do not retain its credentials.
        if hasattr(api_client, "close"):
            api_client.close()
