"""Resolve a catalog item id into a ResolvedItem, credential-safe.

Git credentials and the vault key are read from the Secret Store and never leave
the backend. This is the public entry point Plan 2 (execution) builds on.

For private AgnosticV repositories, the git token is embedded in the clone URL
(``x-access-token:TOKEN@hostname`` pattern) for the backend-local disk cache.
The backend is a trust zone; the token never leaves the backend process.
"""

from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.orm import Session

from app.services.workloads import agnosticv, repo_cache, secret_store


def _authenticated_url(url: str, token: str | None) -> str:
    """Embed a git token in an HTTPS clone URL for private repo access.

    Returns the plain URL if token is absent or the URL is not HTTPS (e.g. SSH).
    """
    if not token:
        return url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return url  # ssh or malformed — leave as-is
    netloc = f"x-access-token:{token}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class ResolverError(Exception):
    pass


def resolve_catalog_item(db: Session, catalog_id: str) -> agnosticv.ResolvedItem:
    vault_password = secret_store.get_secret(db, "vault_key")
    if not vault_password:
        raise ResolverError("vault_key secret is not configured")

    git_conf = secret_store.get_json_secret(db, "agnosticv_git")
    if not git_conf or not git_conf.get("url"):
        raise ResolverError("agnosticv_git secret is not configured")

    repo_root = repo_cache.ensure_agnosticv(
        _authenticated_url(git_conf["url"], git_conf.get("token"))
    )
    rel_path = agnosticv.resolve_path(repo_root, catalog_id)
    merged = agnosticv.merge_path(repo_root, rel_path)
    decrypted = agnosticv.decrypt_vault_strings(merged, vault_password)
    if not isinstance(decrypted, dict):
        raise ResolverError(
            f"expected dict from decrypt_vault_strings, got {type(decrypted).__name__}"
        )
    return agnosticv.to_resolved_item(decrypted)
