"""Immutable supervisor confirmation intent; staff identity is server-produced."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.support_resolution_contracts import SupportResolutionRequestCommand
from app.services.topology.outage_completion_contracts import (
    ReviewedComplaintResolutionScope,
)

PERMISSION = "support:ticket:update"
ACTION = "support.request_resolution_confirmation"


class ResolutionRequestInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    intent_id: UUID
    confirmed: Literal[True]
    reason: str = Field(min_length=1, max_length=1000)
    grace_hours: int = Field(ge=0, le=2147483647)
    reviewed_shared_scopes: tuple[ReviewedComplaintResolutionScope, ...] = ()

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Explain why customer confirmation is requested")
        return value

    def command(
        self, *, ticket_id: UUID, access: PermissionAdmission
    ) -> SupportResolutionRequestCommand:
        return SupportResolutionRequestCommand(
            ticket_id=ticket_id,
            actor_id=access.principal_id,
            access=access,
            grace_hours=self.grace_hours,
            reviewed_shared_scopes=self.reviewed_shared_scopes,
            context=CommandContext(
                command_id=self.intent_id,
                correlation_id=self.intent_id,
                actor=str(access.principal_id),
                scope="support.ticket:request_resolution_confirmation",
                reason=self.reason,
                idempotency_key=str(self.intent_id),
            ),
        )


@dataclass(frozen=True, slots=True)
class ResolutionFormValues:
    intent_id: str
    reason: str = ""
    grace_hours: str = "24"
    reviewed_shared_scopes: str = "[]"
