"""Exact staff intent for applying accepted physical scope."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ApplyAcceptedProjectCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_id: UUID
    command_id: UUID
    correlation_id: UUID
    expected_project_revision: datetime
    expected_task_revision: datetime | None = None
    expected_requirement_token: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A reason is required")
        return value
