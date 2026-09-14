"""OCP client mirror URL routing (4.x stable, 5.x dev-preview, future 5.x GA).

Troshka downloads ``openshift-install`` and ``oc`` from mirror.openshift.com.
The path layout depends on the major version and release type:

- 4.x GA: ``openshift-v4/x86_64/clients/ocp/stable-4.NN/...``
- 5.x dev-preview (Option A): ``openshift-v4/clients/ocp-dev-preview/latest/...``
- 5.x GA (future): ``openshift-v5/x86_64/clients/ocp/stable-5.N/...``
"""

from __future__ import annotations

import re

_MIRROR_ROOT = "https://mirror.openshift.com/pub"
_PREVIEW_VERSION = "5.0"
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)$")


def parse_ocp_version(ocp_version: str) -> tuple[int, int]:
    """Parse ``major.minor`` from a cluster ``ocpVersion`` string."""
    match = _VERSION_RE.match(str(ocp_version or "").strip())
    if not match:
        return (4, 22)
    return int(match.group(1)), int(match.group(2))


def preview_versions_enabled() -> bool:
    """Whether the UI/API should offer OCP 5 dev-preview versions."""
    try:
        from app.core.config import config

        ocp_cfg = getattr(config, "ocp", None)
        if ocp_cfg is not None:
            explicit = getattr(ocp_cfg, "preview_versions_enabled", None)
            if explicit is not None:
                return bool(explicit)
        auth = getattr(config, "auth", None)
        if auth is not None and not bool(getattr(auth, "oauth_enabled", True)):
            return True
    except Exception:
        pass
    return False


def uses_dev_preview_mirror(ocp_version: str) -> bool:
    """True when client downloads should use the ocp-dev-preview tree."""
    major, _minor = parse_ocp_version(ocp_version)
    return major >= 5


def client_mirror_paths(ocp_version: str) -> tuple[str, str]:
    """Return ``(mirror_base, version_segment)`` for client tarballs."""
    major, minor = parse_ocp_version(ocp_version)
    if major >= 5:
        if uses_dev_preview_mirror(ocp_version):
            return (
                f"{_MIRROR_ROOT}/openshift-v4/clients/ocp-dev-preview",
                "latest",
            )
        return (
            f"{_MIRROR_ROOT}/openshift-v5/x86_64/clients/ocp",
            f"stable-{major}.{minor}",
        )
    return (
        f"{_MIRROR_ROOT}/openshift-v4/x86_64/clients/ocp",
        f"stable-{major}.{minor}",
    )


def installer_tarball_url(ocp_version: str, tarball: str) -> str:
    """Full HTTPS URL for an ``openshift-install`` or ``oc`` client tarball."""
    base, segment = client_mirror_paths(ocp_version)
    return f"{base}/{segment}/{tarball}"


def preview_version_entries() -> list[dict[str, str]]:
    """Dropdown entries for dev-preview OCP versions (when enabled)."""
    if not preview_versions_enabled():
        return []
    return [{"name": _PREVIEW_VERSION, "support": "Dev Preview"}]
