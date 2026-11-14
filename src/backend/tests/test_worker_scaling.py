"""Pure DB-connection-aware worker-scaling math (no k8s)."""


def test_compute_max_workers_basic():
    from app.services.worker_scaling import compute_max_workers

    # 100 max_connections, backend uses 1*(5+10)=15, reserve 10 -> 75 budget,
    # worker uses 2 each -> 37 workers.
    assert compute_max_workers(100, 1, 15, 2, reserve=10) == 37


def test_collection_connections_reserved_from_lifecycle_budget():
    from app.services.worker_scaling import compute_max_workers

    assert compute_max_workers(100, 1, 15, 2, collection_connections=6) == 34


def test_collection_capacity_absence_only_for_404():
    from unittest.mock import MagicMock

    import pytest
    from kubernetes.client.exceptions import ApiException

    from app.services.worker_scaling import _collection_capacity

    apps = MagicMock()
    apps.read_namespaced_deployment.side_effect = ApiException(status=404)
    assert _collection_capacity(apps, "namespace") == (0, 0)
    apps.read_namespaced_deployment.side_effect = ApiException(status=403)
    with pytest.raises(ApiException):
        _collection_capacity(apps, "namespace")


def test_pool_resize_keeps_readiness_connections_reserved(monkeypatch):
    import pytest

    from app.services import worker_scaling

    status = dict(
        max_connections=100,
        backend_replicas=1,
        worker_replicas=2,
        collection_worker_replicas=1,
        collection_worker_conns_per_proc=2,
        readiness_worker_replicas=1,
        readiness_worker_conns_per_proc=2,
    )
    monkeypatch.setattr(worker_scaling, "worker_scaling_status", lambda: status)
    with pytest.raises(ValueError, match="would exceed"):
        worker_scaling.set_db_pool(
            29, 0
        )  # 3*29 + 10 reserve + 2 collection + 2 readiness = 101


def test_worker_status_reads_both_reserved_profiles_without_writing(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock, call

    from app.services import worker_scaling

    def deployment(replicas, pool, overflow=0):
        env = [
            SimpleNamespace(name="TROSHKA_DATABASE__POOL_SIZE", value=str(pool)),
            SimpleNamespace(name="TROSHKA_DATABASE__MAX_OVERFLOW", value=str(overflow)),
        ]
        return SimpleNamespace(
            spec=SimpleNamespace(
                replicas=replicas,
                template=SimpleNamespace(
                    spec=SimpleNamespace(containers=[SimpleNamespace(env=env)])
                ),
            )
        )

    deployments = {
        "troshka-worker": deployment(2, 2),
        "troshka-backend": deployment(1, 5, 10),
        "troshka-collection-worker": deployment(1, 2),
        "troshka-readiness-worker": deployment(1, 2),
    }
    apps = MagicMock()
    apps.read_namespaced_deployment.side_effect = lambda name, namespace: deployments[
        name
    ]
    monkeypatch.setattr(worker_scaling, "_apps_api", lambda: apps)
    monkeypatch.setattr(worker_scaling, "_namespace", lambda: "troshka-dev")
    monkeypatch.setattr(worker_scaling, "get_max_connections", lambda: 100)
    status = worker_scaling.worker_scaling_status()
    assert status["max_workers"] == 35  # (100 - 10 - 15 - 2 - 2) // 2
    assert (
        status["collection_worker_replicas"] == status["readiness_worker_replicas"] == 1
    )
    assert apps.read_namespaced_deployment.call_args_list == [
        call(name, "troshka-dev") for name in deployments
    ]
    apps.patch_namespaced_deployment.assert_not_called()


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
