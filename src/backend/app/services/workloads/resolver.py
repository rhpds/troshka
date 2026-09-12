"""Resolve a catalog item id into a ResolvedItem, credential-safe.

Git credentials and the vault key are read from the Secret Store and never leave
the backend. This is the public entry point Plan 2 (execution) builds on.
"""

from sqlalchemy.orm import Session

from app.services.workloads import agnosticv, repo_cache, secret_store


class ResolverError(Exception):
    pass


def resolve_catalog_item(db: Session, catalog_id: str) -> agnosticv.ResolvedItem:
    vault_password = secret_store.get_secret(db, "vault_key")
    if not vault_password:
        raise ResolverError("vault_key secret is not configured")

    git_conf = secret_store.get_json_secret(db, "agnosticv_git")
    if not git_conf or not git_conf.get("url"):
        raise ResolverError("agnosticv_git secret is not configured")

    repo_root = repo_cache.ensure_agnosticv(git_conf["url"])
    rel_path = agnosticv.resolve_path(repo_root, catalog_id)
    merged = agnosticv.merge_path(repo_root, rel_path)
    decrypted = agnosticv.decrypt_vault_strings(merged, vault_password)
    if not isinstance(decrypted, dict):
        raise ResolverError(
            f"expected dict from decrypt_vault_strings, got {type(decrypted).__name__}"
        )
    return agnosticv.to_resolved_item(decrypted)
