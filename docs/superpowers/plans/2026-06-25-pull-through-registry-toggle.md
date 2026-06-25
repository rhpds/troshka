# Pull-Through Registry Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a user-level toggle so OCP cluster installs automatically mirror pulls through the user's pull-through registry.

**Architecture:** When toggle is on, user provides registry URL, username, and password. Backend constructs the pull secret JSON and pull-through mirror config, injects both into OCP deploys. Frontend swaps the pull secret textarea for three credential fields when toggled.

**Tech Stack:** Python/FastAPI (backend), Next.js/PatternFly 6 (frontend), Alembic (migration), SQLite (tests), PostgreSQL (dev)

## Global Constraints

- User model at `src/backend/app/models/user.py`
- Auth API at `src/backend/app/api/auth.py`
- Settings page at `src/frontend/src/app/settings/page.tsx`
- From-template endpoint at `src/backend/app/api/projects.py:202-373`
- Pull-through config dict shape: `{"enabled": True, "url": str, "orgs": {"registry.redhat.io": "registry_redhat_io", "quay.io": "quay_io"}}`
- Tests use SQLite via `tests/conftest.py` — run with `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
- Run `black` before committing
- Encrypt sensitive fields with `app.core.encryption.encrypt()` / `decrypt()`

---

### Task 1: User model + migration

**Files:**
- Modify: `src/backend/app/models/user.py:23` (add columns after `ocp_pull_secret`)
- Create: `src/backend/alembic/versions/<auto>_add_pull_through_registry_to_users.py`

**Interfaces:**
- Produces:
  - `User.pull_through_registry` — `Mapped[bool]`, default `False`
  - `User.pull_through_registry_url` — `Mapped[str | None]`
  - `User.pull_through_registry_user` — `Mapped[str | None]`
  - `User.pull_through_registry_password` — `Mapped[str | None]` (encrypted)

- [ ] **Step 1: Add columns to User model**

In `src/backend/app/models/user.py`, add after line 23 (`ocp_pull_secret`):

```python
pull_through_registry: Mapped[bool] = mapped_column(default=False, server_default="false")
pull_through_registry_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
pull_through_registry_user: Mapped[str | None] = mapped_column(String(255), nullable=True)
pull_through_registry_password: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 2: Generate Alembic migration**

```bash
cd src/backend && ./venv/bin/python3 -m alembic revision -m "add pull_through_registry to users"
```

Edit the generated migration file. The `upgrade()` function:

```python
from alembic import op
import sqlalchemy as sa

def upgrade():
    op.add_column("users", sa.Column("pull_through_registry", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("users", sa.Column("pull_through_registry_url", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("pull_through_registry_user", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("pull_through_registry_password", sa.Text(), nullable=True))

def downgrade():
    op.drop_column("users", "pull_through_registry_password")
    op.drop_column("users", "pull_through_registry_user")
    op.drop_column("users", "pull_through_registry_url")
    op.drop_column("users", "pull_through_registry")
```

- [ ] **Step 3: Run migration**

```bash
cd src/backend && ./venv/bin/python3 -m alembic upgrade head
```

- [ ] **Step 4: Run existing tests to verify nothing broke**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_projects.py tests/test_patterns.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
black src/backend/app/models/user.py
git add src/backend/app/models/user.py src/backend/alembic/versions/*pull_through*
git commit -m "feat: add pull-through registry columns to User model"
```

---

### Task 2: Auth API endpoints

**Files:**
- Modify: `src/backend/app/api/auth.py:156-193` (extend GET, PUT, add PATCH)
- Create: `src/backend/tests/test_pull_through_registry.py`

**Interfaces:**
- Consumes: `User.pull_through_registry`, `User.pull_through_registry_url`, `User.pull_through_registry_user`, `User.pull_through_registry_password` from Task 1
- Produces:
  - `GET /auth/ocp-pull-secret` returns `{"has_secret": bool, "masked": str, "pull_through_registry": bool, "pull_through_registry_url": str}`
  - `PUT /auth/ocp-pull-secret` accepts either `{"pull_secret": str}` OR `{"pull_through_registry": true, "pull_through_registry_url": str, "pull_through_registry_user": str, "pull_through_registry_password": str}`
  - `PATCH /auth/ocp-pull-secret` accepts `{"pull_through_registry": bool}` plus optional url/user/password updates

- [ ] **Step 1: Write failing tests**

Create `src/backend/tests/test_pull_through_registry.py`:

```python
import json

from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

_db = TestSession()
_user = User(
    email="ptr-test@example.com",
    display_name="PTR Tester",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
_db.close()

HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def test_get_includes_pull_through_fields():
    resp = client.get("/api/v1/auth/ocp-pull-secret", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["pull_through_registry"] is False
    assert data["pull_through_registry_url"] == ""


def test_put_with_pull_through_creds():
    resp = client.put(
        "/api/v1/auth/ocp-pull-secret",
        headers=HEADERS,
        json={
            "pull_through_registry": True,
            "pull_through_registry_url": "my-registry.example.com",
            "pull_through_registry_user": "puller",
            "pull_through_registry_password": "secret123",
        },
    )
    assert resp.status_code == 200
    get_resp = client.get("/api/v1/auth/ocp-pull-secret", headers=HEADERS)
    data = get_resp.json()
    assert data["pull_through_registry"] is True
    assert data["pull_through_registry_url"] == "my-registry.example.com"
    assert data["has_secret"] is True


def test_put_pull_through_constructs_pull_secret():
    client.put(
        "/api/v1/auth/ocp-pull-secret",
        headers=HEADERS,
        json={
            "pull_through_registry": True,
            "pull_through_registry_url": "my-registry.example.com",
            "pull_through_registry_user": "puller",
            "pull_through_registry_password": "secret123",
        },
    )
    db = TestSession()
    u = db.query(User).filter_by(email="ptr-test@example.com").first()
    from app.core.encryption import decrypt

    ps = json.loads(decrypt(u.ocp_pull_secret))
    assert "my-registry.example.com" in ps["auths"]
    db.close()


def test_put_pull_through_requires_all_fields():
    resp = client.put(
        "/api/v1/auth/ocp-pull-secret",
        headers=HEADERS,
        json={
            "pull_through_registry": True,
            "pull_through_registry_url": "my-registry.example.com",
        },
    )
    assert resp.status_code == 400


def test_patch_toggle():
    client.put(
        "/api/v1/auth/ocp-pull-secret",
        headers=HEADERS,
        json={
            "pull_through_registry": True,
            "pull_through_registry_url": "my-registry.example.com",
            "pull_through_registry_user": "puller",
            "pull_through_registry_password": "secret123",
        },
    )
    resp = client.patch(
        "/api/v1/auth/ocp-pull-secret",
        headers=HEADERS,
        json={"pull_through_registry": False},
    )
    assert resp.status_code == 200
    get_resp = client.get("/api/v1/auth/ocp-pull-secret", headers=HEADERS)
    assert get_resp.json()["pull_through_registry"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_pull_through_registry.py -v
```

Expected: 5 failures.

- [ ] **Step 3: Update GET endpoint**

In `src/backend/app/api/auth.py`, modify `get_ocp_pull_secret()` (line 156):

```python
@router.get("/ocp-pull-secret")
def get_ocp_pull_secret(user: User = Depends(get_current_user)):
    if not user.ocp_pull_secret:
        return {
            "has_secret": False,
            "masked": "",
            "pull_through_registry": user.pull_through_registry,
            "pull_through_registry_url": user.pull_through_registry_url or "",
        }
    from app.core.encryption import decrypt

    raw = decrypt(user.ocp_pull_secret)
    masked = raw[:20] + "..." if len(raw) > 20 else raw
    return {
        "has_secret": True,
        "masked": masked,
        "pull_through_registry": user.pull_through_registry,
        "pull_through_registry_url": user.pull_through_registry_url or "",
    }
```

- [ ] **Step 4: Update PUT endpoint**

In `src/backend/app/api/auth.py`, replace `set_ocp_pull_secret()` (line 167):

```python
@router.put("/ocp-pull-secret")
def set_ocp_pull_secret(
    body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    import base64
    import json

    from app.core.encryption import encrypt

    if body.get("pull_through_registry"):
        url = body.get("pull_through_registry_url", "").strip()
        ptr_user = body.get("pull_through_registry_user", "").strip()
        ptr_pass = body.get("pull_through_registry_password", "").strip()
        if not url or not ptr_user or not ptr_pass:
            raise HTTPException(
                status_code=400,
                detail="Registry URL, username, and password are all required",
            )
        auth_b64 = base64.b64encode(f"{ptr_user}:{ptr_pass}".encode()).decode()
        pull_secret = json.dumps({"auths": {url: {"auth": auth_b64}}})
        user.ocp_pull_secret = encrypt(pull_secret)
        user.pull_through_registry = True
        user.pull_through_registry_url = url
        user.pull_through_registry_user = ptr_user
        user.pull_through_registry_password = encrypt(ptr_pass)
    else:
        secret = body.get("pull_secret", "").strip()
        if not secret:
            raise HTTPException(status_code=400, detail="Pull secret is required")
        try:
            json.loads(secret)
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=400, detail="Pull secret must be valid JSON"
            )
        user.ocp_pull_secret = encrypt(secret)
        user.pull_through_registry = False
        user.pull_through_registry_url = None
        user.pull_through_registry_user = None
        user.pull_through_registry_password = None
    db.commit()
    return {"status": "saved"}
```

- [ ] **Step 5: Add PATCH endpoint**

In `src/backend/app/api/auth.py`, add after the DELETE endpoint (after line 192):

```python
@router.patch("/ocp-pull-secret")
def patch_ocp_pull_secret(
    body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    if "pull_through_registry" in body:
        user.pull_through_registry = bool(body["pull_through_registry"])
    db.commit()
    return {"status": "updated"}
```

- [ ] **Step 6: Run tests**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_pull_through_registry.py -v
```

Expected: 5 pass.

- [ ] **Step 7: Run full suite**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -q
```

Expected: 247 passed (242 existing + 5 new).

- [ ] **Step 8: Commit**

```bash
black src/backend/app/api/auth.py src/backend/tests/test_pull_through_registry.py
git add src/backend/app/api/auth.py src/backend/tests/test_pull_through_registry.py
git commit -m "feat: pull-through registry API endpoints with credential storage"
```

---

### Task 3: Backend integration — inject pull-through config into OCP deploys

**Files:**
- Modify: `src/backend/app/api/projects.py:316-371` (inject user's pull-through config into `resolved` and construct `pull_secret_json`)
- Modify: `src/backend/tests/test_pull_through_registry.py` (add integration test)

**Interfaces:**
- Consumes: `User.pull_through_registry`, `User.pull_through_registry_url` from Tasks 1-2
- Produces: `resolved["pull_through_registry"]` dict and `pull_secret_json` injected before `customize_ocp()` is called

- [ ] **Step 1: Write failing integration test**

Append to `src/backend/tests/test_pull_through_registry.py`:

```python
def test_build_pull_through_config():
    from app.api.projects import _build_pull_through_config

    config = _build_pull_through_config("my-registry.example.com")
    assert config["enabled"] is True
    assert config["url"] == "my-registry.example.com"
    assert config["orgs"]["registry.redhat.io"] == "registry_redhat_io"
    assert config["orgs"]["quay.io"] == "quay_io"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_pull_through_registry.py::test_build_pull_through_config -v
```

Expected: ImportError — `_build_pull_through_config` not found.

- [ ] **Step 3: Add helper function and injection logic**

In `src/backend/app/api/projects.py`, add a helper function (before the `/from-template` endpoint, around line 200):

```python
def _build_pull_through_config(registry_url: str) -> dict:
    return {
        "enabled": True,
        "url": registry_url,
        "orgs": {
            "registry.redhat.io": "registry_redhat_io",
            "quay.io": "quay_io",
        },
    }
```

Then in the `/from-template` endpoint, after line 320 (`pull_secret_json = decrypt(user.ocp_pull_secret)`), add:

```python
    if not resolved.get("pull_through_registry") and user.pull_through_registry:
        if user.pull_through_registry_url:
            resolved["pull_through_registry"] = _build_pull_through_config(
                user.pull_through_registry_url
            )
```

- [ ] **Step 4: Run tests**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_pull_through_registry.py -v
```

Expected: 6 pass.

- [ ] **Step 5: Run full suite**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -q
```

Expected: 248 passed.

- [ ] **Step 6: Commit**

```bash
black src/backend/app/api/projects.py src/backend/tests/test_pull_through_registry.py
git add src/backend/app/api/projects.py src/backend/tests/test_pull_through_registry.py
git commit -m "feat: inject pull-through config into OCP deploys from user settings"
```

---

### Task 4: Frontend toggle on settings page

**Files:**
- Modify: `src/frontend/src/app/settings/page.tsx:38-43,248-280` (add state + conditional UI)

**Interfaces:**
- Consumes:
  - `GET /auth/ocp-pull-secret` → `{pull_through_registry, pull_through_registry_url}`
  - `PUT /auth/ocp-pull-secret` → with pull-through fields or pull_secret
  - `PATCH /auth/ocp-pull-secret` → toggle only

- [ ] **Step 1: Add state variables**

In `src/frontend/src/app/settings/page.tsx`, near the existing pull secret state (around line 38-43), add:

```typescript
const [pullThroughRegistry, setPullThroughRegistry] = useState(false);
const [ptrUrl, setPtrUrl] = useState("");
const [ptrUser, setPtrUser] = useState("");
const [ptrPassword, setPtrPassword] = useState("");
const [ptrSaving, setPtrSaving] = useState(false);
```

- [ ] **Step 2: Load the toggle state from GET response**

Find the `useEffect` that fetches `/api/v1/auth/ocp-pull-secret` (around line 62-65). After setting `hasPullSecret` and `pullSecretMasked`, add:

```typescript
setPullThroughRegistry(data.pull_through_registry || false);
setPtrUrl(data.pull_through_registry_url || "");
```

- [ ] **Step 3: Add Switch and conditional fields in the OCP Pull Secret section**

Replace the OCP Pull Secret `<PageSection>` (lines 248-281) with:

```tsx
      <PageSection>
        <Title headingLevel="h2" style={{ marginBottom: 12 }}>OCP Pull Secret</Title>
        <p style={{ fontSize: 13, opacity: 0.7, marginBottom: 12 }}>
          Required for OpenShift installation. Get yours from{" "}
          <a href="https://console.redhat.com/openshift/install/pull-secret" target="_blank" rel="noreferrer" style={{ color: "#3b82f6" }}>console.redhat.com</a>.
        </p>
        <div style={{ marginBottom: 16 }}>
          <Switch
            id="pull-through-registry"
            label="Use a pull-through registry"
            isChecked={pullThroughRegistry}
            onChange={(_event, checked) => {
              setPullThroughRegistry(checked);
              if (!checked && hasPullSecret) {
                fetch("/api/v1/auth/ocp-pull-secret", {
                  method: "PATCH",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ pull_through_registry: false }),
                });
              }
            }}
          />
          <p style={{ fontSize: 12, opacity: 0.6, marginTop: 4, marginLeft: 48 }}>
            Mirror image pulls through this registry instead of pulling directly from registry.redhat.io and quay.io.
          </p>
        </div>
        {pullThroughRegistry ? (
          <Card>
            <CardBody>
              {hasPullSecret && !pullSecretEdit ? (
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div style={{ fontSize: 12 }}>Registry: <span style={{ fontFamily: "monospace" }}>{ptrUrl}</span></div>
                  <div style={{ display: "flex", gap: 8 }}>
                    <Button variant="secondary" onClick={() => setPullSecretEdit(true)}>Update</Button>
                    <Button variant="danger" onClick={async () => { await fetch("/api/v1/auth/ocp-pull-secret", { method: "DELETE" }); setHasPullSecret(false); setPullSecretMasked(""); setPullThroughRegistry(false); setPtrUrl(""); }}>Delete</Button>
                  </div>
                </div>
              ) : (
                <>
                  <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                    <div>
                      <label style={{ fontSize: 12, fontWeight: 600, display: "block", marginBottom: 4 }}>Registry URL</label>
                      <input style={{ width: "100%", padding: "6px 10px", borderRadius: 6, fontSize: 12, fontFamily: "monospace", border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)" }} value={ptrUrl} onChange={(e) => setPtrUrl(e.target.value)} placeholder="registry-quay.apps.example.com" />
                    </div>
                    <div>
                      <label style={{ fontSize: 12, fontWeight: 600, display: "block", marginBottom: 4 }}>Username</label>
                      <input style={{ width: "100%", padding: "6px 10px", borderRadius: 6, fontSize: 12, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)" }} value={ptrUser} onChange={(e) => setPtrUser(e.target.value)} placeholder="puller" />
                    </div>
                    <div>
                      <label style={{ fontSize: 12, fontWeight: 600, display: "block", marginBottom: 4 }}>Password</label>
                      <input type="password" style={{ width: "100%", padding: "6px 10px", borderRadius: 6, fontSize: 12, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)" }} value={ptrPassword} onChange={(e) => setPtrPassword(e.target.value)} placeholder="••••••••" />
                    </div>
                  </div>
                  <div style={{ display: "flex", gap: 8, marginTop: 12, justifyContent: "flex-end" }}>
                    {pullSecretEdit && <Button variant="secondary" onClick={() => { setPullSecretEdit(false); setPtrUser(""); setPtrPassword(""); }}>Cancel</Button>}
                    <Button variant="primary" isDisabled={!ptrUrl.trim() || !ptrUser.trim() || !ptrPassword.trim() || ptrSaving} onClick={async () => {
                      setPtrSaving(true);
                      const resp = await fetch("/api/v1/auth/ocp-pull-secret", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pull_through_registry: true, pull_through_registry_url: ptrUrl, pull_through_registry_user: ptrUser, pull_through_registry_password: ptrPassword }) });
                      if (resp.ok) { setHasPullSecret(true); setPullSecretEdit(false); setPtrPassword(""); const data = await fetch("/api/v1/auth/ocp-pull-secret").then(r => r.json()); setPullSecretMasked(data.masked || ""); }
                      else { const err = await resp.json().catch(() => ({ detail: "Save failed" })); alert(err.detail || "Save failed"); }
                      setPtrSaving(false);
                    }}>{ptrSaving ? "Saving..." : "Save"}</Button>
                  </div>
                </>
              )}
            </CardBody>
          </Card>
        ) : (
          <>
            {hasPullSecret && !pullSecretEdit ? (
              <Card>
                <CardBody style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div style={{ fontSize: 11, fontFamily: "monospace", opacity: 0.6, wordBreak: "break-all" }}>{pullSecretMasked}</div>
                  <div style={{ display: "flex", gap: 8 }}>
                    <Button variant="secondary" onClick={() => setPullSecretEdit(true)}>Replace</Button>
                    <Button variant="danger" onClick={async () => { await fetch("/api/v1/auth/ocp-pull-secret", { method: "DELETE" }); setHasPullSecret(false); setPullSecretMasked(""); }}>Delete</Button>
                  </div>
                </CardBody>
              </Card>
            ) : (
              <Card>
                <CardBody>
                  <textarea style={{ width: "100%", minHeight: 80, padding: "8px 10px", borderRadius: 6, fontSize: 12, fontFamily: "monospace", resize: "vertical", border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--primary--default)", color: "var(--pf-t--global--text--color--regular)" }} value={pullSecretInput} onChange={(e) => setPullSecretInput(e.target.value)} placeholder='{"auths":{"cloud.openshift.com":...}}' />
                  <div style={{ display: "flex", gap: 8, marginTop: 8, justifyContent: "flex-end" }}>
                    {pullSecretEdit && <Button variant="secondary" onClick={() => { setPullSecretEdit(false); setPullSecretInput(""); }}>Cancel</Button>}
                    <Button variant="primary" isDisabled={!pullSecretInput.trim() || pullSecretSaving} onClick={async () => {
                      setPullSecretSaving(true);
                      const resp = await fetch("/api/v1/auth/ocp-pull-secret", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pull_secret: pullSecretInput }) });
                      if (resp.ok) { setHasPullSecret(true); setPullSecretEdit(false); setPullSecretInput(""); const data = await fetch("/api/v1/auth/ocp-pull-secret").then(r => r.json()); setPullSecretMasked(data.masked || ""); }
                      else { const err = await resp.json().catch(() => ({ detail: "Save failed" })); alert(err.detail || "Save failed"); }
                      setPullSecretSaving(false);
                    }}>{pullSecretSaving ? "Saving..." : "Save Pull Secret"}</Button>
                  </div>
                </CardBody>
              </Card>
            )}
          </>
        )}
      </PageSection>
```

- [ ] **Step 4: Add Switch import**

Check the existing PatternFly imports at the top of the file. If `Switch` is not already imported, add it:

```typescript
import { Switch } from "@patternfly/react-core";
```

- [ ] **Step 5: Test in browser**

1. Open `http://localhost:3100/settings`
2. Toggle "Use a pull-through registry" — verify textarea is replaced by URL/user/password fields
3. Fill in all three fields, click Save — verify it saves and shows the registry URL
4. Toggle off — verify textarea reappears
5. Refresh — verify toggle state and URL persist
6. Click Delete — verify everything resets

- [ ] **Step 6: Commit**

```bash
git add src/frontend/src/app/settings/page.tsx
git commit -m "feat: pull-through registry toggle with credential fields on settings page"
```

---

## Verification

After all tasks are complete:

1. **Unit tests:** `cd src/backend && ./venv/bin/python3 -m pytest tests/ -q` — all pass
2. **Settings page:** toggle swaps between pull secret textarea and pull-through fields, persists correctly
3. **OCP deploy test:** create a project from the built-in `ocp-sno` template with pull-through enabled. Check the install-config on the bastion — it should contain `imageDigestMirrorSet` entries for `registry.redhat.io` and `quay.io` pointing at the configured pull-through registry URL.
