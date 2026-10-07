"""Versioned, contact-free evidence for a customer's temporary test requests."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class TestConnectionReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: UUID
    created_at: AwareDatetime
    created_by: str | None
    duration_seconds: int = Field(ge=1)


class TestConnectionCreated(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    tenant_id: UUID
    grant_id: UUID
    subscription_id: UUID
    customer_id: UUID
    command_id: UUID
    correlation_id: UUID
    causation_id: UUID | None = None
    created_at: AwareDatetime
    window_start: AwareDatetime
    window_end: AwareDatetime
    count_7d: int = Field(ge=1)
    recent_connections: tuple[TestConnectionReference, ...] = Field(
        min_length=1, max_length=10
    )

    @model_validator(mode="after")
    def validate_window(self) -> "TestConnectionCreated":
        from datetime import timedelta

        if (
            self.window_end != self.created_at
            or self.window_end - self.window_start != timedelta(days=7)
        ):
            raise ValueError("The count must describe the preceding seven days.")
        if len(self.recent_connections) > self.count_7d:
            raise ValueError("References cannot exceed the count.")
        if len({item.grant_id for item in self.recent_connections}) != len(
            self.recent_connections
        ):
            raise ValueError("References must be distinct requests.")
        if self.grant_id not in {item.grant_id for item in self.recent_connections}:
            raise ValueError("The current request must be included.")
        if any(
            not self.window_start < item.created_at <= self.window_end
            for item in self.recent_connections
        ):
            raise ValueError("A referenced request is outside the window.")
        return self


class TestConnectionFinanceReviewQueued(BaseModel):
    """Contact-free delivery-intent evidence, not a delivery confirmation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    tenant_id: UUID
    review_id: UUID
    source_event_id: UUID
    customer_id: UUID
    rule_version_id: UUID
    step_index: int = Field(ge=0)
    recipient_count: int = Field(ge=1)
    command_id: UUID
    correlation_id: UUID
    causation_id: UUID | None = None
