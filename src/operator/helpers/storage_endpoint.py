"""Validate explicitly configured object-store endpoints without defaults."""

from urllib.parse import urlsplit


def require_s3_endpoint(s3_config):
    endpoint = s3_config.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("S3 requires an explicit HTTP(S) endpoint with a hostname")
    parsed = urlsplit(endpoint)
    # OBC's in-cluster RGW uses HTTP; external central stores use HTTPS.
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("S3 requires an explicit HTTP(S) endpoint with a hostname")
    return endpoint
