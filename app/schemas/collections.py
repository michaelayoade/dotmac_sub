from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.catalog import DunningAction
from app.models.collections import DunningCaseStatus


class DunningCaseBase(BaseModel):
    account_id: UUID
    policy_set_id: UUID | None = None
    status: DunningCaseStatus = DunningCaseStatus.open
    current_step: int | None = None
    started_at: datetime | None = None
    resolved_at: datetime | None = None
    notes: str | None = None


class DunningCaseCreate(DunningCaseBase):
    pass


class DunningCaseUpdate(BaseModel):
    account_id: UUID | None = None
    policy_set_id: UUID | None = None
    status: DunningCaseStatus | None = None
    current_step: int | None = None
    started_at: datetime | None = None
    resolved_at: datetime | None = None
    notes: str | None = None


class DunningCaseRead(DunningCaseBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class DunningActionLogBase(BaseModel):
    case_id: UUID
    invoice_id: UUID | None = None
    payment_id: UUID | None = None
    step_day: int | None = None
    action: DunningAction
    outcome: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    executed_at: datetime | None = None


class DunningActionLogCreate(DunningActionLogBase):
    pass


class DunningActionLogUpdate(BaseModel):
    case_id: UUID | None = None
    invoice_id: UUID | None = None
    payment_id: UUID | None = None
    step_day: int | None = None
    action: DunningAction | None = None
    outcome: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    executed_at: datetime | None = None


class DunningActionLogRead(DunningActionLogBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


class DunningRunRequest(BaseModel):
    run_at: datetime | None = None
    dry_run: bool = False


class DunningRunResponse(BaseModel):
    run_at: datetime
    accounts_scanned: int
    cases_created: int
    actions_created: int
    skipped: int


class PrepaidEnforcementRunRequest(BaseModel):
    run_at: datetime | None = None
    dry_run: bool = False


class PrepaidEnforcementRunResponse(BaseModel):
    run_at: datetime
    accounts_scanned: int
    accounts_warned: int
    accounts_suspended: int
    accounts_deactivated: int
    skipped: int
