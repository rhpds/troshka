import json
import os

_IMAGE_TAG = os.environ.get("IMAGE_TAG", "latest")
SUSHY_IMAGE = f"quay.io/redhat-gpte/troshka-bmc:{_IMAGE_TAG}"


def build_bmc_deployment(
    project_name, namespace, bmc_vms, bmc_network_nad, credentials
):
    dep_name = f"bmc-{project_name}"

    vm_map = {}
    bmc_ips = []
    for vm in bmc_vms:
        uuid = vm.get("smbiosUuid", vm.get("vmId", ""))
        kv_name = f"troshka-vm-{vm.get('vmId', '')[:8]}"
        vm_map[uuid] = kv_name
        if vm.get("bmcIp"):
            bmc_ips.append(vm["bmcIp"])

    env = [
        {"name": "SUSHY_VM_MAP", "value": json.dumps(vm_map)},
        {"name": "SUSHY_NAMESPACE", "value": namespace},
        {"name": "SUSHY_LISTEN_PORT", "value": "8000"},
    ]

    if credentials:
        env.append(
            {
                "name": "SUSHY_USERNAME",
                "value": credentials.get("username", "admin"),
            }
        )
        env.append(
            {
                "name": "SUSHY_PASSWORD",
                "value": credentials.get("password", "redhat"),
            }
        )

    labels = {
        "app": "troshka-bmc",
        "troshka-project": project_name,
    }

    if bmc_ips:
        net_annotation = json.dumps([{"name": bmc_network_nad, "ips": bmc_ips}])
    else:
        net_annotation = bmc_network_nad

    annotations = {
        "k8s.v1.cni.cncf.io/networks": net_annotation,
    }

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": dep_name,
            "namespace": namespace,
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
                    "serviceAccountName": "troshka-bmc",
                    "containers": [
                        {
                            "name": "sushy",
                            "image": SUSHY_IMAGE,
                            "imagePullPolicy": "Always",
                            "ports": [
                                {
                                    "containerPort": 8000,
                                    "protocol": "TCP",
                                },
                                {
                                    "containerPort": 8443,
                                    "protocol": "TCP",
                                },
                            ],
                            "env": env,
                            "resources": {
                                "requests": {
                                    "cpu": "100m",
                                    "memory": "128Mi",
                                },
                                "limits": {
                                    "cpu": "500m",
                                    "memory": "256Mi",
                                },
                            },
                        }
                    ],
                },
            },
        },
    }
