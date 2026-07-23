import hashlib
import json
import os
import re

CRD_GROUP = "troshka.redhat.com"

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_PREFIX_RE = re.compile(r"^\d{1,2}$")
CRD_VERSION = "v1alpha1"
_IMAGE_TAG = os.environ.get("IMAGE_TAG", "latest")
TOOLS_IMAGE = f"quay.io/redhat-gpte/troshka-tools:{_IMAGE_TAG}"
DNSMASQ_IMAGE = f"quay.io/redhat-gpte/troshka-dnsmasq:{_IMAGE_TAG}"
GATEWAY_IMAGE = f"quay.io/redhat-gpte/troshka-gateway:{_IMAGE_TAG}"


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


def _dnsmasq_ip_from_cidr(cidr):
    """Derive .2 IP for dnsmasq and prefix from CIDR (gateway gets .1)."""
    if not cidr or "/" not in cidr:
        return "", ""
    parts = cidr.split("/")
    octets = parts[0].split(".")
    octets[3] = "2"
    return ".".join(octets), parts[1]


def build_dnsmasq_deployment(network_cr, dnsmasq_config):
    spec = network_cr["spec"]
    name = network_cr["metadata"]["name"]
    namespace = network_cr["metadata"]["namespace"]
    nad_name = f"{name}-nad"

    dep_name = f"dnsmasq-{name}"

    annotations = {"k8s.v1.cni.cncf.io/networks": nad_name}
    labels = {"app": "troshka-dnsmasq", "troshka-network": name}

    cidr = spec.get("cidr", "")
    dnsmasq_ip, prefix = _dnsmasq_ip_from_cidr(cidr)

    setup_cmd = "true"
    if (
        dnsmasq_ip
        and prefix
        and _IPV4_RE.match(dnsmasq_ip)
        and _PREFIX_RE.match(prefix)
    ):
        setup_cmd = f"ip addr add {dnsmasq_ip}/{prefix} dev net1 && ip link set net1 up"

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": dep_name,
            "namespace": namespace,
            "ownerReferences": [owner_ref(network_cr)],
            "labels": labels,
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": annotations,
                },
                "spec": {
                    "serviceAccountName": "troshka-network",
                    "initContainers": [
                        {
                            "name": "setup-ip",
                            "image": GATEWAY_IMAGE,
                            "imagePullPolicy": "Always",
                            "command": ["sh", "-c", setup_cmd],
                            "securityContext": {"capabilities": {"add": ["NET_ADMIN"]}},
                        }
                    ],
                    "containers": [
                        {
                            "name": "dnsmasq",
                            "image": DNSMASQ_IMAGE,
                            "imagePullPolicy": "Always",
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
                },
            },
        },
    }


def build_exec_deployment(
    project_cr, cluster_nad_name, cidr="10.0.0.0/24", ssh_key_secret=None
):
    """Build an exec deployment for SSH/command execution into project VMs.

    Uses .3 on the subnet (dnsmasq is .2, gateway is .1).
    """
    namespace = project_cr["metadata"]["namespace"]
    name = project_cr["metadata"]["name"]
    project_id = project_cr["spec"].get("projectId", namespace)[:8]

    exec_ip = ""
    prefix = "24"
    if cidr:
        parts = cidr.split("/")
        prefix = parts[1] if len(parts) > 1 else "24"
        octets = parts[0].split(".")
        octets[3] = "3"
        exec_ip = ".".join(octets)

    setup_cmd = "true"
    if exec_ip and _IPV4_RE.match(exec_ip) and _PREFIX_RE.match(prefix):
        setup_cmd = f"ip addr add {exec_ip}/{prefix} dev net1 && ip link set net1 up"

    volumes = [
        {
            "name": "kubeconfig",
            "secret": {
                "secretName": "ocp-kubeconfig",  # pragma: allowlist secret
                "defaultMode": 0o400,
                "optional": True,
            },
        },
    ]
    volume_mounts = [
        {
            "name": "kubeconfig",
            "mountPath": "/root/.kube",
            "readOnly": True,
        },
    ]
    if ssh_key_secret:
        volumes.append(
            {
                "name": "ssh-key",
                "secret": {
                    "secretName": ssh_key_secret,
                    "defaultMode": 0o400,
                },
            }
        )
        volume_mounts.append(
            {
                "name": "ssh-key",
                "mountPath": "/root/.ssh",
                "readOnly": True,
            }
        )

    container = {
        "name": "exec",
        "image": TOOLS_IMAGE,
        "imagePullPolicy": "Always",
        "command": ["sleep", "infinity"],
        "volumeMounts": volume_mounts,
    }

    dns_ip = exec_ip.rsplit(".", 1)[0] + ".2" if exec_ip else ""

    dep_name = f"exec-{project_id}"
    labels = {
        "app": "troshka-exec",
        "troshka-project": name,
    }
    annotations = {
        "k8s.v1.cni.cncf.io/networks": cluster_nad_name,
    }

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": dep_name,
            "namespace": namespace,
            "ownerReferences": [owner_ref(project_cr)],
            "labels": labels,
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": annotations,
                },
                "spec": {
                    "dnsPolicy": "None",
                    "dnsConfig": {
                        "nameservers": [dns_ip] if dns_ip else ["8.8.8.8"],
                    },
                    "serviceAccountName": "troshka-network",
                    "initContainers": [
                        {
                            "name": "setup-ip",
                            "image": GATEWAY_IMAGE,
                            "imagePullPolicy": "Always",
                            "command": ["sh", "-c", setup_cmd],
                            "securityContext": {"capabilities": {"add": ["NET_ADMIN"]}},
                        }
                    ],
                    "containers": [container],
                },
            },
        },
    }
    if volumes:
        deployment["spec"]["template"]["spec"]["volumes"] = volumes
    return deployment


def build_gateway_deployment(project_cr, all_network_nads, gateway_ips=None):
    """Build a single gateway deployment for the project, attached to all networks.

    gateway_ips: dict of {nad_name: {"ip": "10.0.0.1", "cidr": "10.0.0.0/24"}}
    """
    namespace = project_cr["metadata"]["namespace"]
    project_id = project_cr["spec"].get("projectId", namespace)[:8]

    dep_name = f"gateway-{namespace}"
    net_annotation = ",".join(all_network_nads)

    gw_addrs = []
    if gateway_ips:
        for nad in all_network_nads:
            gw = gateway_ips.get(nad)
            if gw:
                prefix = gw["cidr"].split("/")[1] if "/" in gw["cidr"] else "24"
                gw_addrs.append(f"{gw['ip']}/{prefix}")

    labels = {
        "app": f"troshka-gateway-{project_id}",
        "troshka-role": "gateway",
    }
    annotations = {"k8s.v1.cni.cncf.io/networks": net_annotation}

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": dep_name,
            "namespace": namespace,
            "ownerReferences": [owner_ref(project_cr)],
            "labels": labels,
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": annotations,
                },
                "spec": {
                    "serviceAccountName": "troshka-network",
                    "containers": [
                        {
                            "name": "gateway",
                            "image": GATEWAY_IMAGE,
                            "imagePullPolicy": "Always",
                            "securityContext": {
                                "capabilities": {"add": ["NET_ADMIN", "NET_RAW"]},
                                "privileged": True,
                            },
                            "env": [
                                {
                                    "name": "GATEWAY_ADDRS",
                                    "value": ",".join(gw_addrs),
                                },
                            ],
                        }
                    ],
                },
            },
        },
    }
