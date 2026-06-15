"""Customer self-care endpoints — scoped to the authenticated subscriber.

Unlike the staff-facing billing/catalog/usage list endpoints (which are gated by
`billing:read`/`catalog:read`/... permissions and take an explicit account_id),
these require ONLY authentication and force scoping to the caller's own
`subscriber_id`. They are what the customer mobile app / self-care SPA uses so a
subscriber can read their own data without holding staff permissions.

Mounted at /api/v1/me with router-level require_user_auth (see main.py).
"""

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.subscriber import Subscriber
from app.models.support import TicketChannel
from app.schemas.billing import (
    AccountBalanceResponse,
    AutopayEnableRequest,
    AutopayStatusResponse,
    InvoiceRead,
    LedgerEntryRead,
    MyPaymentMethodRead,
    PaymentRead,
    TopupInitiateRequest,
    TopupInitiateResponse,
    TopupPageResponse,
    TopupVerifyRequest,
    TopupVerifyResponse,
)
from app.schemas.catalog import (
    AddonPurchaseRequest,
    AddonPurchaseResponse,
    AddonQuoteResponse,
    AddonsAvailableResponse,
    PlanChangePageResponse,
    PlanChangeSubmitRequest,
    PlanChangeSubmitResponse,
    PlanOfferSummary,
    SubscriptionRead,
)
from app.schemas.common import ListResponse
from app.schemas.gis import (
    MyLocationRead,
    MyLocationRequestCreate,
    MyLocationRequestRead,
)
from app.schemas.notification import (
    NotificationRead,
    PushTokenRead,
    PushTokenRegister,
)
from app.schemas.subscriber import (
    SubscriberContactCreate,
    SubscriberContactRead,
    SubscriberContactUpdate,
    SubscriberContactWriteResponse,
)
from app.schemas.support import (
    MySupportCommentCreate,
    MySupportTicketCreate,
    TicketCommentCreate,
    TicketCommentRead,
    TicketCreate,
    TicketRead,
)
from app.schemas.usage import (
    QuotaBucketRead,
    RadiusAccountingSessionRead,
    UsageSummaryResponse,
)
from app.schemas.vas import (
    VasAutoDeductUpdate,
    VasCategoryRead,
    VasPayBillRequest,
    VasPayBillResponse,
    VasPurchaseRequest,
    VasTopupInitiateRequest,
    VasTopupInitiateResponse,
    VasTopupVerifyRequest,
    VasTopupVerifyResponse,
    VasTransactionRead,
    VasVerifyRequest,
    VasVerifyResponse,
    VasWalletOverviewResponse,
)
from app.services import autopay as autopay_service
from app.services import billing as billing_service
from app.services import catalog as catalog_service
from app.services import customer_location_requests as location_service
from app.services import customer_portal_contacts as contacts_service
from app.services import customer_portal_flow_addons as customer_addons
from app.services import customer_portal_flow_changes as customer_changes
from app.services import customer_portal_flow_payment_methods as customer_cards
from app.services import customer_portal_flow_payments as customer_payments
from app.services import geocoding as geocoding_service
from app.services import notification as notification_service
from app.services import push as push_service
from app.services import support as support_service
from app.services import usage as usage_service
from app.services import usage_summary as usage_summary_service
from app.services import vas_purchases as vas_purchases_service
from app.services import vas_wallet as vas_wallet_service
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


@router.get("/autopay", response_model=AutopayStatusResponse)
def my_autopay_status(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Whether the caller has autopay enabled, and on which saved card."""
    return autopay_service.get_status(db, _subscriber_id(principal))


@router.post("/autopay", response_model=AutopayStatusResponse)
def my_autopay_enable(
    payload: AutopayEnableRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Enable autopay against a saved card (the default card if unspecified)."""
    account_id = _subscriber_id(principal)
    try:
        autopay_service.enable(
            db,
            account_id,
            str(payload.payment_method_id) if payload.payment_method_id else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return autopay_service.get_status(db, account_id)


@router.delete("/autopay", response_model=AutopayStatusResponse)
def my_autopay_disable(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Turn off autopay for the caller."""
    account_id = _subscriber_id(principal)
    autopay_service.disable(db, account_id)
    return autopay_service.get_status(db, account_id)


@router.get("/payment-methods", response_model=list[MyPaymentMethodRead])
def my_payment_methods(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's saved cards (tokens never exposed)."""
    account_id = _subscriber_id(principal)
    return customer_cards.list_for_account(db, account_id)


@router.patch(
    "/payment-methods/{method_id}/default", response_model=MyPaymentMethodRead
)
def my_set_default_card(
    method_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Make one of the caller's own saved cards the default."""
    account_id = _subscriber_id(principal)
    method = customer_cards.set_default(db, account_id, method_id)
    if method is None:
        raise HTTPException(status_code=404, detail="Payment method not found")
    return method


@router.delete("/payment-methods/{method_id}", status_code=204)
def my_remove_card(
    method_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Remove one of the caller's own saved cards."""
    account_id = _subscriber_id(principal)
    if not customer_cards.remove(db, account_id, method_id):
        raise HTTPException(status_code=404, detail="Payment method not found")


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
    """Apply a plan change for the caller's own service.

    Mirrors the web portal: same-family changes apply instantly when the
    customer is eligible (no arrears, sufficient prepaid funds); a cross-family
    change is queued as a migration support ticket. apply_instant_plan_change
    verifies ownership, availability, arrears, and prepaid affordability.
    """
    customer = _customer(db, principal)
    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    if not subscription or str(subscription.subscriber_id) != str(
        customer["account_id"]
    ):
        raise HTTPException(status_code=404, detail="Service not found")
    try:
        result = customer_changes.apply_instant_plan_change(
            db=db,
            customer=customer,
            subscription_id=subscription_id,
            offer_id=str(payload.offer_id),
            notes=payload.notes,
        )
    except ValueError as exc:
        message = str(exc)
        # Cross-family changes can't apply instantly — queue a migration ticket.
        if "same plan family" in message.lower():
            from app.models.catalog import CatalogOffer
            from app.services.common import coerce_uuid

            offer = db.get(CatalogOffer, coerce_uuid(str(payload.offer_id)))
            target_family = str(getattr(offer, "plan_family", "") or "").strip()
            if target_family:
                customer_changes.request_plan_migration(
                    db=db,
                    customer=customer,
                    subscription_id=subscription_id,
                    target_family=target_family,
                    requested_offer_id=str(payload.offer_id),
                    notes=payload.notes,
                )
                return PlanChangeSubmitResponse(
                    success=True,
                    status="migration_requested",
                    message=(
                        "This plan needs a migration. We've opened a support "
                        "request to move you."
                    ),
                )
        # Arrears / validation errors → 400 with the clear message.
        raise HTTPException(status_code=400, detail=message) from exc

    if not result.get("success", False):
        # Insufficient prepaid balance for the prorated upgrade.
        shortfall = result.get("shortfall")
        raise HTTPException(
            status_code=402,
            detail=(
                f"Insufficient wallet balance — top up {shortfall} to apply this "
                "upgrade."
                if shortfall is not None
                else "Insufficient wallet balance to apply this upgrade."
            ),
        )
    return PlanChangeSubmitResponse(
        success=True, status="applied", message="Your plan has been changed."
    )


@router.get(
    "/subscriptions/{subscription_id}/add-ons",
    response_model=AddonsAvailableResponse,
)
def my_addons(
    subscription_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Add-ons the caller can buy for this service, plus their active ones."""
    result = customer_addons.list_available_addons(
        db, _customer(db, principal), subscription_id
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Service not found")
    return result


@router.get(
    "/subscriptions/{subscription_id}/add-ons/quote",
    response_model=AddonQuoteResponse,
)
def my_addon_quote(
    subscription_id: str,
    add_on_id: str,
    quantity: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Cost of buying an add-on, against the wallet balance."""
    try:
        quote = customer_addons.get_addon_quote(
            db, _customer(db, principal), subscription_id, add_on_id, quantity
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if quote is None:
        raise HTTPException(status_code=404, detail="Service not found")
    return quote


@router.post(
    "/subscriptions/{subscription_id}/add-ons",
    response_model=AddonPurchaseResponse,
)
def my_addon_purchase(
    subscription_id: str,
    payload: AddonPurchaseRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Buy an add-on, charged from the caller's wallet balance."""
    try:
        return customer_addons.purchase_addon(
            db,
            _customer(db, principal),
            subscription_id,
            str(payload.add_on_id),
            payload.quantity,
            idempotency_key=payload.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/subscriptions/{subscription_id}/add-ons/{sub_add_on_id}",
    status_code=204,
)
def my_addon_cancel(
    subscription_id: str,
    sub_add_on_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Cancel one of the caller's add-ons (stops billing from the next cycle)."""
    if not customer_addons.cancel_addon(
        db, _customer(db, principal), subscription_id, sub_add_on_id
    ):
        raise HTTPException(status_code=404, detail="Add-on not found")


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
    if payload.save_card:
        customer_cards.capture_card_after_payment(
            db, customer["account_id"], payload.reference, None
        )
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


@router.post("/push-tokens", response_model=PushTokenRead, status_code=201)
def my_register_push_token(
    payload: PushTokenRegister,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Register (or refresh) this device's push token for the calling subscriber."""
    subscriber_id = _subscriber_id(principal)
    return push_service.register_token(
        db, subscriber_id, payload.token, payload.platform
    )


@router.delete("/push-tokens/{token}", status_code=204)
def my_unregister_push_token(
    token: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Unregister a device's push token (e.g. on logout). Idempotent."""
    subscriber_id = _subscriber_id(principal)
    push_service.unregister_token(db, subscriber_id, token)
    return None


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


@router.get("/usage-summary", response_model=UsageSummaryResponse)
async def my_usage_summary(
    period: str = Query(
        default="today", pattern="^(hour|today|yesterday|week|cycle|all)$"
    ),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Time-windowed data-usage total + bucketed series for the caller.

    period: hour | today | yesterday | week | cycle | all. The total is billing-grade for
    cycle (rated quota) and all (session octets), and throughput-integrated for
    sub-day windows — see total_source / is_authoritative on the response. The
    window has a defined start/end (unlike the legacy "last 50 sessions" sum)
    and counts the live session's current octets.
    """
    subscriber_id = _subscriber_id(principal)
    summary = await usage_summary_service.get_usage_summary(db, subscriber_id, period)
    summary["fup"] = usage_summary_service.fup_summary(db, subscriber_id)
    return summary


# --- Support tickets (self-scoped) ---------------------------------------------
#
# The staff endpoints in app/api/support.py are gated by
# require_permission("support:ticket:*"), which a subscriber token (no scopes)
# can never satisfy — so the customer app got 403 on every Support call. These
# mirror the /me pattern: auth-only, forced to the caller's own subscriber_id,
# with ownership checks (no IDOR) and internal staff notes filtered out.


def _owned_ticket(db: Session, subscriber_id: str, ticket_id: str):
    """Fetch a ticket and 404 unless it belongs to the calling subscriber.

    Returns 404 (not 403) for someone else's ticket so a customer can't probe
    which ticket ids exist.
    """
    ticket = support_service.tickets.get(db, ticket_id)
    if str(ticket.subscriber_id or "") != subscriber_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


@router.get("/support/tickets", response_model=ListResponse[TicketRead])
def my_tickets(
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's own support tickets, newest first."""
    subscriber_id = _subscriber_id(principal)
    return support_service.tickets.list_response(
        db,
        status=status,
        subscriber_id=subscriber_id,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.get("/support/tickets/{ticket_id}", response_model=TicketRead)
def my_ticket(
    ticket_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    return _owned_ticket(db, _subscriber_id(principal), ticket_id)


@router.post("/support/tickets", response_model=TicketRead, status_code=201)
def my_create_ticket(
    payload: MySupportTicketCreate,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Raise a ticket on the caller's own account. Identity/assignment fields
    are not accepted from the client — subscriber_id is forced to the caller."""
    subscriber_id = _subscriber_id(principal)
    ticket_payload = TicketCreate(
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        ticket_type=payload.ticket_type,
        channel=TicketChannel.web,
        subscriber_id=UUID(subscriber_id),
    )
    ticket = support_service.tickets.create(
        db, ticket_payload, actor_id=subscriber_id, request=request
    )
    from app.services.crm_ticket_push import enqueue_crm_ticket_push

    if getattr(ticket, "id", None):
        enqueue_crm_ticket_push(ticket.id, source="me_ticket_create")
    return ticket


@router.get(
    "/support/tickets/{ticket_id}/comments",
    response_model=ListResponse[TicketCommentRead],
)
def my_ticket_comments(
    ticket_id: str,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Replies on the caller's own ticket. Staff-internal notes are stripped."""
    _owned_ticket(db, _subscriber_id(principal), ticket_id)
    items = [
        c
        for c in support_service.ticket_comments.list(
            db, ticket_id, limit=limit, offset=offset
        )
        if not c.is_internal
    ]
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.post(
    "/support/tickets/{ticket_id}/comments",
    response_model=TicketCommentRead,
    status_code=201,
)
def my_add_ticket_comment(
    ticket_id: str,
    payload: MySupportCommentCreate,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Reply to the caller's own ticket. is_internal is forced False so a
    customer can never create a staff-only note."""
    subscriber_id = _subscriber_id(principal)
    _owned_ticket(db, subscriber_id, ticket_id)
    comment = support_service.tickets.create_comment(
        db,
        ticket_id,
        TicketCommentCreate(body=payload.body, is_internal=False),
        actor_id=subscriber_id,
        request=request,
    )
    from app.services.crm_ticket_push import enqueue_crm_comment_push

    if getattr(comment, "id", None):
        enqueue_crm_comment_push(comment.id, source="me_ticket_comment")
    return comment


# --- Geocoding (self-care helpers) ----------------------------------------------


@router.get("/geocode/reverse")
def my_reverse_geocode(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Resolve device coordinates to the nearest known address, for the
    opt-in "attach my location" flow. Returns display_name=None when the
    point is unknown or geocoding is disabled."""
    result = geocoding_service.reverse_geocode(db, lat, lon)
    if not result:
        return {"display_name": None}
    return {
        "display_name": result.get("display_name"),
        "latitude": result.get("latitude"),
        "longitude": result.get("longitude"),
    }


# --- Service location (pin validation) -------------------------------------------


@router.get("/location", response_model=MyLocationRead)
def my_location(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's service-location pin plus any correction requests."""
    subscriber_id = _subscriber_id(principal)
    context = location_service.get_customer_location_page_context(
        db, {"subscriber_id": subscriber_id}
    )
    return MyLocationRead(
        address_label=context.get("location_address_label"),
        current_latitude=context.get("current_latitude"),
        current_longitude=context.get("current_longitude"),
        can_submit_request=bool(context.get("can_submit_request")),
        has_address_anchor=bool(context.get("has_address_anchor")),
        pending_request=context.get("pending_request"),
        history=context.get("request_history") or [],
    )


@router.post(
    "/location-requests", response_model=MyLocationRequestRead, status_code=201
)
def my_location_request_create(
    payload: MyLocationRequestCreate,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Submit a pin correction for the caller's own service address. One
    pending request at a time; an admin reviews before anything changes."""
    subscriber_id = _subscriber_id(principal)
    return location_service.submit_request(
        db,
        subscriber_id=subscriber_id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        customer_note=payload.note,
        actor_id=subscriber_id,
        actor_name=None,
        submitted_from_ip=request.client.host if request.client else None,
    )


@router.post(
    "/location-requests/{request_id}/cancel", response_model=MyLocationRequestRead
)
def my_location_request_cancel(
    request_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    subscriber_id = _subscriber_id(principal)
    return location_service.cancel_request(
        db,
        request_id=request_id,
        subscriber_id=subscriber_id,
        actor_id=subscriber_id,
    )


# --- Subscriber contacts (self-scoped) ------------------------------------------
#
# Customer-facing parity with the web portal's /portal/contacts feature. The
# mobile app manages a subscriber's additional contacts (people, not accounts).
# All ops reuse the web service core (app/services/customer_portal_contacts.py)
# so normalization, the "at least one contact channel" rule, duplicate
# detection, and subscriber-id scoping are identical. Ownership is enforced by
# the service (allowed subscriber ids only); a contact that isn't the caller's
# returns 404 (not 403), matching the self-scoped pattern used elsewhere.


def _contact_form(
    payload: SubscriberContactCreate | SubscriberContactUpdate,
) -> contacts_service.ContactForm:
    """Normalize an API payload into the web service's ContactForm."""
    return contacts_service.normalize_contact_form(
        full_name=payload.full_name,
        phone=payload.phone,
        email=payload.email,
        whatsapp=payload.whatsapp,
        facebook=payload.facebook,
        instagram=payload.instagram,
        x_handle=payload.x_handle,
        telegram=payload.telegram,
        linkedin=payload.linkedin,
        other_social=payload.other_social,
        relationship=payload.relationship,
        contact_type=payload.contact_type,
        is_authorized=payload.is_authorized,
        receives_notifications=payload.receives_notifications,
        is_billing_contact=payload.is_billing_contact,
        notes=payload.notes,
    )


@router.get("/contacts", response_model=list[SubscriberContactRead])
def my_contacts(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's subscriber contacts, newest first."""
    return contacts_service.list_contacts(db, _customer(db, principal))


@router.post(
    "/contacts", response_model=SubscriberContactWriteResponse, status_code=201
)
def my_create_contact(
    payload: SubscriberContactCreate,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Add a contact to the caller's account. Requires at least one contact
    channel (phone, email, or a social handle). Returns the saved contact plus
    any duplicate-use warnings (advisory; the save still succeeds)."""
    try:
        contact, warnings = contacts_service.create_contact_returning(
            db, _customer(db, principal), _contact_form(payload)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SubscriberContactWriteResponse(
        contact=SubscriberContactRead.model_validate(contact), warnings=warnings
    )


@router.patch(
    "/contacts/{contact_id}", response_model=SubscriberContactWriteResponse
)
def my_update_contact(
    contact_id: str,
    payload: SubscriberContactUpdate,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Update one of the caller's own contacts (full replace). Requires at least
    one contact channel. 404 if the contact isn't the caller's."""
    customer = _customer(db, principal)
    if contacts_service.get_owned_contact(db, customer, contact_id) is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    try:
        contact, warnings = contacts_service.update_contact_returning(
            db, customer, contact_id, _contact_form(payload)
        )
    except ValueError as exc:
        message = str(exc)
        if message == "Contact not found.":
            raise HTTPException(status_code=404, detail="Contact not found") from exc
        raise HTTPException(status_code=400, detail=message) from exc
    return SubscriberContactWriteResponse(
        contact=SubscriberContactRead.model_validate(contact), warnings=warnings
    )


@router.delete("/contacts/{contact_id}", status_code=204)
def my_delete_contact(
    contact_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Delete one of the caller's own contacts. 404 if it isn't the caller's."""
    customer = _customer(db, principal)
    try:
        contacts_service.delete_contact(db, customer, contact_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Contact not found") from exc


# --- VAS wallet (feature-flagged: 404 when vas.enabled is off) -------------------


@router.get("/wallet", response_model=VasWalletOverviewResponse)
def my_wallet(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Wallet balance, settings, and recent activity."""
    subscriber_id = _subscriber_id(principal)
    overview = vas_wallet_service.wallet_overview(db, subscriber_id)
    return VasWalletOverviewResponse(**overview)


@router.post("/wallet/topup/initiate", response_model=VasTopupInitiateResponse)
def my_wallet_topup_initiate(
    payload: VasTopupInitiateRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    subscriber_id = _subscriber_id(principal)
    result = vas_wallet_service.initiate_topup(db, subscriber_id, payload.amount)
    customer = _customer(db, principal)
    return VasTopupInitiateResponse(
        **result, customer_email=customer.get("username") or None
    )


@router.post("/wallet/topup/verify", response_model=VasTopupVerifyResponse)
def my_wallet_topup_verify(
    payload: VasTopupVerifyRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    subscriber_id = _subscriber_id(principal)
    try:
        result = vas_wallet_service.verify_topup(
            db, subscriber_id, payload.reference, provider=payload.provider
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return VasTopupVerifyResponse(**result)


@router.post("/wallet/pay-bill", response_model=VasPayBillResponse)
def my_wallet_pay_bill(
    payload: VasPayBillRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Pay the caller's DotMac bill from their wallet (the only wallet→billing
    bridge; allocation matches an ordinary gateway payment)."""
    subscriber_id = _subscriber_id(principal)
    result = vas_wallet_service.pay_bill(db, subscriber_id, payload.amount)
    return VasPayBillResponse(**result)


@router.patch("/wallet/auto-deduct", response_model=VasWalletOverviewResponse)
def my_wallet_auto_deduct(
    payload: VasAutoDeductUpdate,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    subscriber_id = _subscriber_id(principal)
    vas_wallet_service.set_auto_deduct(db, subscriber_id, payload.enabled)
    overview = vas_wallet_service.wallet_overview(db, subscriber_id)
    return VasWalletOverviewResponse(**overview)


# --- VAS bill payments (Phase 2, same vas.enabled flag) --------------------------


def _txn_read(txn) -> VasTransactionRead:
    return VasTransactionRead(
        id=txn.id,
        status=txn.status,
        service_name=txn.service.name if txn.service else None,
        identifier=txn.identifier,
        variation_code=txn.variation_code,
        amount=txn.amount,
        token=vas_purchases_service.transaction_token(txn),
        error=txn.error,
        created_at=txn.created_at,
        delivered_at=txn.delivered_at,
        refunded_at=txn.refunded_at,
    )


@router.get("/vas/catalog", response_model=list[VasCategoryRead])
def my_vas_catalog(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Enabled bill-payment categories/services/plans."""
    _subscriber_id(principal)
    return vas_purchases_service.customer_catalog(db)


@router.post("/vas/verify", response_model=VasVerifyResponse)
def my_vas_verify(
    payload: VasVerifyRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Resolve a meter/smartcard/account number to the customer name before
    any money moves."""
    _subscriber_id(principal)
    result = vas_purchases_service.verify_identifier(
        db,
        service_id=payload.service_id,
        identifier=payload.identifier,
        variation_type=payload.variation_type,
    )
    return VasVerifyResponse(
        customer_name=result.get("customer_name"), address=result.get("address")
    )


@router.post("/vas/purchases", response_model=VasTransactionRead, status_code=201)
def my_vas_purchase(
    payload: VasPurchaseRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Buy airtime/data/bills from the wallet (immediate debit, requery-backed
    delivery, auto-refund to wallet on definitive failure)."""
    subscriber_id = _subscriber_id(principal)
    txn = vas_purchases_service.purchase(
        db,
        subscriber_id=subscriber_id,
        service_id=payload.service_id,
        identifier=payload.identifier,
        variation_code=payload.variation_code,
        amount=payload.amount,
        phone=payload.phone,
        confirm_duplicate=payload.confirm_duplicate,
    )
    return _txn_read(txn)


@router.get("/vas/purchases", response_model=list[VasTransactionRead])
def my_vas_purchases(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    subscriber_id = _subscriber_id(principal)
    return [
        _txn_read(txn)
        for txn in vas_purchases_service.list_transactions(
            db, subscriber_id, limit=limit
        )
    ]


@router.get("/vas/purchases/{txn_id}", response_model=VasTransactionRead)
def my_vas_purchase_detail(
    txn_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    subscriber_id = _subscriber_id(principal)
    return _txn_read(vas_purchases_service.get_transaction(db, subscriber_id, txn_id))
