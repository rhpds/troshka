from fastapi import HTTPException

from app.models.elastic_ip import ElasticIp
from app.models.native_project_cleanup import NativeProjectNamespaceCleanup
from app.models.project import Project
from app.models.vm import VM
from app.models.workload_run import WorkloadRun
from app.services.assistant.contracts import Evidence
from app.services.assistant.projection import (
    failure_categories,
    identifier,
    project_summary,
)


def summary(db, ctx, args):
    project = db.get(Project, ctx.project_id)
    facts = project_summary(project).model_dump()
    vms = db.query(VM).filter_by(project_id=ctx.project_id).limit(41).all()
    facts["vms"] = [{"id": v.id, "state": identifier(v.state)} for v in vms[:40]]
    facts["endpoints"] = [
        {"id": e.id, "state": identifier(e.state)}
        for e in db.query(ElasticIp)
        .filter_by(project_id=ctx.project_id)
        .limit(10)
        .all()
    ]
    facts["workload_runs"] = [
        {"id": r.id, "status": identifier(r.status)}
        for r in db.query(WorkloadRun)
        .filter_by(project_id=ctx.project_id)
        .order_by(WorkloadRun.created_at.desc())
        .limit(5)
        .all()
    ]
    return Evidence(
        check="get_project_summary",
        project_id=ctx.project_id,
        facts=facts,
        truncated=len(vms) > 40 or facts["truncated"],
        limitations=[
            "Persisted state, not proof of current guest readiness. Unsaved canvas changes are excluded."
        ],
    )


def install(db, ctx, args):
    project = db.get(Project, ctx.project_id)
    from app.models.user import User

    owner = db.get(User, ctx.actor_id)
    categories = failure_categories(project.deploy_error)
    categories += [
        c for c in failure_categories(project.ocp_status_detail) if c not in categories
    ]
    return Evidence(
        check="get_install_failure",
        project_id=ctx.project_id,
        status="ok" if categories else "unknown",
        facts={
            "installation_state": identifier(project.ocp_status or "unknown"),
            "infrastructure_state": identifier(project.state),
            "failure_categories": categories,
            "owner_pull_secret_present": bool(owner.ocp_pull_secret),
        },
        limitations=[
            "Credential presence is not validity; templates or registries may supply alternatives.",
            "Raw logs suppressed. Check installation logs for earliest causal failure; order across separate sources is unknown.",
        ],
    )


def cleanup(db, ctx, args):
    project = db.get(Project, ctx.project_id)
    rows = (
        db.query(NativeProjectNamespaceCleanup)
        .filter_by(project_id=ctx.project_id)
        .limit(10)
        .all()
    )
    facts = {
        "project_state": identifier(project.state),
        "cleanup": [
            {
                "id": row.id,
                "phase": identifier((row.payload or {}).get("phase")),
                "blockers": failure_categories((row.payload or {}).get("last_error")),
            }
            for row in rows
        ],
        "storage_settlement": "unknown",
    }
    return Evidence(
        check="get_cleanup_status",
        project_id=ctx.project_id,
        facts=facts,
        limitations=[
            "Stopped compute and absent namespaces do not establish storage settlement or charge cessation.",
            "No cleanup, collection, finalizer or accounting mutation was requested.",
        ],
    )


def workload(db, ctx, args):
    run = db.get(WorkloadRun, str(args.run_id))
    if not run or run.project_id != ctx.project_id:
        raise HTTPException(404, "Run not found")
    categories = failure_categories(run.log_ref or run.error)
    return Evidence(
        check="get_workload_failure",
        project_id=ctx.project_id,
        facts={
            "run_id": run.id,
            "status": identifier(run.status),
            "failure_categories": categories,
        },
        limitations=[
            "Raw workload output is suppressed. A timeout alone does not establish target-cluster failure."
        ],
    )
