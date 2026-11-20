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
# Only start a parameter at a delimiter, avoiding retries within malformed tokens.
_BEARER_PARAM_RE = re.compile(r'(?:^|,)\s*(\w+)="([^"]*)"')

# Red Hat pull secrets authorize quay.io/openshift-release-dev pulls used by
# openshift-install agent ISO generation.
OCP_PULL_SECRET_DOWNLOAD_URL = "https://console.redhat.com/openshift/downloads"
_OCP_REGISTRY_HOST = "quay.io"
_SETTINGS_PULL_SECRET = "Settings → OCP Pull Secret"


class PullThroughRegistryError(Exception):
    """Registry unreachable or credentials rejected."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class OcpPullSecretError(Exception):
    """Stored OCP pull secret missing, malformed, or rejected by the registry."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _registry_base_url(url: str) -> str:
    host = (url or "").strip().rstrip("/")
    if not host:
        raise PullThroughRegistryError(
            "Pull-through registry URL is not configured. "
            f"Update credentials in {_SETTINGS_PULL_SECRET}."
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
    return dict(_BEARER_PARAM_RE.findall(www_authenticate[6:]))


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


def _assert_probe_ok(resp, base: str) -> None:
    if getattr(resp, "status", 200) >= 400:
        raise PullThroughRegistryError(
            _auth_failure_message(base, getattr(resp, "status", 0), "error")
        )


def _probe_with_bearer(
    probe_url: str,
    challenge: dict[str, str],
    username: str,
    password: str,
    *,
    base: str,
    timeout: float,
) -> None:
    token = _fetch_bearer_token(challenge, username, password, timeout=timeout)
    bearer_req = urllib.request.Request(
        probe_url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with _urlopen(bearer_req, timeout) as resp:
            _assert_probe_ok(resp, base)
    except urllib.error.HTTPError as be:
        raise PullThroughRegistryError(
            _auth_failure_message(base, be.code, be.reason)
        ) from be


def _handle_probe_http_error(
    e: urllib.error.HTTPError,
    *,
    probe_url: str,
    username: str,
    password: str,
    base: str,
    timeout: float,
) -> None:
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
    _probe_with_bearer(
        probe_url, challenge, username, password, base=base, timeout=timeout
    )


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
            _assert_probe_ok(resp, base)
            return
    except PullThroughRegistryError:
        raise
    except urllib.error.HTTPError as e:
        _handle_probe_http_error(
            e,
            probe_url=probe_url,
            username=username,
            password=password,
            base=base,
            timeout=timeout,
        )
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e) or e
        raise PullThroughRegistryError(
            f"Pull-through registry unreachable ({base}): {reason}. "
            f"Update credentials in {_SETTINGS_PULL_SECRET}."
        ) from e
    except TimeoutError as e:
        raise PullThroughRegistryError(
            f"Pull-through registry unreachable ({base}): timed out. "
            f"Update credentials in {_SETTINGS_PULL_SECRET}."
        ) from e


def _auth_failure_message(base: str, code: int, reason) -> str:
    return (
        f"Pull-through registry unreachable or credentials invalid ({base}): "
        f"{code} {reason}. "
        f"Update credentials in {_SETTINGS_PULL_SECRET}."
    )


def _pull_secret_help(detail: str) -> str:
    return (
        f"{detail} Download a new pull secret from {OCP_PULL_SECRET_DOWNLOAD_URL} "
        f"and update it in {_SETTINGS_PULL_SECRET}."
    )


def _credentials_from_auth_entry(entry: dict) -> tuple[str, str] | None:
    if not isinstance(entry, dict):
        return None
    username = (entry.get("username") or "").strip()
    password = entry.get("password") or ""
    if username and password:
        return username, password
    auth = entry.get("auth")
    if not auth or not isinstance(auth, str):
        return None
    try:
        decoded = base64.b64decode(auth).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    user, password = decoded.split(":", 1)
    if not user or not password:
        return None
    return user, password


def _quay_credentials_from_pull_secret(pull_secret_json: str) -> tuple[str, str]:
    try:
        data = json.loads(pull_secret_json or "")
    except (TypeError, json.JSONDecodeError) as e:
        raise OcpPullSecretError(
            _pull_secret_help("OCP pull secret is not valid JSON.")
        ) from e
    auths = data.get("auths") if isinstance(data, dict) else None
    if not isinstance(auths, dict) or not auths:
        raise OcpPullSecretError(
            _pull_secret_help("OCP pull secret has no registry credentials.")
        )
    for host in (_OCP_REGISTRY_HOST, f"https://{_OCP_REGISTRY_HOST}"):
        creds = _credentials_from_auth_entry(auths.get(host) or {})
        if creds:
            return creds
    raise OcpPullSecretError(
        _pull_secret_help(
            f"OCP pull secret is missing credentials for {_OCP_REGISTRY_HOST}."
        )
    )


def probe_ocp_pull_secret(
    pull_secret_json: str,
    *,
    timeout: float = _PROBE_TIMEOUT_S,
) -> None:
    """Verify the stored pull secret can authenticate to quay.io.

    Raises OcpPullSecretError when the secret is malformed or rejected.
    """
    username, password = _quay_credentials_from_pull_secret(pull_secret_json)
    try:
        probe_pull_through_registry(
            _OCP_REGISTRY_HOST, username, password, timeout=timeout
        )
    except PullThroughRegistryError as e:
        # Rephrase PTR-oriented wording for the Red Hat pull-secret path.
        raise OcpPullSecretError(
            _pull_secret_help(
                "OCP pull secret is invalid or expired "
                f"(registry authentication failed for {_OCP_REGISTRY_HOST})."
            )
        ) from e


def _probe_stored_ocp_pull_secret(owner) -> str | None:
    try:
        secret = decrypt(owner.ocp_pull_secret)
    except Exception:
        logger.exception("Failed to decrypt OCP pull secret")
        return _pull_secret_help("OCP pull secret could not be decrypted.")
    try:
        probe_ocp_pull_secret(secret)
    except OcpPullSecretError as e:
        return e.message
    return None


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


def _requires_ocp_pull_secret(topology: dict, cluster_key: str | None) -> bool:
    """Fresh OCP installs need credentials; OKD and captured disks do not."""
    from app.services.deploy_service import _cluster_uses_recert
    from app.services.ocp.client_mirror import is_okd_scos

    for cluster in topology.get("clusters") or []:
        if cluster_key is not None:
            if cluster.get("id") != cluster_key:
                continue
        elif cluster.get("installOnDeploy", True) is False:
            continue
        if is_okd_scos(cluster.get("ocpDistribution")):
            continue
        if not _cluster_uses_recert(topology, cluster):
            return True
    return False


def check_pull_through_registry_for_project(
    db,
    project,
    topology: dict | None,
    *,
    cluster_key: str | None = None,
) -> str | None:
    """Check install credentials and PTR before placement or job submission."""
    owner = db.query(User).filter_by(id=project.owner_id).first()
    needs_secret = _requires_ocp_pull_secret(topology or {}, cluster_key)
    # Saving PTR credentials also stores the generated registry pull secret.
    # A mirror URL alone cannot satisfy openshift-install's pullSecret input.
    if needs_secret and not (owner and owner.ocp_pull_secret):
        return (
            "OpenShift installation requires a pull secret for the project owner. "
            f"Download one from {OCP_PULL_SECRET_DOWNLOAD_URL} or save "
            f"pull-through registry credentials in {_SETTINGS_PULL_SECRET} "
            "before deploying or retrying installation."
        )

    ptr_enabled = bool(
        owner and owner.pull_through_registry and owner.pull_through_registry_url
    )
    # Direct Red Hat pull secrets must still authenticate to quay.io before we
    # spend time creating VMs / building the agent ISO. PTR installs probe the
    # mirror credentials below instead (the stored secret is PTR-only).
    if needs_secret and owner and owner.ocp_pull_secret and not ptr_enabled:
        secret_err = _probe_stored_ocp_pull_secret(owner)
        if secret_err:
            return secret_err

    if not ptr_enabled:
        return None

    needing = _clusters_needing_ptr(topology or {}, cluster_key)
    if not needing:
        return None

    username = (owner.pull_through_registry_user or "").strip()
    enc_password = owner.pull_through_registry_password
    if not username or not enc_password:
        return (
            "Pull-through registry is enabled but username/password are missing. "
            f"Update credentials in {_SETTINGS_PULL_SECRET}."
        )

    try:
        password = decrypt(enc_password)
    except Exception:
        logger.exception("Failed to decrypt pull-through registry password")
        return (
            "Pull-through registry password could not be decrypted. "
            f"Update credentials in {_SETTINGS_PULL_SECRET}."
        )

    try:
        probe_pull_through_registry(owner.pull_through_registry_url, username, password)
    except PullThroughRegistryError as e:
        return e.message
    return None
