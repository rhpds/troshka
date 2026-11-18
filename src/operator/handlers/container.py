import logging
from helpers.managed_power import assert_running
from helpers.native_origin_drain import FenceClient

# Preserve existing handler imports for callers while rendering stays pure.
from helpers.containers import (  # noqa: F401
    _add_container_mount,
    _apply_managed_dns,
    _apply_volume_mounts,
    _build_init_container_spec,
    _build_network_annotations,
    _build_pod_container_spec,
    _build_setup_ip_init,
    _collect_mount_specs,
    _env_to_list,
    _split_command,
    build_pod_group,
    build_single_container_pod,
)
from kubernetes import client

logger = logging.getLogger(__name__)


def create_container_pods(
    namespace, containers, nad_refs, owner_reference, disk_pvcs=None
):
    assert_running(namespace, owner_reference.get("uid"))
    core_api = FenceClient(client.CoreV1Api())
    disk_pvcs = disk_pvcs or {}

    for ctr in containers:
        assert_running(namespace, owner_reference.get("uid"))
        is_pod = ctr.get("isPod", False)

        if is_pod:
            _create_pod_group(
                core_api, namespace, ctr, nad_refs, owner_reference, disk_pvcs
            )
        else:
            _create_single_container(
                core_api, namespace, ctr, nad_refs, owner_reference, disk_pvcs
            )


def _create_single_container(
    core_api, namespace, ctr, nad_refs, owner_reference, disk_pvcs
):
    pod_body = build_single_container_pod(
        namespace, ctr, nad_refs, owner_reference, disk_pvcs
    )
    try:
        core_api.create_namespaced_pod(namespace=namespace, body=pod_body)
        logger.info("Created pod %s", pod_body["metadata"]["name"])
    except client.ApiException as e:
        if e.status != 409:
            raise
def _create_pod_group(core_api, namespace, ctr, nad_refs, owner_reference, disk_pvcs):
    pod_body = build_pod_group(namespace, ctr, nad_refs, owner_reference, disk_pvcs)
    try:
        core_api.create_namespaced_pod(namespace=namespace, body=pod_body)
        logger.info("Created pod %s", pod_body["metadata"]["name"])
    except client.ApiException as e:
        if e.status != 409:
            raise
