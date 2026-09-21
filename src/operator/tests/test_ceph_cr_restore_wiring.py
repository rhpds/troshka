"""Tests for wiring project-Ceph pattern restore materialization into the
TroshkaProject deploy path (Task 9): ``handlers/project.py``'s
``_materialize_ceph_restore``/``_create_ceph_cr`` must pre-create + await the
mon/OSD restore DataVolumes before the (restore-mode) TroshkaCeph CR is
created, and must leave a fresh-bootstrap deploy (no captured Ceph) untouched.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _base_topology(restore_capture=None):
    topology = {
        "nodes": [
            {
                "id": "net1",
                "type": "networkNode",
                "data": {"id": "net1", "cidr": "10.0.0.0/24"},
            },
            {
                "id": "ceph1",
                "type": "cephClusterNode",
                "data": {"id": "ceph1", "networkRef": "net1", "osdCount": 2},
            },
        ],
        "edges": [],
    }
    if restore_capture is not None:
        topology["projectCephCapture"] = {
            "monDiskId": "d-mon",
            "osdDiskIds": ["d-osd-0", "d-osd-1"],
            "restore": restore_capture,
        }
    return topology


_RESTORE_CAPTURE = {
    "mon": {"s3Path": "patterns/p/ceph-mon-0.tar.gz", "sizeBytes": 1, "source": "central"},
    "osds": [
        {"index": 0, "s3Path": "patterns/p/ceph-osd-0.qcow2", "sizeBytes": 1, "source": "obc"},
        {"index": 1, "s3Path": "patterns/p/ceph-osd-1.qcow2", "sizeBytes": 1, "source": "central"},
    ],
}


class TestMaterializeCephRestore:
    def test_stamps_restore_block_on_ceph_spec(self):
        from handlers.project import _materialize_ceph_restore

        ceph_spec = {"osdStorageClass": "ocs-storage"}
        body = {"spec": {"s3Config": {"bucket": "b"}, "centralS3Config": {"bucket": "c"}}}
        custom_api = MagicMock()

        with (
            patch(
                "helpers.ceph_restore.materialize_ceph_restore_pvcs",
                return_value=("rook-ceph-mon-a", ["osd-restore-data-0", "osd-restore-data-1"]),
            ) as mock_materialize,
            patch(
                "helpers.ceph_restore.wait_for_ceph_restore_datavolumes",
                new_callable=AsyncMock,
            ) as mock_wait,
        ):
            asyncio.run(
                _materialize_ceph_restore(
                    custom_api, "ns1", ceph_spec, _RESTORE_CAPTURE, body
                )
            )

        mock_materialize.assert_called_once_with(
            custom_api, "ns1", _RESTORE_CAPTURE, {"bucket": "b"}, {"bucket": "c"}, "ocs-storage"
        )
        mock_wait.assert_awaited_once_with(
            custom_api,
            "ns1",
            ["rook-ceph-mon-a", "osd-restore-data-0", "osd-restore-data-1"],
        )
        assert ceph_spec["restore"] == {
            "enabled": True,
            "monPvc": "rook-ceph-mon-a",
            "osdPvcs": ["osd-restore-data-0", "osd-restore-data-1"],
        }

    def test_no_restore_key_when_materialize_returns_nothing(self):
        """A stale/emptied capture block (e.g. devices missing s3Path) must not
        stamp spec.restore.enabled=True against zero materialized PVCs."""
        from handlers.project import _materialize_ceph_restore

        ceph_spec = {}
        body = {"spec": {"s3Config": {}}}
        custom_api = MagicMock()

        with (
            patch(
                "helpers.ceph_restore.materialize_ceph_restore_pvcs",
                return_value=("", []),
            ),
            patch(
                "helpers.ceph_restore.wait_for_ceph_restore_datavolumes",
                new_callable=AsyncMock,
            ) as mock_wait,
        ):
            asyncio.run(
                _materialize_ceph_restore(custom_api, "ns1", ceph_spec, {}, body)
            )

        mock_wait.assert_not_awaited()
        assert "restore" not in ceph_spec

    def test_raises_when_wait_fails(self):
        """A failed/timed-out DV import must propagate — callers must not
        proceed to create the restore-mode TroshkaCeph CR."""
        from handlers.project import _materialize_ceph_restore

        ceph_spec = {}
        body = {"spec": {"s3Config": {}}}
        custom_api = MagicMock()

        with (
            patch(
                "helpers.ceph_restore.materialize_ceph_restore_pvcs",
                return_value=("rook-ceph-mon-a", []),
            ),
            patch(
                "helpers.ceph_restore.wait_for_ceph_restore_datavolumes",
                new_callable=AsyncMock,
                side_effect=RuntimeError("ceph-restore DataVolume rook-ceph-mon-a failed to import"),
            ),
        ):
            with pytest.raises(RuntimeError, match="failed to import"):
                asyncio.run(
                    _materialize_ceph_restore(custom_api, "ns1", ceph_spec, {}, body)
                )
        assert "restore" not in ceph_spec


class TestCreateCephCrRestoreWiring:
    def test_fresh_deploy_skips_materialize_and_omits_restore(self):
        """No projectCephCapture.restore in topology -> ordinary fresh-bootstrap
        TroshkaCeph create path, unchanged."""
        from handlers.project import _create_ceph_cr

        custom_api = MagicMock()
        patch_obj = MagicMock()
        patch_obj.status = {}
        body = {"kind": "TroshkaProject", "spec": {"s3Config": {}}, "metadata": {"uid": "u1", "name": "proj1"}}

        with patch("handlers.project._materialize_ceph_restore") as mock_materialize:
            asyncio.run(
                _create_ceph_cr(
                    custom_api, _base_topology(), "ns1", "proj1", body, patch_obj
                )
            )

        mock_materialize.assert_not_called()
        custom_api.create_namespaced_custom_object.assert_called_once()
        created_spec = custom_api.create_namespaced_custom_object.call_args.kwargs["body"]["spec"]
        assert "restore" not in created_spec

    def test_restore_deploy_materializes_before_creating_cr(self):
        """projectCephCapture.restore present -> PVCs must be materialized
        (and awaited) before the TroshkaCeph CR body is submitted, and the
        submitted spec must carry the resulting spec.restore block."""
        from handlers.project import _create_ceph_cr

        custom_api = MagicMock()
        patch_obj = MagicMock()
        patch_obj.status = {}
        body = {"kind": "TroshkaProject", "spec": {"s3Config": {}}, "metadata": {"uid": "u1", "name": "proj1"}}

        call_order = []

        async def fake_materialize(_custom_api, _ns, ceph_spec, _restore_capture, _body):
            call_order.append("materialize")
            ceph_spec["restore"] = {
                "enabled": True,
                "monPvc": "rook-ceph-mon-a",
                "osdPvcs": ["osd-restore-data-0", "osd-restore-data-1"],
            }

        def fake_create(*_args, **kwargs):
            call_order.append("create_cr")
            assert kwargs["body"]["spec"]["restore"]["enabled"] is True

        custom_api.create_namespaced_custom_object.side_effect = fake_create

        with patch(
            "handlers.project._materialize_ceph_restore", side_effect=fake_materialize
        ):
            asyncio.run(
                _create_ceph_cr(
                    custom_api,
                    _base_topology(_RESTORE_CAPTURE),
                    "ns1",
                    "proj1",
                    body,
                    patch_obj,
                )
            )

        assert call_order == ["materialize", "create_cr"]
