import kopf
import logging
from kubernetes import client
from helpers.k8s import (
    CRD_GROUP,
    CRD_VERSION,
    build_nad,
    build_dnsmasq_pod,
    build_gateway_pod,
)
from helpers.dnsmasq import generate_dnsmasq_config

logger = logging.getLogger(__name__)


@kopf.on.create(CRD_GROUP, CRD_VERSION, "troshkanetworks")
async def network_create(spec, meta, namespace, name, body, patch, **_):
    logger.info(f"Creating network {name} in {namespace}")

    api = client.CoreV1Api()
    custom_api = client.CustomObjectsApi()

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
    try:
        api.create_namespaced_pod(namespace=namespace, body=dnsmasq_pod)
        logger.info(f"Created dnsmasq pod for {name}")
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    gateway_ready = True
    if spec.get("externalAccess"):
        nad_name = f"{name}-nad"
        gateway_pod = build_gateway_pod(body, [nad_name])
        try:
            api.create_namespaced_pod(namespace=namespace, body=gateway_pod)
            logger.info(f"Created gateway pod for {name}")
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise

    patch.status["ready"] = True
    patch.status["nadName"] = f"{name}-nad"
    patch.status["dhcpPodReady"] = True
    patch.status["gatewayPodReady"] = gateway_ready
    logger.info(f"Network {name} ready")


@kopf.on.delete(CRD_GROUP, CRD_VERSION, "troshkanetworks")
async def network_delete(spec, meta, namespace, name, **_):
    logger.info(
        f"Deleting network {name} in {namespace} (ownerReferences handle cascade)"
    )
