"""Launch the in-project runner pod. The pod image IS the resolved EE image;
the AgnosticD-v2 checkout, extra-vars, inventory and cluster-access are delivered
as read-only 0600 mounts, and the pod runs ansible-playbook directly.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from app.services.troshkad_client import start_job

_WORKDIR = "/workdir"


@dataclass
class RunPaths:
    extra_vars: str = f"{_WORKDIR}/extra-vars.yml"
    inventory: str = f"{_WORKDIR}/inventory.troshka.yml"
    cloud_creds: str = f"{_WORKDIR}/cloud-creds.env"
    agnosticd: str = f"{_WORKDIR}/agnosticd-v2"
    log: str = f"{_WORKDIR}/run.log"
    kubeconfig: str = f"{_WORKDIR}/kubeconfig"
    mint_playbook: str = f"{_WORKDIR}/mint_cluster_admin.yml"
    clusters: str = f"{_WORKDIR}/clusters.yml"


def _mint_prelude_playbook(paths: RunPaths) -> str:
    """Static prelude playbook: mint a cluster-admin SA token in-pod and write the
    `clusters` extra-var that the openshift-workloads config consumes.

    Runs the agnosticd role ``openshift_cluster_admin_service_account`` (which sets
    the role-internal fact ``_openshift_cluster_admin_token``, b64-decoded), derives
    the in-cluster API server URL from the delivered KUBECONFIG, and writes
    ``clusters: {default: {api_url, api_token}}`` to ``paths.clusters``. The backend
    makes NO cluster calls (D12); everything here executes in-pod at runtime. The
    token is written to a 0600 file, never placed in argv/env (D7).
    """
    clusters_expr = (
        "{{ {'clusters': {'default': {"
        "'api_url': _troshka_api_server.stdout, "
        "'api_token': _openshift_cluster_admin_token}}} | to_nice_yaml }}"
    )
    playbook = [
        {
            "name": "Mint cluster-admin token and build clusters dict",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "tasks": [
                {
                    "name": "Create cluster-admin service account and token",
                    "ansible.builtin.include_role": {
                        "name": "openshift_cluster_admin_service_account"
                    },
                },
                {
                    "name": "Read API server URL from delivered kubeconfig",
                    "ansible.builtin.command": {
                        "cmd": "oc config view --minify "
                        "-o jsonpath={.clusters[0].cluster.server}"
                    },
                    "register": "_troshka_api_server",
                    "changed_when": False,
                },
                {
                    "name": "Write clusters dict for openshift-workloads",
                    "ansible.builtin.copy": {
                        "dest": paths.clusters,
                        "mode": "0600",
                        "content": clusters_expr,
                    },
                    "no_log": True,
                },
            ],
        }
    ]
    return yaml.safe_dump(playbook, sort_keys=False, default_flow_style=False)


def build_artifact_files(
    *,
    extra_vars: dict,
    inventory_yaml: str,
    cloud_creds: dict | None,
    kubeconfig: str | None = None,
    paths: RunPaths,
) -> dict[str, str]:
    """Map container paths to artifact contents for 0600 read-only mounts.

    All sensitive data (cluster tokens, cloud credentials) rides in these files,
    not in the pod command argv.
    """
    files = {
        paths.extra_vars: yaml.safe_dump(extra_vars, sort_keys=False),
        paths.inventory: inventory_yaml,
    }
    if cloud_creds:
        files[paths.cloud_creds] = "\n".join(f"{k}={v}" for k, v in cloud_creds.items())
    if kubeconfig:
        files[paths.kubeconfig] = kubeconfig
        # OCP run: deliver the in-pod mint prelude that produces `clusters`.
        files[paths.mint_playbook] = _mint_prelude_playbook(paths)
    return files


def _safe_sq(value: str) -> str:
    """Escape a value for single-quoted shell interpolation."""
    return value.replace("'", "'\\''")


def _mint_prelude_steps(paths: RunPaths, log_path: str) -> list[str]:
    """Command step(s) running the in-pod mint prelude playbook.

    Runs AFTER install_dynamic_dependencies (so kubernetes.core + agnosticd.core
    collections are installed) and BEFORE main.yml. Writes `clusters` to
    ``paths.clusters`` for main.yml to consume via ``-e @``. ``output_dir`` is set
    so the role's agnosticd_user_info task has a writable target in-pod.
    """
    safe_mint = _safe_sq(paths.mint_playbook)
    safe_log = _safe_sq(log_path)
    return [
        f"ansible-playbook '{safe_mint}' -e output_dir='{_safe_sq(_WORKDIR)}' 2>&1 | tee -a '{safe_log}'",
    ]


def build_run_command(
    _resolved_item,
    paths: RunPaths,
    *,
    agnosticd_v2_url: str,
    scm_ref: str,
    kubeconfig: str | None = None,
) -> list[str]:
    """Build the runner pod command: clone agnosticd-v2, install collections, run playbook.

    Clones agnosticd-v2 at the specified scm_ref INSIDE the pod, then runs
    install_dynamic_dependencies.yml (which installs collections from requirements_content
    in extra_vars). For OCP runs (a kubeconfig was delivered) it then exports KUBECONFIG,
    runs the in-pod mint prelude (which mints a cluster-admin token and writes the
    `clusters` extra-var), and passes ``-e @clusters.yml`` to main.yml. VM-only runs
    (no kubeconfig) get a clean command with no KUBECONFIG/mint/clusters wiring.

    Mirrors the EE entrypoint pattern: install_dynamic_dependencies.yml before main.yml.

    Output is tee'd to a logfile so the monitor can tail progress.
    """
    has_kubeconfig = bool(kubeconfig)
    # Shell-quote the ref and url for safety (they come from config, but keep them safe)
    safe_url = _safe_sq(agnosticd_v2_url)
    safe_ref = _safe_sq(scm_ref)
    safe_agnosticd = _safe_sq(paths.agnosticd)
    safe_extra_vars = _safe_sq(paths.extra_vars)
    safe_inventory = _safe_sq(paths.inventory)
    safe_log = _safe_sq(paths.log)

    script_parts = [
        "set -euo pipefail",
        # Clone agnosticd-v2 at the specified ref (try --branch first, fall back to checkout)
        f"git clone --depth 1 --branch '{safe_ref}' '{safe_url}' '{safe_agnosticd}' 2>/dev/null"
        f" || {{ git clone '{safe_url}' '{safe_agnosticd}' && git -C '{safe_agnosticd}' checkout '{safe_ref}'; }}",
        # Change to agnosticd-v2/ansible directory
        f"cd '{safe_agnosticd}/ansible'",
        # Point ANSIBLE_CONFIG to repo root's ansible.cfg (roles_path, etc.)
        f"export ANSIBLE_CONFIG='{safe_agnosticd}/ansible.cfg'",
    ]
    # Export KUBECONFIG only when one was delivered (OCP runs).
    if has_kubeconfig:
        script_parts.append(f"export KUBECONFIG='{_safe_sq(paths.kubeconfig)}'")
    # Install dynamic dependencies (collections from requirements_content)
    script_parts.append(
        "ansible-playbook install_dynamic_dependencies.yml"
        f" -e @'{safe_extra_vars}'"
        " -e config=openshift-workloads"
    )
    # Mint cluster-admin token + build `clusters` (after deps, before main.yml).
    if has_kubeconfig:
        script_parts.extend(_mint_prelude_steps(paths, paths.log))
    # Run main playbook (consume `clusters` for OCP runs).
    main_cmd = (
        "ansible-playbook main.yml"
        f" -i '{safe_inventory}'"
        f" -e @'{safe_extra_vars}'"
    )
    if has_kubeconfig:
        main_cmd += f" -e @'{_safe_sq(paths.clusters)}'"
    main_cmd += (
        " -e ACTION=provision" " -e cloud_provider=none" f" 2>&1 | tee '{safe_log}'"
    )
    script_parts.append(main_cmd)

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
    from app.services.providers.kubevirt import _project_ns, create_ops_pod

    # Resolve the provider from the HOST (a project row carries no provider_id;
    # the KubeVirt provider lives on the host). Reuse it for the namespace too.
    provider = _provider_for_host(host)
    namespace = _project_ns(provider, project.id)
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
        restart_policy="Never",
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
