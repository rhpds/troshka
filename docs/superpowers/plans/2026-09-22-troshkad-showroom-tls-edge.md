# troshkad Showroom TLS Edge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Terminate TLS for the troshkad (cloud) showroom via a per-project host/gateway edge proxy, using a Let's Encrypt cert (Route53 DNS-01) with a self-signed fallback.

**Architecture:** A `socat` TLS terminator runs inside each project's transit netns, bound to the transit gateway IP `172.30.<vni>.1:443`, proxying decrypted traffic to the showroom nginx at `172.30.<vni>.3:80`. troshkad obtains/generates the cert; the backend orchestrates the DNS record, cert request, terminator start, and URL surfacing. KubeVirt/OCP-Virt are untouched (they edge-terminate at the OCP router).

**Tech Stack:** Python stdlib troshkad daemon (command handlers), `socat`, `certbot`+`certbot-dns-route53`, `openssl`, FastAPI/SQLAlchemy backend, `dns_service` (route53/nsupdate).

**Spec:** `docs/superpowers/specs/2026-09-22-troshkad-showroom-tls-edge-design.md`

## Global Constraints

- troshkad is a single stdlib-only file pushed to hosts; new host-op packages go in `troshkad._REQUIRED_HOST_PACKAGES` (single source of truth) — never a second list.
- All external-command invocations use **argv lists, never `bash -c`** with interpolated user-influenceable values (fqdn/CN/upstream/EIP). Validate FQDN against `^[a-zA-Z0-9.-]{1,253}$` and IPs via `ipaddress.ip_address()` before use.
- Cert private keys and the combined PEM are mode `0600`, owned by root.
- troshkad command handlers register as `COMMAND_HANDLERS["<path>"] = _handle_x` with signature `def _handle_x(job, params) -> dict`; the backend calls them via `start_job(host, "/<path>", params)` + `wait_for_job(host, jid)`.
- Route providers (`_ROUTE_PROVIDERS` = ocpvirt/kubevirt) must NOT get a terminator or an EIP `:443→:80` DNAT — showroom TLS there is the OCP Route's job.
- TLS setup is **non-fatal** to the cluster deploy: a terminator/cert failure logs a warning, never aborts the deploy.
- Per-project gateway dir: `/var/lib/troshka/gateway/<proj8>/tls/`.
- Tests: backend uses SQLite + mocked troshkad/dns (no real I/O); troshkad tests mock `subprocess` (no real I/O), per repo conventions.

---

### Task 1: Add `socat` to the host package set

**Files:**
- Modify: `src/troshkad/troshkad.py` (the `_REQUIRED_HOST_PACKAGES` list)
- Test: `src/troshkad/tests/test_troshkad_helpers.py`

**Interfaces:**
- Consumes: existing `_REQUIRED_HOST_PACKAGES`, `_ensure_host_packages`.
- Produces: `"socat" in _REQUIRED_HOST_PACKAGES`.

- [ ] **Step 1: Write the failing test**

```python
def test_required_packages_includes_socat():
    assert "socat" in troshkad._REQUIRED_HOST_PACKAGES
```
(Add to the existing `TestEnsureHostPackages` class in `test_troshkad_helpers.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/troshkad && python3 -m pytest tests/test_troshkad_helpers.py::TestEnsureHostPackages::test_required_packages_includes_socat -q`
Expected: FAIL (`socat` not in list)

- [ ] **Step 3: Add `socat` to the list**

In `troshkad.py`, append `"socat",` to `_REQUIRED_HOST_PACKAGES`.

- [ ] **Step 4: Run test to verify it passes**

Run: same as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py src/troshkad/tests/test_troshkad_helpers.py
git commit -m "feat(troshkad): add socat to host package set for showroom TLS edge"
```

---

### Task 2: troshkad self-signed cert generation

**Files:**
- Modify: `src/troshkad/troshkad.py` (new `_gen_self_signed_cert`)
- Test: `src/troshkad/tests/test_troshkad_helpers.py`

**Interfaces:**
- Produces: `_gen_self_signed_cert(out_dir: str, cn: str, eip: str) -> tuple[str, str]` returning `(fullchain_path, key_path)`. Writes `fullchain.pem` + `privkey.pem` in `out_dir` (created `0700`), key `0600`. Runs `openssl` as an argv list.

- [ ] **Step 1: Write the failing test**

```python
class TestSelfSignedCert(unittest.TestCase):
    @patch("troshkad.os.chmod")
    @patch("troshkad.os.makedirs")
    @patch("troshkad.subprocess.run")
    def test_builds_openssl_argv_and_returns_paths(self, mock_run, _mk, _ch):
        mock_run.return_value = MagicMock(returncode=0)
        full, key = troshkad._gen_self_signed_cert("/gw/tls", "1.2.3.4", "1.2.3.4")
        assert full.endswith("/fullchain.pem")
        assert key.endswith("/privkey.pem")
        argv = mock_run.call_args[0][0]
        assert argv[0] == "openssl"
        assert "req" in argv and "-x509" in argv
        # user-influenceable values are argv items, never a shell string
        assert "subjectAltName=IP:1.2.3.4" in " ".join(argv)
        assert "bash" not in argv
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/troshkad && python3 -m pytest tests/test_troshkad_helpers.py::TestSelfSignedCert -q`
Expected: FAIL (`_gen_self_signed_cert` undefined)

- [ ] **Step 3: Implement**

```python
def _gen_self_signed_cert(out_dir, cn, eip):
    """Generate a self-signed cert (argv, no shell). Returns (fullchain, key)."""
    os.makedirs(out_dir, exist_ok=True)
    os.chmod(out_dir, 0o700)
    full = os.path.join(out_dir, "fullchain.pem")
    key = os.path.join(out_dir, "privkey.pem")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", key, "-out", full, "-days", "825",
            "-subj", f"/CN={cn}",
            "-addext", f"subjectAltName=IP:{eip}",
        ],
        check=True, timeout=60,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    os.chmod(key, 0o600)
    return full, key
```

- [ ] **Step 4: Run test to verify it passes** — same as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py src/troshkad/tests/test_troshkad_helpers.py
git commit -m "feat(troshkad): self-signed cert generation for showroom TLS"
```

---

### Task 3: troshkad Let's Encrypt cert (with self-signed fallback)

**Files:**
- Modify: `src/troshkad/troshkad.py` (new `_obtain_letsencrypt_cert`)
- Test: `src/troshkad/tests/test_troshkad_helpers.py`

**Interfaces:**
- Consumes: `_gen_self_signed_cert` (Task 2), `_AWS_CLI`/venv paths.
- Produces: `_obtain_letsencrypt_cert(fqdn: str, route53: dict) -> tuple[str, str, str]` returning `(fullchain, key, mode)` where mode is `"letsencrypt"` or `"self-signed"`. On certbot success returns `/etc/letsencrypt/live/<fqdn>/{fullchain.pem,privkey.pem}`; on failure returns `(None, None, "self-signed")` so the caller uses `_gen_self_signed_cert`. certbot runs as an argv list with AWS creds in the env.

- [ ] **Step 1: Write the failing test**

```python
class TestLetsEncryptCert(unittest.TestCase):
    @patch("troshkad.subprocess.run")
    def test_certbot_success_returns_live_paths(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        full, key, mode = troshkad._obtain_letsencrypt_cert(
            "showroom.g.example.com", {"access_key_id": "AK", "secret_access_key": "SK"}
        )
        assert mode == "letsencrypt"
        assert full == "/etc/letsencrypt/live/showroom.g.example.com/fullchain.pem"
        argv = mock_run.call_args[0][0]
        assert argv[-1] != "bash"
        assert "certonly" in argv and "--dns-route53" in argv
        assert "showroom.g.example.com" in argv

    @patch("troshkad.subprocess.run")
    def test_certbot_failure_signals_self_signed(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        full, key, mode = troshkad._obtain_letsencrypt_cert("x.example.com", {})
        assert mode == "self-signed"
        assert full is None
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (`_obtain_letsencrypt_cert` undefined).

- [ ] **Step 3: Implement**

```python
_CERTBOT = "/opt/troshka/venv/bin/certbot"

def _obtain_letsencrypt_cert(fqdn, route53):
    """Request an LE cert via Route53 DNS-01 (argv, no shell). Returns
    (fullchain, key, mode); mode 'self-signed' with None paths on failure so the
    caller falls back."""
    env = os.environ.copy()
    if route53.get("access_key_id"):
        env["AWS_ACCESS_KEY_ID"] = route53["access_key_id"]
        env["AWS_SECRET_ACCESS_KEY"] = route53.get("secret_access_key", "")
        env["AWS_DEFAULT_REGION"] = route53.get("region", "us-east-1")
    certbot = _CERTBOT if os.path.exists(_CERTBOT) else "certbot"
    try:
        proc = subprocess.run(
            [
                certbot, "certonly", "--dns-route53", "-d", fqdn,
                "--non-interactive", "--agree-tos", "-m", "noreply@redhat.com",
                "--preferred-challenges", "dns-01",
            ],
            env=env, timeout=300,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None, None, "self-signed"
    if proc.returncode != 0:
        return None, None, "self-signed"
    live = f"/etc/letsencrypt/live/{fqdn}"
    return f"{live}/fullchain.pem", f"{live}/privkey.pem", "letsencrypt"
```

- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py src/troshkad/tests/test_troshkad_helpers.py
git commit -m "feat(troshkad): Let's Encrypt cert via Route53 DNS-01 with self-signed fallback"
```

---

### Task 4: troshkad `gateway/tls-cert` command handler

**Files:**
- Modify: `src/troshkad/troshkad.py` (new `_handle_gateway_tls_cert` + `COMMAND_HANDLERS` registration)
- Test: `src/troshkad/tests/test_troshkad.py` (or `test_troshkad_helpers.py`)

**Interfaces:**
- Consumes: `_obtain_letsencrypt_cert` (Task 3), `_gen_self_signed_cert` (Task 2).
- Produces: `COMMAND_HANDLERS["gateway/tls-cert"]`. params `{project_id, fqdn, eip, route53}`. Returns `{"cert_path","key_path","mode"}`. Validates `fqdn` (regex) and `eip` (`ipaddress.ip_address`); an empty/invalid fqdn goes straight to self-signed. Writes into `/var/lib/troshka/gateway/<proj8>/tls/`.

- [ ] **Step 1: Write the failing test**

```python
class TestGatewayTlsCert(unittest.TestCase):
    @patch("troshkad._gen_self_signed_cert", return_value=("/gw/full.pem", "/gw/key.pem"))
    @patch("troshkad._obtain_letsencrypt_cert")
    def test_valid_fqdn_uses_letsencrypt(self, mock_le, _ss):
        mock_le.return_value = ("/etc/letsencrypt/live/x/fullchain.pem",
                                "/etc/letsencrypt/live/x/privkey.pem", "letsencrypt")
        out = troshkad._handle_gateway_tls_cert(
            {}, {"project_id": "abcdef12-0000", "fqdn": "showroom.g.example.com",
                  "eip": "1.2.3.4", "route53": {}})
        assert out["mode"] == "letsencrypt"
        mock_le.assert_called_once()

    @patch("troshkad._gen_self_signed_cert", return_value=("/gw/full.pem", "/gw/key.pem"))
    @patch("troshkad._obtain_letsencrypt_cert")
    def test_empty_fqdn_self_signs(self, mock_le, mock_ss):
        out = troshkad._handle_gateway_tls_cert(
            {}, {"project_id": "abcdef12-0000", "fqdn": "", "eip": "1.2.3.4"})
        assert out["mode"] == "self-signed"
        mock_le.assert_not_called()
        mock_ss.assert_called_once()

    @patch("troshkad._gen_self_signed_cert", return_value=("/gw/full.pem", "/gw/key.pem"))
    @patch("troshkad._obtain_letsencrypt_cert")
    def test_injection_fqdn_rejected_to_self_signed(self, mock_le, mock_ss):
        out = troshkad._handle_gateway_tls_cert(
            {}, {"project_id": "abcdef12-0000", "fqdn": "x;rm -rf /", "eip": "1.2.3.4"})
        assert out["mode"] == "self-signed"
        mock_le.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (`_handle_gateway_tls_cert` undefined).

- [ ] **Step 3: Implement**

```python
import re as _re

_FQDN_RE = _re.compile(r"^[a-zA-Z0-9.-]{1,253}$")

def _gateway_tls_dir(project_id):
    return f"/var/lib/troshka/gateway/{project_id[:8]}/tls"

def _handle_gateway_tls_cert(job, params):
    import ipaddress
    project_id = _validate_project_id(params["project_id"])
    fqdn = (params.get("fqdn") or "").strip()
    eip = (params.get("eip") or "").strip()
    out_dir = _gateway_tls_dir(project_id)
    use_le = bool(fqdn) and bool(_FQDN_RE.match(fqdn))
    if use_le:
        full, key, mode = _obtain_letsencrypt_cert(fqdn, params.get("route53") or {})
        if mode == "letsencrypt":
            return {"cert_path": full, "key_path": key, "mode": mode}
    # self-signed fallback (empty/invalid fqdn, or certbot failed)
    cn = fqdn if use_le else eip
    try:
        ipaddress.ip_address(eip)
    except ValueError:
        eip = "127.0.0.1"
    full, key = _gen_self_signed_cert(out_dir, cn or eip, eip)
    return {"cert_path": full, "key_path": key, "mode": "self-signed"}

COMMAND_HANDLERS["gateway/tls-cert"] = _handle_gateway_tls_cert
```
(Use the existing `_validate_project_id` helper if present; otherwise validate with a UUID/`[:8]` sanity check. Confirm the helper name in troshkad before implementing.)

- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py src/troshkad/tests/
git commit -m "feat(troshkad): gateway/tls-cert handler (LE or self-signed, validated)"
```

---

### Task 5: troshkad TLS terminator start/stop + descriptor

**Files:**
- Modify: `src/troshkad/troshkad.py` (`_start_tls_proxy`, `_stop_tls_proxy`)
- Test: `src/troshkad/tests/test_troshkad.py`

**Interfaces:**
- Produces:
  - `_start_tls_proxy(project_id, netns, listen, upstream, cert_path, key_path) -> int` — writes a `0600` `combined.pem` (fullchain+key), launches `ip netns exec <netns> socat OPENSSL-LISTEN:443,bind=<listen_ip>,...,cert=<combined> TCP:<upstream>` via `Popen`, writes `<tls_dir>/proxy.pid` and `<tls_dir>/proxy.json` (descriptor: `{netns,listen,upstream,cert_path,key_path}`), returns pid.
  - `_stop_tls_proxy(project_id)` — kills the pid from `proxy.pid`, removes pidfile + descriptor.
- `listen` is `"<ip>:443"`; the bind IP is parsed from it and validated with `ipaddress.ip_address`.

- [ ] **Step 1: Write the failing test**

```python
class TestTlsProxy(unittest.TestCase):
    @patch("troshkad.subprocess.Popen")
    @patch("troshkad.os.chmod")
    @patch("troshkad.os.makedirs")
    @patch("builtins.open", new_callable=mock_open,
           read_data="CERT")  # fullchain/key reads for combined.pem
    def test_start_builds_netns_socat_argv(self, _open, _mk, _ch, mock_popen):
        proc = MagicMock(); proc.pid = 4321; mock_popen.return_value = proc
        pid = troshkad._start_tls_proxy(
            "abcdef12-0000", "troshka-abcdef12", "172.30.5.1:443",
            "172.30.5.3:80", "/gw/full.pem", "/gw/key.pem")
        assert pid == 4321
        argv = mock_popen.call_args[0][0]
        assert argv[:4] == ["ip", "netns", "exec", "troshka-abcdef12"]
        assert argv[4] == "socat"
        joined = " ".join(argv)
        assert "OPENSSL-LISTEN:443" in joined and "bind=172.30.5.1" in joined
        assert "TCP:172.30.5.3:80" in joined
        assert "bash" not in argv
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (`_start_tls_proxy` undefined).

- [ ] **Step 3: Implement**

```python
def _write_combined_pem(tls_dir, cert_path, key_path):
    os.makedirs(tls_dir, exist_ok=True)
    os.chmod(tls_dir, 0o700)
    combined = os.path.join(tls_dir, "combined.pem")
    with open(cert_path) as c, open(key_path) as k:
        data = c.read() + "\n" + k.read()
    with open(combined, "w") as f:
        f.write(data)
    os.chmod(combined, 0o600)
    return combined

def _start_tls_proxy(project_id, netns, listen, upstream, cert_path, key_path):
    import ipaddress, json as _json
    bind_ip, _, port = listen.partition(":")
    ipaddress.ip_address(bind_ip)
    port = port or "443"
    tls_dir = _gateway_tls_dir(project_id)
    combined = _write_combined_pem(tls_dir, cert_path, key_path)
    argv = [
        "ip", "netns", "exec", netns, "socat",
        f"OPENSSL-LISTEN:{port},bind={bind_ip},reuseaddr,fork,cert={combined},verify=0",
        f"TCP:{upstream}",
    ]
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(os.path.join(tls_dir, "proxy.pid"), "w") as f:
        f.write(str(proc.pid))
    with open(os.path.join(tls_dir, "proxy.json"), "w") as f:
        _json.dump({"netns": netns, "listen": listen, "upstream": upstream,
                    "cert_path": cert_path, "key_path": key_path}, f)
    return proc.pid

def _stop_tls_proxy(project_id):
    tls_dir = _gateway_tls_dir(project_id)
    pidfile = os.path.join(tls_dir, "proxy.pid")
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass
    for name in ("proxy.pid", "proxy.json"):
        try:
            os.remove(os.path.join(tls_dir, name))
        except OSError:
            pass
```

- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py src/troshkad/tests/
git commit -m "feat(troshkad): TLS terminator start/stop (socat in netns) with descriptor"
```

---

### Task 6: troshkad `gateway/tls-proxy` + `gateway/tls-proxy-stop` handlers

**Files:**
- Modify: `src/troshkad/troshkad.py` (`_handle_gateway_tls_proxy`, `_handle_gateway_tls_proxy_stop`, registrations)
- Test: `src/troshkad/tests/test_troshkad.py`

**Interfaces:**
- Consumes: `_start_tls_proxy`/`_stop_tls_proxy` (Task 5).
- Produces: `COMMAND_HANDLERS["gateway/tls-proxy"]` (params `{project_id, netns, listen, upstream, cert_path, key_path}` → `{"pid": int}`) and `COMMAND_HANDLERS["gateway/tls-proxy-stop"]` (params `{project_id}` → `{"stopped": true}`).

- [ ] **Step 1: Write the failing test**

```python
class TestGatewayTlsProxyHandlers(unittest.TestCase):
    @patch("troshkad._start_tls_proxy", return_value=999)
    def test_proxy_start_handler(self, mock_start):
        out = troshkad._handle_gateway_tls_proxy({}, {
            "project_id": "abcdef12-0000", "netns": "troshka-abcdef12",
            "listen": "172.30.5.1:443", "upstream": "172.30.5.3:80",
            "cert_path": "/gw/full.pem", "key_path": "/gw/key.pem"})
        assert out["pid"] == 999
        mock_start.assert_called_once()

    @patch("troshkad._stop_tls_proxy")
    def test_proxy_stop_handler(self, mock_stop):
        out = troshkad._handle_gateway_tls_proxy_stop({}, {"project_id": "abcdef12-0000"})
        assert out["stopped"] is True
        mock_stop.assert_called_once_with("abcdef12-0000")
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (handlers undefined).

- [ ] **Step 3: Implement**

```python
def _handle_gateway_tls_proxy(job, params):
    pid = _start_tls_proxy(
        _validate_project_id(params["project_id"]),
        params["netns"], params["listen"], params["upstream"],
        params["cert_path"], params["key_path"],
    )
    return {"pid": pid}

def _handle_gateway_tls_proxy_stop(job, params):
    _stop_tls_proxy(_validate_project_id(params["project_id"]))
    return {"stopped": True}

COMMAND_HANDLERS["gateway/tls-proxy"] = _handle_gateway_tls_proxy
COMMAND_HANDLERS["gateway/tls-proxy-stop"] = _handle_gateway_tls_proxy_stop
```

- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py src/troshkad/tests/
git commit -m "feat(troshkad): gateway/tls-proxy start+stop command handlers"
```

---

### Task 7: troshkad restore TLS proxies on startup

**Files:**
- Modify: `src/troshkad/troshkad.py` (`_restore_tls_proxies` + call in `main()`)
- Test: `src/troshkad/tests/test_troshkad_helpers.py`

**Interfaces:**
- Consumes: `_start_tls_proxy` (Task 5).
- Produces: `_restore_tls_proxies()` — scans `/var/lib/troshka/gateway/*/tls/proxy.json`, relaunches each terminator; called from `main()` beside `_restore_dnsmasq()`.

- [ ] **Step 1: Write the failing test**

```python
class TestRestoreTlsProxies(unittest.TestCase):
    @patch("troshkad._start_tls_proxy")
    @patch("troshkad.glob.glob", return_value=["/var/lib/troshka/gateway/abcdef12/tls/proxy.json"])
    @patch("builtins.open", new_callable=mock_open,
           read_data='{"netns":"troshka-abcdef12","listen":"172.30.5.1:443",'
                     '"upstream":"172.30.5.3:80","cert_path":"/f","key_path":"/k"}')
    def test_relaunches_from_descriptor(self, _open, _glob, mock_start):
        troshkad._restore_tls_proxies()
        mock_start.assert_called_once()
        _pid, kwargs = mock_start.call_args, mock_start.call_args.kwargs
        assert "troshka-abcdef12" in mock_start.call_args[0]
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (`_restore_tls_proxies` undefined).

- [ ] **Step 3: Implement**

```python
def _restore_tls_proxies():
    """Relaunch showroom TLS terminators from stored descriptors on startup."""
    import json as _json
    for desc in glob.glob("/var/lib/troshka/gateway/*/tls/proxy.json"):
        try:
            with open(desc) as f:
                d = _json.load(f)
            project_id = desc.split("/gateway/")[1].split("/")[0]
            _start_tls_proxy(project_id, d["netns"], d["listen"], d["upstream"],
                             d["cert_path"], d["key_path"])
        except Exception:
            logger.warning("failed to restore TLS proxy from %s", desc, exc_info=True)
```
Then in `main()`, after `_restore_dnsmasq()`:
```python
    _restore_tls_proxies()
```

- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py src/troshkad/tests/
git commit -m "feat(troshkad): restore showroom TLS terminators on startup"
```

---

### Task 8: backend `_showroom_fqdn`

**Files:**
- Modify: `src/backend/app/services/deploy_service.py`
- Test: `src/backend/tests/test_showroom_tls.py` (new)

**Interfaces:**
- Produces: `_showroom_fqdn(project) -> str` — returns `f"showroom.{project.guid}.{project.domain}"` when `project.dns_provider_id and project.guid and project.domain` are all set, else `""`.

- [ ] **Step 1: Write the failing test**

```python
from types import SimpleNamespace
from app.services.deploy_service import _showroom_fqdn

def test_fqdn_when_dns_configured():
    p = SimpleNamespace(dns_provider_id="d", guid="abc", domain="example.com")
    assert _showroom_fqdn(p) == "showroom.abc.example.com"

def test_fqdn_empty_when_no_dns():
    p = SimpleNamespace(dns_provider_id=None, guid="abc", domain="example.com")
    assert _showroom_fqdn(p) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_showroom_tls.py -q`
Expected: FAIL (`_showroom_fqdn` undefined)

- [ ] **Step 3: Implement**

```python
def _showroom_fqdn(project) -> str:
    """Showroom FQDN for LE, or '' when the project has no DNS zone (=> self-signed)."""
    if project.dns_provider_id and project.guid and project.domain:
        return f"showroom.{project.guid}.{project.domain}"
    return ""
```

- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/deploy_service.py src/backend/tests/test_showroom_tls.py
git commit -m "feat(deploy): _showroom_fqdn helper for showroom TLS"
```

---

### Task 9: backend port-forward targets the terminator

**Files:**
- Modify: `src/backend/app/services/vxlan.py` (`_inject_showroom_port_forward`)
- Test: `src/backend/tests/test_vxlan_showroom_pf.py` (new; or extend an existing vxlan test file if present)

**Interfaces:**
- Consumes: existing `_inject_showroom_port_forward(port_forwards, topology, first_vni)`.
- Produces: injected showroom forward `{"extPort":"443","intIp":"172.30.<octet3>.1","intPort":"443","proto":"tcp","managedByShowroom":True}` (was `intIp=.3,intPort=80`). The terminator (Task 5/6) proxies `.1:443 → .3:80`.

- [ ] **Step 1: Write the failing test**

```python
from app.services.vxlan import _inject_showroom_port_forward

def _showroom_topo():
    return {"nodes": [{"type": "containerNode", "data": {"name": "showroom"}}]}

def test_pf_targets_terminator_on_gateway_ip():
    # first_vni 5 -> octet3 5 -> terminator at 172.30.5.1:443
    out = _inject_showroom_port_forward([], _showroom_topo(), 5)
    pf = next(p for p in out if str(p.get("extPort")) == "443")
    assert pf["intIp"] == "172.30.5.1"
    assert str(pf["intPort"]) == "443"
    assert pf["managedByShowroom"] is True
```
(Confirm the showroom node type/`_topology_has_showroom` shape from `vxlan.py` when writing the fixture.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_vxlan_showroom_pf.py -q`
Expected: FAIL (still `.3:80`)

- [ ] **Step 3: Implement**

In `_inject_showroom_port_forward`, change the target from the showroom infra IP `:80` to the transit gateway IP `:443`:
```python
    octet3 = int(first_vni) & 0xFF
    term_ip = f"172.30.{octet3}.1"   # terminator listens here (see showroom TLS edge)
    out = [
        pf for pf in port_forwards
        if not (str(pf.get("extPort")) == "443" and pf.get("managedByShowroom"))
    ]
    if not any(str(pf.get("extPort")) == "443" and pf.get("managedByShowroom") for pf in out):
        out.append({
            "extPort": "443", "intIp": term_ip, "intPort": "443",
            "proto": "tcp", "extIpId": "", "managedByShowroom": True,
        })
    return out
```
(Keep the existing signature and the `_topology_has_showroom`/`first_vni` guards.)

- [ ] **Step 4: Run test to verify it passes** — Expected: PASS. Also run the existing vxlan suite to catch regressions: `./venv/bin/python3 -m pytest tests/ -k vxlan -q`.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/vxlan.py src/backend/tests/test_vxlan_showroom_pf.py
git commit -m "feat(vxlan): point showroom :443 forward at the TLS terminator"
```

---

### Task 10: backend `_ensure_showroom_tls` orchestration

**Files:**
- Modify: `src/backend/app/services/deploy_service.py`
- Test: `src/backend/tests/test_showroom_tls.py`

**Interfaces:**
- Consumes: `_showroom_fqdn` (Task 8), `dns_service.create_dns_records`, `start_job`/`wait_for_job`, `DnsProvider`.
- Produces: `_ensure_showroom_tls(s, host, project, topology, eip, first_vni, netns) -> str` — returns the showroom URL (`https://<fqdn>` for LE, `https://<eip>` for self-signed). Steps: derive fqdn; if fqdn set, create the A record (`showroom.<guid>.<domain> → eip`) via the project's `DnsProvider`; start_job `gateway/tls-cert` (fqdn, eip, route53 creds from the DnsProvider config when type=="route53"); start_job `gateway/tls-proxy` (netns, `172.30.<octet3>.1:443`, `172.30.<octet3>.3:80`, cert paths). Non-fatal: any failure logs a warning and returns the best URL known.

- [ ] **Step 1: Write the failing test**

```python
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
import app.services.deploy_service as ds

def _proj(dns=True):
    return SimpleNamespace(id="abcdef12-0000-0000", dns_provider_id=("d" if dns else None),
                           guid="g", domain="example.com")

def test_ensure_tls_letsencrypt_path():
    host = MagicMock()
    with patch.object(ds, "start_job", return_value="j"), \
         patch.object(ds, "wait_for_job", side_effect=[
             {"status": "completed", "result": {"cert_path": "/f", "key_path": "/k", "mode": "letsencrypt"}},
             {"status": "completed", "result": {"pid": 1}}]), \
         patch.object(ds, "create_dns_records", return_value=[]) as mk_dns, \
         patch.object(ds, "_resolve_showroom_dns_provider",
                      return_value=("route53", {"access_key_id": "AK"})):
        url = ds._ensure_showroom_tls(MagicMock(), host, _proj(), {"nodes": []},
                                      "1.2.3.4", 5, "troshka-abcdef12")
    assert url == "https://showroom.g.example.com"
    mk_dns.assert_called_once()

def test_ensure_tls_self_signed_when_no_dns():
    host = MagicMock()
    with patch.object(ds, "start_job", return_value="j"), \
         patch.object(ds, "wait_for_job", side_effect=[
             {"status": "completed", "result": {"cert_path": "/f", "key_path": "/k", "mode": "self-signed"}},
             {"status": "completed", "result": {"pid": 1}}]), \
         patch.object(ds, "create_dns_records") as mk_dns:
        url = ds._ensure_showroom_tls(MagicMock(), host, _proj(dns=False), {"nodes": []},
                                      "1.2.3.4", 5, "troshka-abcdef12")
    assert url == "https://1.2.3.4"
    mk_dns.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (`_ensure_showroom_tls`/`_resolve_showroom_dns_provider` undefined).

- [ ] **Step 3: Implement**

```python
def _resolve_showroom_dns_provider(s, project):
    """Return (type, config) for the project's DnsProvider, or (None, {})."""
    if not project.dns_provider_id:
        return None, {}
    from app.models.dns_provider import DnsProvider
    dp = s.query(DnsProvider).filter_by(id=project.dns_provider_id).first()
    return (dp.type, dp.config) if dp else (None, {})

def _ensure_showroom_tls(s, host, project, topology, eip, first_vni, netns) -> str:
    """Create DNS + cert + TLS terminator for the troshkad showroom. Non-fatal.
    Returns the showroom URL."""
    octet3 = int(first_vni) & 0xFF
    fqdn = _showroom_fqdn(project)
    dp_type, dp_config = _resolve_showroom_dns_provider(s, project)
    route53 = dp_config if dp_type == "route53" else {}
    try:
        if fqdn and dp_type:
            create_dns_records(dp_type, dp_config,
                               [{"name": fqdn, "type": "A", "value": eip}], ttl=30)
        cert = _tls_cert_via_job(host, project.id, fqdn, eip, route53)
        if not cert:
            logger.warning("Deploy %s: showroom cert job failed", project.id[:8])
            return f"https://{fqdn or eip}"
        _tls_proxy_via_job(host, project.id, netns,
                           f"172.30.{octet3}.1:443", f"172.30.{octet3}.3:80",
                           cert["cert_path"], cert["key_path"])
        return f"https://{fqdn}" if cert["mode"] == "letsencrypt" else f"https://{eip}"
    except Exception:
        logger.warning("Deploy %s: showroom TLS setup failed", project.id[:8], exc_info=True)
        return f"https://{fqdn or eip}"

def _tls_cert_via_job(host, project_id, fqdn, eip, route53):
    jid = start_job(host, "/gateway/tls-cert",
                    {"project_id": project_id, "fqdn": fqdn, "eip": eip, "route53": route53},
                    request_timeout=60)
    job = wait_for_job(host, jid, timeout=360)
    return job.get("result") if job.get("status") == "completed" else None

def _tls_proxy_via_job(host, project_id, netns, listen, upstream, cert_path, key_path):
    jid = start_job(host, "/gateway/tls-proxy",
                    {"project_id": project_id, "netns": netns, "listen": listen,
                     "upstream": upstream, "cert_path": cert_path, "key_path": key_path},
                    request_timeout=60)
    return wait_for_job(host, jid, timeout=60)
```
Add `from app.services.dns_service import create_dns_records` at module top (or local import) — match existing import style in the file.

- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/deploy_service.py src/backend/tests/test_showroom_tls.py
git commit -m "feat(deploy): _ensure_showroom_tls orchestration (DNS + cert + terminator)"
```

---

### Task 11: backend `_teardown_showroom_tls`

**Files:**
- Modify: `src/backend/app/services/deploy_service.py`
- Test: `src/backend/tests/test_showroom_tls.py`

**Interfaces:**
- Consumes: `_showroom_fqdn`, `_resolve_showroom_dns_provider`, `delete_dns_records`, `start_job`/`wait_for_job`.
- Produces: `_teardown_showroom_tls(s, host, project, eip)` — stops the terminator (`gateway/tls-proxy-stop`) and deletes the A record. Non-fatal.

- [ ] **Step 1: Write the failing test**

```python
def test_teardown_stops_proxy_and_deletes_dns():
    host = MagicMock()
    with patch.object(ds, "start_job", return_value="j"), \
         patch.object(ds, "wait_for_job", return_value={"status": "completed", "result": {"stopped": True}}), \
         patch.object(ds, "delete_dns_records", return_value=[]) as mk_del, \
         patch.object(ds, "_resolve_showroom_dns_provider", return_value=("route53", {"x": 1})):
        ds._teardown_showroom_tls(MagicMock(), host, _proj(), "1.2.3.4")
    mk_del.assert_called_once()

def test_teardown_skips_dns_when_none():
    host = MagicMock()
    with patch.object(ds, "start_job", return_value="j"), \
         patch.object(ds, "wait_for_job", return_value={"status": "completed", "result": {}}), \
         patch.object(ds, "delete_dns_records") as mk_del, \
         patch.object(ds, "_resolve_showroom_dns_provider", return_value=(None, {})):
        ds._teardown_showroom_tls(MagicMock(), host, _proj(dns=False), "1.2.3.4")
    mk_del.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (`_teardown_showroom_tls` undefined).

- [ ] **Step 3: Implement**

```python
def _teardown_showroom_tls(s, host, project, eip):
    """Stop the showroom terminator and delete its DNS record. Non-fatal."""
    try:
        jid = start_job(host, "/gateway/tls-proxy-stop",
                        {"project_id": project.id}, request_timeout=30)
        wait_for_job(host, jid, timeout=30)
    except Exception:
        logger.warning("Deploy %s: showroom TLS stop failed", project.id[:8], exc_info=True)
    fqdn = _showroom_fqdn(project)
    dp_type, dp_config = _resolve_showroom_dns_provider(s, project)
    if fqdn and dp_type:
        try:
            delete_dns_records(dp_type, dp_config,
                               [{"name": fqdn, "type": "A", "value": eip}])
        except Exception:
            logger.warning("Deploy %s: showroom DNS delete failed", project.id[:8], exc_info=True)
```
Add `from app.services.dns_service import delete_dns_records` (match existing import style).

- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/deploy_service.py src/backend/tests/test_showroom_tls.py
git commit -m "feat(deploy): _teardown_showroom_tls (stop terminator + delete DNS)"
```

---

### Task 12: Wire ensure/teardown into deploy + destroy

**Files:**
- Modify: `src/backend/app/services/deploy_service.py` (troshkad deploy path + destroy path)
- Test: `src/backend/tests/test_showroom_tls.py`

**Interfaces:**
- Consumes: `_ensure_showroom_tls` (Task 10), `_teardown_showroom_tls` (Task 11), `vxlan._topology_has_showroom`, `_ROUTE_PROVIDERS`, `Provider` model.
- Produces: `_maybe_setup_showroom_tls(s, host, topology, project, external_ips, vni_map)` — called on the troshkad deploy path (skips when the provider is in `_ROUTE_PROVIDERS`, no showroom, no EIP, or no VNI). Persists the URL to `project.deployed_topology["_showroom_url"]`.
- Real patterns to reuse (verified): provider type via `prov = s.get(Provider, host.provider_id); provider_type = prov.type if prov else None`; first VNI via `next(iter(vni_map.values()), None)` (there is no `_first_vni_for` helper — `vni_map` is already in scope at the wiring site, `deploy_service.py:8741`).

- [ ] **Step 1: Write the failing test**

```python
def test_maybe_setup_skips_route_providers():
    host = SimpleNamespace(provider_id="p")
    prov = SimpleNamespace(type="kubevirt")
    sess = MagicMock(); sess.get.return_value = prov
    with patch.object(ds, "_ensure_showroom_tls") as mk:
        ds._maybe_setup_showroom_tls(sess, host, {"nodes": []}, _proj(),
                                     [{"ip": "1.2.3.4"}], {"net": 5})
    mk.assert_not_called()

def test_maybe_setup_runs_for_cloud_with_showroom():
    host = SimpleNamespace(provider_id="p")
    prov = SimpleNamespace(type="ec2")
    proj = _proj()
    proj.deployed_topology = None
    sess = MagicMock(); sess.get.return_value = prov
    topo = {"nodes": [{"type": "containerNode", "data": {"name": "showroom"}}]}
    with patch.object(ds, "_topology_has_showroom", return_value=True), \
         patch.object(ds, "_ensure_showroom_tls", return_value="https://x") as mk:
        ds._maybe_setup_showroom_tls(sess, host, topo, proj, [{"ip": "1.2.3.4"}], {"net": 5})
    mk.assert_called_once()
    assert proj.deployed_topology["_showroom_url"] == "https://x"
```
(`_proj()` from Task 10; add `import` of `_topology_has_showroom` into `deploy_service` so it is patchable as `ds._topology_has_showroom`, or patch it at `app.services.vxlan._topology_has_showroom` — match how the file imports it.)

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (`_maybe_setup_showroom_tls` undefined).

- [ ] **Step 3: Implement**

```python
def _maybe_setup_showroom_tls(s, host, topology, project, external_ips, vni_map):
    """Cloud (non-route) providers only: stand up the showroom TLS edge and
    persist the resulting URL. Non-fatal."""
    from app.services.vxlan import _topology_has_showroom

    prov = s.get(Provider, host.provider_id)
    provider_type = prov.type if prov else None
    if provider_type in _ROUTE_PROVIDERS or not _topology_has_showroom(topology):
        return
    eip = next((e.get("ip") or e.get("_public_ip") for e in (external_ips or [])
                if e.get("ip") or e.get("_public_ip")), None)
    first_vni = next(iter((vni_map or {}).values()), None)
    if not eip or not first_vni:
        return
    netns = f"troshka-{project.id[:8]}"
    url = _ensure_showroom_tls(s, host, project, topology, eip, first_vni, netns)
    logger.info("Deploy %s: showroom TLS URL %s", project.id[:8], url)
    deployed_topo = project.deployed_topology or {}
    deployed_topo["_showroom_url"] = url
    project.deployed_topology = deployed_topo
    s.commit()
```
Call `_maybe_setup_showroom_tls(s, host, topology, project, external_ips, vni_map)` on the troshkad single-host deploy path, right after `inject_showroom_gateway_port_forwards(...)` / the DNS-record step (near `deploy_service.py:8741`, where `provider_type`, `vni_map`, and `external_ips` are all in scope). Call `_teardown_showroom_tls(s, host, project, eip)` on the destroy path where other per-project host resources are torn down (derive `eip` from the deployed EIP the same way the destroy path already resolves per-project IPs). The `_showroom_url` in `deployed_topology` is available for the External Access panel; wiring the frontend to render it as a clickable HTTPS link is a follow-up (out of scope for this plan).

- [ ] **Step 4: Run tests + regression**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_showroom_tls.py -q`
Then the deploy suites: `./venv/bin/python3 -m pytest tests/test_deploy_orchestration.py tests/test_deploy_coverage.py tests/test_deploy_coverage2.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/deploy_service.py src/backend/tests/test_showroom_tls.py
git commit -m "feat(deploy): wire showroom TLS edge into troshkad deploy + destroy"
```

---

### Task 13: Full verification + formatting

**Files:** none (verification only)

- [ ] **Step 1: Backend suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -q`
Expected: all pass (the pre-existing order-dependent `test_deploy_no_host_sets_error` passes in full-suite order).

- [ ] **Step 2: troshkad suite**

Run: `cd src/troshkad && python3 -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 3: Format + type-check**

Run: `cd /Users/prutledg/troshka && black src/troshkad/troshkad.py src/backend/app/services/deploy_service.py src/backend/app/services/vxlan.py && pyright src/backend/app/services/deploy_service.py src/backend/app/services/vxlan.py src/troshkad/troshkad.py`
Expected: 0 errors.

- [ ] **Step 4: Commit any formatting**

```bash
cd /Users/prutledg/troshka && git add -A && git commit -m "chore: format showroom TLS edge" || echo "nothing to format"
```

---

## Notes for the executor

- Verify helper names before use (marked inline): `_validate_project_id` (troshkad), `_provider_type_for_host`/`_first_vni_for` and the showroom-node shape in `vxlan.py`. Reuse existing helpers; do not add parallel ones.
- The `certbot renew` cron installed at bootstrap already renews the LE cert; no per-project renew wiring is needed. When the cert renews in place, the running `socat` keeps the old `combined.pem` until the next terminator restart — acceptable for now (documented follow-up: reload terminator post-renew).
- Do not touch the KubeVirt/OCP-Virt route path.
