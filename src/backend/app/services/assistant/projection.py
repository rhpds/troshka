"""Positive projections, never generic ORM/Kubernetes serialization."""

import ipaddress
import re

from pydantic import Field

from app.services.assistant.contracts import StrictModel

_SECRET = re.compile(
    r"(?i)(?:bearer\s+\S+|(?:password|passwd|token|secret|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+|(?:sk-|trk_|sha256~)[A-Za-z0-9_~.-]+)"
)
_PEM = re.compile(
    r"-----BEGIN[^-]*PRIVATE KEY-----.*?-----END[^-]*PRIVATE KEY-----", re.S
)


def safe_text(value, limit=2000):
    text = _PEM.sub("[credential removed]", str(value or ""))
    text = _SECRET.sub("[credential removed]", text)
    text = re.sub(r"https?://[^\s<>]+", "[URL omitted]", text)
    return "".join(c for c in text if c in "\n\t" or ord(c) >= 32)[:limit]


def label(value):
    return safe_text(value, 80)


def address(value):
    try:
        return str(ipaddress.ip_interface(str(value)))
    except ValueError:
        return None


def identifier(value):
    value = str(value or "")
    return value if re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value) else "invalid"


class SafeProjectSummary(StrictModel):
    project_id: str
    infrastructure_state: str
    installation_state: str
    auto_stop_minutes: int | None
    auto_delete_minutes: int | None
    nodes: list[dict] = Field(default_factory=list)
    connections: list[dict] = Field(default_factory=list)
    truncated: bool = False


def project_summary(project):
    nodes = (project.topology or {}).get("nodes", [])
    return SafeProjectSummary(
        project_id=project.id,
        infrastructure_state=identifier(project.state),
        installation_state=identifier(project.ocp_status or "unknown"),
        auto_stop_minutes=project.auto_stop_minutes,
        auto_delete_minutes=project.auto_delete_minutes,
        nodes=[safe_node(n) for n in nodes[:40]],
        connections=[
            {
                "source": identifier(e.get("source")),
                "target": identifier(e.get("target")),
                "source_handle": identifier(e.get("sourceHandle")),
                "target_handle": identifier(e.get("targetHandle")),
            }
            for e in (project.topology or {}).get("edges", [])[:60]
        ],
        truncated=len(nodes) > 40,
    )


def safe_node(node):
    data = node.get("data", {})
    return {
        "id": identifier(node.get("id")),
        "type": identifier(node.get("type")),
        "cidr": address(data.get("cidr")),
        "bmc_enabled": data.get("bmcEnabled") is True,
        "bmc_ip": address(data.get("bmcIp")),
        "nics": [
            {
                "id": identifier(n.get("id")),
                "configured_ip": address(n.get("ip")),
                "mac": n.get("mac")
                if re.fullmatch(
                    r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", str(n.get("mac", ""))
                )
                else None,
            }
            for n in data.get("nics", [])[:10]
        ],
    }


# Logs remain in the existing authorized viewer. Only known categories leave it.
_FAILURES = (
    ("pull_secret_missing", ("pullsecret", "unexpected end of json")),
    ("nic_missing_network", ("missing networkref",)),
    ("image_pull_failed", ("imagepullbackoff",)),
    ("permission_denied", ("forbidden",)),
    ("ownership_unverified", ("ownership",)),
    ("origin_unresolved", ("root uid",)),
    ("storage_observation_incomplete", ("collection", "unavailable")),
    ("api_unreachable", ("api server", "not reachable")),
    ("timeout", ("timed out",)),
)


def failure_categories(text):
    lower = str(text or "").lower()
    matches = [
        (lower.find(terms[0]), code)
        for code, terms in _FAILURES
        if all(term in lower for term in terms)
    ]
    return [code for _, code in sorted(matches)]
