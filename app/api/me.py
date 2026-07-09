"""Customer self-care endpoints — scoped to the authenticated subscriber.

Unlike the staff-facing billing/catalog/usage list endpoints (which are gated by
`billing:read`/`catalog:read`/... permissions and take an explicit account_id),
these require ONLY authentication and force scoping to the caller's own
`subscriber_id`. They are what the customer mobile app / self-care SPA uses so a
subscriber can read their own data without holding staff permissions.

Mounted at /api/v1/me with router-level require_user_auth (see main.py).
"""

import logging
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.subscriber import Subscriber
from app.models.support import TicketChannel
from app.schemas.billing import (
    AccountBalanceResponse,
    AutopayEnableRequest,
    AutopayStatusResponse,
    BankTransferAccount,
    DirectBankTransferConfig,
    InvoiceRead,
    LedgerEntryRead,
    MyPaymentMethodRead,
    PaymentProviderOption,
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
from app.schemas.chat import ChatSessionResponse
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
from app.schemas.portal import (
    MyProjectsResponse,
    MyQuotesResponse,
    MyReferralsResponse,
    MyWorkOrdersResponse,
    PortalSessionResponse,
    QuoteDepositInitiateRequest,
    QuoteDepositInitiateResponse,
    QuoteDepositVerifyRequest,
    QuoteDepositVerifyResponse,
    QuoteItem,
    QuoteRequestCreate,
    ReferAFriendRequest,
    ReferAFriendResponse,
    TechnicianLocation,
    TechnicianRatingRequest,
    TechnicianRatingResponse,
)
from app.schemas.service_status import ServiceStatusResponse
from app.schemas.subscriber import (
    AccountDeletionRequest,
    AccountDeletionResponse,
    SubscriberContactCreate,
    SubscriberContactRead,
    SubscriberContactUpdate,
    SubscriberContactWriteResponse,
)
from app.schemas.support import (
    AttachmentMeta,
    MySupportCommentCreate,
    MySupportTicketCreate,
    TicketCommentCreate,
    TicketCommentRead,
    TicketCreate,
    TicketRead,
    TicketSatisfactionRequest,
)
from app.schemas.usage import (
    DailyUsageHistoryResponse,
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
from app.services import account_deletion as account_deletion_service
from app.services import autopay as autopay_service
from app.services import billing as billing_service
from app.services import catalog as catalog_service
from app.services import chat_session as chat_session_service
from app.services import crm_portal as crm_portal_service
from app.services import customer_location_requests as location_service
from app.services import customer_portal_contacts as contacts_service
from app.services import customer_portal_flow_addons as customer_addons
from app.services import customer_portal_flow_changes as customer_changes
from app.services import customer_portal_flow_payment_methods as customer_cards
from app.services import customer_portal_flow_payments as customer_payments
from app.services import geocoding as geocoding_service
from app.services import notification as notification_service
from app.services import portal_session as portal_session_service
from app.services import (
    projects_mirror,
    quote_deposits,
    quotes_mirror,
    referrals_mirror,
    web_support_tickets,
    work_orders_mirror,
)
from app.services import push as push_service
from app.services import support as support_service
from app.services import usage as usage_service
from app.services import usage_summary as usage_summary_service
from app.services import vas_purchases as vas_purchases_service
from app.services import vas_wallet as vas_wallet_service
from app.services.auth_dependencies import require_user_auth
from app.services.bandwidth import (
    add_directions_to_series,
    bandwidth_samples,
    with_subscriber_directions,
)
from app.services.topology import connection_status as connection_status_service

router = APIRouter(prefix="/me", tags=["me"])
logger = logging.getLogger(__name__)
PAYMENT_CHARGE_ERROR_MESSAGE = (
    "We could not charge that saved card. Please use another payment method or "
    "try again later."
)
CARD_SAVE_SUCCESS_MESSAGE = "Your card was saved for future payments."
CARD_SAVE_ERROR_MESSAGE = (
    "Payment was recorded, but we could not save this card. You can add a card "
    "from Payment Methods."
)


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
    """The caller's available customer balance (positive = credit on file)."""
    from app.services.collections import get_available_balance

    account_id = _subscriber_id(principal)
    return AccountBalanceResponse(credit_balance=get_available_balance(db, account_id))


@router.get("/ledger", response_model=ListResponse[LedgerEntryRead])
def my_ledger(
    entry_type: str | None = None,
    source: str | None = None,
    order_by: str = Query(default="effective_date"),
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


@router.get("/service-status", response_model=ServiceStatusResponse)
def my_service_status(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Truthful "is my service good, and when does it lapse" view.

    Service expiry is not date-driven: prepaid lapses on balance exhaustion
    (balance + grace/deactivation timers below), postpaid only via dunning on
    overdue invoices. `next_charge_at` is the next charge/invoice date, never an
    expiry — clients should read this endpoint (and `status`) rather than infer
    expiry from `next_billing_at`.
    """
    from app.services.service_status import build_service_status

    return build_service_status(db, _subscriber_id(principal))


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


# Calm, non-alarming fallback when the caller has no resolvable active service
# (mirrors the portal /connection surface so the two never disagree).
_NO_SERVICE_CONNECTION_STATUS = {
    "state": "connected",
    "headline": "No active service",
    "message": "We couldn't find an active service on your account to check.",
    "advice": None,
    "medium": None,
    "area_outage": False,
    "checked_at": None,
}


@router.get("/connection-status")
def my_connection_status_detail(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Richer connection status for the caller's active service (outage
    classifier P4): the per-customer last-mile verdict with area-outage blame
    suppression, from ``topology.connection_status``.

    Bearer-auth sibling of the portal ``/portal/connection/status.json`` — same
    customer-safe payload ``{state, headline, message, advice, medium,
    area_outage, checked_at}`` (no node names / signal values / internals), so
    the mobile app can reach the richer surface the cookie-only portal route
    isn't reachable for. Self-scoped: only ever the caller's own subscription.
    """
    _subscriber_id(principal)  # enforce a subscriber principal (403 otherwise)
    try:
        subscription = bandwidth_samples.get_user_active_subscription(db, principal)
    except HTTPException:
        subscription = None
    if subscription is None:
        return dict(_NO_SERVICE_CONNECTION_STATUS)
    return connection_status_service.connection_status(db, subscription)


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
    """Top-up page context: balance, limits, presets, and the pay-with selector.

    ``payment_options`` mirrors the web chooser (online gateways + a direct
    bank-transfer option) and ``direct_bank_transfer`` carries the admin bank
    account(s) so the customer can transfer and upload a receipt in-app.
    """
    ctx = customer_payments.get_topup_page(db, _customer(db, principal))
    options = [
        PaymentProviderOption(provider_type=opt["provider_type"], label=opt["label"])
        for opt in ctx.get("payment_options", [])
        if opt.get("provider_type") != "direct_bank_transfer"
    ]
    accounts = [
        BankTransferAccount(
            bank_name=acct["bank_name"],
            account_name=acct["account_name"],
            account_number=acct["account_number"],
            sort_code=acct.get("sort_code") or None,
        )
        for acct in customer_payments.enabled_direct_bank_transfer_accounts(db)
    ]
    transfer_settings = customer_payments.direct_bank_transfer_settings(db)
    direct_transfer = DirectBankTransferConfig(
        enabled=customer_payments.direct_bank_transfer_enabled(db),
        instructions=(
            transfer_settings.get("direct_bank_transfer_instructions") or None
        ),
        accounts=accounts,
    )
    return TopupPageResponse(
        provider_type=ctx["provider_type"],
        provider_public_key=ctx.get("provider_public_key"),
        prepaid_balance=ctx.get("prepaid_balance"),
        min_amount=ctx["min_amount"],
        max_amount=ctx["max_amount"],
        preset_amounts=ctx.get("preset_amounts", []),
        customer_email=ctx.get("customer_email"),
        payment_options=options,
        direct_bank_transfer=direct_transfer,
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
        result = customer_payments.create_topup_intent(
            db,
            customer,
            payload.amount,
            provider=payload.provider,
            payment_method_id=(
                str(payload.payment_method_id) if payload.payment_method_id else None
            ),
            idempotency_key=payload.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("Customer API saved-card top-up charge failed", exc_info=True)
        raise HTTPException(
            status_code=400,
            detail=PAYMENT_CHARGE_ERROR_MESSAGE,
        ) from exc
    return TopupInitiateResponse(
        intent_id=result["intent_id"],
        provider_type=result["provider_type"],
        provider_public_key=result.get("provider_public_key"),
        payment_reference=result["reference"],
        amount=result["requested_amount"],
        currency=result.get("currency", "NGN"),
        customer_email=customer["username"] or None,
        charged=result.get("charged", False),
        checkout_url=result.get("checkout_url"),
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
    card_saved: bool | None = None
    card_save_message: str | None = None
    if payload.save_card:
        try:
            customer_cards.capture_card_after_payment(
                db, customer["account_id"], payload.reference, None
            )
            card_saved = True
            card_save_message = CARD_SAVE_SUCCESS_MESSAGE
        except Exception:
            logger.warning("Customer API top-up card capture failed", exc_info=True)
            card_saved = False
            card_save_message = CARD_SAVE_ERROR_MESSAGE
    return TopupVerifyResponse(
        reference=payload.reference,
        amount=Decimal(str(result.get("amount") or "0")),
        already_recorded=result.get("already_recorded", False),
        available_balance=result.get("available_balance"),
        credit_added=result.get("credit_added"),
        card_saved=card_saved,
        card_save_message=card_save_message,
    )


@router.post("/account/deletion-request", response_model=AccountDeletionResponse)
def my_account_deletion_request(
    payload: AccountDeletionRequest | None = None,
    request: Request = None,  # type: ignore[assignment]
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Request deletion of the caller's account (in-app, App Store 5.1.1(v)).

    Records the request and notifies the customer; operations end service and
    delete personal data per the privacy policy (statutory billing/tax records
    are retained where required). The client signs the user out afterwards.
    """
    subscriber_id = _subscriber_id(principal)
    result = account_deletion_service.request_deletion(
        db,
        subscriber_id,
        reason=payload.reason if payload else None,
        request=request,
    )
    return AccountDeletionResponse(
        status=result["status"],
        requested_at=result["requested_at"],
        already_requested=result["already_requested"],
    )


@router.post("/chat/session", response_model=ChatSessionResponse)
def my_chat_session(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
    ticket_id: str | None = None,
    project_id: str | None = None,
):
    """Open (or resume) a live-chat session with support.

    The sub asserts the authenticated subscriber's identity to the CRM and
    returns an opaque visitor token plus the URLs the client uses to talk to the
    CRM chat widget directly (WebSocket for real-time, REST for send/history).

    Pass ``ticket_id`` or ``project_id`` to start the chat about that record —
    the reference rides in the session so the agent has context.
    """
    subscriber_id = _subscriber_id(principal)
    return chat_session_service.broker_customer_session(
        db, subscriber_id, ticket_id=ticket_id, project_id=project_id
    )


@router.post("/portal/session", response_model=PortalSessionResponse)
def my_portal_session(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Mint a scoped customer Portal API token (RFC #73).

    The sub asserts the authenticated subscriber's identity to the CRM and
    returns a short-lived, scoped token plus the absolute base URL the client
    uses to call the CRM Portal API directly (e.g. Refer & Earn).
    """
    subscriber_id = _subscriber_id(principal)
    return portal_session_service.broker_customer_portal_session(db, subscriber_id)


@router.get("/referrals", response_model=MyReferralsResponse)
def my_referrals(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's Refer & Earn summary — code, share link, program terms, and
    history — served from the local mirror (refreshed from the CRM lazily)."""
    subscriber_id = _subscriber_id(principal)
    return referrals_mirror.read_for_subscriber(db, subscriber_id)


@router.get("/projects", response_model=MyProjectsResponse)
def my_projects(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's installations/projects — stage timeline + progress % —
    served from the local mirror (refreshed from the CRM lazily)."""
    subscriber_id = _subscriber_id(principal)
    return projects_mirror.read_for_subscriber(db, subscriber_id)


@router.get("/work-orders", response_model=MyWorkOrdersResponse)
def my_work_orders(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's field-service work orders — technician, schedule, ETA,
    status — served from the local mirror (refreshed from the CRM lazily)."""
    subscriber_id = _subscriber_id(principal)
    return work_orders_mirror.read_for_subscriber(db, subscriber_id)


@router.get(
    "/work-orders/{work_order_id}/technician-location",
    response_model=TechnicianLocation,
)
def my_work_order_technician_location(
    work_order_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Live technician position for an in-progress work order (poll for the
    'where's my technician' map). Proxies the CRM portal; returns
    available=False when the map should be hidden."""
    subscriber_id = _subscriber_id(principal)
    crm_id = crm_portal_service.resolve_crm_subscriber_id(db, subscriber_id)
    if not crm_id:
        return TechnicianLocation(available=False, reason="not_linked")
    from app.services.crm_client import CRMClientError, get_crm_client

    try:
        data = get_crm_client(db).get_portal_technician_location(crm_id, work_order_id)
    except CRMClientError:
        # CRM unreachable / circuit open — degrade to "hidden" so the poller
        # doesn't 500-spam; the client just keeps the map hidden.
        return TechnicianLocation(available=False, reason="unavailable")
    return TechnicianLocation.model_validate(data)


@router.post(
    "/work-orders/{work_order_id}/rate-technician",
    response_model=TechnicianRatingResponse,
)
def my_rate_technician(
    work_order_id: str,
    payload: TechnicianRatingRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Rate the technician after a completed work order (1-5 + optional comment)."""
    subscriber_id = _subscriber_id(principal)
    crm_id = crm_portal_service.resolve_crm_subscriber_id(db, subscriber_id)
    if not crm_id:
        raise HTTPException(status_code=404, detail="Account not linked to CRM")
    from app.services.crm_client import CRMClientError, get_crm_client

    try:
        data = get_crm_client(db).submit_portal_technician_rating(
            crm_id, work_order_id, rating=payload.rating, comment=payload.comment
        )
    except CRMClientError as exc:
        raise HTTPException(
            status_code=503, detail="Rating service is temporarily unavailable."
        ) from exc
    return TechnicianRatingResponse.model_validate(data)


@router.get("/quotes", response_model=MyQuotesResponse)
def my_quotes(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """The caller's self-serve installation quotes — feasibility, estimate,
    deposit, status — served from the local mirror (refreshed lazily)."""
    subscriber_id = _subscriber_id(principal)
    return quotes_mirror.read_for_subscriber(db, subscriber_id)


@router.post("/quote-request", response_model=QuoteItem, status_code=201)
def my_quote_request(
    payload: QuoteRequestCreate,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Request a map-pinned installation quote. The dropped pin drives the CRM's
    feasibility check (proximity to fiber) + estimate + deposit; the result is
    mirrored locally and returned."""
    subscriber_id = _subscriber_id(principal)
    return quotes_mirror.request_quote(
        db,
        subscriber_id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        address=payload.address,
        region=payload.region,
        note=payload.note,
    )


@router.post(
    "/quotes/{quote_id}/deposit/initiate", response_model=QuoteDepositInitiateResponse
)
def my_quote_deposit_initiate(
    quote_id: str,
    payload: QuoteDepositInitiateRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Start paying a quote's installation deposit. Raises a deposit invoice and
    returns a checkout intent via the customer's existing pay flow (any provider)."""
    subscriber_id = _subscriber_id(principal)
    customer = _customer(db, principal)
    return quote_deposits.initiate_deposit(
        db,
        customer,
        subscriber_id,
        quote_id,
        provider=payload.provider,
        redirect_url=payload.redirect_url,
    )


@router.post(
    "/quotes/{quote_id}/deposit/verify", response_model=QuoteDepositVerifyResponse
)
def my_quote_deposit_verify(
    quote_id: str,
    payload: QuoteDepositVerifyRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Verify the deposit payment; on full settlement the quote is accepted in the
    CRM (which triggers the sales order + install project)."""
    subscriber_id = _subscriber_id(principal)
    customer = _customer(db, principal)
    return quote_deposits.verify_deposit(
        db,
        customer,
        subscriber_id,
        quote_id,
        reference=payload.reference,
        provider=payload.provider,
    )


@router.post("/referrals", response_model=ReferAFriendResponse, status_code=201)
def my_refer_a_friend(
    payload: ReferAFriendRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Refer a friend: capture in the CRM (source of truth), mirror locally."""
    subscriber_id = _subscriber_id(principal)
    try:
        return referrals_mirror.refer_a_friend(
            db,
            subscriber_id,
            name=payload.name,
            email=payload.email,
            phone=payload.phone,
            note=payload.note,
        )
    except referrals_mirror.ReferralError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


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
    summary["fup"] = await usage_summary_service.fup_summary(db, subscriber_id)
    return summary


@router.get("/usage-history", response_model=DailyUsageHistoryResponse)
def my_usage_history(
    days: int = Query(default=365, ge=1, le=3660),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Long-history daily upload/download series for the caller.

    Sourced from the historical daily rollup (back to 2018 for migrated
    accounts), which reaches years further than per-session accounting. Default
    window is the last 365 days; raise ``days`` for the full archive.
    """
    subscriber_id = _subscriber_id(principal)
    return usage_summary_service.get_daily_usage_history(db, subscriber_id, days=days)


# --- Live bandwidth (self-scoped, Bearer) --------------------------------------
#
# Bearer-authenticated mirrors of the cookie-only /bandwidth/my/* web routes,
# which the mobile app (Bearer) could never reach (403). Scoped to the caller's
# active subscription; throughput is returned in subscriber perspective
# (download/upload) so the app's live-bandwidth section can read it directly.


@router.get("/bandwidth/stats")
async def my_bandwidth_stats(
    period: str = Query(default="24h", pattern="^(1h|24h|7d|30d)$"),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Current + peak throughput for the caller's active subscription.

    Drives the live-bandwidth section's real-time reading and the Peak figure.
    """
    subscription = bandwidth_samples.get_user_active_subscription(db, principal)
    stats = await bandwidth_samples.get_bandwidth_stats(db, subscription.id, period)
    return with_subscriber_directions(stats)


@router.get("/bandwidth/series")
async def my_bandwidth_series(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    interval: str = Query(default="auto", pattern="^(auto|1m|5m|1h)$"),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Throughput time series for the caller's active subscription (chart)."""
    subscription = bandwidth_samples.get_user_active_subscription(db, principal)
    result = await bandwidth_samples.get_bandwidth_series(
        db, subscription.id, start_at, end_at, interval
    )
    return add_directions_to_series(result)


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


MAX_TICKET_ATTACHMENTS = 5


def _validate_attachment_count(attachments: list[UploadFile]) -> list[UploadFile]:
    files = [a for a in (attachments or []) if getattr(a, "filename", "")]
    if len(files) > MAX_TICKET_ATTACHMENTS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "too_many_attachments",
                "message": f"At most {MAX_TICKET_ATTACHMENTS} files may be attached.",
            },
        )
    return files


@router.post("/support/tickets", response_model=TicketRead, status_code=201)
def my_create_ticket(
    request: Request,
    title: str = Form(..., min_length=1, max_length=255),
    description: str | None = Form(default=None),
    priority: str = Form(default="normal"),
    ticket_type: str | None = Form(default=None),
    attachments: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Raise a ticket on the caller's own account. Identity/assignment fields
    are not accepted from the client — subscriber_id is forced to the caller.

    Accepts multipart/form-data: the JSON-equivalent fields (title, description,
    priority, ticket_type) as form fields plus a repeatable ``attachments`` file
    field (images + PDF, <=5 MB each, <=5 files). Attachments are stored locally
    on the ticket and may not sync to the CRM (the CRM push omits them)."""
    payload = MySupportTicketCreate(
        title=title,
        description=description,
        priority=priority,
        ticket_type=ticket_type,
    )
    files = _validate_attachment_count(attachments)
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
    if files:
        try:
            uploaded = web_support_tickets.upload_ticket_attachments(
                db,
                ticket_id=str(ticket.id),
                attachments=files,
                entity_type="support_ticket_attachment",
                actor_id=subscriber_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_attachment", "message": str(exc)},
            ) from exc
        support_service.tickets.add_attachments(db, str(ticket.id), uploaded)
        db.refresh(ticket)
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
    request: Request,
    body: str = Form(..., min_length=1),
    attachments: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Reply to the caller's own ticket. is_internal is forced False so a
    customer can never create a staff-only note.

    Accepts multipart/form-data: the ``body`` form field plus a repeatable
    ``attachments`` file field (images + PDF, <=5 MB each, <=5 files).
    Attachments are stored locally on the comment and may not sync to the CRM
    (the CRM push omits them)."""
    payload = MySupportCommentCreate(body=body)
    files = _validate_attachment_count(attachments)
    subscriber_id = _subscriber_id(principal)
    _owned_ticket(db, subscriber_id, ticket_id)
    uploaded: list[dict] = []
    if files:
        try:
            uploaded = web_support_tickets.upload_ticket_attachments(
                db,
                ticket_id=ticket_id,
                attachments=files,
                entity_type="support_ticket_comment_attachment",
                actor_id=subscriber_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_attachment", "message": str(exc)},
            ) from exc
    comment = support_service.tickets.create_comment(
        db,
        ticket_id,
        TicketCommentCreate(
            body=payload.body,
            is_internal=False,
            attachments=[AttachmentMeta(**item) for item in uploaded],
        ),
        actor_id=subscriber_id,
        request=request,
    )
    from app.services.crm_ticket_push import enqueue_crm_comment_push

    if getattr(comment, "id", None):
        enqueue_crm_comment_push(comment.id, source="me_ticket_comment")
    return comment


@router.post("/support/tickets/{ticket_id}/rate", response_model=TicketRead)
def my_rate_ticket(
    ticket_id: str,
    payload: TicketSatisfactionRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
):
    """Rate the support experience on the caller's own resolved/closed ticket
    (CSAT, 1-5 + optional comment). Re-rating overwrites the previous score."""
    ticket = _owned_ticket(db, _subscriber_id(principal), ticket_id)
    return support_service.tickets.set_satisfaction(
        db, ticket, rating=payload.rating, comment=payload.comment
    )


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


@router.patch("/contacts/{contact_id}", response_model=SubscriberContactWriteResponse)
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
    result = vas_wallet_service.initiate_topup(
        db, subscriber_id, payload.amount, provider=payload.provider
    )
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
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Pay the caller's DotMac bill from their wallet (the only wallet→billing
    bridge; allocation matches an ordinary gateway payment).

    Pass an ``Idempotency-Key`` header to make the debit safe against
    double-submit: a replay returns the original payment, never a second
    wallet debit."""
    subscriber_id = _subscriber_id(principal)
    result = vas_wallet_service.pay_bill(
        db, subscriber_id, payload.amount, idempotency_key=idempotency_key
    )
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
