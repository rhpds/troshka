"""Resolve OCP Virt HTTP package-repo settings (provider creds override config)."""

from __future__ import annotations

from typing import Any


def resolve_pkg_repo(creds: dict[str, Any] | None = None) -> tuple[str, str, str]:
    """Return (url, username, password) for ocpvirt host dnf repos."""
    from app.core.config import config

    creds = creds or {}
    pkg = getattr(getattr(config, "ocpvirt", None), "pkg_repo", None)
    url = creds.get("pkg_repo_url") or (getattr(pkg, "url", "") if pkg else "")
    user = creds.get("pkg_repo_username") or (
        getattr(pkg, "username", "") if pkg else ""
    )
    password = creds.get("pkg_repo_password") or (
        getattr(pkg, "password", "") if pkg else ""
    )
    return url, user, password
