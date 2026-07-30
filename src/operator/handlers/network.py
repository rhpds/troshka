import asyncio
import datetime
import kopf
import logging
from kubernetes import client
from helpers.k8s import (
    CRD_GROUP,
    CRD_VERSION,
    build_nad,
    build_dnsmasq_deployment,
)
from helpers.dnsmasq import generate_dnsmasq_config

logger = logging.getLogger(__name__)


def _modify_scc_users(custom_api, scc_name, sa_ref, action):
    """Modify a single SCC's users list. Returns True if a patch was applied."""
    scc = custom_api.get_cluster_custom_object(
        group="security.openshift.io",
        version="v1",
        plural="securitycontextconstraints",
        name=scc_name,
    )
    users = scc.get("users", []) or []

    if action == "add" and sa_ref not in users:
        users.append(sa_ref)
    elif action == "remove" and sa_ref in users:
        users.remove(sa_ref)
    else:
        return False

    custom_api.patch_cluster_custom_object(
        group="security.openshift.io",
        version="v1",
        plural="securitycontextconstraints",
        name=scc_name,
        body={"users": users},
    )
    verb = "Added" if action == "add" else "Removed"
    preposition = "to" if action == "add" else "from"
    logger.info(f"{verb} {sa_ref} {preposition} {scc_name} SCC")
    return True


async def _patch_single_scc(custom_api, scc_name, sa_ref, action, namespace):
    """Patch a single SCC with retry on 409 conflict."""
    verb = "patch" if action == "add" else "clean"
    for attempt in range(5):
        try:
            _modify_scc_users(custom_api, scc_name, sa_ref, action)
            return
        except client.ApiException as e:
            if e.status == 409 and attempt < 4:
                await asyncio.sleep(0.2 * (attempt + 1))
                continue
            logger.warning(f"Could not {verb} SCC {scc_name} for {namespace}: {e}")
            return
        except Exception as e:
            logger.warning(f"Could not {verb} SCC {scc_name} for {namespace}: {e}")
            return


async def _patch_scc_users(custom_api, sa_ref, scc_names, action, namespace):
    """Add or remove a service account ref from SCC users lists with retry on 409."""
    for scc_name in scc_names:
        await _patch_single_scc(custom_api, scc_name, sa_ref, action, namespace)


async def _wait_for_deployment_deletion(apps_api, namespace, dep_name):
    """Poll until a deployment is fully deleted (up to 60s)."""
    for _ in range(30):
        try:
            apps_api.read_namespaced_deployment(name=dep_name, namespace=namespace)
            await asyncio.sleep(2)
        except client.ApiException as e:
            if e.status == 404:
                return
            raise


async def _create_deployment_with_stale_cleanup(
    apps_api, namespace, dep_name, deployment_body
):
    """Create a deployment, handling stale 409 by waiting for deletion then retrying."""
    try:
        apps_api.create_namespaced_deployment(namespace=namespace, body=deployment_body)
        logger.info(f"Created deployment {dep_name}")
        return
    except client.ApiException as e:
        if e.status != 409:
            raise

    logger.info(f"Deployment {dep_name} exists (stale), waiting for deletion")
    await _wait_for_deployment_deletion(apps_api, namespace, dep_name)
    apps_api.create_namespaced_deployment(namespace=namespace, body=deployment_body)
    logger.info(f"Created deployment {dep_name} (after stale cleanup)")


def _cleanup_legacy_pod(core_api, namespace, pod_name):
    """Delete a standalone Pod if it exists (migration from Pod to Deployment)."""
    try:
        pod = core_api.read_namespaced_pod(name=pod_name, namespace=namespace)
        owners = getattr(pod.metadata, "owner_references", None) or []
        if not any(o.kind == "ReplicaSet" for o in owners):
            core_api.delete_namespaced_pod(name=pod_name, namespace=namespace)
            logger.info(f"Deleted legacy standalone Pod {pod_name}")
    except client.ApiException as e:
        if e.status != 404:
            raise


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
    except client.ApiException as e:
        if e.status != 409:
            raise

    sa_ref = f"system:serviceaccount:{namespace}:troshka-network"
    await _patch_scc_users(
        custom_api,
        sa_ref,
        ("troshka-network-pods", "troshka-gateway"),
        "add",
        namespace,
    )

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
    except client.ApiException as e:
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
    except client.ApiException as e:
        if e.status != 409:
            raise

    apps_api = client.AppsV1Api()
    dep_name = f"dnsmasq-{name}"
    _cleanup_legacy_pod(api, namespace, dep_name)

    dnsmasq_dep = build_dnsmasq_deployment(body)
    await _create_deployment_with_stale_cleanup(
        apps_api, namespace, dep_name, dnsmasq_dep
    )

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
    except client.ApiException as e:
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

    # Trigger rollout restart via annotation
    apps_api = client.AppsV1Api()
    dep_name = f"dnsmasq-{name}"
    apps_api.patch_namespaced_deployment(
        name=dep_name,
        namespace=namespace,
        body={
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": datetime.datetime.now(
                                datetime.timezone.utc
                            ).isoformat()
                        }
                    }
                }
            }
        },
    )
    logger.info(f"Triggered rollout restart for dnsmasq deployment {dep_name}")


@kopf.on.delete(CRD_GROUP, CRD_VERSION, "troshkanetworks")
async def network_delete(spec, meta, namespace, name, **_):
    logger.info(f"Deleting network {name} in {namespace} — cleaning up resources")
    api = client.CoreV1Api()
    apps_api = client.AppsV1Api()
    custom_api = client.CustomObjectsApi()

    sa_ref = f"system:serviceaccount:{namespace}:troshka-network"
    await _patch_scc_users(
        custom_api,
        sa_ref,
        ("troshka-network-pods", "troshka-gateway"),
        "remove",
        namespace,
    )

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
    except client.ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to delete NAD {nad_name}: {e}")

    for dep_name in [f"dnsmasq-{name}", f"gateway-{namespace}"]:
        try:
            apps_api.delete_namespaced_deployment(name=dep_name, namespace=namespace)
            logger.info(f"Deleted deployment {dep_name}")
        except client.ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete deployment {dep_name}: {e}")

    for resource_name in [f"dnsmasq-{name}"]:
        try:
            api.delete_namespaced_config_map(name=resource_name, namespace=namespace)
        except client.ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete configmap {resource_name}: {e}")
