"""All model capabilities are registered here; unknown tools cannot execute."""

import asyncio
import concurrent.futures
import threading
import time

from fastapi import HTTPException
from pydantic import ValidationError

from app.models.assistant import AssistantAuditEvent
from app.models.user import User
from app.services.assistant import (
    guest_checks,
    host_checks,
    knowledge,
    network_checks,
    project_checks,
)
from app.services.assistant.contracts import (
    ClusterArgs,
    Evidence,
    GuestArgs,
    HelpArgs,
    NetworkArgs,
    NoArgs,
    RunArgs,
)
from app.services.assistant.policy import authorize
from app.services.assistant.settings import enabled

TOOLS = {
    "get_project_summary": (
        NoArgs,
        "project",
        project_checks.summary,
        "Read safe persisted project and VM state.",
    ),
    "get_install_failure": (
        NoArgs,
        "project",
        project_checks.install,
        "Read recognized installation failure categories; no raw logs.",
    ),
    "get_cleanup_status": (
        NoArgs,
        "project",
        project_checks.cleanup,
        "Read cleanup phases and blockers; never triggers cleanup.",
    ),
    "get_host_health": (
        NoArgs,
        "project",
        host_checks.health,
        "Read assigned hosts' filtered health observations.",
    ),
    "get_workload_failure": (
        RunArgs,
        "project",
        project_checks.workload,
        "Read this project's workload failure categories.",
    ),
    "inspect_network_path": (
        NetworkArgs,
        "project",
        network_checks.network,
        "Compare configured forwards and VM state. Reachability may remain unknown.",
    ),
    "inspect_guest": (
        GuestArgs,
        "guest",
        guest_checks.guest,
        "Run one fixed guest-agent diagnostic. Requires guest consent.",
    ),
    "inspect_nested_cluster": (
        ClusterArgs,
        "guest",
        guest_checks.nested,
        "Check whether a verified nested-cluster adapter is available.",
    ),
    "search_help": (
        HelpArgs,
        "project",
        knowledge.help_check,
        "Search reviewed bundled troubleshooting guidance.",
    ),
}
_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="assistant-check"
)
_slots = threading.BoundedSemaphore(4)


def schemas(guest_allowed=False):
    return [
        {
            "name": name,
            "description": description,
            "input_schema": schema.model_json_schema(),
        }
        for name, (schema, scope, _, description) in TOOLS.items()
        if scope != "guest" or guest_allowed
    ]


def _authorize(db_factory, actor_id, conversation_id, scope):
    if not enabled():
        raise HTTPException(503, "Assistant disabled")
    with db_factory() as db:
        return authorize(db, db.get(User, actor_id), conversation_id, scope)


def _execute(db_factory, ctx, args, adapter):
    with db_factory() as db:
        return adapter(db, ctx, args)


def _bounded(evidence):
    if len(evidence.model_dump_json().encode()) <= 4096:
        return evidence
    evidence.facts = {}
    evidence.truncated = True
    evidence.status = "unknown"
    evidence.limitations = [
        "Result exceeded the diagnostic size limit; narrow the check."
    ]
    return evidence


async def dispatch(db_factory, actor_id, conversation_id, request):
    definition = TOOLS.get(request.name)
    if not definition:
        raise HTTPException(400, "Unknown diagnostic")
    schema, scope, adapter, _ = definition
    try:
        args = schema.model_validate(request.arguments)
    except ValidationError:
        raise HTTPException(400, "Invalid diagnostic arguments") from None
    ctx = _authorize(db_factory, actor_id, conversation_id, scope)
    if not _slots.acquire(blocking=False):
        raise HTTPException(503, "Diagnostic capacity is busy")
    started = time.monotonic()
    future = _pool.submit(_execute, db_factory, ctx, args, adapter)
    future.add_done_callback(lambda _: _slots.release())
    result = await _result(future, request.name, ctx.project_id)
    current = _authorize(db_factory, actor_id, conversation_id, scope)
    if current.grant_id != ctx.grant_id:
        raise HTTPException(403, "Diagnostic consent changed")
    with db_factory() as db:
        db.add(
            AssistantAuditEvent(
                actor_id=actor_id,
                conversation_id=conversation_id,
                check=request.name,
                outcome=result.status,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )
        db.commit()
    return _bounded(result)


async def _result(future, name, project_id):
    try:
        return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), 20)
    except TimeoutError:
        status = "timeout"
    except HTTPException:
        status = "denied"
    except Exception:
        status = "error"
    return Evidence(
        check=name,
        project_id=project_id,
        status=status,
        limitations=[
            "The check did not produce usable evidence. No healthy/absent conclusion is supported."
        ],
    )
