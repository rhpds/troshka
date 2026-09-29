"""Unit tests for cluster_storage helpers (Sonar new-code coverage)."""

from unittest.mock import MagicMock, patch

from app.services.cluster_storage import (
    _delete_orphan_pattern_objects,
    _get_client_and_bucket,
    _list_orphan_pattern_ids,
    clean_orphans,
    delete_pattern,
)


class TestGetClientAndBucket:
    @patch("app.services.s3_storage.get_cluster_s3_config", return_value=None)
    def test_returns_none_without_config(self, _cfg):
        assert _get_client_and_bucket(MagicMock(), "prov-12345678") is None

    @patch("app.services.cluster_storage.boto3.client")
    @patch("app.services.s3_storage.get_cluster_s3_config")
    def test_builds_client_with_endpoint(self, mock_cfg, mock_boto):
        mock_cfg.return_value = {
            "region": "us-east-1",
            "access_key_id": "AK",
            "secret_access_key": "SK",
            "endpoint": "https://rgw.example",
            "bucket": "troshka",
        }
        client = MagicMock()
        mock_boto.return_value = client
        result = _get_client_and_bucket(MagicMock(), "prov-12345678")
        assert result == (client, "troshka")
        kwargs = mock_boto.call_args[1]
        assert kwargs["endpoint_url"] == "https://rgw.example"
        assert kwargs["verify"] is False
        assert kwargs["aws_access_key_id"] == "AK"


class TestListOrphanPatternIds:
    def test_collects_orphans(self):
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = [
            {
                "CommonPrefixes": [
                    {"Prefix": "patterns/keep-me/"},
                    {"Prefix": "patterns/orphan-1/"},
                ]
            }
        ]
        orphans = _list_orphan_pattern_ids(client, "b", {"keep-me"})
        assert orphans == ["orphan-1"]

    def test_returns_none_on_error(self):
        client = MagicMock()
        client.get_paginator.side_effect = RuntimeError("boom")
        assert _list_orphan_pattern_ids(client, "b", set()) is None


class TestDeleteOrphanPatternObjects:
    def test_dry_run_skips_delete(self):
        client = MagicMock()
        deleted, deleted_bytes = _delete_orphan_pattern_objects(
            client, "b", "pat-12345678", dry_run=True
        )
        assert deleted == 0
        assert deleted_bytes == 0
        client.delete_objects.assert_not_called()

    def test_deletes_objects(self):
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "patterns/p1/a", "Size": 100},
                    {"Key": "patterns/p1/b", "Size": 50},
                ]
            }
        ]
        deleted, deleted_bytes = _delete_orphan_pattern_objects(
            client, "b", "p1", dry_run=False
        )
        assert deleted == 2
        assert deleted_bytes == 150
        client.delete_objects.assert_called_once()

    def test_swallows_errors(self):
        client = MagicMock()
        client.get_paginator.side_effect = RuntimeError("boom")
        deleted, deleted_bytes = _delete_orphan_pattern_objects(
            client, "b", "pat-12345678", dry_run=False
        )
        assert deleted == 0
        assert deleted_bytes == 0


class TestDeletePattern:
    @patch("app.services.cluster_storage._get_client_and_bucket", return_value=None)
    def test_no_config(self, _get):
        assert delete_pattern(MagicMock(), "prov-12345678", "pat-1") == 0

    @patch("app.services.cluster_storage._get_client_and_bucket")
    def test_deletes_prefix(self, mock_get):
        client = MagicMock()
        mock_get.return_value = (client, "bucket")
        client.get_paginator.return_value.paginate.return_value = [
            {"Contents": [{"Key": "patterns/pat-1/x"}]}
        ]
        assert delete_pattern(MagicMock(), "prov-12345678", "pat-1") == 1
        client.delete_objects.assert_called_once()

    @patch("app.services.cluster_storage._get_client_and_bucket")
    def test_error_returns_partial(self, mock_get):
        client = MagicMock()
        mock_get.return_value = (client, "bucket")
        client.get_paginator.side_effect = RuntimeError("boom")
        assert delete_pattern(MagicMock(), "prov-12345678", "pat-1") == 0


class TestCleanOrphans:
    @patch("app.services.cluster_storage._get_client_and_bucket", return_value=None)
    def test_no_config_error(self, _get):
        report = clean_orphans(MagicMock(), "prov-12345678")
        assert "error" in report

    @patch("app.services.cluster_storage._delete_orphan_pattern_objects")
    @patch("app.services.cluster_storage._list_orphan_pattern_ids")
    @patch("app.services.cluster_storage._get_client_and_bucket")
    def test_happy_path(self, mock_get, mock_list, mock_del):
        client = MagicMock()
        mock_get.return_value = (client, "bucket")
        mock_list.return_value = ["orphan-aaaaaaaa"]
        mock_del.return_value = (3, 5 * 1024**3)
        db = MagicMock()
        db.query.return_value.all.return_value = [MagicMock(id="keep")]
        report = clean_orphans(db, "prov-12345678", dry_run=False)
        assert report["orphan_patterns"] == 1
        assert report["deleted"] == 3
        assert report["deleted_gb"] == 5.0

    @patch("app.services.cluster_storage._list_orphan_pattern_ids", return_value=None)
    @patch("app.services.cluster_storage._get_client_and_bucket")
    def test_list_failure(self, mock_get, _list):
        mock_get.return_value = (MagicMock(), "bucket")
        db = MagicMock()
        db.query.return_value.all.return_value = []
        assert "error" in clean_orphans(db, "prov-12345678")

    @patch("app.services.cluster_storage._delete_orphan_pattern_objects")
    @patch("app.services.cluster_storage._list_orphan_pattern_ids", return_value=["o1"])
    @patch("app.services.cluster_storage._get_client_and_bucket")
    def test_dry_run_flag(self, mock_get, _list, mock_del):
        mock_get.return_value = (MagicMock(), "bucket")
        mock_del.return_value = (0, 0)
        db = MagicMock()
        db.query.return_value.all.return_value = []
        report = clean_orphans(db, "prov-12345678", dry_run=True)
        assert report["dry_run"] is True
        mock_del.assert_called_once()
        assert mock_del.call_args[0][3] is True
