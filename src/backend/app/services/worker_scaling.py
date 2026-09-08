"""DB-connection-aware worker scaling.

Adding RQ worker replicas raises the total Postgres connection count
(``workers*worker_pool + backend*(pool+overflow) + reserve``). Too many workers
once exhausted Postgres (see the infra01 incident), so the admin "add worker"
control is capped by what the DB can actually support. The math here is pure and
unit-tested; the k8s read/patch wiring lives in the API layer.
"""

from typing import Any, cast

# Default headroom left for superuser slots, migrations, psql sessions, etc.
DEFAULT_CONN_RESERVE = 10


def compute_max_workers(
    max_connections: int,
    backend_replicas: int,
    backend_conns_per_proc: int,
    worker_conns_per_proc: int,
    reserve: int = DEFAULT_CONN_RESERVE,
) -> int:
    """Largest worker replica count that keeps total DB connections under
    ``max_connections``. Always at least 1 (a fleet must have a worker)."""
    budget = (
        max_connections
        - max(0, reserve)
        - max(0, backend_replicas) * max(0, backend_conns_per_proc)
    )
    per_worker = worker_conns_per_proc if worker_conns_per_proc > 0 else 1
    return max(1, budget // per_worker)


def clamp_replicas(desired: int, max_workers: int) -> int:
    """Clamp a requested replica count to ``[1, max_workers]`` (max floored at 1)."""
    return max(1, min(desired, max(1, max_workers)))


def db_pool_fits(
    max_connections: int,
    pool_size: int,
    max_overflow: int,
    backend_replicas: int,
    worker_replicas: int,
    reserve: int = DEFAULT_CONN_RESERVE,
) -> bool:
    """Whether a proposed per-process pool keeps the current fleet under
    ``max_connections``. Used to reject a pool-size bump that would exhaust the DB.
    """
    per_proc = max(0, pool_size) + max(0, max_overflow)
    total = (max(0, backend_replicas) + max(0, worker_replicas)) * per_proc + max(
        0, reserve
    )
    return total <= max_connections


# ── Live-env: k8s Deployment scaling + Postgres capacity ────────────────────
_WORKER_DEPLOYMENT = "troshka-worker"
_BACKEND_DEPLOYMENT = "troshka-backend"


def _apps_api():
    from kubernetes import client
    from kubernetes import config as k8s_config

    k8s_config.load_incluster_config()
    return client.AppsV1Api()


def _namespace() -> str:
    from app.services.app_updater import _get_own_namespace

    return _get_own_namespace()


def _env_int(dep, name: str, default: int) -> int:
    """Read an int env var from a Deployment's first container (else default)."""
    try:
        for c in dep.spec.template.spec.containers:
            for e in c.env or []:
                if e.name == name and e.value is not None:
                    return int(e.value)
    except Exception:  # noqa: BLE001
        pass
    return default


def _deploy_conns_per_proc(dep) -> int:
    """Per-process DB connections a Deployment's pods use (pool_size+max_overflow)."""
    return _env_int(dep, "TROSHKA_DATABASE__POOL_SIZE", 5) + _env_int(
        dep, "TROSHKA_DATABASE__MAX_OVERFLOW", 10
    )


def get_max_connections() -> int:
    """Postgres server ``max_connections`` (the hard ceiling on the whole fleet)."""
    from sqlalchemy import text

    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        return int(db.execute(text("SHOW max_connections")).scalar() or 0)
    finally:
        db.close()


def worker_scaling_status() -> dict:
    """[LIVE-ENV] Live worker replicas + the DB-derived max + the connection math.

    Raises on non-Kubernetes deployments (no in-cluster config) — the caller maps
    that to a clean "only available on Kubernetes" response.
    """
    apps = _apps_api()
    ns = _namespace()
    wdep = cast(Any, apps.read_namespaced_deployment(_WORKER_DEPLOYMENT, ns))
    bdep = cast(Any, apps.read_namespaced_deployment(_BACKEND_DEPLOYMENT, ns))
    worker_replicas = int(getattr(wdep.spec, "replicas", 0) or 0)
    backend_replicas = int(getattr(bdep.spec, "replicas", 0) or 0)
    worker_conns = _deploy_conns_per_proc(wdep)
    backend_conns = _deploy_conns_per_proc(bdep)
    max_conns = get_max_connections()
    return {
        "worker_replicas": worker_replicas,
        "max_workers": compute_max_workers(
            max_conns, backend_replicas, backend_conns, worker_conns
        ),
        "max_connections": max_conns,
        "worker_conns_per_proc": worker_conns,
        "backend_conns_per_proc": backend_conns,
        "backend_replicas": backend_replicas,
        "pool_size": _env_int(bdep, "TROSHKA_DATABASE__POOL_SIZE", 5),
        "max_overflow": _env_int(bdep, "TROSHKA_DATABASE__MAX_OVERFLOW", 10),
    }


def scale_workers(delta: int) -> dict:
    """[LIVE-ENV] Scale ``troshka-worker`` by ``delta``, clamped to [1, max]."""
    st = worker_scaling_status()
    target = clamp_replicas(st["worker_replicas"] + int(delta), st["max_workers"])
    _apps_api().patch_namespaced_deployment(
        _WORKER_DEPLOYMENT, _namespace(), {"spec": {"replicas": target}}
    )
    st["worker_replicas"] = target
    return st


def _patch_pool_env(apps, ns: str, dep_name: str, pool_size: int, max_overflow: int):
    """Set the pool env on a Deployment's containers (triggers a rolling restart)."""
    dep = cast(Any, apps.read_namespaced_deployment(dep_name, ns))
    wanted = {
        "TROSHKA_DATABASE__POOL_SIZE": str(pool_size),
        "TROSHKA_DATABASE__MAX_OVERFLOW": str(max_overflow),
    }
    for c in dep.spec.template.spec.containers:
        env = list(c.env or [])
        seen = set()
        for e in env:
            if e.name in wanted:
                e.value = wanted[e.name]
                seen.add(e.name)
        from kubernetes import client

        for name, val in wanted.items():
            if name not in seen:
                env.append(client.V1EnvVar(name=name, value=val))
        c.env = env
    apps.patch_namespaced_deployment(dep_name, ns, dep)


def set_db_pool(pool_size: int, max_overflow: int) -> dict:
    """[LIVE-ENV] Set the per-process DB pool on backend+worker (rolling restart).

    Rejected if the new pool would push the current fleet over Postgres
    ``max_connections``. NOTE: this patches Deployment env; an ArgoCD-managed
    cluster will revert it on the next sync unless the pool env is ignored there.
    """
    st = worker_scaling_status()
    if not db_pool_fits(
        st["max_connections"],
        pool_size,
        max_overflow,
        st["backend_replicas"],
        st["worker_replicas"],
    ):
        raise ValueError(
            f"pool {pool_size}+{max_overflow} across "
            f"{st['backend_replicas']} backend + {st['worker_replicas']} worker(s) "
            f"would exceed max_connections={st['max_connections']}"
        )
    apps = _apps_api()
    ns = _namespace()
    for dep_name in (_BACKEND_DEPLOYMENT, _WORKER_DEPLOYMENT):
        _patch_pool_env(apps, ns, dep_name, pool_size, max_overflow)
    return {"pool_size": pool_size, "max_overflow": max_overflow, "restarting": True}
