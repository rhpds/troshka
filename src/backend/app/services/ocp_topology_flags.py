"""Per-VM OCP checkbox flags on canvas topology nodes."""


def _vm_name(node: dict) -> str:
    data = node.get("data", {})
    return (data.get("label") or data.get("name") or "").lower()


def rhcos_vms(topology: dict) -> list[dict]:
    return [
        n
        for n in topology.get("nodes", [])
        if n.get("type") == "vmNode" and n.get("data", {}).get("os") == "rhcos"
    ]


def has_bastion_vm(topology: dict) -> bool:
    return any(
        n.get("type") == "vmNode" and _vm_name(n) == "bastion"
        for n in topology.get("nodes", [])
    )


def _flag_unset(data: dict, key: str) -> bool:
    return data.get(key) is None


def _cluster_members(topology: dict, cluster_id: str) -> list[dict]:
    return [
        n
        for n in topology.get("nodes", [])
        if n.get("type") == "vmNode"
        and n.get("data", {}).get("clusterId") == cluster_id
    ]


def _is_control_plane(node: dict) -> bool:
    data = node.get("data", {})
    role = data.get("clusterRole")
    if role in ("control-plane", "worker"):
        return role == "control-plane"
    group = data.get("tags", {}).get("AnsibleGroup")
    return isinstance(group, str) and "controllers" in group


def apply_cluster_ocp_flags(topology: dict) -> bool:
    """Project cluster-level OCP flags onto member VMs.

    The cluster object in ``topology["clusters"]`` is the source of truth for
    ``recert`` / ``monitorHealth`` / ``configureBastionBrowser`` (camelCase, as
    the frontend ClusterConfig stores them). This projects them onto member VM
    nodes so the per-VM deploy machinery keeps working unchanged:

    - ``recert``               -> control-plane members' ``recertEnabled``
    - ``monitorHealth``        -> the monitor VM (first control plane) ``ocpMonitor``
    - ``configureBastionBrowser`` -> that VM's ``configureBastionBrowser`` (+ ``ocpMonitor``)

    Additive: only ever SETS flags True, never clears them, so explicit per-VM
    flags on legacy topologies remain honored. Returns True if any node changed.
    """
    changed = False

    def _set(node: dict, key: str) -> None:
        nonlocal changed
        data = node.setdefault("data", {})
        if not data.get(key):
            data[key] = True
            changed = True

    for cluster in topology.get("clusters", []):
        cid = cluster.get("id")
        if not cid:
            continue
        members = _cluster_members(topology, cid)
        if not members:
            continue
        cps = [m for m in members if _is_control_plane(m)]
        monitor_target = (cps or members)[0]
        if cluster.get("recert"):
            for m in cps or members:
                _set(m, "recertEnabled")
        if cluster.get("monitorHealth") or cluster.get("configureBastionBrowser"):
            _set(monitor_target, "ocpMonitor")
        if cluster.get("configureBastionBrowser"):
            _set(monitor_target, "configureBastionBrowser")
    return changed


def apply_sno_ocp_vm_flags(topology: dict, *, recert: bool = False) -> None:
    """Set default OCP checkboxes on the single RHCOS VM when unset (null/missing).

    Explicit False is preserved. Used when deploying or capturing SNO patterns so the
    canvas UI shows recert / monitor / bastion-browser before deploy runs.
    """
    vms = rhcos_vms(topology)
    if len(vms) != 1:
        return

    data = vms[0].setdefault("data", {})
    if recert and _flag_unset(data, "recertEnabled"):
        data["recertEnabled"] = True
    if _flag_unset(data, "ocpMonitor"):
        data["ocpMonitor"] = True
    if has_bastion_vm(topology) and _flag_unset(data, "configureBastionBrowser"):
        data["configureBastionBrowser"] = True
        data["ocpMonitor"] = True
