import json


SUSHY_IMAGE = "quay.io/redhat-gpte/troshka-sushy:latest"


def build_bmc_pod(
    project_name, namespace, bmc_vms, bmc_network_nad, credentials
):
    pod_name = f"bmc-{project_name}"

    vm_map = {}
    for vm in bmc_vms:
        uuid = vm.get("smbiosUuid", vm.get("vmId", ""))
        kv_name = f"troshka-vm-{vm.get('vmId', '')[:8]}"
        vm_map[uuid] = kv_name

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

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "labels": {
                "app": "troshka-bmc",
                "troshka-project": project_name,
            },
            "annotations": {
                "k8s.v1.cni.cncf.io/networks": bmc_network_nad,
            },
        },
        "spec": {
            "serviceAccountName": "troshka-bmc",
            "containers": [
                {
                    "name": "sushy",
                    "image": SUSHY_IMAGE,
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
            "restartPolicy": "Always",
        },
    }
