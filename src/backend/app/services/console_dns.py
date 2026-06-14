"""Console DNS management and JWT token signing for direct VNC proxy."""
import base64
import hashlib
import hmac
import json
import logging
import time

import boto3

logger = logging.getLogger(__name__)

JWT_EXPIRY_SECONDS = 300  # 5 minutes


def console_domain_for_host(instance_id: str, base_domain: str) -> str:
    return f"{instance_id}.{base_domain}"


def sign_console_jwt(domain_name: str, host_id: str, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "domain_name": domain_name,
        "host_id": host_id,
        "exp": int(time.time()) + JWT_EXPIRY_SECONDS,
    }
    h = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
    p = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig_input = f"{h}.{p}".encode()
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), sig_input, hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{h}.{p}.{sig}"


def verify_console_jwt(token: str, secret: str) -> dict | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        sig_input = f"{parts[0]}.{parts[1]}".encode()
        expected_sig = base64.urlsafe_b64encode(
            hmac.new(secret.encode(), sig_input, hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        if not hmac.compare_digest(expected_sig, parts[2]):
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def upsert_dns_record(fqdn: str, ip: str, hosted_zone_id: str, credentials: dict | None = None) -> None:
    if not hosted_zone_id:
        logger.warning("No hosted_zone_id provided, skipping DNS")
        return

    creds = credentials or {}
    client = boto3.client(
        "route53",
        aws_access_key_id=creds.get("access_key_id"),
        aws_secret_access_key=creds.get("secret_access_key"),
    )
    client.change_resource_record_sets(
        HostedZoneId=hosted_zone_id,
        ChangeBatch={
            "Changes": [{
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": fqdn,
                    "Type": "A",
                    "TTL": 60,
                    "ResourceRecords": [{"Value": ip}],
                },
            }],
        },
    )
    logger.info("DNS: upserted %s -> %s", fqdn, ip)


def delete_dns_record(fqdn: str, ip: str, hosted_zone_id: str, credentials: dict | None = None) -> None:
    if not hosted_zone_id:
        return

    creds = credentials or {}
    client = boto3.client(
        "route53",
        aws_access_key_id=creds.get("access_key_id"),
        aws_secret_access_key=creds.get("secret_access_key"),
    )
    try:
        client.change_resource_record_sets(
            HostedZoneId=hosted_zone_id,
            ChangeBatch={
                "Changes": [{
                    "Action": "DELETE",
                    "ResourceRecordSet": {
                        "Name": fqdn,
                        "Type": "A",
                        "TTL": 60,
                        "ResourceRecords": [{"Value": ip}],
                    },
                }],
            },
        )
        logger.info("DNS: deleted %s", fqdn)
    except Exception:
        logger.warning("DNS: failed to delete %s (may already be gone)", fqdn)
