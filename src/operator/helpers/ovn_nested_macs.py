"""Keep OVN LSP port security off for nested-MAC / migration networks.

OCP 4.20 OVN-K ignores NAD ``portSecurity: false`` and does not yet honor
``macSecurityMode: disabled`` on Multus NADs. Nested macvlan (CCLM
lm-network) needs the OKEP-3926 shape on virt-launcher LSPs:

- empty ``port_security``
- ``addresses=unknown``
- ``options:force_fdb_lookup=true`` (learn nested MACs via FDB)

ovnkube re-asserts the primary MAC on reconcile, so Troshka must re-apply
this periodically.
"""

from __future__ import annotations

import logging
import re
from typing import Callable

from kubernetes import client
from kubernetes.stream import stream

logger = logging.getLogger(__name__)

OVN_NAMESPACE = "openshift-ovn-kubernetes"
_NBDB_CONTAINER = "nbdb"
_OVN_NODE_LABEL = "app=ovnkube-node"

ExecFn = Callable[[list[str]], str]


def nad_logical_switch_name(nad_name: str) -> str:
    """OVN layer2 switch name derived from the Multus NAD name."""
    return f"{nad_name.replace('-', '.')}_ovn_layer2_switch"


def resolve_logical_switch(ls_list_output: str, nad_name: str) -> str | None:
    """Pick the layer2 switch for ``nad_name`` from ``ovn-nbctl ls-list`` output."""
    candidate = nad_logical_switch_name(nad_name)
    if candidate in ls_list_output:
        return candidate
    needle = nad_name.replace("-", ".")
    for match in re.finditer(r"\(([^)]+)\)", ls_list_output):
        name = match.group(1).strip()
        if needle in name and "layer2" in name:
            return name
    return None


def virt_launcher_lsp_uuids(lsp_list_output: str) -> list[str]:
    """UUIDs of virt-launcher ports from ``ovn-nbctl lsp-list`` output."""
    uuids: list[str] = []
    for line in lsp_list_output.splitlines():
        if "virt-launcher" not in line:
            continue
        parts = line.strip().split()
        if parts:
            uuids.append(parts[0])
    return uuids


def find_nbdb_pod(core_api: client.CoreV1Api) -> tuple[str, str] | None:
    """Return (pod_name, container_name) for an ovnkube-node nbdb, if any."""
    try:
        pods = core_api.list_namespaced_pod(
            OVN_NAMESPACE, label_selector=_OVN_NODE_LABEL
        )
    except client.ApiException as e:
        logger.warning("Cannot list ovnkube-node pods: %s", e)
        return None
    for pod in pods.items:
        if getattr(pod.status, "phase", None) != "Running":
            continue
        for container in pod.spec.containers or []:
            if container.name == _NBDB_CONTAINER:
                return pod.metadata.name, _NBDB_CONTAINER
    return None


def _exec_nbctl(
    core_api: client.CoreV1Api,
    pod_name: str,
    container: str,
    args: list[str],
) -> str:
    return stream(
        core_api.connect_get_namespaced_pod_exec,
        pod_name,
        OVN_NAMESPACE,
        command=["ovn-nbctl", "--no-leader-only", *args],
        container=container,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )


def apply_nested_mac_lsp(nbctl: ExecFn, uuid: str) -> None:
    """Apply OKEP-3926 nested-MAC settings to one virt-launcher LSP."""
    nbctl(["lsp-set-port-security", uuid])
    nbctl(["lsp-set-addresses", uuid, "unknown"])
    # Required so OVN learns nested (macvlan) MACs via FDB instead of
    # installing primary-MAC-only L2/port-security OpenFlow rules.
    nbctl(["set", "logical_switch_port", uuid, "options:force_fdb_lookup=true"])


def ensure_nested_mac_port_security(
    core_api: client.CoreV1Api,
    *,
    nad_name: str,
    namespace: str,
    exec_fn: ExecFn | None = None,
) -> int:
    """Clear OVN port_security and enable FDB learning on virt-launcher LSPs.

    Returns the number of LSPs updated, or 0 if nothing to do / OVN unavailable.
    """
    target: tuple[str, str] | None = None
    if exec_fn is None:
        target = find_nbdb_pod(core_api)
        if target is None:
            logger.warning(
                "No ovnkube-node/nbdb in %s — cannot clear nested-MAC port "
                "security for %s/%s",
                OVN_NAMESPACE,
                namespace,
                nad_name,
            )
            return 0

    def nbctl(args: list[str]) -> str:
        if exec_fn is not None:
            return exec_fn(args)
        assert target is not None
        return _exec_nbctl(core_api, target[0], target[1], args)

    try:
        ls_list = nbctl(["ls-list"])
    except Exception as e:
        logger.warning("ovn-nbctl ls-list failed for %s/%s: %s", namespace, nad_name, e)
        return 0

    switch = resolve_logical_switch(ls_list, nad_name)
    if not switch:
        logger.debug(
            "OVN switch for NAD %s not found yet (namespace=%s)", nad_name, namespace
        )
        return 0

    try:
        lsp_list = nbctl(["lsp-list", switch])
    except Exception as e:
        logger.warning(
            "ovn-nbctl lsp-list failed for switch %s (%s/%s): %s",
            switch,
            namespace,
            nad_name,
            e,
        )
        return 0

    uuids = virt_launcher_lsp_uuids(lsp_list)
    if not uuids:
        return 0

    cleared = 0
    for uuid in uuids:
        try:
            apply_nested_mac_lsp(nbctl, uuid)
            cleared += 1
        except Exception as e:
            logger.warning(
                "Failed clearing LSP %s on %s (%s/%s): %s",
                uuid,
                switch,
                namespace,
                nad_name,
                e,
            )
    if cleared:
        logger.info(
            "Applied nested-MAC OVN settings on %d virt-launcher LSP(s) for "
            "%s/%s (switch=%s)",
            cleared,
            namespace,
            nad_name,
            switch,
        )
    return cleared
