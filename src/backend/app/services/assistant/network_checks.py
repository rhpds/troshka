import ipaddress
import socket

from fastapi import HTTPException

from app.models.elastic_ip import ElasticIp
from app.models.host import Host
from app.models.project import Project
from app.models.vm import VM
from app.services.assistant.contracts import Evidence
from app.services.assistant.projection import address, identifier


def project_vm(db, ctx, vm_id):
    vm = db.get(VM, str(vm_id))
    if not vm or vm.project_id != ctx.project_id:
        raise HTTPException(404, "VM not found")
    project = db.get(Project, ctx.project_id)
    host = (
        db.get(Host, vm.host_id or project.host_id)
        if (vm.host_id or project.host_id)
        else None
    )
    return project, vm, host


def port(value):
    try:
        value = int(value)
        return value if 1 <= value <= 65535 else None
    except (ValueError, TypeError):
        return None


def network(db, ctx, args):
    project, vm, host = project_vm(db, ctx, args.vm_id)
    forwards = []
    for node in (project.topology or {}).get("nodes", []):
        for rule in node.get("data", {}).get("portForwards", [])[:40]:
            forwards.append(
                {
                    "external_port": port(rule.get("extPort")),
                    "target_ip": address(rule.get("intIp")),
                    "target_port": port(rule.get("intPort")),
                    "protocol": "udp" if rule.get("proto") == "udp" else "tcp",
                }
            )
    facts = {
        "vm_id": vm.id,
        "recorded_state": identifier(vm.state),
        "recorded_ip": address(vm.ip_address),
        "forwards": forwards[:40],
        "requested_source": args.source,
        "probe_performed": False,
    }
    evidence = Evidence(
        check="inspect_network_path",
        project_id=ctx.project_id,
        facts=facts,
        truncated=len(forwards) > 40,
        limitations=[
            "Configured forwarding is not proof of reachability.",
            "No arbitrary endpoint probes are allowed. Guest/ops/gateway reachability is unknown.",
        ],
    )
    if host and host.host_type == "kubevirt-cluster":
        from app.services.assistant.kubevirt_checks import vm_state

        evidence.facts["live_vm"] = vm_state(db, project, host, vm.id)
        evidence.source = "persisted configuration and hosting KubeVirt API"
    if args.endpoint_id:
        evidence.facts["probe"] = probe_endpoint(db, project, vm, args)
        evidence.facts["probe_performed"] = evidence.facts["probe"].get(
            "performed", False
        )
    return evidence


def _matching_forward(project, endpoint, vm, external_port):
    for node in (project.topology or {}).get("nodes", []):
        for rule in node.get("data", {}).get("portForwards", []):
            if (
                rule.get("extIpId") == endpoint.canvas_eip_id
                and port(rule.get("extPort")) == external_port
                and address(rule.get("intIp")) == address(vm.ip_address)
                and address(vm.ip_address) is not None
                and rule.get("proto", "tcp") == "tcp"
            ):
                return True
    return False


def probe_endpoint(db, project, vm, args):
    endpoint = db.get(ElasticIp, str(args.endpoint_id))
    if not endpoint or endpoint.project_id != project.id:
        raise HTTPException(404, "Endpoint not found")
    if not _matching_forward(project, endpoint, vm, args.external_port):
        raise HTTPException(403, "No matching project TCP forwarding rule")
    if args.source != "backend":
        return {"performed": False, "status": "unsupported", "source": args.source}
    ip = public_probe_address(endpoint.public_ip)
    db.commit()
    try:
        # Literal, allocated public IP: no DNS, HTTP redirects, URLs or credentials.
        with socket.create_connection((ip, args.external_port), timeout=3):
            result = "tcp_connected"
    except TimeoutError:
        result = "timeout"
    except OSError:
        result = "connection_failed"
    return {
        "performed": True,
        "source": "Troshka backend",
        "address": ip,
        "port": args.external_port,
        "result": result,
        "limitation": "TCP reachability is not authentication or laptop/guest reachability.",
    }


def public_probe_address(value):
    """Reserved for configured endpoint probes; never accept model-supplied URLs."""
    ip = ipaddress.ip_address(value)
    if not ip.is_global or ip.is_multicast:
        raise ValueError("Endpoint is not an approved public address")
    return str(ip)
