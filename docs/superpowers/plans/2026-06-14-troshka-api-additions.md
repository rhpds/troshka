# Troshka API Additions — Implementation Plan (Plan 1 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the Troshka-side API and UI features needed to support the agnosticd cloud provider integration: VM node tags, student portal, pattern name lookup, deploy-time variable injection, and template YAML refactor.

**Architecture:** All changes are in the Troshka repo. Backend additions follow the existing FastAPI + SQLAlchemy pattern. Frontend additions follow the Next.js App Router + PatternFly + Zustand pattern. New endpoints for portal and API key auth bypass SSO/OAuth proxy.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2, Alembic, Next.js 15, PatternFly 6, React Flow, Zustand

**Plan scope:** This is Plan 1 of 4. It covers Troshka API/UI changes only. Subsequent plans cover:
- Plan 2: Ansible Collection (`agnosticd.cloud_provider_troshka`)
- Plan 3: AgnosticD-v2 cloud provider playbooks
- Plan 4: Agnosticv account and catalog items

---

### Task 1: VM Node Tags — Backend (topology JSONB)

VM nodes already support arbitrary fields via `[key: string]: any` in the TypeScript interface and Python dict flexibility. Tags are stored as a `tags` dict on each VM node's `data` in topology JSONB. No model changes needed — this is purely a data convention that the frontend and inventory plugin use.

**Files:**
- Test: `src/backend/tests/test_patterns.py`

- [ ] **Step 1: Write test — tags preserved through pattern deploy**

Add to `src/backend/tests/test_patterns.py`:

```python
def test_deploy_pattern_preserves_vm_tags():
    topo = copy.deepcopy(SAMPLE_TOPOLOGY)
    topo["nodes"][0]["data"]["tags"] = {"AnsibleGroup": "bastions,showroom"}
    create_resp = client.post("/api/v1/patterns", json={
        "name": "Tag Test",
        "topology": topo,
    }, headers=HEADERS)
    pattern_id = create_resp.json()["id"]
    resp = client.post(f"/api/v1/patterns/{pattern_id}/deploy", json={
        "name": "Tag Deploy Test",
    }, headers=HEADERS)
    assert resp.status_code == 201
    vm_node = [n for n in resp.json()["topology"]["nodes"] if n["type"] == "vmNode"][0]
    assert vm_node["data"]["tags"] == {"AnsibleGroup": "bastions,showroom"}
```

Add `import copy` at the top of the file if not already imported.

- [ ] **Step 2: Run test to verify it passes**

Tags are already preserved by `_remap_topology()` because it only regenerates specific fields (IDs, MACs, controller IDs) and copies everything else in `data`. This test documents the behavior.

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_patterns.py::test_deploy_pattern_preserves_vm_tags -v`
Expected: PASS

---

### Task 2: VM Node Tags — Frontend Tag Editor

Add a "Tags" section to the VM node properties panel. Tags are key-value pairs stored in `data.tags` on the VM node.

**Files:**
- Modify: `src/frontend/src/components/canvas/PropertiesPanel.tsx`
- Modify: `src/frontend/src/stores/canvasStore.ts` (VMNodeData interface)

- [ ] **Step 1: Add `tags` to VMNodeData interface**

In `src/frontend/src/stores/canvasStore.ts`, update the `VMNodeData` interface (around line 17):

```typescript
export interface VMNodeData {
  label: string;
  name: string;
  vcpus: number;
  ram: number;
  os: string;
  status: "running" | "stopped" | "redeploying";
  bootOrder?: number;
  bootMethod?: string;
  cloudInit?: boolean;
  icon: string;
  nics: VMNic[];
  diskControllers: VMDiskController[];
  tags?: Record<string, string>;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [key: string]: any;
}
```

- [ ] **Step 2: Add Tags section to PropertiesPanel**

In `src/frontend/src/components/canvas/PropertiesPanel.tsx`, add a new collapsible "Tags" section after the existing sections (e.g., after the BMC section). This section renders existing tags as editable key-value rows with delete buttons, plus an "Add tag" button.

```tsx
{/* Tags Section */}
{selectedNode?.type === "vmNode" && (
  <div style={{ marginBottom: 16 }}>
    <Button
      variant="plain"
      onClick={() => setCollapsed((c) => ({ ...c, tags: !c.tags }))}
      style={{ padding: 0, marginBottom: 8 }}
    >
      {collapsed.tags ? "▶" : "▼"} Tags
    </Button>
    {!collapsed.tags && (
      <div style={{ paddingLeft: 8 }}>
        {Object.entries(nodeData.tags || {}).map(([key, value]) => (
          <div key={key} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
            <TextInput
              value={key}
              onChange={(_e, newKey) => {
                const tags = { ...(nodeData.tags || {}) };
                const val = tags[key];
                delete tags[key];
                tags[newKey] = val;
                updateNodeData(selectedNode.id, { tags });
              }}
              style={{ flex: 1 }}
              aria-label="Tag key"
            />
            <TextInput
              value={value as string}
              onChange={(_e, newVal) => {
                updateNodeData(selectedNode.id, {
                  tags: { ...(nodeData.tags || {}), [key]: newVal },
                });
              }}
              style={{ flex: 1 }}
              aria-label="Tag value"
            />
            <Button
              variant="plain"
              onClick={() => {
                const tags = { ...(nodeData.tags || {}) };
                delete tags[key];
                updateNodeData(selectedNode.id, { tags });
              }}
              aria-label="Remove tag"
              style={{ padding: 4 }}
            >
              ✕
            </Button>
          </div>
        ))}
        <Button
          variant="link"
          onClick={() => {
            const tags = { ...(nodeData.tags || {}) };
            let newKey = "NewTag";
            let i = 1;
            while (newKey in tags) { newKey = `NewTag${i++}`; }
            tags[newKey] = "";
            updateNodeData(selectedNode.id, { tags });
          }}
          style={{ padding: 0 }}
        >
          + Add tag
        </Button>
      </div>
    )}
  </div>
)}
```

Add `tags: false` to the initial `collapsed` state object (where `collapsed` is initialized).

- [ ] **Step 3: Test in browser**

Run dev server if not already running: `cd /Users/prutledg/troshka && ./dev-services.sh start`

1. Open http://localhost:3100, go to a project
2. Select a VM node on the canvas
3. In the properties panel, expand "Tags"
4. Click "+ Add tag", set key to `AnsibleGroup`, value to `bastions,showroom`
5. Verify the tag persists after deselecting and reselecting the node
6. Verify the tag appears in the project topology via API: `curl http://localhost:8200/api/v1/projects/{id} | jq '.topology.nodes[].data.tags'`


---

### Task 3: Pattern Lookup by Name

Add a `name` query parameter to `GET /api/v1/patterns` that filters to an exact match.

**Files:**
- Modify: `src/backend/app/api/patterns.py`
- Test: `src/backend/tests/test_patterns.py`

- [ ] **Step 1: Write failing test — lookup by name**

Add to `src/backend/tests/test_patterns.py`:

```python
def test_list_patterns_filter_by_name():
    client.post("/api/v1/patterns", json={
        "name": "Unique Lookup Name",
        "topology": SAMPLE_TOPOLOGY,
    }, headers=HEADERS)
    resp = client.get("/api/v1/patterns", params={"name": "Unique Lookup Name"}, headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "Unique Lookup Name"


def test_list_patterns_filter_by_name_not_found():
    resp = client.get("/api/v1/patterns", params={"name": "Nonexistent Pattern xyz"}, headers=HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_patterns.py::test_list_patterns_filter_by_name tests/test_patterns.py::test_list_patterns_filter_by_name_not_found -v`
Expected: FAIL — name parameter is ignored, returns all patterns

- [ ] **Step 3: Add name filter to list_patterns**

In `src/backend/app/api/patterns.py`, modify the `list_patterns` function signature and body:

```python
@router.get("/")
def list_patterns(
    name: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List patterns visible to the current user.
    Optional ``name`` param filters to exact name match.
    """
    if user.role == "admin":
        q = db.query(Pattern)
    else:
        shared_ids = [
            s.pattern_id
            for s in db.query(PatternShare.pattern_id).filter_by(user_id=user.id).all()
        ]
        q = db.query(Pattern).filter(
            or_(
                Pattern.owner_id == user.id,
                Pattern.id.in_(shared_ids) if shared_ids else False,
                Pattern.visibility == "public",
            )
        )

    if name is not None:
        q = q.filter(Pattern.name == name)

    patterns = q.order_by(Pattern.created_at.desc()).all()
    return [_pattern_to_list_dict(p) for p in patterns]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_patterns.py::test_list_patterns_filter_by_name tests/test_patterns.py::test_list_patterns_filter_by_name_not_found -v`
Expected: PASS


---

### Task 4: Deploy-Time Variable Injection

Extend `POST /patterns/{id}/deploy` to accept an `inject_vars` dict. These variables are merged into the bastion VM's cloud-init user-data (the VM tagged with `AnsibleGroup` containing `bastions`). If no bastion is found, inject into the first VM with cloud-init enabled.

**Files:**
- Modify: `src/backend/app/schemas/pattern.py`
- Modify: `src/backend/app/api/patterns.py`
- Test: `src/backend/tests/test_patterns.py`

- [ ] **Step 1: Write failing test — inject_vars in deploy request**

Add to `src/backend/tests/test_patterns.py`:

```python
def test_deploy_pattern_with_inject_vars():
    topo = copy.deepcopy(SAMPLE_TOPOLOGY)
    topo["nodes"][0]["data"]["tags"] = {"AnsibleGroup": "bastions"}
    topo["nodes"][0]["data"]["cloudInit"] = True
    create_resp = client.post("/api/v1/patterns", json={
        "name": "Inject Vars Test",
        "topology": topo,
    }, headers=HEADERS)
    pattern_id = create_resp.json()["id"]
    resp = client.post(f"/api/v1/patterns/{pattern_id}/deploy", json={
        "name": "Injected Deploy",
        "inject_vars": {"guid": "abc123", "student_password": "s3cret"},
    }, headers=HEADERS)
    assert resp.status_code == 201
    vm_node = [n for n in resp.json()["topology"]["nodes"] if n["type"] == "vmNode"][0]
    assert vm_node["data"].get("ciInjectVars") == {"guid": "abc123", "student_password": "s3cret"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_patterns.py::test_deploy_pattern_with_inject_vars -v`
Expected: FAIL — `inject_vars` not recognized

- [ ] **Step 3: Add inject_vars to PatternDeployRequest schema**

In `src/backend/app/schemas/pattern.py`, update `PatternDeployRequest`:

```python
class PatternDeployRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    guid: str | None = None
    domain: str | None = None
    dns_provider_id: str | None = None
    auto_deploy: bool = True
    auto_start: bool = True
    inject_vars: dict | None = None
```

- [ ] **Step 4: Handle inject_vars in deploy_pattern**

In `src/backend/app/api/patterns.py`, in the `deploy_pattern` function (after `_remap_topology` is called), add logic to merge inject_vars into the bastion VM's cloud-init data. Find the section where the project is created from the remapped topology and add:

```python
    if body.inject_vars:
        nodes = new_topology.get("nodes", [])
        target_vm = None
        for n in nodes:
            if n.get("type") == "vmNode":
                tags = n.get("data", {}).get("tags", {})
                groups = tags.get("AnsibleGroup", "")
                if "bastions" in [g.strip() for g in groups.split(",")]:
                    target_vm = n
                    break
        if target_vm is None:
            for n in nodes:
                if n.get("type") == "vmNode" and n.get("data", {}).get("cloudInit"):
                    target_vm = n
                    break
        if target_vm is not None:
            target_vm["data"]["ciInjectVars"] = body.inject_vars
```

Place this after the `new_topology = _remap_topology(...)` call and before the project creation.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_patterns.py::test_deploy_pattern_with_inject_vars -v`
Expected: PASS

- [ ] **Step 6: Run full test suite to verify no regressions**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_patterns.py -v`
Expected: All tests PASS


---

### Task 5: Student Portal — Backend Model and Token API

Create the `ProjectPortalToken` model and API endpoints for creating/validating portal tokens.

**Files:**
- Create: `src/backend/app/models/portal.py`
- Modify: `src/backend/app/models/__init__.py`
- Create: `src/backend/app/api/portal.py`
- Modify: `src/backend/app/main.py`
- Create: `src/backend/tests/test_portal.py`

- [ ] **Step 1: Write failing tests for portal token creation and retrieval**

Create `src/backend/tests/test_portal.py`:

```python
import copy

from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

_db = TestSession()
_user = User(email="portal-test@example.com", display_name="Portal Tester", role="user",
             auth_source="local", password_hash=hash_password("pass"))
_db.add(_user)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
USER_ID = _user.id
_db.close()

HEADERS = {"Authorization": f"Bearer {TOKEN}"}

SAMPLE_TOPOLOGY = {
    "nodes": [
        {"id": "vm-1", "type": "vmNode", "position": {"x": 0, "y": 0},
         "data": {"name": "bastion", "vcpus": 2, "ram": 4096,
                  "nics": [{"id": "nic-1", "name": "eth0", "mac": "52:54:00:aa:bb:cc", "model": "virtio"}],
                  "diskControllers": [{"id": "dp-1", "name": "disk0", "bus": "virtio"}]}},
    ],
    "edges": [],
}


def _create_project():
    resp = client.post("/api/v1/projects", json={
        "name": "Portal Test Project",
        "topology": SAMPLE_TOPOLOGY,
    }, headers=HEADERS)
    return resp.json()["id"]


def test_create_portal_token():
    project_id = _create_project()
    resp = client.post(f"/api/v1/projects/{project_id}/portal-token", json={
        "access_level": "console",
    }, headers=HEADERS)
    assert resp.status_code == 201
    data = resp.json()
    assert "token" in data
    assert "portal_url" in data
    assert data["access_level"] == "console"


def test_get_portal_view_unauthenticated():
    project_id = _create_project()
    token_resp = client.post(f"/api/v1/projects/{project_id}/portal-token", json={
        "access_level": "readonly",
    }, headers=HEADERS)
    token = token_resp.json()["token"]
    resp = client.get(f"/api/v1/portal/{token}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == project_id
    assert data["access_level"] == "readonly"
    assert "topology" in data


def test_get_portal_invalid_token():
    resp = client.get("/api/v1/portal/nonexistent-token-xyz")
    assert resp.status_code == 404


def test_portal_token_deleted_with_project():
    project_id = _create_project()
    token_resp = client.post(f"/api/v1/projects/{project_id}/portal-token", json={
        "access_level": "power",
    }, headers=HEADERS)
    token = token_resp.json()["token"]
    client.delete(f"/api/v1/projects/{project_id}", headers=HEADERS)
    resp = client.get(f"/api/v1/portal/{token}")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_portal.py -v`
Expected: FAIL — ImportError or 404 (model/routes don't exist)

- [ ] **Step 3: Create the ProjectPortalToken model**

Create `src/backend/app/models/portal.py`:

```python
import secrets
import uuid

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class ProjectPortalToken(Base):
    __tablename__ = "project_portal_tokens"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    token: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False,
        default=lambda: secrets.token_urlsafe(32),
    )
    access_level: Mapped[str] = mapped_column(
        String(20), nullable=False, default="readonly"
    )
    expires_at: Mapped[str | None] = mapped_column(DateTime, nullable=True)

    project = relationship("Project", backref="portal_tokens")
```

- [ ] **Step 4: Register model in __init__.py**

In `src/backend/app/models/__init__.py`, add:

```python
from app.models.portal import ProjectPortalToken
```

And add `"ProjectPortalToken"` to the `__all__` list.

- [ ] **Step 5: Create the portal API routes**

Create `src/backend/app/api/portal.py`:

```python
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.portal import ProjectPortalToken
from app.models.project import Project
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(tags=["portal"])


class PortalTokenRequest(BaseModel):
    access_level: str = "readonly"
    expires_at: str | None = None


@router.post("/projects/{project_id}/portal-token", status_code=201)
def create_portal_token(
    project_id: str,
    body: PortalTokenRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(403, "Not authorized")
    if body.access_level not in ("readonly", "power", "console", "manage"):
        raise HTTPException(400, f"Invalid access level: {body.access_level}")

    portal_token = ProjectPortalToken(
        project_id=project_id,
        access_level=body.access_level,
    )
    db.add(portal_token)
    db.commit()
    db.refresh(portal_token)

    base_url = str(request.base_url).rstrip("/")
    return {
        "token": portal_token.token,
        "access_level": portal_token.access_level,
        "portal_url": f"{base_url}/portal/{portal_token.token}",
    }


@router.get("/portal/{token}")
def get_portal(
    token: str,
    db: Session = Depends(get_db),
):
    """Public endpoint — no authentication required. Token is the auth."""
    portal_token = db.query(ProjectPortalToken).filter_by(token=token).first()
    if not portal_token:
        raise HTTPException(404, "Invalid or expired portal token")

    project = db.query(Project).filter_by(id=portal_token.project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")

    topology = project.topology or {}
    hidden = set(topology.get("hiddenNodeIds", []))
    if hidden:
        topology = {
            **topology,
            "nodes": [n for n in topology.get("nodes", []) if n["id"] not in hidden],
            "edges": [
                e for e in topology.get("edges", [])
                if e.get("source") not in hidden and e.get("target") not in hidden
            ],
        }

    return {
        "project_id": project.id,
        "project_name": project.name,
        "project_state": project.state,
        "access_level": portal_token.access_level,
        "topology": topology,
    }
```

- [ ] **Step 6: Register portal router in main.py**

In `src/backend/app/main.py`, add the import and registration:

```python
from app.api import portal as portal_routes
```

And register it:

```python
app.include_router(portal_routes.router, prefix="/api/v1")
```

- [ ] **Step 7: Create database migration**

Run:
```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m alembic revision -m "add project_portal_tokens table"
```

Edit the generated migration file to add:

```python
def upgrade():
    op.create_table(
        "project_portal_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token", sa.String(64), unique=True, index=True, nullable=False),
        sa.Column("access_level", sa.String(20), nullable=False, server_default="readonly"),
        sa.Column("expires_at", sa.DateTime, nullable=True),
    )


def downgrade():
    op.drop_table("project_portal_tokens")
```

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m alembic upgrade head`

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_portal.py -v`
Expected: All PASS


---

### Task 6: Student Portal — Frontend View

Create a new `/portal/{token}` page that shows a stripped-down canvas (read-only topology, power controls, VNC console). Uses the same bare layout pattern as `/console`.

**Files:**
- Create: `src/frontend/src/app/portal/[token]/page.tsx`
- Modify: `src/frontend/src/app/layout.tsx` (add portal to bare layout check)

- [ ] **Step 1: Add portal to bare layout bypass in layout.tsx**

In `src/frontend/src/app/layout.tsx`, find the `isConsolePage` check (around line 85) and extend it:

```typescript
const isConsolePage = pathname?.startsWith("/console");
const isPortalPage = pathname?.startsWith("/portal");
if (isConsolePage || isPortalPage) {
  return (
    <html lang="en">
      <head><title>{isPortalPage ? "Lab Portal" : "Console"}</title></head>
      <body style={{ margin: 0, padding: 0, overflow: "hidden" }}>{children}</body>
    </html>
  );
}
```

- [ ] **Step 2: Create the portal page**

Create `src/frontend/src/app/portal/[token]/page.tsx`:

```tsx
"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  Alert,
  Button,
  Masthead,
  MastheadContent,
  MastheadMain,
  Page,
  PageSection,
  Spinner,
  Title,
  Toolbar,
  ToolbarContent,
  ToolbarItem,
} from "@patternfly/react-core";

interface PortalData {
  project_id: string;
  project_name: string;
  project_state: string;
  access_level: string;
  topology: {
    nodes: any[];
    edges: any[];
  };
}

export default function PortalPage() {
  const { token } = useParams<{ token: string }>();
  const [data, setData] = useState<PortalData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchPortal = useCallback(async () => {
    try {
      const resp = await fetch(`/api/v1/portal/${token}`);
      if (!resp.ok) {
        if (resp.status === 404) {
          setError("This portal link is invalid or has expired.");
        } else {
          setError("Failed to load portal.");
        }
        return;
      }
      setData(await resp.json());
    } catch {
      setError("Failed to connect to server.");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    fetchPortal();
    const interval = setInterval(fetchPortal, 10000);
    return () => clearInterval(interval);
  }, [fetchPortal]);

  if (loading) {
    return (
      <div style={{ display: "flex", justifyContent: "center", alignItems: "center", height: "100vh" }}>
        <Spinner size="xl" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div style={{ display: "flex", justifyContent: "center", alignItems: "center", height: "100vh" }}>
        <Alert variant="danger" title={error || "Portal not available"} />
      </div>
    );
  }

  const canPower = ["power", "console", "manage"].includes(data.access_level);
  const canConsole = ["console", "manage"].includes(data.access_level);

  const handleVmAction = async (vmId: string, action: string) => {
    await fetch(`/api/v1/portal/${token}/vms/${vmId}/${action}`, { method: "POST" });
    setTimeout(fetchPortal, 1000);
  };

  const openConsole = (vmId: string, vmName: string) => {
    window.open(
      `/console?vm=${vmId}&project=${data.project_id}&name=${encodeURIComponent(vmName)}&portal=${token}`,
      `console-${vmId}`,
      "width=1024,height=768"
    );
  };

  const nodes = (data.topology.nodes || []).map((n: any) => ({
    ...n,
    draggable: false,
    selectable: false,
    connectable: false,
  }));

  return (
    <div style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
      <Masthead>
        <MastheadMain>
          <Title headingLevel="h1" size="lg" style={{ color: "white", padding: "0 16px" }}>
            {data.project_name}
          </Title>
        </MastheadMain>
        <MastheadContent>
          <Toolbar>
            <ToolbarContent>
              <ToolbarItem>
                <span style={{ color: "var(--pf-t--global--color--status--info--default)" }}>
                  {data.project_state}
                </span>
              </ToolbarItem>
            </ToolbarContent>
          </Toolbar>
        </MastheadContent>
      </Masthead>
      <div style={{ flex: 1 }}>
        <ReactFlowProvider>
          <ReactFlow
            nodes={nodes}
            edges={data.topology.edges || []}
            fitView
            nodesDraggable={false}
            nodesConnectable={false}
            elementsSelectable={false}
            panOnDrag
            zoomOnScroll
          >
            <Background />
            <Controls showInteractive={false} />
            <MiniMap />
          </ReactFlow>
        </ReactFlowProvider>
      </div>
    </div>
  );
}
```

This is a minimal starting point. The VM power controls and console buttons will be added to the node rendering in a future iteration — for now the page shows the read-only topology. The full node types (`vmNode`, `networkNode`, `storageNode`) with power/console controls would be imported from the existing canvas components and wrapped with access-level checks.

- [ ] **Step 3: Test in browser**

1. Create a project and a portal token via API:
   ```bash
   curl -X POST http://localhost:8200/api/v1/projects/{id}/portal-token \
     -H "Content-Type: application/json" \
     -d '{"access_level": "console"}'
   ```
2. Open the returned `portal_url` in a browser (no login required)
3. Verify the topology is visible, nodes are not draggable
4. Verify the page title shows the project name


---

### Task 7: Portal VM Actions — Backend

Add portal-scoped VM action endpoints that validate the token and check the access level before forwarding to the existing VM action logic.

**Files:**
- Modify: `src/backend/app/api/portal.py`
- Modify: `src/backend/tests/test_portal.py`

- [ ] **Step 1: Write failing test — portal VM power action**

Add to `src/backend/tests/test_portal.py`:

```python
def test_portal_vm_action_requires_power_level():
    project_id = _create_project()
    token_resp = client.post(f"/api/v1/projects/{project_id}/portal-token", json={
        "access_level": "readonly",
    }, headers=HEADERS)
    token = token_resp.json()["token"]
    resp = client.post(f"/api/v1/portal/{token}/vms/vm-1/stop")
    assert resp.status_code == 403


def test_portal_vm_action_allowed_with_power():
    project_id = _create_project()
    token_resp = client.post(f"/api/v1/projects/{project_id}/portal-token", json={
        "access_level": "power",
    }, headers=HEADERS)
    token = token_resp.json()["token"]
    resp = client.post(f"/api/v1/portal/{token}/vms/vm-1/stop")
    # Will be 400/404 because project isn't deployed, but NOT 403
    assert resp.status_code != 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_portal.py::test_portal_vm_action_requires_power_level tests/test_portal.py::test_portal_vm_action_allowed_with_power -v`
Expected: FAIL — routes don't exist

- [ ] **Step 3: Add portal VM action endpoints**

Add to `src/backend/app/api/portal.py`:

```python
ACCESS_LEVELS = {"readonly": 0, "power": 1, "console": 2, "manage": 3}
POWER_ACTIONS = {"start", "stop", "restart", "forcestop"}


def _get_portal_token(token: str, db: Session, min_level: str = "readonly") -> tuple[ProjectPortalToken, Project]:
    portal_token = db.query(ProjectPortalToken).filter_by(token=token).first()
    if not portal_token:
        raise HTTPException(404, "Invalid or expired portal token")
    if ACCESS_LEVELS.get(portal_token.access_level, 0) < ACCESS_LEVELS.get(min_level, 0):
        raise HTTPException(403, f"Access level '{portal_token.access_level}' insufficient, requires '{min_level}'")
    project = db.query(Project).filter_by(id=portal_token.project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")
    return portal_token, project


@router.post("/portal/{token}/vms/{vm_id}/{action}")
def portal_vm_action(
    token: str,
    vm_id: str,
    action: str,
    db: Session = Depends(get_db),
):
    if action not in POWER_ACTIONS:
        raise HTTPException(400, f"Unknown action: {action}")
    portal_token, project = _get_portal_token(token, db, min_level="power")
    if project.state not in ("active", "stopped"):
        raise HTTPException(400, f"Project is {project.state}, cannot perform VM actions")

    from app.services import vm_service
    try:
        result = vm_service.vm_action(project, vm_id, action, db)
        return result
    except Exception as e:
        raise HTTPException(400, str(e))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_portal.py -v`
Expected: All PASS


---

### Task 8: Template YAML Definitions

Extract the hardcoded OCP template topology generation into YAML-driven template definitions.

**Files:**
- Create: `src/backend/templates/ocp-cluster.yaml`
- Create: `src/backend/templates/ocp-sno.yaml`
- Create: `src/backend/templates/ocp-compact.yaml`
- Create: `src/backend/templates/ocp-standard.yaml`
- Create: `src/backend/app/services/template_loader.py`
- Create: `src/backend/tests/test_template_loader.py`

- [ ] **Step 1: Create base template YAML**

Create `src/backend/templates/ocp-cluster.yaml`:

```yaml
name: ocp-cluster
description: OpenShift cluster (configurable topology)
versions: ["4.14", "4.15", "4.16", "4.17"]
parameters:
  control_count:
    default: 3
    min: 1
    description: Number of control plane nodes
  control_vcpus:
    default: 4
    min: 4
  control_ram_gb:
    default: 16
    min: 16
  control_disk_gb:
    default: 120
    min: 100
  control_schedulable:
    default: true
    description: Whether control plane nodes accept workload scheduling
  worker_count:
    default: 0
    min: 0
  worker_vcpus:
    default: 4
    min: 4
  worker_ram_gb:
    default: 16
    min: 16
  worker_disk_gb:
    default: 120
    min: 100
bastion:
  vcpus: 2
  ram_gb: 4
  disk_gb: 50
  image: rhel-10
networks:
  cluster:
    cidr: 10.0.0.0/24
    dhcp: true
  bmc:
    cidr: 192.168.100.0/24
```

- [ ] **Step 2: Create preset templates**

Create `src/backend/templates/ocp-sno.yaml`:

```yaml
name: ocp-sno
description: Single Node OpenShift
extends: ocp-cluster
defaults:
  control_count: 1
  control_vcpus: 16
  control_ram_gb: 64
  control_disk_gb: 120
  control_schedulable: true
  worker_count: 0
```

Create `src/backend/templates/ocp-compact.yaml`:

```yaml
name: ocp-compact
description: Compact cluster (3 schedulable control plane nodes)
extends: ocp-cluster
defaults:
  control_count: 3
  control_vcpus: 4
  control_ram_gb: 16
  control_disk_gb: 120
  control_schedulable: true
  worker_count: 0
```

Create `src/backend/templates/ocp-standard.yaml`:

```yaml
name: ocp-standard
description: Standard cluster (3 CP + 2 workers)
extends: ocp-cluster
defaults:
  control_count: 3
  control_vcpus: 4
  control_ram_gb: 16
  control_disk_gb: 120
  control_schedulable: false
  worker_count: 2
  worker_vcpus: 4
  worker_ram_gb: 16
  worker_disk_gb: 120
```

- [ ] **Step 3: Write failing tests for template loader**

Create `src/backend/tests/test_template_loader.py`:

```python
import os
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "templates")


def test_load_base_template():
    from app.services.template_loader import load_template
    tmpl = load_template("ocp-cluster", templates_dir=TEMPLATES_DIR)
    assert tmpl["name"] == "ocp-cluster"
    assert "parameters" in tmpl
    assert "control_count" in tmpl["parameters"]


def test_load_preset_template():
    from app.services.template_loader import load_template
    tmpl = load_template("ocp-compact", templates_dir=TEMPLATES_DIR)
    assert tmpl["name"] == "ocp-compact"
    assert tmpl["extends"] == "ocp-cluster"


def test_resolve_preset_parameters():
    from app.services.template_loader import resolve_template
    resolved = resolve_template("ocp-compact", overrides={}, templates_dir=TEMPLATES_DIR)
    assert resolved["control_count"] == 3
    assert resolved["control_schedulable"] is True
    assert resolved["worker_count"] == 0
    assert "parameters" in resolved  # has full parameter definitions from base


def test_resolve_with_overrides():
    from app.services.template_loader import resolve_template
    resolved = resolve_template("ocp-compact", overrides={"worker_count": 2, "control_ram_gb": 32}, templates_dir=TEMPLATES_DIR)
    assert resolved["worker_count"] == 2
    assert resolved["control_ram_gb"] == 32


def test_resolve_rejects_unknown_override():
    from app.services.template_loader import resolve_template
    with pytest.raises(ValueError, match="Unknown parameter"):
        resolve_template("ocp-compact", overrides={"fake_param": 99}, templates_dir=TEMPLATES_DIR)


def test_resolve_rejects_below_minimum():
    from app.services.template_loader import resolve_template
    with pytest.raises(ValueError, match="below minimum"):
        resolve_template("ocp-compact", overrides={"control_vcpus": 1}, templates_dir=TEMPLATES_DIR)


def test_validate_version():
    from app.services.template_loader import resolve_template
    resolved = resolve_template("ocp-compact", overrides={}, version="4.16", templates_dir=TEMPLATES_DIR)
    assert resolved["version"] == "4.16"


def test_validate_version_rejects_invalid():
    from app.services.template_loader import resolve_template
    with pytest.raises(ValueError, match="not available"):
        resolve_template("ocp-compact", overrides={}, version="3.11", templates_dir=TEMPLATES_DIR)


def test_load_nonexistent_template():
    from app.services.template_loader import load_template
    with pytest.raises(FileNotFoundError):
        load_template("nonexistent", templates_dir=TEMPLATES_DIR)
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_template_loader.py -v`
Expected: FAIL — module doesn't exist

- [ ] **Step 5: Implement template_loader service**

Create `src/backend/app/services/template_loader.py`:

```python
import os
from pathlib import Path

import yaml

_DEFAULT_TEMPLATES_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "templates"
)


def load_template(name: str, templates_dir: str = _DEFAULT_TEMPLATES_DIR) -> dict:
    path = Path(templates_dir) / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Template '{name}' not found at {path}")
    with open(path) as f:
        return yaml.safe_load(f)


def resolve_template(
    name: str,
    overrides: dict | None = None,
    version: str | None = None,
    templates_dir: str = _DEFAULT_TEMPLATES_DIR,
) -> dict:
    tmpl = load_template(name, templates_dir)
    overrides = overrides or {}

    base_params = {}
    if tmpl.get("extends"):
        base = load_template(tmpl["extends"], templates_dir)
        base_params = base.get("parameters", {})
    else:
        base_params = tmpl.get("parameters", {})

    preset_defaults = tmpl.get("defaults", {})

    resolved = {}
    for param_name, param_def in base_params.items():
        if param_name in overrides:
            value = overrides[param_name]
        elif param_name in preset_defaults:
            value = preset_defaults[param_name]
        else:
            value = param_def["default"]
        resolved[param_name] = value

    unknown = set(overrides.keys()) - set(base_params.keys())
    if unknown:
        raise ValueError(f"Unknown parameter(s): {', '.join(sorted(unknown))}")

    for param_name, value in resolved.items():
        param_def = base_params[param_name]
        if "min" in param_def and isinstance(value, (int, float)):
            if value < param_def["min"]:
                raise ValueError(
                    f"Parameter '{param_name}' value {value} is below minimum {param_def['min']}"
                )

    base_for_versions = load_template(tmpl.get("extends", name), templates_dir)
    versions = base_for_versions.get("versions", [])
    if version is not None:
        if version not in versions:
            raise ValueError(f"Version '{version}' not available. Options: {versions}")
        resolved["version"] = version

    resolved["parameters"] = base_params
    resolved["name"] = tmpl["name"]
    resolved["description"] = tmpl.get("description", "")
    resolved["bastion"] = base_for_versions.get("bastion", {})
    resolved["networks"] = base_for_versions.get("networks", {})

    return resolved
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_template_loader.py -v`
Expected: All PASS


---

### Task 9: Deploy Template API Endpoint

Add `POST /api/v1/deploy-template` that accepts a template name, version, and overrides, resolves the template, generates a topology, and creates a project.

**Files:**
- Modify: `src/backend/app/api/portal.py` (or create new `src/backend/app/api/templates.py`)
- Create: `src/backend/app/api/templates.py`
- Modify: `src/backend/app/main.py`
- Create: `src/backend/tests/test_deploy_template.py`

- [ ] **Step 1: Write failing test**

Create `src/backend/tests/test_deploy_template.py`:

```python
from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

_db = TestSession()
_user = User(email="template-test@example.com", display_name="Template Tester", role="user",
             auth_source="local", password_hash=hash_password("pass"))
_db.add(_user)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
_db.close()

HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def test_deploy_template_creates_project():
    resp = client.post("/api/v1/deploy-template", json={
        "template": "ocp-compact",
        "version": "4.16",
        "name": "My OCP Cluster",
    }, headers=HEADERS)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My OCP Cluster"
    assert data["state"] == "draft"
    assert "topology" in data
    nodes = data["topology"]["nodes"]
    vm_nodes = [n for n in nodes if n["type"] == "vmNode"]
    assert len(vm_nodes) >= 3  # 3 CP + bastion


def test_deploy_template_with_overrides():
    resp = client.post("/api/v1/deploy-template", json={
        "template": "ocp-compact",
        "version": "4.16",
        "name": "Custom OCP",
        "overrides": {"control_ram_gb": 32, "worker_count": 2},
    }, headers=HEADERS)
    assert resp.status_code == 201
    nodes = resp.json()["topology"]["nodes"]
    vm_nodes = [n for n in nodes if n["type"] == "vmNode"]
    # 3 CP + 2 workers + bastion = 6
    assert len(vm_nodes) == 6


def test_deploy_template_rejects_invalid_template():
    resp = client.post("/api/v1/deploy-template", json={
        "template": "nonexistent",
        "version": "4.16",
        "name": "Bad Template",
    }, headers=HEADERS)
    assert resp.status_code == 400


def test_deploy_template_rejects_invalid_version():
    resp = client.post("/api/v1/deploy-template", json={
        "template": "ocp-compact",
        "version": "3.11",
        "name": "Bad Version",
    }, headers=HEADERS)
    assert resp.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_deploy_template.py -v`
Expected: FAIL — 404

- [ ] **Step 3: Create template topology generator**

This function takes a resolved template and generates a topology JSONB dict. It reuses patterns from the existing `topology_templates.py` but is driven by YAML data instead of hardcoded values.

Add to `src/backend/app/services/template_loader.py`:

```python
import uuid

from app.api.patterns import _generate_mac


def generate_topology_from_template(resolved: dict) -> dict:
    nodes = []
    edges = []

    x_spacing = 400
    cp_y = 350
    worker_y = 720
    bastion_x_offset = 150

    cluster_net_id = f"net-{uuid.uuid4()}"
    bmc_net_id = f"net-{uuid.uuid4()}"
    cluster_net = resolved["networks"].get("cluster", {})
    bmc_net = resolved["networks"].get("bmc", {})

    nodes.append({
        "id": cluster_net_id,
        "type": "networkNode",
        "position": {"x": 600, "y": 100},
        "data": {
            "name": "cluster",
            "label": "cluster",
            "cidr": cluster_net.get("cidr", "10.0.0.0/24"),
            "dhcp": cluster_net.get("dhcp", True),
            "icon": "\U0001F310",
        },
    })
    nodes.append({
        "id": bmc_net_id,
        "type": "networkNode",
        "position": {"x": 600, "y": 900},
        "data": {
            "name": "bmc",
            "label": "bmc",
            "cidr": bmc_net.get("cidr", "192.168.100.0/24"),
            "dhcp": False,
            "networkType": "bmc",
            "icon": "\U0001F310",
        },
    })

    control_count = resolved.get("control_count", 3)
    worker_count = resolved.get("worker_count", 0)
    bastion_cfg = resolved.get("bastion", {})

    bastion_id = f"vm-{uuid.uuid4()}"
    nic_id = f"nic-{uuid.uuid4()}"
    dp_id = f"dp-{uuid.uuid4()}"
    total_cols = control_count + (1 if worker_count == 0 else 0)
    bastion_x = bastion_x_offset + total_cols * x_spacing

    nodes.append({
        "id": bastion_id,
        "type": "vmNode",
        "position": {"x": bastion_x, "y": cp_y},
        "data": {
            "name": "bastion",
            "label": "bastion",
            "vcpus": bastion_cfg.get("vcpus", 2),
            "ram": bastion_cfg.get("ram_gb", 4),
            "os": bastion_cfg.get("image", "rhel-10"),
            "icon": "\U0001F5A5",
            "firmware": "uefi",
            "powerOnAtDeploy": True,
            "bootMethod": "disk",
            "nics": [{"id": nic_id, "name": "eth0", "mac": _generate_mac(), "model": "virtio"}],
            "diskControllers": [{"id": dp_id, "name": "disk0", "bus": "virtio"}],
            "tags": {"AnsibleGroup": "bastions,showroom"},
        },
    })
    edges.append({"id": f"e-{uuid.uuid4()}", "source": bastion_id, "target": cluster_net_id,
                  "sourceHandle": f"nic-{nic_id}-bottom", "targetHandle": f"port-{cluster_net_id}-top"})

    for i in range(control_count):
        vm_id = f"vm-{uuid.uuid4()}"
        nic_cluster_id = f"nic-{uuid.uuid4()}"
        nic_bmc_id = f"nic-{uuid.uuid4()}"
        dp_id = f"dp-{uuid.uuid4()}"
        nodes.append({
            "id": vm_id,
            "type": "vmNode",
            "position": {"x": bastion_x_offset + i * x_spacing, "y": cp_y},
            "data": {
                "name": f"cp-{i}",
                "label": f"cp-{i}",
                "vcpus": resolved.get("control_vcpus", 4),
                "ram": resolved.get("control_ram_gb", 16),
                "os": "rhcos",
                "icon": "\U0001F5A5",
                "firmware": "uefi",
                "powerOnAtDeploy": True,
                "bootMethod": "disk",
                "bmcEnabled": True,
                "nics": [
                    {"id": nic_cluster_id, "name": "eth0", "mac": _generate_mac(), "model": "virtio"},
                    {"id": nic_bmc_id, "name": "eth1", "mac": _generate_mac(), "model": "virtio"},
                ],
                "diskControllers": [{"id": dp_id, "name": "disk0", "bus": "virtio"}],
                "tags": {"AnsibleGroup": "controllers"},
            },
        })
        edges.append({"id": f"e-{uuid.uuid4()}", "source": vm_id, "target": cluster_net_id,
                      "sourceHandle": f"nic-{nic_cluster_id}-bottom", "targetHandle": f"port-{cluster_net_id}-top"})
        edges.append({"id": f"e-{uuid.uuid4()}", "source": vm_id, "target": bmc_net_id,
                      "sourceHandle": f"nic-{nic_bmc_id}-bottom", "targetHandle": f"port-{bmc_net_id}-top"})

    for i in range(worker_count):
        vm_id = f"vm-{uuid.uuid4()}"
        nic_cluster_id = f"nic-{uuid.uuid4()}"
        nic_bmc_id = f"nic-{uuid.uuid4()}"
        dp_id = f"dp-{uuid.uuid4()}"
        nodes.append({
            "id": vm_id,
            "type": "vmNode",
            "position": {"x": bastion_x_offset + i * x_spacing, "y": worker_y},
            "data": {
                "name": f"worker-{i}",
                "label": f"worker-{i}",
                "vcpus": resolved.get("worker_vcpus", 4),
                "ram": resolved.get("worker_ram_gb", 16),
                "os": "rhcos",
                "icon": "\U0001F5A5",
                "firmware": "uefi",
                "powerOnAtDeploy": True,
                "bootMethod": "disk",
                "bmcEnabled": True,
                "nics": [
                    {"id": nic_cluster_id, "name": "eth0", "mac": _generate_mac(), "model": "virtio"},
                    {"id": nic_bmc_id, "name": "eth1", "mac": _generate_mac(), "model": "virtio"},
                ],
                "diskControllers": [{"id": dp_id, "name": "disk0", "bus": "virtio"}],
                "tags": {"AnsibleGroup": "workers"},
            },
        })
        edges.append({"id": f"e-{uuid.uuid4()}", "source": vm_id, "target": cluster_net_id,
                      "sourceHandle": f"nic-{nic_cluster_id}-bottom", "targetHandle": f"port-{cluster_net_id}-top"})
        edges.append({"id": f"e-{uuid.uuid4()}", "source": vm_id, "target": bmc_net_id,
                      "sourceHandle": f"nic-{nic_bmc_id}-bottom", "targetHandle": f"port-{bmc_net_id}-top"})

    return {"nodes": nodes, "edges": edges}
```

- [ ] **Step 4: Create templates API route**

Create `src/backend/app/api/templates.py`:

```python
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.project import Project
from app.models.user import User
from app.services.template_loader import (
    generate_topology_from_template,
    resolve_template,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["templates"])


class DeployTemplateRequest(BaseModel):
    template: str
    version: str
    name: str
    description: str | None = None
    overrides: dict | None = None
    auto_deploy: bool = False
    auto_start: bool = True


@router.post("/deploy-template", status_code=201)
def deploy_template(
    body: DeployTemplateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        resolved = resolve_template(body.template, overrides=body.overrides, version=body.version)
    except FileNotFoundError:
        raise HTTPException(400, f"Unknown template: {body.template}")
    except ValueError as e:
        raise HTTPException(400, str(e))

    topology = generate_topology_from_template(resolved)

    existing = db.query(Project).filter_by(owner_id=user.id, name=body.name).first()
    if existing:
        raise HTTPException(409, f"Project named '{body.name}' already exists")

    project = Project(
        name=body.name,
        description=body.description,
        owner_id=user.id,
        topology=topology,
        state="draft",
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    return {
        "id": project.id,
        "name": project.name,
        "state": project.state,
        "topology": project.topology,
    }
```

- [ ] **Step 5: Register templates router in main.py**

In `src/backend/app/main.py`:

```python
from app.api import templates as template_routes
```

```python
app.include_router(template_routes.router, prefix="/api/v1")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_deploy_template.py -v`
Expected: All PASS

- [ ] **Step 7: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All PASS


---

### Task 10: Final Integration Test

Verify the full flow works end-to-end: create pattern with tags → deploy with inject_vars → create portal token → access portal.

**Files:**
- Create: `src/backend/tests/test_integration_agnosticd.py`

- [ ] **Step 1: Write integration test**

Create `src/backend/tests/test_integration_agnosticd.py`:

```python
import copy

from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

_db = TestSession()
_user = User(email="integration-test@example.com", display_name="Integration", role="user",
             auth_source="local", password_hash=hash_password("pass"))
_db.add(_user)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
_db.close()

HEADERS = {"Authorization": f"Bearer {TOKEN}"}

TOPOLOGY = {
    "nodes": [
        {"id": "vm-bastion", "type": "vmNode", "position": {"x": 0, "y": 0},
         "data": {"name": "bastion", "vcpus": 2, "ram": 4, "os": "rhel-10", "cloudInit": True,
                  "tags": {"AnsibleGroup": "bastions,showroom"},
                  "nics": [{"id": "nic-1", "name": "eth0", "mac": "52:54:00:aa:bb:cc", "model": "virtio"}],
                  "diskControllers": [{"id": "dp-1", "name": "disk0", "bus": "virtio"}]}},
        {"id": "vm-cp0", "type": "vmNode", "position": {"x": 400, "y": 0},
         "data": {"name": "cp-0", "vcpus": 4, "ram": 16, "os": "rhcos",
                  "tags": {"AnsibleGroup": "controllers"},
                  "nics": [{"id": "nic-2", "name": "eth0", "mac": "52:54:00:dd:ee:ff", "model": "virtio"}],
                  "diskControllers": [{"id": "dp-2", "name": "disk0", "bus": "virtio"}]}},
        {"id": "net-1", "type": "networkNode", "position": {"x": 200, "y": 200},
         "data": {"name": "cluster", "cidr": "10.0.0.0/24"}},
    ],
    "edges": [
        {"id": "e1", "source": "vm-bastion", "target": "net-1"},
        {"id": "e2", "source": "vm-cp0", "target": "net-1"},
    ],
}


def test_full_agnosticd_flow():
    # 1. Create pattern with tags
    create_resp = client.post("/api/v1/patterns", json={
        "name": "Integration Test Pattern",
        "topology": TOPOLOGY,
        "visibility": "public",
    }, headers=HEADERS)
    assert create_resp.status_code == 201
    pattern_id = create_resp.json()["id"]

    # 2. Look up pattern by name
    lookup_resp = client.get("/api/v1/patterns", params={"name": "Integration Test Pattern"}, headers=HEADERS)
    assert lookup_resp.status_code == 200
    assert len(lookup_resp.json()) == 1
    assert lookup_resp.json()[0]["id"] == pattern_id

    # 3. Deploy with inject_vars
    deploy_resp = client.post(f"/api/v1/patterns/{pattern_id}/deploy", json={
        "name": "Student Lab abc123",
        "inject_vars": {"guid": "abc123", "student_password": "hunter2"},
    }, headers=HEADERS)
    assert deploy_resp.status_code == 201
    project_id = deploy_resp.json()["id"]
    project_topo = deploy_resp.json()["topology"]

    # Verify tags preserved and inject_vars applied
    bastion = [n for n in project_topo["nodes"]
               if n["type"] == "vmNode" and "bastions" in n["data"].get("tags", {}).get("AnsibleGroup", "")][0]
    assert bastion["data"]["ciInjectVars"]["guid"] == "abc123"

    controllers = [n for n in project_topo["nodes"]
                   if n["type"] == "vmNode" and "controllers" in n["data"].get("tags", {}).get("AnsibleGroup", "")]
    assert len(controllers) == 1

    # 4. Create portal token
    token_resp = client.post(f"/api/v1/projects/{project_id}/portal-token", json={
        "access_level": "console",
    }, headers=HEADERS)
    assert token_resp.status_code == 201
    portal_token = token_resp.json()["token"]
    assert "portal_url" in token_resp.json()

    # 5. Access portal (no auth)
    portal_resp = client.get(f"/api/v1/portal/{portal_token}")
    assert portal_resp.status_code == 200
    assert portal_resp.json()["project_id"] == project_id
    assert portal_resp.json()["access_level"] == "console"

    # 6. Delete project → token invalidated
    client.delete(f"/api/v1/projects/{project_id}", headers=HEADERS)
    portal_resp2 = client.get(f"/api/v1/portal/{portal_token}")
    assert portal_resp2.status_code == 404
```

- [ ] **Step 2: Run integration test**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_integration_agnosticd.py -v`
Expected: PASS

- [ ] **Step 3: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 4: Commit all changes**

```bash
cd /Users/prutledg/troshka && git add \
  src/backend/tests/test_patterns.py \
  src/frontend/src/stores/canvasStore.ts \
  src/frontend/src/components/canvas/PropertiesPanel.tsx \
  src/backend/app/api/patterns.py \
  src/backend/app/schemas/pattern.py \
  src/backend/app/models/portal.py \
  src/backend/app/models/__init__.py \
  src/backend/app/api/portal.py \
  src/backend/app/api/templates.py \
  src/backend/app/services/template_loader.py \
  src/backend/app/main.py \
  src/backend/alembic/versions/ \
  src/backend/templates/ \
  src/backend/tests/test_portal.py \
  src/backend/tests/test_template_loader.py \
  src/backend/tests/test_deploy_template.py \
  src/backend/tests/test_integration_agnosticd.py \
  src/frontend/src/app/portal/ \
  src/frontend/src/app/layout.tsx
git commit -m "feat: add agnosticd cloud provider API support

- VM node tags in topology JSONB with canvas tag editor
- Student portal: token model, API endpoints, frontend view
- Pattern lookup by name query parameter
- Deploy-time variable injection (inject_vars)
- Template YAML definitions (ocp-sno, ocp-compact, ocp-standard)
- Deploy-template API endpoint with YAML-driven topology generation
- Portal VM power action endpoints with access level checks"
```
