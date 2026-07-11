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
        "type": "ovn-k8s-cni-overlay",
        "topology": "layer2",
        "subnets": spec["cidr"],
    }
    if spec.get("gateway"):
        config["excludeSubnets"] = f"{spec['gateway']}/32"

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


def build_dnsmasq_pod(network_cr, dnsmasq_config):
    spec = network_cr["spec"]
    name = network_cr["metadata"]["name"]
    namespace = network_cr["metadata"]["namespace"]
    nad_name = f"{name}-nad"

    pod_name = f"dnsmasq-{name}"

    annotations = {"k8s.v1.cni.cncf.io/networks": nad_name}

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


def build_gateway_pod(network_cr, all_network_nads):
    name = network_cr["metadata"]["name"]
    namespace = network_cr["metadata"]["namespace"]
    project_label = namespace

    pod_name = f"gateway-{project_label}"
    net_annotations = ",".join(all_network_nads)

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "ownerReferences": [owner_ref(network_cr)],
            "labels": {
                "app": f"troshka-gateway-{project_label}",
                "troshka-role": "gateway",
            },
            "annotations": {
                "k8s.v1.cni.cncf.io/networks": net_annotations
            },
        },
        "spec": {
            "containers": [
                {
                    "name": "gateway",
                    "image": GATEWAY_IMAGE,
                    "securityContext": {
                        "capabilities": {"add": ["NET_ADMIN", "NET_RAW"]},
                        "privileged": False,
                    },
                    "env": [
                        {
                            "name": "GATEWAY_NETWORKS",
                            "value": net_annotations,
                        },
                    ],
                }
            ],
            "restartPolicy": "Always",
        },
    }
