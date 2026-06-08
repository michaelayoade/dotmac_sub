"""Customer self-care endpoints — scoped to the authenticated subscriber.

Unlike the staff-facing billing/catalog/usage list endpoints (which are gated by
`billing:read`/`catalog:read`/... permissions and take an explicit account_id),
these require ONLY authentication and force scoping to the caller's own
`subscriber_id`. They are what the customer mobile app / self-care SPA uses so a
subscriber can read their own data without holding staff permissions.

Mounted at /api/v1/me with router-level require_user_auth (see main.py).
"""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.subscriber import Subscriber
from app.schemas.billing import (
    AccountBalanceResponse,
    InvoiceRead,
    LedgerEntryRead,
    PaymentRead,
    TopupInitiateRequest,
    TopupInitiateResponse,
    TopupPageResponse,
    TopupVerifyRequest,
    TopupVerifyResponse,
)
from app.schemas.catalog import (
    PlanChangePageResponse,
    PlanChangeSubmitRequest,
    PlanChangeSubmitResponse,
    PlanOfferSummary,
    SubscriptionRead,
)
from app.schemas.common import ListResponse
from app.schemas.notification import NotificationRead
from app.schemas.usage import QuotaBucketRead, RadiusAccountingSessionRead
from app.services import billing as billing_service
from app.services import catalog as catalog_service
from app.services import customer_portal_flow_changes as customer_changes
from app.services import customer_portal_flow_payments as customer_payments
from app.services import notification as notification_service
from app.services import usage as usage_service
from app.services.auth_dependencies import require_user_auth

router = APIRouter(prefix="/me", tags=["me"])


def _subscriber_id(principal: dict) -> str:
    """Return the caller's subscriber id, or 403 for non-subscriber principals."""
    if principal.get("principal_type") != "subscriber":
        raise HTTPException(status_code=403, detail="A customer account is required")
    return str(principal["subscriber_id"])


def _customer(db: Session, principal: dict) -> dict:
    """Adapt the bearer principal to the `customer` dict the portal top-up flow
    expects (account_id == subscriber_id; username carries the email)."""
    sid = _subscriber_id(principal)
    subscriber = db.get(Subscriber, sid)
    return {
        "account_id": sid,
        "subscriber_id": sid,
        "username": getattr(subscriber, "email", "") or "",
    }


@router.get("/invoices", response_model=ListResponse[InvoiceRead])
def my_invoices(
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    account_id = _subscriber_id(principal)
    return billing_service.invoices.list_response(
        db, account_id, status, None, order_by, order_dir, limit, offset
    )


@router.get("/invoices/{invoice_id}", response_model=InvoiceRead)
def my_invoice(
    invoice_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    account_id = _subscriber_id(principal)
    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice or str(getattr(invoice, "account_id", "")) != account_id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


@router.get("/payments", response_model=ListResponse[PaymentRead])
def my_payments(
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    account_id = _subscriber_id(principal)
    return billing_service.payments.list_response(
        db, account_id, None, status, None, order_by, order_dir, limit, offset
    )


@router.get("/balance", response_model=AccountBalanceResponse)
def my_balance(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's wallet/credit balance (positive = credit on file)."""
    from app.services.billing._common import get_account_credit_balance

    account_id = _subscriber_id(principal)
    return AccountBalanceResponse(
        credit_balance=get_account_credit_balance(db, account_id)
    )


@router.get("/ledger", response_model=ListResponse[LedgerEntryRead])
def my_ledger(
    entry_type: str | None = None,
    source: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's account ledger — charges, payments, credits, adjustments."""
    account_id = _subscriber_id(principal)
    return billing_service.ledger_entries.list_response(
        db, account_id, entry_type, source, True, order_by, order_dir, limit, offset
    )


@router.get("/subscriptions", response_model=ListResponse[SubscriptionRead])
def my_subscriptions(
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    subscriber_id = _subscriber_id(principal)
    return catalog_service.subscriptions.list_response(
        db, subscriber_id, None, status, order_by, order_dir, limit, offset
    )


@router.get("/quota-buckets", response_model=ListResponse[QuotaBucketRead])
def my_quota_buckets(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """All quota buckets across the caller's subscriptions (single round-trip)."""
    subscriber_id = _subscriber_id(principal)
    return usage_service.quota_buckets.list_response_for_subscriber(
        db, subscriber_id, limit, offset
    )


def _offer_summary(offer, summary) -> PlanOfferSummary | None:
    if offer is None:
        return None
    return PlanOfferSummary(
        id=offer.id,
        name=getattr(offer, "name", "") or "Plan",
        amount=float(getattr(summary, "amount", 0) or 0),
        currency=getattr(summary, "currency", "NGN"),
        period_label=getattr(summary, "period_label", "/cycle"),
    )


@router.get(
    "/subscriptions/{subscription_id}/plan-change",
    response_model=PlanChangePageResponse,
)
def my_plan_change_options(
    subscription_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Plans the caller can switch their service to (no upfront proration)."""
    ctx = customer_changes.get_change_plan_page(
        db, _customer(db, principal), subscription_id
    )
    if ctx is None:
        raise HTTPException(status_code=404, detail="Service not found")
    summaries = ctx.get("available_offer_summaries", {})
    available = [
        _offer_summary(offer, summaries.get(str(offer.id)))
        for offer in ctx.get("available_offers", [])
    ]
    return PlanChangePageResponse(
        current_offer=_offer_summary(
            ctx.get("current_offer"), ctx.get("current_offer_summary")
        ),
        available_offers=[o for o in available if o is not None],
        wallet_balance=ctx.get("current_wallet_balance"),
        next_billing_date=ctx.get("next_billing_date"),
        billing_message=ctx.get("billing_message"),
    )


@router.get("/subscriptions/{subscription_id}/plan-change/quote")
def my_plan_change_quote(
    subscription_id: str,
    offer_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Prorated quote for switching this service to a single target offer."""
    quote = customer_changes.get_plan_change_quote(
        db, _customer(db, principal), subscription_id, offer_id
    )
    if quote is None:
        raise HTTPException(status_code=404, detail="Plan not available")
    return quote


@router.post(
    "/subscriptions/{subscription_id}/plan-change",
    response_model=PlanChangeSubmitResponse,
)
def my_plan_change_submit(
    subscription_id: str,
    payload: PlanChangeSubmitRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Submit a plan-change request for the caller's own service."""
    customer = _customer(db, principal)
    # Ownership is enforced inside the service (account_id == subscriber).
    try:
        result = customer_changes.submit_change_plan(
            db,
            customer,
            subscription_id,
            str(payload.offer_id),
            payload.effective_date,
            payload.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PlanChangeSubmitResponse(success=bool(result.get("success", True)))


@router.get("/topup", response_model=TopupPageResponse)
def my_topup_page(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Top-up page context: current balance, amount limits, presets, provider."""
    ctx = customer_payments.get_topup_page(db, _customer(db, principal))
    return TopupPageResponse(
        provider_type=ctx["provider_type"],
        provider_public_key=ctx.get("provider_public_key"),
        prepaid_balance=ctx.get("prepaid_balance"),
        min_amount=ctx["min_amount"],
        max_amount=ctx["max_amount"],
        preset_amounts=ctx.get("preset_amounts", []),
        customer_email=ctx.get("customer_email"),
    )


@router.post("/topup/initiate", response_model=TopupInitiateResponse)
def my_topup_initiate(
    payload: TopupInitiateRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Create a top-up checkout intent for the caller's prepaid account."""
    customer = _customer(db, principal)
    try:
        result = customer_payments.create_topup_intent(db, customer, payload.amount)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TopupInitiateResponse(
        intent_id=result["intent_id"],
        provider_type=result["provider_type"],
        provider_public_key=result.get("provider_public_key"),
        payment_reference=result["reference"],
        amount=result["requested_amount"],
        currency=result.get("currency", "NGN"),
        customer_email=customer["username"] or None,
    )


@router.post("/topup/verify", response_model=TopupVerifyResponse)
def my_topup_verify(
    payload: TopupVerifyRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Verify a top-up transaction and credit the account."""
    customer = _customer(db, principal)
    try:
        result = customer_payments.verify_and_record_topup(
            db, customer, payload.reference
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TopupVerifyResponse(
        reference=payload.reference,
        amount=Decimal(str(result.get("amount") or "0")),
        already_recorded=result.get("already_recorded", False),
        available_balance=result.get("available_balance"),
        credit_added=result.get("credit_added"),
    )


@router.get("/notifications", response_model=ListResponse[NotificationRead])
def my_notifications(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The subscriber's own notifications (in-app inbox), newest first."""
    subscriber_id = _subscriber_id(principal)
    return notification_service.notifications.list_response_for_subscriber(
        db, subscriber_id, limit, offset
    )


@router.get(
    "/radius-accounting-sessions",
    response_model=ListResponse[RadiusAccountingSessionRead],
)
def my_accounting_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """RADIUS accounting (data usage) sessions across the caller's own
    subscriptions, newest first."""
    subscriber_id = _subscriber_id(principal)
    return usage_service.radius_accounting_sessions.list_response_for_subscriber(
        db, subscriber_id, limit, offset
    )
