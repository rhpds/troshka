# Image Builder Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build custom RHEL host images via Red Hat Image Builder API so GCP/Azure hosts come pre-loaded with all required packages (qemu-kvm, libvirt, etc.) — no RHSM registration or PAYG premium needed at boot time.

**Architecture:** User saves a Red Hat offline token (encrypted, on User model — same as OCP pull secret). Admin clicks "Build Host Image" on a GCP/Azure provider. Backend spawns a background thread that exchanges the offline token for a bearer token, POSTs a compose request to Red Hat Image Builder API, polls every 30s until the image is built (~10-15 min), then sets the resulting cloud image as the provider's `default_image`.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, urllib3 (HTTP client), Fernet encryption, PatternFly 6 React, Next.js 15

## Global Constraints

- Python 3.11+, always use `python3` not `python`
- SQLAlchemy 2.0+ (`Mapped[type]` + `mapped_column()`)
- UUIDs as strings: `UUID(as_uuid=False)`
- All host operations go through troshkad — never SSH for operational tasks
- Background threads get fresh `SessionLocal()` DB sessions, always `.close()` in `finally`
- Frontend: `"use client"`, raw `fetch()`, `useState`/`useEffect`, PatternFly components
- Tests: pytest + SQLite (type compiler overrides for JSONB/UUID already in conftest.py)
- Run `black` before committing
- Never block HTTP requests with long-running work — use background threads

---

### Task 1: Red Hat Offline Token Storage (Model + API)

**Files:**
- Modify: `src/backend/app/models/user.py` (add column)
- Modify: `src/backend/app/api/auth.py` (add 3 endpoints)
- Create: `src/backend/alembic/versions/*_add_rh_offline_token.py` (migration)
- Create: `src/backend/tests/test_rh_token.py`

**Interfaces:**
- Consumes: `app.core.encryption.encrypt/decrypt`, `app.core.auth.get_current_user`
- Produces: `User.rh_offline_token` column (encrypted text), `GET/PUT/DELETE /auth/rh-offline-token` endpoints

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_rh_token.py
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import get_db
from tests.conftest import get_test_db, TestSession
from app.models.user import User

app.dependency_overrides[get_db] = get_test_db


@pytest.fixture(autouse=True)
def _clean_users():
    db = TestSession()
    db.query(User).delete()
    db.commit()
    db.close()


client = TestClient(app)


def test_save_and_get_rh_offline_token():
    resp = client.put(
        "/api/v1/auth/rh-offline-token",
        json={"offline_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.test_token_value"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "saved"

    resp = client.get("/api/v1/auth/rh-offline-token")
    data = resp.json()
    assert data["has_token"] is True
    assert data["masked"].startswith("eyJhbGci")
    assert data["masked"].endswith("...")


def test_get_rh_offline_token_empty():
    resp = client.get("/api/v1/auth/rh-offline-token")
    data = resp.json()
    assert data["has_token"] is False
    assert data["masked"] == ""


def test_delete_rh_offline_token():
    client.put(
        "/api/v1/auth/rh-offline-token",
        json={"offline_token": "some_token_value"},
    )
    resp = client.delete("/api/v1/auth/rh-offline-token")
    assert resp.status_code == 204

    resp = client.get("/api/v1/auth/rh-offline-token")
    assert resp.json()["has_token"] is False


def test_save_rh_offline_token_empty_rejected():
    resp = client.put(
        "/api/v1/auth/rh-offline-token",
        json={"offline_token": "  "},
    )
    assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_rh_token.py -v`
Expected: FAIL — `User` model has no `rh_offline_token` column, endpoints don't exist

- [ ] **Step 3: Add column to User model**

In `src/backend/app/models/user.py`, add after line 23 (`ocp_pull_secret`):

```python
    rh_offline_token: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 4: Add API endpoints to auth.py**

In `src/backend/app/api/auth.py`, add after the `delete_ocp_pull_secret` function:

```python
@router.get("/rh-offline-token")
def get_rh_offline_token(user: User = Depends(get_current_user)):
    if not user.rh_offline_token:
        return {"has_token": False, "masked": ""}
    from app.core.encryption import decrypt

    raw = decrypt(user.rh_offline_token)
    masked = raw[:20] + "..." if len(raw) > 20 else raw
    return {"has_token": True, "masked": masked}


@router.put("/rh-offline-token")
def set_rh_offline_token(
    body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    token = body.get("offline_token", "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Offline token is required")
    from app.core.encryption import encrypt

    user.rh_offline_token = encrypt(token)
    db.commit()
    return {"status": "saved"}


@router.delete("/rh-offline-token", status_code=204)
def delete_rh_offline_token(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    user.rh_offline_token = None
    db.commit()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_rh_token.py -v`
Expected: All 4 tests PASS

- [ ] **Step 6: Create Alembic migration**

Run: `cd src/backend && ./venv/bin/python3 -m alembic revision -m "add rh_offline_token to users"`

Edit the generated migration file:

```python
def upgrade():
    op.add_column("users", sa.Column("rh_offline_token", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("users", "rh_offline_token")
```

Run: `cd src/backend && ./venv/bin/python3 -m alembic upgrade head`

- [ ] **Step 7: Commit**

```bash
cd /Users/prutledg/troshka
black src/backend/app/models/user.py src/backend/app/api/auth.py src/backend/tests/test_rh_token.py
git add src/backend/app/models/user.py src/backend/app/api/auth.py src/backend/tests/test_rh_token.py src/backend/alembic/versions/*_add_rh_offline_token*.py
git commit -m "feat: add Red Hat offline token storage with encrypted persistence"
```

---

### Task 2: Image Builder Service

**Files:**
- Create: `src/backend/app/services/image_builder_service.py`
- Create: `src/backend/tests/test_image_builder_service.py`

**Interfaces:**
- Consumes: `Provider` model (type, credentials, gcp_project_id, azure_*), `User.rh_offline_token`, `app.core.encryption.decrypt`, `app.core.database.SessionLocal`
- Produces:
  - `build_host_image(provider_id: str, user_id: str, rhel_version: str = "rhel-10") -> None` — background thread entry point
  - `get_build_status(provider_id: str) -> dict` — returns `{"status": str, "message": str, "image": str | None, "compose_id": str | None, "elapsed_seconds": int | None}`
  - `clear_build_status(provider_id: str) -> None`
  - Status values: `"idle"`, `"authenticating"`, `"building"`, `"success"`, `"error"`

- [ ] **Step 1: Write the failing tests**

```python
# src/backend/tests/test_image_builder_service.py
import json
import time
from unittest.mock import MagicMock, patch

import pytest


def _make_provider(ptype="gcp"):
    p = MagicMock()
    p.id = "prov-1234"
    p.type = ptype
    p.gcp_project_id = "troshka-rhdp"
    p.azure_subscription_id = "sub-1234"
    p.azure_resource_group = "troshka-rg"
    p.azure_location = "eastus"
    p.default_image = None
    if ptype == "gcp":
        p.get_credentials.return_value = {
            "service_account_json": {
                "client_email": "troshka@troshka-rhdp.iam.gserviceaccount.com"
            }
        }
    elif ptype == "azure":
        p.get_credentials.return_value = {
            "tenant_id": "tenant-1234",
            "subscription_id": "sub-1234",
        }
    return p


def _make_user():
    u = MagicMock()
    u.id = "user-1234"
    u.rh_offline_token = "encrypted_token"
    return u


class TestTokenExchange:
    @patch("app.services.image_builder_service._http")
    def test_get_access_token(self, mock_http):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.data = json.dumps(
            {"access_token": "bearer_tok_123", "expires_in": 900}
        ).encode()
        mock_http.request.return_value = mock_resp

        from app.services.image_builder_service import _exchange_token

        token = _exchange_token("my_offline_token")
        assert token == "bearer_tok_123"

        call_args = mock_http.request.call_args
        assert call_args[0][0] == "POST"
        assert "sso.redhat.com" in call_args[0][1]

    @patch("app.services.image_builder_service._http")
    def test_get_access_token_failure(self, mock_http):
        mock_resp = MagicMock()
        mock_resp.status = 401
        mock_resp.data = b'{"error": "invalid_grant"}'
        mock_http.request.return_value = mock_resp

        from app.services.image_builder_service import (
            ImageBuilderError,
            _exchange_token,
        )

        with pytest.raises(ImageBuilderError, match="token exchange failed"):
            _exchange_token("bad_token")


class TestComposeRequest:
    @patch("app.services.image_builder_service._http")
    def test_start_compose_gcp(self, mock_http):
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.data = json.dumps({"id": "compose-uuid-123"}).encode()
        mock_http.request.return_value = mock_resp

        from app.services.image_builder_service import _start_compose

        compose_id = _start_compose(
            access_token="tok",
            distribution="rhel-10",
            image_type="gcp",
            upload_options={
                "share_with_accounts": [
                    "troshka@project.iam.gserviceaccount.com"
                ]
            },
        )
        assert compose_id == "compose-uuid-123"

        call_args = mock_http.request.call_args
        body = json.loads(call_args[1]["body"])
        assert body["distribution"] == "rhel-10"
        assert body["image_requests"][0]["image_type"] == "gcp"
        assert "qemu-kvm" in body["customizations"]["packages"]

    @patch("app.services.image_builder_service._http")
    def test_start_compose_azure(self, mock_http):
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.data = json.dumps({"id": "compose-uuid-456"}).encode()
        mock_http.request.return_value = mock_resp

        from app.services.image_builder_service import _start_compose

        compose_id = _start_compose(
            access_token="tok",
            distribution="rhel-10",
            image_type="azure",
            upload_options={
                "tenant_id": "t-1",
                "subscription_id": "s-1",
                "resource_group": "rg",
                "image_name": "troshka-host-rhel10",
            },
        )
        assert compose_id == "compose-uuid-456"

        body = json.loads(mock_http.request.call_args[1]["body"])
        assert body["image_requests"][0]["image_type"] == "azure"


class TestComposePolling:
    @patch("app.services.image_builder_service._http")
    def test_poll_compose_success(self, mock_http):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.data = json.dumps(
            {
                "image_status": {
                    "status": "success",
                    "upload_status": {
                        "type": "gcp",
                        "options": {
                            "image_name": "composer-api-abc123",
                            "project_id": "red-hat-image-builder",
                        },
                    },
                }
            }
        ).encode()
        mock_http.request.return_value = mock_resp

        from app.services.image_builder_service import _poll_compose

        status = _poll_compose("tok", "compose-1")
        assert status["image_status"]["status"] == "success"

    @patch("app.services.image_builder_service._http")
    def test_poll_compose_building(self, mock_http):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.data = json.dumps(
            {"image_status": {"status": "building"}}
        ).encode()
        mock_http.request.return_value = mock_resp

        from app.services.image_builder_service import _poll_compose

        status = _poll_compose("tok", "compose-1")
        assert status["image_status"]["status"] == "building"


class TestImageReference:
    def test_extract_gcp_image_ref(self):
        from app.services.image_builder_service import _extract_image_reference

        status = {
            "image_status": {
                "upload_status": {
                    "type": "gcp",
                    "options": {
                        "image_name": "composer-api-abc123",
                        "project_id": "red-hat-image-builder",
                    },
                }
            }
        }
        ref = _extract_image_reference(status, "gcp")
        assert ref == "projects/red-hat-image-builder/global/images/composer-api-abc123"

    def test_extract_azure_image_ref(self):
        from app.services.image_builder_service import _extract_image_reference

        status = {
            "image_status": {
                "upload_status": {
                    "type": "azure",
                    "options": {"image_name": "troshka-host-rhel10"},
                }
            }
        }
        provider = _make_provider("azure")
        ref = _extract_image_reference(status, "azure", provider=provider)
        assert "/resourceGroups/troshka-rg/" in ref
        assert ref.endswith("/troshka-host-rhel10")


class TestBuildUploadOptions:
    def test_gcp_upload_options(self):
        from app.services.image_builder_service import _build_upload_options

        provider = _make_provider("gcp")
        opts = _build_upload_options(provider)
        assert opts["share_with_accounts"] == [
            "troshka@troshka-rhdp.iam.gserviceaccount.com"
        ]

    def test_azure_upload_options(self):
        from app.services.image_builder_service import _build_upload_options

        provider = _make_provider("azure")
        opts = _build_upload_options(provider)
        assert opts["tenant_id"] == "tenant-1234"
        assert opts["subscription_id"] == "sub-1234"
        assert opts["resource_group"] == "troshka-rg"
        assert "troshka-host-rhel" in opts["image_name"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_image_builder_service.py -v`
Expected: FAIL — `image_builder_service` module does not exist

- [ ] **Step 3: Implement the Image Builder service**

```python
# src/backend/app/services/image_builder_service.py
import json
import logging
import time
import urllib.parse

import urllib3

logger = logging.getLogger(__name__)

SSO_URL = "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
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


def _extract_image_reference(compose_status: dict, provider_type: str, provider=None) -> str:
    upload = compose_status["image_status"]["upload_status"]
    opts = upload.get("options", {})
    if provider_type == "gcp":
        project_id = opts.get("project_id", "red-hat-image-builder")
        image_name = opts["image_name"]
        return f"projects/{project_id}/global/images/{image_name}"
    elif provider_type == "azure":
        image_name = opts["image_name"]
        sub = provider.azure_subscription_id if provider else opts.get("subscription_id", "")
        rg = provider.azure_resource_group if provider else opts.get("resource_group", "")
        return (
            f"/subscriptions/{sub}/resourceGroups/{rg}"
            f"/providers/Microsoft.Compute/images/{image_name}"
        )
    raise ImageBuilderError(f"Unknown provider type: {provider_type}")


def _build_upload_options(provider) -> dict:
    if provider.type == "gcp":
        creds = provider.get_credentials()
        sa_json = creds.get("service_account_json", {})
        if isinstance(sa_json, str):
            sa_json = json.loads(sa_json)
        email = sa_json.get("client_email", "")
        return {"share_with_accounts": [email]}
    elif provider.type == "azure":
        creds = provider.get_credentials()
        return {
            "tenant_id": creds.get("tenant_id", provider.azure_subscription_id),
            "subscription_id": provider.azure_subscription_id,
            "resource_group": provider.azure_resource_group,
            "image_name": f"troshka-host-rhel10-{int(time.time())}",
        }
    raise ImageBuilderError(f"Unsupported provider type for image build: {provider.type}")


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
        provider = db.query(Provider).get(provider_id)
        user = db.query(User).get(user_id)
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

        upload_options = _build_upload_options(provider)
        image_type = "gcp" if provider.type == "gcp" else "azure"

        _build_progress[provider_id] = {
            "status": "building",
            "message": f"Compose submitted — building {rhel_version} image...",
            "image": None,
            "compose_id": None,
            "elapsed_seconds": int(time.time() - start_time),
        }

        compose_id = _start_compose(access_token, rhel_version, image_type, upload_options)
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

            image_status = status.get("image_status", {}).get("status", "unknown")
            _build_progress[provider_id].update(
                {
                    "message": f"Image Builder status: {image_status} ({elapsed}s elapsed)",
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
                logger.info(
                    "Image build complete for %s: %s", provider.name, image_ref
                )
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_image_builder_service.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka
black src/backend/app/services/image_builder_service.py src/backend/tests/test_image_builder_service.py
git add src/backend/app/services/image_builder_service.py src/backend/tests/test_image_builder_service.py
git commit -m "feat: add Image Builder service for custom RHEL image builds"
```

---

### Task 3: Build Image API Endpoint + Azure Driver Fix

**Files:**
- Modify: `src/backend/app/api/providers.py` (add 2 endpoints)
- Modify: `src/backend/app/services/providers/azure.py` (handle managed image IDs)
- Create: `src/backend/tests/test_build_image_api.py`

**Interfaces:**
- Consumes: `image_builder_service.build_host_image`, `image_builder_service.get_build_status`, `image_builder_service.clear_build_status`, `Provider` model, `User` model
- Produces: `POST /providers/{id}/build-image`, `GET /providers/{id}/build-image/status`, `DELETE /providers/{id}/build-image/status`

- [ ] **Step 1: Write the failing tests**

```python
# src/backend/tests/test_build_image_api.py
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import get_db
from app.models.provider import Provider
from app.models.user import User
from tests.conftest import get_test_db, TestSession

app.dependency_overrides[get_db] = get_test_db

client = TestClient(app)


@pytest.fixture(autouse=True)
def _setup():
    db = TestSession()
    db.query(Provider).delete()
    db.query(User).delete()
    user = User(id="u1", email="admin@test.com", role="admin")
    user.rh_offline_token = "encrypted"
    db.add(user)
    prov = Provider(id="p1", name="test-gcp", type="gcp")
    prov.gcp_project_id = "my-project"
    prov.set_credentials(
        {
            "service_account_json": {
                "client_email": "sa@proj.iam.gserviceaccount.com"
            }
        }
    )
    db.add(prov)
    db.commit()
    db.close()
    yield
    from app.services import image_builder_service

    image_builder_service._build_progress.clear()


@patch("app.api.providers.threading")
def test_build_image_starts_thread(mock_threading):
    resp = client.post("/api/v1/providers/p1/build-image", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "started"
    mock_threading.Thread.assert_called_once()
    mock_threading.Thread.return_value.start.assert_called_once()


def test_build_image_unsupported_provider():
    db = TestSession()
    prov = Provider(id="p2", name="test-ec2", type="ec2")
    db.add(prov)
    db.commit()
    db.close()

    resp = client.post("/api/v1/providers/p2/build-image", json={})
    assert resp.status_code == 400
    assert "GCP or Azure" in resp.json()["detail"]


def test_build_image_not_found():
    resp = client.post("/api/v1/providers/nonexistent/build-image", json={})
    assert resp.status_code == 404


def test_build_image_status_idle():
    resp = client.get("/api/v1/providers/p1/build-image/status")
    assert resp.status_code == 200
    assert resp.json()["status"] == "idle"


def test_build_image_status_in_progress():
    from app.services import image_builder_service

    image_builder_service._build_progress["p1"] = {
        "status": "building",
        "message": "Building...",
        "compose_id": "c-1",
        "elapsed_seconds": 120,
    }
    resp = client.get("/api/v1/providers/p1/build-image/status")
    data = resp.json()
    assert data["status"] == "building"
    assert data["compose_id"] == "c-1"


def test_build_image_already_running():
    from app.services import image_builder_service

    image_builder_service._build_progress["p1"] = {"status": "building"}
    resp = client.post("/api/v1/providers/p1/build-image", json={})
    assert resp.status_code == 409


def test_clear_build_status():
    from app.services import image_builder_service

    image_builder_service._build_progress["p1"] = {"status": "success"}
    resp = client.delete("/api/v1/providers/p1/build-image/status")
    assert resp.status_code == 204
    assert "p1" not in image_builder_service._build_progress
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_build_image_api.py -v`
Expected: FAIL — endpoints don't exist

- [ ] **Step 3: Add API endpoints to providers.py**

Add at the bottom of `src/backend/app/api/providers.py`:

```python
@router.post("/{provider_id}/build-image")
def build_image(
    provider_id: str,
    body: dict = None,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    import threading

    from app.services import image_builder_service

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    if provider.type not in ("gcp", "azure"):
        raise HTTPException(
            status_code=400,
            detail="Image Builder only supports GCP or Azure providers",
        )

    current = image_builder_service.get_build_status(provider_id)
    if current.get("status") in ("authenticating", "building"):
        raise HTTPException(
            status_code=409, detail="A build is already in progress"
        )

    body = body or {}
    rhel_version = body.get("rhel_version", "rhel-10")

    threading.Thread(
        target=image_builder_service.build_host_image,
        args=(provider_id, user.id, rhel_version),
        daemon=True,
        name=f"image-build-{provider_id[:8]}",
    ).start()

    return {"status": "started", "message": f"Building {rhel_version} image..."}


@router.get("/{provider_id}/build-image/status")
def build_image_status(
    provider_id: str,
    user: User = Depends(require_role("admin")),
):
    from app.services import image_builder_service

    return image_builder_service.get_build_status(provider_id)


@router.delete("/{provider_id}/build-image/status", status_code=204)
def clear_build_image_status(
    provider_id: str,
    user: User = Depends(require_role("admin")),
):
    from app.services import image_builder_service

    image_builder_service.clear_build_status(provider_id)
```

- [ ] **Step 4: Update Azure driver to handle managed image resource IDs**

In `src/backend/app/services/providers/azure.py`, modify `_parse_image_urn` (line 172) to handle both URN format and resource IDs:

```python
def _parse_image_urn(image_urn):
    """Parse an Azure image reference.

    Accepts either:
    - URN format: 'Publisher:Offer:Sku:Version' (marketplace images)
    - Resource ID: '/subscriptions/.../images/name' (managed images from Image Builder)
    """
    if image_urn.startswith("/subscriptions/"):
        return {"id": image_urn}
    parts = image_urn.split(":")
    if len(parts) != 4:
        raise ValueError(
            f"Invalid Azure image reference '{image_urn}' — "
            f"expected URN (Publisher:Offer:Sku:Version) or resource ID (/subscriptions/...)"
        )
    return {
        "publisher": parts[0],
        "offer": parts[1],
        "sku": parts[2],
        "version": parts[3],
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_build_image_api.py -v`
Expected: All tests PASS

Run existing Azure driver tests to confirm no regression:
`cd src/backend && ./venv/bin/python3 -m pytest tests/test_provider_driver.py -v -k azure 2>/dev/null; echo "exit: $?"`

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka
black src/backend/app/api/providers.py src/backend/app/services/providers/azure.py src/backend/tests/test_build_image_api.py
git add src/backend/app/api/providers.py src/backend/app/services/providers/azure.py src/backend/tests/test_build_image_api.py
git commit -m "feat: add build-image API endpoint, support Azure managed image IDs"
```

---

### Task 4: Frontend — Settings Page Offline Token Field

**Files:**
- Modify: `src/frontend/src/app/settings/page.tsx`

**Interfaces:**
- Consumes: `GET /api/v1/auth/rh-offline-token`, `PUT /api/v1/auth/rh-offline-token`, `DELETE /api/v1/auth/rh-offline-token`
- Produces: UI section for managing Red Hat offline token (follows OCP pull secret pattern exactly)

- [ ] **Step 1: Add state variables for offline token**

In `src/frontend/src/app/settings/page.tsx`, after the OCP Pull Secret state variables (around line 36), add:

```typescript
  // Red Hat Offline Token
  const [rhTokenMasked, setRhTokenMasked] = useState("");
  const [hasRhToken, setHasRhToken] = useState(false);
  const [rhTokenInput, setRhTokenInput] = useState("");
  const [rhTokenSaving, setRhTokenSaving] = useState(false);
  const [rhTokenEdit, setRhTokenEdit] = useState(false);
```

- [ ] **Step 2: Add useEffect fetch for token status**

In the existing `useEffect` (around line 38), add after the OCP pull secret fetch:

```typescript
    fetch("/api/v1/auth/rh-offline-token")
      .then((r) => r.json())
      .then((data) => { setHasRhToken(data.has_token); setRhTokenMasked(data.masked || ""); })
      .catch(() => {});
```

- [ ] **Step 3: Add the UI section**

Add a new `<PageSection>` before the OCP Pull Secret section (before the `<PageSection>` containing "OCP Pull Secret"). This follows the identical pattern as the OCP pull secret UI:

```tsx
      <PageSection>
        <Title headingLevel="h2" style={{ marginBottom: 12 }}>Red Hat Offline Token</Title>
        <p style={{ fontSize: 13, opacity: 0.7, marginBottom: 12 }}>
          Required for building custom host images via Image Builder. Generate a token at{" "}
          <a href="https://access.redhat.com/management/api" target="_blank" rel="noreferrer" style={{ color: "#3b82f6" }}>access.redhat.com/management/api</a>.
        </p>
        {hasRhToken && !rhTokenEdit ? (
          <Card>
            <CardBody style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div style={{ fontSize: 11, fontFamily: "monospace", opacity: 0.6, wordBreak: "break-all" }}>{rhTokenMasked}</div>
              <div style={{ display: "flex", gap: 8 }}>
                <Button variant="secondary" onClick={() => setRhTokenEdit(true)}>Replace</Button>
                <Button variant="danger" onClick={async () => { await fetch("/api/v1/auth/rh-offline-token", { method: "DELETE" }); setHasRhToken(false); setRhTokenMasked(""); }}>Delete</Button>
              </div>
            </CardBody>
          </Card>
        ) : (
          <Card>
            <CardBody>
              <input type="password" style={{ width: "100%", padding: "8px 10px", borderRadius: 6, fontSize: 12, fontFamily: "monospace", border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)" }} value={rhTokenInput} onChange={(e) => setRhTokenInput(e.target.value)} placeholder="eyJhbGci..." />
              <div style={{ display: "flex", gap: 8, marginTop: 8, justifyContent: "flex-end" }}>
                {rhTokenEdit && <Button variant="secondary" onClick={() => { setRhTokenEdit(false); setRhTokenInput(""); }}>Cancel</Button>}
                <Button variant="primary" isDisabled={!rhTokenInput.trim() || rhTokenSaving} onClick={async () => {
                  setRhTokenSaving(true);
                  const resp = await fetch("/api/v1/auth/rh-offline-token", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ offline_token: rhTokenInput }) });
                  if (resp.ok) { setHasRhToken(true); setRhTokenEdit(false); setRhTokenInput(""); const data = await fetch("/api/v1/auth/rh-offline-token").then(r => r.json()); setRhTokenMasked(data.masked || ""); }
                  else { const err = await resp.json().catch(() => ({ detail: "Save failed" })); alert(err.detail || "Save failed"); }
                  setRhTokenSaving(false);
                }}>{rhTokenSaving ? "Saving..." : "Save Token"}</Button>
              </div>
            </CardBody>
          </Card>
        )}
      </PageSection>
```

- [ ] **Step 4: Manual test in browser**

1. Run dev server: `cd /Users/prutledg/troshka && ./dev-services.sh start`
2. Open http://localhost:3100/settings
3. Verify "Red Hat Offline Token" section appears with input field
4. Enter a dummy token, click "Save Token" — should show masked token
5. Click "Replace" — should show input again
6. Click "Delete" — should clear

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka
git add src/frontend/src/app/settings/page.tsx
git commit -m "feat: add Red Hat offline token field to settings page"
```

---

### Task 5: Frontend — Provider Page Build Image Button

**Files:**
- Modify: `src/frontend/src/app/admin/providers/page.tsx`

**Interfaces:**
- Consumes: `POST /api/v1/providers/{id}/build-image`, `GET /api/v1/providers/{id}/build-image/status`, `DELETE /api/v1/providers/{id}/build-image/status`
- Produces: "Build Host Image" button on GCP/Azure provider cards with RHEL version select and progress display

- [ ] **Step 1: Add state variables**

Near the top of the provider page component, add state for build image status:

```typescript
  const [buildStatus, setBuildStatus] = useState<Record<string, { status: string; message?: string; image?: string; elapsed_seconds?: number }>>({});
  const [buildingProvider, setBuildingProvider] = useState<string | null>(null);
  const [rhelVersion, setRhelVersion] = useState<Record<string, string>>({});
```

- [ ] **Step 2: Add build status polling**

Add a `useEffect` that polls build status for any provider with an active build:

```typescript
  useEffect(() => {
    const activeBuilds = Object.entries(buildStatus).filter(
      ([, s]) => s.status === "authenticating" || s.status === "building"
    );
    if (activeBuilds.length === 0) return;

    const interval = setInterval(async () => {
      for (const [pid] of activeBuilds) {
        try {
          const resp = await fetch(`/api/v1/providers/${pid}/build-image/status`);
          const data = await resp.json();
          setBuildStatus((prev) => ({ ...prev, [pid]: data }));
          if (data.status === "success") {
            loadProviders();
          }
          if (data.status === "success" || data.status === "error") {
            setBuildingProvider(null);
          }
        } catch { /* ignore */ }
      }
    }, 10000);
    return () => clearInterval(interval);
  }, [buildStatus]);
```

- [ ] **Step 3: Add the startBuild handler function**

```typescript
  const startBuild = async (providerId: string) => {
    setBuildingProvider(providerId);
    setBuildStatus((prev) => ({ ...prev, [providerId]: { status: "authenticating", message: "Starting..." } }));
    try {
      const version = rhelVersion[providerId] || "rhel-10";
      const resp = await fetch(`/api/v1/providers/${providerId}/build-image`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rhel_version: version }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: "Failed" }));
        setBuildStatus((prev) => ({ ...prev, [providerId]: { status: "error", message: err.detail || "Failed" } }));
        setBuildingProvider(null);
      }
    } catch {
      setBuildStatus((prev) => ({ ...prev, [providerId]: { status: "error", message: "Connection failed" } }));
      setBuildingProvider(null);
    }
  };
```

- [ ] **Step 4: Add the Build Image UI block in the provider card**

In the existing provider card rendering, after the existing action buttons (Test, Setup Console, etc.) and only for GCP/Azure providers, add:

```tsx
{(p.type === "gcp" || p.type === "azure") && (
  <div style={{ marginTop: 16, padding: "12px 0", borderTop: "1px solid var(--pf-t--global--border--color--default)" }}>
    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
      <span style={{ fontSize: 13, fontWeight: 600 }}>Build Host Image</span>
      <select
        value={rhelVersion[p.id] || "rhel-10"}
        onChange={(e) => setRhelVersion((prev) => ({ ...prev, [p.id]: e.target.value }))}
        style={{ padding: "4px 8px", borderRadius: 4, fontSize: 12, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)" }}
      >
        <option value="rhel-10">RHEL 10</option>
        <option value="rhel-9">RHEL 9</option>
      </select>
      <Button
        variant="secondary"
        isLoading={buildingProvider === p.id}
        isDisabled={buildingProvider === p.id}
        onClick={() => startBuild(p.id)}
      >
        Build Image
      </Button>
      {buildStatus[p.id]?.status === "success" && (
        <Button variant="link" onClick={async () => {
          await fetch(`/api/v1/providers/${p.id}/build-image/status`, { method: "DELETE" });
          setBuildStatus((prev) => { const n = { ...prev }; delete n[p.id]; return n; });
        }}>Dismiss</Button>
      )}
    </div>
    {buildStatus[p.id] && buildStatus[p.id].status !== "idle" && (
      <div style={{
        marginTop: 8, fontSize: 12, padding: "6px 10px", borderRadius: 4,
        background: buildStatus[p.id].status === "error" ? "var(--pf-t--global--color--status--danger--default)" :
                    buildStatus[p.id].status === "success" ? "var(--pf-t--global--color--status--success--default)" :
                    "var(--pf-t--global--color--status--info--default)",
        color: "#fff",
      }}>
        {buildStatus[p.id].message}
        {buildStatus[p.id].elapsed_seconds ? ` (${Math.round(buildStatus[p.id].elapsed_seconds! / 60)}m)` : ""}
      </div>
    )}
  </div>
)}
```

- [ ] **Step 5: Load initial build status on page load**

In the existing `loadProviders` function or the initial `useEffect`, after loading providers, fetch build status for each GCP/Azure provider:

```typescript
    // Inside loadProviders or after it runs, for each provider:
    for (const p of data.filter((pr: { type: string }) => pr.type === "gcp" || pr.type === "azure")) {
      fetch(`/api/v1/providers/${p.id}/build-image/status`)
        .then((r) => r.json())
        .then((s) => {
          if (s.status !== "idle") {
            setBuildStatus((prev) => ({ ...prev, [p.id]: s }));
            if (s.status === "authenticating" || s.status === "building") {
              setBuildingProvider(p.id);
            }
          }
        })
        .catch(() => {});
    }
```

- [ ] **Step 6: Manual test in browser**

1. Open http://localhost:3100/admin/providers
2. Find a GCP or Azure provider
3. Verify "Build Host Image" section appears with RHEL version dropdown and Build button
4. Click Build Image — button should show loading spinner
5. Status message should appear below
6. (Full end-to-end test requires a real Red Hat offline token)

- [ ] **Step 7: Commit**

```bash
cd /Users/prutledg/troshka
git add src/frontend/src/app/admin/providers/page.tsx
git commit -m "feat: add Build Host Image button to provider admin page"
```

---

## Verification Checklist

After all tasks are complete:

- [ ] All backend tests pass: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
- [ ] Alembic migration applies cleanly: `cd src/backend && ./venv/bin/python3 -m alembic upgrade head`
- [ ] Settings page shows offline token field at http://localhost:3100/settings
- [ ] Provider page shows Build Image button for GCP/Azure providers
- [ ] Build button is hidden for EC2/OCP Virt providers
- [ ] Existing provider functionality (Test, Setup Console, etc.) still works
- [ ] Cross-reference package list in `image_builder_service.py` against `agent_deployer.py` install list — add any missing packages
