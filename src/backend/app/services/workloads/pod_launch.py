"""Launch the in-project runner pod. The pod image IS the resolved EE image;
the AgnosticD-v2 checkout, extra-vars, inventory and cluster-access are delivered
as read-only 0600 mounts, and the pod runs ansible-playbook directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import yaml

from app.services.troshkad_client import start_job

_WORKDIR = "/workdir"


@dataclass
class RunPaths:
    extra_vars: str = f"{_WORKDIR}/extra-vars.yml"
    inventory: str = f"{_WORKDIR}/inventory.troshka.yml"
    cluster_access: str = f"{_WORKDIR}/cluster-access.json"
    cloud_creds: str = f"{_WORKDIR}/cloud-creds.env"
    agnosticd: str = f"{_WORKDIR}/agnosticd-v2"
    collections: str = f"{_WORKDIR}/collections"
    log: str = f"{_WORKDIR}/run.log"


def build_artifact_files(
    *,
    extra_vars: dict,
    inventory_yaml: str,
    cluster_access: dict,
    cloud_creds: dict | None,
    paths: RunPaths,
) -> dict[str, str]:
    """Map container paths to artifact contents for 0600 read-only mounts.

    All sensitive data (cluster tokens, cloud credentials) rides in these files,
    not in the pod command argv.
    """
    files = {
        paths.extra_vars: yaml.safe_dump(extra_vars, sort_keys=False),
        paths.inventory: inventory_yaml,
        paths.cluster_access: json.dumps(cluster_access),
    }
    if cloud_creds:
        files[paths.cloud_creds] = "\n".join(f"{k}={v}" for k, v in cloud_creds.items())
    return files


def build_run_command(_resolved_item, paths: RunPaths) -> list[str]:
    """Build the ansible-playbook invocation for the AgnosticD-v2 openshift-workloads config.

    Runs ONLY the software/workloads stage against existing infra (no infra deploy).
    Entrypoint confirmed against ~/agnosticd-v2/ansible/main.yml (imports
    configs/{config}/software.yml which runs the openshift_workload_deployer role).

    Output is tee'd to a logfile so the monitor can tail progress.
    """
    # item vars / requirements_content are threaded in Task 6 (run_service)
    # Build the ansible-playbook command - always run main.yml with config=openshift-workloads
    # and cloud_provider=none (we're running against existing infra, not deploying new).
    # Tee output to logfile for monitor (pipefail preserves playbook exit code).
    script_parts = [
        "set -euo pipefail",
        f"cd {paths.agnosticd}/ansible",
        f"export ANSIBLE_COLLECTIONS_PATH={paths.collections}",
        "ansible-playbook main.yml",
        f"-i {paths.inventory}",
        f"-e @{paths.extra_vars}",
        "-e ACTION=provision",
        "-e cloud_provider=none",
        f"2>&1 | tee {paths.log}",
    ]
    script = "; ".join(script_parts)
    return ["bash", "-lc", script]


def launch_runner_pod(
    host,
    project,
    *,
    ee_image: str,
    command: list[str],
    files: dict[str, str],
    networks: list,
) -> str:
    """Launch the workload runner pod on the host, returning a job/pod identifier."""
    if getattr(host, "host_type", None) == "kubevirt-cluster":
        return _launch_kubevirt(
            host,
            project,
            ee_image=ee_image,
            command=command,
            files=files,
            cluster_nads=networks,
        )
    return _launch_troshkad(
        host,
        project,
        ee_image=ee_image,
        command=command,
        files=files,
        networks=networks,
    )


def _launch_troshkad(
    host,
    project,
    *,
    ee_image: str,
    command: list[str],
    files: dict[str, str],
    networks: list,
) -> str:
    """troshkad (podman) runner-pod path: shape /pods/create params and start the job."""
    container = {
        "name": "runner",
        "image": ee_image,
        "cpus": 2,
        "memory": 4096,
        "env": {},
        "mounts": [],
        "command": command,
        "privileged": True,
    }
    params = {
        "project_id": project.id,
        "pod_name": "workload-runner",
        "networks": networks,
        "init_containers": [],
        "containers": [container],
        "volumes": [],
        "files": files,
        "restart_policy": "never",
        "privileged": True,
    }
    return start_job(host, "/pods/create", params)


def _launch_kubevirt(
    host,
    project,
    *,
    ee_image: str,
    command: list[str],
    files: dict[str, str],
    cluster_nads: list,
) -> str:
    """KubeVirt runner-pod path: build Pod+Secret manifests and create via k8s."""
    from app.services.ocp.ops_pod_scaffold import build_ops_pod_kubevirt_manifests
    from app.services.providers.kubevirt import create_ops_pod

    provider = _provider_for_host(host)
    namespace = _namespace_for_project(project)
    pod, secret = build_ops_pod_kubevirt_manifests(
        namespace=namespace,
        project_id=project.id,
        command=command,
        env={},
        config_files=files,
        cluster_nads=cluster_nads,
        bmc_nad=None,
        dns_nameserver="",
        image=ee_image,
        pod_name="workload-runner",
    )
    create_ops_pod(provider, project.id, pod, secret)
    return f"workload-runner-{project.id[:8]}"


def _provider_for_host(host):
    """Resolve the provider row for a KubeVirt host (mirrors deploy_service _ops_pod_provider)."""
    # Import in function scope to avoid circular imports
    from app.core.database import get_db

    s = next(get_db())
    try:
        from app.models.provider import Provider

        provider = s.get(Provider, host.provider_id)
        if not provider:
            raise RuntimeError(f"Provider {host.provider_id} not found for runner pod")
        return provider
    finally:
        s.close()


def _namespace_for_project(project):
    """Resolve the project namespace (mirrors deploy_service _kubevirt_project_ns)."""
    # Import in function scope to avoid circular imports
    from app.core.database import get_db

    s = next(get_db())
    try:
        from app.models.provider import Provider

        provider = s.get(Provider, project.provider_id)
        if not provider:
            raise RuntimeError(
                f"Provider {project.provider_id} not found for namespace"
            )
        from app.services.providers.kubevirt import _project_ns

        return _project_ns(provider, project.id)
    finally:
        s.close()
