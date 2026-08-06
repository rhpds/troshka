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
        raise ValueError(
            "No S3 provider configured. Add an S3 provider in Admin > Providers."
        )


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


_cached_account_ids: dict[str, str] = {}


def _get_account_id(s3_config: dict) -> str:
    """Resolve AWS account ID via STS. Skipped for non-AWS endpoints (S4/MinIO)."""
    if s3_config.get("endpoint_url"):
        return ""
    cache_key = s3_config.get("access_key_id", "_default")
    if cache_key in _cached_account_ids:
        return _cached_account_ids[cache_key]
    try:
        sts = boto3.client(
            "sts",
            region_name=s3_config.get("region", "us-east-1"),
            aws_access_key_id=s3_config.get("access_key_id") or None,
            aws_secret_access_key=s3_config.get("secret_access_key") or None,
        )
        account_id = sts.get_caller_identity()["Account"]
        _cached_account_ids[cache_key] = account_id
        return account_id
    except Exception:
        return ""


def owner_params(s3_config: dict) -> dict:
    """Return ``{'ExpectedBucketOwner': account_id}`` for AWS S3, empty dict otherwise."""
    acct = _get_account_id(s3_config)
    return {"ExpectedBucketOwner": acct} if acct else {}


def upload_file(
    key: str, file_obj, content_type: str = "application/octet-stream"
) -> dict:
    """Upload a file to S3."""
    cfg = _get_s3_config()
    client = _get_s3_client()
    op = owner_params(cfg)
    bucket = cfg["bucket"]
    client.upload_fileobj(
        file_obj,
        bucket,
        key,
        ExtraArgs={"ContentType": content_type, **op},
    )
    head = client.head_object(Bucket=bucket, Key=key, **op)
    logger.info("Uploaded %s (%d bytes)", key, head["ContentLength"])
    return {"key": key, "size_bytes": head["ContentLength"]}


def download_file(key: str, local_path: str):
    """Download a file from S3 to a local path."""
    cfg = _get_s3_config()
    client = _get_s3_client()
    op = owner_params(cfg)
    client.download_file(cfg["bucket"], key, local_path, ExtraArgs=op or None)
    logger.info("Downloaded %s → %s", key, local_path)


def delete_file(key: str):
    """Delete a file from S3."""
    cfg = _get_s3_config()
    client = _get_s3_client()
    client.delete_object(Bucket=cfg["bucket"], Key=key, **owner_params(cfg))
    logger.info("Deleted %s", key)


def delete_prefix(prefix: str):
    """Delete all objects under an S3 prefix."""
    cfg = _get_s3_config()
    client = _get_s3_client()
    bucket = cfg["bucket"]
    op = owner_params(cfg)
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, **op):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if objects:
            client.delete_objects(Bucket=bucket, Delete={"Objects": objects}, **op)
            logger.info("Deleted %d objects under %s", len(objects), prefix)  # NOSONAR


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


def _get_readonly_s3_config() -> dict | None:
    """Get read-only central S3 config from DB provider (type='s3_readonly')."""
    try:
        from app.core.database import SessionLocal
        from app.models.provider import Provider

        s = SessionLocal()
        provider = (
            s.query(Provider).filter_by(type="s3_readonly", state="active").first()
        )
        if provider:
            creds = provider.get_credentials()
            result = {
                "provider_id": provider.id,
                "region": creds.get("region") or provider.default_region or "us-east-1",
                "access_key_id": creds.get("access_key_id", ""),
                "secret_access_key": creds.get("secret_access_key", ""),
                "bucket": creds.get("bucket", "troshka-gold-images"),
                "endpoint_url": creds.get("endpoint_url", ""),
            }
            s.close()
            return result
        s.close()
    except Exception:
        pass
    return None


def _get_readonly_s3_client():
    cfg = _get_readonly_s3_config()
    if not cfg:
        return None
    kwargs = {"region_name": cfg["region"]}
    if cfg["access_key_id"]:
        kwargs["aws_access_key_id"] = cfg["access_key_id"]
    if cfg["secret_access_key"]:
        kwargs["aws_secret_access_key"] = cfg["secret_access_key"]
    if cfg["endpoint_url"]:
        kwargs["endpoint_url"] = cfg["endpoint_url"]
    return boto3.client("s3", **kwargs)


def generate_presigned_url_for_config(cfg: dict, key: str, expires: int = 3600) -> str:
    """Generate a presigned download URL using a specific S3 config."""
    kwargs = {"region_name": cfg["region"]}
    if cfg["access_key_id"]:
        kwargs["aws_access_key_id"] = cfg["access_key_id"]
    if cfg["secret_access_key"]:
        kwargs["aws_secret_access_key"] = cfg["secret_access_key"]
    if cfg["endpoint_url"]:
        kwargs["endpoint_url"] = cfg["endpoint_url"]
    client = boto3.client("s3", **kwargs)
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": cfg["bucket"], "Key": key},
        ExpiresIn=expires,
    )
