"""Pure DB-connection-aware worker-scaling math (no k8s)."""


def test_compute_max_workers_basic():
    from app.services.worker_scaling import compute_max_workers

    # 100 max_connections, backend uses 1*(5+10)=15, reserve 10 -> 75 budget,
    # worker uses 2 each -> 37 workers.
    assert compute_max_workers(100, 1, 15, 2, reserve=10) == 37


def test_compute_max_workers_tight_budget():
    from app.services.worker_scaling import compute_max_workers

    # (30 - 10 - 15) / 2 = 2 (floor).
    assert compute_max_workers(30, 1, 15, 2, reserve=10) == 2


def test_compute_max_workers_never_below_one():
    from app.services.worker_scaling import compute_max_workers

    # Budget goes negative -> still allow at least 1 worker.
    assert compute_max_workers(20, 1, 15, 2, reserve=10) == 1


def test_compute_max_workers_zero_worker_pool_is_safe():
    from app.services.worker_scaling import compute_max_workers

    # A worker pool of 0 must not divide-by-zero.
    assert compute_max_workers(100, 1, 15, 0, reserve=10) >= 1


def test_clamp_replicas():
    from app.services.worker_scaling import clamp_replicas

    assert clamp_replicas(5, 3) == 3  # capped at max
    assert clamp_replicas(2, 3) == 2  # within range
    assert clamp_replicas(0, 3) == 1  # never below 1
    assert clamp_replicas(4, 0) == 1  # max<1 -> floor at 1


def test_db_pool_fits_connections():
    from app.services.worker_scaling import db_pool_fits

    # Raising the pool must not exceed max_connections given current fleet.
    # backend 1*(pool+overflow) + workers 3*(pool+overflow) + reserve <= max_conns
    assert db_pool_fits(
        max_connections=100,
        pool_size=5,
        max_overflow=10,
        backend_replicas=1,
        worker_replicas=3,
        reserve=10,
    )  # 1*15 + 3*15 + 10 = 70 <= 100
    assert not db_pool_fits(
        max_connections=100,
        pool_size=20,
        max_overflow=10,
        backend_replicas=1,
        worker_replicas=3,
        reserve=10,
    )  # 4*30 + 10 = 130 > 100
