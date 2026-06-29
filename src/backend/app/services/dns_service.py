import logging
import subprocess

logger = logging.getLogger(__name__)


def resolve_dns_records(
    templates: list[dict],
    guid: str,
    domain: str,
    eip: str | None,
) -> list[dict]:
    records = []
    for tmpl in templates:
        name = tmpl["name"].replace("{guid}", guid).replace("{domain}", domain)
        value = eip if tmpl.get("target") == "eip" else tmpl.get("target")
        records.append(
            {
                "name": name,
                "type": tmpl.get("type", "A"),
                "value": value,
            }
        )
    return records


def create_dns_records(
    provider_type: str,
    provider_config: dict,
    records: list[dict],
    ttl: int = 30,
) -> list[str]:
    errors: list[str] = []
    if provider_type == "nsupdate":
        _nsupdate_create(provider_config, records, ttl, errors)
    elif provider_type == "route53":
        _route53_create(provider_config, records, ttl, errors)
    else:
        errors.append(f"Unknown DNS provider type: {provider_type}")
    return errors


def delete_dns_records(
    provider_type: str,
    provider_config: dict,
    records: list[dict],
) -> list[str]:
    errors: list[str] = []
    if provider_type == "nsupdate":
        _nsupdate_delete(provider_config, records, errors)
    elif provider_type == "route53":
        _route53_delete(provider_config, records, errors)
    else:
        errors.append(f"Unknown DNS provider type: {provider_type}")
    return errors


def _nsupdate_create(config: dict, records: list[dict], ttl: int, errors: list[str]):
    server = config["server"]
    port = config.get("port", 53)
    key_name = config["key_name"]
    key_secret = config["key_secret"]
    algorithm = config.get("key_algorithm", "hmac-sha256")
    zone = config.get("default_zone", "")

    commands = [f"server {server} {port}", f"zone {zone}"]
    for rec in records:
        if not rec.get("value"):
            continue
        commands.append(f"update add {rec['name']}. {ttl} {rec['type']} {rec['value']}")
    commands.append("send")
    commands.append("quit")

    nsupdate_input = "\n".join(commands) + "\n"

    try:
        result = subprocess.run(
            ["nsupdate", "-y", f"{algorithm}:{key_name}:{key_secret}"],
            input=nsupdate_input,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            err = f"nsupdate failed: {result.stderr.strip()}"
            logger.error(err)
            errors.append(err)
        else:
            logger.info("DNS records created: %s", [r["name"] for r in records])
    except Exception as e:
        err = f"nsupdate error: {e}"
        logger.error(err)
        errors.append(err)


def _nsupdate_delete(config: dict, records: list[dict], errors: list[str]):
    server = config["server"]
    port = config.get("port", 53)
    key_name = config["key_name"]
    key_secret = config["key_secret"]
    algorithm = config.get("key_algorithm", "hmac-sha256")
    zone = config.get("default_zone", "")

    commands = [f"server {server} {port}", f"zone {zone}"]
    for rec in records:
        commands.append(f"update delete {rec['name']}. {rec['type']}")
    commands.append("send")
    commands.append("quit")

    nsupdate_input = "\n".join(commands) + "\n"

    try:
        result = subprocess.run(
            ["nsupdate", "-y", f"{algorithm}:{key_name}:{key_secret}"],
            input=nsupdate_input,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            err = f"nsupdate delete failed: {result.stderr.strip()}"
            logger.error(err)
            errors.append(err)
        else:
            logger.info("DNS records deleted: %s", [r["name"] for r in records])
    except Exception as e:
        err = f"nsupdate delete error: {e}"
        logger.error(err)
        errors.append(err)


def _route53_create(config: dict, records: list[dict], ttl: int, errors: list[str]):
    try:
        import boto3

        client = boto3.client(
            "route53",
            aws_access_key_id=config["access_key_id"],
            aws_secret_access_key=config["secret_access_key"],
        )
        changes = []
        for rec in records:
            if not rec.get("value"):
                continue
            changes.append(
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": rec["name"],
                        "Type": rec["type"],
                        "TTL": ttl,
                        "ResourceRecords": [{"Value": rec["value"]}],
                    },
                }
            )
        if changes:
            client.change_resource_record_sets(
                HostedZoneId=config["hosted_zone_id"],
                ChangeBatch={"Changes": changes},
            )
            logger.info("Route53 records created: %s", [r["name"] for r in records])
    except Exception as e:
        err = f"Route53 error: {e}"
        logger.error(err)
        errors.append(err)


def _route53_delete(config: dict, records: list[dict], errors: list[str]):
    try:
        import boto3

        client = boto3.client(
            "route53",
            aws_access_key_id=config["access_key_id"],
            aws_secret_access_key=config["secret_access_key"],
        )
        changes = []
        for rec in records:
            if not rec.get("value"):
                continue
            changes.append(
                {
                    "Action": "DELETE",
                    "ResourceRecordSet": {
                        "Name": rec["name"],
                        "Type": rec["type"],
                        "TTL": 30,
                        "ResourceRecords": [{"Value": rec["value"]}],
                    },
                }
            )
        if changes:
            client.change_resource_record_sets(
                HostedZoneId=config["hosted_zone_id"],
                ChangeBatch={"Changes": changes},
            )
            logger.info("Route53 records deleted: %s", [r["name"] for r in records])
    except Exception as e:
        err = f"Route53 delete error: {e}"
        logger.error(err)
        errors.append(err)
