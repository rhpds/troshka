"""Launch the in-project runner pod. The pod image IS the resolved EE image;
the AgnosticD-v2 checkout, extra-vars, inventory and cluster-access are delivered
as read-only 0600 mounts, and the pod runs ansible-playbook directly.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from app.services.troshkad_client import start_job

_WORKDIR = "/workdir"
# Wait for each cluster API before minting SA (covers post-install Multus blips).
_API_WAIT_RETRIES = 30
_API_WAIT_DELAY_S = 10


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


def _wait_for_cluster_api_task(name: str | None = None) -> dict:
    """Ansible task: retry ``oc get --raw=/version`` until the API answers.

    Catches transient ``No route to host`` / DNS blips after install reports
    ready. ``name`` is only for the task label (multi-cluster).
    """
    label = f"Wait for {name} API" if name else "Wait for cluster API"
    return {
        "name": label,
        "ansible.builtin.command": {"cmd": "oc get --raw=/version"},
        "register": "_troshka_api_wait",
        "retries": _API_WAIT_RETRIES,
        "delay": _API_WAIT_DELAY_S,
        "until": "_troshka_api_wait is succeeded",
        "changed_when": False,
    }


def _mint_prelude_playbook(
    paths: RunPaths, cluster_kubeconfigs: dict[str, str] | None = None
) -> str:
    """Mint cluster-admin SA token(s) in-pod and write the ``clusters`` extra-var.

    Single-cluster (legacy): mints into ``clusters.default``.
    Multi-cluster: one block per name under ``/workdir/kubeconfigs/<name>/``,
    writing ``clusters: {<name>: {api_url, api_token}, ..., default: <first>}``.
    """
    names = [n for n in (cluster_kubeconfigs or {}) if n]
    if len(names) <= 1:
        return _mint_prelude_single(paths)

    tasks: list[dict] = [
        {
            "name": "Init clusters accumulator",
            "ansible.builtin.set_fact": {"_troshka_clusters": {}},
        }
    ]
    for name in names:
        kc_path = f"{_WORKDIR}/kubeconfigs/{name}/kubeconfig"
        tasks.append(
            {
                "name": f"Mint and record cluster {name}",
                "block": [
                    _wait_for_cluster_api_task(name),
                    {
                        "name": f"Create cluster-admin SA on {name}",
                        "ansible.builtin.include_role": {
                            "name": "openshift_cluster_admin_service_account"
                        },
                    },
                    {
                        "name": f"Read API server for {name}",
                        "ansible.builtin.command": {
                            "cmd": (
                                "oc config view --minify "
                                "-o jsonpath={.clusters[0].cluster.server}"
                            )
                        },
                        "register": "_troshka_api_server",
                        "changed_when": False,
                    },
                    {
                        "name": f"Accumulate {name}",
                        # One closing brace for the inner dict, then "}) }}" for
                        # combine(...) and the outer Jinja — an extra "}" here
                        # breaks Ansible with: unexpected '}', expected ')'.
                        "ansible.builtin.set_fact": {
                            "_troshka_clusters": (
                                "{{ _troshka_clusters | combine({"
                                f"'{name}': {{"
                                "'api_url': _troshka_api_server.stdout, "
                                "'api_token': _openshift_cluster_admin_token}"
                                "}) }}"
                            )
                        },
                        "no_log": True,
                    },
                ],
                # kubernetes.core honors K8S_AUTH_KUBECONFIG, not KUBECONFIG. The
                # outer mint shell also sets K8S_AUTH_KUBECONFIG to the primary
                # kubeconfig — without per-block override, every cluster's SA
                # is minted against source and destination tokens 401.
                "environment": {
                    "KUBECONFIG": kc_path,
                    "K8S_AUTH_KUBECONFIG": kc_path,
                },
            }
        )
    first = names[0]
    tasks.append(
        {
            "name": "Write multi-cluster clusters dict",
            "ansible.builtin.copy": {
                "dest": paths.clusters,
                "mode": "0600",
                "content": (
                    "{{ {'clusters': _troshka_clusters | combine({"
                    f"'default': _troshka_clusters['{first}']"
                    "})} | to_nice_yaml }}"
                ),
            },
            "no_log": True,
        }
    )
    playbook = [
        {
            "name": "Mint cluster-admin tokens for all OCP clusters",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "tasks": tasks,
        }
    ]
    return yaml.safe_dump(playbook, sort_keys=False, default_flow_style=False)


def _mint_prelude_single(paths: RunPaths) -> str:
    """Single-kubeconfig mint → ``clusters.default``."""
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
                _wait_for_cluster_api_task(),
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
    cluster_kubeconfigs: dict[str, str] | None = None,
    paths: RunPaths,
) -> dict[str, str]:
    """Map container paths to artifact contents for 0600 read-only mounts.

    All sensitive data (cluster tokens, cloud credentials) rides in these files,
    not in the pod command argv.

    ``cluster_kubeconfigs`` (optional) maps cluster name → kubeconfig YAML for
    multi-cluster mint. When provided (2+ entries), each is written under
    ``/workdir/kubeconfigs/<name>/kubeconfig`` and the mint prelude stamps a
    named ``clusters`` dict. ``kubeconfig`` remains the primary KUBECONFIG export.
    """
    files = {
        paths.extra_vars: yaml.safe_dump(extra_vars, sort_keys=False),
        paths.inventory: inventory_yaml,
    }
    if cloud_creds:
        files[paths.cloud_creds] = "\n".join(f"{k}={v}" for k, v in cloud_creds.items())
    named = {k: v for k, v in (cluster_kubeconfigs or {}).items() if k and v}
    primary = kubeconfig or next(iter(named.values()), None)
    if primary:
        files[paths.kubeconfig] = primary
        for name, kc in named.items():
            files[f"{_WORKDIR}/kubeconfigs/{name}/kubeconfig"] = kc
        files[paths.mint_playbook] = _mint_prelude_playbook(
            paths, named if len(named) > 1 else None
        )
    return files


def _safe_sq(value: str) -> str:
    """Escape a value for single-quoted shell interpolation."""
    return value.replace("'", "'\\''")


# troshkad names a pod's containers "troshka-<pid8>-<pod_name>-<container_name>"
# (see deploy_service._start_pod / _pod_create_params). The runner pod is
# pod_name="workload-runner" with a single container name="runner".
_TROSHKAD_RUNNER_POD = "workload-runner"
_TROSHKAD_RUNNER_CONTAINER = "runner"


def _troshkad_runner_pod_name(project_id: str) -> str:
    return f"troshka-{project_id[:8]}-{_TROSHKAD_RUNNER_POD}"


def troshkad_runner_container_name(project_id: str) -> str:
    """Full podman container name of the troshkad runner (what /containers/states
    keys by and /containers/exec expects) — NOT the bare "runner"."""
    return f"{_troshkad_runner_pod_name(project_id)}-{_TROSHKAD_RUNNER_CONTAINER}"


def _mint_prelude_steps(paths: RunPaths, log_path: str) -> list[str]:
    """Command step(s) running the in-pod mint prelude playbook.

    Runs AFTER install_dynamic_dependencies (so kubernetes.core is available) and
    BEFORE main.yml. Writes `clusters` to ``paths.clusters`` for main.yml to
    consume via ``-e @``. ``output_dir`` is set so the role's agnosticd_user_info
    task has a writable target in-pod.

    The playbook is COPIED next to main.yml (``{agnosticd}/ansible/``) before it
    runs: ``agnosticd.core`` is bundled in the repo's ``ansible/collections`` and
    is only discoverable via the playbook-adjacent collections dir. Running the
    mounted playbook from ``/workdir`` would not resolve ``agnosticd.core`` (its
    role uses ``agnosticd.core.agnosticd_user_info``), so it must share main.yml's
    playbook_dir.

    ``K8S_AUTH_KUBECONFIG`` is set for the mint invocation so ``kubernetes.core.k8s``
    (used by the role to create the cluster-admin SA) authenticates via the
    delivered admin kubeconfig against the target cluster. Plain ``KUBECONFIG`` is
    NOT honored by kubernetes.core inside a pod — without this it falls back to the
    runner pod's own in-cluster ServiceAccount (which has no rights on the target).

    Multi-cluster mint playbooks also set ``K8S_AUTH_KUBECONFIG`` per block to the
    matching ``/workdir/kubeconfigs/<name>/kubeconfig`` (overrides this shell
    default) so each SA is minted on the correct cluster.
    """
    safe_mint = _safe_sq(paths.mint_playbook)
    safe_log = _safe_sq(log_path)
    safe_kubeconfig = _safe_sq(paths.kubeconfig)
    dest = f"{paths.agnosticd}/ansible/_troshka_mint_cluster_admin.yml"
    safe_dest = _safe_sq(dest)
    return [
        f"cp '{safe_mint}' '{safe_dest}'",
        f"K8S_AUTH_KUBECONFIG='{safe_kubeconfig}' ansible-playbook '{safe_dest}'"
        f" -e output_dir='{_safe_sq(_WORKDIR)}' 2>&1 | tee -a '{safe_log}'",
    ]


def build_run_command(
    _resolved_item,
    paths: RunPaths,
    *,
    agnosticd_v2_url: str,
    scm_ref: str,
    kubeconfig: str | None = None,
    net_prelude: str = "",
    limit: str | None = None,
) -> list[str]:
    """Build the runner pod command: clone agnosticd-v2, install collections, run playbook.

    Clones agnosticd-v2 at the specified scm_ref INSIDE the pod, then runs
    install_dynamic_dependencies.yml (which installs collections from requirements_content
    in extra_vars). For OCP runs (a kubeconfig was delivered) it then exports KUBECONFIG,
    runs the in-pod mint prelude (which mints a cluster-admin token and writes the
    `clusters` extra-var), and passes ``-e @clusters.yml`` to main.yml. VM-only runs
    (no kubeconfig) get a clean command with no KUBECONFIG/mint/clusters wiring.

    ``net_prelude`` (KubeVirt only) is a block of ``ip addr add`` lines run FIRST so
    the pod self-assigns its lab-net IP (OVN-L2 NADs have no IPAM) before it clones
    or resolves anything. Empty on troshkad (podman does IPAM).

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

    script_parts = ["set -euo pipefail"]
    # Self-assign lab-net IP(s) FIRST (KubeVirt OVN-L2 has no IPAM); empty on troshkad.
    if net_prelude.strip():
        script_parts.append(net_prelude.strip())
    script_parts += [
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
    main_cmd += " -e ACTION=provision" " -e cloud_provider=none"
    if limit:
        main_cmd += f" --limit '{_safe_sq(limit)}'"
    main_cmd += f" 2>&1 | tee '{safe_log}'"
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
    dns_nameserver: str = "",
    host_aliases: list[dict] | None = None,
    topology: dict | None = None,
) -> str:
    """Launch the workload runner pod on the host, returning a job/pod identifier.

    ``dns_nameserver`` points the pod at the project dnsmasq so it resolves the
    cluster API (``api.<cluster>.local``). On KubeVirt it becomes the pod's
    ``dnsConfig``; on troshkad the resolver is already embedded per-network entry.
    """
    if getattr(host, "host_type", None) == "kubevirt-cluster":
        return _launch_kubevirt(
            host,
            project,
            ee_image=ee_image,
            command=command,
            files=files,
            cluster_nads=networks,
            dns_nameserver=dns_nameserver,
            host_aliases=host_aliases,
            topology=topology,
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
    # /pods/create only CREATES the pod (containers land in "Created"); a separate
    # /pods/start is required to actually run them (mirrors the ops pod's
    # create-then-_start_pod). Without the start the runner never executes and the
    # monitor sees a non-running container.
    from app.services.troshkad_client import wait_for_job

    job_id = start_job(host, "/pods/create", params)
    wait_for_job(host, job_id, timeout=300)
    full_pod_name = _troshkad_runner_pod_name(project.id)
    start_job(host, "/pods/start", {"pod_name": full_pod_name})
    return full_pod_name


def _launch_kubevirt(
    host,
    project,
    *,
    ee_image: str,
    command: list[str],
    files: dict[str, str],
    cluster_nads: list,
    dns_nameserver: str = "",
    host_aliases: list[dict] | None = None,
    topology: dict | None = None,
) -> str:
    """KubeVirt runner-pod path: build Pod+Secret manifests and create via k8s."""
    from app.services.ocp.ops_pod_scaffold import build_ops_pod_kubevirt_manifests
    from app.services.providers.kubevirt import _project_ns, create_ops_pod

    # Resolve the provider from the HOST (a project row carries no provider_id;
    # the KubeVirt provider lives on the host). Reuse it for the namespace too.
    provider = _provider_for_host(host)
    namespace = _project_ns(provider, project.id)
    from app.services.project_ceph import topology_has_ceph

    pod, secret = build_ops_pod_kubevirt_manifests(
        namespace=namespace,
        project_id=project.id,
        command=command,
        env={},
        config_files=files,
        cluster_nads=cluster_nads,
        bmc_nad=None,
        dns_nameserver=dns_nameserver,
        host_aliases=host_aliases,
        image=ee_image,
        pod_name="workload-runner",
        restart_policy="Never",
        mount_project_ceph_secret=topology_has_ceph(topology or {}),
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
