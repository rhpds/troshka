"""
S3 storage service for the image library.

Handles upload, download, delete, and presigned URL generation
for ISOs and disk images stored in S3.
"""
import logging

import boto3

from app.core.config import config

logger = logging.getLogger(__name__)


def _get_s3_config() -> dict:
    """Get S3 config from DB provider (type='s3') or fall back to config.yaml."""
    try:
        from app.core.database import SessionLocal
        from app.models.provider import Provider
        s = SessionLocal()
        provider = s.query(Provider).filter_by(type="s3", state="active").first()
        if provider:
            creds = provider.get_credentials()
            result = {
                "region": creds.get("region") or provider.default_region or "us-east-1",
                "access_key_id": creds.get("access_key_id", ""),
                "secret_access_key": creds.get("secret_access_key", ""),
                "bucket": creds.get("bucket", "troshka-images"),
                "endpoint_url": creds.get("endpoint_url", ""),
            }
            s.close()
            return result
        s.close()
    except Exception:
        pass
    try:
        return {
            "region": config.s3.region or "us-east-1",
            "access_key_id": getattr(config.s3, "access_key_id", ""),
            "secret_access_key": getattr(config.s3, "secret_access_key", ""),
            "bucket": config.s3.bucket or "troshka-images",
            "endpoint_url": getattr(config.s3, "endpoint_url", ""),
        }
    except AttributeError:
        raise ValueError("No S3 provider configured. Add an S3 provider in Admin > Providers.")


def _get_s3_client():
    cfg = _get_s3_config()
    kwargs = {"region_name": cfg["region"]}
    if cfg["access_key_id"]:
        kwargs["aws_access_key_id"] = cfg["access_key_id"]
    if cfg["secret_access_key"]:
        kwargs["aws_secret_access_key"] = cfg["secret_access_key"]
    if cfg["endpoint_url"]:
        kwargs["endpoint_url"] = cfg["endpoint_url"]
    return boto3.client("s3", **kwargs)


def _bucket():
    return _get_s3_config()["bucket"]


def upload_file(key: str, file_obj, content_type: str = "application/octet-stream") -> dict:
    """Upload a file to S3."""
    client = _get_s3_client()
    client.upload_fileobj(
        file_obj,
        _bucket(),
        key,
        ExtraArgs={"ContentType": content_type},
    )
    head = client.head_object(Bucket=_bucket(), Key=key)
    logger.info("Uploaded %s (%d bytes)", key, head["ContentLength"])
    return {"key": key, "size_bytes": head["ContentLength"]}


def download_file(key: str, local_path: str):
    """Download a file from S3 to a local path."""
    client = _get_s3_client()
    client.download_file(_bucket(), key, local_path)
    logger.info("Downloaded %s → %s", key, local_path)


def delete_file(key: str):
    """Delete a file from S3."""
    client = _get_s3_client()
    client.delete_object(Bucket=_bucket(), Key=key)
    logger.info("Deleted %s", key)


def generate_presigned_url(key: str, expires: int = 3600) -> str:
    """Generate a presigned download URL for a file in S3."""
    client = _get_s3_client()
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": _bucket(), "Key": key},
        ExpiresIn=expires,
    )
    return url


def generate_presigned_upload_url(key: str, expires: int = 3600) -> str:
    """Generate a presigned upload URL for a file in S3."""
    s3 = _get_s3_client()
    cfg = _get_s3_config()
    return s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": cfg["bucket"], "Key": key},
        ExpiresIn=expires,
    )


def file_exists(key: str) -> bool:
    """Check if a file exists in S3."""
    client = _get_s3_client()
    try:
        client.head_object(Bucket=_bucket(), Key=key)
        return True
    except client.exceptions.ClientError:
        return False
