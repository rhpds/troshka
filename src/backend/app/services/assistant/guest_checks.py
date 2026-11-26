"""Fixed guest-agent diagnostics only: no SSH/console fallback or repair."""

import json

from app.services.assistant.contracts import Evidence
from app.services.assistant.network_checks import project_vm
from app.services.assistant.projection import address

COMMANDS = {
    "addresses": "ip -j address show",
    "routes": "ip -j route show",
    "listeners": "ss -H -lnt",
    "service_status": "systemctl is-active sshd",
}


def parse_output(check, output):
    if len(output.encode()) > 16384:
        raise ValueError("Guest result too large")
    if check == "service_status":
        state = output.strip()
        return {
            "sshd": state
            if state in ("active", "inactive", "failed", "unknown")
            else "unknown"
        }
    if check == "listeners":
        ports = set()
        for line in output.splitlines()[:100]:
            parts = line.split()
            if len(parts) > 3 and parts[3].rsplit(":", 1)[-1].isdigit():
                ports.add(int(parts[3].rsplit(":", 1)[-1]))
        return {"tcp_listen_ports": sorted(p for p in ports if 0 < p < 65536)}
    rows = json.loads(output)
    if not isinstance(rows, list):
        raise ValueError("Unrecognized guest output")
    if check == "routes":
        return {
            "routes": [
                {
                    "destination": "default"
                    if r.get("dst") == "default"
                    else address(r.get("dst")),
                    "gateway": address(r.get("gateway")),
                }
                for r in rows[:30]
            ]
        }
    return {
        "addresses": [
            address(a.get("local"))
            for r in rows[:20]
            for a in r.get("addr_info", [])[:10]
            if address(a.get("local"))
        ]
    }


def guest(db, ctx, args):
    project, vm, host = project_vm(db, ctx, args.vm_id)
    evidence = Evidence(
        check="inspect_guest",
        project_id=ctx.project_id,
        source="guest agent",
        resource_refs=[vm.id],
    )
    if not host:
        evidence.status = "unsupported"
        evidence.limitations = ["No guest-agent host binding is available."]
        return evidence
    command = COMMANDS[args.check]
    if host.host_type == "kubevirt-cluster":
        from app.services.assistant.kubevirt_checks import project_clients, vm_state
        from app.services.providers.kubevirt import kubevirt_exec_guest_agent

        vm_state(db, project, host, vm.id)  # Exact ownership before exec transport.
        with project_clients(db, project, host) as (_, _, _, provider, _):
            result = kubevirt_exec_guest_agent(
                provider, project.id, vm.id, command, timeout=5
            )
    else:
        from app.services.deploy_topology import _vm_domain_name
        from app.services.troshkad_client import start_job, wait_for_job

        db.commit()
        job = start_job(
            host,
            "/vm/guest-exec",
            {
                "domain_name": _vm_domain_name(project.id, vm.id),
                "command": command,
                "timeout": 5,
            },
            request_timeout=5,
        )
        completed = wait_for_job(host, job, timeout=8)
        result = (
            completed.get("result", {})
            if completed.get("status") == "completed"
            else {}
        )
    if not result or result.get("error") or result.get("exit_code", 0) not in (0, 3):
        evidence.status = "unknown"
        evidence.limitations = [
            "Guest agent did not return a supported result. No fallback login was attempted."
        ]
    else:
        evidence.facts = parse_output(args.check, result.get("output", ""))
    return evidence


def nested(db, ctx, args):
    # Nested cluster credentials/routes need a provider-specific verified adapter.
    # Never use an arbitrary kubeconfig/URL from topology as a model capability.
    from app.models.project import Project

    project = db.get(Project, ctx.project_id)
    keys = {
        str(n.get("data", {}).get("clusterName", ""))
        for n in (project.topology or {}).get("nodes", [])
    }
    return Evidence(
        check="inspect_nested_cluster",
        project_id=ctx.project_id,
        status="unsupported",
        facts={"configured_cluster_key": args.cluster_key in keys},
        limitations=[
            "Nested cluster inspection is not available for this provider. Use the existing cluster console and attach sanitized findings."
        ],
    )
