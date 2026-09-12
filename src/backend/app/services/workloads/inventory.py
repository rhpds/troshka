"""Emit the troshka.cloud inventory file and validate the AnsibleGroup contract."""

from __future__ import annotations

import yaml


class InventoryError(Exception):
    pass


def build_inventory_yaml(
    api_url: str, api_key: str, project_id: str, connection_mode: str = "ssh"
) -> str:
    return yaml.safe_dump(
        {
            "plugin": "troshka.cloud.troshka",
            "api_url": api_url,
            "api_key": api_key,
            "project_id": project_id,
            "connection_mode": connection_mode,
        },
        sort_keys=True,
    )


def _vm_nodes(topology: dict) -> list[dict]:
    return [n for n in (topology.get("nodes") or []) if n.get("type") == "vmNode"]


def _groups(node: dict) -> list[str]:
    raw = ((node.get("data") or {}).get("tags") or {}).get("AnsibleGroup", "")
    return [g.strip() for g in raw.split(",") if g.strip()]


def _external_ip_vm_ids(topology: dict) -> set[str]:
    return {e.get("vmId") for e in (topology.get("externalIps") or []) if e.get("ip")}


def validate_ansible_groups(topology: dict, *, require_bastion: bool) -> None:
    nodes = _vm_nodes(topology)
    if not nodes:
        raise InventoryError("project has no VM nodes to target")
    bastions: list[str] = []
    for node in nodes:
        data = node.get("data") or {}
        if not data.get("name"):
            raise InventoryError("a VM node is missing data.name")
        nics = data.get("nics") or []
        if not nics or not nics[0].get("ip"):
            raise InventoryError(f"VM {data.get('name')} has no first-NIC IP")
        groups = _groups(node)
        if not groups:
            raise InventoryError(f"VM {data['name']} has no AnsibleGroup tag")
        if "bastions" in groups:
            node_id = node.get("id")
            if node_id is not None:
                bastions.append(node_id)
    if require_bastion:
        _validate_bastion(bastions, topology)


def _validate_bastion(bastions: list[str], topology: dict) -> None:
    if len(bastions) != 1:
        raise InventoryError(
            f"exactly one VM must be tagged 'bastions' (found {len(bastions)})"
        )
    if bastions[0] not in _external_ip_vm_ids(topology):
        raise InventoryError("bastion VM has no external IP")
