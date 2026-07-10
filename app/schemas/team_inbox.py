from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class InboxConversationEscalateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    service_team_id: UUID
    assigned_person_id: UUID | None = None
    auto_assign: bool = True
    reason: str | None = Field(default=None, max_length=500)


class InboxConversationEscalationRead(BaseModel):
    conversation_id: UUID
    kind: str
    service_team_id: UUID | None = None
    assigned_person_id: UUID | None = None
    reason: str | None = None
