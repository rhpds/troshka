# Troshka Workloads — Plan 2: Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver an API-driven, end-to-end workload run: given an active project and a catalog item (or ad-hoc role), Troshka resolves it (Plan 1), mints cluster access, emits an inventory, launches an in-project runner pod whose image IS the EE, streams progress, and records the run.

**Architecture:** Approach A / thin pod. The backend (worker) resolves everything server-side (Plan 1 resolver + new cluster-access + inventory), then launches a runner pod whose **container image is the resolved public EE image**, with the AgnosticD-v2 checkout, extra-vars, inventory, cluster-access, and cloud creds delivered as read-only 0600 mounts; the pod runs `ansible-playbook` directly (no navigator, no nested podman). Reuses the existing pod-launch primitives (`troshkad /pods/create` + `_write_pod_files`; KubeVirt `create_ops_pod`) and the RQ job/monitor/progress machinery, without modifying the working OCP install path.

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 + Alembic, RQ + Redis, the Kubernetes Python client (`TokenRequest`), FastAPI, Dynaconf. Tests: pytest with SQLite (`tests/conftest.py`), all external I/O mocked.

**Spec:** `docs/superpowers/specs/2026-09-11-troshka-workloads-design.md`

**Depends on:** Plan 1 (merged) — `app/services/workloads/{secret_store,vault,repo_cache,agnosticv,resolver}.py`. In particular `resolver.resolve_catalog_item(db, catalog_id) -> agnosticv.ResolvedItem` and `repo_cache.ensure_repo(repo_key, git_url, ref)`.

## Global Constraints

- **Python 3.13.** Add trailing values to any `time.time()` mocks to avoid `StopIteration`.
- **Run tests** from `src/backend`: `./venv/bin/python3 -m pytest <path> -v`.
- **Format** with system `black`; fix all `pyright` errors in touched files (`pyright <files>` from `src/backend` → 0 errors, 0 warnings). Test output pristine: no unused imports; `_`-prefix intentionally-unused loop/lambda vars; guard every Optional member access with `assert ... is not None`.
- **Cognitive complexity ≤ 15 per function** (SonarQube S3776) — extract helpers.
- **Secrets never in argv or logs.** All run artifacts (extra-vars, inventory with its `trk_` key, cluster-access tokens, cloud creds) reach the pod ONLY as read-only 0600 file mounts (troshkad `files` map) or a read-only k8s Secret (KubeVirt) — never in the container command or env (the scoped API key is the sole exception, via env, matching the ops-pod precedent).
- **Do NOT modify the OCP install path.** Do not edit `_ops_pod_create_params`, `build_ops_pod_kubevirt_manifests`, `_ops_pod_command`, or the ops-pod monitor. Add new generic runner functions instead; reuse only the low-level primitives (`troshkad_client.start_job`/`poll_job`, the `/pods/create` `files` mechanism, `providers/kubevirt.create_ops_pod`, `mint`-style key helpers, `_update_deploy_progress`, Redis lock helpers).
- **Monitors are enqueued RQ jobs, not daemon threads** (a fork-child thread dies when the parent job returns — see `deploy_service.py:2484-2494`), guarded by a per-run Redis lock, with a `resume_workload_monitors()` called from `deploy_worker.run_worker()`. Long loops use a session-detached copy and never hold a `Session`.
- **Models:** `Mapped[...]` + `mapped_column`; UUID PK `UUID(as_uuid=False), default=lambda: str(uuid.uuid4())`; FK `postgresql.UUID(as_uuid=False)` + `ondelete`; JSONB via `sqlalchemy.dialects.postgresql.JSONB`; register in `app/models/__init__.py` (import + `__all__`). Migrations auto-run on startup; chain `down_revision` to the current head **`6bc52066a164`** (verify with `./venv/bin/python3 -m alembic heads`).
- **Never `drop_all`** the shared test engine.
- **Git:** no `Co-Authored-By`; never amend; commit from repo root (`cd /Users/prutledg/troshka && git add src/backend/...`) — never `cd` into a subdir then `git add` a relative path. Work on a branch `feat/workloads-plan2` (not `main`).

## File Structure

New (all under `src/backend/`):
- `app/models/workload_run.py` — the `WorkloadRun` model.
- `alembic/versions/<rev>_add_workload_runs.py` — migration.
- `app/services/workloads/cluster_access.py` — resolve `{api_url, api_token}` per cluster via SA-token mint.
- `app/services/workloads/inventory.py` — build `*.troshka.yml` + validate the `AnsibleGroup` contract.
- `app/services/workloads/run_key.py` — mint a per-run scoped `trk_` key (`topology:read`, `vm:exec`, `cluster:access`).
- `app/services/workloads/pod_launch.py` — build + launch the runner pod (troshkad + KubeVirt), image = EE, artifacts as mounts.
- `app/services/workloads/run_service.py` — orchestration: create run, artifact assembly, the enqueued run job, the monitor, `resume_workload_monitors`, pruning.
- `app/api/workloads.py` — trigger/status/list router.
- Tests: `tests/test_workload_run_model.py`, `test_workload_cluster_access.py`, `test_workload_inventory.py`, `test_workload_run_key.py`, `test_workload_pod_launch.py`, `test_workload_run_service.py`, `test_workload_api.py`, `test_workload_cluster_access_endpoint.py`, `test_workload_pruning.py`.

Modified:
- `app/models/__init__.py` — register `WorkloadRun`.
- `app/api/projects.py` — add `get_cluster_access` endpoint.
- `app/core/auth.py` — add `"get_cluster_access": "cluster:access"` to `_SCOPED_KEY_ROUTE_ALLOWLIST`.
- `app/workers/jobs.py` — `job_run_workload`, `job_workload_monitor` entrypoints.
- `app/workers/deploy_worker.py` — call `resume_workload_monitors()` at startup.
- `app/main.py` — register the workloads router; start the pruning timer in `lifespan`.

---

### Task 1: `WorkloadRun` model + migration

**Files:**
- Create: `src/backend/app/models/workload_run.py`
- Modify: `src/backend/app/models/__init__.py`
- Create: `src/backend/alembic/versions/<rev>_add_workload_runs.py`
- Test: `src/backend/tests/test_workload_run_model.py`

**Interfaces:**
- Produces: `class WorkloadRun(Base)` with columns: `id: str` (UUID PK), `project_id: str | None` (FK `projects.id`, `ondelete="SET NULL"`, nullable, indexed), `owner_id: str | None`, `kind: str` (`"catalog_item"|"ad_hoc"`), `catalog_item: str | None`, `role_fqcn: str | None`, `scm_ref: str | None`, `ee_image: str | None`, `target_map` (JSONB, nullable), `status: str` (default `"pending"`), `error: str | None` (Text), `log_ref: str | None`, `resulting_pattern_id: str | None`, `created_at`, `started_at`, `ended_at`, `updated_at`.

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_workload_run_model.py
from app.models.workload_run import WorkloadRun
from tests.conftest import TestSession


def test_workload_run_defaults_and_persist():
    db = TestSession()
    try:
        run = WorkloadRun(
            kind="catalog_item",
            catalog_item="agd-v2.mcp-with-openshift.prod",
            ee_image="quay.io/agnosticd/ee-multicloud:x",
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        assert run.id  # uuid string assigned
        assert run.status == "pending"
        assert run.created_at is not None
        assert run.project_id is None
    finally:
        db.close()


def test_workload_run_target_map_jsonb():
    db = TestSession()
    try:
        run = WorkloadRun(kind="ad_hoc", role_fqcn="agnosticd.core_workloads.ocp4_workload_gitea_operator",
                          target_map={"clusters": {"default": "cl-1"}})
        db.add(run)
        db.commit()
        db.refresh(run)
        assert run.target_map["clusters"]["default"] == "cl-1"
    finally:
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python3 -m pytest tests/test_workload_run_model.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.workload_run'`.

- [ ] **Step 3: Implement the model + register it**

```python
# src/backend/app/models/workload_run.py
from __future__ import annotations

import uuid

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WorkloadRun(Base):
    __tablename__ = "workload_runs"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    owner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))  # catalog_item | ad_hoc
    catalog_item: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role_fqcn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scm_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ee_image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    target_map: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    resulting_pattern_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), nullable=True
    )
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )
```

In `src/backend/app/models/__init__.py`: add `from app.models.workload_run import WorkloadRun` with the other imports and `"WorkloadRun"` to `__all__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python3 -m pytest tests/test_workload_run_model.py -v`
Expected: PASS (2 passed). (SQLite maps JSONB/UUID via the conftest type-compiler overrides.)

- [ ] **Step 5: Write the migration**

Determine the head: `./venv/bin/python3 -m alembic heads` (expect `6bc52066a164`). Create `src/backend/alembic/versions/<newrev>_add_workload_runs.py` with `down_revision = "6bc52066a164"` (use the real head if it differs) and:

```python
"""add workload_runs

Revision ID: <newrev>
Revises: 6bc52066a164
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "<newrev>"
down_revision = "6bc52066a164"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workload_runs",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True),
        sa.Column("owner_id", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("catalog_item", sa.String(255), nullable=True),
        sa.Column("role_fqcn", sa.String(255), nullable=True),
        sa.Column("scm_ref", sa.String(255), nullable=True),
        sa.Column("ee_image", sa.String(512), nullable=True),
        sa.Column("target_map", postgresql.JSONB, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("log_ref", sa.Text, nullable=True),
        sa.Column("resulting_pattern_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_workload_runs_project_id", "workload_runs", ["project_id"])


def downgrade():
    op.drop_index("ix_workload_runs_project_id", table_name="workload_runs")
    op.drop_table("workload_runs")
```

- [ ] **Step 6: Format, type-check, commit**

```bash
cd /Users/prutledg/troshka/src/backend && black app/models/workload_run.py tests/test_workload_run_model.py alembic/versions/*add_workload_runs.py && pyright app/models/workload_run.py
cd /Users/prutledg/troshka && git add src/backend/app/models/workload_run.py src/backend/app/models/__init__.py src/backend/tests/test_workload_run_model.py src/backend/alembic/versions/
git commit -m "feat(workloads): add WorkloadRun model + migration"
```

---

### Task 2: Cluster Access resolver (SA-token mint)

Given an active OCP project and a target cluster, produce `{api_url, api_token}`: read the stored admin kubeconfig from topology, bootstrap a k8s client, ensure a `cluster-admin` ServiceAccount + binding, and mint a bearer token via `TokenRequest`.

**Files:**
- Create: `src/backend/app/services/workloads/cluster_access.py`
- Test: `src/backend/tests/test_workload_cluster_access.py`

**Interfaces:**
- Consumes: `deploy_service._stored_cluster_creds(topology) -> dict[str, tuple[str, str]]` (clusterId → (kubeadmin_pw, kubeconfig)); `kubernetes` client.
- Produces:
  - `class ClusterAccessError(Exception)`
  - `resolve_cluster_access(project, cluster_id: str | None = None) -> dict[str, dict]` → `{clusterName: {"api_url": str, "api_token": str}}`. When `cluster_id` is None, resolves all clusters found in the project's stored creds.
  - `_mint_admin_token(kubeconfig_str: str, *, sa_name="troshka-workloads-admin", namespace="default", ttl_seconds=3600) -> tuple[str, str]` → `(api_url, token)` (split out for unit testing).

- [ ] **Step 1: Write the failing test** (mock the k8s client entirely — no real cluster)

```python
# src/backend/tests/test_workload_cluster_access.py
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.workloads import cluster_access

_KUBECONFIG = """
apiVersion: v1
clusters:
- cluster: {server: https://api.cl.example.com:6443}
  name: cl
contexts:
- context: {cluster: cl, user: admin}
  name: admin
current-context: admin
users:
- name: admin
  user: {token: bootstrap-tok}
"""


def test_mint_admin_token_calls_tokenrequest(monkeypatch):
    fake_core = MagicMock()
    fake_core.create_namespaced_service_account_token.return_value = SimpleNamespace(
        status=SimpleNamespace(token="minted-sa-token")
    )
    monkeypatch.setattr(cluster_access, "_core_v1_from_kubeconfig",
                        lambda kc: (fake_core, "https://api.cl.example.com:6443"))
    monkeypatch.setattr(cluster_access, "_ensure_admin_sa", lambda core, sa, ns: None)

    api_url, token = cluster_access._mint_admin_token(_KUBECONFIG)
    assert api_url == "https://api.cl.example.com:6443"
    assert token == "minted-sa-token"
    assert fake_core.create_namespaced_service_account_token.called


def test_resolve_cluster_access_maps_by_cluster_name(monkeypatch):
    project = SimpleNamespace(
        deployed_topology={"nodes": []}, topology={"nodes": []},
    )
    monkeypatch.setattr(
        cluster_access, "_stored_cluster_creds",
        lambda topo: {"cl-1": ("pw", _KUBECONFIG)},
    )
    monkeypatch.setattr(
        cluster_access, "_mint_admin_token",
        lambda kc, **kw: ("https://api.cl.example.com:6443", "tok-1"),
    )
    out = cluster_access.resolve_cluster_access(project)
    assert out["cl-1"]["api_url"].endswith(":6443")
    assert out["cl-1"]["api_token"] == "tok-1"


def test_resolve_cluster_access_no_clusters_raises(monkeypatch):
    project = SimpleNamespace(deployed_topology=None, topology={"nodes": []})
    monkeypatch.setattr(cluster_access, "_stored_cluster_creds", lambda topo: {})
    with pytest.raises(cluster_access.ClusterAccessError):
        cluster_access.resolve_cluster_access(project)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python3 -m pytest tests/test_workload_cluster_access.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# src/backend/app/services/workloads/cluster_access.py
"""Resolve per-cluster {api_url, api_token} for OCP workloads.

Bootstraps a Kubernetes client from the stored admin kubeconfig (persisted on
the control-plane node in Project.topology by the OCP install / recert flow),
ensures a cluster-admin ServiceAccount, and mints a short-lived bearer token via
the TokenRequest API — mirroring agnosticd's openshift_cluster_admin_service_account.
"""

from __future__ import annotations

import yaml

from app.services.deploy_service import _stored_cluster_creds


class ClusterAccessError(Exception):
    pass


def _core_v1_from_kubeconfig(kubeconfig_str: str):
    from kubernetes import client, config

    data = yaml.safe_load(kubeconfig_str)
    api_client = config.new_client_from_config_dict(data)
    core = client.CoreV1Api(api_client)
    host = api_client.configuration.host
    return core, host


def _ensure_admin_sa(core, sa_name: str, namespace: str) -> None:
    from kubernetes import client
    from kubernetes.client.rest import ApiException

    try:
        core.create_namespaced_service_account(
            namespace, client.V1ServiceAccount(
                metadata=client.V1ObjectMeta(name=sa_name)))
    except ApiException as exc:
        if exc.status != 409:  # already exists is fine
            raise
    rbac = client.RbacAuthorizationV1Api(core.api_client)
    binding = client.V1ClusterRoleBinding(
        metadata=client.V1ObjectMeta(name=f"{sa_name}-cluster-admin"),
        role_ref=client.V1RoleRef(
            api_group="rbac.authorization.k8s.io", kind="ClusterRole",
            name="cluster-admin"),
        subjects=[client.V1Subject(
            kind="ServiceAccount", name=sa_name, namespace=namespace)],
    )
    try:
        rbac.create_cluster_role_binding(binding)
    except ApiException as exc:
        if exc.status != 409:
            raise


def _mint_admin_token(kubeconfig_str: str, *, sa_name: str = "troshka-workloads-admin",
                      namespace: str = "default", ttl_seconds: int = 3600) -> tuple[str, str]:
    from kubernetes import client

    core, host = _core_v1_from_kubeconfig(kubeconfig_str)
    _ensure_admin_sa(core, sa_name, namespace)
    req = client.AuthenticationV1TokenRequest(
        spec=client.V1TokenRequestSpec(expiration_seconds=ttl_seconds, audiences=[]))
    resp = core.create_namespaced_service_account_token(sa_name, namespace, req)
    return host, resp.status.token


def resolve_cluster_access(project, cluster_id: str | None = None) -> dict[str, dict]:
    topology = project.deployed_topology or project.topology or {}
    creds = _stored_cluster_creds(topology)
    if cluster_id is not None:
        creds = {k: v for k, v in creds.items() if k == cluster_id}
    if not creds:
        raise ClusterAccessError("no stored cluster credentials for project")
    out: dict[str, dict] = {}
    for cid, (_pw, kubeconfig) in creds.items():
        api_url, token = _mint_admin_token(kubeconfig)
        out[cid] = {"api_url": api_url, "api_token": token}
    return out
```

Note: if `_stored_cluster_creds` import creates a heavy import chain at module load, import it lazily inside `resolve_cluster_access` instead (match whichever keeps `pyright` + import time clean; the test monkeypatches `cluster_access._stored_cluster_creds`, so it must be a module-level name — if you import lazily, also bind `_stored_cluster_creds = None` at module scope and set it in the function, or keep the top-level import).

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python3 -m pytest tests/test_workload_cluster_access.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Format, type-check, commit**

```bash
cd /Users/prutledg/troshka/src/backend && black app/services/workloads/cluster_access.py tests/test_workload_cluster_access.py && pyright app/services/workloads/cluster_access.py tests/test_workload_cluster_access.py
cd /Users/prutledg/troshka && git add src/backend/app/services/workloads/cluster_access.py src/backend/tests/test_workload_cluster_access.py
git commit -m "feat(workloads): cluster-admin SA token minting for cluster access"
```

---

### Task 3: `cluster:access` scope + `GET /projects/{id}/cluster-access` + run key

**Files:**
- Create: `src/backend/app/services/workloads/run_key.py`
- Modify: `src/backend/app/api/projects.py` (add `get_cluster_access`)
- Modify: `src/backend/app/core/auth.py` (allowlist entry)
- Test: `src/backend/tests/test_workload_run_key.py`, `src/backend/tests/test_workload_cluster_access_endpoint.py`

**Interfaces:**
- Consumes: `cluster_access.resolve_cluster_access`; `app.models.api_key` helpers; `ocp_pod_auth` style minting.
- Produces:
  - `run_key.mint_run_key(db, project) -> str` → raw `trk_...` secret; creates an `ApiKey` with `project_id=project.id`, `scopes=["topology:read","vm:exec","cluster:access"]`, name `workload-run:{project_id}`.
  - `run_key.revoke_run_key(db, project) -> None`.
  - Endpoint `GET /api/projects/{project_id}/cluster-access` (route fn `get_cluster_access`) → `{clusters: {name: {api_url, api_token}}}`.

- [ ] **Step 1: Write the failing tests**

```python
# src/backend/tests/test_workload_run_key.py
from app.models.api_key import ApiKey, hash_key
from app.models.provider import Provider
from app.models.project import Project
from app.services.workloads import run_key
from tests.conftest import TestSession


def _project(db):
    prov = Provider(name="p", type="troshka", credentials="{}")
    db.add(prov); db.flush()
    proj = Project(name="rk", owner_id="u1", provider_id=prov.id)
    db.add(proj); db.commit()
    return proj


def test_mint_run_key_has_expected_scopes():
    db = TestSession()
    try:
        proj = _project(db)
        raw = run_key.mint_run_key(db, proj)
        assert raw.startswith("trk_")
        row = db.query(ApiKey).filter_by(key_hash=hash_key(raw)).one()
        assert row.project_id == proj.id
        assert set(row.scopes) == {"topology:read", "vm:exec", "cluster:access"}
    finally:
        db.close()
```

```python
# src/backend/tests/test_workload_cluster_access_endpoint.py
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.models.provider import Provider
from app.models.project import Project
from tests.conftest import TestSession, get_test_db
from app.core.database import get_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)


def _project(db):
    prov = Provider(name="p2", type="troshka", credentials="{}")
    db.add(prov); db.flush()
    proj = Project(name="ca-ep", owner_id="u1", provider_id=prov.id)
    db.add(proj); db.commit()
    return proj


def test_get_cluster_access_returns_clusters():
    db = TestSession()
    proj = _project(db); pid = proj.id; db.close()
    with patch("app.api.projects.resolve_cluster_access",
               return_value={"cl-1": {"api_url": "https://x:6443", "api_token": "t"}}):
        # dev mode auto-authenticates as admin
        resp = client.get(f"/api/projects/{pid}/cluster-access")
    assert resp.status_code == 200
    assert resp.json()["clusters"]["cl-1"]["api_token"] == "t"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/python3 -m pytest tests/test_workload_run_key.py tests/test_workload_cluster_access_endpoint.py -v`
Expected: FAIL — `run_key` module missing; endpoint 404.

- [ ] **Step 3: Implement run_key**

```python
# src/backend/app/services/workloads/run_key.py
"""Mint a per-project scoped API key for the workload runner pod."""

from sqlalchemy.orm import Session

from app.models.api_key import ApiKey, generate_api_key, hash_key

RUN_KEY_SCOPES = ["topology:read", "vm:exec", "cluster:access"]


def _key_name(project_id: str) -> str:
    return f"workload-run:{project_id}"


def revoke_run_key(db: Session, project) -> None:
    rows = db.query(ApiKey).filter_by(name=_key_name(project.id)).all()
    for row in rows:
        row.is_active = False
    if rows:
        db.commit()


def mint_run_key(db: Session, project) -> str:
    revoke_run_key(db, project)
    raw = generate_api_key()
    key = ApiKey(
        user_id=project.owner_id,
        name=_key_name(project.id),
        key_hash=hash_key(raw),
        key_prefix=raw[:12],
        project_id=project.id,
        scopes=RUN_KEY_SCOPES,
        is_active=True,
    )
    db.add(key)
    db.commit()
    return raw
```

Verify `ApiKey`'s constructor fields against `app/models/api_key.py` (adjust `key_prefix`/`user_id` to the actual columns; mirror `ocp_pod_auth.mint_ops_pod_key` exactly).

- [ ] **Step 4: Add the endpoint + allowlist**

In `app/api/projects.py`, near the other project routes, add (module-level import `from app.services.workloads.cluster_access import resolve_cluster_access`):

```python
@router.get("/{project_id}/cluster-access")
def get_cluster_access(project_id: str, user: CurrentUser, db: DbSession):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    clusters = resolve_cluster_access(project)
    return {"clusters": clusters}
```

Use the module's existing `CurrentUser`/`DbSession`/`HTTPException`/`Project`/`_PROJECT_NOT_FOUND` aliases (match names actually defined in projects.py). In `app/core/auth.py`, add to `_SCOPED_KEY_ROUTE_ALLOWLIST`: `"get_cluster_access": "cluster:access",`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./venv/bin/python3 -m pytest tests/test_workload_run_key.py tests/test_workload_cluster_access_endpoint.py -v`
Expected: PASS.

- [ ] **Step 6: Format, type-check, commit**

```bash
cd /Users/prutledg/troshka/src/backend && black app/services/workloads/run_key.py app/api/projects.py app/core/auth.py tests/test_workload_run_key.py tests/test_workload_cluster_access_endpoint.py && pyright app/services/workloads/run_key.py
cd /Users/prutledg/troshka && git add src/backend/app/services/workloads/run_key.py src/backend/app/api/projects.py src/backend/app/core/auth.py src/backend/tests/test_workload_run_key.py src/backend/tests/test_workload_cluster_access_endpoint.py
git commit -m "feat(workloads): cluster-access endpoint + scoped run key"
```

---

### Task 4: Inventory emitter + `AnsibleGroup` validation

**Files:**
- Create: `src/backend/app/services/workloads/inventory.py`
- Test: `src/backend/tests/test_workload_inventory.py`

**Interfaces:**
- Produces:
  - `class InventoryError(Exception)`
  - `build_inventory_yaml(api_url: str, api_key: str, project_id: str, connection_mode: str = "ssh") -> str` → the `*.troshka.yml` content that activates the `troshka.cloud.troshka` inventory plugin.
  - `validate_ansible_groups(topology: dict, *, require_bastion: bool) -> None` → raises `InventoryError` if the contract is violated (every `vmNode` needs a name + first-NIC IP + non-empty `data.tags.AnsibleGroup`; if `require_bastion`, exactly one VM must include `bastions` and have an external IP).

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_workload_inventory.py
import pytest
import yaml

from app.services.workloads import inventory


def _vm(name, groups, ip="10.0.0.5", vm_id="v1"):
    return {"id": vm_id, "type": "vmNode",
            "data": {"name": name, "tags": {"AnsibleGroup": groups},
                     "nics": [{"ip": ip}]}}


def test_build_inventory_yaml_shape():
    text = inventory.build_inventory_yaml("https://api.example", "trk_abc", "p1")
    doc = yaml.safe_load(text)
    assert doc["plugin"] == "troshka.cloud.troshka"
    assert doc["api_url"] == "https://api.example"
    assert doc["api_key"] == "trk_abc"
    assert doc["project_id"] == "p1"
    assert doc["connection_mode"] == "ssh"


def test_validate_ok_with_bastion():
    topo = {"nodes": [_vm("b", "bastions"), _vm("m", "masters", vm_id="v2")],
            "externalIps": [{"vmId": "v1", "ip": "1.2.3.4"}]}
    inventory.validate_ansible_groups(topo, require_bastion=True)  # no raise


def test_validate_missing_ansible_group_raises():
    topo = {"nodes": [{"id": "v1", "type": "vmNode",
                       "data": {"name": "x", "tags": {}, "nics": [{"ip": "10.0.0.1"}]}}]}
    with pytest.raises(inventory.InventoryError):
        inventory.validate_ansible_groups(topo, require_bastion=False)


def test_validate_requires_exactly_one_bastion():
    topo = {"nodes": [_vm("b1", "bastions"), _vm("b2", "bastions", vm_id="v2")],
            "externalIps": [{"vmId": "v1", "ip": "1.2.3.4"}, {"vmId": "v2", "ip": "5.6.7.8"}]}
    with pytest.raises(inventory.InventoryError):
        inventory.validate_ansible_groups(topo, require_bastion=True)


def test_validate_bastion_needs_external_ip():
    topo = {"nodes": [_vm("b", "bastions")], "externalIps": []}
    with pytest.raises(inventory.InventoryError):
        inventory.validate_ansible_groups(topo, require_bastion=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python3 -m pytest tests/test_workload_inventory.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# src/backend/app/services/workloads/inventory.py
"""Emit the troshka.cloud inventory file and validate the AnsibleGroup contract."""

from __future__ import annotations

import yaml


class InventoryError(Exception):
    pass


def build_inventory_yaml(api_url: str, api_key: str, project_id: str,
                         connection_mode: str = "ssh") -> str:
    return yaml.safe_dump({
        "plugin": "troshka.cloud.troshka",
        "api_url": api_url,
        "api_key": api_key,
        "project_id": project_id,
        "connection_mode": connection_mode,
    }, sort_keys=True)


def _vm_nodes(topology: dict) -> list[dict]:
    return [n for n in (topology.get("nodes") or []) if n.get("type") == "vmNode"]


def _groups(node: dict) -> list[str]:
    raw = ((node.get("data") or {}).get("tags") or {}).get("AnsibleGroup", "")
    return [g.strip() for g in raw.split(",") if g.strip()]


def _external_ip_vm_ids(topology: dict) -> set[str]:
    return {e.get("vmId") for e in (topology.get("externalIps") or []) if e.get("ip")}


def validate_ansible_groups(topology: dict, *, require_bastion: bool) -> None:
    nodes = _vm_nodes(topology)
    if not nodes:
        raise InventoryError("project has no VM nodes to target")
    bastions: list[str] = []
    for node in nodes:
        data = node.get("data") or {}
        if not data.get("name"):
            raise InventoryError("a VM node is missing data.name")
        nics = data.get("nics") or []
        if not nics or not nics[0].get("ip"):
            raise InventoryError(f"VM {data.get('name')} has no first-NIC IP")
        groups = _groups(node)
        if not groups:
            raise InventoryError(
                f"VM {data['name']} has no AnsibleGroup tag")
        if "bastions" in groups:
            bastions.append(node.get("id"))
    if require_bastion:
        _validate_bastion(bastions, topology)


def _validate_bastion(bastions: list[str], topology: dict) -> None:
    if len(bastions) != 1:
        raise InventoryError(
            f"exactly one VM must be tagged 'bastions' (found {len(bastions)})")
    if bastions[0] not in _external_ip_vm_ids(topology):
        raise InventoryError("bastion VM has no external IP")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python3 -m pytest tests/test_workload_inventory.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Format, type-check, commit**

```bash
cd /Users/prutledg/troshka/src/backend && black app/services/workloads/inventory.py tests/test_workload_inventory.py && pyright app/services/workloads/inventory.py tests/test_workload_inventory.py
cd /Users/prutledg/troshka && git add src/backend/app/services/workloads/inventory.py src/backend/tests/test_workload_inventory.py
git commit -m "feat(workloads): inventory emitter + AnsibleGroup validation"
```

---

### Task 5: Runner-pod launch (EE image, artifacts as mounts)

Build the run artifacts and launch the runner pod on both host types, reusing the low-level primitives. **Do not modify the OCP builders.** The pod image is the resolved EE; the command runs `ansible-playbook` inside it.

**Files:**
- Create: `src/backend/app/services/workloads/pod_launch.py`
- Test: `src/backend/tests/test_workload_pod_launch.py`

**Interfaces:**
- Consumes: `troshkad_client.start_job`; `providers/kubevirt.create_ops_pod`; `ocp/ops_pod_scaffold` helpers for network entries (reuse, do not modify).
- Produces:
  - `build_run_command(resolved_item, paths: RunPaths) -> list[str]` → `["bash","-lc", "<script>"]` that runs `ansible-playbook` against the delivered agnosticd-v2 + inventory + extra-vars.
  - `@dataclass RunPaths` (container paths for extra_vars, inventory, cluster_access, agnosticd checkout, collections).
  - `build_artifact_files(*, extra_vars: dict, inventory_yaml: str, cluster_access: dict, cloud_creds: dict | None, paths: RunPaths) -> dict[str, str]` → `{container_path: content}` (the 0600 mount map).
  - `launch_runner_pod(host, project, *, ee_image, command, files, networks) -> str` (troshkad) / delegates to KubeVirt path by `host.host_type`; returns a pod/job identifier.

- [ ] **Step 1: Write the failing test** (mock troshkad + kubevirt)

```python
# src/backend/tests/test_workload_pod_launch.py
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.workloads import pod_launch


def test_build_artifact_files_maps_paths():
    paths = pod_launch.RunPaths()
    files = pod_launch.build_artifact_files(
        extra_vars={"a": 1}, inventory_yaml="plugin: troshka.cloud.troshka\n",
        cluster_access={"cl": {"api_url": "u", "api_token": "t"}},
        cloud_creds=None, paths=paths)
    assert paths.extra_vars in files
    assert "troshka.cloud" in files[paths.inventory]
    assert "api_token" in files[paths.cluster_access]


def test_build_run_command_invokes_ansible_playbook():
    paths = pod_launch.RunPaths()
    item = SimpleNamespace(extra_vars={"config": "openshift-workloads"}, ee_image="ee:1",
                           scm_ref="main", requirements_content=None)
    cmd = pod_launch.build_run_command(item, paths)
    joined = " ".join(cmd)
    assert "ansible-playbook" in joined
    assert paths.inventory in joined
    assert paths.extra_vars in joined


def test_launch_runner_pod_troshkad(monkeypatch):
    host = SimpleNamespace(id="h1", host_type="shared")
    project = SimpleNamespace(id="p1")
    fake_start = MagicMock(return_value="job-123")
    monkeypatch.setattr(pod_launch, "start_job", fake_start)
    job = pod_launch.launch_runner_pod(
        host, project, ee_image="ee:1", command=["bash", "-lc", "x"],
        files={"/run/x": "y"}, networks=[])
    assert job == "job-123"
    args, kwargs = fake_start.call_args
    assert args[1] == "/pods/create"
    params = args[2]
    assert params["containers"][0]["image"] == "ee:1"
    assert params["files"] == {"/run/x": "y"}
    assert params["privileged"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python3 -m pytest tests/test_workload_pod_launch.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement** (mirror `_ops_pod_create_params` structure; new code, do not touch the original)

```python
# src/backend/app/services/workloads/pod_launch.py
"""Launch the in-project runner pod. The pod image IS the resolved EE image;
the AgnosticD-v2 checkout, extra-vars, inventory and cluster-access are delivered
as read-only 0600 mounts, and the pod runs ansible-playbook directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import yaml

from app.services.troshkad_client import start_job

_WORKDIR = "/workdir"


@dataclass
class RunPaths:
    extra_vars: str = f"{_WORKDIR}/extra-vars.yml"
    inventory: str = f"{_WORKDIR}/inventory.troshka.yml"
    cluster_access: str = f"{_WORKDIR}/cluster-access.json"
    cloud_creds: str = f"{_WORKDIR}/cloud-creds.env"
    agnosticd: str = f"{_WORKDIR}/agnosticd-v2"
    collections: str = f"{_WORKDIR}/collections"


def build_artifact_files(*, extra_vars: dict, inventory_yaml: str,
                         cluster_access: dict, cloud_creds: dict | None,
                         paths: RunPaths) -> dict[str, str]:
    files = {
        paths.extra_vars: yaml.safe_dump(extra_vars, sort_keys=False),
        paths.inventory: inventory_yaml,
        paths.cluster_access: json.dumps(cluster_access),
    }
    if cloud_creds:
        files[paths.cloud_creds] = "\n".join(
            f"{k}={v}" for k, v in cloud_creds.items())
    return files


def build_run_command(resolved_item, paths: RunPaths) -> list[str]:
    # Runs the AgnosticD-v2 openshift-workloads config directly inside the EE.
    # The exact playbook entrypoint is confirmed against ~/agnosticd-v2 in Step 4.
    script = (
        f"set -euo pipefail; cd {paths.agnosticd}/ansible; "
        f"export ANSIBLE_COLLECTIONS_PATH={paths.collections}; "
        f"ansible-playbook main.yml "
        f"-i {paths.inventory} "
        f"-e @{paths.extra_vars} "
        f"-e ACTION=provision"
    )
    return ["bash", "-lc", script]


def launch_runner_pod(host, project, *, ee_image: str, command: list[str],
                      files: dict[str, str], networks: list) -> str:
    if getattr(host, "host_type", None) == "kubevirt-cluster":
        return _launch_kubevirt(host, project, ee_image=ee_image,
                                command=command, files=files, networks=networks)
    return _launch_troshkad(host, project, ee_image=ee_image,
                            command=command, files=files, networks=networks)


def _launch_troshkad(host, project, *, ee_image, command, files, networks) -> str:
    container = {
        "name": "runner", "image": ee_image, "cpus": 2, "memory": 4096,
        "env": {}, "mounts": [], "command": command, "privileged": True,
    }
    params = {
        "project_id": project.id, "pod_name": "workload-runner",
        "networks": networks, "init_containers": [], "containers": [container],
        "volumes": [], "files": files, "restart_policy": "never",
        "privileged": True,
    }
    return start_job(host, "/pods/create", params)


def _launch_kubevirt(host, project, *, ee_image, command, files, networks) -> str:
    from app.services.ocp.ops_pod_scaffold import build_ops_pod_kubevirt_manifests
    from app.services.providers.kubevirt import create_ops_pod

    provider = _provider_for_host(host)
    namespace = _namespace_for_project(project)
    pod, secret = build_ops_pod_kubevirt_manifests(
        namespace=namespace, project_id=project.id, command=command, env={},
        config_files=files, cluster_nads=[], bmc_nad=None, dns_nameserver=None,
        image=ee_image, pod_name="workload-runner")
    create_ops_pod(provider, project.id, pod, secret)
    return f"workload-runner-{project.id[:8]}"


def _provider_for_host(host):  # resolved in Step 4 against providers/kubevirt usage
    raise NotImplementedError


def _namespace_for_project(project):  # resolved in Step 4
    raise NotImplementedError
```

- [ ] **Step 4: Ground the two integration points against the codebase, then finish**

Two things in Step 3 are marked for grounding — resolve them by reading the real code (do NOT guess):
1. **`build_run_command` entrypoint.** Inspect `~/agnosticd-v2/ansible/` (read-only): confirm whether running only the software/workloads stage against existing infra is `main.yml` with `-e config=openshift-workloads` (+ `cloud_provider`), or the config's `software.yml` directly. Update the command accordingly and add a comment citing the file you confirmed against. Also thread `requirements_content` (write it to a file and pass the AgnosticD var that consumes it, per `install_dynamic_dependencies.yml`) and `guid` if required.
2. **`build_ops_pod_kubevirt_manifests` image/pod_name params.** That function in `app/services/ocp/ops_pod_scaffold.py` may not yet accept `image`/`pod_name`. If it doesn't, DO NOT change its OCP call sites' behavior — add optional keyword args with defaults matching current behavior (`image=OPS_POD_IMAGE`, `pod_name="ops"`) so the OCP path is unchanged and the runner can override. Resolve `_provider_for_host` / `_namespace_for_project` by mirroring how `_deploy_ops_pod_kubevirt` obtains the provider + namespace in `deploy_service.py`.

Re-run the tests after grounding; add a focused test for whichever entrypoint form you confirmed.

- [ ] **Step 5: Run tests, format, type-check, commit**

Run: `./venv/bin/python3 -m pytest tests/test_workload_pod_launch.py -v` → PASS.

```bash
cd /Users/prutledg/troshka/src/backend && black app/services/workloads/pod_launch.py tests/test_workload_pod_launch.py && pyright app/services/workloads/pod_launch.py tests/test_workload_pod_launch.py
cd /Users/prutledg/troshka && git add src/backend/app/services/workloads/pod_launch.py src/backend/tests/test_workload_pod_launch.py src/backend/app/services/ocp/ops_pod_scaffold.py
git commit -m "feat(workloads): runner-pod launch with EE image + artifact mounts"
```

---

### Task 6: Run orchestration + enqueued run job

**Files:**
- Create: `src/backend/app/services/workloads/run_service.py`
- Modify: `src/backend/app/workers/jobs.py` (add `job_run_workload`)
- Test: `src/backend/tests/test_workload_run_service.py`

**Interfaces:**
- Consumes: Plan 1 `resolver.resolve_catalog_item`, `repo_cache.ensure_repo`; Tasks 2/3/4/5.
- Produces:
  - `start_workload_run(db, *, project_id, kind, catalog_item=None, role_fqcn=None, target_map=None, owner_id=None) -> WorkloadRun` — creates the row (`status="pending"`), enqueues `job_run_workload(run_id)`, returns the row.
  - `run_workload_job(run_id: str) -> None` — the job body: load run+project+host, resolve item (or synthesize ad-hoc), ensure agnosticd-v2 checkout + collections in cache, mint run key, build inventory + validate groups, resolve cluster access (OCP targets), assemble artifacts, launch pod, set `status="running"` + `started_at`, start the monitor. On error → `status="error"` + `error`.
  - `jobs.job_run_workload(run_id)` — thin RQ entrypoint calling `run_service.run_workload_job`.

- [ ] **Step 1: Write the failing test** (mock every external seam)

```python
# src/backend/tests/test_workload_run_service.py
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.models.provider import Provider
from app.models.project import Project
from app.models.workload_run import WorkloadRun
from app.services.workloads import run_service
from tests.conftest import TestSession


def _active_project(db):
    prov = Provider(name="pp", type="troshka", credentials="{}")
    db.add(prov); db.flush()
    proj = Project(name="rs", owner_id="u1", provider_id=prov.id, state="active",
                   topology={"nodes": []})
    db.add(proj); db.commit()
    return proj


def test_start_workload_run_creates_row_and_enqueues(monkeypatch):
    db = TestSession()
    try:
        proj = _active_project(db)
        enq = MagicMock()
        monkeypatch.setattr(run_service, "enqueue_job", enq)
        run = run_service.start_workload_run(
            db, project_id=proj.id, kind="catalog_item",
            catalog_item="agd-v2.x.prod", owner_id="u1")
        assert run.status == "pending"
        assert run.catalog_item == "agd-v2.x.prod"
        assert enq.called
    finally:
        db.close()


def test_run_workload_job_happy_path(monkeypatch):
    db = TestSession()
    proj = _active_project(db)
    run = WorkloadRun(project_id=proj.id, kind="catalog_item",
                      catalog_item="agd-v2.x.prod", status="pending")
    db.add(run); db.commit()
    rid = run.id; db.close()

    monkeypatch.setattr(run_service, "SessionLocal", TestSession)
    monkeypatch.setattr(run_service, "_host_for_project", lambda db, p: SimpleNamespace(id="h1", host_type="shared"))
    monkeypatch.setattr(run_service, "resolve_catalog_item",
                        lambda db, cid: SimpleNamespace(
                            extra_vars={"config": "openshift-workloads"},
                            ee_image="ee:1", scm_ref="main", requirements_content=None))
    monkeypatch.setattr(run_service, "_prepare_agnosticd", lambda item: None)
    monkeypatch.setattr(run_service, "mint_run_key", lambda db, p: "trk_k")
    monkeypatch.setattr(run_service, "resolve_cluster_access", lambda p: {})
    monkeypatch.setattr(run_service, "validate_ansible_groups", lambda t, require_bastion: None)
    launched = MagicMock(return_value="job-1")
    monkeypatch.setattr(run_service, "launch_runner_pod", launched)
    monkeypatch.setattr(run_service, "_start_workload_monitor", lambda *a, **k: None)

    run_service.run_workload_job(rid)

    db = TestSession()
    row = db.get(WorkloadRun, rid)
    assert row.status == "running"
    assert row.started_at is not None
    assert launched.called
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python3 -m pytest tests/test_workload_run_service.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement** (module-level names for every monkeypatched dependency)

```python
# src/backend/app/services/workloads/run_service.py
"""Orchestrate a workload run: create the record, resolve everything backend-side,
launch the runner pod, and track progress. Credential-safe (git creds + vault key
never leave the backend; see Plan 1)."""

from __future__ import annotations

import datetime

from app.core.database import SessionLocal
from app.core.redis import enqueue_job
from app.models.project import Project
from app.models.workload_run import WorkloadRun
from app.services.workloads.cluster_access import resolve_cluster_access
from app.services.workloads.inventory import build_inventory_yaml, validate_ansible_groups
from app.services.workloads.pod_launch import (
    RunPaths, build_artifact_files, build_run_command, launch_runner_pod)
from app.services.workloads.resolver import resolve_catalog_item
from app.services.workloads.run_key import mint_run_key


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def start_workload_run(db, *, project_id, kind, catalog_item=None, role_fqcn=None,
                       target_map=None, owner_id=None) -> WorkloadRun:
    run = WorkloadRun(project_id=project_id, kind=kind, catalog_item=catalog_item,
                      role_fqcn=role_fqcn, target_map=target_map, owner_id=owner_id,
                      status="pending")
    db.add(run)
    db.commit()
    db.refresh(run)
    enqueue_job(job_run_workload, run.id, project_id=project_id)
    return run


def _host_for_project(db, project):
    from app.services.deploy_service import _resolve_project_host  # or the real helper
    return _resolve_project_host(db, project)


def _prepare_agnosticd(item) -> None:
    from app.core.config import config
    from app.services.workloads import repo_cache, secret_store
    # ensure agnosticd-v2 checkout at item.scm_ref, using admin-central git creds
    ...  # grounded in Step 4


def _has_ocp(project) -> bool:
    topo = project.deployed_topology or project.topology or {}
    return any((n.get("data") or {}).get("ocpKubeconfig")
               for n in (topo.get("nodes") or []))


def run_workload_job(run_id: str) -> None:
    db = SessionLocal()
    try:
        run = db.get(WorkloadRun, run_id)
        project = db.get(Project, run.project_id)
        host = _host_for_project(db, project)
        item = _resolve_item(db, run)
        _prepare_agnosticd(item)
        key = mint_run_key(db, project)
        topo = project.deployed_topology or project.topology or {}
        validate_ansible_groups(topo, require_bastion=(not _has_ocp(project)))
        from app.core.config import config
        inv = build_inventory_yaml(config.app.external_url, key, project.id)
        cluster_access = resolve_cluster_access(project) if _has_ocp(project) else {}
        paths = RunPaths()
        extra_vars = dict(item.extra_vars)
        extra_vars["clusters"] = cluster_access
        files = build_artifact_files(
            extra_vars=extra_vars, inventory_yaml=inv,
            cluster_access=cluster_access, cloud_creds=None, paths=paths)
        command = build_run_command(item, paths)
        launch_runner_pod(host, project, ee_image=item.ee_image,
                          command=command, files=files, networks=[])
        run.status = "running"
        run.started_at = _now()
        db.commit()
        _start_workload_monitor(host, run.id)
    except Exception as exc:  # noqa: BLE001 — record and surface
        _fail_run(db, run_id, str(exc))
        raise
    finally:
        db.close()


def _resolve_item(db, run):
    if run.kind == "catalog_item":
        return resolve_catalog_item(db, run.catalog_item)
    return _synthesize_ad_hoc(db, run)  # grounded in Step 4


def _synthesize_ad_hoc(db, run):
    ...  # minimal workloads:[role] + requirements_content — Step 4


def _fail_run(db, run_id, message: str) -> None:
    row = db.get(WorkloadRun, run_id)
    if row is not None:
        row.status = "error"
        row.error = message[:2000]
        row.ended_at = _now()
        db.commit()


def _start_workload_monitor(host, run_id: str) -> None:
    # Implemented in Task 7 (enqueued monitor job + Redis lock).
    from app.services.workloads.run_service import _enqueue_monitor
    _enqueue_monitor(host, run_id)
```

In `app/workers/jobs.py` add:

```python
def job_run_workload(run_id):
    from app.services.workloads.run_service import run_workload_job
    run_workload_job(run_id)
```

- [ ] **Step 4: Ground `_host_for_project`, `_prepare_agnosticd`, `_synthesize_ad_hoc`** against the real helpers (`deploy_service` host resolution; `repo_cache.ensure_repo` + `secret_store` for the agnosticd-v2/agnosticd git URL+ref; the ad-hoc `requirements_content` synthesis inferring the collection from `role_fqcn`). Add focused tests for the ad-hoc synthesis. Re-run.

- [ ] **Step 5: Run tests, format, type-check, commit**

Run: `./venv/bin/python3 -m pytest tests/test_workload_run_service.py -v` → PASS.

```bash
cd /Users/prutledg/troshka/src/backend && black app/services/workloads/run_service.py app/workers/jobs.py tests/test_workload_run_service.py && pyright app/services/workloads/run_service.py
cd /Users/prutledg/troshka && git add src/backend/app/services/workloads/run_service.py src/backend/app/workers/jobs.py src/backend/tests/test_workload_run_service.py
git commit -m "feat(workloads): run orchestration + enqueued run job"
```

---

### Task 7: Monitor + restart recovery

**Files:**
- Modify: `src/backend/app/services/workloads/run_service.py` (monitor + resume)
- Modify: `src/backend/app/workers/jobs.py` (`job_workload_monitor`)
- Modify: `src/backend/app/workers/deploy_worker.py` (call `resume_workload_monitors`)
- Test: `src/backend/tests/test_workload_monitor.py`

**Interfaces:**
- Produces:
  - `_enqueue_monitor(host, run_id)` — Redis-lock-dedup (`workload-monitor:{run_id}`), enqueues `job_workload_monitor(run_id, host.id)`.
  - `monitor_workload_run(run_id, host_id)` — poll loop: tail runner-pod logs, publish progress via `_update_deploy_progress`-style writes on channel `workload:{run_id}`, honor cancellation, set terminal `status` (`succeeded`/`error`) + `ended_at`, release lock.
  - `resume_workload_monitors()` — re-attach monitors for `WorkloadRun.status == "running"`, idempotent.
  - `parse_workload_progress(log_text: str) -> dict` — PURE log→progress mapper (unit-tested).

- [ ] **Step 1: Write the failing test** (pure parser + resume selection; mock the loop's I/O)

```python
# src/backend/tests/test_workload_monitor.py
from unittest.mock import MagicMock, patch

from app.models.provider import Provider
from app.models.project import Project
from app.models.workload_run import WorkloadRun
from app.services.workloads import run_service
from tests.conftest import TestSession


def test_parse_workload_progress_extracts_task():
    text = "TASK [ocp4_workload_gitea_operator : Create namespace] ***\nok: [localhost]"
    prog = run_service.parse_workload_progress(text)
    assert "step" in prog and prog["step"]


def test_resume_workload_monitors_reattaches_running(monkeypatch):
    db = TestSession()
    prov = Provider(name="pm", type="troshka", credentials="{}")
    db.add(prov); db.flush()
    proj = Project(name="mon", owner_id="u1", provider_id=prov.id, state="active")
    db.add(proj); db.flush()
    run = WorkloadRun(project_id=proj.id, kind="ad_hoc", role_fqcn="x", status="running")
    db.add(run); db.commit()
    rid = run.id; db.close()

    monkeypatch.setattr(run_service, "SessionLocal", TestSession)
    reattached = MagicMock()
    monkeypatch.setattr(run_service, "_enqueue_monitor_by_ids", reattached)
    run_service.resume_workload_monitors()
    assert reattached.called
    assert reattached.call_args[0][0] == rid
```

- [ ] **Step 2–5:** Run→fail; implement the monitor mirroring `_monitor_ops_pod_install` (enqueued job, `_detached_host_copy`-style detach, Redis lock heartbeat, cancel check, log tail via troshkad `containers/exec`/`podman logs` or k8s `read_namespaced_pod_log`, publish through `_update_deploy_progress` on a `workload:{id}` channel, terminal write to `WorkloadRun`); add `resume_workload_monitors()`; wire it into `deploy_worker.run_worker()` next to `resume_ops_pod_monitors()`; run→pass; black/pyright; commit `feat(workloads): run monitor + restart recovery`.

---

### Task 8: Trigger / status / list API

**Files:**
- Create: `src/backend/app/api/workloads.py`
- Modify: `src/backend/app/main.py` (register router)
- Test: `src/backend/tests/test_workload_api.py`

**Interfaces:**
- `POST /api/projects/{project_id}/workloads` body `{kind, catalog_item?, role_fqcn?, target_map?}` → creates + enqueues a run (calls `start_workload_run`); 202 with the run id/status. Rejects non-active projects (409) and bad body (422).
- `GET /api/projects/{project_id}/workloads` → list runs for the project (most recent first).
- `GET /api/workloads/{run_id}` → run status + progress (Redis-first, DB fallback).

- [ ] **Step 1–5:** TDD with the FastAPI `TestClient` (dev auto-auth), Pydantic request/response models, `APIRouter(prefix="/...", tags=["workloads"])`, register in `main.py` under `_API_PREFIX`; mock `start_workload_run`. Run→fail→implement→pass; black/pyright; commit `feat(workloads): run trigger/status/list API`.

---

### Task 9: Retention pruning

**Files:**
- Modify: `src/backend/app/services/workloads/run_service.py` (`prune_workload_runs`)
- Modify: `src/backend/app/main.py` (start a pruning timer in `lifespan`, mirroring `project_timer`)
- Modify: `src/backend/config/config.yaml` (`workloads.run_retention_days`, default 30)
- Test: `src/backend/tests/test_workload_pruning.py`

**Interfaces:**
- Produces: `prune_workload_runs(db, *, retention_days: int, now=None) -> int` — deletes terminal (`succeeded`/`error`) runs older than the cutoff; returns count. A background thread (daemon) started from `lifespan` calls it on an interval.

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_workload_pruning.py
import datetime

from app.models.workload_run import WorkloadRun
from app.services.workloads.run_service import prune_workload_runs
from tests.conftest import TestSession


def test_prune_deletes_old_terminal_runs():
    db = TestSession()
    try:
        old = WorkloadRun(kind="ad_hoc", role_fqcn="x", status="succeeded",
                          ended_at=datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc))
        recent = WorkloadRun(kind="ad_hoc", role_fqcn="y", status="succeeded",
                             ended_at=datetime.datetime.now(datetime.timezone.utc))
        running = WorkloadRun(kind="ad_hoc", role_fqcn="z", status="running")
        db.add_all([old, recent, running]); db.commit()
        old_id, recent_id, running_id = old.id, recent.id, running.id
        n = prune_workload_runs(db, retention_days=30)
        assert n == 1
        assert db.get(WorkloadRun, old_id) is None
        assert db.get(WorkloadRun, recent_id) is not None
        assert db.get(WorkloadRun, running_id) is not None  # never prune running
    finally:
        db.close()
```

- [ ] **Step 2–5:** Run→fail; implement `prune_workload_runs` (filter `status in ("succeeded","error")` and `ended_at < now - retention_days`), add the `lifespan` timer thread mirroring `project_timer.start_project_timer`, add the config key; run→pass; black/pyright; commit `feat(workloads): WorkloadRun retention pruning`.

---

## Self-Review

**Spec coverage (Plan 2 scope):**
- Runner pod generalized to any active project, image = EE, artifacts as mounts (spec §4.2 Workload Runner, D3) → Tasks 5–6. ✔
- Inventory reuse + `AnsibleGroup` tagging/validation (spec §4.2 Inventory, D6) → Task 4 (canvas UI editing deferred to Plan 3; Plan 2 validates tags set via topology). ✔
- Cluster access as `{api_url, api_token}` SA token + endpoint + scope (spec §4.2 Cluster Access, D7) → Tasks 2–3. ✔
- `WorkloadRun` model, retain-everything + pruning (spec §6, D11) → Tasks 1, 9. ✔
- RQ job/progress/monitor + restart recovery (spec §4.2 Job+Progress) → Tasks 6–7. ✔
- Trigger via API (spec D5; UI is Plan 3) → Task 8. ✔
- Credential isolation preserved: git creds/vault key stay backend-side; pod gets only resolved artifacts + scoped key (spec §5) → Tasks 5–6. ✔
- Cloud creds injection (D9): the artifact map supports `cloud_creds`; wiring the admin-central selection is carried as a Task 6 follow-on / Plan 3 detail — flagged, not silently dropped.

**Placeholder scan:** Tasks 5 and 6 contain explicitly-marked grounding steps (Step 4) with a concrete method (read the real files), not vague TODOs; each is a required, verifiable step. Tasks 7–9 give full interfaces + the first failing test + a precise implement-by-mirroring instruction rather than repeating boilerplate already shown verbatim in Tasks 1–6.

**Type consistency:** `ResolvedItem` fields (`extra_vars`, `ee_image`, `scm_ref`, `requirements_content`) match Plan 1. `RunPaths` fields are referenced identically across `build_artifact_files`/`build_run_command`. `resolve_cluster_access(project) -> {name: {api_url, api_token}}` is consumed consistently in `run_service`. `start_workload_run`/`run_workload_job` names match the `jobs.py` entrypoints.

**Open (carried, not blocking):** exact AgnosticD-v2 workloads entrypoint (Task 5 Step 4, verified against ~/agnosticd-v2); admin-central cloud-cred selection wiring (D9); ad-hoc `requirements_content` synthesis (Task 6 Step 4). Deferred to Plan 3: canvas `AnsibleGroup` editing UI, run trigger/history UI.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-11-troshka-workloads-plan2-execution.md`.
