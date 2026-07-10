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


class InboxConversationReplyRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    body_html: str = Field(min_length=1)
    body_text: str | None = None
    subject: str | None = Field(default=None, max_length=200)
    to_email: str | None = Field(default=None, max_length=255)


class InboxConversationReplyRead(BaseModel):
    conversation_id: UUID
    kind: str
    message_id: UUID | None = None
    service_team_id: UUID | None = None
    sender_key: str | None = None
    activity: str | None = None
    from_address: str | None = None
    to_email: str | None = None
    reason: str | None = None
