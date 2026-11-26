from uuid import UUID

from pydantic import Field

from app.services.assistant.contracts import StrictModel


class ConversationCreate(StrictModel):
    project_id: UUID | None = None


class GrantCreate(StrictModel):
    guest_checks: bool = False


class MessageCreate(StrictModel):
    request_id: UUID
    text: str = Field(min_length=1, max_length=2000)
