"""OCP/OKD client mirror URL routing.

Troshka downloads ``openshift-install`` and ``oc`` from either:

- **OCP** (default): mirror.openshift.com
  - 4.x GA: ``openshift-v4/x86_64/clients/ocp/stable-4.NN/...``
  - Exact GA releases: ``openshift-v4/x86_64/clients/ocp/<release>/...``
  - 5.x dev-preview: ``openshift-v4/clients/ocp-dev-preview/<release>/...``
- **OKD/SCOS**: GitHub ``okd-project/okd`` release assets
  (``openshift-install-linux-{tag}.tar.gz``), no Red Hat pull secret required.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from functools import lru_cache
from html.parser import HTMLParser

_MIRROR_ROOT = "https://mirror.openshift.com/pub"
_OKD_GITHUB_API = "https://api.github.com/repos/okd-project/okd/releases"
_OKD_DOWNLOAD = "https://github.com/okd-project/okd/releases/download"
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.\d+(?:-(?:ec|rc)\.\d+)?)?$")
_RELEASE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-(ec|rc)\.(\d+))?$")
_GA_MIRROR = f"{_MIRROR_ROOT}/openshift-v4/x86_64/clients/ocp"
_PREVIEW_MIRROR = f"{_MIRROR_ROOT}/openshift-v4/clients/ocp-dev-preview"
_mirror_cache: dict[bool, tuple[float, list[str]]] = {}
_CATALOG_TTL = 6 * 60 * 60
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


def release_sort_key(version: str) -> tuple[int, int, int, int, int]:
    """Numeric ordering, with GA newer than RC and engineering candidates."""
    match = _RELEASE_RE.fullmatch(version)
    if not match:
        return (*parse_ocp_version(version), 0, 2, 0)
    major, minor, patch, stage, build = match.groups()
    return (
        int(major),
        int(minor),
        int(patch),
        {"ec": 0, "rc": 1, None: 2}[stage],
        int(build or 0),
    )


class _ReleaseLinks(HTMLParser):
    """Accept only versioned child directories, never floating or external URLs."""

    def __init__(self):
        super().__init__()
        self.versions: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        if href.endswith("/") and _RELEASE_RE.fullmatch(href[:-1]):
            self.versions.add(href[:-1])


def available_mirror_versions(preview: bool) -> list[str]:
    """Discover named releases; retain known releases during mirror outages."""
    now = time.monotonic()
    at, cached = _mirror_cache.get(preview, (0.0, []))
    if cached and now - at < _CATALOG_TTL:
        return list(cached)
    base = _PREVIEW_MIRROR if preview else _GA_MIRROR
    try:
        with urllib.request.urlopen(base + "/", timeout=15) as response:
            parser = _ReleaseLinks()
            parser.feed(response.read().decode("utf-8"))
        versions = [
            v
            for v in parser.versions
            if (parse_ocp_version(v)[0] >= 5 if preview else "-" not in v)
        ]
        if not versions:
            raise ValueError("Mirror returned no versioned release directories")
        versions.sort(key=release_sort_key, reverse=True)
        _mirror_cache[preview] = (now, versions)
        return list(versions)
    except (OSError, ValueError) as exc:
        logger.warning("OCP release catalog lookup failed for %s: %s", base, exc)
        return list(cached)


def resolve_preview_version(version: str) -> str:
    """Legacy minor selections stay within their minor; never use global latest."""
    if _RELEASE_RE.fullmatch(version):
        return version
    candidates = [
        v for v in available_mirror_versions(True) if v.startswith(version + ".")
    ]
    if not candidates:
        raise ValueError(
            f"No available OCP preview release for {version}; select a concrete release"
        )
    return max(candidates, key=release_sort_key)


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
    if _RELEASE_RE.fullmatch(ocp_version):
        return "-" in ocp_version
    major, _minor = parse_ocp_version(ocp_version)
    return major >= 5


def client_mirror_paths(ocp_version: str) -> tuple[str, str]:
    """Return ``(mirror_base, version_segment)`` for client tarballs."""
    version = str(ocp_version or "").strip()
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(f"Invalid OCP version: {version!r}")
    major, minor = parse_ocp_version(version)
    if _RELEASE_RE.fullmatch(version):
        return (_PREVIEW_MIRROR if "-" in version else _GA_MIRROR), version
    if major >= 5:
        stable = [
            v
            for v in available_mirror_versions(False)
            if v.startswith(version + ".") and "-" not in v
        ]
        if stable:
            return _GA_MIRROR, max(stable, key=release_sort_key)
        return _PREVIEW_MIRROR, resolve_preview_version(version)
    return _GA_MIRROR, f"stable-{major}.{minor}"


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
    except (OSError, json.JSONDecodeError) as exc:
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


def installer_version_check_cmd(
    ocp_version: str, installer: str = "openshift-install"
) -> str:
    """Stop installation if a cached or downloaded binary differs from the selection."""
    version = str(ocp_version).strip()
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(f"Invalid OCP version: {version!r}")
    pattern = version if _RELEASE_RE.fullmatch(version) else version + ".*"
    return (
        f"installed_version=$({installer} version | awk 'NR == 1 {{print $2}}')\n"
        'case "$installed_version" in\n'
        f"  {pattern}) ;;\n"
        f'  *) echo "ERROR: Requested OCP {version}, installer is $installed_version" >&2; exit 1 ;;\n'
        "esac\n"
    )


def preview_version_entries() -> list[dict[str, str]]:
    """Dropdown entries for dev-preview OCP versions (when enabled)."""
    if not preview_versions_enabled():
        return []
    return [
        {"name": version, "support": "Dev Preview"}
        for version in available_mirror_versions(True)
    ]


def default_pull_secret_for_distribution(distribution: str | None) -> str:
    """Pull secret body when the user has none configured.

    OKD/SCOS accepts an empty JSON object; OCP requires a real Red Hat secret.
    """
    if is_okd_scos(distribution):
        return "{}"
    return ""
