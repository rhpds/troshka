"""Proxy + cache for the list of currently supported OpenShift versions.

The frontend cluster editor offers a dropdown of OCP versions. Rather than
hardcode a list that goes stale, we read Red Hat's public Product Life Cycle
API and expose the non-EOL 4.x minor versions. Fetched server-side (avoids
browser CORS), cached in-memory across requests, with a static fallback so
the dropdown still works when the upstream API is unreachable.
"""

import logging
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ocp-versions", tags=["ocp-versions"])

_LIFECYCLE_URL = (
    "https://access.redhat.com/product-life-cycles/api/v1/products"
    "?name=Openshift%20Container%20Platform%204"
)
# Only clean "4.NN" names are offered (skips the "3", "4.6 EUS" style rows).
_VERSION_RE = re.compile(r"^4\.\d+$")
_CACHE_TTL_SECONDS = 6 * 60 * 60

# Served when the upstream lifecycle API is unreachable. Kept aligned with the
# supported (non-EOL) 4.x stream as of this writing.
_FALLBACK_VERSIONS: list[dict[str, str]] = [
    {"name": "4.20", "support": "Maintenance Support"},
    {"name": "4.19", "support": "Maintenance Support"},
    {"name": "4.18", "support": "Maintenance Support"},
]

_cache: dict[str, Any] = {"at": 0.0, "versions": []}


class OcpVersion(BaseModel):
    name: str
    support: str


class OcpVersionsResponse(BaseModel):
    versions: list[OcpVersion]
    source: str  # "live" | "cache" | "fallback"


def _version_key(name: str) -> tuple[int, int]:
    major, minor = name.split(".")
    return (int(major), int(minor))


def _parse_versions(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Extract non-EOL 4.x versions from the lifecycle API payload, newest first."""
    data = payload.get("data") or []
    if not data:
        return []
    out: list[dict[str, str]] = []
    for v in data[0].get("versions") or []:
        name = str(v.get("name", "")).strip()
        support = str(v.get("type", "")).strip()
        if _VERSION_RE.match(name) and support.lower() != "end of life":
            out.append({"name": name, "support": support})
    out.sort(key=lambda item: _version_key(item["name"]), reverse=True)
    return out


def _fetch_versions() -> list[dict[str, str]]:
    resp = httpx.get(_LIFECYCLE_URL, timeout=15.0)
    resp.raise_for_status()
    return _parse_versions(resp.json())


def _response(versions: list[dict[str, str]], source: str) -> OcpVersionsResponse:
    return OcpVersionsResponse(
        versions=[OcpVersion(**v) for v in versions], source=source
    )


@router.get(
    "", response_model=OcpVersionsResponse, dependencies=[Depends(get_current_user)]
)
def list_ocp_versions() -> OcpVersionsResponse:
    now = time.time()
    cached = _cache.get("versions") or []
    if cached and now - float(_cache.get("at", 0.0)) < _CACHE_TTL_SECONDS:
        return _response(cached, "cache")
    try:
        versions = _fetch_versions()
        if versions:
            _cache["versions"] = versions
            _cache["at"] = now
            return _response(versions, "live")
    except Exception as exc:  # noqa: BLE001 - upstream is best-effort
        logger.warning("OCP version lookup failed: %s", exc)
    # Serve stale cache if we have any, else the static fallback.
    if cached:
        return _response(cached, "cache")
    return _response(_FALLBACK_VERSIONS, "fallback")
