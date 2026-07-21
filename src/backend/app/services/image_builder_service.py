import json
import logging
import time
import urllib.parse

import urllib3

logger = logging.getLogger(__name__)

SSO_URL = (
    "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
)
API_BASE = "https://console.redhat.com/api/image-builder/v1"

HOST_PACKAGES = [
    "qemu-kvm",
    "libvirt",
    "virt-install",
    "dnsmasq",
    "nftables",
    "python3",
    "xorriso",
    "ncat",
    "sshpass",
    "nfs-utils",
    "cloud-init",
    "cloud-utils-growpart",
]

HOST_SERVICES_ENABLED = ["libvirtd", "nftables", "sshd"]

_http = urllib3.PoolManager(retries=urllib3.Retry(total=2, backoff_factor=1))

_build_progress: dict[str, dict] = {}


class ImageBuilderError(Exception):
    pass


def _exchange_token(offline_token: str) -> str:
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": "rhsm-api",
            "refresh_token": offline_token,
        }
    ).encode()
    resp = _http.request(
        "POST",
        SSO_URL,
        body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30.0,
    )
    if resp.status >= 400:
        raise ImageBuilderError(
            f"SSO token exchange failed (HTTP {resp.status}): {resp.data.decode()[:200]}"
        )
    data = json.loads(resp.data.decode())
    return data["access_token"]


def _api_request(method: str, path: str, access_token: str, body: dict | None = None):
    headers = {"Authorization": f"Bearer {access_token}"}
    encoded_body = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        encoded_body = json.dumps(body).encode()
    resp = _http.request(
        method,
        f"{API_BASE}{path}",
        body=encoded_body,
        headers=headers,
        timeout=30.0,
    )
    if resp.status >= 400:
        raise ImageBuilderError(
            f"Image Builder API error (HTTP {resp.status}): {resp.data.decode()[:500]}"
        )
    return json.loads(resp.data.decode())


def _start_compose(
    access_token: str,
    distribution: str,
    image_type: str,
    upload_options: dict,
) -> str:
    body = {
        "distribution": distribution,
        "image_requests": [
            {
                "architecture": "x86_64",
                "image_type": image_type,
                "upload_request": {
                    "type": image_type,
                    "options": upload_options,
                },
            }
        ],
        "customizations": {
            "packages": HOST_PACKAGES,
            "services": {"enabled": HOST_SERVICES_ENABLED},
        },
    }
    data = _api_request("POST", "/compose", access_token, body)
    return data["id"]


def _poll_compose(access_token: str, compose_id: str) -> dict:
    return _api_request("GET", f"/composes/{compose_id}", access_token)


def _extract_image_reference(
    compose_status: dict, provider_type: str, provider=None
) -> str:
    upload = compose_status["image_status"]["upload_status"]
    opts = upload.get("options", {})
    if provider_type == "gcp":
        project_id = opts.get("project_id", "red-hat-image-builder")
        image_name = opts["image_name"]
        return f"projects/{project_id}/global/images/{image_name}"
    elif provider_type == "azure":
        image_name = opts["image_name"]
        sub = (
            provider.azure_subscription_id
            if provider
            else opts.get("subscription_id", "")
        )
        rg = (
            provider.azure_resource_group
            if provider
            else opts.get("resource_group", "")
        )
        return (
            f"/subscriptions/{sub}/resourceGroups/{rg}"
            f"/providers/Microsoft.Compute/images/{image_name}"
        )
    raise ImageBuilderError(f"Unknown provider type: {provider_type}")


def _build_upload_options(provider, rhel_version: str = "rhel-10") -> dict:
    if provider.type == "gcp":
        creds = provider.get_credentials()
        sa_json = creds.get("service_account_json", {})
        if isinstance(sa_json, str):
            sa_json = json.loads(sa_json)
        email = sa_json.get("client_email", "")
        return {"share_with_accounts": [f"serviceAccount:{email}"]}
    elif provider.type == "azure":
        creds = provider.get_credentials()
        return {
            "tenant_id": creds.get("tenant_id", provider.azure_subscription_id),
            "subscription_id": provider.azure_subscription_id,
            "resource_group": provider.azure_resource_group,
            "image_name": f"troshka-host-{rhel_version}-{int(time.time())}",
        }
    raise ImageBuilderError(
        f"Unsupported provider type for image build: {provider.type}"
    )


def get_build_status(provider_id: str) -> dict:
    return _build_progress.get(
        provider_id, {"status": "idle", "message": "", "image": None}
    )


def clear_build_status(provider_id: str):
    _build_progress.pop(provider_id, None)


def build_host_image(provider_id: str, user_id: str, rhel_version: str = "rhel-10"):
    from app.core.database import SessionLocal
    from app.core.encryption import decrypt
    from app.models.provider import Provider
    from app.models.user import User

    start_time = time.time()
    _build_progress[provider_id] = {
        "status": "authenticating",
        "message": "Exchanging Red Hat token...",
        "image": None,
        "compose_id": None,
        "elapsed_seconds": 0,
    }

    db = SessionLocal()
    try:
        provider = db.get(Provider, provider_id)
        user = db.get(User, user_id)
        if not provider or not user:
            _build_progress[provider_id] = {
                "status": "error",
                "message": "Provider or user not found",
            }
            return

        if not user.rh_offline_token:
            _build_progress[provider_id] = {
                "status": "error",
                "message": "No Red Hat offline token configured — add one in Settings",
            }
            return

        offline_token = decrypt(user.rh_offline_token)
        if not offline_token:
            _build_progress[provider_id] = {
                "status": "error",
                "message": "Failed to decrypt offline token",
            }
            return

        access_token = _exchange_token(offline_token)

        upload_options = _build_upload_options(provider, rhel_version)
        image_type = "gcp" if provider.type == "gcp" else "azure"

        _build_progress[provider_id] = {
            "status": "building",
            "message": f"Compose submitted — building {rhel_version} image...",
            "image": None,
            "compose_id": None,
            "elapsed_seconds": int(time.time() - start_time),
        }

        compose_id = _start_compose(
            access_token, rhel_version, image_type, upload_options
        )
        _build_progress[provider_id]["compose_id"] = compose_id
        logger.info(
            "Image Builder compose started: %s for provider %s",
            compose_id,
            provider.name,
        )

        while True:
            time.sleep(30)
            elapsed = int(time.time() - start_time)

            try:
                status = _poll_compose(access_token, compose_id)
            except ImageBuilderError as e:
                if "401" in str(e) or "403" in str(e):
                    access_token = _exchange_token(offline_token)
                    status = _poll_compose(access_token, compose_id)
                else:
                    raise

            image_status_obj = status.get("image_status", {})
            image_status = image_status_obj.get("status", "unknown")
            progress = image_status_obj.get("progress", {})
            done = progress.get("done", 0)
            total = progress.get("total", 0)
            minutes = elapsed // 60
            if total:
                msg = f"Step {done}/{total} — {image_status} ({minutes}m elapsed)"
            else:
                msg = f"{image_status} ({minutes}m elapsed)"
            _build_progress[provider_id].update(
                {
                    "message": msg,
                    "elapsed_seconds": elapsed,
                }
            )

            if image_status == "success":
                image_ref = _extract_image_reference(status, provider.type, provider)
                provider.default_image = image_ref
                db.commit()
                _build_progress[provider_id] = {
                    "status": "success",
                    "message": f"Image ready: {image_ref}",
                    "image": image_ref,
                    "compose_id": compose_id,
                    "elapsed_seconds": elapsed,
                }
                logger.info("Image build complete for %s: %s", provider.name, image_ref)
                return

            if image_status == "failure":
                error_info = status.get("image_status", {}).get("error", {})
                reason = error_info.get("reason", "Unknown error")
                details = error_info.get("details", "")
                msg = f"Image build failed: {reason}"
                if details:
                    msg += f" — {details[:200]}"
                _build_progress[provider_id] = {
                    "status": "error",
                    "message": msg,
                    "compose_id": compose_id,
                    "elapsed_seconds": elapsed,
                }
                logger.error("Image build failed for %s: %s", provider.name, msg)
                return

            if elapsed > 3600:
                _build_progress[provider_id] = {
                    "status": "error",
                    "message": "Image build timed out after 1 hour",
                    "compose_id": compose_id,
                    "elapsed_seconds": elapsed,
                }
                return

    except Exception as e:
        logger.exception("Image build error for provider %s", provider_id)
        _build_progress[provider_id] = {
            "status": "error",
            "message": f"Build error: {e}",
            "elapsed_seconds": int(time.time() - start_time),
        }
    finally:
        db.close()
