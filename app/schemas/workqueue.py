from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class WorkqueueItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_kind: str
    item_id: UUID
    title: str
    subtitle: str | None = None
    status: str
    priority: int
    # SLA-derived ranking — see app/services/workqueue/scoring_config.py.
    score: int = 0
    reason: str = ""
    urgency: str = "low"
    happened_at: datetime | None = None
    due_at: datetime | None = None
    last_activity_at: datetime | None = None
    subscriber_id: UUID | None = None
    service_team_id: UUID | None = None
    assigned_person_id: UUID | None = None
    url: str | None = None
    actions: list[str] = Field(default_factory=list)
    can_act: bool = False
    metadata: dict = Field(default_factory=dict)


class WorkqueueSectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_kind: str
    total: int
    items: list[WorkqueueItemRead] = Field(default_factory=list)


class WorkqueueViewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    audience: str
    generated_at: datetime
    total: int
    right_now: list[WorkqueueItemRead] = Field(default_factory=list)
    sections: list[WorkqueueSectionRead] = Field(default_factory=list)


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
