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


# ── Self-serve quotes (served from the local mirror) ─────────────────────────


class QuoteRequestCreate(BaseModel):
    """Request a map-pinned installation quote (the pin drives feasibility +
    estimate + deposit, computed by the CRM)."""

    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    address: str | None = None
    region: str | None = None
    note: str | None = None


class QuoteFeasibility(BaseModel):
    coverage: str | None = None  # covered | survey_required | out_of_area
    feasible: bool | None = None
    distance_meters: float | None = None
    nearest_fap_name: str | None = None


class QuoteLineItem(BaseModel):
    description: str
    quantity: str | None = None
    unit_price: str | None = None
    amount: str | None = None


class QuoteItem(BaseModel):
    id: str
    status: str
    currency: str = "NGN"
    subtotal: str | None = None
    tax_total: str | None = None
    total: str | None = None
    project_type: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    region: str | None = None
    feasibility: QuoteFeasibility = Field(default_factory=QuoteFeasibility)
    estimate_provisional: bool = False
    deposit_percent: int | None = None
    deposit_amount: str | None = None
    deposit_paid: bool = False
    deposit_reference: str | None = None
    line_items: list[QuoteLineItem] = Field(default_factory=list)
    sales_order_id: str | None = None
    project_id: str | None = None
    created_at: str | None = None
    expires_at: str | None = None


class MyQuotesResponse(BaseModel):
    """The signed-in subscriber's self-serve installation quotes."""

    quotes: list[QuoteItem] = Field(default_factory=list)
    total: int = 0
    open: int = 0


class QuoteDepositInitiateRequest(BaseModel):
    """Start paying a quote's deposit via the existing billing/pay flow."""

    provider: str | None = None
    redirect_url: str | None = None


class QuoteDepositInitiateResponse(BaseModel):
    invoice_id: str
    quote_id: str
    amount: str
    currency: str = "NGN"
    provider_type: str | None = None
    provider_public_key: str | None = None
    payment_reference: str | None = None
    checkout_url: str | None = None
    customer_email: str | None = None
    charged: bool = False


class QuoteDepositVerifyRequest(BaseModel):
    reference: str
    provider: str | None = None


class QuoteDepositVerifyResponse(BaseModel):
    paid: bool
    reference: str
    quote: QuoteItem | None = None
