from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ErpStaffLeaveRestrictionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1, max_length=240)
    restriction_id: str = Field(min_length=1, max_length=200)
    erp_employee_id: str = Field(min_length=1, max_length=200)
    system_user_id: UUID
    effective_from: datetime
    effective_until: datetime | None = None
    status: Literal["active", "cancelled", "revoked"]
    version: int = Field(ge=1)
    updated_at: datetime
    source_system: str = Field(default="dotmac_erp", min_length=1, max_length=80)


class ErpStaffAccountStatusEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1, max_length=240)
    erp_employee_id: str = Field(min_length=1, max_length=200)
    system_user_id: UUID
    account_status: Literal["active", "inactive"]
    version: int = Field(ge=1)
    updated_at: datetime
    source_system: str = Field(default="dotmac_erp", min_length=1, max_length=80)
    reason: str | None = Field(default=None, max_length=240)


class ErpStaffAccessWebhook(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: Literal[
        "staff.leave_restriction.v1",
        "staff.account_status.v1",
    ]
    leave_restriction: ErpStaffLeaveRestrictionEvent | None = None
    account_status: ErpStaffAccountStatusEvent | None = None

    @model_validator(mode="after")
    def _event_matches_type(self) -> ErpStaffAccessWebhook:
        has_leave = self.leave_restriction is not None
        has_status = self.account_status is not None
        if has_leave == has_status:
            raise ValueError("exactly one staff access event is required")
        if self.event_type == "staff.leave_restriction.v1" and not has_leave:
            raise ValueError("leave restriction event is required")
        if self.event_type == "staff.account_status.v1" and not has_status:
            raise ValueError("account status event is required")
        return self

    @property
    def provider_event_id(self) -> str:
        event = self.leave_restriction or self.account_status
        assert event is not None
        return event.event_id


class ErpStaffAccessReceipt(BaseModel):
    event_id: str
    event_type: str
    applied: bool
    replayed: bool
    status: str
