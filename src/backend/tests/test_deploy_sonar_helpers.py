"""Coverage for deploy helpers extracted for Sonar S3776."""

from unittest.mock import MagicMock, patch

from app.services.deploy_service import (
    _all_ops_clusters_error,
    _iso_or_none,
    _maybe_stamp_showroom_url_from_route,
    _ops_pod_resume_should_abort,
    _run_container_redeploy,
    _stamp_cluster_monitoring_fields,
    _stamp_fresh_install_clusters_monitoring,
    _stamp_fresh_install_on_topology,
)
from app.services.ocp.agent_template import (
    _apply_cluster_install_options,
    _maybe_bake_bastion_install,
)
from app.services.showroom_scaffold import SHOWROOM_URL_KEY


def test_stamp_cluster_monitoring_fields():
    cluster = {"ocpInstallStatus": "error", "ocpInstallElapsed": 9}
    assert _stamp_cluster_monitoring_fields(cluster, 111) is True
    assert cluster["ocpInstallStatus"] == "monitoring"
    assert cluster["ocpInstallStartedAt"] == 111
    assert "ocpInstallElapsed" not in cluster
    assert _stamp_cluster_monitoring_fields(cluster, 222) is False
    assert cluster["ocpInstallStartedAt"] == 111


def test_stamp_fresh_install_on_topology_filters_keys():
    topo = {
        "clusters": [
            {"id": "keep", "ocpInstallStatus": "error"},
            {"id": "skip", "ocpInstallStatus": "error"},
        ]
    }
    assert _stamp_fresh_install_on_topology(topo, {"keep"}, 5) is True
    assert topo["clusters"][0]["ocpInstallStatus"] == "monitoring"
    assert topo["clusters"][1]["ocpInstallStatus"] == "error"


def test_stamp_fresh_install_clusters_monitoring_flags_attrs():
    topo = {"clusters": [{"id": "c1", "ocpInstallStatus": "error"}]}
    project = MagicMock()
    project.topology = topo
    project.deployed_topology = topo
    with (
        patch(
            "app.services.deploy_service._partition_ops_pod_clusters",
            return_value=([], [{"id": "c1"}]),
        ),
        patch("app.services.deploy_service._time.time", return_value=99),
    ):
        assert _stamp_fresh_install_clusters_monitoring(project, [{"id": "c1"}]) is True
    assert topo["clusters"][0]["ocpInstallStatus"] == "monitoring"


def test_all_ops_clusters_error():
    assert _all_ops_clusters_error({"clusters": [{"ocpInstallStatus": "error"}]})
    assert not _all_ops_clusters_error({"clusters": [{"ocpInstallStatus": "ready"}]})
    assert not _all_ops_clusters_error({"clusters": [{}]})


def test_ops_pod_resume_abort_and_recover():
    db = MagicMock()
    p = MagicMock()
    p.ocp_status = "monitoring"
    p.id = "12345678-aaaa"
    assert _ops_pod_resume_should_abort(db, p, [], True, False, False) is True
    assert p.ocp_status == "error"

    p.ocp_status = "error"
    assert _ops_pod_resume_should_abort(db, p, [], True, False, True) is True

    p.ocp_status = "error"
    with patch(
        "app.services.deploy_service._stamp_fresh_install_clusters_monitoring"
    ) as stamp:
        assert (
            _ops_pod_resume_should_abort(db, p, [{"id": "c"}], True, True, False)
            is False
        )
        stamp.assert_called_once()
    assert p.ocp_status == "monitoring"

    p.ocp_status = "monitoring"
    assert _ops_pod_resume_should_abort(db, p, [], False, True, False) is False


def test_run_container_redeploy_troshkad_and_kubevirt():
    db = MagicMock()
    host = MagicMock()
    host.host_type = "baremetal"
    project = MagicMock()
    ctr = {"name": "sr"}
    with (
        patch("app.services.deploy_service.set_progress"),
        patch("app.services.deploy_service._redeploy_container_troshkad") as troshkad,
        patch("app.services.deploy_service.notify_project"),
    ):
        _run_container_redeploy(db, host, project, "p" * 8, "c" * 8, {}, ctr)
    troshkad.assert_called_once()
    db.commit.assert_called()

    host.host_type = "kubevirt-cluster"
    kv = MagicMock()
    with (
        patch("app.services.deploy_service.set_progress"),
        patch(
            "app.services.kubevirt_reconfigure.redeploy_container_kubevirt_bg",
            kv,
        ),
        patch("app.services.deploy_service.notify_project"),
    ):
        _run_container_redeploy(db, host, project, "p" * 8, "c" * 8, {}, ctr)
    kv.assert_called_once()


def test_maybe_stamp_showroom_url_from_route():
    project = MagicMock()
    project.deployed_topology = {SHOWROOM_URL_KEY: "https://x"}
    with (
        patch(
            "app.services.deploy_service._showroom_public_base_url",
            return_value="https://route",
        ),
        patch("app.services.deploy_service._stamp_showroom_access_url") as stamp,
    ):
        _maybe_stamp_showroom_url_from_route(project, {})
        stamp.assert_called_once()
        project.deployed_topology[SHOWROOM_URL_KEY] = "https://x?token=abc"
        stamp.reset_mock()
        _maybe_stamp_showroom_url_from_route(project, {})
        stamp.assert_not_called()


def test_iso_or_none():
    ts = MagicMock()
    ts.isoformat.return_value = "iso"
    assert _iso_or_none(ts) == "iso"
    assert _iso_or_none(None) is None


def test_apply_cluster_install_options():
    clusters = [{}]
    with patch(
        "app.services.ocp.client_mirror.normalize_distribution",
        return_value="okd-scos",
    ):
        _apply_cluster_install_options(
            clusters,
            {
                "auto_install_ocp": False,
                "ocp_version": "4.19",
                "distribution": "okd-scos",
            },
        )
    assert clusters[0]["installOnDeploy"] is False
    assert clusters[0]["ocpVersion"] == "4.19"
    assert clusters[0]["ocpDistribution"] == "okd-scos"


def test_maybe_bake_bastion_install_skips_pod():
    with patch("app.services.ocp.agent_template._bake_single_cluster_bastion") as bake:
        _maybe_bake_bastion_install({}, {}, "tid", "pod", ("1", "2"))
        bake.assert_not_called()


def test_apply_redeploy_project_state_recert_vs_rebuild():
    from app.api.projects import _apply_redeploy_project_state

    p = MagicMock()
    _apply_redeploy_project_state(p, "host-1", "recert")
    assert p.ocp_status == "monitoring"
    _apply_redeploy_project_state(p, "host-1", "rebuild")
    assert p.vni_map is None
    assert p.ocp_status is None


def test_emit_deploy_complete_notifications():
    from app.services.deploy_service import _emit_deploy_complete_notifications

    project = MagicMock()
    project.auto_stop_expires_at = None
    project.lifetime_expires_at = None
    with patch("app.services.deploy_service.notify_project") as notify:
        _emit_deploy_complete_notifications(
            "pid", project, [{"node_id": "vm1"}], ["1.2.3.4"]
        )
    assert notify.call_count == 3
