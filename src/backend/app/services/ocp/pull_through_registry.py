"""Pull-through registry reachability / auth checks before OCP install."""

from __future__ import annotations

import base64
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from app.core.encryption import decrypt
from app.models.user import User

logger = logging.getLogger("troshka.ocp.ptr")

_PROBE_TIMEOUT_S = 10
# Bounded token classes avoid catastrophic backtracking on malformed auth headers.
_BEARER_PARAM_RE = re.compile(r'([A-Za-z0-9_]+)="([^"]*)"')


class PullThroughRegistryError(Exception):
    """Registry unreachable or credentials rejected."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _registry_base_url(url: str) -> str:
    host = (url or "").strip().rstrip("/")
    if not host:
        raise PullThroughRegistryError(
            "Pull-through registry URL is not configured. "
            "Update credentials in Settings → OCP Pull Secret."
        )
    if "://" not in host:
        host = f"https://{host}"
    return host


def _basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


def _parse_bearer_challenge(www_authenticate: str | None) -> dict[str, str]:
    if not www_authenticate or not www_authenticate.lower().startswith("bearer"):
        return {}
    return {k: v for k, v in _BEARER_PARAM_RE.findall(www_authenticate)}


def _urlopen(req: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(req, timeout=timeout)


def _fetch_bearer_token(
    challenge: dict[str, str],
    username: str,
    password: str,
    *,
    timeout: float,
) -> str:
    realm = challenge.get("realm")
    if not realm:
        raise PullThroughRegistryError(
            "Pull-through registry returned a Bearer challenge without a realm."
        )
    params = {}
    if challenge.get("service"):
        params["service"] = challenge["service"]
    if challenge.get("scope"):
        params["scope"] = challenge["scope"]
    auth_url = realm
    if params:
        auth_url = f"{realm}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        auth_url,
        headers={"Authorization": _basic_auth_header(username, password)},
        method="GET",
    )
    try:
        with _urlopen(req, timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise PullThroughRegistryError(
            _auth_failure_message(_registry_base_url(realm), e.code, e.reason)
        ) from e
    try:
        data = json.loads(body.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PullThroughRegistryError(
            "Pull-through registry auth endpoint returned invalid JSON."
        ) from e
    token = data.get("token") or data.get("access_token")
    if not token:
        raise PullThroughRegistryError(
            "Pull-through registry auth endpoint did not return a token."
        )
    return token


def probe_pull_through_registry(
    url: str,
    username: str,
    password: str,
    *,
    timeout: float = _PROBE_TIMEOUT_S,
) -> None:
    """Verify registry URL + credentials (Basic or Bearer challenge).

    Raises PullThroughRegistryError on failure.
    """
    base = _registry_base_url(url)
    probe_url = f"{base}/v2/"
    basic = _basic_auth_header(username, password)
    req = urllib.request.Request(
        probe_url,
        headers={"Authorization": basic},
        method="GET",
    )
    try:
        with _urlopen(req, timeout) as resp:
            if getattr(resp, "status", 200) >= 400:
                raise PullThroughRegistryError(
                    _auth_failure_message(base, getattr(resp, "status", 0), "error")
                )
            return
    except PullThroughRegistryError:
        raise
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise PullThroughRegistryError(
                _auth_failure_message(base, e.code, e.reason)
            ) from e
        challenge = _parse_bearer_challenge(
            e.headers.get("WWW-Authenticate") if e.headers else None
        )
        if not challenge.get("realm"):
            raise PullThroughRegistryError(
                _auth_failure_message(base, e.code, e.reason)
            ) from e
        token = _fetch_bearer_token(challenge, username, password, timeout=timeout)
        # Confirm the token works against /v2/.
        bearer_req = urllib.request.Request(
            probe_url,
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with _urlopen(bearer_req, timeout) as resp:
                if getattr(resp, "status", 200) >= 400:
                    raise PullThroughRegistryError(
                        _auth_failure_message(base, getattr(resp, "status", 0), "error")
                    )
        except urllib.error.HTTPError as be:
            raise PullThroughRegistryError(
                _auth_failure_message(base, be.code, be.reason)
            ) from be
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e) or e
        raise PullThroughRegistryError(
            f"Pull-through registry unreachable ({base}): {reason}. "
            "Update credentials in Settings → OCP Pull Secret."
        ) from e
    except TimeoutError as e:
        raise PullThroughRegistryError(
            f"Pull-through registry unreachable ({base}): timed out. "
            "Update credentials in Settings → OCP Pull Secret."
        ) from e


def _auth_failure_message(base: str, code: int, reason) -> str:
    return (
        f"Pull-through registry unreachable or credentials invalid ({base}): "
        f"{code} {reason}. "
        "Update credentials in Settings → OCP Pull Secret."
    )


def _clusters_needing_ptr(topology: dict, cluster_key: str | None) -> list[dict]:
    from app.services.ocp.agent_template import _cluster_use_pull_through_registry

    clusters = (topology or {}).get("clusters") or []
    if cluster_key is not None:
        return [
            c
            for c in clusters
            if c.get("id") == cluster_key and _cluster_use_pull_through_registry(c)
        ]
    return [
        c
        for c in clusters
        if c.get("installOnDeploy", True) is not False
        and _cluster_use_pull_through_registry(c)
    ]


def check_pull_through_registry_for_project(
    db,
    project,
    topology: dict | None,
    *,
    cluster_key: str | None = None,
) -> str | None:
    """Return an error string when PTR is required but not reachable; else None."""
    owner = db.query(User).filter_by(id=project.owner_id).first()
    if not (owner and owner.pull_through_registry and owner.pull_through_registry_url):
        return None

    needing = _clusters_needing_ptr(topology or {}, cluster_key)
    if not needing:
        return None

    username = (owner.pull_through_registry_user or "").strip()
    enc_password = owner.pull_through_registry_password
    if not username or not enc_password:
        return (
            "Pull-through registry is enabled but username/password are missing. "
            "Update credentials in Settings → OCP Pull Secret."
        )

    try:
        password = decrypt(enc_password)
    except Exception:
        logger.exception("Failed to decrypt pull-through registry password")
        return (
            "Pull-through registry password could not be decrypted. "
            "Update credentials in Settings → OCP Pull Secret."
        )

    try:
        probe_pull_through_registry(owner.pull_through_registry_url, username, password)
    except PullThroughRegistryError as e:
        return e.message
    return None
