"""Tests for s3_creds_for_host (EKS host_endpoint_url)."""

from unittest.mock import patch

from app.services.s3_storage import s3_creds_for_host


def test_s3_creds_for_host_uses_host_endpoint():
    base = {
        "region": "us-east-1",
        "access_key_id": "ak",
        "secret_access_key": "sk",
        "bucket": "troshka-images",
        "endpoint_url": "http://troshka-s4.troshka.svc:7480",
    }

    class _S3:
        host_endpoint_url = "https://s4.example.sslip.io"
        endpoint_url = "http://troshka-s4.troshka.svc:7480"

    with patch("app.services.s3_storage.config") as cfg:
        cfg.s3 = _S3()
        out = s3_creds_for_host(base)
    assert out["endpoint_url"] == "https://s4.example.sslip.io"
    assert base["endpoint_url"].endswith(".svc:7480")  # input unchanged


def test_s3_creds_for_host_keeps_endpoint_without_host_url():
    base = {
        "region": "us-east-1",
        "access_key_id": "ak",
        "secret_access_key": "sk",
        "bucket": "troshka-images",
        "endpoint_url": "http://troshka-s4.troshka.svc:7480",
    }

    class _S3:
        host_endpoint_url = ""
        endpoint_url = "http://troshka-s4.troshka.svc:7480"

    with patch("app.services.s3_storage.config") as cfg:
        cfg.s3 = _S3()
        out = s3_creds_for_host(base)
    assert out["endpoint_url"] == "http://troshka-s4.troshka.svc:7480"
