# src/backend/app/services/workloads/agnosticv.py
"""Drive the canonical `agnosticv` binary for list + merge, and resolve the
merged output into the fields the workloads subsystem needs.

The AgnosticV overlay/#include/.agnosticv.yaml merge is NOT reimplemented — the
`agnosticv` binary is the single source of truth. Only vault decrypt and the
path->catalog-id transform live here.
"""

import json
import subprocess
from dataclasses import dataclass

from app.services.workloads.vault import decrypt_vault, is_vault

_AGNOSTICV = "agnosticv"


def path_to_catalog_id(
    component_path: str, default_stage: str = "prod"
) -> tuple[str, str]:
    """Port of ci-inspector `_resolve_binder_crd_name`.

    ``agd_v2/ocp-cluster-cnv-pools/event`` -> (``agd-v2.ocp-cluster-cnv-pools``, ``event``).
    Strips a trailing ``.yaml``/``.yml`` stage extension first.
    """
    cleaned = component_path
    for ext in (".yaml", ".yml"):
        if cleaned.endswith(ext):
            cleaned = cleaned[: -len(ext)]
            break
    parts = cleaned.replace("_", "-").lower().split("/")
    if len(parts) >= 2:
        stage = parts[-1] if len(parts) >= 3 else default_stage
        ci_name = ".".join(parts[:-1]) if len(parts) >= 3 else ".".join(parts)
        return ci_name, stage
    return cleaned.lower(), default_stage


def list_item_paths(repo_root: str) -> list[str]:
    proc = subprocess.run(
        [_AGNOSTICV, "--list", "--dir", repo_root, "--git=false", "--output=json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def build_catalog_index(repo_root: str) -> dict[str, str]:
    index: dict[str, str] = {}
    for path in list_item_paths(repo_root):
        ci_name, stage = path_to_catalog_id(path)
        index[f"{ci_name}.{stage}"] = path
    return index


def resolve_path(repo_root: str, catalog_id: str) -> str:
    index = build_catalog_index(repo_root)
    if catalog_id not in index:
        raise KeyError(f"catalog item not found: {catalog_id}")
    return index[catalog_id]


def merge_path(repo_root: str, rel_path: str) -> dict:
    proc = subprocess.run(
        [_AGNOSTICV, "--git=false", "--merge", rel_path, "--output=json"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def decrypt_vault_strings(data, vault_password: str):
    if isinstance(data, str):
        return decrypt_vault(data, vault_password) if is_vault(data) else data
    if isinstance(data, dict):
        return {k: decrypt_vault_strings(v, vault_password) for k, v in data.items()}
    if isinstance(data, list):
        return [decrypt_vault_strings(v, vault_password) for v in data]
    return data


@dataclass
class ResolvedItem:
    extra_vars: dict
    ee_image: str | None
    scm_ref: str | None
    requirements_content: dict | None


def to_resolved_item(merged: dict) -> ResolvedItem:
    deployer = (merged.get("__meta__") or {}).get("deployer") or {}
    ee_image = (deployer.get("execution_environment") or {}).get("image")
    return ResolvedItem(
        extra_vars=merged,
        ee_image=ee_image,
        scm_ref=deployer.get("scm_ref"),
        requirements_content=merged.get("requirements_content"),
    )
