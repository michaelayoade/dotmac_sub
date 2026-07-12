from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class WorkqueueItemRead(BaseModel):
    item_kind: str
    item_id: UUID
    title: str
    subtitle: str | None = None
    status: str
    priority: int
    due_at: datetime | None = None
    last_activity_at: datetime | None = None
    subscriber_id: UUID | None = None
    service_team_id: UUID | None = None
    assigned_person_id: UUID | None = None
    url: str | None = None
    metadata: dict = Field(default_factory=dict)


class WorkqueueSnoozeCreate(BaseModel):
    item_kind: str = Field(min_length=1, max_length=32)
    item_id: UUID
    snooze_until: datetime | None = None
    until_next_reply: bool = False


class WorkqueueSnoozeRead(BaseModel):
    id: UUID
    user_id: UUID
    item_kind: str
    item_id: UUID
    snooze_until: datetime | None = None
    until_next_reply: bool
    created_at: datetime
