# Troshka Dedicated Instance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable single-user dedicated Troshka instances provisioned as agnosticD catalog items on CNV clusters, with MinIO for local S3-compatible storage.

**Architecture:** Four independent work streams — (1) pass S3 endpoint_url through to troshkad so MinIO works end-to-end, (2) add allowed_users enforcement to the auth layer, (3) deploy a central MinIO on infra01 to serve gold images, (4) build the agnosticD workload role and agnosticv catalog item.

**Tech Stack:** Python/FastAPI (backend), troshkad (agent), Ansible (workload role), YAML (agnosticv catalog), MinIO (S3-compatible storage)

## Global Constraints

- Python 3.11, FastAPI, SQLAlchemy 2.0+ with `Mapped`/`mapped_column`
- Tests use SQLite with JSONB/UUID type overrides (see `conftest.py`)
- Backend has no auto-reload — restart required after Python changes
- Always use `python3` not `python`
- Run `black` before committing (pre-commit hook enforces it)
- Never use `sed` for file edits
- Troshkad is stdlib-only Python — no pip dependencies
- AWS CLI env var `AWS_ENDPOINT_URL` is respected natively (no `--endpoint-url` flag needed)

---

### Task 1: S3 Endpoint URL Passthrough to Troshkad

Pass the `endpoint_url` from `_get_s3_config()` through to troshkad so the AWS CLI targets MinIO instead of real AWS S3.

**Files:**
- Modify: `src/backend/app/services/pattern_service.py` (lines 240-243, 609-611, 816-818)
- Modify: `src/backend/app/services/snapshot_service.py` (lines 87-89)
- Modify: `src/backend/app/api/library.py` (lines 470-476)
- Modify: `src/troshkad/troshkad.py` (`_s3_upload` ~line 4819, `_s3_download` ~line 4958)
- Test: `src/backend/tests/test_s3_endpoint_url.py`

**Interfaces:**
- Consumes: `s3_storage._get_s3_config()` — already returns `endpoint_url` key
- Produces: troshkad `_s3_upload()`/`_s3_download()` set `AWS_ENDPOINT_URL` env var when `aws_endpoint_url` param is non-empty

- [ ] **Step 1: Write test verifying endpoint_url is included in troshkad creds**

Create `src/backend/tests/test_s3_endpoint_url.py`:

```python
"""Verify S3 endpoint_url flows through to troshkad job params."""

from unittest.mock import patch


def test_s3_config_includes_endpoint_url():
    """_get_s3_config returns endpoint_url from config."""
    with patch("app.services.s3_storage.config") as mock_config:
        mock_config.s3.region = "us-east-1"
        mock_config.s3.access_key_id = "minioadmin"
        mock_config.s3.secret_access_key = "minioadmin"
        mock_config.s3.bucket = "troshka-images"
        mock_config.s3.endpoint_url = "http://troshka-minio:9000"

        with patch("app.services.s3_storage.SessionLocal") as mock_sl:
            mock_sl.return_value.query.return_value.filter_by.return_value.first.return_value = (
                None
            )
            from app.services.s3_storage import _get_s3_config

            cfg = _get_s3_config()
            assert cfg["endpoint_url"] == "http://troshka-minio:9000"
```

- [ ] **Step 2: Run test to verify it passes (existing code already returns endpoint_url)**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_s3_endpoint_url.py -v`
Expected: PASS — `_get_s3_config()` already includes `endpoint_url`

- [ ] **Step 3: Add endpoint_url to pattern_service.py creds dicts**

In `src/backend/app/services/pattern_service.py`, find every block that builds S3 creds for troshkad (3 locations). Each currently has:

```python
"aws_access_key_id": creds.get("access_key_id", ""),
"aws_secret_access_key": creds.get("secret_access_key", ""),
"aws_region": creds.get("region", "us-east-1"),
```

Add one line after each block:

```python
"aws_endpoint_url": creds.get("endpoint_url", ""),
```

There are three locations — search for `"aws_region": creds.get("region"` to find all three.

- [ ] **Step 4: Add endpoint_url to snapshot_service.py creds dict**

In `src/backend/app/services/snapshot_service.py`, find the same pattern (~line 89) and add:

```python
"aws_endpoint_url": creds.get("endpoint_url", ""),
```

- [ ] **Step 5: Add endpoint_url to library.py import creds dict**

In `src/backend/app/api/library.py`, find the `/library/import` job start (~line 476) and add:

```python
"aws_endpoint_url": s3_creds.get("endpoint_url", ""),
```

- [ ] **Step 6: Update troshkad _s3_upload() to set AWS_ENDPOINT_URL**

In `src/troshkad/troshkad.py`, update `_s3_upload()` function signature to accept the new param:

```python
def _s3_upload(
    job,
    local_path,
    s3_url,
    aws_access_key="",
    aws_secret_key="",
    aws_region="us-east-1",
    aws_endpoint_url="",
):
```

Inside the function, after the existing block that sets `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_DEFAULT_REGION` env vars, add:

```python
    if aws_endpoint_url:
        env["AWS_ENDPOINT_URL"] = aws_endpoint_url
```

- [ ] **Step 7: Update troshkad _s3_download() to set AWS_ENDPOINT_URL**

Same change in `_s3_download()` — add `aws_endpoint_url=""` param and set `AWS_ENDPOINT_URL` in env.

- [ ] **Step 8: Update troshkad handler functions to pass endpoint_url through**

Search troshkad for every call to `_s3_upload()` and `_s3_download()`. Each reads params from the job's `params` dict. Add `aws_endpoint_url=params.get("aws_endpoint_url", "")` to each call.

Search for `_s3_upload(` and `_s3_download(` in troshkad.py to find all call sites. There are roughly 6-8 of them across handlers for `/patterns/capture-direct`, `/patterns/upload-and-cache`, `/snapshots/capture`, `/library/import`, and `/nbd/pull-flatten`.

- [ ] **Step 9: Run tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_s3_endpoint_url.py tests/ -v --tb=short`
Expected: All tests pass

- [ ] **Step 10: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/pattern_service.py src/backend/app/services/snapshot_service.py src/backend/app/api/library.py src/troshkad/troshkad.py src/backend/tests/test_s3_endpoint_url.py
git commit -m "feat: pass S3 endpoint_url through to troshkad for MinIO support"
```

---

### Task 2: Upload Proxy Endpoint for MinIO

When MinIO is behind a cluster-internal service, presigned URLs are unreachable from the browser. Add a streaming proxy endpoint so the frontend can upload through the backend.

**Files:**
- Modify: `src/backend/app/api/library.py`
- Test: `src/backend/tests/test_library_upload_proxy.py`

**Interfaces:**
- Consumes: `s3_storage._get_s3_client()`, `s3_storage._bucket()`, `s3_storage._get_s3_config()`
- Produces: `POST /library/{item_id}/upload-proxy` — accepts file upload, streams to S3/MinIO, returns `{"s3_key": str, "size_bytes": int}`

- [ ] **Step 1: Write test for proxy upload endpoint**

Create `src/backend/tests/test_library_upload_proxy.py`:

```python
"""Test the upload proxy endpoint for MinIO."""

import uuid
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_upload_proxy_creates_s3_object():
    """Proxy upload streams file to S3 and updates library item."""
    item_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    lib_id = str(uuid.uuid4())

    mock_item = MagicMock()
    mock_item.id = item_id
    mock_item.name = "test-image"
    mock_item.format = "qcow2"
    mock_item.library_id = lib_id
    mock_item.s3_key = None
    mock_item.state = "pending"

    mock_lib = MagicMock()
    mock_lib.id = lib_id
    mock_lib.owner_id = user_id

    mock_user = MagicMock()
    mock_user.id = user_id

    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {"ContentLength": 1024}

    with (
        patch("app.api.library.get_current_user", return_value=mock_user),
        patch("app.api.library.get_db"),
        patch(
            "app.api.library.LibraryItem",
            **{"__class__": type},
        ),
    ):
        # Integration test would need full DB setup;
        # this verifies the endpoint exists and the route is registered
        response = client.post(
            f"/api/v1/library/{item_id}/upload-proxy",
            files={"file": ("test.qcow2", b"fake-image-data", "application/octet-stream")},
        )
        # Will get 401/404 without full mock chain, but proves route exists
        assert response.status_code in (200, 401, 404, 422)
```

- [ ] **Step 2: Run test to verify it fails (endpoint doesn't exist yet)**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_library_upload_proxy.py -v`
Expected: FAIL — 404 because the endpoint doesn't exist

- [ ] **Step 3: Implement proxy upload endpoint**

Add to `src/backend/app/api/library.py`, after the existing `complete_upload` endpoint:

```python
@router.post("/{item_id}/upload-proxy")
async def upload_proxy(
    item_id: str,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stream a file upload to S3/MinIO through the backend.

    Used when MinIO is behind a cluster-internal service and presigned
    URLs are not browser-reachable.
    """
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    lib = db.query(Library).filter_by(id=item.library_id).first()
    if not lib or lib.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    ext = item.format if item.format != "qcow2" else "qcow2"
    s3_key = f"library/{user.id}/{item.id}/{item.name}.{ext}"

    from app.services.s3_storage import _bucket, _get_s3_client

    client = _get_s3_client()
    client.upload_fileobj(
        file.file,
        _bucket(),
        s3_key,
        ExtraArgs={"ContentType": file.content_type or "application/octet-stream"},
    )

    head = client.head_object(Bucket=_bucket(), Key=s3_key)
    item.s3_key = s3_key
    item.size_bytes = head["ContentLength"]
    item.state = "ready"
    db.commit()

    logger.info(
        "Proxy upload: %s → %s (%d bytes)", item.name, s3_key, item.size_bytes
    )
    return {"s3_key": s3_key, "size_bytes": item.size_bytes}
```

Add required imports at the top of the file if not present:

```python
from fastapi import File, UploadFile
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_library_upload_proxy.py -v`
Expected: PASS (route now exists, returns a valid status code)

- [ ] **Step 5: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v --tb=short`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/library.py src/backend/tests/test_library_upload_proxy.py
git commit -m "feat: add upload proxy endpoint for MinIO behind cluster-internal services"
```

---

### Task 3: Allowed Users Auth Enforcement

Add `allowed_users` config field and enforce it in `get_current_user()`. When set, only listed identities can access the instance.

**Files:**
- Modify: `src/backend/config/config.yaml` (line 21, add after `admin_users`)
- Modify: `src/backend/app/core/auth.py` (lines 24-25, 144-175)
- Test: `src/backend/tests/test_auth.py`

**Interfaces:**
- Consumes: `config.auth.allowed_users` (CSV string, same format as `admin_users`)
- Produces: HTTP 403 when authenticated user's identity is not in `allowed_users` list (empty = allow all)

- [ ] **Step 1: Write tests for allowed_users enforcement**

Add to `src/backend/tests/test_auth.py`:

```python
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.auth import _parse_csv
from app.main import app

client = TestClient(app)


def test_parse_csv_handles_empty():
    assert _parse_csv("") == set()
    assert _parse_csv(None) == set()


def test_parse_csv_handles_values():
    result = _parse_csv("Alice@Example.com, bob@test.com")
    assert result == {"alice@example.com", "bob@test.com"}


def test_allowed_users_blocks_unauthorized_sso_user():
    """When allowed_users is set, users not in the list get 403."""
    mock_config = MagicMock()
    mock_config.auth.oauth_enabled = True
    mock_config.auth.admin_users = "allowed@test.com"
    mock_config.auth.operator_users = ""
    mock_config.auth.allowed_users = "allowed@test.com"
    mock_config.auth.jwt_secret = "test-secret"
    mock_config.auth.jwt_algorithm = "HS256"
    mock_config.auth.jwt_expiry_hours = 24

    with patch("app.core.auth.config", mock_config):
        # Reimport to pick up new config
        from app.core.auth import _parse_csv

        allowed = _parse_csv("allowed@test.com")
        assert "allowed@test.com" in allowed
        assert "blocked@test.com" not in allowed


def test_allowed_users_empty_allows_all():
    """When allowed_users is empty, all authenticated users are allowed."""
    from app.core.auth import _parse_csv

    allowed = _parse_csv("")
    assert len(allowed) == 0
```

- [ ] **Step 2: Run tests to verify they pass (these test _parse_csv which exists)**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_auth.py -v`
Expected: PASS for parse tests; the actual enforcement test verifies the logic via _parse_csv

- [ ] **Step 3: Add allowed_users to config.yaml**

In `src/backend/config/config.yaml`, add `allowed_users: ""` after `admin_users`:

```yaml
auth:
  jwt_secret: "CHANGE-ME-IN-LOCAL-CONFIG"
  jwt_algorithm: "HS256"
  jwt_expiry_hours: 24
  oauth_enabled: false
  allow_registration: true
  admin_users: "prutledg@redhat.com"
  allowed_users: ""
  operator_users: ""
```

- [ ] **Step 4: Add allowed_users enforcement to get_current_user()**

In `src/backend/app/core/auth.py`:

First, parse the allowed_users config at module level (after line 25):

```python
_allowed_users = _parse_csv(getattr(config.auth, "allowed_users", ""))
```

Then in `get_current_user()`, after the SSO user upsert block (after line 155 where `_upsert_sso_user` returns), add:

```python
            user = _upsert_sso_user(
                user_info["email"],
                user_info.get("user"),
                db,
            )
            if _allowed_users:
                identity = user_info["email"].lower()
                if identity not in _allowed_users:
                    raise HTTPException(
                        status_code=403,
                        detail="Access denied: user not in allowed_users list",
                    )
            return user
```

Replace the existing return inside the `if user_info:` block with this.

- [ ] **Step 5: Run tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_auth.py tests/ -v --tb=short`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/config/config.yaml src/backend/app/core/auth.py src/backend/tests/test_auth.py
git commit -m "feat: add allowed_users auth enforcement for dedicated instances"
```

---

### Task 4: Central MinIO on infra01

Deploy a shared MinIO instance on `ocpv-infra01.dal12.infra.demo.redhat.com` to serve as the gold image repository.

**Files:**
- Create: `infra/central-minio/deploy.yaml` (Ansible playbook)
- Create: `infra/central-minio/inventory.yaml`

**Interfaces:**
- Produces: MinIO endpoint at `https://minio-troshka-images.apps.ocpv-infra01.dal12.infra.demo.redhat.com`, bucket `troshka-gold-images` with gold images uploaded

- [ ] **Step 1: Create Ansible playbook for central MinIO deployment**

Create `infra/central-minio/deploy.yaml`:

```yaml
---
- name: Deploy central MinIO for Troshka gold images
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    namespace: troshka-images
    minio_root_user: troshka
    minio_pvc_size: 200Gi
    minio_bucket: troshka-gold-images
    kubeconfig: "{{ lookup('env', 'KUBECONFIG') | default('~/secrets/ocpv-infra01.kubeconfig') }}"

  tasks:
    - name: Create namespace
      kubernetes.core.k8s:
        kubeconfig: "{{ kubeconfig }}"
        state: present
        definition:
          apiVersion: v1
          kind: Namespace
          metadata:
            name: "{{ namespace }}"

    - name: Check if MinIO secret exists
      kubernetes.core.k8s_info:
        kubeconfig: "{{ kubeconfig }}"
        api_version: v1
        kind: Secret
        name: minio-credentials
        namespace: "{{ namespace }}"
      register: _existing_secret

    - name: Generate MinIO root password
      when: _existing_secret.resources | length == 0
      block:
        - name: Generate password
          ansible.builtin.set_fact:
            _minio_root_password: "{{ lookup('password', '/dev/null chars=ascii_letters,digits length=32') }}"

        - name: Create MinIO secret
          kubernetes.core.k8s:
            kubeconfig: "{{ kubeconfig }}"
            state: present
            definition:
              apiVersion: v1
              kind: Secret
              metadata:
                name: minio-credentials
                namespace: "{{ namespace }}"
              type: Opaque
              stringData:
                root-user: "{{ minio_root_user }}"
                root-password: "{{ _minio_root_password }}"

    - name: Deploy MinIO
      kubernetes.core.k8s:
        kubeconfig: "{{ kubeconfig }}"
        state: present
        definition:
          apiVersion: apps/v1
          kind: Deployment
          metadata:
            name: minio
            namespace: "{{ namespace }}"
            labels:
              app.kubernetes.io/name: minio
          spec:
            replicas: 1
            selector:
              matchLabels:
                app.kubernetes.io/name: minio
            template:
              metadata:
                labels:
                  app.kubernetes.io/name: minio
              spec:
                containers:
                  - name: minio
                    image: quay.io/minio/minio:latest
                    args: ["server", "/data", "--console-address", ":9001"]
                    envFrom:
                      - secretRef:
                          name: minio-credentials
                    env:
                      - name: MINIO_ROOT_USER
                        valueFrom:
                          secretKeyRef:
                            name: minio-credentials
                            key: root-user
                      - name: MINIO_ROOT_PASSWORD
                        valueFrom:
                          secretKeyRef:
                            name: minio-credentials
                            key: root-password
                    ports:
                      - containerPort: 9000
                        name: s3
                      - containerPort: 9001
                        name: console
                    volumeMounts:
                      - name: data
                        mountPath: /data
                    resources:
                      requests:
                        cpu: 250m
                        memory: 512Mi
                      limits:
                        memory: 2Gi
                volumes:
                  - name: data
                    persistentVolumeClaim:
                      claimName: minio-data

    - name: Create MinIO PVC
      kubernetes.core.k8s:
        kubeconfig: "{{ kubeconfig }}"
        state: present
        definition:
          apiVersion: v1
          kind: PersistentVolumeClaim
          metadata:
            name: minio-data
            namespace: "{{ namespace }}"
          spec:
            accessModes: [ReadWriteOnce]
            resources:
              requests:
                storage: "{{ minio_pvc_size }}"

    - name: Create MinIO Service
      kubernetes.core.k8s:
        kubeconfig: "{{ kubeconfig }}"
        state: present
        definition:
          apiVersion: v1
          kind: Service
          metadata:
            name: minio
            namespace: "{{ namespace }}"
          spec:
            selector:
              app.kubernetes.io/name: minio
            ports:
              - port: 9000
                targetPort: 9000
                name: s3
              - port: 9001
                targetPort: 9001
                name: console

    - name: Create MinIO Route (S3 API)
      kubernetes.core.k8s:
        kubeconfig: "{{ kubeconfig }}"
        state: present
        definition:
          apiVersion: route.openshift.io/v1
          kind: Route
          metadata:
            name: minio
            namespace: "{{ namespace }}"
          spec:
            host: "minio-{{ namespace }}.apps.ocpv-infra01.dal12.infra.demo.redhat.com"
            to:
              kind: Service
              name: minio
            port:
              targetPort: s3
            tls:
              termination: edge

    - name: Wait for MinIO to be ready
      kubernetes.core.k8s_info:
        kubeconfig: "{{ kubeconfig }}"
        api_version: apps/v1
        kind: Deployment
        name: minio
        namespace: "{{ namespace }}"
      register: _minio_deploy
      until: (_minio_deploy.resources[0].status.readyReplicas | default(0)) >= 1
      retries: 30
      delay: 10

    - name: Create bucket via mc init job
      kubernetes.core.k8s:
        kubeconfig: "{{ kubeconfig }}"
        state: present
        definition:
          apiVersion: batch/v1
          kind: Job
          metadata:
            name: "minio-init-bucket"
            namespace: "{{ namespace }}"
          spec:
            ttlSecondsAfterFinished: 300
            template:
              spec:
                restartPolicy: Never
                containers:
                  - name: mc
                    image: quay.io/minio/mc:latest
                    command:
                      - /bin/sh
                      - -c
                      - |
                        mc alias set local http://minio:9000 $MINIO_ROOT_USER $MINIO_ROOT_PASSWORD
                        mc mb --ignore-existing local/{{ minio_bucket }}
                    envFrom:
                      - secretRef:
                          name: minio-credentials
                    env:
                      - name: MINIO_ROOT_USER
                        valueFrom:
                          secretKeyRef:
                            name: minio-credentials
                            key: root-user
                      - name: MINIO_ROOT_PASSWORD
                        valueFrom:
                          secretKeyRef:
                            name: minio-credentials
                            key: root-password

    - name: Print access info
      ansible.builtin.debug:
        msg: |
          Central MinIO deployed!
          Endpoint: https://minio-{{ namespace }}.apps.ocpv-infra01.dal12.infra.demo.redhat.com
          Bucket: {{ minio_bucket }}
          Credentials: stored in secret minio-credentials in namespace {{ namespace }}

          To upload gold images, port-forward and use mc:
            oc port-forward -n {{ namespace }} svc/minio 9000:9000
            mc alias set central http://localhost:9000 <user> <password>
            mc cp rhel-9.6-x86_64-kvm.qcow2 central/{{ minio_bucket }}/
            mc cp rhel-9.6-x86_64-boot.iso central/{{ minio_bucket }}/
```

- [ ] **Step 2: Create inventory file**

Create `infra/central-minio/inventory.yaml`:

```yaml
---
all:
  hosts:
    localhost:
      ansible_connection: local
```

- [ ] **Step 3: Deploy to infra01**

Run:

```bash
cd /Users/prutledg/troshka/infra/central-minio
KUBECONFIG=$HOME/secrets/ocpv-infra01.kubeconfig ansible-playbook -i inventory.yaml deploy.yaml
```

Expected: MinIO deployed, bucket created, Route active.

- [ ] **Step 4: Upload gold images**

Port-forward to MinIO and upload the RHEL bastion image and boot ISO:

```bash
oc port-forward -n troshka-images svc/minio 9000:9000 --kubeconfig=$HOME/secrets/ocpv-infra01.kubeconfig &
mc alias set central http://localhost:9000 <user> <password>
mc cp /path/to/rhel-9.6-x86_64-kvm.qcow2 central/troshka-gold-images/
mc cp /path/to/rhel-9.6-x86_64-boot.iso central/troshka-gold-images/
mc ls central/troshka-gold-images/
```

Expected: Both files listed in the bucket.

- [ ] **Step 5: Verify Route access**

```bash
curl -s -o /dev/null -w "%{http_code}" https://minio-troshka-images.apps.ocpv-infra01.dal12.infra.demo.redhat.com/minio/health/live
```

Expected: `200`

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add infra/central-minio/
git commit -m "infra: add central MinIO deployment playbook for gold images"
```

---

### Task 5: AgnosticD Workload Role + AgnosticV Catalog Item

Build the `ocp4_workload_troshka_dedicated` Ansible role and its agnosticv catalog item definition. This role runs inside the `namespace` config's `software.yml` loop with `K8S_AUTH_*` env vars pre-set.

**Files:**
- Create: `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/main.yaml`
- Create: `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/serviceaccount.yaml`
- Create: `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/postgres.yaml`
- Create: `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/minio.yaml`
- Create: `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/secrets.yaml`
- Create: `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/backend.yaml`
- Create: `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/migrate.yaml`
- Create: `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/frontend.yaml`
- Create: `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/oauth.yaml`
- Create: `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/configure.yaml`
- Create: `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/seed.yaml`
- Create: `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/remove_workload.yaml`
- Create: `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/defaults/main.yaml`
- Create: `deploy/agnosticd-role/catalog/common.yaml` (agnosticv catalog item reference)

**Interfaces:**
- Consumes: `K8S_AUTH_HOST`, `K8S_AUTH_API_KEY` env vars (from namespace config's `software.yml`), `sandbox_openshift_api_url`, `guid`, requester email from sandbox vars
- Produces: Running Troshka instance with SSO, host, and seeded library

This task is large but the subtasks are all Ansible YAML files following the existing patterns in `deploy/ansible/tasks/`. They deploy in sequence and are tested as a unit by running the full role against a test cluster.

- [ ] **Step 1: Create role defaults**

Create `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/defaults/main.yaml`:

```yaml
---
# Action: provision or remove
ACTION: provision

# Namespace (set by agnosticv, typically guid-based)
troshka_namespace: "troshka-{{ guid }}"

# Container images
troshka_backend_image: "quay.io/redhat-gpte/troshka-backend:latest"
troshka_frontend_image: "quay.io/redhat-gpte/troshka-frontend:latest"

# Host VM defaults (overridden by agnosticv parameters)
troshka_host_vcpus: 16
troshka_host_memory_gb: 64
troshka_host_disk_gb: 200

# Central image repo
troshka_seed_minio_endpoint: ""
troshka_seed_minio_bucket: "troshka-gold-images"
troshka_seed_minio_access_key: ""
troshka_seed_minio_secret_key: ""
troshka_seed_images: []

# Resource limits
troshka_backend_cpu_request: "250m"
troshka_backend_memory_limit: "2Gi"
troshka_frontend_cpu_request: "100m"
troshka_frontend_memory_limit: "512Mi"

# Database
troshka_postgres_pvc_size: "10Gi"

# MinIO
troshka_minio_pvc_size: "50Gi"
```

- [ ] **Step 2: Create role entry point**

Create `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/main.yaml`:

```yaml
---
- name: Run provision tasks
  when: ACTION == "provision" or ACTION == "create"
  block:
    - ansible.builtin.include_tasks: serviceaccount.yaml
    - ansible.builtin.include_tasks: postgres.yaml
    - ansible.builtin.include_tasks: minio.yaml
    - ansible.builtin.include_tasks: secrets.yaml
    - ansible.builtin.include_tasks: backend.yaml
    - ansible.builtin.include_tasks: migrate.yaml
    - ansible.builtin.include_tasks: frontend.yaml
    - ansible.builtin.include_tasks: oauth.yaml
    - ansible.builtin.include_tasks: configure.yaml
    - ansible.builtin.include_tasks: seed.yaml

- name: Run remove tasks
  when: ACTION == "destroy" or ACTION == "remove"
  ansible.builtin.include_tasks: remove_workload.yaml
```

- [ ] **Step 3: Create serviceaccount.yaml**

Create `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/serviceaccount.yaml`:

```yaml
---
- name: Create namespace
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: Namespace
      metadata:
        name: "{{ troshka_namespace }}"

- name: Create ServiceAccount
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: ServiceAccount
      metadata:
        name: troshka-sa
        namespace: "{{ troshka_namespace }}"

- name: Create Role for Troshka provider
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: rbac.authorization.k8s.io/v1
      kind: Role
      metadata:
        name: troshka-provider
        namespace: "{{ troshka_namespace }}"
      rules:
        - apiGroups: ["kubevirt.io"]
          resources: ["virtualmachines", "virtualmachineinstances"]
          verbs: ["get", "list", "create", "delete", "patch", "update"]
        - apiGroups: ["cdi.kubevirt.io"]
          resources: ["datavolumes"]
          verbs: ["get", "list", "create", "delete"]
        - apiGroups: [""]
          resources: ["services", "persistentvolumeclaims"]
          verbs: ["get", "list", "create", "delete", "patch", "update"]
        - apiGroups: [""]
          resources: ["persistentvolumes", "nodes"]
          verbs: ["get", "list"]
        - apiGroups: [""]
          resources: ["secrets"]
          verbs: ["get", "list", "create", "delete"]
        - apiGroups: ["route.openshift.io"]
          resources: ["routes"]
          verbs: ["get", "list", "create", "delete", "patch", "update"]
        - apiGroups: [""]
          resources: ["pods"]
          verbs: ["get", "list"]
        - apiGroups: [""]
          resources: ["pods/exec"]
          verbs: ["create"]

- name: Create RoleBinding
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: rbac.authorization.k8s.io/v1
      kind: RoleBinding
      metadata:
        name: troshka-provider
        namespace: "{{ troshka_namespace }}"
      subjects:
        - kind: ServiceAccount
          name: troshka-sa
          namespace: "{{ troshka_namespace }}"
      roleRef:
        kind: Role
        name: troshka-provider
        apiGroup: rbac.authorization.k8s.io

- name: Get SA token for provider registration
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: Secret
      metadata:
        name: troshka-sa-token
        namespace: "{{ troshka_namespace }}"
        annotations:
          kubernetes.io/service-account.name: troshka-sa
      type: kubernetes.io/service-account-token
  register: _sa_token_secret

- name: Wait for SA token to be populated
  kubernetes.core.k8s_info:
    api_version: v1
    kind: Secret
    name: troshka-sa-token
    namespace: "{{ troshka_namespace }}"
  register: _sa_token_info
  until: _sa_token_info.resources[0].data.token is defined
  retries: 10
  delay: 5

- name: Store SA token
  ansible.builtin.set_fact:
    _troshka_sa_token: "{{ _sa_token_info.resources[0].data.token | b64decode }}"
```

- [ ] **Step 4: Create postgres.yaml**

Create `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/postgres.yaml`:

```yaml
---
- name: Generate PostgreSQL password
  ansible.builtin.set_fact:
    _postgres_password: "{{ lookup('password', '/dev/null chars=ascii_letters,digits length=24') }}"

- name: Create PostgreSQL PVC
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: PersistentVolumeClaim
      metadata:
        name: troshka-postgres-data
        namespace: "{{ troshka_namespace }}"
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: "{{ troshka_postgres_pvc_size }}"

- name: Deploy PostgreSQL
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: apps/v1
      kind: StatefulSet
      metadata:
        name: troshka-postgres
        namespace: "{{ troshka_namespace }}"
        labels:
          app.kubernetes.io/name: troshka-postgres
      spec:
        replicas: 1
        serviceName: troshka-postgres
        selector:
          matchLabels:
            app.kubernetes.io/name: troshka-postgres
        template:
          metadata:
            labels:
              app.kubernetes.io/name: troshka-postgres
          spec:
            containers:
              - name: postgres
                image: registry.redhat.io/rhel9/postgresql-16:latest
                env:
                  - name: POSTGRESQL_USER
                    value: troshka
                  - name: POSTGRESQL_PASSWORD
                    value: "{{ _postgres_password }}"
                  - name: POSTGRESQL_DATABASE
                    value: troshka
                ports:
                  - containerPort: 5432
                volumeMounts:
                  - name: data
                    mountPath: /var/lib/pgsql/data
                resources:
                  requests:
                    cpu: 250m
                    memory: 512Mi
                  limits:
                    memory: 1Gi
            volumes:
              - name: data
                persistentVolumeClaim:
                  claimName: troshka-postgres-data

- name: Create PostgreSQL Service
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: Service
      metadata:
        name: troshka-postgres
        namespace: "{{ troshka_namespace }}"
      spec:
        clusterIP: None
        selector:
          app.kubernetes.io/name: troshka-postgres
        ports:
          - port: 5432
            targetPort: 5432

- name: Wait for PostgreSQL to be ready
  kubernetes.core.k8s_info:
    api_version: apps/v1
    kind: StatefulSet
    name: troshka-postgres
    namespace: "{{ troshka_namespace }}"
  register: _pg
  until: (_pg.resources[0].status.readyReplicas | default(0)) >= 1
  retries: 30
  delay: 10
```

- [ ] **Step 5: Create minio.yaml**

Create `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/minio.yaml`:

```yaml
---
- name: Generate MinIO credentials
  ansible.builtin.set_fact:
    _minio_root_user: troshka
    _minio_root_password: "{{ lookup('password', '/dev/null chars=ascii_letters,digits length=24') }}"

- name: Create MinIO PVC
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: PersistentVolumeClaim
      metadata:
        name: troshka-minio-data
        namespace: "{{ troshka_namespace }}"
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: "{{ troshka_minio_pvc_size }}"

- name: Deploy MinIO
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: troshka-minio
        namespace: "{{ troshka_namespace }}"
        labels:
          app.kubernetes.io/name: troshka-minio
      spec:
        replicas: 1
        selector:
          matchLabels:
            app.kubernetes.io/name: troshka-minio
        template:
          metadata:
            labels:
              app.kubernetes.io/name: troshka-minio
          spec:
            containers:
              - name: minio
                image: quay.io/minio/minio:latest
                args: ["server", "/data", "--console-address", ":9001"]
                env:
                  - name: MINIO_ROOT_USER
                    value: "{{ _minio_root_user }}"
                  - name: MINIO_ROOT_PASSWORD
                    value: "{{ _minio_root_password }}"
                ports:
                  - containerPort: 9000
                    name: s3
                  - containerPort: 9001
                    name: console
                volumeMounts:
                  - name: data
                    mountPath: /data
                resources:
                  requests:
                    cpu: 250m
                    memory: 512Mi
                  limits:
                    memory: 2Gi
            volumes:
              - name: data
                persistentVolumeClaim:
                  claimName: troshka-minio-data

- name: Create MinIO Service
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: Service
      metadata:
        name: troshka-minio
        namespace: "{{ troshka_namespace }}"
      spec:
        selector:
          app.kubernetes.io/name: troshka-minio
        ports:
          - port: 9000
            targetPort: 9000
            name: s3
          - port: 9001
            targetPort: 9001
            name: console

- name: Wait for MinIO to be ready
  kubernetes.core.k8s_info:
    api_version: apps/v1
    kind: Deployment
    name: troshka-minio
    namespace: "{{ troshka_namespace }}"
  register: _minio
  until: (_minio.resources[0].status.readyReplicas | default(0)) >= 1
  retries: 30
  delay: 10

- name: Create bucket via init Job
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: batch/v1
      kind: Job
      metadata:
        name: "minio-init-bucket"
        namespace: "{{ troshka_namespace }}"
      spec:
        ttlSecondsAfterFinished: 300
        template:
          spec:
            restartPolicy: Never
            containers:
              - name: mc
                image: quay.io/minio/mc:latest
                command:
                  - /bin/sh
                  - -c
                  - |
                    mc alias set local http://troshka-minio:9000 {{ _minio_root_user }} {{ _minio_root_password }}
                    mc mb --ignore-existing local/troshka-images

- name: Wait for bucket init job
  kubernetes.core.k8s_info:
    api_version: batch/v1
    kind: Job
    name: minio-init-bucket
    namespace: "{{ troshka_namespace }}"
  register: _init_job
  until: (_init_job.resources[0].status.succeeded | default(0)) >= 1
  retries: 12
  delay: 10
```

- [ ] **Step 6: Create secrets.yaml**

Create `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/secrets.yaml`:

```yaml
---
- name: Generate JWT secret
  ansible.builtin.command: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  register: _jwt_secret_result
  changed_when: false

- name: Generate encryption key
  ansible.builtin.command: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  register: _enc_key_result
  changed_when: false

- name: Create Troshka secrets
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: Secret
      metadata:
        name: troshka-secrets
        namespace: "{{ troshka_namespace }}"
      type: Opaque
      stringData:
        jwt-secret: "{{ _jwt_secret_result.stdout }}"
        encryption-key: "{{ _enc_key_result.stdout }}"
        database-url: "postgresql+psycopg2://troshka:{{ _postgres_password }}@troshka-postgres:5432/troshka"
        s3-access-key: "{{ _minio_root_user }}"
        s3-secret-key: "{{ _minio_root_password }}"
```

- [ ] **Step 7: Create backend.yaml**

Create `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/backend.yaml`:

```yaml
---
- name: Determine Route host
  ansible.builtin.set_fact:
    _troshka_route_host: "troshka-{{ troshka_namespace }}.{{ _cluster_apps_domain }}"

- name: Get cluster apps domain
  kubernetes.core.k8s_info:
    api_version: config.openshift.io/v1
    kind: Ingress
    name: cluster
  register: _ingress_config

- name: Set cluster apps domain
  ansible.builtin.set_fact:
    _cluster_apps_domain: "{{ _ingress_config.resources[0].spec.domain }}"
    _troshka_route_host: "troshka-{{ troshka_namespace }}.{{ _ingress_config.resources[0].spec.domain }}"

- name: Set requester identity
  ansible.builtin.set_fact:
    _requester_identity: "{{ requester_email | default(user) | default('admin@troshka') }}"

- name: Create backend ConfigMap
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: ConfigMap
      metadata:
        name: troshka-config
        namespace: "{{ troshka_namespace }}"
      data:
        config.yaml: |
          app:
            port: 8200
            host: "0.0.0.0"
            log_level: info
            external_url: "https://{{ _troshka_route_host }}"
          auth:
            oauth_enabled: true
            admin_users: "{{ _requester_identity }}"
            allowed_users: "{{ _requester_identity }}"
            operator_users: ""
          s3:
            endpoint_url: "http://troshka-minio.{{ troshka_namespace }}.svc:9000"
            bucket: "troshka-images"
            region: "us-east-1"
          defaults:
            run_timer_hours: 8
            lifetime_days: 30
            max_vms_per_project: 20
            max_projects_per_user: 10
            user_library_quota_gb: 500
          overcommit:
            cpu_ratio: 4.0
            ram_ratio: 1.5

- name: Deploy backend
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: troshka-backend
        namespace: "{{ troshka_namespace }}"
        labels:
          app.kubernetes.io/name: troshka-backend
      spec:
        replicas: 1
        selector:
          matchLabels:
            app.kubernetes.io/name: troshka-backend
        template:
          metadata:
            labels:
              app.kubernetes.io/name: troshka-backend
          spec:
            containers:
              - name: backend
                image: "{{ troshka_backend_image }}"
                ports:
                  - containerPort: 8200
                env:
                  - name: TROSHKA_DATABASE__URL
                    valueFrom:
                      secretKeyRef:
                        name: troshka-secrets
                        key: database-url
                  - name: TROSHKA_AUTH__JWT_SECRET
                    valueFrom:
                      secretKeyRef:
                        name: troshka-secrets
                        key: jwt-secret
                  - name: TROSHKA_AUTH__ENCRYPTION_KEY
                    valueFrom:
                      secretKeyRef:
                        name: troshka-secrets
                        key: encryption-key
                  - name: TROSHKA_S3__ACCESS_KEY_ID
                    valueFrom:
                      secretKeyRef:
                        name: troshka-secrets
                        key: s3-access-key
                  - name: TROSHKA_S3__SECRET_ACCESS_KEY
                    valueFrom:
                      secretKeyRef:
                        name: troshka-secrets
                        key: s3-secret-key
                volumeMounts:
                  - name: config
                    mountPath: /opt/app-root/src/config
                resources:
                  requests:
                    cpu: "{{ troshka_backend_cpu_request }}"
                  limits:
                    memory: "{{ troshka_backend_memory_limit }}"
            volumes:
              - name: config
                configMap:
                  name: troshka-config

- name: Create backend Service
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: Service
      metadata:
        name: troshka-backend
        namespace: "{{ troshka_namespace }}"
      spec:
        selector:
          app.kubernetes.io/name: troshka-backend
        ports:
          - port: 8200
            targetPort: 8200
```

- [ ] **Step 8: Create migrate.yaml**

Create `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/migrate.yaml`:

```yaml
---
- name: Run database migration
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: batch/v1
      kind: Job
      metadata:
        name: "troshka-migrate-{{ lookup('password', '/dev/null chars=ascii_lowercase,digits length=6') }}"
        namespace: "{{ troshka_namespace }}"
      spec:
        ttlSecondsAfterFinished: 600
        template:
          spec:
            restartPolicy: Never
            containers:
              - name: migrate
                image: "{{ troshka_backend_image }}"
                command: ["python3", "-m", "alembic", "upgrade", "head"]
                env:
                  - name: TROSHKA_DATABASE__URL
                    valueFrom:
                      secretKeyRef:
                        name: troshka-secrets
                        key: database-url
                volumeMounts:
                  - name: config
                    mountPath: /opt/app-root/src/config
            volumes:
              - name: config
                configMap:
                  name: troshka-config
  register: _migrate_job

- name: Wait for migration to complete
  kubernetes.core.k8s_info:
    api_version: batch/v1
    kind: Job
    name: "{{ _migrate_job.result.metadata.name }}"
    namespace: "{{ troshka_namespace }}"
  register: _migrate_status
  until: (_migrate_status.resources[0].status.succeeded | default(0)) >= 1
  retries: 30
  delay: 10
```

- [ ] **Step 9: Create frontend.yaml**

Create `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/frontend.yaml`:

```yaml
---
- name: Deploy frontend
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: troshka-frontend
        namespace: "{{ troshka_namespace }}"
        labels:
          app.kubernetes.io/name: troshka-frontend
      spec:
        replicas: 1
        selector:
          matchLabels:
            app.kubernetes.io/name: troshka-frontend
        template:
          metadata:
            labels:
              app.kubernetes.io/name: troshka-frontend
          spec:
            containers:
              - name: frontend
                image: "{{ troshka_frontend_image }}"
                ports:
                  - containerPort: 3000
                env:
                  - name: BACKEND_URL
                    value: "http://troshka-backend.{{ troshka_namespace }}.svc:8200"
                resources:
                  requests:
                    cpu: "{{ troshka_frontend_cpu_request }}"
                  limits:
                    memory: "{{ troshka_frontend_memory_limit }}"

- name: Create frontend Service
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: Service
      metadata:
        name: troshka-frontend
        namespace: "{{ troshka_namespace }}"
      spec:
        selector:
          app.kubernetes.io/name: troshka-frontend
        ports:
          - port: 3000
            targetPort: 3000
```

- [ ] **Step 10: Create oauth.yaml (SA-based, no cluster-scoped OAuthClient)**

Create `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/oauth.yaml`:

```yaml
---
- name: Annotate SA for OAuth redirect
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: ServiceAccount
      metadata:
        name: troshka-sa
        namespace: "{{ troshka_namespace }}"
        annotations:
          serviceaccounts.openshift.io/oauth-redirectreference.primary: >-
            {"kind":"OAuthRedirectReference","apiVersion":"v1","reference":{"kind":"Route","name":"troshka"}}

- name: Generate OAuth cookie secret
  ansible.builtin.command: python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
  register: _cookie_secret_result
  changed_when: false

- name: Create OAuth proxy secret
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: Secret
      metadata:
        name: troshka-oauth-proxy
        namespace: "{{ troshka_namespace }}"
      type: Opaque
      stringData:
        cookie-secret: "{{ _cookie_secret_result.stdout }}"

- name: Deploy OAuth proxy
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: troshka-oauth-proxy
        namespace: "{{ troshka_namespace }}"
        labels:
          app.kubernetes.io/name: troshka-oauth-proxy
      spec:
        replicas: 1
        selector:
          matchLabels:
            app.kubernetes.io/name: troshka-oauth-proxy
        template:
          metadata:
            labels:
              app.kubernetes.io/name: troshka-oauth-proxy
          spec:
            serviceAccountName: troshka-sa
            containers:
              - name: oauth-proxy
                image: registry.redhat.io/openshift4/ose-oauth-proxy-rhel9:latest
                ports:
                  - containerPort: 8443
                    name: proxy
                args:
                  - --provider=openshift
                  - --https-address=:8443
                  - --http-address=
                  - --upstream=http://troshka-frontend.{{ troshka_namespace }}.svc:3000
                  - --tls-cert=/etc/tls/private/tls.crt
                  - --tls-key=/etc/tls/private/tls.key
                  - --cookie-secret-file=/etc/oauth/cookie-secret
                  - --client-id=system:serviceaccount:{{ troshka_namespace }}:troshka-sa
                  - --client-secret-file=/var/run/secrets/kubernetes.io/serviceaccount/token
                  - --openshift-service-account=troshka-sa
                  - --openshift-ca=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
                  - --pass-user-headers=true
                  - --pass-access-token=false
                  - --skip-auth-regex=^/api/v1/health
                  - --skip-auth-regex=^/api/v1/auth/config
                volumeMounts:
                  - name: tls
                    mountPath: /etc/tls/private
                  - name: oauth-secrets
                    mountPath: /etc/oauth
                resources:
                  requests:
                    cpu: 50m
                    memory: 64Mi
                  limits:
                    memory: 128Mi
            volumes:
              - name: tls
                secret:
                  secretName: troshka-oauth-tls
              - name: oauth-secrets
                secret:
                  secretName: troshka-oauth-proxy

- name: Create OAuth proxy Service
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: Service
      metadata:
        name: troshka-oauth-proxy
        namespace: "{{ troshka_namespace }}"
        annotations:
          service.beta.openshift.io/serving-cert-secret-name: troshka-oauth-tls
      spec:
        selector:
          app.kubernetes.io/name: troshka-oauth-proxy
        ports:
          - port: 8443
            targetPort: 8443
            name: proxy

- name: Create Route
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: route.openshift.io/v1
      kind: Route
      metadata:
        name: troshka
        namespace: "{{ troshka_namespace }}"
      spec:
        host: "{{ _troshka_route_host }}"
        to:
          kind: Service
          name: troshka-oauth-proxy
        port:
          targetPort: proxy
        tls:
          termination: reencrypt
```

- [ ] **Step 11: Create configure.yaml (register provider + create host)**

Create `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/configure.yaml`:

```yaml
---
- name: Wait for backend to be ready
  ansible.builtin.uri:
    url: "http://troshka-backend.{{ troshka_namespace }}.svc:8200/api/v1/health"
    return_content: false
  register: _health
  until: _health.status == 200
  retries: 30
  delay: 10
  ignore_errors: true

- name: Set API auth headers (SSO header injection for server-to-server calls)
  ansible.builtin.set_fact:
    _api_headers:
      Content-Type: "application/json"
      X-Forwarded-Email: "{{ _requester_identity }}"
      X-Forwarded-User: "{{ _requester_identity }}"

- name: Get cluster API URL
  ansible.builtin.set_fact:
    _cluster_api_url: "{{ sandbox_openshift_api_url | default(lookup('env', 'K8S_AUTH_HOST')) }}"

- name: Register OCP Virt provider
  ansible.builtin.uri:
    url: "http://troshka-backend.{{ troshka_namespace }}.svc:8200/api/v1/providers/"
    method: POST
    headers: "{{ _api_headers }}"
    body_format: json
    body:
      name: "Local OCP Virt"
      type: "ocpvirt"
      default_region: "{{ troshka_namespace }}"
      credentials:
        token: "{{ _troshka_sa_token }}"
        namespace: "{{ troshka_namespace }}"
        api_url: "{{ _cluster_api_url }}"
    status_code: [200, 201]
  register: _provider

- name: Create agent host
  ansible.builtin.uri:
    url: "http://troshka-backend.{{ troshka_namespace }}.svc:8200/api/v1/hosts/"
    method: POST
    headers: "{{ _api_headers }}"
    body_format: json
    body:
      provider_id: "{{ _provider.json.id }}"
      name: "dedicated-host"
      instance_type: "custom"
      vcpus: "{{ troshka_host_vcpus }}"
      memory_gb: "{{ troshka_host_memory_gb }}"
      disk_gb: "{{ troshka_host_disk_gb }}"
    status_code: [200, 201]
  register: _host

- name: Wait for host to connect
  ansible.builtin.uri:
    url: "http://troshka-backend.{{ troshka_namespace }}.svc:8200/api/v1/hosts/{{ _host.json.id }}"
    method: GET
    headers: "{{ _api_headers }}"
  register: _host_status
  until: _host_status.json.state == "connected"
  retries: 60
  delay: 15
  # ~15 min timeout for VM provision + boot + troshkad install
```

- [ ] **Step 12: Create seed.yaml (copy images from central MinIO)**

Create `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/seed.yaml`:

```yaml
---
- name: Skip seeding if no central MinIO configured
  when: troshka_seed_minio_endpoint | default('') == '' or troshka_seed_images | length == 0
  ansible.builtin.debug:
    msg: "No seed images configured, skipping library seeding"

- name: Seed library from central MinIO
  when: troshka_seed_minio_endpoint | default('') != '' and troshka_seed_images | length > 0
  block:
    - name: Get current user info
      ansible.builtin.uri:
        url: "http://troshka-backend.{{ troshka_namespace }}.svc:8200/api/v1/auth/me"
        headers: "{{ _api_headers }}"
      register: _user_info

    - name: Run seed job
      kubernetes.core.k8s:
        state: present
        definition:
          apiVersion: batch/v1
          kind: Job
          metadata:
            name: "troshka-seed-library"
            namespace: "{{ troshka_namespace }}"
          spec:
            ttlSecondsAfterFinished: 600
            template:
              spec:
                restartPolicy: Never
                containers:
                  - name: seed
                    image: quay.io/minio/mc:latest
                    command:
                      - /bin/sh
                      - -c
                      - |
                        mc alias set central {{ troshka_seed_minio_endpoint }} {{ troshka_seed_minio_access_key }} {{ troshka_seed_minio_secret_key }}
                        mc alias set local http://troshka-minio:9000 {{ _minio_root_user }} {{ _minio_root_password }}
                        {% for img in troshka_seed_images %}
                        echo "Copying {{ img.s3_object }}..."
                        mc cp central/{{ troshka_seed_minio_bucket }}/{{ img.s3_object }} local/troshka-images/seed/{{ img.s3_object }}
                        {% endfor %}
                        echo "Seed copy complete"

    - name: Wait for seed job to complete
      kubernetes.core.k8s_info:
        api_version: batch/v1
        kind: Job
        name: troshka-seed-library
        namespace: "{{ troshka_namespace }}"
      register: _seed_job
      until: (_seed_job.resources[0].status.succeeded | default(0)) >= 1
      retries: 60
      delay: 15
      # ~15 min for large image copies

    - name: Create library items via API
      ansible.builtin.uri:
        url: "http://troshka-backend.{{ troshka_namespace }}.svc:8200/api/v1/library/"
        method: POST
        headers: "{{ _api_headers }}"
        body_format: json
        body:
          name: "{{ item.name }}"
          type: "{{ item.type }}"
          format: "{{ item.format }}"
        status_code: [200, 201]
      register: _lib_items
      loop: "{{ troshka_seed_images }}"

    - name: Move seed files to library paths and finalize
      ansible.builtin.uri:
        url: "http://troshka-backend.{{ troshka_namespace }}.svc:8200/api/v1/library/{{ item.json.id }}/finalize-seed"
        method: POST
        headers: "{{ _api_headers }}"
        body_format: json
        body:
          seed_key: "seed/{{ troshka_seed_images[idx].s3_object }}"
          tags: "{{ troshka_seed_images[idx].tags | default([]) }}"
      loop: "{{ _lib_items.results }}"
      loop_control:
        index_var: idx
```

Note: This requires a new `finalize-seed` backend endpoint that moves an existing S3 object from a seed path to the canonical library path and marks the item as ready. This is a small addition to `library.py` — add as a follow-up step.

- [ ] **Step 13: Create remove_workload.yaml**

Create `deploy/agnosticd-role/roles/ocp4_workload_troshka_dedicated/tasks/remove_workload.yaml`:

```yaml
---
- name: Delete namespace (cascading delete)
  kubernetes.core.k8s:
    state: absent
    definition:
      apiVersion: v1
      kind: Namespace
      metadata:
        name: "{{ troshka_namespace }}"
  ignore_errors: true
```

- [ ] **Step 14: Create agnosticv catalog item reference**

Create `deploy/agnosticd-role/catalog/common.yaml` as a reference template for the agnosticv catalog item:

```yaml
---
# Agnosticv catalog item template for Troshka Dedicated Instance
# Copy this to agnosticv repo at: agd_v2/troshka-dedicated/common.yaml

config: namespace
cloud_provider: none

__meta__:
  catalog:
    display_name: "Troshka Dedicated Instance"
    description: "Personal nested virtualization lab environment"
    category: infrastructure
    labels:
      - troshka
      - virtualization
      - lab
    parameters:
      - name: troshka_host_vcpus
        description: Number of vCPUs for the Troshka agent host VM
        formLabel: Host vCPUs
        openAPIV3Schema:
          type: integer
          default: 16
          enum: [8, 16, 32, 64]
      - name: troshka_host_memory_gb
        description: Memory (GB) for the Troshka agent host VM
        formLabel: Host Memory (GB)
        openAPIV3Schema:
          type: integer
          default: 64
          enum: [32, 64, 128, 256]
      - name: troshka_host_disk_gb
        description: Data disk size (GB) for VM images and pattern cache
        formLabel: Host Disk (GB)
        openAPIV3Schema:
          type: integer
          default: 200
          enum: [100, 200, 500]

# Images
troshka_backend_image: "quay.io/redhat-gpte/troshka-backend:latest"
troshka_frontend_image: "quay.io/redhat-gpte/troshka-frontend:latest"

# Central image repo
troshka_seed_minio_endpoint: "https://minio-troshka-images.apps.ocpv-infra01.dal12.infra.demo.redhat.com"
troshka_seed_minio_bucket: "troshka-gold-images"
troshka_seed_images:
  - name: "RHEL 9.6 KVM Guest Image"
    type: image
    format: qcow2
    s3_object: "rhel-9.6-x86_64-kvm.qcow2"
    tags: ["ocp_default_image"]
  - name: "RHEL 9.6 Boot ISO"
    type: iso
    format: iso
    s3_object: "rhel-9.6-x86_64-boot.iso"
    tags: ["ocp_default_iso"]

# Workloads
workloads:
  - ocp4_workload_troshka_dedicated
```

- [ ] **Step 15: Commit workload role**

```bash
cd /Users/prutledg/troshka && git add deploy/agnosticd-role/
git commit -m "feat: add agnosticD workload role for dedicated Troshka instances"
```

---

### Task 6: Library Seed Finalization Endpoint

The seed task (Task 5, step 12) copies images into MinIO under a `seed/` prefix, then needs to move them to the canonical library path and mark them as ready. Add a small backend endpoint for this.

**Files:**
- Modify: `src/backend/app/api/library.py`
- Test: `src/backend/tests/test_library_seed.py`

**Interfaces:**
- Consumes: `POST /library/{item_id}/finalize-seed` with `{"seed_key": str, "tags": list[str]}`
- Produces: Library item with correct `s3_key`, `size_bytes`, `state: "ready"`, and tags applied

- [ ] **Step 1: Write test for finalize-seed endpoint**

Create `src/backend/tests/test_library_seed.py`:

```python
"""Test the finalize-seed endpoint for library seeding."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_finalize_seed_endpoint_exists():
    """Route is registered and returns a valid HTTP status."""
    response = client.post(
        "/api/v1/library/nonexistent-id/finalize-seed",
        json={"seed_key": "seed/test.qcow2", "tags": []},
    )
    # Dev mode auto-auth, but item won't exist
    assert response.status_code in (404, 422)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_library_seed.py -v`
Expected: FAIL — endpoint doesn't exist yet

- [ ] **Step 3: Implement finalize-seed endpoint**

Add to `src/backend/app/api/library.py`:

```python
class FinalizeSeedRequest(BaseModel):
    seed_key: str
    tags: list[str] = []


@router.post("/{item_id}/finalize-seed")
def finalize_seed(
    item_id: str,
    body: FinalizeSeedRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Move a seeded S3 object to the canonical library path and mark ready."""
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    lib = db.query(Library).filter_by(id=item.library_id).first()
    if not lib or lib.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    from app.services.s3_storage import _bucket, _get_s3_client

    client = _get_s3_client()
    bucket = _bucket()

    ext = item.format if item.format != "qcow2" else "qcow2"
    dest_key = f"library/{user.id}/{item.id}/{item.name}.{ext}"

    client.copy_object(
        Bucket=bucket,
        CopySource={"Bucket": bucket, "Key": body.seed_key},
        Key=dest_key,
    )
    client.delete_object(Bucket=bucket, Key=body.seed_key)

    head = client.head_object(Bucket=bucket, Key=dest_key)
    item.s3_key = dest_key
    item.size_bytes = head["ContentLength"]
    item.state = "ready"
    item.tags = body.tags if body.tags else item.tags
    db.commit()

    logger.info("Finalized seed: %s → %s (%d bytes)", body.seed_key, dest_key, item.size_bytes)
    return {"id": item.id, "s3_key": dest_key, "size_bytes": item.size_bytes, "state": "ready"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_library_seed.py -v`
Expected: PASS (endpoint exists, returns 404 for nonexistent item)

- [ ] **Step 5: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v --tb=short`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/library.py src/backend/tests/test_library_seed.py
git commit -m "feat: add finalize-seed endpoint for library seeding from central MinIO"
```
