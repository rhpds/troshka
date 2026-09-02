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
