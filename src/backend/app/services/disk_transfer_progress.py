"""Best-effort byte progress for CDI's S3 download before qemu conversion."""

import base64
import time
from urllib.parse import quote, unquote, urlsplit

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from kubernetes.stream import stream

from app.core.redis import get_progress, set_progress

_PROBE_INTERVAL = 15
_REQUEST_TIMEOUT = 5
_CACHE_TTL = 3600


def _download_source(dv):
    if not isinstance(dv, dict):
        return None
    status = dv.get("status") or {}
    if status.get("phase") != "ImportInProgress":
        return None
    if status.get("progress") not in (None, "", "N/A"):
        return None
    source = (dv.get("spec") or {}).get("source") or {}
    s3 = source.get("s3")
    return s3 if isinstance(s3, dict) and s3.get("url") else None


def _object_location(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Expected an HTTP or HTTPS path-style S3 URL")
    bucket, separator, key = parsed.path.lstrip("/").partition("/")
    if not separator or not bucket or not key:
        raise ValueError("S3 URL requires bucket and object key")
    return f"{parsed.scheme}://{parsed.netloc}", unquote(bucket), unquote(key)


def _object_size(core_api, source, namespace):
    endpoint, bucket, key = _object_location(source["url"])
    credentials = {}
    signature = UNSIGNED
    if source.get("secretRef"):
        secret = core_api.read_namespaced_secret(
            source["secretRef"], namespace, _request_timeout=_REQUEST_TIMEOUT
        )
        data = secret.data or {}
        credentials = {
            "aws_access_key_id": base64.b64decode(data["accessKeyId"]).decode(),
            "aws_secret_access_key": base64.b64decode(data["secretKey"]).decode(),
        }
        signature = "s3v4"
    config = Config(
        signature_version=signature,
        connect_timeout=2,
        read_timeout=3,
        retries={"total_max_attempts": 1},
        s3={"addressing_style": "path"},
    )
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        config=config,
        **credentials,
    )
    try:
        # CDI sources include external/public buckets and non-AWS S3 servers.
        # There is no expected AWS account owner; this optional probe reads only
        # the byte count from the source CDI is already importing.
        total = client.head_object(Bucket=bucket, Key=key).get("ContentLength")
        return total if isinstance(total, int) and total > 0 else None
    finally:
        client.close()


def _find_importer(core_api, dv, namespace):
    claim = (dv.get("status") or {}).get("claimName") or dv["metadata"]["name"]
    pvc = core_api.read_namespaced_persistent_volume_claim(
        claim, namespace, _request_timeout=_REQUEST_TIMEOUT
    )
    if not pvc.metadata.uid:
        return None
    claims = {claim, f"prime-{pvc.metadata.uid}"}
    pods = core_api.list_namespaced_pod(
        namespace,
        label_selector="cdi.kubevirt.io=importer",
        _request_timeout=_REQUEST_TIMEOUT,
    )
    for pod in pods.items:
        if _matches_importer(pod, claims):
            return pod
    return None


def _matches_importer(pod, claims):
    if pod.status.phase != "Running" or not pod.metadata.uid:
        return False
    if not any(container.name == "importer" for container in pod.spec.containers or []):
        return False
    return any(
        volume.persistent_volume_claim is not None
        and volume.persistent_volume_claim.claim_name in claims
        for volume in pod.spec.volumes or []
    )


def _downloaded_bytes(core_api, pod, namespace):
    output = stream(
        core_api.connect_get_namespaced_pod_exec,
        pod.metadata.name,
        namespace,
        container="importer",
        command=["stat", "-c", "%s", "/scratch/tmpimage"],
        stderr=False,
        stdin=False,
        stdout=True,
        tty=False,
        _request_timeout=_REQUEST_TIMEOUT,
    )
    count = int(output.strip())
    return max(0, count)


def _sample_rate(previous, count, importer_uid, now):
    elapsed = now - previous.get("sample_at", now)
    old_count = previous.get("bytes")
    if (
        previous.get("importer_uid") != importer_uid
        or old_count is None
        or count < old_count
        or elapsed <= 0
    ):
        return None
    rate = (count - old_count) / elapsed
    old_rate = previous.get("rate")
    return rate if old_rate is None else (old_rate + rate) / 2


def _format_download(count, total, rate):
    unit, divisor = "B", 1
    for candidate, size in (("GB", 1_000_000_000), ("MB", 1_000_000), ("KB", 1000)):
        if total >= size:
            unit, divisor = candidate, size
            break
    percent = min(100.0, max(0.0, 100 * count / total))
    sizes = (
        f"{count / divisor:.1f} / {total / divisor:.1f}"
        if divisor > 1
        else f"{count} / {total}"
    )
    result = f"downloading {percent:.1f}% — {sizes} {unit}"
    if rate is not None:
        result += f" · {rate / 1_000_000:.1f} MB/s"
    return result


def _probe_download(core_api, dv, source, namespace, previous):
    total = previous.get("total") or _object_size(core_api, source, namespace)
    if not total:
        return previous
    previous["total"] = total
    pod = _find_importer(core_api, dv, namespace)
    if pod is None:
        return previous
    count = _downloaded_bytes(core_api, pod, namespace)
    sampled_at = time.time()
    rate = _sample_rate(previous, count, pod.metadata.uid, sampled_at)
    previous.update(
        bytes=count,
        sample_at=sampled_at,
        importer_uid=pod.metadata.uid,
        rate=rate,
        result=_format_download(count, total, rate),
    )
    return previous


def get_s3_download_progress(core_api, dv, cache_scope: str) -> str | None:
    """Return bounded transfer metrics, or None when unavailable or converting.

    Shared cache keys include provider scope, namespace, and DV UID. All probes,
    including failures, are throttled. No credentials or source URLs are cached.
    """
    try:
        source = _download_source(dv)
        if source is None:
            return None
        metadata = dv.get("metadata") or {}
        namespace, uid = metadata.get("namespace"), metadata.get("uid")
        if not cache_scope or not namespace or not uid:
            return None
        key = "s3-download:" + ":".join(
            quote(str(part), safe="") for part in (cache_scope, namespace, uid)
        )
        previous = dict(get_progress(key) or {})
        now = time.time()
        if 0 <= now - previous.get("probe_at", -_PROBE_INTERVAL) < _PROBE_INTERVAL:
            return previous.get("result")
        previous.update(probe_at=now, result=None)
        set_progress(key, previous, ttl=_CACHE_TTL)
        try:
            previous = _probe_download(core_api, dv, source, namespace, previous)
        finally:
            set_progress(key, previous, ttl=_CACHE_TTL)
        return previous.get("result")
    except Exception:
        # An optional metric must never interrupt deployment or expose secrets.
        return None
