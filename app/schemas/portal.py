"""Schemas for the customer Portal API broker (RFC #73)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PortalSessionResponse(BaseModel):
    """A brokered, short-lived Portal API token the client uses to call the CRM
    Portal API directly (e.g. Refer & Earn)."""

    portal_token: str
    expires_at: int = Field(..., description="Unix epoch seconds")
    api_base: str = Field(..., description="Absolute base URL for the CRM Portal API")


# ── Refer & Earn (served from the local mirror) ──────────────────────────────


class ReferralProgram(BaseModel):
    enabled: bool
    reward_amount: str = Field("0", description="Advertised reward, decimal string")
    reward_currency: str = "NGN"


class ReferralTotals(BaseModel):
    total: int = 0
    pending: int = 0
    qualified: int = 0
    rewarded: int = 0
    total_earned: str = "0"


class ReferralItem(BaseModel):
    id: str
    status: str
    referred_name: str | None = None
    reward_amount: str | None = None
    reward_currency: str = "NGN"
    reward_status: str = "none"
    created_at: str | None = None
    qualified_at: str | None = None


class MyReferralsResponse(BaseModel):
    """The signed-in subscriber's referral code, program terms, and history,
    served from the local mirror (RFC #73)."""

    code: str
    share_url: str
    program: ReferralProgram
    totals: ReferralTotals
    referrals: list[ReferralItem] = Field(default_factory=list)


class ReferAFriendRequest(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    note: str | None = None


class ReferAFriendResponse(BaseModel):
    id: str
    status: str
    message: str = "Referral submitted"


# ── Installation / Project tracker (served from the local mirror) ────────────


class ProjectStage(BaseModel):
    key: str | None = None
    title: str
    status: str = "pending"  # pending | in_progress | done
    completed_at: str | None = None


class ProjectItem(BaseModel):
    id: str
    name: str
    status: str
    project_type: str | None = None
    progress_pct: int = 0
    current_stage: str | None = None
    stages: list[ProjectStage] = Field(default_factory=list)
    customer_address: str | None = None
    region: str | None = None
    start_at: str | None = None
    due_at: str | None = None
    completed_at: str | None = None
    created_at: str | None = None


class MyProjectsResponse(BaseModel):
    """The signed-in subscriber's installations/projects, served from the local
    mirror (Installation tracker)."""

    projects: list[ProjectItem] = Field(default_factory=list)
    total: int = 0
    active: int = 0


# ── Field Service / Work Orders (served from the local mirror) ───────────────


class WorkOrderItem(BaseModel):
    id: str
    title: str
    status: str
    work_type: str | None = None
    priority: str | None = None
    technician_name: str | None = None
    technician_phone: str | None = None
    address: str | None = None
    scheduled_start: str | None = None
    scheduled_end: str | None = None
    estimated_arrival_at: str | None = None
    estimated_duration_minutes: int | None = None
    completed_at: str | None = None
    created_at: str | None = None


class MyWorkOrdersResponse(BaseModel):
    """The signed-in subscriber's field-service work orders (Field Service
    tracker)."""

    work_orders: list[WorkOrderItem] = Field(default_factory=list)
    total: int = 0
    upcoming: int = 0
