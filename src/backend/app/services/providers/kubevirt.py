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


def _ensure_s3_secret(provider, namespace, s3_config):
    """Create or update s3-credentials Secret in the project namespace."""
    from kubernetes import client as k8s_client

    _, core_api, _ = _get_k8s_clients(provider)
    secret_data = {
        "AWS_ACCESS_KEY_ID": s3_config.get("access_key_id", ""),
        "AWS_SECRET_ACCESS_KEY": s3_config.get("secret_access_key", ""),
        "AWS_DEFAULT_REGION": s3_config.get("region", "us-east-1"),
    }
    endpoint = s3_config.get("endpoint_url", "")
    if endpoint:
        secret_data["AWS_ENDPOINT_URL"] = endpoint

    try:
        core_api.create_namespaced_secret(
            namespace=namespace,
            body=k8s_client.V1Secret(
                metadata=k8s_client.V1ObjectMeta(name="s3-credentials"),
                string_data=secret_data,
            ),
        )
    except Exception as e:
        if "AlreadyExists" in str(e):
            core_api.patch_namespaced_secret(
                name="s3-credentials",
                namespace=namespace,
                body=k8s_client.V1Secret(string_data=secret_data),
            )
        else:
            raise


def _deploy_operator(provider):
    from kubernetes import client
    from kubernetes.client.exceptions import ApiException

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
        except ApiException as e:
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
                    logger.info(f"ClusterRole {name} already exists, skipping")
                    continue
                else:
                    rbac_api.patch_cluster_role_binding(name=name, body=body)
                    logger.info(f"ClusterRoleBinding {name} patched")
                    continue
            except ApiException as e:
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
        except ApiException as e:
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
            for node in getattr(nodes, "items", []):
                labels = node.metadata.labels or {}
                taints = node.spec.taints or []

                is_worker = "node-role.kubernetes.io/worker" in labels
                is_unschedulable = node.spec.unschedulable or False
                has_noschedule = any(t.effect == "NoSchedule" for t in taints)
                if not is_worker or is_unschedulable or has_noschedule:
                    continue

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

        storage_gb = 0
        try:
            toolbox_pods = core_api.list_namespaced_pod(
                namespace="openshift-storage",
                label_selector="app=rook-ceph-tools",
            )
            if getattr(toolbox_pods, "items", []):
                from kubernetes.stream import stream as k8s_stream

                resp = k8s_stream(
                    core_api.connect_get_namespaced_pod_exec,
                    getattr(toolbox_pods, "items", [])[0].metadata.name,
                    "openshift-storage",
                    command=["ceph", "df", "-f", "json"],
                    stderr=True,
                    stdout=True,
                    stdin=False,
                    tty=False,
                    _preload_content=False,
                )
                stdout = ""
                while resp.is_open():
                    resp.update(timeout=10)
                    if resp.peek_stdout():
                        stdout += resp.read_stdout()
                    if resp.peek_stderr():
                        resp.read_stderr()
                resp.close()

                import json

                ceph_df = json.loads(stdout)
                stats = ceph_df.get("stats", {})
                total_bytes = stats.get("total_bytes", 0)
                storage_gb = int(total_bytes / (1024**3))
        except Exception as e:
            logger.warning(f"Failed to query Ceph storage capacity: {e}")

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
            "storage_size_gb": storage_gb or storage_size_gb or 0,
            "max_eips": 0,
        }

    def terminate_host(self, provider, instance_id):
        pass

    def get_host_status(self, provider, instance_id):
        try:
            _, core_api, _ = _get_k8s_clients(provider)
            creds = provider.get_credentials()
            op_ns = creds.get("namespace", "troshka-operator")
            core_api.read_namespace(name=op_ns)
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
            for svc in getattr(svcs, "items", []):
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
            for route in dict(routes).get("items", []):  # type: ignore[call-overload]
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
            ingress = svc.status.load_balancer.ingress  # type: ignore[union-attr]
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
            hostname = dict(result).get("spec", {}).get("host", "")  # type: ignore[call-overload]
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
            for svc in getattr(svcs, "items", []):
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
            for route in dict(routes).get("items", []):  # type: ignore[call-overload]
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
        if kwargs.get("exec_ssh_key"):
            project_cr["spec"]["execSshKey"] = kwargs["exec_ssh_key"]
            logger.info(
                "deploy_project: execSshKey set, length=%d",
                len(kwargs["exec_ssh_key"]),
            )

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

        # Delete VM CRs first — stops KubeVirt from recreating VMIs
        try:
            vms = custom_api.list_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
            )
            for vm in dict(vms).get("items", []):  # type: ignore[call-overload]
                try:
                    custom_api.delete_namespaced_custom_object(
                        group="kubevirt.io",
                        version="v1",
                        namespace=namespace,
                        plural="virtualmachines",
                        name=vm["metadata"]["name"],
                        grace_period_seconds=0,
                    )
                except Exception:
                    pass
        except Exception:
            pass

        # Force-delete VMIs and virt-launcher pods
        try:
            vmis = custom_api.list_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachineinstances",
            )
            for vmi in dict(vmis).get("items", []):  # type: ignore[call-overload]
                try:
                    custom_api.delete_namespaced_custom_object(
                        group="kubevirt.io",
                        version="v1",
                        namespace=namespace,
                        plural="virtualmachineinstances",
                        name=vmi["metadata"]["name"],
                        grace_period_seconds=0,
                    )
                except Exception:
                    pass
        except Exception:
            pass

        # Force-delete virt-launcher pods directly
        try:
            pods = core_api.list_namespaced_pod(
                namespace=namespace,
                label_selector="kubevirt.io=virt-launcher",
            )
            for pod in getattr(pods, "items", []):
                try:
                    core_api.delete_namespaced_pod(
                        name=pod.metadata.name,
                        namespace=namespace,
                        grace_period_seconds=0,
                    )
                except Exception:
                    pass
        except Exception:
            pass

        # Wait for virt-launcher pods and VMIs to be fully gone — ghost records
        # in virt-handler linger until the VMI deletion is processed, causing
        # "can not add ghost record with differing UID" on rapid redeploys
        for _ in range(30):
            try:
                pods = core_api.list_namespaced_pod(
                    namespace=namespace,
                    label_selector="kubevirt.io=virt-launcher",
                )
                vmis = custom_api.list_namespaced_custom_object(
                    group="kubevirt.io",
                    version="v1",
                    namespace=namespace,
                    plural="virtualmachineinstances",
                )
                if not getattr(pods, "items", []) and not dict(vmis).get("items", []):  # type: ignore[call-overload]
                    break
            except Exception:
                break
            time.sleep(2)

        # Delete all Jobs (recert, guestfish, export)
        try:
            from kubernetes import client as _kc

            batch_api = _kc.BatchV1Api(_kc.ApiClient(_kc.Configuration()))
            # Reuse the same api_client
            _, _, api_client = _get_k8s_clients(provider)
            batch_api = _kc.BatchV1Api(api_client)
            jobs = batch_api.list_namespaced_job(namespace=namespace)
            for job in getattr(jobs, "items", []):
                try:
                    batch_api.delete_namespaced_job(
                        name=job.metadata.name,
                        namespace=namespace,
                        propagation_policy="Background",
                    )
                except Exception:
                    pass
        except Exception:
            pass

        # Delete TroshkaProject CR
        cr_name = f"project-{project_id[:8]}"
        try:
            custom_api.delete_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural="troshkaprojects",
                name=cr_name,
            )
        except Exception:
            pass

        # Wait for TroshkaProject CR to be fully deleted (finalizers may take time)
        for _ in range(30):
            try:
                custom_api.get_namespaced_custom_object(
                    group=CRD_GROUP,
                    version=CRD_VERSION,
                    namespace=namespace,
                    plural="troshkaprojects",
                    name=cr_name,
                )
                time.sleep(2)
            except Exception:
                break

        try:
            core_api.delete_namespace(name=namespace)
        except Exception:
            pass

    def get_project_status(self, provider, project_id):
        custom_api, _, _ = _get_k8s_clients(provider)
        namespace = _project_ns(provider, project_id)
        try:
            cr = custom_api.get_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural="troshkaprojects",
                name=f"project-{project_id[:8]}",
            )
            s = dict(cr).get("status", {})  # type: ignore[call-overload]
            return s if isinstance(s, dict) else {}
        except Exception:
            return {}

    def get_vm_states(self, provider, project_id):
        status = self.get_project_status(provider, project_id)
        return status.get("vmStates", {})


def kubevirt_exec_guest_agent(provider, project_id, vm_id, command, timeout=600):
    """Execute command via qemu-guest-agent inside the virt-launcher pod."""
    import json
    import base64

    _, core_v1, _ = _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)
    vm_name = f"troshka-vm-{vm_id[:8]}"

    pod_list: list = getattr(
        core_v1.list_namespaced_pod(
            namespace, label_selector=f"vm.kubevirt.io/name={vm_name}"
        ),
        "items",
        [],
    )
    launcher = None
    for p in pod_list:
        if p.metadata.name.startswith("virt-launcher-") and p.status.phase == "Running":
            launcher = p
            break
    if not launcher:
        raise RuntimeError(f"No running virt-launcher pod for {vm_name}")

    from kubernetes.stream import stream as k8s_stream

    def _pod_exec_raw(pod_name, ns, cmd, req_timeout=30):
        """Exec in pod and return raw stdout (not Python-parsed)."""
        ws = k8s_stream(
            core_v1.connect_get_namespaced_pod_exec,
            pod_name,
            ns,
            container="compute",
            command=cmd,
            stderr=True,
            stdout=True,
            stdin=False,
            tty=False,
            _preload_content=False,
            _request_timeout=req_timeout,
        )
        out = ""
        while ws.is_open():
            ws.update(timeout=req_timeout)
            if ws.peek_stdout():
                out += ws.read_stdout()
            if ws.peek_stderr():
                ws.read_stderr()
        ws.close()
        return out

    # Discover the libvirt domain name inside the pod
    resp = k8s_stream(
        core_v1.connect_get_namespaced_pod_exec,
        launcher.metadata.name,
        namespace,
        container="compute",
        command=["virsh", "list", "--name"],
        stderr=True,
        stdout=True,
        stdin=False,
        tty=False,
        _preload_content=True,
        _request_timeout=30,
    )
    domain = resp.strip().split("\n")[0].strip()
    if not domain:
        raise RuntimeError("No libvirt domain found in virt-launcher pod")

    # Check guest agent availability
    check_resp = _pod_exec_raw(
        launcher.metadata.name,
        namespace,
        [
            "virsh",
            "qemu-agent-command",
            domain,
            '{"execute":"guest-info"}',
            "--timeout",
            "10",
        ],
    )
    if "error" in check_resp.lower() and "guest agent" in check_resp.lower():
        raise RuntimeError(f"Guest agent not available: {check_resp}")

    try:
        info = json.loads(check_resp)
        cmds = info.get("return", {}).get("supported_commands", [])
        exec_cmd = next((c for c in cmds if c.get("name") == "guest-exec"), None)
        if exec_cmd and not exec_cmd.get("enabled", False):
            raise RuntimeError("guest-exec is disabled (blocked by guest agent config)")
    except (json.JSONDecodeError, StopIteration):
        pass

    # Execute command
    exec_payload = json.dumps(
        {
            "execute": "guest-exec",
            "arguments": {
                "path": "/bin/sh",
                "arg": ["-c", command],
                "capture-output": True,
            },
        }
    )
    exec_resp = _pod_exec_raw(
        launcher.metadata.name,
        namespace,
        ["virsh", "qemu-agent-command", domain, exec_payload, "--timeout", "10"],
    )
    parsed = json.loads(exec_resp)
    pid = parsed.get("return", {}).get("pid")
    if pid is None:
        raise RuntimeError(f"No PID in guest-exec response: {exec_resp}")

    # Poll for completion
    import time

    status_payload = json.dumps(
        {
            "execute": "guest-exec-status",
            "arguments": {"pid": pid},
        }
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        sr = _pod_exec_raw(
            launcher.metadata.name,
            namespace,
            ["virsh", "qemu-agent-command", domain, status_payload, "--timeout", "10"],
        )
        status = json.loads(sr).get("return", {})
        if status.get("exited"):
            stdout = ""
            stderr = ""
            if status.get("out-data"):
                stdout = base64.b64decode(status["out-data"]).decode(
                    "utf-8", errors="replace"
                )
            if status.get("err-data"):
                stderr = base64.b64decode(status["err-data"]).decode(
                    "utf-8", errors="replace"
                )
            return {
                "output": stdout,
                "error": stderr,
                "exit_code": status.get("exitcode", -1),
                "method": "guest-agent",
            }
        time.sleep(0.5)

    raise RuntimeError(f"guest-exec timed out after {timeout}s (pid={pid})")


def _find_exec_pod(core_v1, namespace, project_id):
    """Find the exec pod for a project, falling back to dnsmasq pod."""
    project_name = f"project-{project_id[:8]}"
    exec_pods: list = getattr(
        core_v1.list_namespaced_pod(
            namespace,
            label_selector=f"app=troshka-exec,troshka-project={project_name}",
        ),
        "items",
        [],
    )
    for p in exec_pods:
        if p.status.phase == "Running":
            return p
    dns_pods: list = getattr(
        core_v1.list_namespaced_pod(namespace, label_selector="app=troshka-dnsmasq"),
        "items",
        [],
    )
    for p in dns_pods:
        if p.status.phase == "Running":
            return p
    return None


def kubevirt_exec_ssh(
    provider, project_id, vm_id, vm_ip, username, password, command, timeout=600
):
    """Execute command via SSH from the exec pod (or dnsmasq pod fallback)."""
    _, core_v1, _ = _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)

    exec_pod = _find_exec_pod(core_v1, namespace, project_id)
    if not exec_pod:
        raise RuntimeError("No running exec pod found")

    if not vm_ip:
        raise RuntimeError("No VM IP for SSH exec")
    if not password:
        raise RuntimeError(
            "No password for SSH exec (key auth not supported on KubeVirt)"
        )

    from kubernetes.stream import stream as k8s_stream

    ssh_cmd = [
        "sshpass",
        "-p",
        password,
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        "-o",
        f"ConnectTimeout={min(timeout, 10)}",
        f"{username}@{vm_ip}",
        command,
    ]
    resp = k8s_stream(
        core_v1.connect_get_namespaced_pod_exec,
        exec_pod.metadata.name,
        namespace,
        command=ssh_cmd,
        stderr=True,
        stdout=True,
        stdin=False,
        tty=False,
        _preload_content=True,
        _request_timeout=timeout + 10,
    )
    return {
        "output": resp,
        "error": "",
        "exit_code": 0,
        "method": "ssh",
    }


_CHAR_TO_KEYS = {}
for _c in "abcdefghijklmnopqrstuvwxyz":
    _CHAR_TO_KEYS[_c] = [f"KEY_{_c.upper()}"]
    _CHAR_TO_KEYS[_c.upper()] = ["KEY_LEFTSHIFT", f"KEY_{_c.upper()}"]
for _c in "1234567890":
    _CHAR_TO_KEYS[_c] = [f"KEY_{_c}"]
_CHAR_TO_KEYS.update(
    {
        "!": ["KEY_LEFTSHIFT", "KEY_1"],
        "@": ["KEY_LEFTSHIFT", "KEY_2"],
        "#": ["KEY_LEFTSHIFT", "KEY_3"],
        "$": ["KEY_LEFTSHIFT", "KEY_4"],
        "%": ["KEY_LEFTSHIFT", "KEY_5"],
        "^": ["KEY_LEFTSHIFT", "KEY_6"],
        "&": ["KEY_LEFTSHIFT", "KEY_7"],
        "*": ["KEY_LEFTSHIFT", "KEY_8"],
        "(": ["KEY_LEFTSHIFT", "KEY_9"],
        ")": ["KEY_LEFTSHIFT", "KEY_0"],
        " ": ["KEY_SPACE"],
        "\n": ["KEY_ENTER"],
        "\t": ["KEY_TAB"],
        "-": ["KEY_MINUS"],
        "=": ["KEY_EQUAL"],
        "[": ["KEY_LEFTBRACE"],
        "]": ["KEY_RIGHTBRACE"],
        "\\": ["KEY_BACKSLASH"],
        ";": ["KEY_SEMICOLON"],
        "'": ["KEY_APOSTROPHE"],
        "`": ["KEY_GRAVE"],
        ",": ["KEY_COMMA"],
        ".": ["KEY_DOT"],
        "/": ["KEY_SLASH"],
        "_": ["KEY_LEFTSHIFT", "KEY_MINUS"],
        "+": ["KEY_LEFTSHIFT", "KEY_EQUAL"],
        "{": ["KEY_LEFTSHIFT", "KEY_LEFTBRACE"],
        "}": ["KEY_LEFTSHIFT", "KEY_RIGHTBRACE"],
        "|": ["KEY_LEFTSHIFT", "KEY_BACKSLASH"],
        ":": ["KEY_LEFTSHIFT", "KEY_SEMICOLON"],
        '"': ["KEY_LEFTSHIFT", "KEY_APOSTROPHE"],
        "~": ["KEY_LEFTSHIFT", "KEY_GRAVE"],
        "<": ["KEY_LEFTSHIFT", "KEY_COMMA"],
        ">": ["KEY_LEFTSHIFT", "KEY_DOT"],
        "?": ["KEY_LEFTSHIFT", "KEY_SLASH"],
    }
)


def kubevirt_exec_vnc(
    provider, project_id, vm_id, username, password, command, timeout=600
):
    """Execute command via VNC console: virsh send-key + screenshot + OCR.

    Screenshot taken in virt-launcher pod, OCR runs in the exec/tools pod
    (which has tesseract installed).
    """
    import re
    import time

    if not password:
        raise RuntimeError("Password required for VNC console exec")

    _, core_v1, _ = _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)
    vm_name = f"troshka-vm-{vm_id[:8]}"

    all_pods: list = getattr(core_v1.list_namespaced_pod(namespace), "items", [])
    launcher = None
    exec_pod = None
    for p in all_pods:
        if not p.status or p.status.phase != "Running":
            continue
        if p.metadata.name.startswith("virt-launcher-") and vm_name in p.metadata.name:
            launcher = p
        if p.metadata.name.startswith("exec-"):
            exec_pod = p
    if not launcher:
        raise RuntimeError(f"No running virt-launcher pod for {vm_name}")
    if not exec_pod:
        raise RuntimeError("No running exec pod for VNC OCR")

    from kubernetes.stream import stream as k8s_stream

    def _launcher_exec(cmd, req_timeout=15):
        ws = k8s_stream(
            core_v1.connect_get_namespaced_pod_exec,
            launcher.metadata.name,
            namespace,
            container="compute",
            command=cmd,
            stderr=True,
            stdout=True,
            stdin=False,
            tty=False,
            _preload_content=True,
            _request_timeout=req_timeout,
        )
        return ws.strip() if isinstance(ws, str) else ""

    def _tools_exec(cmd, req_timeout=15):
        ws = k8s_stream(
            core_v1.connect_get_namespaced_pod_exec,
            exec_pod.metadata.name,
            namespace,
            container="exec",
            command=cmd,
            stderr=True,
            stdout=True,
            stdin=False,
            tty=False,
            _preload_content=True,
            _request_timeout=req_timeout,
        )
        return ws.strip() if isinstance(ws, str) else ""

    resp = _launcher_exec(["virsh", "-c", "qemu:///session", "list", "--name"])
    domain = resp.split("\n")[0].strip()
    if not domain:
        raise RuntimeError("No libvirt domain found in virt-launcher pod")

    def _send_keys(*keys):
        _launcher_exec(
            ["virsh", "-c", "qemu:///session", "send-key", domain] + list(keys)
        )

    def _send_text(text):
        for ch in text:
            keys = _CHAR_TO_KEYS.get(ch)
            if keys:
                _send_keys(*keys)

    def _screenshot_ocr():
        img_path = "/tmp/troshka-screen.ppm"
        _launcher_exec(
            ["virsh", "-c", "qemu:///session", "screenshot", domain, img_path]
        )
        b64 = _launcher_exec(["base64", "-w0", img_path], req_timeout=10)
        _launcher_exec(["rm", "-f", img_path])
        if not b64:
            return ""
        # Write base64 to file in tools pod then decode + OCR
        # (avoids shell arg length limits on large PPM screenshots)
        _tools_exec(
            ["bash", "-c", f"cat > /tmp/screen.b64 << 'ENDOFB64'\n{b64}\nENDOFB64"],
            req_timeout=10,
        )
        result = _tools_exec(
            [
                "bash",
                "-c",
                "base64 -d /tmp/screen.b64 | tesseract stdin stdout 2>/dev/null;"
                " rm -f /tmp/screen.b64",
            ],
            req_timeout=15,
        )
        return result

    def _detect_state(ocr_text):
        text = ocr_text.strip()
        if not text or len(text) < 3:
            return "unknown"
        last_lines = "\n".join(text.split("\n")[-5:])
        if re.search(r"login\s*:?\s*$", last_lines, re.IGNORECASE | re.MULTILINE):
            return "login"
        if re.search(r"[Pp]ass[wvu]ord\s*:?\s*$", last_lines, re.MULTILINE):
            return "password"
        if re.search(r"[\]$#~]\s*$", last_lines, re.MULTILINE):
            return "shell"
        return "unknown"

    # Switch to TTY3 to avoid graphical desktop
    _send_keys("KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_F3")
    time.sleep(2)

    # Login loop
    logged_in = False
    for attempt in range(4):
        ocr = _screenshot_ocr()
        state = _detect_state(ocr)

        if state == "shell":
            logged_in = True
            break
        if state == "unknown":
            _send_keys("KEY_ENTER")
            time.sleep(1)
            continue
        if state == "login":
            _send_text(username + "\n")
            time.sleep(2)
            continue
        if state == "password":
            _send_text(password + "\n")
            time.sleep(3)
            continue

    if not logged_in:
        return {
            "output": "",
            "error": "Could not reach shell prompt via VNC console",
            "exit_code": None,
            "method": "vnc",
        }

    # Clear screen and send command with markers
    _send_text("clear\n")
    time.sleep(0.5)
    wrapped = f"echo TROSHKA_BEGIN; {command} 2>&1; echo TROSHKA_EXIT $?"
    _send_text(wrapped + "\n")

    # Poll for output markers
    ocr = ""
    deadline = time.time() + min(timeout, 60)
    while time.time() < deadline:
        time.sleep(2)
        ocr = _screenshot_ocr()
        if "TROSHKA_EXIT" in ocr:
            break

    # Extract output between markers
    m = re.search(r"TROSHKA_BEGIN\s*\n(.*?)TROSHKA_EXIT\s*(\d+)?", ocr, re.DOTALL)
    if m:
        output = m.group(1).strip()
        exit_code = int(m.group(2)) if m.group(2) else None
    else:
        output = ocr.strip()
        exit_code = None

    # Switch back to TTY1
    _send_keys("KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_F1")

    return {
        "output": output,
        "error": "",
        "exit_code": exit_code,
        "method": "vnc",
    }


def kubevirt_exec_console(
    provider, project_id, vm_id, username, password, command, timeout=600
):
    """Execute command via KubeVirt serial console (WebSocket-based)."""
    import re
    import time

    _, core_v1, api_client = _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)
    vm_name = f"troshka-vm-{vm_id[:8]}"

    if not password:
        raise RuntimeError("Password required for console exec")

    # Use websocket to connect to the console subresource
    # The kubernetes client doesn't have a native console stream helper,
    # so we use the raw API path with the websocket protocol.
    import ssl

    import websocket

    creds = provider.get_credentials()
    api_url = creds["api_url"]
    token = creds["token"]
    verify = creds.get("verify_ssl", False)
    ssl_opts = (
        {"cert_reqs": ssl.CERT_REQUIRED} if verify else {"cert_reqs": ssl.CERT_NONE}
    )
    ws_url = api_url.replace("https://", "wss://").replace("http://", "ws://")
    console_path = (
        f"/apis/subresources.kubevirt.io/v1/namespaces/{namespace}"
        f"/virtualmachineinstances/{vm_name}/console"
    )
    full_url = f"{ws_url}{console_path}"

    ws = None
    try:
        ws = websocket.create_connection(
            full_url,
            header=[f"Authorization: Bearer {token}"],
            subprotocols=["plain.kubevirt.io"],
            sslopt=ssl_opts,
            timeout=min(timeout, 30),
        )

        def _ws_read(secs):
            """Read all available data from WebSocket within timeout."""
            buf = ""
            deadline = time.time() + secs
            ws.settimeout(0.5)
            while time.time() < deadline:
                try:
                    data = ws.recv()
                    if isinstance(data, bytes):
                        data = data.decode("utf-8", errors="replace")
                    buf += data
                except websocket.WebSocketTimeoutException:
                    if buf:
                        break
            return buf

        def _ws_send(text):
            ws.send(text.encode("utf-8") if isinstance(text, str) else text)

        def _strip_ansi(s):
            return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", s)

        # Read initial output to detect state
        initial = _ws_read(3)

        # Send enter to wake up console
        _ws_send("\n")
        prompt_check = _ws_read(3)
        combined = initial + prompt_check

        # Detect state: login prompt, password prompt, or shell
        if "login:" in combined.lower():
            _ws_send(f"{username}\n")
            _ws_read(2)
            _ws_send(f"{password}\n")
            login_resp = _ws_read(3)
            if "login incorrect" in login_resp.lower():
                raise RuntimeError("Console login failed")
        elif "password:" in combined.lower():
            _ws_send(f"{password}\n")
            login_resp = _ws_read(3)
            if "login incorrect" in login_resp.lower():
                raise RuntimeError("Console login failed")
        # else: already at a shell prompt

        # Send command wrapped with markers
        _ws_send("echo TROSHKA_BEGIN\n")
        _ws_read(1)
        _ws_send(f"({command}) 2>&1; echo TROSHKA_END $?\n")

        # Read until TROSHKA_END marker
        output = ""
        deadline = time.time() + min(timeout, 300)
        while time.time() < deadline:
            chunk = _ws_read(2)
            output += chunk
            if "TROSHKA_END" in output:
                break

        # Parse output between markers
        clean = _strip_ansi(output)
        begin_idx = clean.find("TROSHKA_BEGIN")
        end_idx = clean.find("TROSHKA_END")
        if begin_idx >= 0 and end_idx >= 0:
            body = clean[begin_idx + len("TROSHKA_BEGIN") : end_idx].strip()
            # Extract exit code from the TROSHKA_END line
            end_line = clean[end_idx:].split("\n")[0]
            exit_code_match = re.search(r"TROSHKA_END\s+(\d+)", end_line)
            exit_code = int(exit_code_match.group(1)) if exit_code_match else None
        else:
            body = clean
            exit_code = None

        return {
            "output": body,
            "error": "",
            "exit_code": exit_code,
            "method": "console",
        }
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
