from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AIInsightCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    persona_key: str = Field(min_length=1, max_length=80)
    domain: str = Field(min_length=1, max_length=80)
    severity: str = "info"
    entity_type: str = Field(min_length=1, max_length=80)
    entity_id: str | None = Field(default=None, max_length=120)
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1)
    structured_output: dict | None = None
    recommendations: list | None = None
    confidence_score: float | None = None
    context_quality_score: float | None = None
    trigger: str = "manual"
    expires_at: datetime | None = None
    metadata: dict | None = None


class AIInsightRead(BaseModel):
    id: UUID
    persona_key: str
    domain: str
    severity: str
    status: str
    entity_type: str
    entity_id: str | None = None
    title: str
    summary: str
    structured_output: dict | None = None
    recommendations: list | None = None
    confidence_score: float | None = None
    context_quality_score: float | None = None
    trigger: str
    acknowledged_at: datetime | None = None
    acknowledged_by_system_user_id: UUID | None = None
    expires_at: datetime | None = None
    metadata: dict | None = None
    created_at: datetime
    updated_at: datetime


class AiIntakeConfigUpsert(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    scope_key: str = Field(min_length=1, max_length=160)
    channel_type: str = Field(min_length=1, max_length=40)
    is_enabled: bool = False
    confidence_threshold: float = 0.75
    allow_followup_questions: bool = True
    max_clarification_turns: int = 1
    escalate_after_minutes: int = 5
    exclude_campaign_attribution: bool = True
    fallback_team_id: UUID | None = None
    instructions: str | None = None
    department_mappings: list | None = None
    metadata: dict | None = None


class AiIntakeConfigRead(AiIntakeConfigUpsert):
    id: UUID
    created_at: datetime
    updated_at: datetime
