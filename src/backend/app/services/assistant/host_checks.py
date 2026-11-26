from app.models.host import Host
from app.models.project import Project
from app.models.vm import VM
from app.services.assistant.contracts import Evidence
from app.services.assistant.projection import identifier


def assigned_hosts(db, project):
    ids = {project.host_id, project.mesh_network_host_id}
    ids.update(
        row.host_id for row in db.query(VM).filter_by(project_id=project.id).all()
    )
    # Runtime VM rows are authoritative; do not accept arbitrary host IDs from LLM.
    return db.query(Host).filter(Host.id.in_(ids - {None})).limit(10).all()


def health(db, ctx, args):
    hosts = assigned_hosts(db, db.get(Project, ctx.project_id))
    return Evidence(
        check="get_host_health",
        project_id=ctx.project_id,
        facts={
            "hosts": [
                {
                    "id": h.id,
                    "type": identifier(h.host_type),
                    "state": identifier(h.state),
                    "agent_status": identifier(h.agent_status),
                    "total_vcpus": h.total_vcpus,
                    "used_vcpus": h.used_vcpus,
                    "total_ram_mb": h.total_ram_mb,
                    "used_ram_mb": h.used_ram_mb,
                    "observed_at": h.last_health_at.isoformat()
                    if h.last_health_at
                    else None,
                }
                for h in hosts
            ]
        },
        status="ok" if hosts else "unknown",
        limitations=[
            "Host-poller observations may be stale; no other tenants' resource identities are exposed."
        ],
    )
