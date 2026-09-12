"""Resolve per-cluster {api_url, api_token} for OCP workloads.

Bootstraps a Kubernetes client from the stored admin kubeconfig (persisted on
the control-plane node in Project.topology by the OCP install / recert flow),
ensures a cluster-admin ServiceAccount, and mints a short-lived bearer token via
the TokenRequest API — mirroring agnosticd's openshift_cluster_admin_service_account.
"""

from __future__ import annotations

import yaml

from app.services.deploy_service import _stored_cluster_creds


class ClusterAccessError(Exception):
    pass


def _core_v1_from_kubeconfig(kubeconfig_str: str) -> tuple:
    """Bootstrap a Kubernetes CoreV1Api client and return API host."""
    from kubernetes import client, config

    data = yaml.safe_load(kubeconfig_str)
    api_client = config.new_client_from_config_dict(data)
    core = client.CoreV1Api(api_client)
    host = api_client.configuration.host
    return core, host


def _ensure_admin_sa(core, sa_name: str, namespace: str) -> None:
    """Ensure cluster-admin ServiceAccount and ClusterRoleBinding exist."""
    from kubernetes import client
    from kubernetes.client.rest import ApiException

    try:
        core.create_namespaced_service_account(
            namespace,
            client.V1ServiceAccount(metadata=client.V1ObjectMeta(name=sa_name)),
        )
    except ApiException as exc:
        if exc.status != 409:  # already exists is fine
            raise
    rbac = client.RbacAuthorizationV1Api(core.api_client)
    binding = client.V1ClusterRoleBinding(
        metadata=client.V1ObjectMeta(name=f"{sa_name}-cluster-admin"),
        role_ref=client.V1RoleRef(
            api_group="rbac.authorization.k8s.io",
            kind="ClusterRole",
            name="cluster-admin",
        ),
        subjects=[
            client.RbacV1Subject(
                kind="ServiceAccount", name=sa_name, namespace=namespace
            )
        ],
    )
    try:
        rbac.create_cluster_role_binding(binding)
    except ApiException as exc:
        if exc.status != 409:
            raise


def _mint_admin_token(
    kubeconfig_str: str,
    *,
    sa_name: str = "troshka-workloads-admin",
    namespace: str = "default",
    ttl_seconds: int = 3600,
) -> tuple[str, str]:
    """Mint a short-lived bearer token for cluster-admin ServiceAccount.

    Args:
        kubeconfig_str: YAML kubeconfig string for admin access.
        sa_name: ServiceAccount name to create/use.
        namespace: Namespace for ServiceAccount.
        ttl_seconds: Token expiration time in seconds.

    Returns:
        Tuple of (api_url, token).
    """
    from kubernetes import client

    core, host = _core_v1_from_kubeconfig(kubeconfig_str)
    _ensure_admin_sa(core, sa_name, namespace)
    req = client.AuthenticationV1TokenRequest(
        spec=client.V1TokenRequestSpec(expiration_seconds=ttl_seconds, audiences=[])
    )
    resp = core.create_namespaced_service_account_token(sa_name, namespace, req)
    return host, resp.status.token


def resolve_cluster_access(project, cluster_id: str | None = None) -> dict[str, dict]:
    """Resolve {api_url, api_token} for cluster(s) in a project.

    Args:
        project: Project object with deployed_topology or topology.
        cluster_id: Optional cluster ID to filter. If None, resolves all.

    Returns:
        Dict mapping cluster ID -> {api_url, api_token}.

    Raises:
        ClusterAccessError: If no stored cluster credentials found.
    """
    topology = project.deployed_topology or project.topology or {}
    creds = _stored_cluster_creds(topology)
    if cluster_id is not None:
        creds = {k: v for k, v in creds.items() if k == cluster_id}
    if not creds:
        raise ClusterAccessError("no stored cluster credentials for project")
    out: dict[str, dict] = {}
    for cid, (_pw, kubeconfig) in creds.items():
        api_url, token = _mint_admin_token(kubeconfig)
        out[cid] = {"api_url": api_url, "api_token": token}
    return out
