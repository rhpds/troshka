import hashlib
import json

CRD_GROUP = "troshka.redhat.com"
CRD_VERSION = "v1alpha1"
TOOLS_IMAGE = "quay.io/redhat-gpte/troshka-tools:latest"
DNSMASQ_IMAGE = "quay.io/redhat-gpte/troshka-dnsmasq:latest"
GATEWAY_IMAGE = "quay.io/redhat-gpte/troshka-gateway:latest"


def owner_ref(cr):
    return {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": cr["kind"],
        "name": cr["metadata"]["name"],
        "uid": cr["metadata"]["uid"],
        "controller": True,
    }


def golden_pvc_name(s3_path):
    h = hashlib.sha256(s3_path.encode()).hexdigest()[:16]
    return f"golden-{h}"


def build_nad(network_cr):
    spec = network_cr["spec"]
    name = network_cr["metadata"]["name"]
    namespace = network_cr["metadata"]["namespace"]

    nad_name = f"{name}-nad"
    config = {
        "cniVersion": "0.3.1",
        "name": nad_name,
        "netAttachDefName": f"{namespace}/{nad_name}",
        "type": "ovn-k8s-cni-overlay",
        "topology": "layer2",
    }

    return {
        "apiVersion": "k8s.cni.cncf.io/v1",
        "kind": "NetworkAttachmentDefinition",
        "metadata": {
            "name": nad_name,
            "namespace": namespace,
            "ownerReferences": [owner_ref(network_cr)],
            "labels": {"app": "troshka", "troshka-network": name},
        },
        "spec": {"config": json.dumps(config)},
    }


def _gateway_ip_from_cidr(cidr):
    """Derive .1 gateway IP and prefix from CIDR (e.g., '10.0.0.0/24' -> '10.0.0.1/24')."""
    if not cidr or "/" not in cidr:
        return "", ""
    parts = cidr.split("/")
    octets = parts[0].split(".")
    octets[3] = "1"
    return ".".join(octets), parts[1]


def build_dnsmasq_pod(network_cr, dnsmasq_config):
    spec = network_cr["spec"]
    name = network_cr["metadata"]["name"]
    namespace = network_cr["metadata"]["namespace"]
    nad_name = f"{name}-nad"

    pod_name = f"dnsmasq-{name}"

    annotations = {"k8s.v1.cni.cncf.io/networks": nad_name}

    cidr = spec.get("cidr", "")
    gw_ip, prefix = _gateway_ip_from_cidr(cidr)
    gateway_spec = spec.get("gateway", "")
    if gateway_spec:
        gw_ip = gateway_spec

    setup_cmd = "true"
    if gw_ip and prefix:
        setup_cmd = f"ip addr add {gw_ip}/{prefix} dev net1 && ip link set net1 up"

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "ownerReferences": [owner_ref(network_cr)],
            "labels": {"app": "troshka-dnsmasq", "troshka-network": name},
            "annotations": annotations,
        },
        "spec": {
            "serviceAccountName": "troshka-network",
            "initContainers": [
                {
                    "name": "setup-ip",
                    "image": DNSMASQ_IMAGE,
                    "command": ["sh", "-c", setup_cmd],
                    "securityContext": {
                        "capabilities": {"add": ["NET_ADMIN"]}
                    },
                }
            ],
            "containers": [
                {
                    "name": "dnsmasq",
                    "image": DNSMASQ_IMAGE,
                    "command": [
                        "dnsmasq",
                        "--no-daemon",
                        "--conf-file=/etc/dnsmasq/dnsmasq.conf",
                    ],
                    "securityContext": {
                        "capabilities": {"add": ["NET_ADMIN", "NET_RAW"]}
                    },
                    "volumeMounts": [
                        {"name": "config", "mountPath": "/etc/dnsmasq"}
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "config",
                    "configMap": {"name": f"dnsmasq-{name}"},
                }
            ],
            "restartPolicy": "Always",
        },
    }


def build_gateway_pod(project_cr, all_network_nads, gateway_ips=None):
    """Build a single gateway pod for the project, attached to all networks.

    gateway_ips: dict of {nad_name: {"ip": "10.0.0.1", "cidr": "10.0.0.0/24"}}
    """
    namespace = project_cr["metadata"]["namespace"]
    project_id = project_cr["spec"].get("projectId", namespace)[:8]

    pod_name = f"gateway-{namespace}"

    if gateway_ips:
        net_list = []
        for nad in all_network_nads:
            entry = {"name": nad}
            gw = gateway_ips.get(nad)
            if gw:
                prefix = gw["cidr"].split("/")[1] if "/" in gw["cidr"] else "24"
                entry["ips"] = [f"{gw['ip']}/{prefix}"]
            net_list.append(entry)
        net_annotation = json.dumps(net_list)
    else:
        net_annotation = ",".join(all_network_nads)

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "ownerReferences": [owner_ref(project_cr)],
            "labels": {
                "app": f"troshka-gateway-{project_id}",
                "troshka-role": "gateway",
            },
            "annotations": {
                "k8s.v1.cni.cncf.io/networks": net_annotation
            },
        },
        "spec": {
            "serviceAccountName": "troshka-network",
            "containers": [
                {
                    "name": "gateway",
                    "image": GATEWAY_IMAGE,
                    "securityContext": {
                        "capabilities": {"add": ["NET_ADMIN", "NET_RAW"]},
                        "privileged": False,
                    },
                }
            ],
            "restartPolicy": "Always",
        },
    }
