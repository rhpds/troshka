"""Verify S3 endpoint_url flows through to troshkad job params."""

from unittest.mock import MagicMock, patch


def test_s3_config_includes_endpoint_url():
    """_get_s3_config returns endpoint_url from config."""
    # Mock config to return endpoint_url
    mock_config = MagicMock()
    mock_config.s3.region = "us-east-1"
    mock_config.s3.access_key_id = "minioadmin"
    mock_config.s3.secret_access_key = "minioadmin"
    mock_config.s3.bucket = "troshka-images"
    mock_config.s3.endpoint_url = "http://troshka-minio:9000"

    # Patch SessionLocal at its import location inside _get_s3_config
    with patch("app.core.database.SessionLocal") as mock_sl:
        # Make the query return no provider to force config fallback
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None
        mock_sl.return_value = mock_session

        with patch("app.services.s3_storage.config", mock_config):
            from app.services.s3_storage import _get_s3_config

            cfg = _get_s3_config()
            assert cfg["endpoint_url"] == "http://troshka-minio:9000"
