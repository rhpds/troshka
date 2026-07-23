import os

from helpers.k8s import owner_ref

_IMAGE_TAG = os.environ.get("IMAGE_TAG", "latest")
VNC_PROXY_IMAGE = f"quay.io/redhat-gpte/troshka-vnc-proxy:{_IMAGE_TAG}"


def build_vnc_proxy_deployment(project_name, namespace, owner_body=None):
    dep_name = f"vnc-proxy-{project_name}"
    labels = {
        "app": f"vnc-proxy-{project_name}",
        "troshka-role": "vnc-proxy",
    }

    dep = {
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
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": "troshka-vnc",
                    "containers": [
                        {
                            "name": "vnc-proxy",
                            "image": VNC_PROXY_IMAGE,
                            "imagePullPolicy": "Always",
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
                                "requests": {"cpu": "100m", "memory": "64Mi"},
                                "limits": {"cpu": "500m", "memory": "256Mi"},
                            },
                        }
                    ],
                },
            },
        },
    }
    if owner_body:
        dep["metadata"]["ownerReferences"] = [owner_ref(owner_body)]
    return dep


def build_vnc_service(project_name, namespace, owner_body=None):
    svc_name = f"vnc-proxy-{project_name}"
    svc = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": svc_name,
            "namespace": namespace,
            "labels": {"app": f"vnc-proxy-{project_name}", "troshka-role": "vnc-proxy"},
        },
        "spec": {
            "type": "ClusterIP",
            "ports": [{"port": 8080, "targetPort": 8080, "protocol": "TCP"}],
            "selector": {"app": f"vnc-proxy-{project_name}"},
        },
    }
    if owner_body:
        svc["metadata"]["ownerReferences"] = [owner_ref(owner_body)]
    return svc


def build_vnc_route(project_name, namespace, owner_body=None):
    route_name = f"vnc-proxy-{project_name}"
    svc_name = f"vnc-proxy-{project_name}"
    route = {
        "apiVersion": "route.openshift.io/v1",
        "kind": "Route",
        "metadata": {
            "name": route_name,
            "namespace": namespace,
            "labels": {"app": f"vnc-proxy-{project_name}", "troshka-role": "vnc-proxy"},
            "annotations": {"haproxy.router.openshift.io/timeout": "3600s"},
        },
        "spec": {
            "to": {"kind": "Service", "name": svc_name},
            "port": {"targetPort": 8080},
            "tls": {
                "termination": "edge",
                "insecureEdgeTerminationPolicy": "Redirect",
            },
        },
    }
    if owner_body:
        route["metadata"]["ownerReferences"] = [owner_ref(owner_body)]
    return route
