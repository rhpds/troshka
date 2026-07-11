import logging
from kubernetes import client

logger = logging.getLogger(__name__)


def create_container_pods(namespace, containers, nad_refs, owner_reference):
    core_api = client.CoreV1Api()

    for ctr in containers:
        is_pod = ctr.get("isPod", False)

        if is_pod:
            _create_pod_group(
                core_api, namespace, ctr, nad_refs, owner_reference
            )
        else:
            _create_single_container(
                core_api, namespace, ctr, nad_refs, owner_reference
            )


def _create_single_container(
    core_api, namespace, ctr, nad_refs, owner_reference
):
    ctr_id = ctr.get("id", "")[:8]
    pod_name = f"ctr-{ctr_id}"

    net_annotations = []
    for nic in ctr.get("nics", []):
        net_ref = nic.get("networkRef", "")
        nad = nad_refs.get(net_ref, f"{net_ref}-nad")
        net_annotations.append(nad)

    env_list = []
    for k, v in ctr.get("env", {}).items():
        env_list.append({"name": k, "value": str(v)})

    container_spec = {
        "name": "main",
        "image": ctr.get("image", ""),
        "env": env_list,
        "resources": {
            "requests": {
                "cpu": f"{ctr.get('cpus', 1) * 1000}m",
                "memory": f"{ctr.get('memory', 512)}Mi",
            },
        },
    }
    if ctr.get("command"):
        container_spec["command"] = ["/bin/sh", "-c", ctr["command"]]
    if ctr.get("ports"):
        container_spec["ports"] = [
            {
                "containerPort": p.get(
                    "container_port", p.get("port", 0)
                ),
                "protocol": "TCP",
            }
            for p in ctr["ports"]
        ]

    pod_body = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "labels": {
                "app": "troshka-container",
                "troshka-container": ctr_id,
            },
            "ownerReferences": [owner_reference],
        },
        "spec": {
            "containers": [container_spec],
            "restartPolicy": "Always",
        },
    }

    if net_annotations:
        pod_body["metadata"]["annotations"] = {
            "k8s.v1.cni.cncf.io/networks": ",".join(net_annotations)
        }

    try:
        core_api.create_namespaced_pod(
            namespace=namespace, body=pod_body
        )
        logger.info(f"Created container pod {pod_name}")
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise


def _create_pod_group(
    core_api, namespace, ctr, nad_refs, owner_reference
):
    ctr_id = ctr.get("id", "")[:8]
    pod_name = f"pod-{ctr_id}"

    init_containers = []
    for ic in ctr.get("initContainers", []):
        init_spec = {
            "name": ic.get("name", f"init-{len(init_containers)}"),
            "image": ic.get("image", ""),
        }
        if ic.get("command"):
            init_spec["command"] = ["/bin/sh", "-c", ic["command"]]
        init_containers.append(init_spec)

    containers = []
    for pc in ctr.get("podContainers", []):
        c_spec = {
            "name": pc.get("name", f"container-{len(containers)}"),
            "image": pc.get("image", ""),
        }
        if pc.get("command"):
            c_spec["command"] = ["/bin/sh", "-c", pc["command"]]
        if pc.get("ports"):
            c_spec["ports"] = [
                {
                    "containerPort": p.get(
                        "container_port", p.get("port", 0)
                    ),
                    "protocol": "TCP",
                }
                for p in pc["ports"]
            ]
        env_list = []
        for k, v in pc.get("env", {}).items():
            env_list.append({"name": k, "value": str(v)})
        if env_list:
            c_spec["env"] = env_list
        containers.append(c_spec)

    if not containers:
        containers = [
            {"name": "main", "image": ctr.get("image", "")}
        ]

    net_annotations = []
    for nic in ctr.get("nics", []):
        net_ref = nic.get("networkRef", "")
        nad = nad_refs.get(net_ref, f"{net_ref}-nad")
        net_annotations.append(nad)

    pod_body = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "labels": {
                "app": "troshka-pod",
                "troshka-pod": ctr_id,
            },
            "ownerReferences": [owner_reference],
        },
        "spec": {
            "initContainers": init_containers,
            "containers": containers,
            "restartPolicy": "Always",
        },
    }

    if net_annotations:
        pod_body["metadata"]["annotations"] = {
            "k8s.v1.cni.cncf.io/networks": ",".join(net_annotations)
        }

    try:
        core_api.create_namespaced_pod(
            namespace=namespace, body=pod_body
        )
        logger.info(f"Created pod group {pod_name}")
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise
