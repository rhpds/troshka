"""OCP/OKD client mirror URL routing.

Troshka downloads ``openshift-install`` and ``oc`` from either:

- **OCP** (default): mirror.openshift.com
  - 4.x GA: ``openshift-v4/x86_64/clients/ocp/stable-4.NN/...``
  - 5.x dev-preview: ``openshift-v4/clients/ocp-dev-preview/latest/...``
  - 5.x GA (future): ``openshift-v5/x86_64/clients/ocp/stable-5.N/...``
- **OKD/SCOS**: GitHub ``okd-project/okd`` release assets
  (``openshift-install-linux-{tag}.tar.gz``), no Red Hat pull secret required.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from functools import lru_cache

_MIRROR_ROOT = "https://mirror.openshift.com/pub"
_OKD_GITHUB_API = "https://api.github.com/repos/okd-project/okd/releases"
_OKD_DOWNLOAD = "https://github.com/okd-project/okd/releases/download"
_PREVIEW_VERSION = "5.0"
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)$")
_OKD_TAG_RE = re.compile(r"^(\d+)\.(\d+)\.\d+-okd-scos(?:\.ec)?\.(\d+)$")

DIST_OCP = "ocp"
DIST_OKD_SCOS = "okd-scos"

# Offline / API-failure fallbacks (keep roughly current with okd-project/okd).
_OKD_TAG_FALLBACK = {
    "4.22": "4.22.0-okd-scos.9",
    "5.0": "5.0.0-okd-scos.0",
}

logger = logging.getLogger(__name__)


def parse_ocp_version(ocp_version: str) -> tuple[int, int]:
    """Parse ``major.minor`` from a cluster ``ocpVersion`` string."""
    match = _VERSION_RE.match(str(ocp_version or "").strip())
    if not match:
        return (4, 22)
    return int(match.group(1)), int(match.group(2))


def normalize_distribution(distribution: str | None) -> str:
    """Map template/cluster distribution aliases to a canonical value."""
    value = str(distribution or DIST_OCP).strip().lower().replace("_", "-")
    if value in ("okd", "okd-scos", "scos"):
        return DIST_OKD_SCOS
    return DIST_OCP


def is_okd_scos(distribution: str | None) -> bool:
    """True when the cluster uses OKD Stream CoreOS builds."""
    return normalize_distribution(distribution) == DIST_OKD_SCOS


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


def _okd_tag_sort_key(tag: str) -> tuple[int, int]:
    """Prefer stable (non-ec) tags, then higher scos build number."""
    match = _OKD_TAG_RE.match(tag)
    if not match:
        return (0, 0)
    is_stable = 1 if ".ec." not in tag else 0
    return (is_stable, int(match.group(3)))


def _pick_okd_scos_tag(tags: list[str], major: int, minor: int) -> str | None:
    """Pick the best OKD/SCOS tag for ``major.minor`` from a tag list."""
    prefix = f"{major}.{minor}."
    candidates = [
        t
        for t in tags
        if t.startswith(prefix) and "-okd-scos." in t and _OKD_TAG_RE.match(t)
    ]
    if not candidates:
        return None
    return max(candidates, key=_okd_tag_sort_key)


def _fetch_okd_release_tags(pages: int = 3) -> list[str]:
    """Fetch recent OKD release tags from GitHub (network I/O)."""
    tags: list[str] = []
    for page in range(1, pages + 1):
        url = f"{_OKD_GITHUB_API}?per_page=30&page={page}"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "troshka-okd-mirror",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
        if not payload:
            break
        for release in payload:
            tag = release.get("tag_name")
            if tag:
                tags.append(str(tag))
    return tags


@lru_cache(maxsize=32)
def resolve_okd_scos_tag(ocp_version: str) -> str:
    """Resolve ``major.minor`` to a concrete OKD/SCOS GitHub release tag.

    Tries GitHub releases (cached), then a static fallback map, then a
    constructed ``{major}.{minor}.0-okd-scos.0`` guess.
    """
    major, minor = parse_ocp_version(ocp_version)
    key = f"{major}.{minor}"
    try:
        tags = _fetch_okd_release_tags()
        picked = _pick_okd_scos_tag(tags, major, minor)
        if picked:
            return picked
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.debug("OKD tag resolve via GitHub failed for %s: %s", key, exc)
    if key in _OKD_TAG_FALLBACK:
        return _OKD_TAG_FALLBACK[key]
    return f"{major}.{minor}.0-okd-scos.0"


def okd_scos_tarball_url(ocp_version: str, tarball: str) -> str:
    """GitHub download URL for an OKD/SCOS client/installer tarball."""
    tag = resolve_okd_scos_tag(ocp_version)
    # OCP mirror uses unversioned names; OKD assets embed the tag.
    stem = str(tarball or "").removesuffix(".tar.gz")
    asset = f"{stem}-{tag}.tar.gz"
    return f"{_OKD_DOWNLOAD}/{tag}/{asset}"


def installer_tarball_url(
    ocp_version: str,
    tarball: str,
    distribution: str | None = None,
) -> str:
    """Full HTTPS URL for an ``openshift-install`` or ``oc`` client tarball."""
    if is_okd_scos(distribution):
        return okd_scos_tarball_url(ocp_version, tarball)
    base, segment = client_mirror_paths(ocp_version)
    return f"{base}/{segment}/{tarball}"


def preview_version_entries() -> list[dict[str, str]]:
    """Dropdown entries for dev-preview OCP versions (when enabled)."""
    if not preview_versions_enabled():
        return []
    return [{"name": _PREVIEW_VERSION, "support": "Dev Preview"}]


def default_pull_secret_for_distribution(distribution: str | None) -> str:
    """Pull secret body when the user has none configured.

    OKD/SCOS accepts an empty JSON object; OCP requires a real Red Hat secret.
    """
    if is_okd_scos(distribution):
        return "{}"
    return ""
