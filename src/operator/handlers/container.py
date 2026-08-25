import logging
from kubernetes import client
from helpers.k8s import GATEWAY_IMAGE

logger = logging.getLogger(__name__)

_SHELL = "/bin/sh"


def create_container_pods(namespace, containers, nad_refs, owner_reference, disk_pvcs=None):
    core_api = client.CoreV1Api()
    disk_pvcs = disk_pvcs or {}

    for ctr in containers:
        is_pod = ctr.get("isPod", False)

        if is_pod:
            _create_pod_group(
                core_api, namespace, ctr, nad_refs, owner_reference, disk_pvcs
            )
        else:
            _create_single_container(
                core_api, namespace, ctr, nad_refs, owner_reference, disk_pvcs
            )


def _env_to_list(env):
    """Normalize env dict or envVars list to K8s env list."""
    if not env:
        return []
    if isinstance(env, list):
        return [
            {"name": item.get("key", item.get("name", "")), "value": str(item.get("value", ""))}
            for item in env
            if item.get("key") or item.get("name")
        ]
    return [{"name": k, "value": str(v)} for k, v in env.items()]


def _split_command(command):
    """Map topology command to K8s command and/or args."""
    if not command:
        return {}
    if isinstance(command, list):
        first = command[0] if command else ""
        if first.startswith("-"):
            return {"args": command}
        return {"command": command}
    return {"command": [_SHELL, "-c", command]}


def _collect_mount_specs(ctr, disk_pvcs):
    """Build shared pod volumes and per-container volumeMounts."""
    volumes = []
    volume_mounts = []
    seen_vols = set()
    seen_mounts = set()

    def _add_mount(mount):
        disk_id = mount.get("diskNodeId", "")
        mount_path = mount.get("mountPath", "")
        if not disk_id or not mount_path:
            return
        pvc_name = disk_pvcs.get(disk_id)
        if not pvc_name:
            return
        vol_name = f"disk-{disk_id[:8]}"
        if vol_name not in seen_vols:
            seen_vols.add(vol_name)
            volumes.append(
                {
                    "name": vol_name,
                    "persistentVolumeClaim": {"claimName": pvc_name},
                }
            )
        mount_key = (vol_name, mount_path)
        if mount_key in seen_mounts:
            return
        seen_mounts.add(mount_key)
        volume_mounts.append({"name": vol_name, "mountPath": mount_path})

    for mount in ctr.get("mounts", []):
        _add_mount(mount)
    for ic in ctr.get("initContainers", []):
        for mount in ic.get("mounts", []):
            _add_mount(mount)
    for pc in ctr.get("podContainers", []):
        for mount in pc.get("mounts", []):
            _add_mount(mount)

    return volumes, volume_mounts


def _apply_volume_mounts(container_spec, volume_mounts):
    if volume_mounts:
        container_spec["volumeMounts"] = volume_mounts


def _create_single_container(
    core_api, namespace, ctr, nad_refs, owner_reference, disk_pvcs
):
    ctr_id = ctr.get("id", "")[:8]
    pod_name = f"ctr-{ctr_id}"

    net_annotations = _build_network_annotations(ctr, nad_refs)
    volumes, volume_mounts = _collect_mount_specs(ctr, disk_pvcs)

    container_spec = {
        "name": "main",
        "image": ctr.get("image", ""),
        "env": _env_to_list(ctr.get("env") or ctr.get("envVars")),
        "resources": {
            "requests": {
                "cpu": f"{ctr.get('cpus', 1) * 1000}m",
                "memory": f"{ctr.get('memory', 512)}Mi",
            },
        },
    }
    cmd = _split_command(ctr.get("command"))
    if cmd:
        container_spec.update(cmd)
    if ctr.get("ports"):
        container_spec["ports"] = [
            {
                "containerPort": p.get("container_port", p.get("containerPort", p.get("port", 0))),
                "protocol": "TCP",
            }
            for p in ctr["ports"]
        ]
    _apply_volume_mounts(container_spec, volume_mounts)

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
            "securityContext": {"runAsUser": 0, "fsGroup": 0},
            "serviceAccountName": "troshka-network",
        },
    }
    if volumes:
        pod_body["spec"]["volumes"] = volumes
    if net_annotations:
        pod_body["metadata"]["annotations"] = {
            "k8s.v1.cni.cncf.io/networks": ",".join(net_annotations)
        }

    try:
        core_api.create_namespaced_pod(namespace=namespace, body=pod_body)
        logger.info(f"Created container pod {pod_name}")
    except client.ApiException as e:
        if e.status != 409:
            raise


def _build_init_container_spec(ic, index, volume_mounts):
    """Build spec for a single init container."""
    init_spec = {
        "name": ic.get("name", f"init-{index}"),
        "image": ic.get("image", ""),
        "env": _env_to_list(ic.get("env") or ic.get("envVars")),
    }
    cmd = _split_command(ic.get("command"))
    if cmd:
        init_spec.update(cmd)
    _apply_volume_mounts(init_spec, volume_mounts)
    return init_spec


def _build_pod_container_spec(pc, index, volume_mounts):
    """Build spec for a single pod container."""
    c_spec = {
        "name": pc.get("name", f"container-{index}"),
        "image": pc.get("image", ""),
        "env": _env_to_list(pc.get("env") or pc.get("envVars")),
    }
    cmd = _split_command(pc.get("command"))
    if cmd:
        c_spec.update(cmd)
    if pc.get("ports"):
        c_spec["ports"] = [
            {
                "containerPort": p.get("container_port", p.get("containerPort", p.get("port", 0))),
                "protocol": "TCP",
            }
            for p in pc["ports"]
        ]
    _apply_volume_mounts(c_spec, volume_mounts)
    return c_spec


def _build_setup_ip_init(ctr):
    """Init container to assign static IPs on multus interfaces (net1, net2, ...)."""
    inits = []
    for idx, nic in enumerate(ctr.get("nics", [])):
        ip = nic.get("ip", "")
        cidr = nic.get("cidr", "")
        if not ip or not cidr or "/" not in cidr:
            continue
        prefix = cidr.split("/", 1)[1]
        dev = f"net{idx + 1}"
        setup_cmd = f"ip addr add {ip}/{prefix} dev {dev} && ip link set {dev} up"
        inits.append(
            {
                "name": f"setup-ip-{idx}",
                "image": GATEWAY_IMAGE,
                "imagePullPolicy": "Always",
                "command": ["sh", "-c", setup_cmd],
                "securityContext": {"capabilities": {"add": ["NET_ADMIN"]}},
            }
        )
    return inits


def _build_network_annotations(ctr, nad_refs):
    """Build network annotations from NICs."""
    net_annotations = []
    for nic in ctr.get("nics", []):
        net_ref = nic.get("networkRef", "")
        nad = nad_refs.get(net_ref, f"{net_ref}-nad")
        net_annotations.append(nad)
    return net_annotations


def _create_pod_group(core_api, namespace, ctr, nad_refs, owner_reference, disk_pvcs):
    ctr_id = ctr.get("id", "")[:8]
    pod_name = f"pod-{ctr_id}"

    volumes, volume_mounts = _collect_mount_specs(ctr, disk_pvcs)

    init_containers = _build_setup_ip_init(ctr) + [
        _build_init_container_spec(ic, i, volume_mounts)
        for i, ic in enumerate(ctr.get("initContainers", []))
    ]

    containers = [
        _build_pod_container_spec(pc, i, volume_mounts)
        for i, pc in enumerate(ctr.get("podContainers", []))
    ]

    if not containers:
        containers = [{"name": "main", "image": ctr.get("image", "")}]

    net_annotations = _build_network_annotations(ctr, nad_refs)

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
            "securityContext": {"runAsUser": 0, "fsGroup": 0},
            "serviceAccountName": "troshka-network",
        },
    }
    if volumes:
        pod_body["spec"]["volumes"] = volumes

    if net_annotations:
        pod_body["metadata"]["annotations"] = {
            "k8s.v1.cni.cncf.io/networks": ",".join(net_annotations)
        }

    try:
        core_api.create_namespaced_pod(namespace=namespace, body=pod_body)
        logger.info(f"Created pod group {pod_name}")
    except client.ApiException as e:
        if e.status != 409:
            raise
