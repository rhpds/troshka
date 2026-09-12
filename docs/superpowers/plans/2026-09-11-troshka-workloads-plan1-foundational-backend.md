# Troshka Workloads — Plan 1: Foundational Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the credential-safe, pod-free backend layer that resolves an AgnosticV catalog item into `{extra_vars, ee_image, scm_ref, requirements_content}` — the Central Secret Store, Vault decrypt, Repo Cache, and the `agnosticv`-binary-driven Merge Engine.

**Architecture:** Approach A (thin pod): all git credentials and the Vault key stay server-side. The backend clones/caches the private repos, drives the canonical `agnosticv` binary for the overlay merge, and decrypts `!vault` values itself. This plan delivers only the backend resolution layer — no runner pod, no API, no UI (those are Plans 2 and 3).

**Tech Stack:** Python 3.13, SQLAlchemy 2.0, Dynaconf, `cryptography` (already a dep), PyYAML (already a dep), the `agnosticv` Go binary, git 2.x. Tests: pytest with SQLite (`tests/conftest.py`).

**Spec:** `docs/superpowers/specs/2026-09-11-troshka-workloads-design.md`

## Global Constraints

- **Python 3.13** (CI and dev). Add trailing values to any `time.time()` mocks to avoid `StopIteration`.
- **Run tests** from `src/backend`: `./venv/bin/python3 -m pytest <path> -v`.
- **Format** with system `black` before every commit; fix all `pyright` errors (use `pyright`, the npm global — not `python3 -m pyright`), including pre-existing ones in files you touch.
- **Cognitive complexity ≤ 15 per function** (SonarQube S3776) — extract helpers rather than nesting.
- **No real network/SSH/troshkad I/O in unit tests.** Local `git` against `file://` repos in a tmp dir is allowed (it is the honest way to test the cache); guard such tests with `shutil.which("git")`. Real-`agnosticv`-binary tests are guarded with `shutil.which("agnosticv")` and skipped when absent (CI may not have the binary).
- **Never `drop_all`** the shared test engine; do not add fixtures that reset `test_engine`.
- **Secrets never in plaintext at rest**: all secret values are Fernet-encrypted via `app/core/encryption.py`.
- **New services live under** `src/backend/app/services/workloads/` (a subpackage, like `services/ocp/`). **Tests are flat** in `src/backend/tests/` named `test_workload_*.py` (matching the existing flat test convention).
- **Git commits:** no `Co-Authored-By` lines; never amend; use absolute paths or `cd /Users/prutledg/troshka && git add src/backend/...` (never `cd` into a subdir then `git add` a relative path).

---

### Task 1: Central Secret Store

Stores admin-central secrets (AgnosticV/AgnosticD git creds, the Vault key, named cloud-cred sets) encrypted at rest in the existing `system_config` key/value table, namespaced under `workload.secret.`.

**Files:**
- Create: `src/backend/app/services/workloads/__init__.py` (empty package marker)
- Create: `src/backend/app/services/workloads/secret_store.py`
- Test: `src/backend/tests/test_workload_secret_store.py`

**Interfaces:**
- Consumes: `app.core.encryption.encrypt/decrypt`; `app.models.system_config.SystemConfig`.
- Produces:
  - `set_secret(db: Session, name: str, value: str) -> None`
  - `get_secret(db: Session, name: str) -> str | None`
  - `delete_secret(db: Session, name: str) -> None`
  - `list_secret_names(db: Session) -> list[str]`
  - `set_json_secret(db: Session, name: str, obj) -> None`
  - `get_json_secret(db: Session, name: str)` (returns parsed object or `None`)

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_workload_secret_store.py
import json

from app.models.system_config import SystemConfig
from app.services.workloads import secret_store
from tests.conftest import TestSession


def test_set_get_roundtrip():
    db = TestSession()
    try:
        secret_store.set_secret(db, "vault_key_t1", "s3cr3t-pass")
        assert secret_store.get_secret(db, "vault_key_t1") == "s3cr3t-pass"
    finally:
        db.close()


def test_stored_value_is_encrypted_at_rest():
    db = TestSession()
    try:
        secret_store.set_secret(db, "vault_key_t2", "plaintext-value")
        row = db.get(SystemConfig, "workload.secret.vault_key_t2")
        assert row is not None
        assert row.value != "plaintext-value"  # ciphertext, not plaintext
    finally:
        db.close()


def test_update_overwrites():
    db = TestSession()
    try:
        secret_store.set_secret(db, "k_t3", "one")
        secret_store.set_secret(db, "k_t3", "two")
        assert secret_store.get_secret(db, "k_t3") == "two"
    finally:
        db.close()


def test_get_missing_returns_none():
    db = TestSession()
    try:
        assert secret_store.get_secret(db, "does-not-exist-t4") is None
    finally:
        db.close()


def test_delete_removes_secret():
    db = TestSession()
    try:
        secret_store.set_secret(db, "k_t5", "v")
        secret_store.delete_secret(db, "k_t5")
        assert secret_store.get_secret(db, "k_t5") is None
    finally:
        db.close()


def test_list_secret_names_includes_set_names():
    db = TestSession()
    try:
        secret_store.set_secret(db, "alpha_t6", "v")
        secret_store.set_secret(db, "beta_t6", "v")
        names = secret_store.list_secret_names(db)
        assert "alpha_t6" in names
        assert "beta_t6" in names
    finally:
        db.close()


def test_json_secret_roundtrip():
    db = TestSession()
    try:
        creds = {"url": "https://github.com/rhpds/agnosticv.git", "token": "abc"}
        secret_store.set_json_secret(db, "agnosticv_git_t7", creds)
        assert secret_store.get_json_secret(db, "agnosticv_git_t7") == creds
        # and it is stored encrypted, not as readable json
        row = db.get(SystemConfig, "workload.secret.agnosticv_git_t7")
        assert "github.com" not in row.value
    finally:
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python3 -m pytest tests/test_workload_secret_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.workloads'`.

- [ ] **Step 3: Create the package marker and implementation**

```python
# src/backend/app/services/workloads/__init__.py
```

(empty file)

```python
# src/backend/app/services/workloads/secret_store.py
"""Admin-central secret storage for the workloads subsystem.

Secrets (git credentials, the Ansible Vault key, named cloud-cred sets) are kept
encrypted at rest in the existing ``system_config`` key/value table, namespaced
under ``workload.secret.``. Values are Fernet-encrypted via
``app.core.encryption`` (the same mechanism used for pull secrets).
"""

import json

from sqlalchemy.orm import Session

from app.core.encryption import decrypt, encrypt
from app.models.system_config import SystemConfig

_PREFIX = "workload.secret."


def _key(name: str) -> str:
    return _PREFIX + name


def set_secret(db: Session, name: str, value: str) -> None:
    row = db.get(SystemConfig, _key(name))
    ciphertext = encrypt(value)
    if row is None:
        db.add(SystemConfig(key=_key(name), value=ciphertext))
    else:
        row.value = ciphertext
    db.commit()


def get_secret(db: Session, name: str) -> str | None:
    row = db.get(SystemConfig, _key(name))
    if row is None:
        return None
    return decrypt(row.value)


def delete_secret(db: Session, name: str) -> None:
    row = db.get(SystemConfig, _key(name))
    if row is not None:
        db.delete(row)
        db.commit()


def list_secret_names(db: Session) -> list[str]:
    rows = (
        db.query(SystemConfig)
        .filter(SystemConfig.key.like(_PREFIX + "%"))
        .all()
    )
    return sorted(r.key[len(_PREFIX):] for r in rows)


def set_json_secret(db: Session, name: str, obj) -> None:
    set_secret(db, name, json.dumps(obj))


def get_json_secret(db: Session, name: str):
    raw = get_secret(db, name)
    return None if raw is None else json.loads(raw)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python3 -m pytest tests/test_workload_secret_store.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Format, type-check, commit**

```bash
cd /Users/prutledg/troshka/src/backend && black app/services/workloads/secret_store.py tests/test_workload_secret_store.py && pyright app/services/workloads/secret_store.py
cd /Users/prutledg/troshka && git add src/backend/app/services/workloads/__init__.py src/backend/app/services/workloads/secret_store.py src/backend/tests/test_workload_secret_store.py
git commit -m "feat(workloads): add central secret store"
```

---

### Task 2: Ansible Vault decrypt (VaultAES256)

Self-contained decrypt of `$ANSIBLE_VAULT;1.1;AES256` values using `cryptography` only — no `ansible-core` in the backend. The Merge Engine (Task 4) calls this on `$ANSIBLE_VAULT`-prefixed strings in the merged output.

**Files:**
- Create: `src/backend/app/services/workloads/vault.py`
- Test: `src/backend/tests/test_workload_vault.py`

**Interfaces:**
- Consumes: `cryptography` primitives only.
- Produces:
  - `is_vault(text: str) -> bool`
  - `decrypt_vault(vaulttext: str, password: str) -> str`
  - `class VaultError(Exception)` (raised on wrong password / malformed input)

- [ ] **Step 1: Generate a real known-answer vector**

Run (records a real `ansible-vault` ciphertext for the password `testpass` and plaintext `hello-vault-value`):

```bash
printf 'testpass' > /tmp/vpw.txt
ansible-vault encrypt_string --vault-password-file /tmp/vpw.txt 'hello-vault-value' --name 'x' 2>/dev/null
rm -f /tmp/vpw.txt
```

Copy the emitted block (the lines from `x: !vault |` through the indented `$ANSIBLE_VAULT...` hex body). You only need the `$ANSIBLE_VAULT`-through-hex portion (strip the `x: !vault |` wrapper and the leading indentation) for the test fixture in Step 2.

- [ ] **Step 2: Write the failing test**

Paste the generated ciphertext into `REAL_VECTOR` below (header line + hex lines, no indentation, no `x: !vault |` wrapper).

```python
# src/backend/tests/test_workload_vault.py
import pytest

from app.services.workloads.vault import VaultError, decrypt_vault, is_vault

# Generated once with: ansible-vault encrypt_string --vault-password-file <(printf testpass) 'hello-vault-value'
REAL_VECTOR = """$ANSIBLE_VAULT;1.1;AES256
<PASTE_HEX_LINES_HERE>"""


def test_is_vault_detects_header():
    assert is_vault(REAL_VECTOR) is True
    assert is_vault("plain string") is False


def test_decrypt_real_ansible_vector():
    assert decrypt_vault(REAL_VECTOR, "testpass") == "hello-vault-value"


def test_wrong_password_raises():
    with pytest.raises(VaultError):
        decrypt_vault(REAL_VECTOR, "wrong-password")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./venv/bin/python3 -m pytest tests/test_workload_vault.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.workloads.vault'`.

- [ ] **Step 4: Write the implementation**

```python
# src/backend/app/services/workloads/vault.py
"""Decrypt Ansible Vault (VAULT;1.1;AES256) values without ansible-core.

Ansible Vault format:
  line 0: ``$ANSIBLE_VAULT;1.1;AES256``
  body:   hex of an ASCII string ``<salt_hex>\\n<hmac_hex>\\n<ciphertext_hex>``.
Key material is PBKDF2-HMAC-SHA256(password, salt, 10000, 80 bytes) split into
cipher key (32), HMAC key (32), IV (16). Integrity is HMAC-SHA256 over the
ciphertext; content is AES-256-CTR with PKCS7 padding.
"""

from binascii import unhexlify

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, hmac, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_HEADER = "$ANSIBLE_VAULT"
_ITERATIONS = 10000
_KEYLEN = 80  # 32 cipher + 32 hmac + 16 iv


class VaultError(Exception):
    pass


def is_vault(text: str) -> bool:
    return isinstance(text, str) and text.lstrip().startswith(_HEADER)


def _split_envelope(vaulttext: str) -> tuple[bytes, bytes, bytes]:
    lines = vaulttext.strip().splitlines()
    body = "".join(line.strip() for line in lines[1:])
    try:
        decoded = unhexlify(body).decode("ascii")
        salt_hex, hmac_hex, ct_hex = decoded.split("\n")
        return unhexlify(salt_hex), unhexlify(hmac_hex), unhexlify(ct_hex)
    except (ValueError, UnicodeDecodeError) as exc:
        raise VaultError(f"malformed vault payload: {exc}") from exc


def decrypt_vault(vaulttext: str, password: str) -> str:
    if not is_vault(vaulttext):
        raise VaultError("not an ansible-vault value")
    salt, expected_hmac, ciphertext = _split_envelope(vaulttext)
    keymat = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=_KEYLEN, salt=salt, iterations=_ITERATIONS
    ).derive(password.encode("utf-8"))
    cipher_key, hmac_key, iv = keymat[:32], keymat[32:64], keymat[64:80]

    verifier = hmac.HMAC(hmac_key, hashes.SHA256())
    verifier.update(ciphertext)
    try:
        verifier.verify(expected_hmac)
    except InvalidSignature as exc:
        raise VaultError("vault HMAC verification failed (wrong password?)") from exc

    decryptor = Cipher(algorithms.AES(cipher_key), modes.CTR(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return plaintext.decode("utf-8")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./venv/bin/python3 -m pytest tests/test_workload_vault.py -v`
Expected: PASS (3 passed). If `test_decrypt_real_ansible_vector` fails, re-check that the `REAL_VECTOR` header line and hex body were pasted without indentation.

- [ ] **Step 6: Format, type-check, commit**

```bash
cd /Users/prutledg/troshka/src/backend && black app/services/workloads/vault.py tests/test_workload_vault.py && pyright app/services/workloads/vault.py
cd /Users/prutledg/troshka && git add src/backend/app/services/workloads/vault.py src/backend/tests/test_workload_vault.py
git commit -m "feat(workloads): add self-contained ansible-vault decrypt"
```

---

### Task 3: Repo Cache

Bounded, disk-safe git cache. `agnosticv` is a single working copy on `master`, refreshed each run. `agnosticd-v2` / workload repos use a bare treeless mirror + a worktree per `scm_ref` (shared object store), with a default-ref config map and LRU eviction under a disk budget.

**Files:**
- Create: `src/backend/app/services/workloads/repo_cache.py`
- Modify: `src/backend/config/config.yaml` (add `workloads:` section)
- Test: `src/backend/tests/test_workload_repo_cache.py`

**Interfaces:**
- Consumes: `app.core.config.config`; system `git`.
- Produces:
  - `default_ref(repo_key: str) -> str`
  - `ensure_agnosticv(git_url: str) -> str` (returns path to the refreshed working copy)
  - `ensure_repo(repo_key: str, git_url: str, ref: str | None) -> str` (returns worktree path; `ref=None` → `default_ref(repo_key)`)
  - `cache_root() -> str`

- [ ] **Step 1: Add the config section**

In `src/backend/config/config.yaml`, add a top-level `workloads:` block (place it after the `ocp:` section):

```yaml
# Workloads subsystem: repo cache + default git refs.
#   cache_root: base dir for the agnosticv working copy, bare mirrors, worktrees.
#   disk_budget_gb: soft cap; least-recently-used worktrees are evicted above it.
#   agnosticv_branch: agnosticv is a single-branch pull (always master).
#   default_refs: fallback git ref per repo when a catalog item pins none.
workloads:
  cache_root: "/var/lib/troshka/workload-cache"
  disk_budget_gb: 20
  agnosticv_branch: "master"
  default_refs:
    agnosticd-v2: "main"
    agnosticd: "development"
    core_workloads: "main"
    demo_workloads: "main"
    namespaced_workloads: "main"
```

- [ ] **Step 2: Write the failing test**

```python
# src/backend/tests/test_workload_repo_cache.py
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


def _run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _make_remote(tmp_path, name, branches):
    """Create a bare 'remote' repo with the given branches, each holding a
    marker file whose content is the branch name."""
    work = tmp_path / f"{name}-work"
    bare = tmp_path / f"{name}.git"
    _run("git", "init", "-q", str(work))
    _run("git", "-C", str(work), "config", "user.email", "t@t")
    _run("git", "-C", str(work), "config", "user.name", "t")
    _run("git", "-C", str(work), "checkout", "-q", "-b", branches[0])
    for i, br in enumerate(branches):
        if i > 0:
            _run("git", "-C", str(work), "checkout", "-q", "-b", br)
        (work / "marker.txt").write_text(br)
        _run("git", "-C", str(work), "add", "marker.txt")
        _run("git", "-C", str(work), "commit", "-q", "-m", f"on {br}")
    _run("git", "clone", "-q", "--bare", str(work), str(bare))
    return f"file://{bare}"


def _configure_cache(monkeypatch, tmp_path):
    from app.services.workloads import repo_cache

    root = str(tmp_path / "cache")
    monkeypatch.setattr(repo_cache, "cache_root", lambda: root)
    monkeypatch.setattr(
        repo_cache,
        "_cfg",
        lambda: {"disk_budget_gb": 100, "agnosticv_branch": "master",
                 "default_refs": {"demo_workloads": "main"}},
    )
    return repo_cache, root


def test_default_ref_from_config(monkeypatch, tmp_path):
    repo_cache, _ = _configure_cache(monkeypatch, tmp_path)
    assert repo_cache.default_ref("demo_workloads") == "main"


def test_ensure_repo_checks_out_requested_ref(monkeypatch, tmp_path):
    repo_cache, _ = _configure_cache(monkeypatch, tmp_path)
    url = _make_remote(tmp_path, "wl", ["main", "development"])
    wt = repo_cache.ensure_repo("wl", url, "development")
    assert (open(os.path.join(wt, "marker.txt")).read()) == "development"


def test_ensure_repo_shares_object_store_across_refs(monkeypatch, tmp_path):
    repo_cache, root = _configure_cache(monkeypatch, tmp_path)
    url = _make_remote(tmp_path, "wl", ["main", "development"])
    repo_cache.ensure_repo("wl", url, "main")
    repo_cache.ensure_repo("wl", url, "development")
    mirrors = os.listdir(os.path.join(root, "mirrors"))
    assert mirrors == ["wl.git"]  # one shared mirror, two worktrees


def test_ensure_repo_none_uses_default_ref(monkeypatch, tmp_path):
    repo_cache, _ = _configure_cache(monkeypatch, tmp_path)
    url = _make_remote(tmp_path, "demo_workloads", ["main"])
    wt = repo_cache.ensure_repo("demo_workloads", url, None)
    assert (open(os.path.join(wt, "marker.txt")).read()) == "main"


def test_ensure_agnosticv_tracks_master(monkeypatch, tmp_path):
    repo_cache, _ = _configure_cache(monkeypatch, tmp_path)
    url = _make_remote(tmp_path, "agnosticv", ["master"])
    path = repo_cache.ensure_agnosticv(url)
    assert (open(os.path.join(path, "marker.txt")).read()) == "master"


def test_lru_eviction_under_budget(monkeypatch, tmp_path):
    from app.services.workloads import repo_cache

    root = str(tmp_path / "cache")
    monkeypatch.setattr(repo_cache, "cache_root", lambda: root)
    # Tiny budget forces eviction after the first worktree.
    monkeypatch.setattr(
        repo_cache, "_cfg",
        lambda: {"disk_budget_gb": 0, "agnosticv_branch": "master",
                 "default_refs": {}},
    )
    url = _make_remote(tmp_path, "wl", ["main", "development"])
    repo_cache.ensure_repo("wl", url, "main")
    repo_cache.ensure_repo("wl", url, "development")
    worktrees = os.path.join(root, "worktrees", "wl")
    # With a zero budget, only the most-recent worktree survives eviction.
    assert os.listdir(worktrees) == ["development"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./venv/bin/python3 -m pytest tests/test_workload_repo_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.workloads.repo_cache'`.

- [ ] **Step 4: Write the implementation**

```python
# src/backend/app/services/workloads/repo_cache.py
"""Disk-bounded git cache for the workloads subsystem.

- agnosticv: one working copy tracking a single branch (master), refreshed on use.
- agnosticd-v2 / workload repos: one bare treeless mirror per repo plus a git
  worktree per ref (shared object store), with LRU eviction under a disk budget.
"""

import os
import shutil
import subprocess

from app.core.config import config


def _cfg() -> dict:
    return config.get("workloads", {}) or {}


def cache_root() -> str:
    return _cfg().get("cache_root", "/var/lib/troshka/workload-cache")


def default_ref(repo_key: str) -> str:
    return (_cfg().get("default_refs", {}) or {}).get(repo_key, "main")


def _git(*args, cwd=None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _touch(path: str) -> None:
    os.utime(path, None)


def ensure_agnosticv(git_url: str) -> str:
    branch = _cfg().get("agnosticv_branch", "master")
    path = os.path.join(cache_root(), "agnosticv")
    if os.path.isdir(os.path.join(path, ".git")):
        _git("fetch", "-q", "origin", branch, cwd=path)
        _git("reset", "-q", "--hard", f"origin/{branch}", cwd=path)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _git("clone", "-q", "--branch", branch, "--single-branch", git_url, path)
    return path


def _mirror_dir(repo_key: str) -> str:
    return os.path.join(cache_root(), "mirrors", f"{repo_key}.git")


def _worktree_dir(repo_key: str, ref: str) -> str:
    return os.path.join(cache_root(), "worktrees", repo_key, ref)


def _ensure_mirror(repo_key: str, git_url: str) -> str:
    mirror = _mirror_dir(repo_key)
    if os.path.isdir(mirror):
        _git("fetch", "-q", "--filter=blob:none", "origin", cwd=mirror)
    else:
        os.makedirs(os.path.dirname(mirror), exist_ok=True)
        _git("clone", "-q", "--filter=blob:none", "--bare", git_url, mirror)
    return mirror


def ensure_repo(repo_key: str, git_url: str, ref: str | None) -> str:
    ref = ref or default_ref(repo_key)
    mirror = _ensure_mirror(repo_key, git_url)
    worktree = _worktree_dir(repo_key, ref)
    if os.path.isdir(worktree):
        _git("fetch", "-q", "--filter=blob:none", "origin", cwd=mirror)
        _git("reset", "-q", "--hard", ref, cwd=worktree)
    else:
        os.makedirs(os.path.dirname(worktree), exist_ok=True)
        _git("worktree", "add", "-q", "--force", "--detach", worktree, ref,
             cwd=mirror)
    _touch(worktree)
    _evict_if_needed()
    return worktree


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            if not os.path.islink(fp):
                total += os.path.getsize(fp)
    return total


def _all_worktrees() -> list[str]:
    base = os.path.join(cache_root(), "worktrees")
    result = []
    if not os.path.isdir(base):
        return result
    for repo_key in os.listdir(base):
        repo_dir = os.path.join(base, repo_key)
        for ref in os.listdir(repo_dir):
            result.append(os.path.join(repo_dir, ref))
    return result


def _remove_worktree(worktree: str) -> None:
    repo_key = os.path.basename(os.path.dirname(worktree))
    mirror = _mirror_dir(repo_key)
    try:
        _git("worktree", "remove", "-q", "--force", worktree, cwd=mirror)
    except subprocess.CalledProcessError:
        shutil.rmtree(worktree, ignore_errors=True)
        if os.path.isdir(mirror):
            _git("worktree", "prune", cwd=mirror)


def _evict_if_needed() -> None:
    budget = int(_cfg().get("disk_budget_gb", 20)) * (1024 ** 3)
    worktrees = _all_worktrees()
    total = sum(_dir_size(w) for w in worktrees)
    # Oldest first; keep evicting until under budget or only one remains.
    for worktree in sorted(worktrees, key=os.path.getmtime):
        if total <= budget or len(worktrees) <= 1:
            break
        total -= _dir_size(worktree)
        _remove_worktree(worktree)
        worktrees.remove(worktree)
```

Note on the eviction test: with `disk_budget_gb: 0` the loop evicts every worktree except the newest (the `len(worktrees) <= 1` guard stops at one), and the newest (`development`, just touched) is last in mtime order — so it survives. That matches `test_lru_eviction_under_budget`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./venv/bin/python3 -m pytest tests/test_workload_repo_cache.py -v`
Expected: PASS (6 passed).

- [ ] **Step 6: Format, type-check, commit**

```bash
cd /Users/prutledg/troshka/src/backend && black app/services/workloads/repo_cache.py tests/test_workload_repo_cache.py && pyright app/services/workloads/repo_cache.py
cd /Users/prutledg/troshka && git add src/backend/app/services/workloads/repo_cache.py src/backend/config/config.yaml src/backend/tests/test_workload_repo_cache.py
git commit -m "feat(workloads): add disk-bounded git repo cache"
```

---

### Task 4: AgnosticV Merge Engine (drives the `agnosticv` binary)

Wraps `agnosticv --list` / `--merge`, ports ci-inspector's path→id transform to build the catalog index, decrypts `$ANSIBLE_VAULT` strings, and extracts the resolved fields.

**Files:**
- Create: `src/backend/app/services/workloads/agnosticv.py`
- Test: `src/backend/tests/test_workload_agnosticv.py`

**Interfaces:**
- Consumes: `app.services.workloads.vault.decrypt_vault/is_vault`; system `agnosticv` binary.
- Produces:
  - `path_to_catalog_id(component_path: str, default_stage: str = "prod") -> tuple[str, str]` (returns `(catalog_id_without_stage, stage)`)
  - `list_item_paths(repo_root: str) -> list[str]`
  - `build_catalog_index(repo_root: str) -> dict[str, str]` (maps `"<id>.<stage>"` → repo-relative path)
  - `resolve_path(repo_root: str, catalog_id: str) -> str`
  - `merge_path(repo_root: str, rel_path: str) -> dict`
  - `decrypt_vault_strings(data, vault_password: str)`
  - `@dataclass ResolvedItem(extra_vars: dict, ee_image: str | None, scm_ref: str | None, requirements_content: dict | None)`
  - `to_resolved_item(merged: dict) -> ResolvedItem`

- [ ] **Step 1: Write the failing test (pure + mocked subprocess)**

```python
# src/backend/tests/test_workload_agnosticv.py
import json
from unittest.mock import patch

import pytest

from app.services.workloads import agnosticv
from app.services.workloads.vault import VaultError


# --- pure: path -> catalog id (ci-inspector _resolve_binder_crd_name vectors) ---

@pytest.mark.parametrize(
    "path,default_stage,expected",
    [
        ("agd_v2/ocp-cluster-cnv-pools/event", "prod",
         ("agd-v2.ocp-cluster-cnv-pools", "event")),
        ("agd_v2/ocp-cluster-cnv-pools", "prod",
         ("agd-v2.ocp-cluster-cnv-pools", "prod")),
        ("agd_v2/my_component/event", "prod",
         ("agd-v2.my-component", "event")),
        ("AgD_V2/My-Component/Event", "prod",
         ("agd-v2.my-component", "event")),
        ("my-component", "dev", ("my-component", "dev")),
    ],
)
def test_path_to_catalog_id(path, default_stage, expected):
    assert agnosticv.path_to_catalog_id(path, default_stage) == expected


def test_build_catalog_index_maps_id_stage_to_path():
    paths = [
        "agd_v2/aap-multiinstance-workshop/prod.yaml",
        "agd_v2/aap-multiinstance-workshop/dev.yaml",
    ]
    with patch.object(agnosticv, "list_item_paths", return_value=paths):
        index = agnosticv.build_catalog_index("/root")
    assert index["agd-v2.aap-multiinstance-workshop.prod"] == \
        "agd_v2/aap-multiinstance-workshop/prod.yaml"
    assert index["agd-v2.aap-multiinstance-workshop.dev"] == \
        "agd_v2/aap-multiinstance-workshop/dev.yaml"


def test_resolve_path_found_and_missing():
    paths = ["agd_v2/mcp-with-openshift/prod.yaml"]
    with patch.object(agnosticv, "list_item_paths", return_value=paths):
        assert agnosticv.resolve_path("/root", "agd-v2.mcp-with-openshift.prod") == \
            "agd_v2/mcp-with-openshift/prod.yaml"
        with pytest.raises(KeyError):
            agnosticv.resolve_path("/root", "agd-v2.nope.prod")


# --- subprocess wrappers (mocked) ---

def test_list_item_paths_parses_json(monkeypatch):
    class _CP:
        stdout = json.dumps(["a/prod.yaml", "b/dev.yaml"])
    calls = {}

    def fake_run(args, **kw):
        calls["args"] = args
        return _CP()

    monkeypatch.setattr(agnosticv.subprocess, "run", fake_run)
    out = agnosticv.list_item_paths("/root")
    assert out == ["a/prod.yaml", "b/dev.yaml"]
    assert "--list" in calls["args"] and "--git=false" in calls["args"]
    assert "/root" in calls["args"]


def test_merge_path_parses_json(monkeypatch):
    class _CP:
        stdout = json.dumps({"foo": "bar", "__meta__": {}})

    def fake_run(args, **kw):
        assert "--merge" in args
        return _CP()

    monkeypatch.setattr(agnosticv.subprocess, "run", fake_run)
    assert agnosticv.merge_path("/root", "x/prod.yaml") == {"foo": "bar", "__meta__": {}}


# --- vault decrypt over merged structure ---

def test_decrypt_vault_strings_walks_structure(monkeypatch):
    monkeypatch.setattr(
        agnosticv, "decrypt_vault",
        lambda text, pw: "PLAIN" if text.startswith("$ANSIBLE_VAULT") else text,
    )
    monkeypatch.setattr(
        agnosticv, "is_vault", lambda t: isinstance(t, str) and t.startswith("$ANSIBLE_VAULT")
    )
    data = {"a": "$ANSIBLE_VAULT;1.1;AES256\nxx", "b": ["plain", "$ANSIBLE_VAULT;1.1;AES256\nyy"], "c": 3}
    out = agnosticv.decrypt_vault_strings(data, "pw")
    assert out == {"a": "PLAIN", "b": ["plain", "PLAIN"], "c": 3}


# --- resolved item extraction ---

def test_to_resolved_item_extracts_deployer_fields():
    merged = {
        "requirements_content": {"collections": [{"name": "agnosticd.core_workloads"}]},
        "__meta__": {
            "deployer": {
                "scm_ref": "v1.2.3",
                "execution_environment": {"image": "quay.io/agnosticd/ee-multicloud:x"},
            }
        },
    }
    item = agnosticv.to_resolved_item(merged)
    assert item.ee_image == "quay.io/agnosticd/ee-multicloud:x"
    assert item.scm_ref == "v1.2.3"
    assert item.requirements_content == {"collections": [{"name": "agnosticd.core_workloads"}]}
    assert item.extra_vars is merged


def test_to_resolved_item_missing_deployer_is_none():
    item = agnosticv.to_resolved_item({"foo": "bar"})
    assert item.ee_image is None and item.scm_ref is None and item.requirements_content is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python3 -m pytest tests/test_workload_agnosticv.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.workloads.agnosticv'`.

- [ ] **Step 3: Write the implementation**

```python
# src/backend/app/services/workloads/agnosticv.py
"""Drive the canonical `agnosticv` binary for list + merge, and resolve the
merged output into the fields the workloads subsystem needs.

The AgnosticV overlay/#include/.agnosticv.yaml merge is NOT reimplemented — the
`agnosticv` binary is the single source of truth. Only vault decrypt and the
path->catalog-id transform live here.
"""

import json
import subprocess
from dataclasses import dataclass

from app.services.workloads.vault import decrypt_vault, is_vault

_AGNOSTICV = "agnosticv"


def path_to_catalog_id(component_path: str, default_stage: str = "prod") -> tuple[str, str]:
    """Port of ci-inspector `_resolve_binder_crd_name`.

    ``agd_v2/ocp-cluster-cnv-pools/event`` -> (``agd-v2.ocp-cluster-cnv-pools``, ``event``).
    Strips a trailing ``.yaml``/``.yml`` stage extension first.
    """
    cleaned = component_path
    for ext in (".yaml", ".yml"):
        if cleaned.endswith(ext):
            cleaned = cleaned[: -len(ext)]
            break
    parts = cleaned.replace("_", "-").lower().split("/")
    if len(parts) >= 2:
        stage = parts[-1] if len(parts) >= 3 else default_stage
        ci_name = ".".join(parts[:-1]) if len(parts) >= 3 else ".".join(parts)
        return ci_name, stage
    return cleaned.lower(), default_stage


def list_item_paths(repo_root: str) -> list[str]:
    proc = subprocess.run(
        [_AGNOSTICV, "--list", "--dir", repo_root, "--git=false", "--output=json"],
        check=True, capture_output=True, text=True,
    )
    return json.loads(proc.stdout)


def build_catalog_index(repo_root: str) -> dict[str, str]:
    index: dict[str, str] = {}
    for path in list_item_paths(repo_root):
        ci_name, stage = path_to_catalog_id(path)
        index[f"{ci_name}.{stage}"] = path
    return index


def resolve_path(repo_root: str, catalog_id: str) -> str:
    index = build_catalog_index(repo_root)
    if catalog_id not in index:
        raise KeyError(f"catalog item not found: {catalog_id}")
    return index[catalog_id]


def merge_path(repo_root: str, rel_path: str) -> dict:
    proc = subprocess.run(
        [_AGNOSTICV, "--git=false", "--merge", rel_path, "--output=json"],
        cwd=repo_root, check=True, capture_output=True, text=True,
    )
    return json.loads(proc.stdout)


def decrypt_vault_strings(data, vault_password: str):
    if isinstance(data, str):
        return decrypt_vault(data, vault_password) if is_vault(data) else data
    if isinstance(data, dict):
        return {k: decrypt_vault_strings(v, vault_password) for k, v in data.items()}
    if isinstance(data, list):
        return [decrypt_vault_strings(v, vault_password) for v in data]
    return data


@dataclass
class ResolvedItem:
    extra_vars: dict
    ee_image: str | None
    scm_ref: str | None
    requirements_content: dict | None


def to_resolved_item(merged: dict) -> ResolvedItem:
    deployer = (merged.get("__meta__") or {}).get("deployer") or {}
    ee_image = (deployer.get("execution_environment") or {}).get("image")
    return ResolvedItem(
        extra_vars=merged,
        ee_image=ee_image,
        scm_ref=deployer.get("scm_ref"),
        requirements_content=merged.get("requirements_content"),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python3 -m pytest tests/test_workload_agnosticv.py -v`
Expected: PASS (all parametrized + others). If `test_decrypt_vault_strings_walks_structure` fails, confirm the module references `decrypt_vault`/`is_vault` at module scope (so `monkeypatch.setattr` can patch them).

- [ ] **Step 5: Add the optional real-binary integration test**

Append to `tests/test_workload_agnosticv.py`:

```python
import os
import shutil
import subprocess as _sp


@pytest.mark.skipif(shutil.which("agnosticv") is None, reason="agnosticv binary absent")
def test_real_agnosticv_merge_roundtrip(tmp_path):
    # Minimal agnosticv tree: root common.yaml + one overlay item with prod.yaml.
    root = tmp_path
    (root / "common.yaml").write_text("root_var: from_root\n")
    (root / ".agnosticv.yaml").write_text("---\nrelated_files: []\n")
    item = root / "agd_v2" / "sample-item"
    item.mkdir(parents=True)
    (root / "agd_v2" / "account.yaml").write_text("account_var: acct\n")
    (item / "common.yaml").write_text("item_var: base\n")
    (item / "prod.yaml").write_text("item_var: prod_override\n")

    paths = agnosticv.list_item_paths(str(root))
    assert "agd_v2/sample-item/prod.yaml" in paths
    rel = agnosticv.resolve_path(str(root), "agd-v2.sample-item.prod")
    merged = agnosticv.merge_path(str(root), rel)
    assert merged["root_var"] == "from_root"
    assert merged["item_var"] == "prod_override"  # stage overrides base
```

Run: `./venv/bin/python3 -m pytest tests/test_workload_agnosticv.py -v`
Expected: PASS (the integration test runs in dev where `agnosticv` is on PATH; skips in CI if absent).

- [ ] **Step 6: Format, type-check, commit**

```bash
cd /Users/prutledg/troshka/src/backend && black app/services/workloads/agnosticv.py tests/test_workload_agnosticv.py && pyright app/services/workloads/agnosticv.py
cd /Users/prutledg/troshka && git add src/backend/app/services/workloads/agnosticv.py src/backend/tests/test_workload_agnosticv.py
git commit -m "feat(workloads): drive agnosticv binary for list/merge + vault decrypt"
```

---

### Task 5: Resolver (integration entry point)

Ties the pieces together: read git creds + vault key from the Secret Store, refresh the agnosticv checkout via the Repo Cache, resolve the path, merge, decrypt, and return a `ResolvedItem`.

**Files:**
- Create: `src/backend/app/services/workloads/resolver.py`
- Test: `src/backend/tests/test_workload_resolver.py`

**Interfaces:**
- Consumes: `secret_store.get_secret/get_json_secret`; `repo_cache.ensure_agnosticv`; `agnosticv.resolve_path/merge_path/decrypt_vault_strings/to_resolved_item`.
- Produces:
  - `resolve_catalog_item(db: Session, catalog_id: str) -> agnosticv.ResolvedItem`
  - `class ResolverError(Exception)`

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_workload_resolver.py
import pytest

from app.services.workloads import agnosticv, resolver
from app.services.workloads import repo_cache, secret_store
from tests.conftest import TestSession


def _seed_secrets(db):
    secret_store.set_secret(db, "vault_key", "testpass")
    secret_store.set_json_secret(
        db, "agnosticv_git", {"url": "https://example.com/agnosticv.git"}
    )


def test_resolve_catalog_item_happy_path(monkeypatch):
    db = TestSession()
    try:
        _seed_secrets(db)
        monkeypatch.setattr(repo_cache, "ensure_agnosticv", lambda url: "/fake/root")
        monkeypatch.setattr(
            agnosticv, "resolve_path", lambda root, cid: "agd_v2/x/prod.yaml"
        )
        monkeypatch.setattr(
            agnosticv, "merge_path",
            lambda root, rel: {
                "k": "$ANSIBLE_VAULT-marker",
                "__meta__": {"deployer": {
                    "scm_ref": "main",
                    "execution_environment": {"image": "ee:1"},
                }},
            },
        )
        monkeypatch.setattr(
            agnosticv, "decrypt_vault_strings",
            lambda data, pw: {**data, "k": "decrypted"} if pw == "testpass" else data,
        )
        item = resolver.resolve_catalog_item(db, "agd-v2.x.prod")
        assert item.ee_image == "ee:1"
        assert item.scm_ref == "main"
        assert item.extra_vars["k"] == "decrypted"
    finally:
        db.close()


def test_resolve_requires_vault_key(monkeypatch):
    db = TestSession()
    try:
        secret_store.set_json_secret(db, "agnosticv_git", {"url": "u"})
        with pytest.raises(resolver.ResolverError):
            resolver.resolve_catalog_item(db, "agd-v2.x.prod")
    finally:
        db.close()


def test_resolve_requires_git_config(monkeypatch):
    db = TestSession()
    try:
        secret_store.set_secret(db, "vault_key", "testpass")
        secret_store.delete_secret(db, "agnosticv_git")
        with pytest.raises(resolver.ResolverError):
            resolver.resolve_catalog_item(db, "agd-v2.x.prod")
    finally:
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python3 -m pytest tests/test_workload_resolver.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.workloads.resolver'`.

- [ ] **Step 3: Write the implementation**

```python
# src/backend/app/services/workloads/resolver.py
"""Resolve a catalog item id into a ResolvedItem, credential-safe.

Git credentials and the vault key are read from the Secret Store and never leave
the backend. This is the public entry point Plan 2 (execution) builds on.
"""

from sqlalchemy.orm import Session

from app.services.workloads import agnosticv, repo_cache, secret_store


class ResolverError(Exception):
    pass


def resolve_catalog_item(db: Session, catalog_id: str) -> agnosticv.ResolvedItem:
    vault_password = secret_store.get_secret(db, "vault_key")
    if not vault_password:
        raise ResolverError("vault_key secret is not configured")

    git_conf = secret_store.get_json_secret(db, "agnosticv_git")
    if not git_conf or not git_conf.get("url"):
        raise ResolverError("agnosticv_git secret is not configured")

    repo_root = repo_cache.ensure_agnosticv(git_conf["url"])
    rel_path = agnosticv.resolve_path(repo_root, catalog_id)
    merged = agnosticv.merge_path(repo_root, rel_path)
    merged = agnosticv.decrypt_vault_strings(merged, vault_password)
    return agnosticv.to_resolved_item(merged)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python3 -m pytest tests/test_workload_resolver.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the full workloads test module set**

Run: `./venv/bin/python3 -m pytest tests/test_workload_*.py -v`
Expected: all PASS (binary integration test may SKIP in CI).

- [ ] **Step 6: Format, type-check, commit**

```bash
cd /Users/prutledg/troshka/src/backend && black app/services/workloads/resolver.py tests/test_workload_resolver.py && pyright app/services/workloads/resolver.py
cd /Users/prutledg/troshka && git add src/backend/app/services/workloads/resolver.py src/backend/tests/test_workload_resolver.py
git commit -m "feat(workloads): add catalog item resolver entry point"
```

---

### Task 6: Ship the `agnosticv` binary in the backend image

The Merge Engine shells out to `agnosticv` at runtime, so the backend/worker container image must include it. This task is infra-only (no Python); full verification happens at image build in CI.

**Files:**
- Modify: `src/backend/Containerfile.backend` (or the repo's actual backend Containerfile — confirm the path with `git ls-files | grep -i containerfile`)

**Interfaces:**
- Consumes: nothing at code level.
- Produces: an `agnosticv` binary on `PATH` inside the backend/worker image.

- [ ] **Step 1: Locate the backend Containerfile**

Run: `cd /Users/prutledg/troshka && git ls-files | grep -iE 'containerfile|dockerfile' | grep -i back`
Note the exact path (referred to below as `<CONTAINERFILE>`).

- [ ] **Step 2: Add the binary install layer**

Add to `<CONTAINERFILE>` (before the app is copied), pinning a released version — confirm the latest tag at https://github.com/rhpds/agnosticv/releases and the correct asset name for the image's arch:

```dockerfile
# agnosticv CLI — used by the workloads subsystem to list/merge catalog items.
ARG AGNOSTICV_VERSION=0.20.0
RUN curl -fsSL -o /usr/local/bin/agnosticv \
      "https://github.com/rhpds/agnosticv/releases/download/v${AGNOSTICV_VERSION}/agnosticv-linux-amd64" \
    && chmod +x /usr/local/bin/agnosticv \
    && agnosticv --help >/dev/null 2>&1 || true
```

(Adjust the asset filename/URL to match the actual release assets; if only a source tarball is published, build with a golang builder stage instead. Record the resolved URL in a comment.)

- [ ] **Step 3: Verify the Containerfile edit**

Run: `cd /Users/prutledg/troshka && grep -n agnosticv <CONTAINERFILE>`
Expected: the install layer is present.

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add <CONTAINERFILE>
git commit -m "build(workloads): install agnosticv binary in backend image"
```

- [ ] **Step 5: Note for the operator**

Image build/push is handled by CI on push to `main` (do not build locally). After this merges, confirm the built image contains `agnosticv` by checking the CI build logs or, once deployed, `oc exec <backend-pod> -- agnosticv --help`.

---

## Self-Review

**Spec coverage (Plan 1 scope only):**
- Central Secret Store (spec §4.2) → Task 1. ✔
- Repo Cache with per-repo default-ref map + disk budget + LRU (spec §4.2, D10) → Task 3. ✔
- AgnosticV Merge via the `agnosticv` binary + path→id port + vault decrypt (spec D2, §4.2) → Tasks 2 + 4. ✔
- Resolved output `{extra_vars, ee_image, scm_ref, requirements_content}` (spec §4.2) → Task 4 `to_resolved_item` + Task 5. ✔
- Credential isolation: git creds + vault key stay backend-side (spec §5, D1) → enforced by Task 5 reading from the Secret Store; nothing writes them to a pod in this plan. ✔
- `agnosticv` binary dependency (spec D2) → Task 6. ✔
- Out of Plan 1 (correctly deferred): runner pod, inventory emitter, cluster access, `WorkloadRun` model, RQ job/progress, API, UI, cloud-cred injection → Plans 2 and 3.

**Placeholder scan:** The only intentional fill-in is `<PASTE_HEX_LINES_HERE>` in Task 2 Step 2 (produced by Task 2 Step 1) and `<CONTAINERFILE>` in Task 6 (resolved in Task 6 Step 1). Both are generated by an explicit preceding step, not left vague.

**Type consistency:** `ResolvedItem` fields (`extra_vars`, `ee_image`, `scm_ref`, `requirements_content`) are used identically in Tasks 4 and 5. `path_to_catalog_id` returns `(id_without_stage, stage)`; `build_catalog_index` composes `f"{ci_name}.{stage}"` keys consumed by `resolve_path`. `ensure_agnosticv(git_url)` / `ensure_repo(repo_key, git_url, ref)` signatures match their callers. `secret_store` function names are consistent across Tasks 1 and 5.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-11-troshka-workloads-plan1-foundational-backend.md`.
