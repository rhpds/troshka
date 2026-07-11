# KubeVirt Native Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new `kubevirt` provider type that creates KubeVirt VMs directly on OCP clusters, with a kopf-based operator managing all resources via CRDs.

**Architecture:** Two new components — a `kubevirt.py` provider driver in the backend (thin layer creating/watching CRDs) and a standalone Troshka operator (Python/kopf) that reconciles CRDs into KubeVirt VMs, OVN networks, and helper pods. The operator lives in `src/operator/`. Pattern portability is preserved: same topology JSONB, same S3 disk images, same deploy semantics.

**Tech Stack:** Python 3.11, kopf (K8s operator framework), kubernetes Python client (already a dependency), KubeVirt/CDI APIs, OVN-Kubernetes secondary networks, sushy-tools (Redfish emulation)

## Global Constraints

- Fully additive — zero changes to existing providers, models, deploy pipeline, or frontend
- `kubernetes` Python client is already in `pyproject.toml` (>=29.0)
- Tests use SQLite with JSONB/UUID compiler overrides (see `src/backend/tests/conftest.py`)
- K8s API calls use lazy `from kubernetes import client` imports inside method bodies (follow `ocpvirt.py` pattern)
- K8s clients created via `_get_k8s_clients(provider)` helper returning `(CustomObjectsApi, CoreV1Api)`
- Provider credentials dict shape: `{"api_url": str, "token": str, "namespace": str, "verify_ssl": bool}`
- Host model `host_type` field: new value `"kubevirt-cluster"`
- CRD API group: `troshka.redhat.com/v1alpha1`
- All UUIDs as strings (match existing `UUID(as_uuid=False)` pattern)
- Run tests: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
- Run black before committing: `black src/backend/app/ src/backend/tests/`

---

### Task 1: CRD Manifests & Operator Scaffold

**Files:**
- Create: `src/operator/crds/troshkaproject.yaml`
- Create: `src/operator/crds/troshkanetwork.yaml`
- Create: `src/operator/crds/troshkavm.yaml`
- Create: `src/operator/requirements.txt`
- Create: `src/operator/operator.py` (entrypoint)
- Create: `src/operator/handlers/__init__.py`
- Create: `src/operator/Dockerfile`

**Interfaces:**
- Produces: CRD YAML files that define the API schema for `TroshkaProject`, `TroshkaNetwork`, `TroshkaVM`
- Produces: Operator entrypoint that kopf uses to discover handlers

- [ ] **Step 1: Create `src/operator/` directory structure**

```bash
mkdir -p src/operator/crds src/operator/handlers src/operator/helpers
touch src/operator/handlers/__init__.py src/operator/helpers/__init__.py
```

- [ ] **Step 2: Write TroshkaProject CRD**

Create `src/operator/crds/troshkaproject.yaml`:

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: troshkaprojects.troshka.redhat.com
spec:
  group: troshka.redhat.com
  versions:
    - name: v1alpha1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              required: [projectId, topology, s3Config]
              properties:
                projectId:
                  type: string
                topology:
                  type: object
                  x-kubernetes-preserve-unknown-fields: true
                s3Config:
                  type: object
                  properties:
                    bucket:
                      type: string
                    endpoint:
                      type: string
                    region:
                      type: string
                    credentialsSecret:
                      type: string
                registryCredentials:
                  type: object
                  properties:
                    secretRef:
                      type: string
                commonPassword:
                  type: string
                action:
                  type: string
                  enum: [deploy, destroy, capture]
            status:
              type: object
              x-kubernetes-preserve-unknown-fields: true
      subresources:
        status: {}
      additionalPrinterColumns:
        - name: Phase
          type: string
          jsonPath: .status.phase
        - name: Age
          type: date
          jsonPath: .metadata.creationTimestamp
  scope: Namespaced
  names:
    plural: troshkaprojects
    singular: troshkaproject
    kind: TroshkaProject
    shortNames: [tp]
```

- [ ] **Step 3: Write TroshkaNetwork CRD**

Create `src/operator/crds/troshkanetwork.yaml`:

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: troshkanetworks.troshka.redhat.com
spec:
  group: troshka.redhat.com
  versions:
    - name: v1alpha1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              required: [networkId, cidr]
              properties:
                networkId:
                  type: string
                cidr:
                  type: string
                gateway:
                  type: string
                dhcpRange:
                  type: string
                networkType:
                  type: string
                  enum: [standard, external, bmc]
                  default: standard
                staticLeases:
                  type: array
                  items:
                    type: object
                    properties:
                      mac:
                        type: string
                      ip:
                        type: string
                      hostname:
                        type: string
                pxeConfig:
                  type: object
                  properties:
                    enabled:
                      type: boolean
                    libraryIsoId:
                      type: string
                    isoS3Path:
                      type: string
                dnsForwarders:
                  type: array
                  items:
                    type: string
                externalAccess:
                  type: boolean
                  default: false
            status:
              type: object
              x-kubernetes-preserve-unknown-fields: true
      subresources:
        status: {}
      additionalPrinterColumns:
        - name: Ready
          type: boolean
          jsonPath: .status.ready
        - name: CIDR
          type: string
          jsonPath: .spec.cidr
  scope: Namespaced
  names:
    plural: troshkanetworks
    singular: troshkanetwork
    kind: TroshkaNetwork
    shortNames: [tn]
```

- [ ] **Step 4: Write TroshkaVM CRD**

Create `src/operator/crds/troshkavm.yaml`:

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: troshkavms.troshka.redhat.com
spec:
  group: troshka.redhat.com
  versions:
    - name: v1alpha1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              required: [vmId, name, cpus, memory]
              properties:
                vmId:
                  type: string
                name:
                  type: string
                cpus:
                  type: integer
                memory:
                  type: integer
                firmware:
                  type: string
                  enum: [bios, uefi, uefi-secure]
                  default: bios
                machineType:
                  type: string
                  enum: [q35, i440fx]
                  default: q35
                smbiosUuid:
                  type: string
                powerOnAtDeploy:
                  type: boolean
                  default: true
                bootOrder:
                  type: array
                  items:
                    type: object
                    properties:
                      type:
                        type: string
                      id:
                        type: string
                disks:
                  type: array
                  items:
                    type: object
                    x-kubernetes-preserve-unknown-fields: true
                nics:
                  type: array
                  items:
                    type: object
                    x-kubernetes-preserve-unknown-fields: true
                cloudInit:
                  type: object
                  properties:
                    userData:
                      type: string
                    networkConfig:
                      type: string
                bmcEnabled:
                  type: boolean
                  default: false
                cdrom:
                  type: object
                  properties:
                    libraryIsoId:
                      type: string
                    s3Path:
                      type: string
                guestfishCommands:
                  type: array
                  items:
                    type: string
            status:
              type: object
              x-kubernetes-preserve-unknown-fields: true
      subresources:
        status: {}
      additionalPrinterColumns:
        - name: State
          type: string
          jsonPath: .status.state
        - name: VM Name
          type: string
          jsonPath: .spec.name
  scope: Namespaced
  names:
    plural: troshkavms
    singular: troshkavm
    kind: TroshkaVM
    shortNames: [tvm]
```

- [ ] **Step 5: Write operator requirements.txt**

Create `src/operator/requirements.txt`:

```
kopf>=1.37.0
kubernetes>=29.0.0
pyyaml>=6.0
```

- [ ] **Step 6: Write operator entrypoint**

Create `src/operator/operator.py`:

```python
import kopf
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("troshka-operator")

CRD_GROUP = "troshka.redhat.com"
CRD_VERSION = "v1alpha1"


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    settings.posting.level = logging.WARNING
    settings.persistence.finalizer = "troshka.redhat.com/finalizer"
    logger.info("Troshka operator starting")
```

- [ ] **Step 7: Write operator Dockerfile**

Create `src/operator/Dockerfile`:

```dockerfile
FROM registry.access.redhat.com/ubi9/python-311:latest

WORKDIR /opt/operator

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["kopf", "run", "operator.py", "--verbose"]
```

- [ ] **Step 8: Validate CRDs are valid YAML**

```bash
cd /Users/prutledg/troshka
python3 -c "
import yaml
for f in ['src/operator/crds/troshkaproject.yaml',
          'src/operator/crds/troshkanetwork.yaml',
          'src/operator/crds/troshkavm.yaml']:
    with open(f) as fh:
        doc = yaml.safe_load(fh)
        print(f'{f}: {doc[\"spec\"][\"names\"][\"kind\"]} OK')
"
```

Expected: All three print OK with correct Kind names.

- [ ] **Step 9: Commit**

```bash
cd /Users/prutledg/troshka
git add src/operator/
git commit -m "feat: scaffold troshka operator with CRD manifests

Add TroshkaProject, TroshkaNetwork, TroshkaVM CRDs and kopf-based
operator entrypoint. This is the foundation for the kubevirt native
provider."
```

---

### Task 2: Provider Driver — Registration & K8s Client Setup

**Files:**
- Create: `src/backend/app/services/providers/kubevirt.py`
- Modify: `src/backend/app/services/providers/__init__.py` (add `"kubevirt"` case)
- Create: `src/backend/tests/test_kubevirt_provider.py`

**Interfaces:**
- Consumes: `ProviderDriver` base class from `base.py`
- Produces: `KubeVirtDriver` class with `_get_k8s_clients(provider)` helper, `provision_host()`, `terminate_host()`, `get_host_status()`, `get_host_powerstate()`, `start_host()`, `stop_host()`, `resize_host()`, `extend_host_storage()`

- [ ] **Step 1: Write failing test for provider registration**

Create `src/backend/tests/test_kubevirt_provider.py`:

```python
import pytest
from unittest.mock import MagicMock, patch
from app.services.providers import get_provider_driver


def _make_provider(provider_type="kubevirt"):
    p = MagicMock()
    p.type = provider_type
    p.get_credentials.return_value = {
        "api_url": "https://api.cluster.example.com:6443",
        "token": "test-token",
        "namespace": "troshka",
        "verify_ssl": False,
    }
    return p


def test_get_provider_driver_returns_kubevirt():
    provider = _make_provider()
    driver = get_provider_driver(provider)
    from app.services.providers.kubevirt import KubeVirtDriver

    assert isinstance(driver, KubeVirtDriver)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_kubevirt_provider.py::test_get_provider_driver_returns_kubevirt -v
```

Expected: FAIL with `ValueError` (no `"kubevirt"` case in dispatcher).

- [ ] **Step 3: Create KubeVirtDriver with K8s client setup**

Create `src/backend/app/services/providers/kubevirt.py`:

```python
import logging
import json

from app.services.providers.base import ProviderDriver

logger = logging.getLogger(__name__)

CRD_GROUP = "troshka.redhat.com"
CRD_VERSION = "v1alpha1"


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
    )


class KubeVirtDriver(ProviderDriver):
    def provision_host(self, provider, host_id, instance_type, storage_size_gb, **kwargs):
        custom_api, core_api = _get_k8s_clients(provider)
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
            _, core_api = _get_k8s_clients(provider)
            core_api.get_api_versions()
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
        custom_api, core_api = _get_k8s_clients(provider)
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
                "tls": {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"},
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
        custom_api, core_api = _get_k8s_clients(provider)
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        try:
            core_api.delete_namespaced_service(name=f"vnc-{hostname}", namespace=namespace)
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
        custom_api, core_api = _get_k8s_clients(provider)
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        try:
            svcs = core_api.list_namespaced_service(
                namespace=namespace, label_selector="app=troshka-vnc"
            )
            for svc in svcs.items:
                core_api.delete_namespaced_service(name=svc.metadata.name, namespace=namespace)
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
        custom_api, core_api = _get_k8s_clients(provider)
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

        import time

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
        _, core_api = _get_k8s_clients(provider)
        creds = provider.get_credentials()
        ns = namespace or creds.get("namespace", "troshka")
        try:
            core_api.delete_namespaced_service(name=allocation_id, namespace=ns)
        except Exception:
            pass

    def update_eip_ports(self, provider, host, allocation_id, ports):
        _, core_api = _get_k8s_clients(provider)
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        svc_ports = [
            {"port": p["port"], "targetPort": p.get("target_port", p["port"]), "protocol": "TCP"}
            for p in ports
        ]
        core_api.patch_namespaced_service(
            name=allocation_id,
            namespace=namespace,
            body={"spec": {"ports": svc_ports}},
        )

    def create_route_access(self, provider, host, project_id, vm_name, int_ip, port, target_port=None):
        custom_api, core_api = _get_k8s_clients(provider)
        creds = provider.get_credentials()
        namespace = f"troshka-{project_id[:8]}"
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
                "tls": {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"},
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
        custom_api, core_api = _get_k8s_clients(provider)
        ns = namespace or f"troshka-{project_id[:8]}"
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
        custom_api, _ = _get_k8s_clients(provider)
        namespace = f"troshka-{project_id[:8]}"

        from kubernetes import client as k8s_client

        core_api = _get_k8s_clients(provider)[1]
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
                    "credentialsSecret": "s3-credentials",
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
        custom_api, core_api = _get_k8s_clients(provider)
        namespace = f"troshka-{project_id[:8]}"
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
        namespace = f"troshka-{project_id[:8]}"
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
```

- [ ] **Step 4: Register KubeVirtDriver in provider dispatcher**

In `src/backend/app/services/providers/__init__.py`, add the `"kubevirt"` case. The current file has `if/elif` branches for each provider type ending with `else: raise ValueError(...)`. Add before the `else`:

```python
    elif provider.type == "kubevirt":
        from app.services.providers.kubevirt import KubeVirtDriver
        return KubeVirtDriver()
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_kubevirt_provider.py::test_get_provider_driver_returns_kubevirt -v
```

Expected: PASS

- [ ] **Step 6: Write tests for provision_host and get_host_status**

Add to `src/backend/tests/test_kubevirt_provider.py`:

```python
def test_provision_host_returns_cluster_info():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    mock_node = MagicMock()
    mock_node.status.allocatable = {"cpu": "64", "memory": "262144Mi"}
    mock_nodes = MagicMock()
    mock_nodes.items = [mock_node]

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_core.list_node.return_value = mock_nodes
        mock_clients.return_value = (mock_custom, mock_core)

        result = driver.provision_host(provider, "test-host-id", "kubevirt-cluster", 1000)

    assert result["host_id"] == "test-host-id"
    assert result["instance_type"] == "kubevirt-cluster"
    assert result["total_vcpus"] == 64
    assert result["total_ram_mb"] == 262144


def test_get_host_status_returns_running():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        result = driver.get_host_status(provider, "https://api.cluster.example.com:6443")

    assert result is not None
    assert result["state"] == "running"


def test_get_host_powerstate_always_running():
    provider = _make_provider()
    driver = get_provider_driver(provider)
    assert driver.get_host_powerstate(provider, "any") == "running"


def test_deploy_project_creates_namespace_and_cr():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        result = driver.deploy_project(
            provider,
            "12345678-1234-1234-1234-123456789abc",
            {"nodes": [], "edges": []},
            {"bucket": "test", "endpoint": "s3.amazonaws.com", "region": "us-east-1"},
        )

    assert result == "project-12345678"
    mock_core.create_namespace.assert_called_once()
    mock_custom.create_namespaced_custom_object.assert_called_once()
    call_args = mock_custom.create_namespaced_custom_object.call_args
    assert call_args.kwargs["namespace"] == "troshka-12345678"
    assert call_args.kwargs["body"]["spec"]["action"] == "deploy"


def test_destroy_project_deletes_cr_and_namespace():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        driver.destroy_project(provider, "12345678-1234-1234-1234-123456789abc")

    mock_custom.delete_namespaced_custom_object.assert_called_once()
    mock_core.delete_namespace.assert_called_once_with(name="troshka-12345678")
```

- [ ] **Step 7: Run all tests**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_kubevirt_provider.py -v
```

Expected: All tests PASS.

- [ ] **Step 8: Run black and commit**

```bash
cd /Users/prutledg/troshka
black src/backend/app/services/providers/kubevirt.py src/backend/tests/test_kubevirt_provider.py
git add src/backend/app/services/providers/kubevirt.py src/backend/app/services/providers/__init__.py src/backend/tests/test_kubevirt_provider.py
git commit -m "feat: add kubevirt provider driver with K8s client setup

New KubeVirtDriver implementing the ProviderDriver interface. Creates
TroshkaProject CRs for deploy, manages EIPs via LoadBalancer Services,
console via OCP Routes. Virtual host represents the cluster itself."
```

---

### Task 3: Operator — TroshkaNetwork Handler (OVN + dnsmasq + Gateway)

**Files:**
- Create: `src/operator/handlers/network.py`
- Create: `src/operator/helpers/k8s.py` (shared K8s resource builders)
- Create: `src/operator/helpers/dnsmasq.py` (config generator)
- Modify: `src/operator/operator.py` (import network handler)

**Interfaces:**
- Consumes: `TroshkaNetwork` CRs created by the project handler (Task 4)
- Produces: `NetworkAttachmentDefinition`, dnsmasq Pod, gateway Pod per network. Sets `status.ready`, `status.nadName`, `status.dhcpPodReady`, `status.gatewayPodReady`

- [ ] **Step 1: Create K8s resource builder helpers**

Create `src/operator/helpers/k8s.py`:

```python
import hashlib

CRD_GROUP = "troshka.redhat.com"
CRD_VERSION = "v1alpha1"
TOOLS_IMAGE = "quay.io/troshka/troshka-tools:latest"
DNSMASQ_IMAGE = "quay.io/troshka/dnsmasq:latest"
GATEWAY_IMAGE = "quay.io/troshka/gateway:latest"


def owner_ref(cr):
    return {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": cr["kind"],
        "name": cr["metadata"]["name"],
        "uid": cr["metadata"]["uid"],
        "controller": True,
        "blockOwnerDeletion": True,
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

    import json

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

    annotations = {f"k8s.v1.cni.cncf.io/networks": f"{nad_name}"}
    if spec.get("gateway"):
        annotations[f"k8s.ovn.org/static-ip-{nad_name}"] = spec["gateway"]

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
                    "command": ["dnsmasq", "--no-daemon", "--conf-file=/etc/dnsmasq/dnsmasq.conf"],
                    "securityContext": {"capabilities": {"add": ["NET_ADMIN", "NET_RAW"]}},
                    "volumeMounts": [{"name": "config", "mountPath": "/etc/dnsmasq"}],
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
    spec = network_cr["spec"]
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
            "annotations": {"k8s.v1.cni.cncf.io/networks": net_annotations},
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
                        {"name": "GATEWAY_NETWORKS", "value": net_annotations},
                    ],
                }
            ],
            "restartPolicy": "Always",
        },
    }
```

- [ ] **Step 2: Create dnsmasq config generator**

Create `src/operator/helpers/dnsmasq.py`:

```python
def generate_dnsmasq_config(network_spec):
    lines = [
        "port=0" if not network_spec.get("dnsForwarders") else "",
        "bind-interfaces",
        "except-interface=lo",
        "log-dhcp",
    ]

    if network_spec.get("dnsForwarders"):
        lines[0] = f"port=53"
        for fwd in network_spec["dnsForwarders"]:
            lines.append(f"server={fwd}")

    if network_spec.get("dhcpRange"):
        cidr = network_spec["cidr"]
        netmask = _cidr_to_netmask(cidr)
        dhcp_range = network_spec["dhcpRange"]
        lines.append(f"dhcp-range={dhcp_range},{netmask},12h")

    if network_spec.get("gateway"):
        lines.append(f"dhcp-option=3,{network_spec['gateway']}")

    for lease in network_spec.get("staticLeases", []):
        mac = lease.get("mac", "")
        ip = lease.get("ip", "")
        hostname = lease.get("hostname", "")
        if mac and ip:
            if hostname:
                lines.append(f"dhcp-host={mac},{ip},{hostname}")
            else:
                lines.append(f"dhcp-host={mac},{ip}")

    pxe = network_spec.get("pxeConfig", {})
    if pxe.get("enabled"):
        lines.append("enable-tftp")
        lines.append("tftp-root=/var/lib/tftpboot")
        lines.append("dhcp-boot=pxelinux.0")

    return "\n".join(line for line in lines if line) + "\n"


def _cidr_to_netmask(cidr):
    prefix = int(cidr.split("/")[1])
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    return f"{(mask >> 24) & 0xFF}.{(mask >> 16) & 0xFF}.{(mask >> 8) & 0xFF}.{mask & 0xFF}"
```

- [ ] **Step 3: Write the TroshkaNetwork handler**

Create `src/operator/handlers/network.py`:

```python
import kopf
import logging
from kubernetes import client
from helpers.k8s import CRD_GROUP, CRD_VERSION, build_nad, build_dnsmasq_pod, build_gateway_pod
from helpers.dnsmasq import generate_dnsmasq_config

logger = logging.getLogger(__name__)


@kopf.on.create(CRD_GROUP, CRD_VERSION, "troshkanetworks")
async def network_create(spec, meta, namespace, name, body, patch, **_):
    logger.info(f"Creating network {name} in {namespace}")

    api = client.CoreV1Api()
    custom_api = client.CustomObjectsApi()

    nad = build_nad(body)
    try:
        custom_api.create_namespaced_custom_object(
            group="k8s.cni.cncf.io",
            version="v1",
            namespace=namespace,
            plural="net-attach-defs",
            body=nad,
        )
        logger.info(f"Created NAD {nad['metadata']['name']}")
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    dnsmasq_conf = generate_dnsmasq_config(spec)
    cm_body = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=f"dnsmasq-{name}",
            namespace=namespace,
        ),
        data={"dnsmasq.conf": dnsmasq_conf},
    )
    try:
        api.create_namespaced_config_map(namespace=namespace, body=cm_body)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    dnsmasq_pod = build_dnsmasq_pod(body, dnsmasq_conf)
    try:
        api.create_namespaced_pod(namespace=namespace, body=dnsmasq_pod)
        logger.info(f"Created dnsmasq pod for {name}")
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    gateway_ready = True
    if spec.get("externalAccess"):
        nad_name = f"{name}-nad"
        gateway_pod = build_gateway_pod(body, [nad_name])
        try:
            api.create_namespaced_pod(namespace=namespace, body=gateway_pod)
            logger.info(f"Created gateway pod for {name}")
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise
    else:
        gateway_ready = True

    patch.status["ready"] = True
    patch.status["nadName"] = f"{name}-nad"
    patch.status["dhcpPodReady"] = True
    patch.status["gatewayPodReady"] = gateway_ready
    logger.info(f"Network {name} ready")


@kopf.on.delete(CRD_GROUP, CRD_VERSION, "troshkanetworks")
async def network_delete(spec, meta, namespace, name, **_):
    logger.info(f"Deleting network {name} in {namespace} (ownerReferences handle cascade)")
```

- [ ] **Step 4: Import network handler in operator.py**

Add to `src/operator/operator.py` after the existing imports:

```python
import handlers.network  # noqa: F401 — registers kopf handlers
```

- [ ] **Step 5: Validate Python syntax**

```bash
cd /Users/prutledg/troshka
python3 -c "
import ast
for f in ['src/operator/handlers/network.py',
          'src/operator/helpers/k8s.py',
          'src/operator/helpers/dnsmasq.py']:
    with open(f) as fh:
        ast.parse(fh.read())
    print(f'{f}: syntax OK')
"
```

Expected: All three print OK.

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka
git add src/operator/
git commit -m "feat: add TroshkaNetwork operator handler

Creates OVN NetworkAttachmentDefinitions, dnsmasq Pods for DHCP/DNS,
and gateway Pods for NAT. Includes dnsmasq config generator with
static leases and PXE support."
```

---

### Task 4: Operator — TroshkaProject Handler (Topology Parsing & Orchestration)

**Files:**
- Create: `src/operator/handlers/project.py`
- Create: `src/operator/helpers/topology.py` (topology parser)
- Modify: `src/operator/operator.py` (import project handler)

**Interfaces:**
- Consumes: `TroshkaProject` CRs created by the backend provider driver
- Produces: `TroshkaNetwork` and `TroshkaVM` child CRs. Sets `status.phase`, `status.vmStates`, `status.deployProgress`

- [ ] **Step 1: Create topology parser**

Create `src/operator/helpers/topology.py`:

```python
def extract_networks(topology):
    nodes = topology.get("nodes", [])
    networks = []
    for node in nodes:
        data = node.get("data", {})
        if node.get("type") == "networkNode":
            networks.append({
                "id": data.get("id", node.get("id", "")),
                "label": data.get("label", ""),
                "cidr": data.get("cidr", ""),
                "gateway": data.get("gatewayIp", ""),
                "dhcpRange": data.get("dhcpRange", ""),
                "networkType": data.get("networkType", "standard"),
                "dnsForwarders": data.get("dnsForwarders", []),
                "externalAccess": data.get("externalAccess", False),
                "pxeConfig": data.get("pxeConfig", {}),
                "staticLeases": [],
            })
    return networks


def extract_vms(topology):
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])
    vms = []
    for node in nodes:
        data = node.get("data", {})
        if node.get("type") == "vmNode":
            vm = {
                "id": data.get("id", node.get("id", "")),
                "name": data.get("label", ""),
                "cpus": data.get("cpus", 2),
                "memory": data.get("memory", 4096),
                "firmware": data.get("firmware", "bios"),
                "machineType": data.get("machineType", "q35"),
                "smbiosUuid": data.get("domainUuid", ""),
                "powerOnAtDeploy": data.get("powerOnAtDeploy", True),
                "disks": data.get("disks", []),
                "nics": data.get("nics", []),
                "cloudInit": {
                    "userData": data.get("ciUserData", ""),
                    "networkConfig": data.get("ciNetworkConfig", ""),
                },
                "bmcEnabled": data.get("bmcEnabled", False),
                "bootOrder": data.get("bootDevices", []),
                "cdrom": {},
                "guestfishCommands": data.get("guestfishCommands", []),
            }
            if data.get("pxeBootIsoId"):
                vm["cdrom"] = {
                    "libraryIsoId": data.get("pxeBootIsoId", ""),
                    "s3Path": data.get("pxeBootIsoS3Path", ""),
                }
            vms.append(vm)
    return vms


def extract_containers(topology):
    nodes = topology.get("nodes", [])
    containers = []
    for node in nodes:
        data = node.get("data", {})
        if node.get("type") == "containerNode":
            containers.append({
                "id": data.get("id", node.get("id", "")),
                "name": data.get("label", ""),
                "image": data.get("image", ""),
                "command": data.get("command", ""),
                "ports": data.get("ports", []),
                "env": data.get("env", {}),
                "volumes": data.get("volumes", []),
                "isPod": data.get("isPod", False),
                "initContainers": data.get("initContainers", []),
                "podContainers": data.get("podContainers", []),
                "cpus": data.get("cpus", 1),
                "memory": data.get("memory", 512),
                "nics": data.get("nics", []),
            })
    return containers


def extract_start_order(topology):
    nodes = topology.get("nodes", [])
    for node in nodes:
        data = node.get("data", {})
        if node.get("type") == "vmNode" or node.get("type") == "containerNode":
            continue
    start_order = []
    for node in nodes:
        data = node.get("data", {})
        so = data.get("startOrder", [])
        if so:
            start_order = so
            break
    if not start_order:
        vms = extract_vms(topology)
        start_order = [{"vmId": vm["id"]} for vm in vms]
    return start_order


def build_static_leases(topology):
    edges = topology.get("edges", [])
    nodes = topology.get("nodes", [])

    node_map = {}
    for node in nodes:
        data = node.get("data", {})
        node_id = data.get("id", node.get("id", ""))
        node_map[node_id] = data

    network_leases = {}

    for edge in edges:
        source = edge.get("source", "")
        target = edge.get("target", "")
        source_handle = edge.get("sourceHandle", "")

        source_data = node_map.get(source, {})
        target_data = node_map.get(target, {})

        vm_data = None
        net_id = None
        nic_id = None

        if source_data.get("nics"):
            vm_data = source_data
            net_id = target
            nic_id = source_handle
        elif target_data.get("nics"):
            vm_data = target_data
            net_id = source
            nic_id = edge.get("targetHandle", "")

        if vm_data and net_id and nic_id:
            for nic in vm_data.get("nics", []):
                if nic.get("id") == nic_id:
                    mac = nic.get("mac", "")
                    ip = nic.get("ip", "")
                    hostname = vm_data.get("label", "")
                    if mac and ip:
                        if net_id not in network_leases:
                            network_leases[net_id] = []
                        network_leases[net_id].append({
                            "mac": mac,
                            "ip": ip,
                            "hostname": hostname,
                        })

    return network_leases
```

- [ ] **Step 2: Write the TroshkaProject handler**

Create `src/operator/handlers/project.py`:

```python
import kopf
import logging
from kubernetes import client
from helpers.k8s import CRD_GROUP, CRD_VERSION, owner_ref
from helpers.topology import (
    extract_networks,
    extract_vms,
    extract_containers,
    extract_start_order,
    build_static_leases,
)

logger = logging.getLogger(__name__)


@kopf.on.create(CRD_GROUP, CRD_VERSION, "troshkaprojects")
async def project_create(spec, meta, namespace, name, body, patch, **_):
    action = spec.get("action", "deploy")
    logger.info(f"TroshkaProject {name} created with action={action}")

    if action != "deploy":
        return

    patch.status["phase"] = "Deploying"
    patch.status["deployProgress"] = {"percent": 0, "stage": "Parsing topology", "detail": ""}

    topology = spec.get("topology", {})
    custom_api = client.CustomObjectsApi()

    networks = extract_networks(topology)
    static_leases = build_static_leases(topology)

    patch.status["deployProgress"] = {
        "percent": 10,
        "stage": "Creating networks",
        "detail": f"0/{len(networks)} networks",
    }

    for i, net in enumerate(networks):
        net_name = f"net-{net['id'][:8]}"

        net_spec = {
            "networkId": net["id"],
            "cidr": net["cidr"],
            "gateway": net.get("gateway", ""),
            "dhcpRange": net.get("dhcpRange", ""),
            "networkType": net.get("networkType", "standard"),
            "dnsForwarders": net.get("dnsForwarders", []),
            "externalAccess": net.get("externalAccess", False),
            "staticLeases": static_leases.get(net["id"], []),
        }
        if net.get("pxeConfig"):
            net_spec["pxeConfig"] = net["pxeConfig"]

        net_cr = {
            "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
            "kind": "TroshkaNetwork",
            "metadata": {
                "name": net_name,
                "namespace": namespace,
                "ownerReferences": [owner_ref(body)],
                "labels": {"troshka-project": name},
            },
            "spec": net_spec,
        }

        try:
            custom_api.create_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural="troshkanetworks",
                body=net_cr,
            )
            logger.info(f"Created TroshkaNetwork {net_name}")
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise

        patch.status["deployProgress"] = {
            "percent": 10 + int(20 * (i + 1) / max(len(networks), 1)),
            "stage": "Creating networks",
            "detail": f"{i + 1}/{len(networks)} networks",
        }

    vms = extract_vms(topology)

    patch.status["deployProgress"] = {
        "percent": 30,
        "stage": "Creating VMs",
        "detail": f"0/{len(vms)} VMs",
    }

    for i, vm in enumerate(vms):
        vm_name = f"vm-{vm['id'][:8]}"

        disk_specs = []
        for disk in vm.get("disks", []):
            disk_spec = {
                "id": disk.get("id", ""),
                "sizeGb": disk.get("sizeGb", disk.get("size_gb", 20)),
                "bus": disk.get("bus", "virtio"),
            }
            lib_item_id = disk.get("libraryItemId", disk.get("library_item_id", ""))
            if lib_item_id:
                disk_spec["libraryImage"] = {
                    "s3Path": f"library/{lib_item_id}.qcow2",
                    "format": "qcow2",
                }
            elif disk.get("patternImage"):
                disk_spec["patternImage"] = disk["patternImage"]
            else:
                disk_spec["blank"] = True
            disk_specs.append(disk_spec)

        nic_specs = []
        for nic in vm.get("nics", []):
            nic_spec = {
                "id": nic.get("id", ""),
                "mac": nic.get("mac", ""),
                "model": nic.get("model", "virtio"),
                "networkRef": "",
            }
            nic_specs.append(nic_spec)

        vm_cr = {
            "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
            "kind": "TroshkaVM",
            "metadata": {
                "name": vm_name,
                "namespace": namespace,
                "ownerReferences": [owner_ref(body)],
                "labels": {"troshka-project": name},
            },
            "spec": {
                "vmId": vm["id"],
                "name": vm["name"],
                "cpus": vm["cpus"],
                "memory": vm["memory"],
                "firmware": vm.get("firmware", "bios"),
                "machineType": vm.get("machineType", "q35"),
                "smbiosUuid": vm.get("smbiosUuid", ""),
                "powerOnAtDeploy": vm.get("powerOnAtDeploy", True),
                "disks": disk_specs,
                "nics": nic_specs,
                "cloudInit": vm.get("cloudInit", {}),
                "bmcEnabled": vm.get("bmcEnabled", False),
                "bootOrder": vm.get("bootOrder", []),
            },
        }
        if vm.get("cdrom") and vm["cdrom"].get("s3Path"):
            vm_cr["spec"]["cdrom"] = vm["cdrom"]
        if vm.get("guestfishCommands"):
            vm_cr["spec"]["guestfishCommands"] = vm["guestfishCommands"]

        try:
            custom_api.create_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural="troshkavms",
                body=vm_cr,
            )
            logger.info(f"Created TroshkaVM {vm_name}")
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise

        patch.status["deployProgress"] = {
            "percent": 30 + int(60 * (i + 1) / max(len(vms), 1)),
            "stage": "Creating VMs",
            "detail": f"{i + 1}/{len(vms)} VMs",
        }

    patch.status["phase"] = "Running"
    patch.status["deployProgress"] = {"percent": 100, "stage": "Done", "detail": ""}
    logger.info(f"TroshkaProject {name} deploy complete")


@kopf.on.delete(CRD_GROUP, CRD_VERSION, "troshkaprojects")
async def project_delete(spec, meta, namespace, name, **_):
    logger.info(f"TroshkaProject {name} deleting — ownerReferences handle child cascade")
```

- [ ] **Step 3: Import project handler in operator.py**

Add to `src/operator/operator.py`:

```python
import handlers.project  # noqa: F401 — registers kopf handlers
```

- [ ] **Step 4: Validate syntax**

```bash
cd /Users/prutledg/troshka
python3 -c "
import ast
for f in ['src/operator/handlers/project.py', 'src/operator/helpers/topology.py']:
    with open(f) as fh:
        ast.parse(fh.read())
    print(f'{f}: syntax OK')
"
```

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka
git add src/operator/
git commit -m "feat: add TroshkaProject operator handler

Parses topology JSONB, creates child TroshkaNetwork and TroshkaVM CRs.
Includes topology parser for extracting networks, VMs, containers, and
static DHCP leases from the canvas topology format."
```

---

### Task 5: Operator — TroshkaVM Handler (KubeVirt VMs, DataVolumes, Cloud-init)

**Files:**
- Create: `src/operator/handlers/vm.py`
- Create: `src/operator/helpers/kubevirt.py` (KubeVirt VM spec builder)
- Modify: `src/operator/operator.py` (import vm handler)

**Interfaces:**
- Consumes: `TroshkaVM` CRs created by the project handler
- Produces: KubeVirt `VirtualMachine` CRs, CDI `DataVolume`s, cloud-init `Secret`s. Sets `status.state`, `status.kubevirtVmName`

- [ ] **Step 1: Create KubeVirt VM spec builder**

Create `src/operator/helpers/kubevirt.py`:

```python
import base64

CACHE_NAMESPACE = "troshka-cache"
STORAGE_CLASS = "ocs-storagecluster-ceph-rbd-virtualization"


def build_kubevirt_vm(vm_cr, disk_pvcs, nad_refs, cloudinit_secret_name):
    spec = vm_cr["spec"]
    name = vm_cr["metadata"]["name"]
    namespace = vm_cr["metadata"]["namespace"]

    kv_name = f"troshka-{name}"

    domain = {
        "cpu": {"cores": spec["cpus"]},
        "resources": {"requests": {"memory": f"{spec['memory']}Mi"}},
        "devices": {
            "disks": [],
            "interfaces": [],
        },
    }

    if spec.get("machineType"):
        domain["machine"] = {"type": spec["machineType"]}

    if spec.get("smbiosUuid"):
        domain.setdefault("firmware", {})["uuid"] = spec["smbiosUuid"]

    firmware_type = spec.get("firmware", "bios")
    if firmware_type == "uefi":
        domain.setdefault("firmware", {})["bootloader"] = {"efi": {}}
    elif firmware_type == "uefi-secure":
        domain.setdefault("firmware", {})["bootloader"] = {
            "efi": {"secureBoot": True}
        }

    volumes = []
    boot_idx = 1

    for i, disk_info in enumerate(spec.get("disks", [])):
        disk_id = disk_info.get("id", f"disk-{i}")[:8]
        vol_name = f"disk-{disk_id}"
        bus = disk_info.get("bus", "virtio")

        disk_entry = {"name": vol_name, "disk": {"bus": bus}}

        for bo in spec.get("bootOrder", []):
            if bo.get("type") == "disk" and bo.get("id") == disk_info.get("id"):
                disk_entry["disk"]["bootOrder"] = boot_idx
                boot_idx += 1
                break

        domain["devices"]["disks"].append(disk_entry)

        pvc_name = disk_pvcs.get(disk_info.get("id", ""), vol_name)
        volumes.append({
            "name": vol_name,
            "persistentVolumeClaim": {"claimName": pvc_name},
        })

    if spec.get("cdrom", {}).get("s3Path"):
        cd_vol_name = "cdrom"
        domain["devices"]["disks"].append({
            "name": cd_vol_name,
            "cdrom": {"bus": "sata"},
        })
        cd_pvc = disk_pvcs.get("cdrom", "cdrom-pvc")
        volumes.append({
            "name": cd_vol_name,
            "persistentVolumeClaim": {"claimName": cd_pvc},
        })

    for i, nic in enumerate(spec.get("nics", [])):
        nic_id = nic.get("id", f"nic-{i}")[:8]
        iface_name = f"nic-{nic_id}"
        model = nic.get("model", "virtio")
        net_ref = nic.get("networkRef", "")

        iface = {"name": iface_name, "bridge": {}}
        if model and model != "virtio":
            iface["model"] = model

        for bo in spec.get("bootOrder", []):
            if bo.get("type") == "network" and bo.get("id") == nic.get("id"):
                iface["bootOrder"] = boot_idx
                boot_idx += 1
                break

        domain["devices"]["interfaces"].append(iface)

    if cloudinit_secret_name:
        domain["devices"]["disks"].append({
            "name": "cloudinit",
            "disk": {"bus": "virtio"},
        })
        volumes.append({
            "name": "cloudinit",
            "cloudInitNoCloud": {
                "secretRef": {"name": cloudinit_secret_name},
            },
        })

    networks = []
    for i, nic in enumerate(spec.get("nics", [])):
        nic_id = nic.get("id", f"nic-{i}")[:8]
        iface_name = f"nic-{nic_id}"
        net_ref = nic.get("networkRef", "")
        nad_name = nad_refs.get(net_ref, f"{net_ref}-nad")

        networks.append({
            "name": iface_name,
            "multus": {"networkName": nad_name},
        })

    vm_body = {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachine",
        "metadata": {
            "name": kv_name,
            "namespace": namespace,
            "labels": {"app": "troshka", "troshka-vm": name},
        },
        "spec": {
            "running": spec.get("powerOnAtDeploy", True),
            "template": {
                "metadata": {
                    "labels": {"app": "troshka", "troshka-vm": name},
                },
                "spec": {
                    "domain": domain,
                    "volumes": volumes,
                    "networks": networks,
                },
            },
        },
    }

    return vm_body


def build_cloudinit_secret(vm_cr):
    spec = vm_cr["spec"]
    ci = spec.get("cloudInit", {})
    if not ci.get("userData") and not ci.get("networkConfig"):
        return None

    name = vm_cr["metadata"]["name"]
    namespace = vm_cr["metadata"]["namespace"]

    data = {}
    if ci.get("userData"):
        data["userdata"] = base64.b64encode(ci["userData"].encode()).decode()
    if ci.get("networkConfig"):
        data["networkdata"] = base64.b64encode(ci["networkConfig"].encode()).decode()

    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": f"cloudinit-{name}",
            "namespace": namespace,
        },
        "data": data,
    }


def build_datavolume_from_s3(name, namespace, s3_path, size_gb, s3_config):
    return {
        "apiVersion": "cdi.kubevirt.io/v1beta1",
        "kind": "DataVolume",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {
            "source": {
                "s3": {
                    "url": f"s3://{s3_config.get('bucket', '')}/{s3_path}",
                    "secretRef": s3_config.get("credentialsSecret", "s3-credentials"),
                },
            },
            "pvc": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": f"{size_gb}Gi"}},
                "storageClassName": STORAGE_CLASS,
            },
        },
    }


def build_blank_pvc(name, namespace, size_gb):
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": f"{size_gb}Gi"}},
            "storageClassName": STORAGE_CLASS,
        },
    }


def build_clone_datavolume(name, namespace, source_pvc, source_namespace, size_gb):
    return {
        "apiVersion": "cdi.kubevirt.io/v1beta1",
        "kind": "DataVolume",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {
            "source": {
                "pvc": {
                    "name": source_pvc,
                    "namespace": source_namespace,
                },
            },
            "pvc": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": f"{size_gb}Gi"}},
                "storageClassName": STORAGE_CLASS,
            },
        },
    }
```

- [ ] **Step 2: Write the TroshkaVM handler**

Create `src/operator/handlers/vm.py`:

```python
import kopf
import logging
import time
from kubernetes import client
from helpers.k8s import CRD_GROUP, CRD_VERSION, golden_pvc_name, owner_ref, TOOLS_IMAGE
from helpers.kubevirt import (
    build_kubevirt_vm,
    build_cloudinit_secret,
    build_datavolume_from_s3,
    build_blank_pvc,
    build_clone_datavolume,
    CACHE_NAMESPACE,
)

logger = logging.getLogger(__name__)


def _get_s3_config_from_project(namespace):
    custom_api = client.CustomObjectsApi()
    projects = custom_api.list_namespaced_custom_object(
        group=CRD_GROUP,
        version=CRD_VERSION,
        namespace=namespace,
        plural="troshkaprojects",
    )
    items = projects.get("items", [])
    if items:
        return items[0].get("spec", {}).get("s3Config", {})
    return {}


def _wait_for_datavolume(custom_api, name, namespace, timeout=600):
    for _ in range(timeout // 5):
        try:
            dv = custom_api.get_namespaced_custom_object(
                group="cdi.kubevirt.io",
                version="v1beta1",
                namespace=namespace,
                plural="datavolumes",
                name=name,
            )
            phase = dv.get("status", {}).get("phase", "")
            if phase == "Succeeded":
                return True
            if phase in ("Failed", "Error"):
                logger.error(f"DataVolume {name} failed: {dv.get('status', {}).get('conditions', [])}")
                return False
        except Exception:
            pass
        time.sleep(5)
    return False


def _ensure_golden_pvc(custom_api, core_api, s3_path, size_gb, s3_config):
    pvc_name = golden_pvc_name(s3_path)
    try:
        core_api.read_namespaced_persistent_volume_claim(
            name=pvc_name, namespace=CACHE_NAMESPACE
        )
        logger.info(f"Golden PVC {pvc_name} already exists")
        return pvc_name
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise

    try:
        core_api.create_namespace(
            body=client.V1Namespace(
                metadata=client.V1ObjectMeta(
                    name=CACHE_NAMESPACE,
                    labels={"app": "troshka-cache"},
                )
            )
        )
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    dv = build_datavolume_from_s3(pvc_name, CACHE_NAMESPACE, s3_path, size_gb, s3_config)
    try:
        custom_api.create_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=CACHE_NAMESPACE,
            plural="datavolumes",
            body=dv,
        )
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    if not _wait_for_datavolume(custom_api, pvc_name, CACHE_NAMESPACE):
        raise kopf.TemporaryError(f"Golden PVC {pvc_name} import failed", delay=30)

    logger.info(f"Golden PVC {pvc_name} ready")
    return pvc_name


@kopf.on.create(CRD_GROUP, CRD_VERSION, "troshkavms")
async def vm_create(spec, meta, namespace, name, body, patch, **_):
    logger.info(f"Creating VM {name} in {namespace}")
    patch.status["state"] = "Creating"

    core_api = client.CoreV1Api()
    custom_api = client.CustomObjectsApi()

    s3_config = _get_s3_config_from_project(namespace)

    disk_pvcs = {}

    for disk in spec.get("disks", []):
        disk_id = disk.get("id", "")[:8]
        pvc_name = f"{name}-disk-{disk_id}"

        s3_path = None
        if disk.get("libraryImage", {}).get("s3Path"):
            s3_path = disk["libraryImage"]["s3Path"]
        elif disk.get("patternImage", {}).get("s3Path"):
            s3_path = disk["patternImage"]["s3Path"]

        if s3_path:
            size_gb = disk.get("sizeGb", 20)
            golden_name = _ensure_golden_pvc(custom_api, core_api, s3_path, size_gb, s3_config)

            clone_dv = build_clone_datavolume(
                pvc_name, namespace, golden_name, CACHE_NAMESPACE, size_gb
            )
            clone_dv["metadata"]["ownerReferences"] = [owner_ref(body)]
            try:
                custom_api.create_namespaced_custom_object(
                    group="cdi.kubevirt.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="datavolumes",
                    body=clone_dv,
                )
            except client.exceptions.ApiException as e:
                if e.status != 409:
                    raise

            if not _wait_for_datavolume(custom_api, pvc_name, namespace):
                patch.status["state"] = "Error"
                patch.status["message"] = f"Disk clone failed for {disk_id}"
                raise kopf.PermanentError(f"Disk clone {pvc_name} failed")

        elif disk.get("blank"):
            size_gb = disk.get("sizeGb", 20)
            pvc = build_blank_pvc(pvc_name, namespace, size_gb)
            pvc["metadata"]["ownerReferences"] = [owner_ref(body)]
            try:
                core_api.create_namespaced_persistent_volume_claim(
                    namespace=namespace, body=pvc
                )
            except client.exceptions.ApiException as e:
                if e.status != 409:
                    raise

        disk_pvcs[disk.get("id", "")] = pvc_name

    if spec.get("cdrom", {}).get("s3Path"):
        cdrom_pvc = f"{name}-cdrom"
        cdrom_s3 = spec["cdrom"]["s3Path"]
        golden_name = _ensure_golden_pvc(custom_api, core_api, cdrom_s3, 10, s3_config)
        clone_dv = build_clone_datavolume(cdrom_pvc, namespace, golden_name, CACHE_NAMESPACE, 10)
        clone_dv["metadata"]["ownerReferences"] = [owner_ref(body)]
        try:
            custom_api.create_namespaced_custom_object(
                group="cdi.kubevirt.io",
                version="v1beta1",
                namespace=namespace,
                plural="datavolumes",
                body=clone_dv,
            )
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise
        _wait_for_datavolume(custom_api, cdrom_pvc, namespace)
        disk_pvcs["cdrom"] = cdrom_pvc

    cloudinit_secret_name = None
    ci_secret = build_cloudinit_secret(body)
    if ci_secret:
        ci_secret["metadata"]["ownerReferences"] = [owner_ref(body)]
        cloudinit_secret_name = ci_secret["metadata"]["name"]
        try:
            core_api.create_namespaced_secret(namespace=namespace, body=ci_secret)
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise

    if spec.get("guestfishCommands"):
        gf_commands = spec["guestfishCommands"]
        root_disk_id = spec["disks"][0]["id"] if spec.get("disks") else ""
        root_pvc = disk_pvcs.get(root_disk_id)
        if root_pvc and gf_commands:
            gf_job_name = f"guestfish-{name}"
            gf_args = []
            for cmd in gf_commands:
                gf_args.extend(["--", cmd])

            job = {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {
                    "name": gf_job_name,
                    "namespace": namespace,
                    "ownerReferences": [owner_ref(body)],
                },
                "spec": {
                    "backoffLimit": 1,
                    "template": {
                        "spec": {
                            "containers": [{
                                "name": "guestfish",
                                "image": TOOLS_IMAGE,
                                "command": ["guestfish", "--rw", "-a", "/disk/disk.img", "-i"] + gf_args,
                                "volumeMounts": [{"name": "disk", "mountPath": "/disk"}],
                                "securityContext": {"privileged": True},
                            }],
                            "volumes": [{
                                "name": "disk",
                                "persistentVolumeClaim": {"claimName": root_pvc},
                            }],
                            "restartPolicy": "Never",
                        },
                    },
                },
            }
            batch_api = client.BatchV1Api()
            try:
                batch_api.create_namespaced_job(namespace=namespace, body=job)
            except client.exceptions.ApiException as e:
                if e.status != 409:
                    raise

            for _ in range(120):
                try:
                    j = batch_api.read_namespaced_job(name=gf_job_name, namespace=namespace)
                    if j.status.succeeded:
                        break
                    if j.status.failed:
                        logger.error(f"Guestfish job {gf_job_name} failed")
                        break
                except Exception:
                    pass
                time.sleep(5)

    nad_refs = {}
    try:
        networks = custom_api.list_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural="troshkanetworks",
        )
        for net in networks.get("items", []):
            net_name = net["metadata"]["name"]
            nad_name = net.get("status", {}).get("nadName", f"{net_name}-nad")
            nad_refs[net_name] = nad_name
    except Exception:
        pass

    kv_vm = build_kubevirt_vm(body, disk_pvcs, nad_refs, cloudinit_secret_name)
    kv_vm["metadata"]["ownerReferences"] = [owner_ref(body)]

    try:
        custom_api.create_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            body=kv_vm,
        )
        logger.info(f"Created KubeVirt VM {kv_vm['metadata']['name']}")
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    patch.status["state"] = "Running" if spec.get("powerOnAtDeploy", True) else "Stopped"
    patch.status["kubevirtVmName"] = kv_vm["metadata"]["name"]
    logger.info(f"TroshkaVM {name} reconciled")


@kopf.on.delete(CRD_GROUP, CRD_VERSION, "troshkavms")
async def vm_delete(spec, meta, namespace, name, **_):
    logger.info(f"TroshkaVM {name} deleting — ownerReferences handle KubeVirt VM cascade")
```

- [ ] **Step 3: Import vm handler in operator.py**

Add to `src/operator/operator.py`:

```python
import handlers.vm  # noqa: F401 — registers kopf handlers
```

- [ ] **Step 4: Validate syntax**

```bash
cd /Users/prutledg/troshka
python3 -c "
import ast
for f in ['src/operator/handlers/vm.py', 'src/operator/helpers/kubevirt.py']:
    with open(f) as fh:
        ast.parse(fh.read())
    print(f'{f}: syntax OK')
"
```

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka
git add src/operator/
git commit -m "feat: add TroshkaVM operator handler

Creates KubeVirt VirtualMachines from TroshkaVM CRs. Handles disk image
import from S3 via CDI DataVolumes, golden PVC caching with Ceph RBD
clone, cloud-init Secrets, guestfish Jobs for offline disk modification,
and boot order configuration."
```

---

### Task 6: Operator — BMC Emulation (sushy KubeVirt Driver + Pod)

**Files:**
- Create: `src/operator/helpers/bmc.py` (sushy driver + pod builder)
- Modify: `src/operator/handlers/vm.py` (trigger BMC pod creation when `bmcEnabled`)

**Interfaces:**
- Consumes: `TroshkaVM` CRs with `bmcEnabled: true`
- Produces: BMC Pod running sushy-emulator with custom KubeVirt driver

- [ ] **Step 1: Create BMC helper with sushy driver and pod builder**

Create `src/operator/helpers/bmc.py`:

```python
import json


SUSHY_IMAGE = "quay.io/troshka/sushy-kubevirt:latest"


def build_bmc_pod(project_name, namespace, bmc_vms, bmc_network_nad, credentials):
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
        env.append({"name": "SUSHY_USERNAME", "value": credentials.get("username", "admin")})
        env.append({"name": "SUSHY_PASSWORD", "value": credentials.get("password", "redhat")})

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
            "containers": [{
                "name": "sushy",
                "image": SUSHY_IMAGE,
                "ports": [{"containerPort": 8000, "protocol": "TCP"}],
                "env": env,
                "resources": {
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "500m", "memory": "256Mi"},
                },
            }],
            "restartPolicy": "Always",
        },
    }


SUSHY_KUBEVIRT_DRIVER = '''
"""sushy KubeVirt driver — translates Redfish to KubeVirt API calls."""

import json
import os
from kubernetes import client, config


class KubeVirtDriver:
    def __init__(self):
        config.load_incluster_config()
        self.custom_api = client.CustomObjectsApi()
        self.namespace = os.environ.get("SUSHY_NAMESPACE", "default")
        self.vm_map = json.loads(os.environ.get("SUSHY_VM_MAP", "{}"))

    def _kv_name(self, identity):
        identity = identity.strip("/")
        return self.vm_map.get(identity, identity)

    def _get_vm(self, identity):
        name = self._kv_name(identity)
        return self.custom_api.get_namespaced_custom_object(
            group="kubevirt.io", version="v1",
            namespace=self.namespace, plural="virtualmachines", name=name,
        )

    def _get_vmi(self, identity):
        name = self._kv_name(identity)
        try:
            return self.custom_api.get_namespaced_custom_object(
                group="kubevirt.io", version="v1",
                namespace=self.namespace, plural="virtualmachineinstances", name=name,
            )
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return None
            raise

    def get_power_state(self, identity):
        vmi = self._get_vmi(identity)
        if not vmi:
            return "Off"
        phase = vmi.get("status", {}).get("phase", "")
        return "On" if phase == "Running" else "Off"

    def set_power_state(self, identity, state):
        name = self._kv_name(identity)
        running = state in ("On", "ForceOn")
        self.custom_api.patch_namespaced_custom_object(
            group="kubevirt.io", version="v1",
            namespace=self.namespace, plural="virtualmachines", name=name,
            body={"spec": {"running": running}},
        )
        if state in ("ForceOff", "GracefulShutdown"):
            try:
                self.custom_api.delete_namespaced_custom_object(
                    group="kubevirt.io", version="v1",
                    namespace=self.namespace, plural="virtualmachineinstances", name=name,
                )
            except client.exceptions.ApiException:
                pass
        if state == "ForceRestart":
            try:
                self.custom_api.delete_namespaced_custom_object(
                    group="kubevirt.io", version="v1",
                    namespace=self.namespace, plural="virtualmachineinstances", name=name,
                )
            except client.exceptions.ApiException:
                pass
            self.custom_api.patch_namespaced_custom_object(
                group="kubevirt.io", version="v1",
                namespace=self.namespace, plural="virtualmachines", name=name,
                body={"spec": {"running": True}},
            )

    def get_boot_device(self, identity):
        vm = self._get_vm(identity)
        devices = vm.get("spec", {}).get("template", {}).get("spec", {}).get("domain", {}).get("devices", {})
        disks = devices.get("disks", [])
        interfaces = devices.get("interfaces", [])
        boot_items = []
        for d in disks:
            order = d.get("disk", {}).get("bootOrder") or d.get("cdrom", {}).get("bootOrder")
            if order:
                boot_items.append((order, "Hdd" if "disk" in d else "Cd"))
        for iface in interfaces:
            order = iface.get("bootOrder")
            if order:
                boot_items.append((order, "Pxe"))
        boot_items.sort()
        return boot_items[0][1] if boot_items else "Hdd"

    def get_boot_mode(self, identity):
        vm = self._get_vm(identity)
        fw = vm.get("spec", {}).get("template", {}).get("spec", {}).get("domain", {}).get("firmware", {})
        if fw.get("bootloader", {}).get("efi"):
            return "UEFI"
        return "Legacy"

    def get_total_memory(self, identity):
        vm = self._get_vm(identity)
        res = vm.get("spec", {}).get("template", {}).get("spec", {}).get("domain", {}).get("resources", {})
        mem = res.get("requests", {}).get("memory", "0Mi")
        if mem.endswith("Mi"):
            return int(mem[:-2])
        if mem.endswith("Gi"):
            return int(mem[:-2]) * 1024
        return 0

    def get_total_cpus(self, identity):
        vm = self._get_vm(identity)
        cpu = vm.get("spec", {}).get("template", {}).get("spec", {}).get("domain", {}).get("cpu", {})
        return cpu.get("cores", 1)
'''
```

- [ ] **Step 2: Add BMC pod creation to VM handler**

In `src/operator/handlers/vm.py`, add after the KubeVirt VM creation (before the final `patch.status` lines):

```python
    if spec.get("bmcEnabled"):
        from helpers.bmc import build_bmc_pod

        bmc_nad = None
        try:
            nets = custom_api.list_namespaced_custom_object(
                group=CRD_GROUP, version=CRD_VERSION,
                namespace=namespace, plural="troshkanetworks",
            )
            for net in nets.get("items", []):
                if net.get("spec", {}).get("networkType") == "bmc":
                    bmc_nad = net.get("status", {}).get("nadName", f"{net['metadata']['name']}-nad")
                    break
        except Exception:
            pass

        if bmc_nad:
            project_label = namespace.replace("troshka-", "")
            bmc_vms = [{"vmId": spec["vmId"], "smbiosUuid": spec.get("smbiosUuid", "")}]

            existing_bmc = None
            try:
                existing_bmc = core_api.read_namespaced_pod(
                    name=f"bmc-{project_label}", namespace=namespace
                )
            except client.exceptions.ApiException:
                pass

            if not existing_bmc:
                bmc_pod = build_bmc_pod(project_label, namespace, bmc_vms, bmc_nad, {})
                try:
                    core_api.create_namespaced_pod(namespace=namespace, body=bmc_pod)
                    logger.info(f"Created BMC pod for {namespace}")
                except client.exceptions.ApiException as e:
                    if e.status != 409:
                        raise
```

- [ ] **Step 3: Validate syntax**

```bash
cd /Users/prutledg/troshka
python3 -c "
import ast
for f in ['src/operator/helpers/bmc.py', 'src/operator/handlers/vm.py']:
    with open(f) as fh:
        ast.parse(fh.read())
    print(f'{f}: syntax OK')
"
```

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka
git add src/operator/
git commit -m "feat: add BMC emulation via sushy KubeVirt driver

Custom sushy driver translates Redfish power/boot operations to KubeVirt
API calls. BMC Pod created per project when VMs have bmcEnabled, attached
to BMC network via Multus."
```

---

### Task 7: Operator — VNC Console Proxy & Container Support

**Files:**
- Create: `src/operator/helpers/vnc.py` (VNC proxy pod builder)
- Create: `src/operator/handlers/container.py` (container/pod handler)
- Modify: `src/operator/operator.py` (import container handler)

**Interfaces:**
- Consumes: TroshkaProject CRs (VNC proxy created per project), container nodes from topology
- Produces: VNC proxy Pod, container Pods

- [ ] **Step 1: Create VNC proxy pod builder**

Create `src/operator/helpers/vnc.py`:

```python
VNC_PROXY_IMAGE = "quay.io/troshka/vnc-proxy:latest"


def build_vnc_proxy_pod(project_name, namespace, console_domain):
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
            "containers": [{
                "name": "vnc-proxy",
                "image": VNC_PROXY_IMAGE,
                "ports": [{"containerPort": 8080, "protocol": "TCP"}],
                "env": [
                    {"name": "NAMESPACE", "value": namespace},
                    {"name": "LISTEN_PORT", "value": "8080"},
                ],
                "resources": {
                    "requests": {"cpu": "100m", "memory": "64Mi"},
                    "limits": {"cpu": "500m", "memory": "256Mi"},
                },
            }],
            "restartPolicy": "Always",
        },
    }
```

- [ ] **Step 2: Create container handler**

Create `src/operator/handlers/container.py`:

```python
import kopf
import logging
from kubernetes import client
from helpers.k8s import CRD_GROUP, CRD_VERSION

logger = logging.getLogger(__name__)


def create_container_pods(namespace, containers, nad_refs, owner_reference):
    core_api = client.CoreV1Api()

    for ctr in containers:
        ctr_id = ctr.get("id", "")[:8]
        is_pod = ctr.get("isPod", False)

        if is_pod:
            _create_pod_group(core_api, namespace, ctr, nad_refs, owner_reference)
        else:
            _create_single_container(core_api, namespace, ctr, nad_refs, owner_reference)


def _create_single_container(core_api, namespace, ctr, nad_refs, owner_reference):
    ctr_id = ctr.get("id", "")[:8]
    pod_name = f"ctr-{ctr_id}"

    net_annotations = []
    for nic in ctr.get("nics", []):
        net_ref = nic.get("networkRef", "")
        nad = nad_refs.get(net_ref, f"{net_ref}-nad")
        net_annotations.append(nad)

    env_list = []
    for k, v in ctr.get("env", {}).items():
        env_list.append({"name": k, "value": str(v)})

    container_spec = {
        "name": "main",
        "image": ctr.get("image", ""),
        "env": env_list,
        "resources": {
            "requests": {
                "cpu": f"{ctr.get('cpus', 1) * 1000}m",
                "memory": f"{ctr.get('memory', 512)}Mi",
            },
        },
    }
    if ctr.get("command"):
        container_spec["command"] = ["/bin/sh", "-c", ctr["command"]]
    if ctr.get("ports"):
        container_spec["ports"] = [
            {"containerPort": p.get("container_port", p.get("port", 0)), "protocol": "TCP"}
            for p in ctr["ports"]
        ]

    pod_body = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "labels": {"app": "troshka-container", "troshka-container": ctr_id},
            "ownerReferences": [owner_reference],
        },
        "spec": {
            "containers": [container_spec],
            "restartPolicy": "Always",
        },
    }

    if net_annotations:
        pod_body["metadata"]["annotations"] = {
            "k8s.v1.cni.cncf.io/networks": ",".join(net_annotations)
        }

    try:
        core_api.create_namespaced_pod(namespace=namespace, body=pod_body)
        logger.info(f"Created container pod {pod_name}")
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise


def _create_pod_group(core_api, namespace, ctr, nad_refs, owner_reference):
    ctr_id = ctr.get("id", "")[:8]
    pod_name = f"pod-{ctr_id}"

    init_containers = []
    for ic in ctr.get("initContainers", []):
        init_spec = {
            "name": ic.get("name", f"init-{len(init_containers)}"),
            "image": ic.get("image", ""),
        }
        if ic.get("command"):
            init_spec["command"] = ["/bin/sh", "-c", ic["command"]]
        init_containers.append(init_spec)

    containers = []
    for pc in ctr.get("podContainers", []):
        c_spec = {
            "name": pc.get("name", f"container-{len(containers)}"),
            "image": pc.get("image", ""),
        }
        if pc.get("command"):
            c_spec["command"] = ["/bin/sh", "-c", pc["command"]]
        if pc.get("ports"):
            c_spec["ports"] = [
                {"containerPort": p.get("container_port", p.get("port", 0)), "protocol": "TCP"}
                for p in pc["ports"]
            ]
        env_list = []
        for k, v in pc.get("env", {}).items():
            env_list.append({"name": k, "value": str(v)})
        if env_list:
            c_spec["env"] = env_list
        containers.append(c_spec)

    if not containers:
        containers = [{"name": "main", "image": ctr.get("image", "")}]

    net_annotations = []
    for nic in ctr.get("nics", []):
        net_ref = nic.get("networkRef", "")
        nad = nad_refs.get(net_ref, f"{net_ref}-nad")
        net_annotations.append(nad)

    pod_body = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "labels": {"app": "troshka-pod", "troshka-pod": ctr_id},
            "ownerReferences": [owner_reference],
        },
        "spec": {
            "initContainers": init_containers,
            "containers": containers,
            "restartPolicy": "Always",
        },
    }

    if net_annotations:
        pod_body["metadata"]["annotations"] = {
            "k8s.v1.cni.cncf.io/networks": ",".join(net_annotations)
        }

    try:
        core_api.create_namespaced_pod(namespace=namespace, body=pod_body)
        logger.info(f"Created pod group {pod_name}")
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise
```

- [ ] **Step 3: Import container handler in operator.py**

Add to `src/operator/operator.py`:

```python
import handlers.container  # noqa: F401
```

- [ ] **Step 4: Validate syntax**

```bash
cd /Users/prutledg/troshka
python3 -c "
import ast
for f in ['src/operator/helpers/vnc.py', 'src/operator/handlers/container.py']:
    with open(f) as fh:
        ast.parse(fh.read())
    print(f'{f}: syntax OK')
"
```

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka
git add src/operator/
git commit -m "feat: add VNC proxy and container support to operator

VNC proxy pod builder for KubeVirt VNC subresource relay. Container
handler creates K8s Pods for single containers and pod groups with
init containers, Multus network attachment, and env injection."
```

---

### Task 8: Operator — Pattern Capture (VolumeSnapshot + S3 Export)

**Files:**
- Create: `src/operator/helpers/patterns.py` (pattern capture/export logic)
- Modify: `src/operator/handlers/project.py` (handle `action: capture`)

**Interfaces:**
- Consumes: `TroshkaProject` CR with `spec.action: "capture"`
- Produces: VolumeSnapshots, export Jobs that upload qcow2 to S3

- [ ] **Step 1: Create pattern capture helper**

Create `src/operator/helpers/patterns.py`:

```python
from helpers.k8s import TOOLS_IMAGE

SNAPSHOT_CLASS = "ocs-storagecluster-rbdplugin-snapclass"


def build_volume_snapshot(name, namespace, pvc_name):
    return {
        "apiVersion": "snapshot.storage.k8s.io/v1",
        "kind": "VolumeSnapshot",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {
            "volumeSnapshotClassName": SNAPSHOT_CLASS,
            "source": {"persistentVolumeClaimName": pvc_name},
        },
    }


def build_export_job(name, namespace, snapshot_name, s3_path, s3_config, size_gb):
    temp_pvc_name = f"export-{name}"

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"export-{name}",
            "namespace": namespace,
        },
        "spec": {
            "backoffLimit": 2,
            "template": {
                "spec": {
                    "initContainers": [{
                        "name": "create-pvc",
                        "image": TOOLS_IMAGE,
                        "command": ["sh", "-c", "echo 'PVC created from snapshot via dataSource'"],
                    }],
                    "containers": [{
                        "name": "export",
                        "image": TOOLS_IMAGE,
                        "command": ["sh", "-c", f"""
set -e
echo "Converting raw to qcow2..."
qemu-img convert -f raw -O qcow2 /disk/disk.img /tmp/disk.qcow2
echo "Uploading to S3..."
aws s3 cp /tmp/disk.qcow2 s3://{s3_config.get('bucket', '')}/{s3_path} \
    --endpoint-url {s3_config.get('endpoint', 'https://s3.amazonaws.com')} \
    --region {s3_config.get('region', 'us-east-1')}
echo "Done"
""".strip()],
                        "volumeMounts": [{"name": "disk", "mountPath": "/disk"}],
                        "envFrom": [{"secretRef": {"name": s3_config.get("credentialsSecret", "s3-credentials")}}],
                    }],
                    "volumes": [{
                        "name": "disk",
                        "persistentVolumeClaim": {"claimName": temp_pvc_name},
                    }],
                    "restartPolicy": "Never",
                },
            },
        },
    }


def build_temp_pvc_from_snapshot(name, namespace, snapshot_name, size_gb):
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": f"{size_gb}Gi"}},
            "dataSource": {
                "name": snapshot_name,
                "kind": "VolumeSnapshot",
                "apiGroup": "snapshot.storage.k8s.io",
            },
        },
    }
```

- [ ] **Step 2: Add capture handling to project handler**

In `src/operator/handlers/project.py`, update the `project_create` handler to handle `action: "capture"`. Add after the `if action != "deploy": return` block:

Replace:
```python
    if action != "deploy":
        return
```

With:
```python
    if action == "capture":
        await _handle_capture(spec, namespace, name, body, patch)
        return

    if action not in ("deploy",):
        logger.warning(f"Unknown action {action} for {name}")
        return
```

Then add the capture function before the `project_create` handler:

```python
async def _handle_capture(spec, namespace, name, body, patch):
    import time
    from helpers.patterns import build_volume_snapshot, build_export_job, build_temp_pvc_from_snapshot

    patch.status["phase"] = "Capturing"
    custom_api = client.CustomObjectsApi()
    core_api = client.CoreV1Api()
    batch_api = client.BatchV1Api()

    s3_config = spec.get("s3Config", {})
    pattern_id = spec.get("patternId", name)

    vms = custom_api.list_namespaced_custom_object(
        group=CRD_GROUP, version=CRD_VERSION,
        namespace=namespace, plural="troshkavms",
    )

    for vm_item in vms.get("items", []):
        vm_name = vm_item["metadata"]["name"]
        kv_name = vm_item.get("status", {}).get("kubevirtVmName", f"troshka-{vm_name}")
        try:
            custom_api.patch_namespaced_custom_object(
                group="kubevirt.io", version="v1",
                namespace=namespace, plural="virtualmachines", name=kv_name,
                body={"spec": {"running": False}},
            )
        except Exception as e:
            logger.warning(f"Failed to stop VM {kv_name}: {e}")

    time.sleep(5)

    for vm_item in vms.get("items", []):
        vm_name = vm_item["metadata"]["name"]
        vm_spec = vm_item.get("spec", {})

        for disk in vm_spec.get("disks", []):
            disk_id = disk.get("id", "")[:8]
            pvc_name = f"{vm_name}-disk-{disk_id}"
            snap_name = f"snap-{vm_name}-{disk_id}"
            s3_path = f"patterns/{pattern_id}/{vm_name}-{disk_id}.qcow2"
            size_gb = disk.get("sizeGb", 20)

            snapshot = build_volume_snapshot(snap_name, namespace, pvc_name)
            try:
                custom_api.create_namespaced_custom_object(
                    group="snapshot.storage.k8s.io", version="v1",
                    namespace=namespace, plural="volumesnapshots",
                    body=snapshot,
                )
            except client.exceptions.ApiException as e:
                if e.status != 409:
                    raise

            for _ in range(60):
                try:
                    vs = custom_api.get_namespaced_custom_object(
                        group="snapshot.storage.k8s.io", version="v1",
                        namespace=namespace, plural="volumesnapshots", name=snap_name,
                    )
                    if vs.get("status", {}).get("readyToUse"):
                        break
                except Exception:
                    pass
                time.sleep(5)

            temp_pvc_name = f"export-{vm_name}-{disk_id}"
            temp_pvc = build_temp_pvc_from_snapshot(temp_pvc_name, namespace, snap_name, size_gb)
            try:
                core_api.create_namespaced_persistent_volume_claim(namespace=namespace, body=temp_pvc)
            except client.exceptions.ApiException as e:
                if e.status != 409:
                    raise

            export_job = build_export_job(f"{vm_name}-{disk_id}", namespace, snap_name, s3_path, s3_config, size_gb)
            export_job["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = temp_pvc_name
            try:
                batch_api.create_namespaced_job(namespace=namespace, body=export_job)
            except client.exceptions.ApiException as e:
                if e.status != 409:
                    raise

    patch.status["phase"] = "CaptureComplete"
    logger.info(f"Pattern capture initiated for {name}")
```

- [ ] **Step 3: Validate syntax**

```bash
cd /Users/prutledg/troshka
python3 -c "
import ast
for f in ['src/operator/helpers/patterns.py', 'src/operator/handlers/project.py']:
    with open(f) as fh:
        ast.parse(fh.read())
    print(f'{f}: syntax OK')
"
```

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka
git add src/operator/
git commit -m "feat: add pattern capture to operator

VolumeSnapshot-based pattern capture: stops VMs, snapshots disk PVCs,
creates export Jobs that convert raw to qcow2 and upload to S3. Includes
temp PVC creation from snapshots for export."
```

---

### Task 9: Operator Deployment Manifests (RBAC, ServiceAccount, Deployment)

**Files:**
- Create: `src/operator/deploy/namespace.yaml`
- Create: `src/operator/deploy/serviceaccount.yaml`
- Create: `src/operator/deploy/clusterrole.yaml`
- Create: `src/operator/deploy/clusterrolebinding.yaml`
- Create: `src/operator/deploy/deployment.yaml`
- Create: `src/operator/deploy/kustomization.yaml`

**Interfaces:**
- Produces: Complete K8s manifests for deploying the operator to a cluster

- [ ] **Step 1: Create deployment manifests**

Create `src/operator/deploy/namespace.yaml`:
```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: troshka-operator
  labels:
    app: troshka-operator
```

Create `src/operator/deploy/serviceaccount.yaml`:
```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: troshka-operator
  namespace: troshka-operator
```

Create `src/operator/deploy/clusterrole.yaml`:
```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: troshka-operator
rules:
  - apiGroups: ["troshka.redhat.com"]
    resources: ["troshkaprojects", "troshkanetworks", "troshkavms"]
    verbs: ["*"]
  - apiGroups: ["troshka.redhat.com"]
    resources: ["troshkaprojects/status", "troshkanetworks/status", "troshkavms/status"]
    verbs: ["*"]
  - apiGroups: ["kubevirt.io"]
    resources: ["virtualmachines", "virtualmachineinstances"]
    verbs: ["*"]
  - apiGroups: ["cdi.kubevirt.io"]
    resources: ["datavolumes"]
    verbs: ["*"]
  - apiGroups: ["k8s.cni.cncf.io"]
    resources: ["net-attach-defs"]
    verbs: ["*"]
  - apiGroups: ["route.openshift.io"]
    resources: ["routes"]
    verbs: ["*"]
  - apiGroups: ["snapshot.storage.k8s.io"]
    resources: ["volumesnapshots"]
    verbs: ["*"]
  - apiGroups: [""]
    resources: ["namespaces", "pods", "services", "secrets", "configmaps", "persistentvolumeclaims"]
    verbs: ["*"]
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "list"]
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["*"]
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["create", "patch"]
```

Create `src/operator/deploy/clusterrolebinding.yaml`:
```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: troshka-operator
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: troshka-operator
subjects:
  - kind: ServiceAccount
    name: troshka-operator
    namespace: troshka-operator
```

Create `src/operator/deploy/deployment.yaml`:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: troshka-operator
  namespace: troshka-operator
spec:
  replicas: 1
  selector:
    matchLabels:
      app: troshka-operator
  template:
    metadata:
      labels:
        app: troshka-operator
    spec:
      serviceAccountName: troshka-operator
      containers:
        - name: operator
          image: quay.io/troshka/operator:latest
          command: ["kopf", "run", "operator.py", "--verbose", "--all-namespaces"]
          resources:
            requests:
              cpu: 200m
              memory: 256Mi
            limits:
              cpu: "1"
              memory: 512Mi
```

Create `src/operator/deploy/kustomization.yaml`:
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - namespace.yaml
  - serviceaccount.yaml
  - clusterrole.yaml
  - clusterrolebinding.yaml
  - deployment.yaml
  - ../crds/troshkaproject.yaml
  - ../crds/troshkanetwork.yaml
  - ../crds/troshkavm.yaml
```

- [ ] **Step 2: Validate YAML**

```bash
cd /Users/prutledg/troshka
python3 -c "
import yaml
import os
for root, dirs, files in os.walk('src/operator/deploy'):
    for f in files:
        if f.endswith('.yaml'):
            path = os.path.join(root, f)
            with open(path) as fh:
                docs = list(yaml.safe_load_all(fh))
            print(f'{path}: {len(docs)} doc(s) OK')
"
```

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka
git add src/operator/deploy/
git commit -m "feat: add operator deployment manifests

Kustomize-based deployment with ClusterRole, ServiceAccount,
Deployment, and CRD installation. Operator runs with cluster-wide
permissions for managing KubeVirt VMs, CDI DataVolumes, OVN NADs,
and project namespaces."
```

---

### Task 10: troshka-tools Container Image

**Files:**
- Create: `src/operator/images/troshka-tools/Dockerfile`
- Create: `src/operator/images/dnsmasq/Dockerfile`
- Create: `src/operator/images/gateway/Dockerfile`
- Create: `src/operator/images/gateway/entrypoint.sh`

**Interfaces:**
- Produces: Container images for guestfish Jobs, PXE init Jobs, pattern export Jobs, dnsmasq Pods, gateway Pods

- [ ] **Step 1: Create troshka-tools Dockerfile**

```bash
mkdir -p src/operator/images/troshka-tools src/operator/images/dnsmasq src/operator/images/gateway
```

Create `src/operator/images/troshka-tools/Dockerfile`:

```dockerfile
FROM registry.access.redhat.com/ubi9/ubi:latest

RUN dnf install -y \
    qemu-img \
    guestfs-tools \
    genisoimage \
    isomd5sum \
    python3 \
    python3-pip \
    && dnf clean all

RUN pip3 install awscli

CMD ["sleep", "infinity"]
```

- [ ] **Step 2: Create dnsmasq Dockerfile**

Create `src/operator/images/dnsmasq/Dockerfile`:

```dockerfile
FROM registry.access.redhat.com/ubi9/ubi-minimal:latest

RUN microdnf install -y dnsmasq tftp-server python3 && microdnf clean all

EXPOSE 53/udp 53/tcp 67/udp 69/udp 8080/tcp

CMD ["dnsmasq", "--no-daemon", "--conf-file=/etc/dnsmasq/dnsmasq.conf"]
```

- [ ] **Step 3: Create gateway image**

Create `src/operator/images/gateway/entrypoint.sh`:

```bash
#!/bin/bash
set -e

echo 1 > /proc/sys/net/ipv4/ip_forward

nft add table nat
nft add chain nat postrouting '{ type nat hook postrouting priority 100 ; }'
nft add rule nat postrouting oifname "eth0" masquerade

echo "Gateway NAT active, forwarding enabled"

exec sleep infinity
```

Create `src/operator/images/gateway/Dockerfile`:

```dockerfile
FROM registry.access.redhat.com/ubi9/ubi-minimal:latest

RUN microdnf install -y nftables iproute && microdnf clean all

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
```

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka
git add src/operator/images/
git commit -m "feat: add container images for operator helper pods

troshka-tools: qemu-img, guestfish, awscli for disk operations.
dnsmasq: DHCP/DNS/TFTP for project networks.
gateway: nftables NAT for outbound internet access."
```

---

### Task 11: Backend Integration Tests

**Files:**
- Modify: `src/backend/tests/test_kubevirt_provider.py` (add comprehensive tests)

**Interfaces:**
- Consumes: `KubeVirtDriver` from Task 2
- Produces: Test coverage for all provider driver methods

- [ ] **Step 1: Add tests for console, EIP, and route access methods**

Add to `src/backend/tests/test_kubevirt_provider.py`:

```python
def test_setup_console_returns_config():
    provider = _make_provider()
    driver = get_provider_driver(provider)
    result = driver.setup_console(provider, "console.example.com")
    assert result["console_base_domain"] == "console.example.com"


def test_create_console_record_creates_service_and_route():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        host = MagicMock()
        driver.create_console_record(provider, host, "vm1.console.example.com", "10.0.0.1")

    mock_core.create_namespaced_service.assert_called_once()
    mock_custom.create_namespaced_custom_object.assert_called_once()
    route_call = mock_custom.create_namespaced_custom_object.call_args
    assert route_call.kwargs["body"]["spec"]["host"] == "vm1.console.example.com"


def test_delete_console_record_cleans_up():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core)

        host = MagicMock()
        driver.delete_console_record(provider, host, "vm1.console.example.com", "10.0.0.1")

    mock_core.delete_namespaced_service.assert_called_once()
    mock_custom.delete_namespaced_custom_object.assert_called_once()


def test_create_route_access_creates_service_and_route():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_custom.create_namespaced_custom_object.return_value = {
            "spec": {"host": "bastion-443.apps.cluster.example.com"}
        }
        mock_clients.return_value = (mock_custom, mock_core)

        host = MagicMock()
        result = driver.create_route_access(
            provider, host, "proj-1234-5678", "bastion", "10.0.0.10", 443
        )

    assert result["hostname"] == "bastion-443.apps.cluster.example.com"
    mock_core.create_namespaced_service.assert_called_once()


def test_delete_route_access_cleans_up_by_label():
    provider = _make_provider()
    driver = get_provider_driver(provider)

    mock_svc = MagicMock()
    mock_svc.metadata.name = "rt-bastion-443"
    mock_route = {"metadata": {"name": "rt-bastion-443"}}

    with patch("app.services.providers.kubevirt._get_k8s_clients") as mock_clients:
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_core.list_namespaced_service.return_value.items = [mock_svc]
        mock_custom.list_namespaced_custom_object.return_value = {"items": [mock_route]}
        mock_clients.return_value = (mock_custom, mock_core)

        driver.delete_route_access(provider, "proj-1234-5678")

    mock_core.delete_namespaced_service.assert_called_once()
    mock_custom.delete_namespaced_custom_object.assert_called_once()


def test_resize_and_extend_are_noops():
    provider = _make_provider()
    driver = get_provider_driver(provider)
    assert driver.resize_host(provider, "any", "any") == {}
    assert driver.extend_host_storage(provider, MagicMock(), MagicMock()) == {}


def test_start_stop_host_are_noops():
    provider = _make_provider()
    driver = get_provider_driver(provider)
    driver.start_host(provider, "any")
    driver.stop_host(provider, "any")
```

- [ ] **Step 2: Run all tests**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_kubevirt_provider.py -v
```

Expected: All tests PASS.

- [ ] **Step 3: Run the full test suite to verify no regressions**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/ -v --timeout=60
```

Expected: All existing tests still pass.

- [ ] **Step 4: Run black and commit**

```bash
cd /Users/prutledg/troshka
black src/backend/tests/test_kubevirt_provider.py
git add src/backend/tests/test_kubevirt_provider.py
git commit -m "test: comprehensive tests for kubevirt provider driver

Tests cover all 18 ProviderDriver methods plus deploy_project,
destroy_project, and get_vm_states. Mocked K8s API calls verify
correct namespace usage, label selectors, and resource creation."
```

---
