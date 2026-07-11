VNC_PROXY_IMAGE = "quay.io/redhat-gpte/troshka-vnc-proxy:latest"


def build_vnc_proxy_pod(project_name, namespace):
    pod_name = f"vnc-proxy-{project_name}"

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "labels": {
                "app": f"vnc-proxy-{project_name}",
                "troshka-role": "vnc-proxy",
            },
        },
        "spec": {
            "serviceAccountName": "troshka-vnc",
            "containers": [
                {
                    "name": "vnc-proxy",
                    "image": VNC_PROXY_IMAGE,
                    "ports": [
                        {
                            "containerPort": 8080,
                            "protocol": "TCP",
                        }
                    ],
                    "env": [
                        {"name": "NAMESPACE", "value": namespace},
                        {"name": "LISTEN_PORT", "value": "8080"},
                    ],
                    "resources": {
                        "requests": {
                            "cpu": "100m",
                            "memory": "64Mi",
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
