# Container Nodes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add containers as first-class canvas nodes — drag from palette, connect to networks/storage, deploy via podman on hosts, with template YAML support, pattern save/restore, and console access.

**Architecture:** New `containerNode` canvas type with its own data model, properties panel, and troshkad handlers. Containers run on the host via podman, attach to the same VXLAN bridges as VMs (full L2 peers). Registry credentials stored per-user in Settings with Fernet encryption. Container images captured as `.tar.gz` for pattern save/restore.

**Tech Stack:** React Flow (canvas), Zustand (state), PatternFly 6 (UI), FastAPI (API), SQLAlchemy 2 (ORM), podman (container runtime), xterm.js (terminal)

## Global Constraints

- Python 3.11+, use `python3` not `python`
- SQLAlchemy 2.0+ with `Mapped[type]` + `mapped_column()` syntax
- UUIDs as strings: `UUID(as_uuid=False), default=lambda: str(uuid.uuid4())`
- Fernet encryption for secrets (same as OCP pull secret)
- Troshkad is stdlib-only Python — no pip dependencies
- Container naming: `troshka-{project_id[:8]}-{container_id[:8]}`
- Disk paths: `/var/lib/troshka/vms/{project_id}/`
- Mount dirs: `/var/lib/troshka/vms/{project_id}/mnt-{disk_id[:8]}/`
- No `sed` for file edits — use the Edit tool
- Run `black` before committing
- Always tell user to restart backend after Python changes

## File Structure

### New Files
- `src/backend/app/models/registry_credential.py` — RegistryCredential SQLAlchemy model
- `src/backend/app/api/registry_credential_routes.py` — CRUD API router
- `src/backend/alembic/versions/XXXX_add_registry_credentials.py` — Migration
- `src/frontend/src/components/canvas/nodes/ContainerNode.tsx` — Canvas node component

### Modified Files
- `src/backend/app/models/__init__.py` — Register RegistryCredential model
- `src/backend/app/main.py` — Register registry credential router
- `src/backend/app/services/deploy_service.py` — Container deploy steps, extraction, teardown
- `src/backend/app/services/template_loader.py` — Parse `containers:` section
- `src/backend/app/services/template_loader.py` — Export `containers:` section (export is in same file)
- `src/backend/app/services/pattern_service.py` — Container image save/restore
- `src/frontend/src/stores/canvasStore.ts` — ContainerNodeData interface, connection validation
- `src/frontend/src/components/canvas/Canvas.tsx` — nodeTypes registration, onDrop handler
- `src/frontend/src/components/canvas/Palette.tsx` — Containers section
- `src/frontend/src/components/canvas/PropertiesPanel.tsx` — Container property sections
- `src/frontend/src/components/canvas/StartOrderPanel.tsx` — Container entries in start order
- `src/frontend/src/app/settings/page.tsx` — Registry credentials section
- `src/troshkad/troshkad.py` — Container lifecycle, logs, exec handlers

---

### Task 1: Registry Credentials — Backend Model + Migration + API

**Files:**
- Create: `src/backend/app/models/registry_credential.py`
- Create: `src/backend/app/api/registry_credential_routes.py`
- Create: `src/backend/alembic/versions/XXXX_add_registry_credentials.py`
- Modify: `src/backend/app/models/__init__.py`
- Modify: `src/backend/app/main.py`
- Test: Manual API test via curl

**Interfaces:**
- Produces: `RegistryCredential` model with `id`, `user_id`, `name`, `registry`, `username`, `password` (encrypted)
- Produces: CRUD endpoints at `/api/v1/auth/registry-credentials`

- [ ] **Step 1: Create RegistryCredential model**

```python
# src/backend/app/models/registry_credential.py
import uuid

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class RegistryCredential(Base):
    __tablename__ = "registry_credentials"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    registry: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user = relationship("User", backref="registry_credentials")
```

- [ ] **Step 2: Register model in `__init__.py`**

Add to `src/backend/app/models/__init__.py`:
```python
from app.models.registry_credential import RegistryCredential
```
And add `"RegistryCredential"` to the `__all__` list.

- [ ] **Step 3: Create Alembic migration**

```bash
cd src/backend && ./venv/bin/python3 -m alembic revision -m "add registry credentials"
```

Edit the generated migration file:
```python
def upgrade() -> None:
    op.create_table(
        "registry_credentials",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("registry", sa.String(255), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("password", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )

def downgrade() -> None:
    op.drop_table("registry_credentials")
```

- [ ] **Step 4: Run migration**

```bash
cd src/backend && ./venv/bin/python3 -m alembic upgrade head
```

- [ ] **Step 5: Create CRUD API router**

Follow the pattern from `src/backend/app/api/auth.py` (OCP pull secret endpoints):

```python
# src/backend/app/api/registry_credential_routes.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.encryption import decrypt, encrypt
from app.db import get_db
from app.models.registry_credential import RegistryCredential
from app.models.user import User

router = APIRouter(prefix="/auth/registry-credentials", tags=["auth"])


@router.get("")
def list_registry_credentials(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    creds = (
        db.query(RegistryCredential)
        .filter(RegistryCredential.user_id == user.id)
        .order_by(RegistryCredential.name)
        .all()
    )
    return [
        {
            "id": c.id,
            "name": c.name,
            "registry": c.registry,
            "username": c.username,
            "created_at": str(c.created_at) if c.created_at else None,
        }
        for c in creds
    ]


@router.post("", status_code=201)
def create_registry_credential(
    body: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    name = (body.get("name") or "").strip()
    registry = (body.get("registry") or "").strip()
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()
    if not all([name, registry, username, password]):
        raise HTTPException(
            status_code=400,
            detail="name, registry, username, and password are required",
        )
    cred = RegistryCredential(
        user_id=user.id,
        name=name,
        registry=registry,
        username=username,
        password=encrypt(password),
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return {"id": cred.id, "name": cred.name, "registry": cred.registry}


@router.put("/{cred_id}")
def update_registry_credential(
    cred_id: str,
    body: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = (
        db.query(RegistryCredential)
        .filter(
            RegistryCredential.id == cred_id,
            RegistryCredential.user_id == user.id,
        )
        .first()
    )
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    if "name" in body:
        cred.name = body["name"].strip()
    if "registry" in body:
        cred.registry = body["registry"].strip()
    if "username" in body:
        cred.username = body["username"].strip()
    if "password" in body and body["password"].strip():
        cred.password = encrypt(body["password"].strip())
    db.commit()
    return {"id": cred.id, "name": cred.name, "registry": cred.registry}


@router.delete("/{cred_id}", status_code=204)
def delete_registry_credential(
    cred_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = (
        db.query(RegistryCredential)
        .filter(
            RegistryCredential.id == cred_id,
            RegistryCredential.user_id == user.id,
        )
        .first()
    )
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    db.delete(cred)
    db.commit()
```

- [ ] **Step 6: Register router in main.py**

Add to `src/backend/app/main.py` with the other router imports and includes:
```python
from app.api import registry_credential_routes
# ...
app.include_router(registry_credential_routes.router, prefix="/api/v1")
```

- [ ] **Step 7: Test API**

Tell user to restart backend, then test:
```bash
curl -s http://localhost:8200/api/v1/auth/registry-credentials | python3 -m json.tool
curl -s -X POST http://localhost:8200/api/v1/auth/registry-credentials \
  -H 'Content-Type: application/json' \
  -d '{"name":"Test Registry","registry":"quay.io","username":"testuser","password":"testpass"}' | python3 -m json.tool
```

- [ ] **Step 8: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/models/registry_credential.py src/backend/app/api/registry_credential_routes.py
git add src/backend/app/models/registry_credential.py src/backend/app/api/registry_credential_routes.py src/backend/app/models/__init__.py src/backend/app/main.py src/backend/alembic/versions/
git commit -m "feat: registry credentials model, migration, and CRUD API"
```

---

### Task 2: Registry Credentials — Frontend Settings UI

**Files:**
- Modify: `src/frontend/src/app/settings/page.tsx`

**Interfaces:**
- Consumes: `GET/POST/PUT/DELETE /api/v1/auth/registry-credentials` from Task 1
- Produces: UI section in Settings page for managing registry credentials

- [ ] **Step 1: Add state variables for registry credentials**

In `src/frontend/src/app/settings/page.tsx`, add state variables alongside the existing OCP pull secret / RH token state (near lines 42-57):

```typescript
const [registryCreds, setRegistryCreds] = useState<
  Array<{ id: string; name: string; registry: string; username: string }>
>([]);
const [showAddCred, setShowAddCred] = useState(false);
const [editCredId, setEditCredId] = useState<string | null>(null);
const [credForm, setCredForm] = useState({ name: "", registry: "", username: "", password: "" });
```

- [ ] **Step 2: Add fetch effect for registry credentials**

Add inside the existing `useEffect` block or a new one:
```typescript
useEffect(() => {
  fetch("/api/v1/auth/registry-credentials")
    .then((r) => r.json())
    .then((data) => setRegistryCreds(data))
    .catch(() => {});
}, []);
```

- [ ] **Step 3: Add helper functions**

```typescript
const fetchCreds = () => {
  fetch("/api/v1/auth/registry-credentials")
    .then((r) => r.json())
    .then((data) => setRegistryCreds(data))
    .catch(() => {});
};

const saveCred = async () => {
  const method = editCredId ? "PUT" : "POST";
  const url = editCredId
    ? `/api/v1/auth/registry-credentials/${editCredId}`
    : "/api/v1/auth/registry-credentials";
  const resp = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(credForm),
  });
  if (resp.ok) {
    setShowAddCred(false);
    setEditCredId(null);
    setCredForm({ name: "", registry: "", username: "", password: "" });
    fetchCreds();
  }
};

const deleteCred = async (id: string) => {
  await fetch(`/api/v1/auth/registry-credentials/${id}`, { method: "DELETE" });
  fetchCreds();
};
```

- [ ] **Step 4: Add Registry Credentials UI section**

Add below the OCP Pull Secret section in the JSX. Follow the existing card/section pattern used by OCP pull secret:

```tsx
<div className="settings-section">
  <h3>Registry Credentials</h3>
  <p className="settings-desc">
    Container registry credentials for pulling private images. Referenced by name in container nodes.
  </p>
  {registryCreds.length > 0 && (
    <table style={{ width: "100%", borderCollapse: "collapse", marginBottom: 12 }}>
      <thead>
        <tr style={{ borderBottom: "1px solid var(--troshka-border)" }}>
          <th style={{ textAlign: "left", padding: "6px 8px", fontSize: 12 }}>Name</th>
          <th style={{ textAlign: "left", padding: "6px 8px", fontSize: 12 }}>Registry</th>
          <th style={{ textAlign: "left", padding: "6px 8px", fontSize: 12 }}>Username</th>
          <th style={{ textAlign: "right", padding: "6px 8px", fontSize: 12 }}></th>
        </tr>
      </thead>
      <tbody>
        {registryCreds.map((c) => (
          <tr key={c.id} style={{ borderBottom: "1px solid var(--troshka-border)" }}>
            <td style={{ padding: "6px 8px", fontSize: 13 }}>{c.name}</td>
            <td style={{ padding: "6px 8px", fontSize: 13, fontFamily: "monospace" }}>{c.registry}</td>
            <td style={{ padding: "6px 8px", fontSize: 13 }}>{c.username}</td>
            <td style={{ padding: "6px 8px", textAlign: "right" }}>
              <button
                className="settings-btn-sm"
                onClick={() => {
                  setEditCredId(c.id);
                  setCredForm({ name: c.name, registry: c.registry, username: c.username, password: "" });
                  setShowAddCred(true);
                }}
              >
                Edit
              </button>
              <button
                className="settings-btn-sm settings-btn-danger"
                style={{ marginLeft: 6 }}
                onClick={() => deleteCred(c.id)}
              >
                Delete
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )}
  {showAddCred ? (
    <div style={{ background: "var(--troshka-surface2)", borderRadius: 8, padding: 12 }}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginBottom: 8 }}>
        <div>
          <label className="settings-label">Name</label>
          <input
            className="settings-input"
            placeholder="e.g., Quay.io prod"
            value={credForm.name}
            onChange={(e) => setCredForm({ ...credForm, name: e.target.value })}
          />
        </div>
        <div>
          <label className="settings-label">Registry</label>
          <input
            className="settings-input"
            placeholder="e.g., quay.io"
            value={credForm.registry}
            onChange={(e) => setCredForm({ ...credForm, registry: e.target.value })}
          />
        </div>
        <div>
          <label className="settings-label">Username</label>
          <input
            className="settings-input"
            value={credForm.username}
            onChange={(e) => setCredForm({ ...credForm, username: e.target.value })}
          />
        </div>
        <div>
          <label className="settings-label">Password</label>
          <input
            className="settings-input"
            type="password"
            placeholder={editCredId ? "(unchanged)" : ""}
            value={credForm.password}
            onChange={(e) => setCredForm({ ...credForm, password: e.target.value })}
          />
        </div>
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <button className="settings-btn" onClick={saveCred}>
          {editCredId ? "Update" : "Add"}
        </button>
        <button
          className="settings-btn-secondary"
          onClick={() => {
            setShowAddCred(false);
            setEditCredId(null);
            setCredForm({ name: "", registry: "", username: "", password: "" });
          }}
        >
          Cancel
        </button>
      </div>
    </div>
  ) : (
    <button className="settings-btn" onClick={() => setShowAddCred(true)}>
      + Add Registry Credential
    </button>
  )}
</div>
```

- [ ] **Step 5: Test in browser**

Open http://localhost:3100/settings, verify:
- Can add a registry credential
- Credential appears in table
- Can edit (password field shows placeholder)
- Can delete

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/settings/page.tsx
git commit -m "feat: registry credentials management UI in Settings page"
```

---

### Task 3: Canvas Data Model + Palette + ContainerNode Component

**Files:**
- Create: `src/frontend/src/components/canvas/nodes/ContainerNode.tsx`
- Modify: `src/frontend/src/stores/canvasStore.ts`
- Modify: `src/frontend/src/components/canvas/Canvas.tsx`
- Modify: `src/frontend/src/components/canvas/Palette.tsx`

**Interfaces:**
- Produces: `ContainerNodeData` interface with `image`, `cpus`, `memory`, `nics`, `envVars`, `ports`, `command`, `restartPolicy`, `privileged`, `mounts`, `registryCredentialId`
- Produces: `containerNode` React Flow node type
- Produces: Palette "Containers" section with draggable container item

- [ ] **Step 1: Add ContainerNodeData interface to canvasStore.ts**

Add after the `StorageNodeData` interface (around line 69):

```typescript
export interface ContainerMount {
  diskNodeId: string;
  mountPath: string;
}

export interface ContainerPort {
  containerPort: number;
  hostPort?: number;
  protocol: "tcp" | "udp";
}

export interface ContainerEnvVar {
  key: string;
  value: string;
}

export interface ContainerNodeData {
  label: string;
  name: string;
  image: string;
  registryCredentialId: string | null;
  cpus: number;
  memory: number;
  nics: VMNic[];
  envVars: ContainerEnvVar[];
  ports: ContainerPort[];
  command: string | null;
  restartPolicy: "always" | "on-failure" | "never";
  privileged: boolean;
  mounts: ContainerMount[];
  status: "running" | "stopped" | "created";
  icon: string;
  [key: string]: any;
}
```

- [ ] **Step 2: Update StartOrderEntry to support containers**

Modify the `StartOrderEntry` interface (around line 81) to add an optional type discriminator and container ID:

```typescript
export interface StartOrderEntry {
  vmId: string;
  containerId?: string;
  entryType?: "vm" | "container";
  autoStart: boolean;
  waitForVm: string | null;
  waitForService: string;
  waitForPort: string;
  delaySeconds: number;
}
```

- [ ] **Step 3: Create ContainerNode component**

```typescript
// src/frontend/src/components/canvas/nodes/ContainerNode.tsx
"use client";

import React, { memo, useEffect } from "react";
import { Handle, Position, useUpdateNodeInternals } from "@xyflow/react";
import type { NodeProps } from "@xyflow/react";
import { useCanvasStore } from "@/stores/canvasStore";
import type { ContainerNodeData } from "@/stores/canvasStore";

function ContainerNodeComponent({ id, data, selected }: NodeProps) {
  const projectState = useCanvasStore((s) => s.projectState);
  const deployedNodeData = useCanvasStore((s) => s.deployedNodeData);
  const updateNodeInternals = useUpdateNodeInternals();
  const d = data as unknown as ContainerNodeData;
  const isRunning = d.status === "running";

  const nicCount = (d.nics || []).length;
  const mountCount = (d.mounts || []).length;
  useEffect(() => {
    const t1 = setTimeout(() => updateNodeInternals(id), 0);
    const t2 = setTimeout(() => updateNodeInternals(id), 200);
    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
    };
  }, [id, nicCount, mountCount, updateNodeInternals]);

  const deployed = deployedNodeData?.[id];
  const displayStatus =
    projectState === "deployed" && deployed?.status
      ? deployed.status
      : d.status || "stopped";

  const statusColor =
    displayStatus === "running"
      ? "var(--troshka-green)"
      : displayStatus === "created"
        ? "var(--troshka-yellow)"
        : "var(--troshka-text-dim)";

  const imageName = d.image
    ? d.image.split("/").pop()?.split(":")[0] || d.image
    : "no image";

  return (
    <div
      className={`canvas-node canvas-node-container ${selected ? "canvas-node-selected" : ""}`}
      style={{
        borderColor: selected
          ? "var(--troshka-blue)"
          : "rgba(56, 189, 248, 0.3)",
      }}
    >
      <div className="canvas-node-header">
        <span className="canvas-node-icon">📦</span>
        <span className="canvas-node-label">{d.name || "container"}</span>
        <span
          className="canvas-node-status-dot"
          style={{ background: statusColor }}
          title={displayStatus}
        />
      </div>

      <div className="canvas-node-body">
        <div
          className="canvas-node-detail"
          style={{ fontFamily: "monospace", fontSize: 10 }}
          title={d.image}
        >
          {imageName}
        </div>
        <div className="canvas-node-detail">
          {d.cpus} CPU · {d.memory >= 1024 ? `${d.memory / 1024}G` : `${d.memory}M`} RAM
        </div>
      </div>

      {/* Network handles — top/bottom, same pattern as VM */}
      {(d.nics || [{ id: "default" }]).map((nic, i, arr) => {
        const pct =
          arr.length === 1
            ? 50
            : 20 + (i * 60) / Math.max(arr.length - 1, 1);
        return (
          <React.Fragment key={nic.id}>
            <Handle
              type="source"
              position={Position.Top}
              id={`nic-${nic.id}-top`}
              className="canvas-handle canvas-handle-network"
              style={{ left: `${pct}%` }}
            />
            <Handle
              type="source"
              position={Position.Bottom}
              id={`nic-${nic.id}-bottom`}
              className="canvas-handle canvas-handle-network"
              style={{ left: `${pct}%` }}
            />
          </React.Fragment>
        );
      })}

      {/* Mount handles — left/right, same pattern as VM disk controllers */}
      {(d.mounts && d.mounts.length > 0
        ? d.mounts
        : [{ diskNodeId: "default", mountPath: "" }]
      ).map((mount, i, arr) => {
        const pct =
          arr.length === 1
            ? 50
            : 20 + (i * 60) / Math.max(arr.length - 1, 1);
        const handleId = mount.diskNodeId || `mount-${i}`;
        return (
          <React.Fragment key={handleId}>
            <Handle
              type="source"
              position={Position.Left}
              id={`mnt-${handleId}-left`}
              className="canvas-handle canvas-handle-storage"
              style={{ top: `${pct}%` }}
            />
            <Handle
              type="source"
              position={Position.Right}
              id={`mnt-${handleId}-right`}
              className="canvas-handle canvas-handle-storage"
              style={{ top: `${pct}%` }}
            />
          </React.Fragment>
        );
      })}
    </div>
  );
}

export const ContainerNode = memo(ContainerNodeComponent);
```

- [ ] **Step 4: Register containerNode type in Canvas.tsx**

In `src/frontend/src/components/canvas/Canvas.tsx`, add the import and node type:

```typescript
import { ContainerNode } from "./nodes/ContainerNode";

const nodeTypes = {
  vmNode: VMNode,
  networkNode: NetworkNode,
  storageNode: StorageNode,
  containerNode: ContainerNode,
};
```

- [ ] **Step 5: Add onDrop handler for container type**

In `Canvas.tsx`, add a new branch in the `onDrop` callback (after the vm-linux block, around line 223):

```typescript
} else if (item.type === "container") {
  const name = nextName("ctr");
  newNode = {
    id,
    type: "containerNode",
    position,
    data: {
      label: name,
      name,
      image: "",
      registryCredentialId: null,
      cpus: 1,
      memory: 512,
      status: "stopped" as const,
      icon: "📦",
      nics: [
        {
          id: generateNicId(),
          name: "eth0",
          mac: generateMac(),
          model: "virtio",
        },
      ],
      envVars: [],
      ports: [],
      command: null,
      restartPolicy: "always" as const,
      privileged: false,
      mounts: [],
    },
  };
}
```

- [ ] **Step 6: Add Containers section to Palette**

In `src/frontend/src/components/canvas/Palette.tsx`, add a new section between "Compute" and "Networking":

```typescript
{
  title: "Containers",
  items: [
    {
      type: "container",
      label: "Container",
      desc: "Podman container",
      icon: "📦",
      iconClass: "palette-icon-container",
    },
  ],
},
```

- [ ] **Step 7: Add CSS for container node**

Add to the canvas CSS file (find it by searching for `.canvas-node` styles — likely in a global CSS or component-level styles):

```css
.canvas-node-container {
  border-left: 3px solid rgba(56, 189, 248, 0.5);
}
```

- [ ] **Step 8: Test in browser**

Open http://localhost:3100, create a project, verify:
- "Containers" section appears in palette between Compute and Networking
- Can drag "Container" onto canvas
- Container node renders with icon, name, resource summary
- NIC handles appear on top/bottom
- Mount handles appear on left/right

- [ ] **Step 9: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/components/canvas/nodes/ContainerNode.tsx src/frontend/src/stores/canvasStore.ts src/frontend/src/components/canvas/Canvas.tsx src/frontend/src/components/canvas/Palette.tsx
git commit -m "feat: container canvas node type with palette, data model, and handles"
```

---

### Task 4: Container Properties Panel + Connection Validation + Start Order

**Files:**
- Modify: `src/frontend/src/components/canvas/PropertiesPanel.tsx`
- Modify: `src/frontend/src/stores/canvasStore.ts` (onConnect)
- Modify: `src/frontend/src/components/canvas/StartOrderPanel.tsx`

**Interfaces:**
- Consumes: `ContainerNodeData`, `ContainerMount`, `ContainerPort`, `ContainerEnvVar` from Task 3
- Consumes: `GET /api/v1/auth/registry-credentials` from Task 1
- Produces: Container properties editing UI
- Produces: Connection validation rules for containerNode
- Produces: Container entries in start order panel

- [ ] **Step 1: Add container connection validation to onConnect**

In `src/frontend/src/stores/canvasStore.ts`, inside the `onConnect` handler (around line 289), add container validation rules. After the existing router/gateway checks and before the edge creation:

```typescript
const sIsContainer = sType === "containerNode";
const tIsContainer = tType === "containerNode";

// Containers can only connect to networks (via NIC handles) and storage (via mount handles)
if (sIsContainer || tIsContainer) {
  const otherType = sIsContainer ? tType : sType;
  if (otherType !== "networkNode" && otherType !== "storageNode") return;
  // Containers cannot connect to routers/gateways directly
  const otherSub = sIsContainer
    ? (targetNode.data as Record<string, any>).subtype
    : (sourceNode.data as Record<string, any>).subtype;
  if (otherSub === "router" || otherSub === "gateway" || otherSub === "loadbalancer") return;
}
```

Also update the storage connection validation — currently it only allows storage → vmNode. Extend it to allow storage → containerNode:

Find the block that checks storage connections (around line 305-320) and change `vmNode` checks to also accept `containerNode`:
```typescript
// Before: if (otherType !== "vmNode") return;
// After:
if (otherType !== "vmNode" && otherType !== "containerNode") return;
```

- [ ] **Step 2: Add container properties panel sections**

In `src/frontend/src/components/canvas/PropertiesPanel.tsx`, add container-specific sections. Add the import for ContainerNodeData:

```typescript
import type { VMNodeData, ContainerNodeData } from "@/stores/canvasStore";
```

Then add the container panel sections inside the main return JSX. After the existing `nodeType === "vmNode"` block, add `nodeType === "containerNode"` sections:

```tsx
{nodeType === "containerNode" && (
  <>
    {/* Image section */}
    <div className="props-section">
      <div className="props-section-title">Image</div>
      <div className="props-field">
        <label className="props-label">Image</label>
        <input
          className="props-input"
          placeholder="registry/org/image:tag"
          value={(data as unknown as ContainerNodeData).image || ""}
          onChange={(e) => update("image", e.target.value)}
          style={{ fontFamily: "monospace", fontSize: 11 }}
        />
      </div>
      <div className="props-field">
        <label className="props-label">Registry Credential</label>
        <RegistryCredentialDropdown
          value={(data as unknown as ContainerNodeData).registryCredentialId}
          onChange={(v) => update("registryCredentialId", v)}
        />
      </div>
    </div>

    {/* Resources section */}
    <div className="props-section">
      <div className="props-section-title">Resources</div>
      <div className="props-row">
        <div className="props-field">
          <label className="props-label">CPUs</label>
          <input
            className="props-input"
            type="number"
            min={1}
            max={32}
            value={(data as unknown as ContainerNodeData).cpus}
            onFocus={(e) => e.target.select()}
            onChange={(e) => update("cpus", parseInt(e.target.value) || 1)}
          />
        </div>
        <div className="props-field">
          <label className="props-label">Memory (MB)</label>
          <input
            className="props-input"
            type="number"
            min={64}
            max={524288}
            value={(data as unknown as ContainerNodeData).memory}
            onFocus={(e) => e.target.select()}
            onChange={(e) => update("memory", parseInt(e.target.value) || 512)}
          />
        </div>
      </div>
    </div>

    {/* NIC section — reuse the VM NIC panel pattern */}
    {/* Copy the NIC management section from the vmNode block, changing VMNodeData to ContainerNodeData */}

    {/* Environment Variables section */}
    <div className="props-section">
      <div className="props-section-title">Environment Variables</div>
      {((data as unknown as ContainerNodeData).envVars || []).map((ev, i) => (
        <div key={i} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
          <input
            className="props-input"
            placeholder="KEY"
            value={ev.key}
            style={{ flex: 1, fontFamily: "monospace", fontSize: 11 }}
            onChange={(e) => {
              const updated = [...((data as unknown as ContainerNodeData).envVars || [])];
              updated[i] = { ...ev, key: e.target.value };
              update("envVars", updated);
            }}
          />
          <span style={{ color: "var(--troshka-text-dim)" }}>=</span>
          <input
            className="props-input"
            placeholder="value"
            value={ev.value}
            style={{ flex: 2, fontFamily: "monospace", fontSize: 11 }}
            onChange={(e) => {
              const updated = [...((data as unknown as ContainerNodeData).envVars || [])];
              updated[i] = { ...ev, value: e.target.value };
              update("envVars", updated);
            }}
          />
          <button
            style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", fontSize: 12 }}
            onClick={() => {
              const updated = ((data as unknown as ContainerNodeData).envVars || []).filter((_, idx) => idx !== i);
              update("envVars", updated);
            }}
          >✕</button>
        </div>
      ))}
      <button
        className="props-library-btn"
        onClick={() => update("envVars", [...((data as unknown as ContainerNodeData).envVars || []), { key: "", value: "" }])}
      >+ Add Variable</button>
    </div>

    {/* Ports section */}
    <div className="props-section">
      <div className="props-section-title">Ports</div>
      {((data as unknown as ContainerNodeData).ports || []).map((p, i) => (
        <div key={i} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
          <input
            className="props-input"
            type="number"
            placeholder="Container"
            value={p.containerPort || ""}
            style={{ width: 70 }}
            onChange={(e) => {
              const updated = [...((data as unknown as ContainerNodeData).ports || [])];
              updated[i] = { ...p, containerPort: parseInt(e.target.value) || 0 };
              update("ports", updated);
            }}
          />
          <span style={{ color: "var(--troshka-text-dim)", fontSize: 11 }}>→</span>
          <input
            className="props-input"
            type="number"
            placeholder="Host (opt)"
            value={p.hostPort || ""}
            style={{ width: 70 }}
            onChange={(e) => {
              const updated = [...((data as unknown as ContainerNodeData).ports || [])];
              updated[i] = { ...p, hostPort: parseInt(e.target.value) || undefined };
              update("ports", updated);
            }}
          />
          <select
            className="props-select"
            value={p.protocol || "tcp"}
            style={{ width: 60 }}
            onChange={(e) => {
              const updated = [...((data as unknown as ContainerNodeData).ports || [])];
              updated[i] = { ...p, protocol: e.target.value as "tcp" | "udp" };
              update("ports", updated);
            }}
          >
            <option value="tcp">TCP</option>
            <option value="udp">UDP</option>
          </select>
          <button
            style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", fontSize: 12 }}
            onClick={() => {
              const updated = ((data as unknown as ContainerNodeData).ports || []).filter((_, idx) => idx !== i);
              update("ports", updated);
            }}
          >✕</button>
        </div>
      ))}
      <button
        className="props-library-btn"
        onClick={() => update("ports", [...((data as unknown as ContainerNodeData).ports || []), { containerPort: 0, protocol: "tcp" }])}
      >+ Add Port</button>
    </div>

    {/* Volumes section — auto-populated from connected storage edges */}
    <div className="props-section">
      <div className="props-section-title">Volumes</div>
      {(() => {
        const connectedDisks = edges
          .filter(
            (e) =>
              (e.source === node!.id || e.target === node!.id) &&
              (e.sourceHandle?.startsWith("mnt-") || e.targetHandle?.startsWith("mnt-"))
          )
          .map((e) => {
            const diskId = e.source === node!.id ? e.target : e.source;
            return nodes.find((n) => n.id === diskId && n.type === "storageNode");
          })
          .filter(Boolean);

        if (connectedDisks.length === 0) {
          return <span style={{ fontSize: 11, color: "var(--troshka-text-dim)" }}>Connect a Disk node to add volumes</span>;
        }

        const mounts = (data as unknown as ContainerNodeData).mounts || [];
        return connectedDisks.map((diskNode) => {
          const existing = mounts.find((m) => m.diskNodeId === diskNode!.id);
          const diskData = diskNode!.data as Record<string, any>;
          return (
            <div key={diskNode!.id} style={{ display: "flex", gap: 6, marginBottom: 4, alignItems: "center" }}>
              <span style={{ fontSize: 12, minWidth: 60 }}>🛢 {diskData.name}</span>
              <span style={{ color: "var(--troshka-text-dim)", fontSize: 11 }}>→</span>
              <input
                className="props-input"
                placeholder="/mount/path"
                value={existing?.mountPath || ""}
                style={{ flex: 1, fontFamily: "monospace", fontSize: 11 }}
                onChange={(e) => {
                  const updated = mounts.filter((m) => m.diskNodeId !== diskNode!.id);
                  updated.push({ diskNodeId: diskNode!.id, mountPath: e.target.value });
                  update("mounts", updated);
                }}
              />
            </div>
          );
        });
      })()}
    </div>

    {/* Advanced section */}
    <div className="props-section">
      <div className="props-section-title">Advanced</div>
      <div className="props-field">
        <label className="props-label">Restart Policy</label>
        <select
          className="props-select"
          value={(data as unknown as ContainerNodeData).restartPolicy || "always"}
          onChange={(e) => update("restartPolicy", e.target.value)}
        >
          <option value="always">Always</option>
          <option value="on-failure">On Failure</option>
          <option value="never">Never</option>
        </select>
      </div>
      <div className="props-field">
        <label className="props-label">Command Override</label>
        <input
          className="props-input"
          placeholder="Optional entrypoint override"
          value={(data as unknown as ContainerNodeData).command || ""}
          style={{ fontFamily: "monospace", fontSize: 11 }}
          onChange={(e) => update("command", e.target.value || null)}
        />
      </div>
      <div className="props-field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="checkbox"
          checked={(data as unknown as ContainerNodeData).privileged || false}
          onChange={(e) => update("privileged", e.target.checked)}
        />
        <label className="props-label" style={{ marginBottom: 0 }}>Privileged</label>
      </div>
    </div>
  </>
)}
```

- [ ] **Step 3: Create RegistryCredentialDropdown component**

Add this as a helper component inside PropertiesPanel.tsx (or as a separate file):

```tsx
function RegistryCredentialDropdown({
  value,
  onChange,
}: {
  value: string | null;
  onChange: (v: string | null) => void;
}) {
  const [creds, setCreds] = useState<Array<{ id: string; name: string; registry: string }>>([]);
  useEffect(() => {
    fetch("/api/v1/auth/registry-credentials")
      .then((r) => r.json())
      .then(setCreds)
      .catch(() => {});
  }, []);
  return (
    <select
      className="props-select"
      value={value || ""}
      onChange={(e) => onChange(e.target.value || null)}
    >
      <option value="">None (public)</option>
      {creds.map((c) => (
        <option key={c.id} value={c.id}>
          {c.name} ({c.registry})
        </option>
      ))}
    </select>
  );
}
```

- [ ] **Step 4: Update header detection for container nodes**

In the props-header section of PropertiesPanel.tsx (around line 183-218), add containerNode to the icon/subtitle logic:

```typescript
// Icon
nodeType === "containerNode"
  ? "📦"
  : // ... existing vm/network/storage logic

// Subtitle
nodeType === "containerNode"
  ? `Container · ${(data as unknown as ContainerNodeData).status === "running" ? "Running" : "Stopped"}`
  : // ... existing logic
```

- [ ] **Step 5: Add containers to StartOrderPanel**

In `src/frontend/src/components/canvas/StartOrderPanel.tsx`, update the auto-population logic that adds new VMs to include container nodes. Find where VMs are discovered from topology nodes and extend:

```typescript
// When building the order list from topology, also include containerNodes
const allNodes = nodes.filter((n) => n.type === "vmNode" || n.type === "containerNode");
```

Update the display to show a container icon for container entries:
```typescript
<span className="start-order-name">
  {entry.entryType === "container" ? "📦" : "🖥"} {getNodeName(entry.vmId || entry.containerId || "")}
</span>
```

The `getNodeName` helper should look up nodes by ID regardless of type.

- [ ] **Step 6: Test in browser**

Open http://localhost:3100, create a project:
- Drag a container, drag a network, connect them
- Verify connection works (edge appears)
- Verify container ↔ container connections are rejected
- Verify container ↔ VM connections are rejected
- Select container, verify properties panel shows all sections
- Add env vars, ports, set image
- Drag a disk, connect to container, verify mount path field appears
- Check start order includes the container

- [ ] **Step 7: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/components/canvas/PropertiesPanel.tsx src/frontend/src/stores/canvasStore.ts src/frontend/src/components/canvas/StartOrderPanel.tsx
git commit -m "feat: container properties panel, connection validation, and start order support"
```

---

### Task 5: Template Loader — Parse `containers:` Section

**Files:**
- Modify: `src/backend/app/services/template_loader.py`

**Interfaces:**
- Consumes: Template YAML with `containers:` section (format defined in spec)
- Produces: `containerNode` topology nodes with edges to networks and storage nodes
- Produces: Start order entries with `entryType: "container"`

- [ ] **Step 1: Parse containers section in resolve_inline_template**

In `src/backend/app/services/template_loader.py`, in the `resolve_inline_template()` function (around line 123), add:

```python
if tmpl.get("containers"):
    resolved["containers"] = tmpl["containers"]
```

- [ ] **Step 2: Generate container topology nodes**

In `_generate_topology_from_vms()` (or its calling function `generate_topology_from_template()`), after the VM node generation loop, add container node generation. This follows the exact same pattern as VM node creation — create node, create NICs, connect to networks via edges:

```python
# After VM nodes are generated, generate container nodes
containers_def = resolved.get("containers", {})
for ctr_key, ctr_cfg in containers_def.items():
    ctr_id = str(uuid.uuid4())
    ctr_nics = []
    for i, nic_cfg in enumerate(ctr_cfg.get("nics", [])):
        nic_id = str(uuid.uuid4())[:8]
        mac = _mac()
        if nic_cfg.get("mac"):
            mac = nic_cfg["mac"]
        ctr_nics.append({
            "id": nic_id,
            "name": f"eth{i}",
            "mac": mac,
            "model": nic_cfg.get("model", "virtio"),
            "ip": nic_cfg.get("ip", ""),
        })

        # Create edge from container NIC to network node
        net_name = nic_cfg.get("network", "")
        net_node_id = net_name_to_id.get(net_name)
        if net_node_id:
            edges.append({
                "id": f"e-{ctr_id}-nic-{nic_id}",
                "source": ctr_id,
                "target": net_node_id,
                "sourceHandle": f"nic-{nic_id}-bottom",
                "targetHandle": "top",
                "type": "smoothstep",
                "style": {"stroke": "rgba(96,165,250,0.5)", "strokeWidth": 2},
            })

    # Create storage nodes + mount entries for container disks
    ctr_mounts = []
    for disk_cfg in ctr_cfg.get("disks", []):
        disk_id = str(uuid.uuid4())
        disk_node = {
            "id": disk_id,
            "type": "storageNode",
            "position": {"x": 0, "y": 0},  # auto-layout fixes this
            "data": {
                "label": f"{ctr_key}-vol",
                "name": f"{ctr_key}-vol-{len(ctr_mounts)}",
                "size": disk_cfg.get("size_gb", 10),
                "format": "raw",
                "icon": "🛢",
            },
        }
        nodes.append(disk_node)
        ctr_mounts.append({
            "diskNodeId": disk_id,
            "mountPath": disk_cfg.get("mount_path", ""),
        })
        # Edge from storage to container mount handle
        edges.append({
            "id": f"e-{disk_id}-{ctr_id}",
            "source": disk_id,
            "target": ctr_id,
            "sourceHandle": "right",
            "targetHandle": f"mnt-{disk_id}-left",
            "type": "smoothstep",
            "style": {"stroke": "rgba(251,191,36,0.6)", "strokeWidth": 2, "strokeDasharray": "4 4"},
        })

    # Build env vars
    env_vars = []
    for k, v in (ctr_cfg.get("env") or {}).items():
        env_vars.append({"key": k, "value": str(v)})

    # Build ports
    ports = []
    for p in ctr_cfg.get("ports", []):
        ports.append({
            "containerPort": p.get("container_port", 0),
            "hostPort": p.get("host_port"),
            "protocol": p.get("protocol", "tcp"),
        })

    # Container node
    ctr_node = {
        "id": ctr_id,
        "type": "containerNode",
        "position": {"x": 0, "y": 0},  # auto-layout fixes this
        "data": {
            "label": ctr_key,
            "name": ctr_key,
            "image": ctr_cfg.get("image", ""),
            "registryCredentialId": None,  # Resolved by name at deploy time
            "registryCredentialName": ctr_cfg.get("registry_credential"),
            "cpus": ctr_cfg.get("cpus", 1),
            "memory": ctr_cfg.get("memory_mb", 512),
            "nics": ctr_nics,
            "envVars": env_vars,
            "ports": ports,
            "command": ctr_cfg.get("command"),
            "restartPolicy": ctr_cfg.get("restart_policy", "always"),
            "privileged": ctr_cfg.get("privileged", False),
            "mounts": ctr_mounts,
            "status": "stopped",
            "icon": "📦",
        },
    }
    nodes.append(ctr_node)
```

- [ ] **Step 3: Add containers to start_order generation**

In the start order generation section, process `start_order` entries with `container:` prefix:

```python
for entry in resolved.get("start_order", []):
    if "container" in entry:
        ctr_name = entry["container"]
        ctr_node = next((n for n in nodes if n["type"] == "containerNode" and n["data"]["name"] == ctr_name), None)
        if ctr_node:
            start_order.append({
                "vmId": ctr_node["id"],
                "containerId": ctr_node["id"],
                "entryType": "container",
                "autoStart": True,
                "waitForVm": None,
                "waitForService": "none",
                "waitForPort": "",
                "delaySeconds": 0,
            })
    elif "vm" in entry:
        # existing VM start order logic
        pass
```

- [ ] **Step 4: Include containers in auto-layout**

The auto-layout function positions nodes after generation. Ensure `containerNode` types are included in the layout calculation alongside `vmNode` types.

- [ ] **Step 5: Test template import**

Create a test template file and test the import API:
```bash
curl -s -X POST http://localhost:8200/api/v1/projects \
  -H 'Content-Type: application/json' \
  -d '{"name":"container-test","description":"test"}' | python3 -m json.tool
# Get project ID, then:
curl -s -X POST http://localhost:8200/api/v1/projects/{id}/import-template \
  -H 'Content-Type: application/json' \
  -d '{"template_yaml": {"name":"test","networks":{"cluster":{"cidr":"10.0.0.0/24","dhcp":true}},"containers":{"registry":{"image":"docker.io/library/registry:2","cpus":1,"memory_mb":1024,"nics":[{"network":"cluster","ip":"10.0.0.5"}],"ports":[{"container_port":5000}],"disks":[{"size_gb":50,"mount_path":"/var/lib/registry"}]}}}}'
```

Verify the topology JSONB contains containerNode with correct edges.

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/template_loader.py
git add src/backend/app/services/template_loader.py
git commit -m "feat: template loader parses containers section into topology"
```

---

### Task 6: Template Export — Emit `containers:` Section

**Files:**
- Modify: `src/backend/app/services/template_loader.py` (export function is in the same file)

**Interfaces:**
- Consumes: Topology JSONB with `containerNode` nodes
- Produces: YAML `containers:` section in exported template

- [ ] **Step 1: Add container export to export_topology_to_template**

In the `export_topology_to_template()` function, after the VM export loop, add container export. Follow the same pattern — index nodes, resolve edges to find network/storage connections:

```python
# Export containers
container_nodes = [n for n in topology.get("nodes", []) if n.get("type") == "containerNode"]
if container_nodes:
    containers = {}
    for ctr_node in container_nodes:
        cd = ctr_node.get("data", {})
        ctr_name = cd.get("name", "container")

        # Resolve NIC → network connections (same edge-walking as VMs)
        nics_export = []
        for nic in cd.get("nics", []):
            nic_id = nic.get("id", "")
            handle_top = f"nic-{nic_id}-top"
            handle_bottom = f"nic-{nic_id}-bottom"
            net_name = None
            for edge in all_edges:
                if edge.get("source") == ctr_node["id"] and edge.get("sourceHandle") in (handle_top, handle_bottom):
                    net_node = net_nodes.get(edge["target"])
                    if net_node:
                        net_name = net_node.get("data", {}).get("name")
                elif edge.get("target") == ctr_node["id"] and edge.get("targetHandle") in (handle_top, handle_bottom):
                    net_node = net_nodes.get(edge["source"])
                    if net_node:
                        net_name = net_node.get("data", {}).get("name")
            nic_entry = {}
            if net_name:
                nic_entry["network"] = net_name
            if nic.get("ip"):
                nic_entry["ip"] = nic["ip"]
            if nic.get("model") and nic["model"] != "virtio":
                nic_entry["model"] = nic["model"]
            if nic_entry:
                nics_export.append(nic_entry)

        # Resolve mount → storage connections
        disks_export = []
        for mount in cd.get("mounts", []):
            disk_node = storage_nodes.get(mount.get("diskNodeId", ""))
            if disk_node:
                dd = disk_node.get("data", {})
                disks_export.append({
                    "size_gb": dd.get("size", 10),
                    "mount_path": mount.get("mountPath", ""),
                })

        ctr_export = {"image": cd.get("image", "")}
        if cd.get("registryCredentialName"):
            ctr_export["registry_credential"] = cd["registryCredentialName"]
        if cd.get("cpus", 1) != 1:
            ctr_export["cpus"] = cd["cpus"]
        if cd.get("memory", 512) != 512:
            ctr_export["memory_mb"] = cd["memory"]
        if cd.get("privileged"):
            ctr_export["privileged"] = True
        if cd.get("restartPolicy", "always") != "always":
            ctr_export["restart_policy"] = cd["restartPolicy"]
        if cd.get("command"):
            ctr_export["command"] = cd["command"]
        if nics_export:
            ctr_export["nics"] = nics_export
        if cd.get("envVars"):
            ctr_export["env"] = {ev["key"]: ev["value"] for ev in cd["envVars"] if ev.get("key")}
        if cd.get("ports"):
            ctr_export["ports"] = [
                {"container_port": p["containerPort"], **({"host_port": p["hostPort"]} if p.get("hostPort") else {}), **({"protocol": p["protocol"]} if p.get("protocol", "tcp") != "tcp" else {})}
                for p in cd["ports"]
            ]
        if disks_export:
            ctr_export["disks"] = disks_export

        containers[ctr_name] = ctr_export

    result["containers"] = containers
```

- [ ] **Step 2: Add containers to start_order export**

In the start_order export section, handle container entries:

```python
for entry in topology.get("startOrder", []):
    if entry.get("entryType") == "container":
        ctr_node = next((n for n in container_nodes if n["id"] == entry.get("containerId", entry.get("vmId", ""))), None)
        if ctr_node:
            exported_order.append({"container": ctr_node["data"]["name"]})
    else:
        # existing VM start order export
        vm_node = next((n for n in vm_nodes if n["id"] == entry.get("vmId", "")), None)
        if vm_node:
            exported_order.append({"vm": vm_node["data"]["name"]})
```

- [ ] **Step 3: Test round-trip**

Import a template with containers, then export and verify the `containers:` section appears correctly:

```bash
curl -s http://localhost:8200/api/v1/projects/{id}/export-template
```

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/template_loader.py
git add src/backend/app/services/template_loader.py
git commit -m "feat: template export emits containers section from topology"
```

---

### Task 7: Troshkad Container Lifecycle Handlers

**Files:**
- Modify: `src/troshkad/troshkad.py`

**Interfaces:**
- Produces: `/containers/pull`, `/containers/create`, `/containers/start`, `/containers/stop`, `/containers/destroy` command handlers
- Produces: `/containers/states` GET endpoint

- [ ] **Step 1: Add container pull handler**

Add near the other command handlers in `src/troshkad/troshkad.py`:

```python
def _handle_container_pull(job, params):
    image = params["image"]
    registry = params.get("registry")
    username = params.get("username")
    password = params.get("password")

    # Login if credentials provided
    if registry and username and password:
        _job_log(job, f"Logging in to {registry}...")
        _run_cmd(
            job,
            ["podman", "login", registry, "-u", username, "-p", password],
            timeout=30,
        )

    _job_log(job, f"Pulling {image}...")
    _run_cmd(job, ["podman", "pull", image], timeout=600)
    return {"image": image, "status": "pulled"}

COMMAND_HANDLERS["containers/pull"] = _handle_container_pull
```

- [ ] **Step 2: Add container create handler**

```python
def _handle_container_create(job, params):
    name = params["container_name"]
    image = params["image"]
    cpus = params.get("cpus", 1)
    memory_mb = params.get("memory_mb", 512)
    env_vars = params.get("env_vars", [])
    ports = params.get("ports", [])
    networks = params.get("networks", [])
    volumes = params.get("volumes", [])
    command = params.get("command")
    restart_policy = params.get("restart_policy", "always")
    privileged = params.get("privileged", False)

    # Loop-mount raw disk volumes
    mount_dirs = []
    for vol in volumes:
        disk_path = _validate_path(vol["disk_path"])
        mount_dir = _validate_path(vol["mount_dir"])
        os.makedirs(mount_dir, exist_ok=True)

        # Format if not already formatted
        try:
            blkid = subprocess.run(
                ["blkid", disk_path], capture_output=True, text=True, timeout=5
            )
            if blkid.returncode != 0:
                _job_log(job, f"Formatting {os.path.basename(disk_path)} as ext4...")
                _run_cmd(job, ["mkfs.ext4", "-q", "-F", disk_path], timeout=30)
        except subprocess.TimeoutExpired:
            pass

        _job_log(job, f"Mounting {os.path.basename(disk_path)} at {mount_dir}")
        _run_cmd(job, ["mount", "-o", "loop", disk_path, mount_dir], timeout=10)
        mount_dirs.append(mount_dir)

    # Build podman create command
    cmd = ["podman", "create", "--name", name]
    cmd.extend(["--cpus", str(cpus)])
    cmd.extend(["--memory", f"{memory_mb}m"])
    cmd.extend(["--restart", restart_policy])

    if privileged:
        cmd.append("--privileged")

    for ev in env_vars:
        cmd.extend(["-e", f"{ev['key']}={ev['value']}"])

    for p in ports:
        port_str = f"{p['containerPort']}"
        if p.get("hostPort"):
            port_str = f"{p['hostPort']}:{p['containerPort']}"
        if p.get("protocol", "tcp") == "udp":
            port_str += "/udp"
        cmd.extend(["-p", port_str])

    # Network attachment — register podman network for each bridge, then attach
    for net in networks:
        bridge = _validate_bridge_name(net["bridge"])
        cidr = net.get("cidr", "10.0.0.0/24")
        podman_net_name = f"troshka-{bridge}"

        # Create podman network pointing at existing bridge (idempotent)
        try:
            _run_cmd(
                job,
                [
                    "podman", "network", "create", podman_net_name,
                    "--driver", "bridge",
                    "--opt", f"bridge.name={bridge}",
                    "--subnet", cidr,
                ],
                timeout=10,
            )
        except RuntimeError:
            _job_log(job, f"Network {podman_net_name} already exists, reusing")

        net_arg = podman_net_name
        if net.get("ip"):
            net_arg += f":ip={net['ip']}"
        if net.get("mac"):
            net_arg += f",mac={net['mac']}"
        cmd.extend(["--network", net_arg])

    for vol in volumes:
        mount_dir = _validate_path(vol["mount_dir"])
        mount_path = vol["mount_path"]
        cmd.extend(["-v", f"{mount_dir}:{mount_path}"])

    cmd.append(image)
    if command:
        cmd.extend(command.split())

    _job_log(job, f"Creating container {name}...")
    _run_cmd(job, cmd, timeout=60)
    return {"container_name": name, "status": "created"}

COMMAND_HANDLERS["containers/create"] = _handle_container_create
```

- [ ] **Step 3: Add start/stop/destroy handlers**

```python
def _handle_container_start(job, params):
    name = params["container_name"]
    _job_log(job, f"Starting container {name}...")
    _run_cmd(job, ["podman", "start", name], timeout=30)
    return {"container_name": name, "status": "started"}

COMMAND_HANDLERS["containers/start"] = _handle_container_start


def _handle_container_stop(job, params):
    name = params["container_name"]
    timeout = params.get("timeout", 10)
    _job_log(job, f"Stopping container {name}...")
    _run_cmd(job, ["podman", "stop", "-t", str(timeout), name], timeout=timeout + 10)
    return {"container_name": name, "status": "stopped"}

COMMAND_HANDLERS["containers/stop"] = _handle_container_stop


def _handle_container_destroy(job, params):
    name = params["container_name"]
    project_id = params.get("project_id", "")
    volumes = params.get("volumes", [])

    # Stop container (ignore errors if already stopped)
    _job_log(job, f"Stopping container {name}...")
    try:
        _run_cmd(job, ["podman", "stop", "-t", "5", name], timeout=15)
    except RuntimeError:
        pass

    # Remove container
    _job_log(job, f"Removing container {name}...")
    try:
        _run_cmd(job, ["podman", "rm", "-f", name], timeout=15)
    except RuntimeError:
        pass

    # Unmount loop devices
    for vol in volumes:
        mount_dir = vol.get("mount_dir", "")
        if mount_dir and os.path.ismount(mount_dir):
            _job_log(job, f"Unmounting {mount_dir}")
            try:
                _run_cmd(job, ["umount", mount_dir], timeout=10)
            except RuntimeError:
                _run_cmd(job, ["umount", "-l", mount_dir], timeout=10)

    return {"container_name": name, "status": "destroyed"}

COMMAND_HANDLERS["containers/destroy"] = _handle_container_destroy
```

- [ ] **Step 4: Add container state endpoint**

```python
@route("GET", "/containers/states")
def handle_container_states(handler, params):
    domains = {}
    try:
        result = subprocess.run(
            ["podman", "ps", "-a", "--filter", "name=troshka-", "--format", "{{.Names}} {{.State}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                name, state = parts
                state_map = {
                    "running": "running",
                    "created": "created",
                    "exited": "stopped",
                    "paused": "paused",
                    "dead": "stopped",
                }
                domains[name] = {"state": state_map.get(state.lower(), state.lower())}
    except Exception as e:
        logger.warning("Failed to list container states: %s", e)

    handler._send_json(200, {"containers": domains, "source": "podman"})
```

- [ ] **Step 5: Test troshkad handlers**

Push the updated agent to a connected host and test:
```bash
./scripts/update-agent.sh
# Then via backend API or direct curl to troshkad
```

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py
git commit -m "feat: troshkad container lifecycle handlers — pull, create, start, stop, destroy, states"
```

---

### Task 8: Deploy Service — Container Deploy Steps

**Files:**
- Modify: `src/backend/app/services/deploy_service.py`

**Interfaces:**
- Consumes: Topology JSONB with `containerNode` nodes
- Consumes: Troshkad handlers from Task 7 (`/containers/pull`, `/containers/create`, `/containers/start`, `/containers/destroy`)
- Consumes: Registry credentials from Task 1
- Produces: `_extract_containers()` function
- Produces: `container_pull` and `containers` deploy steps
- Produces: Container teardown in `destroy_project_sync()`

- [ ] **Step 1: Add container_pull and containers to DEPLOY_STEPS**

```python
DEPLOY_STEPS = [
    "eips",
    "networks",
    "seeds",
    "images",
    "container_pull",  # NEW
    "disks",
    "vms",
    "containers",      # NEW
    "starting",
    "dns",
    "done",
]
```

- [ ] **Step 2: Add _extract_containers() function**

Add after `_extract_vms()`:

```python
def _extract_containers(topology: dict) -> list[dict]:
    containers = []
    for node in topology.get("nodes", []):
        if node.get("type") != "containerNode":
            continue
        data = node.get("data", {})
        containers.append(
            {
                "node_id": node["id"],
                "name": data.get("name", "container"),
                "image": data.get("image", ""),
                "registry_credential_id": data.get("registryCredentialId"),
                "registry_credential_name": data.get("registryCredentialName"),
                "cpus": data.get("cpus", 1),
                "memory_mb": data.get("memory", 512),
                "nics": data.get("nics", []),
                "env_vars": data.get("envVars", []),
                "ports": data.get("ports", []),
                "command": data.get("command"),
                "restart_policy": data.get("restartPolicy", "always"),
                "privileged": data.get("privileged", False),
                "mounts": data.get("mounts", []),
            }
        )
    return containers
```

- [ ] **Step 3: Add _find_container_networks() function**

Reuse the exact same edge-walking pattern as `_find_vm_networks()`, but for container nodes. The handle format is identical (`nic-{id}-top/bottom`):

```python
def _find_container_networks(
    container_node_id: str, topology: dict, vni_map: dict, project_id: str = ""
) -> list[dict]:
    # Identical logic to _find_vm_networks — walks edges from container NIC handles to network nodes
    # Returns list of {bridge, mac, nic_id, model, ip, cidr}
    results = []
    container_node = next(
        (n for n in topology.get("nodes", []) if n["id"] == container_node_id), None
    )
    if not container_node:
        return results

    nics_by_id = {nic["id"]: nic for nic in container_node.get("data", {}).get("nics", [])}

    for edge in topology.get("edges", []):
        src, tgt = edge.get("source"), edge.get("target")
        src_h, tgt_h = edge.get("sourceHandle", ""), edge.get("targetHandle", "")

        nic_id = None
        net_node_id = None
        if src == container_node_id and src_h.startswith("nic-"):
            nic_id = src_h.split("-", 1)[1].rsplit("-", 1)[0]
            net_node_id = tgt
        elif tgt == container_node_id and tgt_h.startswith("nic-"):
            nic_id = tgt_h.split("-", 1)[1].rsplit("-", 1)[0]
            net_node_id = src

        if not nic_id or not net_node_id:
            continue

        nic = nics_by_id.get(nic_id, {})
        vni = vni_map.get(net_node_id)
        if not vni:
            continue

        net_node = next(
            (n for n in topology.get("nodes", []) if n["id"] == net_node_id), None
        )
        cidr = net_node.get("data", {}).get("cidr", "") if net_node else ""

        results.append({
            "bridge": f"br-{vni}",
            "mac": nic.get("mac", ""),
            "nic_id": nic_id,
            "model": nic.get("model", "virtio"),
            "ip": nic.get("ip", ""),
            "cidr": cidr,
        })

    return results
```

- [ ] **Step 4: Add _find_container_volumes() function**

```python
def _find_container_volumes(
    container_node_id: str, topology: dict, project_id: str, pool=None
) -> list[dict]:
    container_node = next(
        (n for n in topology.get("nodes", []) if n["id"] == container_node_id), None
    )
    if not container_node:
        return []

    mounts = container_node.get("data", {}).get("mounts", [])
    mounts_by_disk = {m["diskNodeId"]: m for m in mounts}

    results = []
    for edge in topology.get("edges", []):
        src, tgt = edge.get("source"), edge.get("target")
        src_h, tgt_h = edge.get("sourceHandle", ""), edge.get("targetHandle", "")

        disk_node_id = None
        if src == container_node_id and (tgt_h or "").startswith("mnt-"):
            disk_node_id = tgt
        elif tgt == container_node_id and (src_h or "").startswith("mnt-"):
            disk_node_id = src
        elif tgt == container_node_id and (tgt_h or "").startswith("mnt-"):
            disk_node_id = src
        elif src == container_node_id and (src_h or "").startswith("mnt-"):
            disk_node_id = tgt

        if not disk_node_id:
            continue

        disk_node = next(
            (n for n in topology.get("nodes", []) if n["id"] == disk_node_id and n.get("type") == "storageNode"),
            None,
        )
        if not disk_node:
            continue

        mount_info = mounts_by_disk.get(disk_node_id, {})
        dd = disk_node.get("data", {})
        disk_path = _disk_path(project_id, container_node_id, disk_node_id, "raw", pool)
        mount_dir = os.path.join(
            _vm_dir(project_id, pool), f"mnt-{disk_node_id[:8]}"
        )
        results.append({
            "disk_path": disk_path,
            "mount_dir": mount_dir,
            "mount_path": mount_info.get("mountPath", "/data"),
            "size_gb": dd.get("size", 10),
            "node_id": disk_node_id,
        })

    return results
```

- [ ] **Step 5: Add container_pull step to deploy loop**

In the main deploy loop, after the `images` step and before `disks`:

```python
if step == "container_pull":
    containers = _extract_containers(topology)
    if not containers:
        continue
    _update_progress(project_id, step="container_pull", detail="Pulling container images...")
    for ctr in containers:
        if not ctr["image"]:
            continue
        pull_params = {"image": ctr["image"]}

        # Resolve registry credentials
        cred_id = ctr.get("registry_credential_id")
        if cred_id:
            from app.models.registry_credential import RegistryCredential
            from app.core.encryption import decrypt
            cred = session.query(RegistryCredential).filter_by(id=cred_id).first()
            if cred:
                pull_params["registry"] = cred.registry
                pull_params["username"] = cred.username
                pull_params["password"] = decrypt(cred.password)

        job_id = start_job(host, "/containers/pull", pull_params)
        wait_for_job(host, job_id, timeout=600)
    _checkpoint(session, project_id, "container_pull")
```

- [ ] **Step 6: Add containers step to deploy loop**

After the `vms` step:

```python
if step == "containers":
    containers = _extract_containers(topology)
    if not containers:
        continue
    _update_progress(project_id, step="containers", detail="Creating containers...")

    # Respect start order for containers
    start_order = topology.get("startOrder", [])
    ordered_ids = set()
    for entry in start_order:
        if entry.get("entryType") == "container":
            ctr_id = entry.get("containerId", entry.get("vmId", ""))
            ctr = next((c for c in containers if c["node_id"] == ctr_id), None)
            if ctr:
                ordered_ids.add(ctr_id)
                delay = entry.get("delaySeconds", 0)
                if delay > 0:
                    _time.sleep(delay)
                _create_and_start_container(host, project_id, ctr, topology, vni_map, pool)

    # Create any containers not in start order
    for ctr in containers:
        if ctr["node_id"] not in ordered_ids:
            _create_and_start_container(host, project_id, ctr, topology, vni_map, pool)

    _checkpoint(session, project_id, "containers")
```

- [ ] **Step 7: Add _create_and_start_container helper**

```python
def _create_and_start_container(host, project_id, ctr, topology, vni_map, pool=None):
    container_name = f"troshka-{project_id[:8]}-{ctr['node_id'][:8]}"
    networks = _find_container_networks(ctr["node_id"], topology, vni_map, project_id)
    volumes = _find_container_volumes(ctr["node_id"], topology, project_id, pool)

    create_params = {
        "container_name": container_name,
        "image": ctr["image"],
        "cpus": ctr["cpus"],
        "memory_mb": ctr["memory_mb"],
        "env_vars": ctr["env_vars"],
        "ports": ctr["ports"],
        "networks": [
            {"bridge": n["bridge"], "ip": n.get("ip"), "mac": n.get("mac"), "cidr": n.get("cidr")}
            for n in networks
        ],
        "volumes": [
            {"disk_path": v["disk_path"], "mount_dir": v["mount_dir"], "mount_path": v["mount_path"]}
            for v in volumes
        ],
        "command": ctr.get("command"),
        "restart_policy": ctr.get("restart_policy", "always"),
        "privileged": ctr.get("privileged", False),
    }
    job_id = start_job(host, "/containers/create", create_params)
    wait_for_job(host, job_id, timeout=120)

    job_id = start_job(host, "/containers/start", {"container_name": container_name})
    wait_for_job(host, job_id, timeout=30)
```

- [ ] **Step 8: Add container teardown to destroy_project_sync**

In `destroy_project_sync()`, before the VM destroy step:

```python
# Destroy containers first (before networks teardown)
containers = _extract_containers(topology)
for ctr in containers:
    container_name = f"troshka-{project_id[:8]}-{ctr['node_id'][:8]}"
    volumes = _find_container_volumes(ctr["node_id"], topology, project_id, pool)
    try:
        job_id = start_job(
            host,
            "/containers/destroy",
            {
                "container_name": container_name,
                "project_id": project_id,
                "volumes": [{"mount_dir": v["mount_dir"]} for v in volumes],
            },
        )
        wait_for_job(host, job_id, timeout=30)
    except Exception as e:
        logger.warning("Failed to destroy container %s: %s", container_name, e)
```

- [ ] **Step 9: Test deploy with containers**

Create a project with a container (via template import or manual canvas), deploy it, verify:
- Container image is pulled
- Container disk volumes are created and formatted
- Container is created and started on the host
- Container is visible via `podman ps` on the host
- Undeploy cleans up containers before networks

- [ ] **Step 10: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/deploy_service.py
git add src/backend/app/services/deploy_service.py
git commit -m "feat: deploy pipeline with container pull, create, start, and teardown steps"
```

---

### Task 9: Pattern Save/Restore for Container Images

**Files:**
- Modify: `src/backend/app/services/pattern_service.py`
- Modify: `src/troshkad/troshkad.py`

**Interfaces:**
- Consumes: Topology with containerNode nodes
- Consumes: Troshkad `/containers/save-image` and `/containers/load-image` handlers (added here)
- Produces: Container images saved as `.tar.gz` to S3 alongside VM disks
- Produces: Container images restored from S3 during pattern deploy

- [ ] **Step 1: Add container save-image handler to troshkad**

```python
def _handle_container_save_image(job, params):
    image = params["image"]
    output_path = _validate_path(params["output_path"])

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    _job_log(job, f"Saving container image {image}...")

    # podman save | gzip > output.tar.gz (streaming, no intermediate file)
    cmd = f"podman save {image} | gzip > {output_path}"
    proc = subprocess.Popen(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    job["_process"] = proc
    try:
        _, stderr = proc.communicate(timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f"Image save timed out: {image}")
    finally:
        job["_process"] = None

    if proc.returncode != 0:
        raise RuntimeError(f"podman save failed: {stderr}")

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    _job_log(job, f"Saved {image} ({size_mb:.1f} MB)")
    return {"output_path": output_path, "size_bytes": os.path.getsize(output_path)}

COMMAND_HANDLERS["containers/save-image"] = _handle_container_save_image
```

- [ ] **Step 2: Add container load-image handler to troshkad**

```python
def _handle_container_load_image(job, params):
    input_path = _validate_path(params["input_path"])

    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Image file not found: {input_path}")

    _job_log(job, f"Loading container image from {os.path.basename(input_path)}...")
    cmd = f"gunzip -c {input_path} | podman load"
    proc = subprocess.Popen(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    job["_process"] = proc
    try:
        stdout, stderr = proc.communicate(timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f"Image load timed out: {input_path}")
    finally:
        job["_process"] = None

    if proc.returncode != 0:
        raise RuntimeError(f"podman load failed: {stderr}")

    if stdout:
        _job_log(job, stdout.strip())
    return {"input_path": input_path, "status": "loaded"}

COMMAND_HANDLERS["containers/load-image"] = _handle_container_load_image
```

- [ ] **Step 3: Add container image capture to pattern_service**

In `capture_pattern_disks()` (or a separate function called alongside it), after VM disk capture, save container images:

```python
# Capture container images
containers = _extract_containers(topology)
for ctr in containers:
    if not ctr["image"]:
        continue
    ctr_id = ctr["node_id"]
    tar_filename = f"container-{ctr_id[:8]}-image.tar.gz"
    save_path = f"/var/lib/troshka/local/cache/patterns/{pattern_id}/{tar_filename}"
    s3_key = f"patterns/{pattern_id}/{tar_filename}"

    # Save image to local tar.gz
    job_id = start_job(host, "/containers/save-image", {
        "image": ctr["image"],
        "output_path": save_path,
    })
    wait_for_job(host, job_id, timeout=600)

    # Upload to S3 (reuse existing upload pattern)
    job_id = start_job(host, "/patterns/upload-and-cache", {
        "local_path": save_path,
        "s3_bucket": s3_bucket,
        "s3_key": s3_key,
        "cache_path": save_path,
        "aws_access_key_id": creds.get("access_key_id", ""),
        "aws_secret_access_key": creds.get("secret_access_key", ""),
        "aws_region": creds.get("region", "us-east-1"),
    })
    wait_for_job(host, job_id, timeout=1200)
```

- [ ] **Step 4: Add container image restore to pattern deploy**

In the deploy pipeline, during the `container_pull` step, check if deploying from a pattern. If so, download the tar.gz from S3 and load via `podman load` instead of pulling from a registry:

```python
if _is_pattern_deploy(topology):
    # Load from pattern cache instead of pulling
    tar_filename = f"container-{ctr['node_id'][:8]}-image.tar.gz"
    cache_path = f"/var/lib/troshka/local/cache/patterns/{pattern_id}/{tar_filename}"
    s3_key = f"patterns/{pattern_id}/{tar_filename}"

    # Download from S3 if not cached
    job_id = start_job(host, "/images/cache", {
        "url": f"s3://{s3_bucket}/{s3_key}",
        "cache_path": cache_path,
        "aws_access_key_id": creds.get("access_key_id", ""),
        "aws_secret_access_key": creds.get("secret_access_key", ""),
        "aws_region": creds.get("region", "us-east-1"),
    })
    wait_for_job(host, job_id, timeout=600)

    # Load image from tar.gz
    job_id = start_job(host, "/containers/load-image", {"input_path": cache_path})
    wait_for_job(host, job_id, timeout=300)
else:
    # Normal pull from registry
    # ... existing pull logic
```

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/pattern_service.py
git add src/troshkad/troshkad.py src/backend/app/services/pattern_service.py
git commit -m "feat: pattern save/restore captures container images as tar.gz to S3"
```

---

### Task 10: Troshkad Container Logs + Exec Handlers

**Files:**
- Modify: `src/troshkad/troshkad.py`

**Interfaces:**
- Produces: `/containers/logs` command handler (returns log output)
- Produces: `/containers/exec-ws` WebSocket endpoint (PTY stream)

- [ ] **Step 1: Add container logs handler**

```python
def _handle_container_logs(job, params):
    name = params["container_name"]
    tail = params.get("tail", 500)

    _job_log(job, f"Fetching logs for {name}...")
    result = subprocess.run(
        ["podman", "logs", "--tail", str(tail), name],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get logs: {result.stderr}")

    return {"logs": result.stdout, "container_name": name}

COMMAND_HANDLERS["containers/logs"] = _handle_container_logs
```

- [ ] **Step 2: Add container exec WebSocket endpoint**

This is a direct GET route (not a job) that upgrades to WebSocket. It uses the same PTY pattern as serial exec:

```python
@route("GET", "/containers/exec-ws")
def handle_container_exec_ws(handler, params):
    name = params.get("container_name", "")
    if not name:
        handler._send_json(400, {"error": "container_name required"})
        return

    # Verify container exists and is running
    result = subprocess.run(
        ["podman", "inspect", "--format", "{{.State.Status}}", name],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0 or result.stdout.strip() != "running":
        handler._send_json(400, {"error": f"Container {name} is not running"})
        return

    # Determine shell
    shell = "/bin/sh"
    for candidate in ["/bin/bash", "/bin/sh"]:
        check = subprocess.run(
            ["podman", "exec", name, "test", "-x", candidate],
            capture_output=True, timeout=5,
        )
        if check.returncode == 0:
            shell = candidate
            break

    # Upgrade to WebSocket and spawn PTY
    # Implementation depends on the existing WebSocket upgrade pattern in troshkad
    # This follows the same approach as the serial console exec
    handler._send_json(200, {"shell": shell, "status": "ready"})
```

Note: The full WebSocket upgrade + PTY relay is complex and should follow whatever pattern the existing serial exec or vncd uses. The exact implementation depends on the HTTP server's WebSocket support in troshkad.

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py
git commit -m "feat: troshkad container logs and exec handlers"
```

---

### Task 11: Container Console — Backend API + Frontend UI

**Files:**
- Modify: `src/backend/app/api/` — Add container logs/exec API endpoints
- Create or modify: Frontend container console components

**Interfaces:**
- Consumes: Troshkad `/containers/logs` and `/containers/exec-ws` from Task 10
- Produces: `GET /api/v1/projects/{id}/containers/{container_id}/logs` API endpoint
- Produces: Frontend log viewer and terminal UI

- [ ] **Step 1: Add container logs API endpoint**

Add to the project routes or create a new container routes file:

```python
@router.get("/projects/{project_id}/containers/{container_id}/logs")
def get_container_logs(
    project_id: str,
    container_id: str,
    tail: int = 500,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = _get_project(db, user, project_id)
    host = _get_project_host(db, project)
    container_name = f"troshka-{project_id[:8]}-{container_id[:8]}"

    job_id = start_job(host, "/containers/logs", {
        "container_name": container_name,
        "tail": tail,
    })
    result = wait_for_job(host, job_id, timeout=30)
    return {"logs": result.get("result", {}).get("logs", ""), "container_name": container_name}
```

- [ ] **Step 2: Add log viewer to frontend**

Add a "Logs" button to the container node right-click context menu and/or properties panel. When clicked, fetch logs and display in a modal or panel:

```tsx
const [containerLogs, setContainerLogs] = useState<string | null>(null);

const fetchLogs = async (containerId: string) => {
  const resp = await fetch(
    `/api/v1/projects/${projectId}/containers/${containerId}/logs?tail=500`
  );
  const data = await resp.json();
  setContainerLogs(data.logs);
};

// In the modal/panel:
{containerLogs !== null && (
  <div className="container-logs-panel">
    <pre style={{ fontFamily: "monospace", fontSize: 11, whiteSpace: "pre-wrap", maxHeight: 400, overflow: "auto" }}>
      {containerLogs}
    </pre>
  </div>
)}
```

- [ ] **Step 3: Test logs in browser**

Deploy a project with a container, then view logs from the UI.

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/ src/frontend/src/
git commit -m "feat: container logs API endpoint and frontend log viewer"
```

Note: The full interactive terminal (xterm.js + WebSocket exec) is a substantial follow-up that can be implemented after the core container functionality is validated. The log viewer provides immediate debugging capability.

---

## Dependency Graph

```
Task 1 (Backend: Registry Credentials)
  └── Task 2 (Frontend: Settings UI)

Task 3 (Canvas: Data Model + Palette + Node)
  └── Task 4 (Canvas: Properties + Validation + Start Order)

Task 5 (Backend: Template Import) ← depends on Task 3 data model knowledge
  └── Task 6 (Backend: Template Export)

Task 7 (Agent: Container Lifecycle) ← independent
  └── Task 8 (Backend: Deploy Pipeline) ← depends on Task 7
      └── Task 9 (Pattern Save/Restore) ← depends on Task 7, Task 8

Task 10 (Agent: Logs + Exec) ← depends on Task 7
  └── Task 11 (Console: API + Frontend) ← depends on Task 10
```

Tasks 1-2, 3-4, 5-6, 7 can all proceed in parallel.
Tasks 8 depends on 7. Task 9 depends on 7+8. Tasks 10-11 depend on 7.
