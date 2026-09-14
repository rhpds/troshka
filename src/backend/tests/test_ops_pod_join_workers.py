from app.services.ocp.ops_pod_install import _cluster_install_block


def test_cluster_install_block_includes_cp_usable_and_worker_join():
    workers = [
        {
            "name": "source-worker-0",
            "mac": "52:54:00:aa:bb:02",
            "bmc_ip": "192.168.100.20",
        },
    ]
    script = _cluster_install_block(
        "source",
        ["192.168.100.10"],
        "secret",
        8080,
        "/workdir",
        serving_ip="10.0.0.5",
        deferred_workers=workers,
    )
    assert "[source] control-plane-usable" in script
    assert "oc adm node-image create" in script
    assert "install already complete" in script
    assert ".deferred-workers-joined" in script
