#!/usr/bin/env python3
"""Local oc helper: tunnel nested OCP APIs through Troshka WebSocket.

Does not modify showroom cluster-terminal kubeconfigs — those stay VIP-rewritten
on the showroom disk. This tool only writes ~/.troshka/kube/<project>.yaml.

Background use (default)::

    eval "$(./scripts/troshka-oc use <project> [cluster])"
    oc get nodes
    ./scripts/troshka-oc teardown <project>

Stdout from ``use`` is eval-safe (export lines only). Status goes to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import websockets
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover
    print("Error: websockets package required (use backend venv)", file=sys.stderr)
    sys.exit(1)

# Allow importing backend helpers when run from repo checkout.
_BACKEND = Path(__file__).resolve().parents[1] / "src" / "backend"
if _BACKEND.is_dir() and str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services.ocp.api_tunnel import localhost_kubeconfig  # noqa: E402
from app.services.ocp.kubeconfig_merge import merge_kubeconfigs  # noqa: E402

API_URL = os.environ.get("TROSHKA_API_URL", "http://localhost:8200").rstrip("/")
API_KEY = os.environ.get("TROSHKA_API_KEY", "")
KUBE_DIR = Path.home() / ".troshka" / "kube"
_READY_TIMEOUT_S = 90


def _headers() -> dict[str, str]:
    h = {"Accept": "application/json"}
    if API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


def _api(path: str) -> object:
    req = urllib.request.Request(f"{API_URL}{path}", headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "yaml" in ctype or path.endswith("/kubeconfig"):
                return body.decode()
            return json.loads(body.decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise SystemExit(f"API {exc.code} {path}: {detail}") from exc


def _resolve_project(prefix: str) -> dict:
    data = _api("/api/v1/projects/")
    projs = data if isinstance(data, list) else data.get("projects", data.get("items", []))
    matches = [
        p
        for p in projs
        if str(p.get("id", "")).startswith(prefix) or p.get("name") == prefix
    ]
    if not matches:
        raise SystemExit(f'No project matching "{prefix}"')
    if len(matches) > 1:
        lines = "\n".join(f"  {p['id'][:8]}  {p.get('name')}" for p in matches)
        raise SystemExit(f"Multiple projects match:\n{lines}")
    return matches[0]


def _list_clusters(project_id: str) -> list[dict]:
    data = _api(f"/api/v1/projects/{project_id}/clusters")
    if not isinstance(data, list):
        raise SystemExit("Unexpected clusters response")
    return data


def _fetch_kubeconfig(project_id: str, cluster_id: str) -> str:
    raw = _api(f"/api/v1/projects/{project_id}/clusters/{cluster_id}/kubeconfig")
    if not isinstance(raw, str) or not raw.strip():
        raise SystemExit(f"Empty kubeconfig for cluster {cluster_id}")
    return raw


def _ws_url(project_id: str, cluster_id: str) -> str:
    base = API_URL.replace("https://", "wss://").replace("http://", "ws://")
    url = f"{base}/api/v1/projects/{project_id}/clusters/{cluster_id}/api-tunnel"
    if API_KEY:
        from urllib.parse import quote

        url += f"?token={quote(API_KEY)}"
    return url


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _project_key(project_id: str) -> str:
    return project_id[:8]


def _kubeconfig_path(project_id: str) -> Path:
    return KUBE_DIR / f"{_project_key(project_id)}.yaml"


def _state_path(project_id: str) -> Path:
    return KUBE_DIR / f"{_project_key(project_id)}.state.json"


def _pid_path(project_id: str) -> Path:
    return KUBE_DIR / f"{_project_key(project_id)}.pid"


def _log_path(project_id: str) -> Path:
    return KUBE_DIR / f"{_project_key(project_id)}.log"


def _read_state(project_id: str) -> dict | None:
    path = _state_path(project_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_state(project_id: str, state: dict) -> None:
    KUBE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_path(project_id)
    path.write_text(json.dumps(state, indent=2) + "\n")
    path.chmod(0o600)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _running_daemon(project_id: str) -> dict | None:
    state = _read_state(project_id)
    if not state:
        return None
    pid = int(state.get("pid") or 0)
    if not _pid_alive(pid):
        return None
    return state


async def _relay_one_connection(
    project_id: str,
    cluster_id: str,
    local_reader: asyncio.StreamReader,
    local_writer: asyncio.StreamWriter,
) -> dict:
    """Open one API tunnel WS and bridge a single local TCP client. Returns ready."""
    ssl_ctx = None
    if _ws_url(project_id, cluster_id).startswith("wss://"):
        ssl_ctx = ssl.create_default_context()

    async with ws_connect(
        _ws_url(project_id, cluster_id),
        ssl=ssl_ctx,
        max_size=8 * 1024 * 1024,
        open_timeout=30,
    ) as ws:
        first = await asyncio.wait_for(ws.recv(), timeout=60)
        if isinstance(first, bytes):
            first = first.decode()
        ready = json.loads(first)
        if ready.get("type") == "error":
            raise RuntimeError(ready.get("error") or "tunnel error")
        if ready.get("type") != "ready":
            raise RuntimeError(f"unexpected tunnel handshake: {ready!r}")

        async def tcp_to_ws():
            try:
                while True:
                    data = await local_reader.read(65536)
                    if not data:
                        break
                    await ws.send(data)
            finally:
                try:
                    await ws.close()
                except Exception:
                    pass

        async def ws_to_tcp():
            try:
                async for message in ws:
                    if isinstance(message, str):
                        continue
                    local_writer.write(message)
                    await local_writer.drain()
            finally:
                try:
                    local_writer.close()
                    await local_writer.wait_closed()
                except Exception:
                    pass

        await asyncio.wait(
            [
                asyncio.create_task(tcp_to_ws()),
                asyncio.create_task(ws_to_tcp()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        return ready


async def _probe_ready(project_id: str, cluster_id: str) -> dict:
    """Open a short-lived tunnel to learn tls_server_name / force_insecure."""
    ssl_ctx = None
    url = _ws_url(project_id, cluster_id)
    if url.startswith("wss://"):
        ssl_ctx = ssl.create_default_context()
    async with ws_connect(url, ssl=ssl_ctx, open_timeout=30) as ws:
        first = await asyncio.wait_for(ws.recv(), timeout=60)
        if isinstance(first, bytes):
            first = first.decode()
        ready = json.loads(first)
        if ready.get("type") == "error":
            raise RuntimeError(ready.get("error") or "tunnel error")
        if ready.get("type") != "ready":
            raise RuntimeError(f"unexpected tunnel handshake: {ready!r}")
        await ws.close()
        return ready


async def _serve_cluster_tunnel(
    project_id: str,
    cluster_id: str,
    port: int,
    ready_holder: dict,
) -> None:
    async def _on_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await _relay_one_connection(project_id, cluster_id, reader, writer)
        except Exception as exc:
            print(f"troshka-oc tunnel error ({cluster_id}): {exc}", file=sys.stderr)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(_on_client, "127.0.0.1", port)
    ready_holder.update(await _probe_ready(project_id, cluster_id))
    async with server:
        await server.serve_forever()


def _write_merged_kubeconfig(
    project: dict,
    clusters: list[dict],
    ports: dict[str, int],
    ready_by_id: dict[str, dict],
) -> Path:
    KUBE_DIR.mkdir(parents=True, exist_ok=True)
    named: list[tuple[str, str]] = []
    for c in clusters:
        cid = c["id"]
        raw = _fetch_kubeconfig(project["id"], cid)
        ready = ready_by_id.get(cid) or {}
        rewritten = localhost_kubeconfig(
            raw,
            ports[cid],
            tls_server_name=str(
                ready.get("tls_server_name") or c.get("tls_server_name") or ""
            ),
            force_insecure=bool(ready.get("force_insecure")),
        )
        named.append((c.get("name") or cid, rewritten))
    merged = merge_kubeconfigs(named)
    path = _kubeconfig_path(project["id"])
    path.write_text(merged)
    path.chmod(0o600)
    return path


def _select_clusters(all_clusters: list[dict], cluster_arg: str | None) -> list[dict]:
    available = [c for c in all_clusters if c.get("kubeconfig_available")]
    if not available:
        raise SystemExit("No clusters have a harvested kubeconfig yet")
    if not cluster_arg:
        return available
    needle = cluster_arg.lower()
    matches = [
        c
        for c in available
        if str(c.get("id", "")).lower() == needle
        or str(c.get("name", "")).lower() == needle
        or str(c.get("id", "")).lower().startswith(needle)
    ]
    if not matches:
        names = ", ".join(c.get("name") or c["id"] for c in available)
        raise SystemExit(f'Cluster "{cluster_arg}" not found. Available: {names}')
    return matches


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


async def _run_tunnels_until_ready(
    project: dict,
    clusters: list[dict],
) -> tuple[Path, dict[str, int], dict[str, dict], list[asyncio.Task]]:
    ports = {c["id"]: _pick_free_port() for c in clusters}
    ready_by_id: dict[str, dict] = {c["id"]: {} for c in clusters}
    tasks = [
        asyncio.create_task(
            _serve_cluster_tunnel(
                project["id"], c["id"], ports[c["id"]], ready_by_id[c["id"]]
            )
        )
        for c in clusters
    ]
    for _ in range(_READY_TIMEOUT_S * 4):
        if all(ready_by_id[c["id"]] for c in clusters):
            break
        for t in tasks:
            if t.done() and t.exception():
                raise t.exception()  # type: ignore[misc]
        await asyncio.sleep(0.25)
    else:
        missing = [c["name"] for c in clusters if not ready_by_id[c["id"]]]
        for t in tasks:
            t.cancel()
        raise SystemExit(f"Tunnel handshake timed out for: {', '.join(missing)}")

    path = _write_merged_kubeconfig(project, clusters, ports, ready_by_id)
    return path, ports, ready_by_id, tasks


def _print_export(path: Path) -> None:
    """Eval-safe stdout: only shell export lines."""
    print(f"export KUBECONFIG={path}")


def _print_tunnel_summary(
    project: dict,
    clusters: list[dict],
    ports: dict[str, int],
    ready_by_id: dict[str, dict],
    *,
    pid: int | None = None,
) -> None:
    print(
        f"troshka-oc: tunnels ready for {project['id'][:8]}  {project.get('name')}",
        file=sys.stderr,
    )
    if pid:
        print(f"troshka-oc: daemon pid {pid}", file=sys.stderr)
    for c in clusters:
        r = ready_by_id[c["id"]]
        print(
            f"  context={c.get('name') or c['id']}  "
            f"local=127.0.0.1:{ports[c['id']]}  "
            f"via={r.get('via')}  sni={r.get('tls_server_name')}",
            file=sys.stderr,
        )


async def _daemon_serve(
    project: dict,
    clusters: list[dict],
) -> int:
    """Run as background daemon: ready state file, then serve forever."""
    path, ports, ready_by_id, tasks = await _run_tunnels_until_ready(project, clusters)
    state = {
        "pid": os.getpid(),
        "project_id": project["id"],
        "project_name": project.get("name"),
        "kubeconfig": str(path),
        "api_url": API_URL,
        "clusters": [
            {
                "id": c["id"],
                "name": c.get("name") or c["id"],
                "port": ports[c["id"]],
                "via": ready_by_id[c["id"]].get("via"),
                "tls_server_name": ready_by_id[c["id"]].get("tls_server_name"),
            }
            for c in clusters
        ],
        "status": "ready",
        "started_at": time.time(),
    }
    _write_state(project["id"], state)
    _pid_path(project["id"]).write_text(f"{os.getpid()}\n")

    stop = asyncio.Event()

    def _stop(*_args):
        stop.set()
        for t in tasks:
            t.cancel()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_a: _stop())

    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    for t in tasks:
        t.cancel()
    return 0


def _spawn_daemon(project_prefix: str, cluster: str | None) -> int:
    """Fork/spawn daemon worker, wait until ready, print export to stdout."""
    import subprocess

    KUBE_DIR.mkdir(parents=True, exist_ok=True)
    # Resolve first so we know the state file path before spawning.
    project = _resolve_project(project_prefix)
    clusters = _select_clusters(_list_clusters(project["id"]), cluster)
    if not clusters:
        raise SystemExit("No clusters with kubeconfigs in this project")

    existing = _running_daemon(project["id"])
    if existing:
        print(
            f"troshka-oc: replacing existing daemon pid {existing.get('pid')}",
            file=sys.stderr,
        )
        _teardown_project(project["id"], quiet=True)

    # Clear stale ready marker so we wait for the new daemon.
    state_path = _state_path(project["id"])
    if state_path.is_file():
        try:
            state_path.unlink()
        except OSError:
            pass

    helper = Path(__file__).resolve()
    venv_python = os.environ.get("TROSHKA_OC_PYTHON") or sys.executable
    cmd = [
        venv_python,
        str(helper),
        "_daemon",
        project["id"],
    ]
    if cluster:
        cmd.append(cluster)

    log_path = _log_path(project["id"])
    log_f = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    log_f.write(f"\n--- daemon start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    log_f.flush()

    env = os.environ.copy()
    env["TROSHKA_API_URL"] = API_URL
    if API_KEY:
        env["TROSHKA_API_KEY"] = API_KEY

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    log_f.close()

    deadline = time.time() + _READY_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = ""
            try:
                tail = log_path.read_text()[-800:]
            except OSError:
                pass
            raise SystemExit(
                f"troshka-oc daemon exited early (code {proc.returncode}).\n{tail}"
            )
        state = _read_state(project["id"])
        if state and state.get("status") == "ready" and int(state.get("pid") or 0) == proc.pid:
            path = Path(state["kubeconfig"])
            ports = {c["id"]: c["port"] for c in state.get("clusters") or []}
            ready_by_id = {
                c["id"]: {
                    "via": c.get("via"),
                    "tls_server_name": c.get("tls_server_name"),
                }
                for c in state.get("clusters") or []
            }
            # Rebuild cluster list order from state for summary.
            cluster_rows = [
                {"id": c["id"], "name": c.get("name") or c["id"]}
                for c in state.get("clusters") or []
            ]
            _print_tunnel_summary(
                project, cluster_rows, ports, ready_by_id, pid=proc.pid
            )
            print(
                f"troshka-oc: log {log_path}",
                file=sys.stderr,
            )
            print(
                "troshka-oc: stop with  ./scripts/troshka-oc teardown "
                f"{project['id'][:8]}",
                file=sys.stderr,
            )
            _print_export(path)
            return 0
        time.sleep(0.25)

    # Timed out — kill orphan.
    try:
        os.kill(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    raise SystemExit(
        f"troshka-oc: daemon did not become ready within {_READY_TIMEOUT_S}s "
        f"(see {log_path})"
    )


def _teardown_project(project_id: str, *, quiet: bool = False) -> bool:
    """Kill daemon for project_id. Returns True if something was stopped."""
    state = _read_state(project_id)
    pid = 0
    if state:
        pid = int(state.get("pid") or 0)
    if not pid:
        try:
            pid = int(_pid_path(project_id).read_text().strip() or "0")
        except (OSError, ValueError):
            pid = 0

    stopped = False
    if pid and _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except OSError as exc:
            if not quiet:
                print(f"troshka-oc: failed to signal pid {pid}: {exc}", file=sys.stderr)
        # Wait briefly for exit.
        for _ in range(40):
            if not _pid_alive(pid):
                break
            time.sleep(0.05)
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    elif not quiet and not state:
        print(
            f"troshka-oc: no tunnel daemon for {project_id[:8]}",
            file=sys.stderr,
        )

    for path in (
        _state_path(project_id),
        _pid_path(project_id),
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    if stopped and not quiet:
        print(f"troshka-oc: stopped daemon pid {pid}", file=sys.stderr)
        print(
            f"troshka-oc: unset with  unset KUBECONFIG  "
            f"(was {_kubeconfig_path(project_id)})",
            file=sys.stderr,
        )
    return stopped


def cmd_list(project_prefix: str) -> int:
    project = _resolve_project(project_prefix)
    clusters = _list_clusters(project["id"])
    print(f"{project['id'][:8]}  {project.get('name')}")
    daemon = _running_daemon(project["id"])
    if daemon:
        print(f"  tunnel: running (pid {daemon.get('pid')})  kubeconfig={daemon.get('kubeconfig')}")
    else:
        print("  tunnel: not running")
    for c in clusters:
        flag = "yes" if c.get("kubeconfig_available") else "no"
        print(
            f"  {c.get('name') or c['id']:<20}  "
            f"kubeconfig={flag}  apiVip={c.get('api_vip') or '-'}  "
            f"route={c.get('route_hostname') or '-'}"
        )
    return 0


def cmd_status(project_prefix: str | None) -> int:
    if project_prefix:
        project = _resolve_project(project_prefix)
        ids = [project["id"]]
    else:
        ids = []
        if KUBE_DIR.is_dir():
            for path in sorted(KUBE_DIR.glob("*.state.json")):
                try:
                    st = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                pid = st.get("project_id")
                if pid:
                    ids.append(str(pid))
    if not ids:
        print("troshka-oc: no tunnel state files", file=sys.stderr)
        return 0
    for pid in ids:
        st = _running_daemon(pid)
        key = pid[:8]
        if not st:
            print(f"{key}  not running")
            continue
        print(
            f"{key}  pid={st.get('pid')}  kubeconfig={st.get('kubeconfig')}  "
            f"clusters={','.join(c.get('name') or c['id'] for c in st.get('clusters') or [])}"
        )
    return 0


def cmd_teardown(project_prefix: str | None) -> int:
    if not project_prefix:
        # Tear down all running daemons we know about.
        stopped_any = False
        if KUBE_DIR.is_dir():
            for path in list(KUBE_DIR.glob("*.state.json")):
                try:
                    st = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                pid = str(st.get("project_id") or "")
                if pid and _teardown_project(pid):
                    stopped_any = True
        if not stopped_any:
            print("troshka-oc: nothing to tear down", file=sys.stderr)
        # Eval-friendly clear when tearing down "current" implicitly.
        print("unset KUBECONFIG")
        return 0

    project = _resolve_project(project_prefix)
    _teardown_project(project["id"])
    # So `eval "$(troshka-oc teardown …)"` clears the var when it pointed here.
    print("unset KUBECONFIG")
    return 0


def cmd_use(
    project_prefix: str,
    cluster: str | None,
    *,
    shell: bool,
    foreground: bool,
) -> int:
    if shell or foreground:
        project = _resolve_project(project_prefix)
        clusters = _select_clusters(_list_clusters(project["id"]), cluster)
        return asyncio.run(
            _run_foreground(project, clusters, shell=shell)
        )
    return _spawn_daemon(project_prefix, cluster)


async def _run_foreground(
    project: dict,
    clusters: list[dict],
    *,
    shell: bool,
) -> int:
    path, ports, ready_by_id, tasks = await _run_tunnels_until_ready(project, clusters)
    _print_tunnel_summary(project, clusters, ports, ready_by_id)
    _print_export(path)
    print(
        "troshka-oc: foreground mode — Ctrl-C stops tunnels "
        "(use default `use` to daemonize)",
        file=sys.stderr,
    )

    env = os.environ.copy()
    env["KUBECONFIG"] = str(path)

    if shell:
        shell_path = env.get("SHELL") or "/bin/bash"
        print(
            f"troshka-oc: starting {shell_path} with KUBECONFIG set",
            file=sys.stderr,
        )
        proc = await asyncio.create_subprocess_exec(shell_path, env=env)
        code = await proc.wait()
        for t in tasks:
            t.cancel()
        return int(code or 0)

    stop = asyncio.Event()

    def _stop(*_args):
        stop.set()
        for t in tasks:
            t.cancel()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    return 0


def cmd_oc(project_prefix: str, cluster: str | None, oc_args: list[str]) -> int:
    if not oc_args:
        raise SystemExit("No oc arguments provided")
    if not shutil_which("oc"):
        raise SystemExit("`oc` not found on PATH")
    project = _resolve_project(project_prefix)
    clusters = _select_clusters(_list_clusters(project["id"]), cluster)

    async def _run() -> int:
        path, _ports, _ready, tasks = await _run_tunnels_until_ready(project, clusters)
        env = os.environ.copy()
        env["KUBECONFIG"] = str(path)
        proc = await asyncio.create_subprocess_exec("oc", *oc_args, env=env)
        code = await proc.wait()
        for t in tasks:
            t.cancel()
        return int(code or 0)

    return asyncio.run(_run())


def cmd_daemon_worker(project_id: str, cluster: str | None) -> int:
    """Internal: long-running tunnel process (spawned by ``use``)."""
    # project_id is already a full UUID from the parent.
    data = _api(f"/api/v1/projects/{project_id}")
    if not isinstance(data, dict) or not data.get("id"):
        # Fall back to list resolve if direct get shape differs.
        project = _resolve_project(project_id)
    else:
        project = data
    clusters = _select_clusters(_list_clusters(project["id"]), cluster)
    return asyncio.run(_daemon_serve(project, clusters))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "Usage:\n"
            "  eval \"$(troshka-oc use <project> [cluster])\"   # daemonize + export\n"
            "  troshka-oc teardown [project]                    # stop daemon (+ unset)\n"
            "  troshka-oc status [project]\n"
            "  troshka-oc list <project>\n"
            "  troshka-oc use --foreground|--shell <project> [cluster]\n"
            "  troshka-oc [--project P] [--cluster C] <oc args...>\n"
            "\n"
            "Background `use` prints only `export KUBECONFIG=...` on stdout so you can:\n"
            "  eval \"$(./scripts/troshka-oc use e0f60a08)\"\n"
            "  oc config use-context <cluster>\n",
            end="",
        )
        return 0 if argv else 2

    if argv[0] == "_daemon":
        # Internal worker entrypoint.
        p = argparse.ArgumentParser(prog="troshka-oc _daemon")
        p.add_argument("project_id")
        p.add_argument("cluster", nargs="?")
        args = p.parse_args(argv[1:])
        return cmd_daemon_worker(args.project_id, args.cluster)

    if argv[0] == "list":
        p = argparse.ArgumentParser(prog="troshka-oc list")
        p.add_argument("project")
        args = p.parse_args(argv[1:])
        return cmd_list(args.project)

    if argv[0] == "status":
        p = argparse.ArgumentParser(prog="troshka-oc status")
        p.add_argument("project", nargs="?")
        args = p.parse_args(argv[1:])
        return cmd_status(args.project)

    if argv[0] == "teardown":
        p = argparse.ArgumentParser(prog="troshka-oc teardown")
        p.add_argument("project", nargs="?")
        args = p.parse_args(argv[1:])
        return cmd_teardown(args.project)

    if argv[0] == "use":
        p = argparse.ArgumentParser(prog="troshka-oc use")
        p.add_argument("project")
        p.add_argument("cluster", nargs="?")
        p.add_argument(
            "--shell",
            action="store_true",
            help="Foreground: open a shell with KUBECONFIG (tunnels die on exit)",
        )
        p.add_argument(
            "--foreground",
            "-f",
            action="store_true",
            help="Keep tunnels in this terminal (Ctrl-C to stop)",
        )
        args = p.parse_args(argv[1:])
        return cmd_use(
            args.project,
            args.cluster,
            shell=args.shell,
            foreground=args.foreground,
        )

    p = argparse.ArgumentParser(prog="troshka-oc")
    p.add_argument("--project", "-P")
    p.add_argument("--cluster", "-C")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    args = p.parse_args(argv)
    rest = list(args.rest or [])
    if rest and rest[0] == "--":
        rest = rest[1:]
    project = args.project
    if not project:
        if not rest:
            print("Error: project required", file=sys.stderr)
            return 2
        project = rest[0]
        rest = rest[1:]
    return cmd_oc(project, args.cluster, rest)


if __name__ == "__main__":
    sys.exit(main())
