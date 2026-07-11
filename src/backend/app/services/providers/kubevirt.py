import logging
import os
import time

import yaml

from app.services.providers.base import ProviderDriver

logger = logging.getLogger(__name__)

CRD_GROUP = "troshka.redhat.com"
CRD_VERSION = "v1alpha1"

OPERATOR_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "operator"
)


def _project_ns(provider, project_id):
    creds = provider.get_credentials()
    prefix = creds.get("project_prefix", "troshka-")
    return f"{prefix}{project_id[:8]}"


def _get_k8s_clients(provider):
    from kubernetes import client

    creds = provider.get_credentials()
    config = client.Configuration()
    config.host = creds["api_url"]
    config.api_key = {"authorization": f"Bearer {creds['token']}"}
    config.verify_ssl = creds.get("verify_ssl", False)
    api_client = client.ApiClient(config)
    return (
        client.CustomObjectsApi(api_client),
        client.CoreV1Api(api_client),
        client.ApiClient(config),
    )


def _deploy_operator(provider):
    from kubernetes import client

    custom_api, core_api, api_client = _get_k8s_clients(provider)
    apps_api = client.AppsV1Api(api_client)
    rbac_api = client.RbacAuthorizationV1Api(api_client)
    ext_api = client.ApiextensionsV1Api(api_client)

    creds = provider.get_credentials()
    operator_ns = creds.get("namespace", "troshka-operator")

    operator_dir = os.path.normpath(OPERATOR_DIR)

    crd_files = [
        os.path.join(operator_dir, "crds", "troshkaproject.yaml"),
        os.path.join(operator_dir, "crds", "troshkanetwork.yaml"),
        os.path.join(operator_dir, "crds", "troshkavm.yaml"),
    ]
    for crd_path in crd_files:
        with open(crd_path) as f:
            crd_body = yaml.safe_load(f)
        try:
            ext_api.create_custom_resource_definition(body=crd_body)
            logger.info(f"Created CRD {crd_body['metadata']['name']}")
        except client.exceptions.ApiException as e:
            if e.status == 409:
                ext_api.patch_custom_resource_definition(
                    name=crd_body["metadata"]["name"], body=crd_body
                )
                logger.info(f"Updated CRD {crd_body['metadata']['name']}")
            else:
                raise

    deploy_dir = os.path.join(operator_dir, "deploy")
    manifest_order = [
        "namespace.yaml",
        "serviceaccount.yaml",
        "clusterrole.yaml",
        "clusterrolebinding.yaml",
        "deployment.yaml",
    ]

    for filename in manifest_order:
        path = os.path.join(deploy_dir, filename)
        with open(path) as f:
            body = yaml.safe_load(f)

        kind = body["kind"]
        name = body["metadata"]["name"]
        ns = body["metadata"].get("namespace")

        if ns:
            body["metadata"]["namespace"] = operator_ns
            ns = operator_ns
        if kind == "Namespace":
            body["metadata"]["name"] = operator_ns
            name = operator_ns
        if kind == "ClusterRoleBinding":
            for subj in body.get("subjects", []):
                if subj.get("namespace"):
                    subj["namespace"] = operator_ns

        if kind in ("ClusterRole", "ClusterRoleBinding"):
            try:
                if kind == "ClusterRole":
                    rbac_api.read_cluster_role(name=name)
                else:
                    rbac_api.read_cluster_role_binding(name=name)
                logger.info(f"{kind} {name} already exists, skipping")
                continue
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    pass
                else:
                    raise

        try:
            if kind == "Namespace":
                core_api.create_namespace(body=body)
            elif kind == "ServiceAccount":
                core_api.create_namespaced_service_account(namespace=ns, body=body)
            elif kind == "ClusterRole":
                rbac_api.create_cluster_role(body=body)
            elif kind == "ClusterRoleBinding":
                rbac_api.create_cluster_role_binding(body=body)
            elif kind == "Deployment":
                apps_api.create_namespaced_deployment(namespace=ns, body=body)
            logger.info(f"Created {kind} {name}")
        except client.exceptions.ApiException as e:
            if e.status == 409:
                if kind == "Deployment":
                    apps_api.patch_namespaced_deployment(
                        name=name, namespace=ns, body=body
                    )
                logger.info(f"Updated {kind} {name}")
            else:
                raise

    logger.info("Operator deployed successfully")


class KubeVirtDriver(ProviderDriver):
    def provision_host(
        self, provider, host_id, instance_type, storage_size_gb, **kwargs
    ):
        _deploy_operator(provider)

        custom_api, core_api, _ = _get_k8s_clients(provider)
        creds = provider.get_credentials()
        api_url = creds["api_url"]

        total_vcpus = 0
        total_ram_mb = 0
        try:
            nodes = core_api.list_node()
            for node in nodes.items:
                alloc = node.status.allocatable or {}
                cpu_str = alloc.get("cpu", "0")
                mem_str = alloc.get("memory", "0")
                total_vcpus += int(cpu_str)
                if mem_str.endswith("Ki"):
                    total_ram_mb += int(mem_str[:-2]) // 1024
                elif mem_str.endswith("Mi"):
                    total_ram_mb += int(mem_str[:-2])
                elif mem_str.endswith("Gi"):
                    total_ram_mb += int(mem_str[:-2]) * 1024
        except Exception as e:
            logger.warning(f"Failed to query cluster capacity: {e}")
            total_vcpus = 256
            total_ram_mb = 1024 * 1024

        return {
            "host_id": host_id,
            "instance_id": api_url,
            "instance_type": "kubevirt-cluster",
            "public_ip": api_url.replace("https://", "").split(":")[0],
            "private_ip": api_url.replace("https://", "").split(":")[0],
            "total_vcpus": total_vcpus,
            "total_ram_mb": total_ram_mb,
            "private_key": "",
            "key_pair_name": "",
            "storage_size_gb": storage_size_gb or 0,
            "max_eips": 0,
        }

    def terminate_host(self, provider, instance_id):
        pass

    def get_host_status(self, provider, instance_id):
        try:
            _, core_api, _ = _get_k8s_clients(provider)
            core_api.list_namespace(limit=1)
            return {
                "instance_id": instance_id,
                "state": "running",
                "public_ip": instance_id.replace("https://", "").split(":")[0],
                "private_ip": instance_id.replace("https://", "").split(":")[0],
            }
        except Exception:
            return None

    def resize_host(self, provider, instance_id, new_instance_type):
        return {}

    def extend_host_storage(self, provider, host, db, increment_gb=None):
        return {}

    def get_host_powerstate(self, provider, instance_id):
        return "running"

    def start_host(self, provider, instance_id):
        pass

    def stop_host(self, provider, instance_id):
        pass

    def setup_console(self, provider, base_domain):
        return {
            "console_base_domain": base_domain,
            "console_zone_id": "",
            "console_nameservers": [],
        }

    def create_console_record(self, provider, host, hostname, ip_address):
        custom_api, core_api, _ = _get_k8s_clients(provider)
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")

        svc_name = f"vnc-{hostname}"
        svc_body = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": svc_name,
                "namespace": namespace,
                "labels": {"app": "troshka-vnc", "troshka-host": hostname},
            },
            "spec": {
                "type": "ClusterIP",
                "ports": [{"port": 8080, "targetPort": 8080, "protocol": "TCP"}],
                "selector": {"app": f"vnc-proxy-{hostname}"},
            },
        }
        try:
            core_api.create_namespaced_service(namespace=namespace, body=svc_body)
        except Exception as e:
            if "AlreadyExists" not in str(e):
                raise

        route_body = {
            "apiVersion": "route.openshift.io/v1",
            "kind": "Route",
            "metadata": {
                "name": f"console-{hostname}",
                "namespace": namespace,
                "labels": {"app": "troshka-vnc", "troshka-host": hostname},
                "annotations": {"haproxy.router.openshift.io/timeout": "3600s"},
            },
            "spec": {
                "host": hostname,
                "to": {"kind": "Service", "name": svc_name},
                "port": {"targetPort": 8080},
                "tls": {
                    "termination": "edge",
                    "insecureEdgeTerminationPolicy": "Redirect",
                },
            },
        }
        try:
            custom_api.create_namespaced_custom_object(
                group="route.openshift.io",
                version="v1",
                namespace=namespace,
                plural="routes",
                body=route_body,
            )
        except Exception as e:
            if "AlreadyExists" not in str(e):
                raise

    def delete_console_record(self, provider, host, hostname, ip_address):
        custom_api, core_api, _ = _get_k8s_clients(provider)
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        try:
            core_api.delete_namespaced_service(
                name=f"vnc-{hostname}", namespace=namespace
            )
        except Exception:
            pass
        try:
            custom_api.delete_namespaced_custom_object(
                group="route.openshift.io",
                version="v1",
                namespace=namespace,
                plural="routes",
                name=f"console-{hostname}",
            )
        except Exception:
            pass

    def delete_console(self, provider):
        custom_api, core_api, _ = _get_k8s_clients(provider)
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        try:
            svcs = core_api.list_namespaced_service(
                namespace=namespace, label_selector="app=troshka-vnc"
            )
            for svc in svcs.items:
                core_api.delete_namespaced_service(
                    name=svc.metadata.name, namespace=namespace
                )
        except Exception:
            pass
        try:
            routes = custom_api.list_namespaced_custom_object(
                group="route.openshift.io",
                version="v1",
                namespace=namespace,
                plural="routes",
                label_selector="app=troshka-vnc",
            )
            for route in routes.get("items", []):
                custom_api.delete_namespaced_custom_object(
                    group="route.openshift.io",
                    version="v1",
                    namespace=namespace,
                    plural="routes",
                    name=route["metadata"]["name"],
                )
        except Exception:
            pass

    def allocate_eip(self, provider, host, eip_id):
        custom_api, core_api, _ = _get_k8s_clients(provider)
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")

        svc_name = f"troshka-eip-{eip_id[:8]}"
        svc_body = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": svc_name,
                "namespace": namespace,
                "labels": {
                    "app": "troshka-eip",
                    "troshka-eip-id": eip_id[:8],
                },
            },
            "spec": {
                "type": "LoadBalancer",
                "ports": [{"port": 443, "targetPort": 443, "protocol": "TCP"}],
                "selector": {"app": f"troshka-gateway-{eip_id[:8]}"},
            },
        }
        core_api.create_namespaced_service(namespace=namespace, body=svc_body)

        for _ in range(60):
            svc = core_api.read_namespaced_service(name=svc_name, namespace=namespace)
            ingress = svc.status.load_balancer.ingress
            if ingress and ingress[0].ip:
                return {"public_ip": ingress[0].ip, "allocation_id": svc_name}
            time.sleep(2)

        raise TimeoutError(f"MetalLB did not assign IP to {svc_name} within 120s")

    def associate_eip(self, provider, host, allocation_id):
        return {}

    def release_eip(self, provider, allocation_id, namespace=None):
        _, core_api, _ = _get_k8s_clients(provider)
        creds = provider.get_credentials()
        ns = namespace or creds.get("namespace", "troshka")
        try:
            core_api.delete_namespaced_service(name=allocation_id, namespace=ns)
        except Exception:
            pass

    def update_eip_ports(self, provider, host, allocation_id, ports):
        _, core_api, _ = _get_k8s_clients(provider)
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        svc_ports = [
            {
                "port": p["port"],
                "targetPort": p.get("target_port", p["port"]),
                "protocol": "TCP",
            }
            for p in ports
        ]
        core_api.patch_namespaced_service(
            name=allocation_id,
            namespace=namespace,
            body={"spec": {"ports": svc_ports}},
        )

    def create_route_access(
        self, provider, host, project_id, vm_name, int_ip, port, target_port=None
    ):
        custom_api, core_api, _ = _get_k8s_clients(provider)
        creds = provider.get_credentials()
        namespace = _project_ns(provider, project_id)
        tgt_port = target_port or port

        svc_name = f"rt-{vm_name}-{port}"[:63]
        route_name = svc_name

        svc_body = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": svc_name,
                "namespace": namespace,
                "labels": {
                    "app": "troshka-route-access",
                    "troshka-project": project_id[:8],
                },
            },
            "spec": {
                "type": "ClusterIP",
                "ports": [{"port": port, "targetPort": tgt_port, "protocol": "TCP"}],
                "selector": {"app": f"troshka-gateway-{project_id[:8]}"},
            },
        }
        try:
            core_api.create_namespaced_service(namespace=namespace, body=svc_body)
        except Exception as e:
            if "AlreadyExists" not in str(e):
                raise

        route_body = {
            "apiVersion": "route.openshift.io/v1",
            "kind": "Route",
            "metadata": {
                "name": route_name,
                "namespace": namespace,
                "labels": {
                    "app": "troshka-route-access",
                    "troshka-project": project_id[:8],
                },
                "annotations": {"haproxy.router.openshift.io/timeout": "3600s"},
            },
            "spec": {
                "to": {"kind": "Service", "name": svc_name},
                "port": {"targetPort": tgt_port},
                "tls": {
                    "termination": "edge",
                    "insecureEdgeTerminationPolicy": "Redirect",
                },
            },
        }
        try:
            result = custom_api.create_namespaced_custom_object(
                group="route.openshift.io",
                version="v1",
                namespace=namespace,
                plural="routes",
                body=route_body,
            )
            hostname = result.get("spec", {}).get("host", "")
        except Exception as e:
            if "AlreadyExists" not in str(e):
                raise
            hostname = ""

        return {
            "hostname": hostname,
            "route_name": route_name,
            "service_name": svc_name,
        }

    def delete_route_access(self, provider, project_id, namespace=None):
        custom_api, core_api, _ = _get_k8s_clients(provider)
        ns = namespace or _project_ns(provider, project_id)
        label = f"troshka-project={project_id[:8]}"
        try:
            svcs = core_api.list_namespaced_service(namespace=ns, label_selector=label)
            for svc in svcs.items:
                core_api.delete_namespaced_service(name=svc.metadata.name, namespace=ns)
        except Exception:
            pass
        try:
            routes = custom_api.list_namespaced_custom_object(
                group="route.openshift.io",
                version="v1",
                namespace=ns,
                plural="routes",
                label_selector=label,
            )
            for route in routes.get("items", []):
                custom_api.delete_namespaced_custom_object(
                    group="route.openshift.io",
                    version="v1",
                    namespace=ns,
                    plural="routes",
                    name=route["metadata"]["name"],
                )
        except Exception:
            pass

    def deploy_project(self, provider, project_id, topology, s3_config, **kwargs):
        custom_api, core_api, _ = _get_k8s_clients(provider)
        namespace = _project_ns(provider, project_id)

        from kubernetes import client as k8s_client

        try:
            core_api.create_namespace(
                body=k8s_client.V1Namespace(
                    metadata=k8s_client.V1ObjectMeta(
                        name=namespace,
                        labels={"app": "troshka", "troshka-project": project_id[:8]},
                    )
                )
            )
        except Exception as e:
            if "AlreadyExists" not in str(e):
                raise

        if s3_config.get("credentials_secret_data"):
            try:
                core_api.create_namespaced_secret(
                    namespace=namespace,
                    body=k8s_client.V1Secret(
                        metadata=k8s_client.V1ObjectMeta(name="s3-credentials"),
                        string_data=s3_config["credentials_secret_data"],
                    ),
                )
            except Exception as e:
                if "AlreadyExists" not in str(e):
                    raise

        project_cr = {
            "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
            "kind": "TroshkaProject",
            "metadata": {
                "name": f"project-{project_id[:8]}",
                "namespace": namespace,
            },
            "spec": {
                "projectId": project_id,
                "topology": topology,
                "s3Config": {
                    "bucket": s3_config.get("bucket", ""),
                    "endpoint": s3_config.get("endpoint", ""),
                    "region": s3_config.get("region", ""),
                    "credentialsSecret": "s3-credentials",  # pragma: allowlist secret
                },
                "action": "deploy",
            },
        }
        if kwargs.get("common_password"):
            project_cr["spec"]["commonPassword"] = kwargs["common_password"]
        if kwargs.get("registry_credentials"):
            project_cr["spec"]["registryCredentials"] = kwargs["registry_credentials"]

        custom_api.create_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural="troshkaprojects",
            body=project_cr,
        )
        return f"project-{project_id[:8]}"

    def destroy_project(self, provider, project_id):
        custom_api, core_api, _ = _get_k8s_clients(provider)
        namespace = _project_ns(provider, project_id)
        try:
            custom_api.delete_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural="troshkaprojects",
                name=f"project-{project_id[:8]}",
            )
        except Exception:
            pass
        try:
            core_api.delete_namespace(name=namespace)
        except Exception:
            pass

    def get_project_status(self, provider, project_id):
        custom_api, _ = _get_k8s_clients(provider)
        namespace = _project_ns(provider, project_id)
        try:
            cr = custom_api.get_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural="troshkaprojects",
                name=f"project-{project_id[:8]}",
            )
            return cr.get("status", {})
        except Exception:
            return {}

    def get_vm_states(self, provider, project_id):
        status = self.get_project_status(provider, project_id)
        return status.get("vmStates", {})
