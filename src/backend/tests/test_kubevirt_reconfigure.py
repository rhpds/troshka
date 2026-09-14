"""Tests for KubeVirt reconfigure parity helpers."""

from unittest.mock import MagicMock, patch


class TestKubevirtNetworkHelpers:
    def test_build_troshkanetwork_spec_includes_static_leases(self):
        from app.services.kubevirt_reconfigure import (
            _network_entry_from_node,
            build_troshkanetwork_spec,
        )

        topology = {
            "nodes": [
                {
                    "id": "net-lab01",
                    "type": "networkNode",
                    "data": {
                        "id": "net-lab01",
                        "subtype": "network",
                        "cidr": "10.0.0.0/24",
                        "gateway": "10.0.0.1",
                    },
                },
                {
                    "id": "vm-cp",
                    "type": "vmNode",
                    "data": {
                        "nics": [
                            {
                                "id": "nic-1",
                                "mac": "52:54:00:11:22:33",
                                "ip": "10.0.0.10",
                            }
                        ],
                        "name": "cp",
                    },
                },
            ],
            "edges": [
                {
                    "source": "net-lab01",
                    "target": "vm-cp",
                    "targetHandle": "nic-nic-1-left",
                }
            ],
        }
        node = topology["nodes"][0]
        entry = _network_entry_from_node(node, topology)
        assert entry is not None
        spec = build_troshkanetwork_spec(entry, topology)
        assert spec["networkId"] == "net-lab01"
        assert any(l["ip"] == "10.0.0.10" for l in spec["staticLeases"])

    @patch("app.services.kubevirt_reconfigure._wait_kubevirt_networks_ready")
    def test_apply_kubevirt_network_changes_creates_cr(self, mock_wait):
        from app.services.kubevirt_reconfigure import apply_kubevirt_network_changes

        custom_api = MagicMock()
        project_cr = {
            "apiVersion": "troshka.redhat.com/v1alpha1",
            "kind": "TroshkaProject",
            "metadata": {"name": "project-p1", "uid": "uid-1"},
        }
        added = [
            {
                "id": "net-new123",
                "type": "networkNode",
                "data": {
                    "id": "net-new123",
                    "subtype": "network",
                    "cidr": "10.1.0.0/24",
                },
            }
        ]
        errors: list[str] = []
        changed = apply_kubevirt_network_changes(
            custom_api,
            "ns1",
            "p12345678",
            {"nodes": added, "edges": []},
            {},
            {"added_networks": added, "removed_networks": []},
            project_cr,
            errors,
        )
        assert changed is False
        custom_api.create_namespaced_custom_object.assert_called_once()
        assert not errors


class TestKubevirtShowroomReconfigure:
    @patch("app.services.kubevirt_reconfigure.redeploy_showroom_pod_kubevirt")
    @patch("app.services.deploy_service._inject_stored_cluster_kubeconfigs")
    @patch("app.services.deploy_service._recreate_showroom_app_proxy")
    @patch("app.services.deploy_service._kubevirt_prebake_showroom")
    @patch("app.api.projects._prepare_showroom_topology")
    @patch("app.api.projects._showroom_config_changed", return_value=True)
    def test_reconfigure_showroom_kubevirt_runs_pipeline(
        self,
        mock_changed,
        mock_prepare,
        mock_prebake,
        mock_proxy,
        mock_inject,
        mock_redeploy,
    ):
        from app.services.kubevirt_reconfigure import reconfigure_showroom_kubevirt

        current = {
            "nodes": [
                {
                    "id": "showroom-1",
                    "type": "containerNode",
                    "data": {
                        "id": "showroom-1",
                        "name": "showroom",
                        "isShowroom": True,
                        "isPod": True,
                        "podContainers": [],
                    },
                }
            ]
        }
        errors: list[str] = []
        reconfigure_showroom_kubevirt(
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            "p1",
            current,
            {},
            {},
            {"metadata": {"uid": "u1", "name": "project-p1"}},
            errors,
        )
        mock_prepare.assert_called_once()
        mock_redeploy.assert_called_once()
        assert not errors

    @patch("app.services.kubevirt_reconfigure.redeploy_showroom_pod_kubevirt")
    @patch("app.services.kubevirt_reconfigure._fetch_troshka_project_cr")
    @patch("app.services.providers.kubevirt._project_ns", return_value="ns1")
    @patch(
        "app.services.providers.kubevirt._get_k8s_clients",
        return_value=(MagicMock(), MagicMock(), MagicMock()),
    )
    @patch("app.services.deploy_service._sync_deployed_container_node")
    @patch("app.services.deploy_service._inject_stored_cluster_kubeconfigs")
    @patch("app.services.deploy_service._kubevirt_prebake_showroom")
    @patch("app.services.deploy_service._recreate_showroom_app_proxy")
    def test_redeploy_container_kubevirt_bg(
        self,
        mock_proxy,
        mock_prebake,
        mock_inject,
        mock_sync,
        mock_k8s,
        mock_ns,
        mock_fetch_cr,
        mock_redeploy_pod,
    ):
        from app.services.kubevirt_reconfigure import redeploy_container_kubevirt_bg

        host = MagicMock()
        host.provider_id = "prov1"
        host.host_type = "kubevirt-cluster"
        provider = MagicMock()
        provider.type = "kubevirt"
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = provider
        project = MagicMock()
        topo = {
            "nodes": [
                {
                    "id": "showroom-1",
                    "type": "containerNode",
                    "data": {"id": "showroom-1", "isShowroom": True, "isPod": True},
                }
            ]
        }
        with patch(
            "app.services.providers.get_provider_driver", return_value=MagicMock()
        ):
            redeploy_container_kubevirt_bg(
                db, host, project, "p12345678", "showroom-1", topo
            )
        mock_proxy.assert_called_once()
        mock_prebake.assert_called_once()
        mock_redeploy_pod.assert_called_once()
        mock_sync.assert_called_once_with(project, "showroom-1", topo)

    @patch("app.api.projects._showroom_config_changed", return_value=False)
    def test_reconfigure_showroom_skips_when_unchanged(self, mock_changed):
        from app.services.kubevirt_reconfigure import reconfigure_showroom_kubevirt

        with patch(
            "app.services.kubevirt_reconfigure.redeploy_showroom_pod_kubevirt"
        ) as mock_redeploy:
            reconfigure_showroom_kubevirt(
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                "p1",
                {"nodes": []},
                {},
                {},
                {},
                [],
            )
            mock_redeploy.assert_not_called()
