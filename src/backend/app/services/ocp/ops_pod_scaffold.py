"""Ops-pod network + spec builder (Plan 4, Task 4).

The in-cluster *ops pod* runs the OpenShift agent-based install for one or more
clusters entirely from inside the project network — no bastion VM required. This
module shapes the payload the pod-create logic (Task 5) consumes: a transit-side
network on the project netns and a pod spec carrying the project-scoped API key,
per-cluster install/agent configs and the pull secret.

Everything here is pure payload shaping and unit-testable; no live pod, network
or file is created. The `files` map (path -> content) is materialised into the
pod's workdir by the pod-create runner in Task 5.
"""

from __future__ import annotations

from app.services.deploy_topology import showroom_transit_octet3

# Placeholder ops-pod execution-environment image. The real image is built and
# published by Task 8 / Plan 4b CI; this constant is overridable from config
# (`ocp.ops_pod_image`) so a deployment can point at its own registry ref.
_OPS_POD_IMAGE_DEFAULT = "quay.io/redhat-gpte/troshka-ops-pod:latest"


def _resolve_ops_pod_image() -> str:
    """Ops-pod image ref, from `ocp.ops_pod_image` config if set, else default."""
    try:
        from app.core.config import config as app_config

        ocp_cfg = getattr(app_config, "ocp", None)
        image = getattr(ocp_cfg, "ops_pod_image", None) if ocp_cfg else None
        if image:
            return str(image)
    except Exception:
        # Config not importable (e.g. minimal test env) — fall back to default.
        pass
    return _OPS_POD_IMAGE_DEFAULT


OPS_POD_IMAGE: str = _resolve_ops_pod_image()

# Workdir mounted into the ops pod; the install runner (Task 5) reads the
# per-cluster configs + pull secret from here.
OPS_POD_WORKDIR = "/workdir"


def ops_pod_infra_network(
    vni_map: dict, mac: str = "", dns_nameserver: str = ""
) -> list[dict]:
    """Transit-side ops-pod addressing in the project netns (not lab DHCP).

    Mirrors :func:`app.services.deploy_topology.showroom_infra_network` but uses
    a distinct pod IP (``172.30.{octet3}.4`` vs the showroom pod's ``.3``) so the
    ops pod and showroom pod can coexist on the same transit subnet.
    """
    octet3 = showroom_transit_octet3(vni_map)
    if octet3 is None:
        return []
    net: dict = {
        "bridge": "",
        "mac": mac,
        "ip": f"172.30.{octet3}.4",
        "cidr": f"172.30.{octet3}.0/24",
        "gateway": f"172.30.{octet3}.2",
        "infra_transit": True,
    }
    if dns_nameserver:
        net["dns_nameserver"] = dns_nameserver
    return [net]


def ops_pod_config_files(
    clusters: list[dict], workdir: str, pull_secret_json: str
) -> dict[str, str]:
    """Absolute ``{container_path: content}`` map for the ops pod's secret files.

    These are delivered to the pod via troshkad's ``/pods/create`` ``files``
    capability (Plan 4b, Task 8): troshkad writes each entry to a per-pod host
    dir (mode 0600) and bind-mounts it read-only at ``container_path`` — so the
    install-config/agent-config/pull-secret never appear in the pod's ``bash
    -c`` argv / ``podman inspect``.

    Files are scoped by cluster id (falling back to name) so a multi-cluster
    project's configs never collide: ``<workdir>/<clusterId>/install-config.yaml``
    and ``<workdir>/<clusterId>/agent-config.yaml``. The shared pull secret lands
    at ``<workdir>/pull-secret.json``. The install-runner script (Task 5) reads
    these exact paths after ``cd``-ing into each cluster dir.
    """
    files: dict[str, str] = {}
    for cluster in clusters:
        cluster_key = str(cluster.get("id") or cluster.get("name") or "cluster")
        cluster_dir = f"{workdir}/{cluster_key}"
        install_cfg = cluster.get("_generatedInstallConfig")
        agent_cfg = cluster.get("_generatedAgentConfig")
        if install_cfg is not None:
            files[f"{cluster_dir}/install-config.yaml"] = str(install_cfg)
        if agent_cfg is not None:
            files[f"{cluster_dir}/agent-config.yaml"] = str(agent_cfg)
    if pull_secret_json:
        files[f"{workdir}/pull-secret.json"] = pull_secret_json
    return files


# ---------------------------------------------------------------------------
# Task 8b (Plan 4b): KubeVirt ops-pod Pod + Secret spec builder (pure)
# ---------------------------------------------------------------------------
#
# On a ``kubevirt-cluster`` host there is no troshkad ``/pods/create`` — the ops
# pod is a native k8s Pod created in the project namespace. This section shapes
# the Pod + Secret manifests it needs; everything here is pure dict-building and
# unit-testable. The live k8s create lives in
# :func:`app.services.providers.kubevirt.create_ops_pod`.

# The Multus network-attachment annotation key (mirrors
# ``operator/helpers/k8s._NET_ANNOTATION_KEY`` and ``build_bmc_deployment``).
_NET_ANNOTATION_KEY = "k8s.v1.cni.cncf.io/networks"

# ServiceAccount that already carries the privileged SCC in a project namespace
# (used by the operator's privileged dnsmasq/gateway/exec deployments).
_OPS_POD_SERVICE_ACCOUNT = "troshka-network"


def _nad_name(net_node: dict) -> str:
    """Operator NAD naming convention for a network node: ``net-<id[:8]>-nad``.

    Resolves the network id the same way the operator's canonical
    ``extract_networks`` does (``operator/helpers/topology.py``): ``data.id``
    takes precedence over the node id, so the two never diverge.
    """
    node_id = net_node.get("data", {}).get("id", net_node.get("id", ""))
    return f"net-{str(node_id)[:8]}-nad"


def ops_pod_network_nads(topology: dict) -> tuple[list[str], str | None]:
    """Split a topology's network NADs into ``(cluster_nads, bmc_nad)``.

    The KubeVirt ops pod attaches to BOTH the cluster network(s) — to reach the
    nested VMs / serve the agent ISO — and the BMC network — to reach the sushy
    Redfish emulator on ``:8000`` (mirrors ``build_bmc_deployment``). NAD names
    follow the operator convention ``net-<network-id[:8]>-nad``.
    """
    cluster_nads: list[str] = []
    bmc_nad: str | None = None
    for node in (topology or {}).get("nodes", []):
        if node.get("type") != "networkNode":
            continue
        if node.get("data", {}).get("networkType") == "bmc":
            bmc_nad = _nad_name(node)
        else:
            cluster_nads.append(_nad_name(node))
    return cluster_nads, bmc_nad


def _ops_pod_secret_key(path: str) -> str:
    """k8s Secret keys can't contain ``/``; flatten an absolute file path.

    e.g. ``/workdir/cl-1/install-config.yaml`` -> ``workdir_cl-1_install-config.yaml``.
    Each config path is unique so the flattened keys stay unique too; the file is
    remounted at its ORIGINAL absolute path via a ``subPath`` volume mount.
    """
    return path.lstrip("/").replace("/", "_")


def build_ops_pod_kubevirt_manifests(
    *,
    namespace: str,
    project_id: str,
    command: list[str],
    env: dict[str, str],
    config_files: dict[str, str],
    cluster_nads: list[str],
    bmc_nad: str | None,
    image: str = OPS_POD_IMAGE,
) -> tuple[dict, dict]:
    """Build the ``(Pod, Secret)`` manifests for the KubeVirt ops pod.

    The KubeVirt analog of the troshkad ops pod (:func:`_ops_pod_create_params`):
    a privileged Pod on :data:`OPS_POD_IMAGE` attached via
    ``k8s.v1.cni.cncf.io/networks`` to BOTH the cluster NAD(s) and the BMC NAD —
    mirroring ``operator/helpers/bmc.build_bmc_deployment``'s NAD attachment plus
    its ``privileged`` + ``NET_ADMIN``/``NET_RAW`` securityContext. The
    per-cluster install/agent configs and the pull secret ride in a k8s Secret
    (the analog of Task 8's troshkad file-mount) mounted read-only at the SAME
    absolute ``<workdir>/<clusterId>/...`` paths the install script reads — so no
    secret appears in the pod argv. ``command`` is the shared install-runner
    script; ``TROSHKA_API_KEY`` rides in ``env`` (not exposed in the argv).
    ``restartPolicy`` is ``Always`` (the install script is idempotent and skips
    already-installed clusters on restart).
    """
    pid = project_id[:8]
    pod_name = f"troshka-{pid}-ops"
    secret_name = f"{pod_name}-config"
    labels = {"app": "troshka-ops-pod", "troshka-project": pid}

    nads = [n for n in [*cluster_nads, bmc_nad] if n]
    annotations = {_NET_ANNOTATION_KEY: ",".join(nads)}

    secret_data: dict[str, str] = {}
    volume_mounts: list[dict] = []
    for path, content in config_files.items():
        key = _ops_pod_secret_key(path)
        secret_data[key] = content
        volume_mounts.append(
            {
                "name": "ops-config",
                "mountPath": path,
                "subPath": key,
                "readOnly": True,
            }
        )

    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": secret_name,
            "namespace": namespace,
            "labels": labels,
        },
        "stringData": secret_data,
    }

    container = {
        "name": "ops",
        "image": image,
        "imagePullPolicy": "Always",
        "command": command,
        "env": [{"name": k, "value": v} for k, v in env.items()],
        "volumeMounts": volume_mounts,
        "securityContext": {
            "privileged": True,
            "capabilities": {"add": ["NET_ADMIN", "NET_RAW"]},
        },
        "resources": {
            "requests": {"cpu": "500m", "memory": "2Gi"},
            "limits": {"cpu": "2", "memory": "4Gi"},
        },
    }

    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "labels": labels,
            "annotations": annotations,
        },
        "spec": {
            "serviceAccountName": _OPS_POD_SERVICE_ACCOUNT,
            "restartPolicy": "Always",
            "containers": [container],
            "volumes": [{"name": "ops-config", "secret": {"secretName": secret_name}}],
        },
    }
    return pod, secret
