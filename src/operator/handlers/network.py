import kopf
import logging
from kubernetes import client
from helpers.k8s import (
    CRD_GROUP,
    CRD_VERSION,
    build_nad,
    build_dnsmasq_pod,
)
from helpers.dnsmasq import generate_dnsmasq_config

logger = logging.getLogger(__name__)


@kopf.on.create(CRD_GROUP, CRD_VERSION, "troshkanetworks")
async def network_create(spec, meta, namespace, name, body, patch, **_):
    logger.info(f"Creating network {name} in {namespace}")

    api = client.CoreV1Api()
    custom_api = client.CustomObjectsApi()

    try:
        api.create_namespaced_service_account(
            namespace=namespace,
            body=client.V1ServiceAccount(
                metadata=client.V1ObjectMeta(name="troshka-network"),
            ),
        )
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    sa_ref = f"system:serviceaccount:{namespace}:troshka-network"
    for scc_name in ("troshka-network-pods", "troshka-gateway"):
        try:
            scc = custom_api.get_cluster_custom_object(
                group="security.openshift.io",
                version="v1",
                plural="securitycontextconstraints",
                name=scc_name,
            )
            users = scc.get("users", []) or []
            if sa_ref not in users:
                users.append(sa_ref)
                custom_api.patch_cluster_custom_object(
                    group="security.openshift.io",
                    version="v1",
                    plural="securitycontextconstraints",
                    name=scc_name,
                    body={"users": users},
                )
                logger.info(f"Added {sa_ref} to {scc_name} SCC")
        except Exception as e:
            logger.warning(f"Could not patch SCC {scc_name} for {namespace}: {e}")

    nad = build_nad(body)
    try:
        custom_api.create_namespaced_custom_object(
            group="k8s.cni.cncf.io",
            version="v1",
            namespace=namespace,
            plural="network-attachment-definitions",
            body=nad,
        )
        logger.info(f"Created NAD {nad['metadata']['name']}")
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    dnsmasq_conf = generate_dnsmasq_config(spec)
    cm_body = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=f"dnsmasq-{name}",
            namespace=namespace,
        ),
        data={"dnsmasq.conf": dnsmasq_conf},
    )
    try:
        api.create_namespaced_config_map(namespace=namespace, body=cm_body)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    dnsmasq_pod = build_dnsmasq_pod(body, dnsmasq_conf)
    pod_name = f"dnsmasq-{name}"
    try:
        api.create_namespaced_pod(namespace=namespace, body=dnsmasq_pod)
        logger.info(f"Created dnsmasq pod for {name}")
    except client.exceptions.ApiException as e:
        if e.status == 409:
            import time

            logger.info(f"Dnsmasq pod {pod_name} exists (stale), waiting for deletion")
            for _ in range(30):
                try:
                    api.read_namespaced_pod(name=pod_name, namespace=namespace)
                    time.sleep(2)
                except client.exceptions.ApiException as ge:
                    if ge.status == 404:
                        break
                    raise
            api.create_namespaced_pod(namespace=namespace, body=dnsmasq_pod)
            logger.info(f"Created dnsmasq pod {pod_name} (after stale cleanup)")
        else:
            raise

    patch.status["ready"] = True
    patch.status["nadName"] = f"{name}-nad"
    patch.status["dhcpPodReady"] = True
    patch.status["gatewayPodReady"] = True
    logger.info(f"Network {name} ready")


@kopf.on.update(CRD_GROUP, CRD_VERSION, "troshkanetworks", field="spec")
async def network_update(spec, meta, namespace, name, body, patch, **_):
    """Reconcile dnsmasq config when network spec changes (e.g. DNS records added)."""
    logger.info(f"Updating network {name} in {namespace}")
    api = client.CoreV1Api()

    dnsmasq_conf = generate_dnsmasq_config(spec)
    cm_name = f"dnsmasq-{name}"
    try:
        api.patch_namespaced_config_map(
            name=cm_name,
            namespace=namespace,
            body={"data": {"dnsmasq.conf": dnsmasq_conf}},
        )
        logger.info(f"Updated ConfigMap {cm_name}")
    except client.exceptions.ApiException as e:
        if e.status == 404:
            api.create_namespaced_config_map(
                namespace=namespace,
                body=client.V1ConfigMap(
                    metadata=client.V1ObjectMeta(name=cm_name, namespace=namespace),
                    data={"dnsmasq.conf": dnsmasq_conf},
                ),
            )
        else:
            raise

    pod_name = f"dnsmasq-{name}"
    try:
        api.delete_namespaced_pod(name=pod_name, namespace=namespace)
        logger.info(f"Deleted dnsmasq pod {pod_name} for restart")
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise

    import time

    for _ in range(30):
        try:
            api.read_namespaced_pod(name=pod_name, namespace=namespace)
            time.sleep(2)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                break
            raise

    dnsmasq_pod = build_dnsmasq_pod(body, dnsmasq_conf)
    api.create_namespaced_pod(namespace=namespace, body=dnsmasq_pod)
    logger.info(f"Recreated dnsmasq pod {pod_name} with updated config")


@kopf.on.delete(CRD_GROUP, CRD_VERSION, "troshkanetworks")
async def network_delete(spec, meta, namespace, name, **_):
    logger.info(f"Deleting network {name} in {namespace} — cleaning up resources")
    api = client.CoreV1Api()
    custom_api = client.CustomObjectsApi()

    sa_ref = f"system:serviceaccount:{namespace}:troshka-network"
    for scc_name in ("troshka-network-pods", "troshka-gateway"):
        try:
            scc = custom_api.get_cluster_custom_object(
                group="security.openshift.io",
                version="v1",
                plural="securitycontextconstraints",
                name=scc_name,
            )
            users = scc.get("users", []) or []
            if sa_ref in users:
                users.remove(sa_ref)
                custom_api.patch_cluster_custom_object(
                    group="security.openshift.io",
                    version="v1",
                    plural="securitycontextconstraints",
                    name=scc_name,
                    body={"users": users},
                )
                logger.info(f"Removed {sa_ref} from {scc_name} SCC")
        except Exception as e:
            logger.warning(f"Could not clean SCC {scc_name} for {namespace}: {e}")

    nad_name = f"{name}-nad"
    try:
        custom_api.delete_namespaced_custom_object(
            group="k8s.cni.cncf.io",
            version="v1",
            namespace=namespace,
            plural="network-attachment-definitions",
            name=nad_name,
        )
        logger.info(f"Deleted NAD {nad_name}")
    except client.exceptions.ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to delete NAD {nad_name}: {e}")

    for pod_name in [f"dnsmasq-{name}", f"gateway-{namespace}"]:
        try:
            api.delete_namespaced_pod(name=pod_name, namespace=namespace)
            logger.info(f"Deleted pod {pod_name}")
        except client.exceptions.ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete pod {pod_name}: {e}")

    for resource_name in [f"dnsmasq-{name}"]:
        try:
            api.delete_namespaced_config_map(name=resource_name, namespace=namespace)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete configmap {resource_name}: {e}")
