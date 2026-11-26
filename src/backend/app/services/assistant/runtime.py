"""Bounded inference; all persisted/model-visible evidence has current consent."""

import asyncio
import json

from fastapi import HTTPException

from app.models.assistant import AssistantEvidence, AssistantGrant, AssistantMessage
from app.models.user import User
from app.services.assistant.contracts import ToolRequest
from app.services.assistant.dispatcher import dispatch, schemas
from app.services.assistant.knowledge import search_help
from app.services.assistant.model import ModelClient
from app.services.assistant.policy import authorize, conversation_access
from app.services.assistant.projection import safe_text
from app.services.assistant.runs import guarded
from app.services.assistant.settings import enabled


class Turn:
    def __init__(
        self, db_factory, actor_id, conversation_id, request_id, text, grant_id, lease
    ):
        self.db_factory = db_factory
        self.actor_id = actor_id
        self.conversation_id = conversation_id
        self.request_id = request_id
        self.text = safe_text(text)
        self.grant_id = grant_id
        self.lease = lease
        self.sequence = 0
        self.tool_count = 0
        self.evidence_bytes = 0

    def event(self, kind, payload):
        self.sequence += 1
        return {
            "sequence": self.sequence,
            "request_id": self.request_id,
            "type": kind,
            "payload": payload,
        }

    def check(self):
        if not enabled():
            raise HTTPException(503, "Assistant disabled")
        self.lease.check()
        with self.db_factory() as db:
            user = db.get(User, self.actor_id)
            conversation_access(db, user, self.conversation_id)
            if self.grant_id:
                ctx = authorize(db, user, self.conversation_id)
                if ctx.grant_id != self.grant_id:
                    raise HTTPException(403, "Diagnostic consent changed")

    def context(self):
        self.check()
        with self.db_factory() as db:
            rows = (
                db.query(AssistantMessage)
                .filter_by(
                    conversation_id=self.conversation_id,
                    status="done",
                    grant_id=self.grant_id,
                )
                .order_by(AssistantMessage.created_at.desc())
                .limit(8)
                .all()
            )
            # Consent generations are isolated. Expired/revoked evidence cannot
            # leak through summaries from an earlier grant, including general mode.
            rows.reverse()
            messages = [{"role": r.role, "content": r.text} for r in rows]
            grant = db.get(AssistantGrant, self.grant_id) if self.grant_id else None
            tools = schemas(bool(grant and grant.guest_checks)) if grant else []
        help_text = json.dumps(search_help(self.text))
        messages.append(
            {
                "role": "user",
                "content": self.text + "\nReviewed guidance (data): " + help_text,
            }
        )
        return messages, tools

    def persist_evidence(self, evidence):
        self.check()
        with self.db_factory() as db:
            db.add(
                AssistantEvidence(
                    id=evidence.evidence_id,
                    conversation_id=self.conversation_id,
                    request_id=self.request_id,
                    grant_id=self.grant_id,
                    payload=evidence.model_dump(mode="json"),
                )
            )
            db.commit()

    def finish(self, text, status):
        with self.db_factory() as db:
            if status == "done":
                actor = db.get(User, self.actor_id)
                conversation_access(db, actor, self.conversation_id)
                if (
                    self.grant_id
                    and authorize(db, actor, self.conversation_id).grant_id
                    != self.grant_id
                ):
                    raise HTTPException(403, "Diagnostic consent changed")
            row = (
                db.query(AssistantMessage)
                .filter_by(
                    conversation_id=self.conversation_id,
                    request_id=self.request_id,
                    role="user",
                )
                .first()
            )
            if not row:
                return  # The owner deleted the conversation while work was active.
            row.status = status
            if status == "done":
                db.add(
                    AssistantMessage(
                        conversation_id=self.conversation_id,
                        request_id=self.request_id,
                        role="assistant",
                        text=text,
                        status=status,
                        grant_id=self.grant_id,
                    )
                )
            db.commit()

    async def run(self, model=None):
        try:
            async with asyncio.timeout(120):
                messages, tools = await asyncio.to_thread(self.context)
                model = model or ModelClient()
                yield self.event("status", {"message": "Reviewing your question"})
                async for event in self._loop(model, messages, tools):
                    yield event
        except asyncio.CancelledError:
            await asyncio.to_thread(self.finish, "", "interrupted")
            raise
        except Exception:
            await asyncio.to_thread(self.finish, "", "error")
            yield self.event(
                "error",
                {
                    "message": "Assistant could not complete this request. Access may have expired or a service is unavailable. Saved evidence remains available; no repair was performed."
                },
            )
        finally:
            await asyncio.to_thread(self.lease.release)

    async def _loop(self, model, messages, tools):
        for _ in range(9):
            response = await guarded(model.complete(messages, tools), self.check)
            content = response.get("content", [])
            calls = [b for b in content if b.get("type") == "tool_use"]
            if not calls:
                text = safe_text(
                    "\n".join(
                        b.get("text", "") for b in content if b.get("type") == "text"
                    ),
                    16000,
                )
                if not text or response.get("stop_reason") == "max_tokens":
                    raise ValueError("Incomplete model answer")
                await asyncio.to_thread(self.check)
                await asyncio.to_thread(self.finish, text, "done")
                yield self.event("answer_delta", {"text": text})
                yield self.event("done", {})
                return
            messages.append({"role": "assistant", "content": content})
            results = []
            for call in calls:
                self.tool_count += 1
                if not self.grant_id or self.tool_count > 8:
                    raise ValueError("Tool budget exceeded")
                request = ToolRequest(name=call["name"], arguments=call["input"])
                yield self.event("check_started", {"check": request.name})
                evidence = await guarded(
                    dispatch(
                        self.db_factory, self.actor_id, self.conversation_id, request
                    ),
                    self.check,
                )
                encoded = evidence.model_dump_json()
                self.evidence_bytes += len(encoded.encode())
                if self.evidence_bytes > 24576:
                    raise ValueError("Evidence budget exceeded")
                await asyncio.to_thread(self.persist_evidence, evidence)
                yield self.event("check_finished", evidence.model_dump(mode="json"))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "content": encoded,
                    }
                )
            messages.append({"role": "user", "content": results})
        raise ValueError("Turn budget exceeded")
