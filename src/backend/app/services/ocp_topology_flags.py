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
