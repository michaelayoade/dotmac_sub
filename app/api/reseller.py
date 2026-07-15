"""Reseller self-care endpoints — scoped to the authenticated reseller.

A reseller is a ``Subscriber`` (``user_type == reseller``) linked via
``ResellerUser`` to a ``Reseller``, which owns a set of customer accounts
(``Subscriber.reseller_id``). These endpoints require ONLY authentication and
force scoping to the caller's own ``reseller_id`` — the bearer-API counterpart of
the server-rendered ``/reseller`` portal, for the mobile app.

The underlying ``reseller_portal`` services already scope every query by
``reseller_id`` and return ``None`` for an account that isn't the caller's, so
account-id endpoints simply surface that as a 404 (no IDOR).

Mounted at ``/api/v1/reseller`` with router-level ``require_user_auth`` (main.py).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.chat import ChatSessionResponse
from app.schemas.portal import (
    TechnicianLocation,
    TechnicianRatingRequest,
    TechnicianRatingResponse,
)
from app.services import chat_session as chat_session_service
from app.services import (
    quotes_mirror,
    reseller_crm_views,
    reseller_portal,
    work_orders_mirror,
)
from app.services.auth_dependencies import require_user_auth

router = APIRouter(prefix="/reseller", tags=["reseller"])


class ResellerProfileUpdate(BaseModel):
    contact_email: str | None = Field(default=None, max_length=255)
    contact_phone: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=2000)


class MfaConfirmRequest(BaseModel):
    method_id: str
    code: str = Field(min_length=4, max_length=10)


class PayIntentRequest(BaseModel):
    amount: str = Field(min_length=1, max_length=20)
    payment_method_id: str | None = Field(default=None, max_length=64)
    save_card: bool = False


class PayVerifyRequest(BaseModel):
    reference: str = Field(min_length=1, max_length=120)
    provider: str | None = Field(default=None, max_length=40)


class ServiceRequestCreate(BaseModel):
    subscriber_id: str | None = None
    contact_name: str | None = Field(default=None, max_length=160)
    contact_phone: str | None = Field(default=None, max_length=40)
    contact_email: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=2000)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    notes: str | None = Field(default=None, max_length=2000)


class ResellerQuoteRequest(BaseModel):
    """Request a map-pinned installation quote on a managed customer's behalf."""

    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    address: str | None = Field(default=None, max_length=255)
    region: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=2000)


def _reseller_id(db: Session, principal: dict) -> str:
    """Return the caller's reseller_id, or 403 for non-reseller principals.

    Handles both a legacy subscriber-backed reseller login and a first-class
    reseller_user principal (Layer 3).
    """
    principal_type = principal.get("principal_type")
    reseller_id: str | None = None
    if principal_type == "reseller_user":
        from app.models.subscriber import ResellerUser
        from app.services.common import coerce_uuid

        ru = db.get(ResellerUser, coerce_uuid(principal.get("principal_id")))
        if ru is not None and ru.is_active and ru.reseller_id is not None:
            reseller_id = str(ru.reseller_id)
    elif principal_type == "subscriber":
        reseller_id = reseller_portal.reseller_id_for_subscriber(
            db, str(principal["subscriber_id"])
        )
    if not reseller_id:
        raise HTTPException(status_code=403, detail="A reseller account is required")
    return reseller_id


@router.post("/chat/session", response_model=ChatSessionResponse)
def my_reseller_chat_session(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
    ticket_id: str | None = None,
    project_id: str | None = None,
):
    """Open (or resume) a live-chat session with DotMac support.

    Reseller chats land in the same general support pool as customer chats; the
    session is tagged with the reseller for agent context only. Pass
    ``ticket_id``/``project_id`` to scope the chat to a customer's record.
    """
    reseller_id = _reseller_id(db, principal)
    return chat_session_service.broker_reseller_session(
        db, reseller_id, principal, ticket_id=ticket_id, project_id=project_id
    )


@router.get("/dashboard")
def my_reseller_dashboard(
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """KPIs plus a first page of the caller's managed accounts."""
    reseller_id = _reseller_id(db, principal)
    summary = reseller_portal.get_dashboard_summary(db, reseller_id, limit, offset)
    # Open-ticket count mirrors the web dashboard: best-effort against the
    # external CRM (None when unreachable), bounded to the page's accounts.
    from app.services import crm_portal

    try:
        account_ids = [
            str(a.get("id")) for a in summary.get("accounts", []) if a.get("id")
        ]
        summary["open_tickets"] = crm_portal.reseller_open_tickets_count(
            db, reseller_id, account_ids
        )
    except Exception:
        summary["open_tickets"] = None
    return summary


@router.get("/accounts")
def my_reseller_accounts(
    search: str | None = None,
    status: str | None = Query(default=None, pattern="^(overdue|suspended)$"),
    order_by: str = Query(
        default="created_at", pattern="^(created_at|balance|overdue|name)$"
    ),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """The reseller's managed customer accounts (paginated, filter+sortable)."""
    reseller_id = _reseller_id(db, principal)
    return {
        "items": reseller_portal.list_accounts(
            db,
            reseller_id,
            limit,
            offset,
            search,
            status_filter=status,
            order_by=order_by,
            order_dir=order_dir,
        ),
        "count": reseller_portal.count_accounts(
            db, reseller_id, search, status_filter=status
        ),
        "limit": limit,
        "offset": offset,
    }


@router.get("/accounts/{account_id}")
def my_reseller_account(
    account_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """One managed account; 404 if it is not one of the caller's."""
    reseller_id = _reseller_id(db, principal)
    detail = reseller_portal.get_account_detail(db, reseller_id, account_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return detail


@router.get(
    "/accounts/{account_id}/work-orders/{work_order_id}/technician-location",
    response_model=TechnicianLocation,
)
def reseller_work_order_technician_location(
    account_id: str,
    work_order_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> TechnicianLocation:
    """Live technician position for a managed account's in-progress work order
    (poll for the map). 404 if the account isn't one of the caller's."""
    reseller_id = _reseller_id(db, principal)
    account = reseller_portal.owned_account(db, reseller_id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    data = work_orders_mirror.technician_location(db, str(account.id), work_order_id)
    return TechnicianLocation.model_validate(data)


@router.post(
    "/accounts/{account_id}/work-orders/{work_order_id}/rate-technician",
    response_model=TechnicianRatingResponse,
)
def reseller_rate_technician(
    account_id: str,
    work_order_id: str,
    payload: TechnicianRatingRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> TechnicianRatingResponse:
    """Rate the technician on a managed account's completed work order."""
    reseller_id = _reseller_id(db, principal)
    account = reseller_portal.owned_account(db, reseller_id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    try:
        data = work_orders_mirror.rate_technician(
            db,
            str(account.id),
            work_order_id,
            rating=payload.rating,
            comment=payload.comment,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Work order not found") from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail="Work order is not completed"
        ) from exc
    return TechnicianRatingResponse.model_validate(data)


@router.get("/profile")
def my_reseller_profile(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """The caller's reseller organization profile + MFA state."""
    reseller_id = _reseller_id(db, principal)
    profile = reseller_portal.get_profile(
        db, reseller_id, str(principal["subscriber_id"])
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="Reseller not found")
    return profile


@router.patch("/profile")
def my_reseller_profile_update(
    payload: ResellerProfileUpdate,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Update contact details (same fields the web profile form edits)."""
    reseller_id = _reseller_id(db, principal)
    profile = reseller_portal.update_profile(
        db,
        reseller_id,
        str(principal["subscriber_id"]),
        fields={
            k: getattr(payload, k)
            for k in payload.model_fields_set
            if k in {"contact_email", "contact_phone", "notes"}
        },
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="Reseller not found")
    return profile


@router.post("/profile/mfa/setup")
def my_reseller_mfa_setup(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Begin TOTP enrollment: returns the secret + otpauth URI to present in
    the app. The method stays unverified until /profile/mfa/confirm."""
    _reseller_id(db, principal)  # 403 unless the caller is a reseller
    from app.services import auth_flow as auth_flow_service

    setup = auth_flow_service.auth_flow.mfa_setup(
        db, str(principal["subscriber_id"]), "Authenticator app"
    )
    return {
        "method_id": str(setup["method_id"]),
        "secret": setup["secret"],
        "otpauth_uri": setup["otpauth_uri"],
    }


@router.post("/profile/mfa/confirm")
def my_reseller_mfa_confirm(
    payload: MfaConfirmRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Verify the first TOTP code and activate the method. The service binds
    the method to the caller, so a foreign method_id can't be confirmed."""
    reseller_id = _reseller_id(db, principal)
    from app.services import auth_flow as auth_flow_service

    try:
        auth_flow_service.auth_flow.mfa_confirm(
            db, payload.method_id, payload.code.strip(), str(principal["subscriber_id"])
        )
    except Exception:
        raise HTTPException(
            status_code=400, detail="Invalid verification code"
        ) from None
    return reseller_portal.get_profile(
        db, reseller_id, str(principal["subscriber_id"])
    ) or {"mfa_enabled": True}


@router.get("/billing")
def my_reseller_billing(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Consolidated billing statement + account-activity ledger for the caller's
    reseller account (mobile parity with the reseller web billing page)."""
    from app.services import reseller_portal_billing

    reseller_id = _reseller_id(db, principal)
    summary = reseller_portal_billing.get_billing_account_summary(db, reseller_id)
    summary["account_activity"] = reseller_portal_billing.account_activity(
        db, reseller_id, summary
    )
    return summary


@router.post("/billing/pay/intent")
def my_reseller_pay_intent(
    payload: PayIntentRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Start a consolidated payment; returns the gateway checkout context the
    app feeds into its payment webview (same shape as the web checkout)."""
    from app.services import reseller_portal_billing

    reseller_id = _reseller_id(db, principal)
    try:
        return reseller_portal_billing.start_consolidated_payment(
            db,
            reseller_id,
            payload.amount,
            payment_method_id=payload.payment_method_id,
            save_card=payload.save_card,
            login_subscriber_id=str(principal["subscriber_id"]),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/payment-methods")
def my_reseller_payment_methods(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> list[dict]:
    """The reseller's saved cards (keyed on the caller's login subscriber)."""
    from app.services import reseller_portal_billing

    _reseller_id(db, principal)  # 403 unless the caller is a reseller
    methods = reseller_portal_billing.list_payment_methods(
        db, str(principal["subscriber_id"])
    )
    return [reseller_portal_billing.payment_method_api_dict(m) for m in methods]


@router.post("/payment-methods/{method_id}/default")
def my_reseller_payment_method_default(
    method_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Make a saved card the default; 404 if it is not the caller's."""
    from app.services import reseller_portal_billing

    _reseller_id(db, principal)
    if not reseller_portal_billing.set_default_payment_method(
        db, str(principal["subscriber_id"]), method_id
    ):
        raise HTTPException(status_code=404, detail="Payment method not found")
    return {"ok": True}


@router.delete("/payment-methods/{method_id}", status_code=204)
def my_reseller_payment_method_delete(
    method_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> Response:
    """Remove a saved card; 404 if it is not the caller's."""
    from app.services import reseller_portal_billing

    _reseller_id(db, principal)
    if not reseller_portal_billing.remove_payment_method(
        db, str(principal["subscriber_id"]), method_id
    ):
        raise HTTPException(status_code=404, detail="Payment method not found")
    return Response(status_code=204)


@router.post("/billing/pay/verify")
def my_reseller_pay_verify(
    payload: PayVerifyRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Verify a gateway charge and record it against the reseller's billing
    account. The service rejects references issued to anyone else."""
    from app.services import reseller_portal_billing

    reseller_id = _reseller_id(db, principal)
    try:
        return reseller_portal_billing.verify_and_record_consolidated_payment(
            db, reseller_id, payload.reference, provider=payload.provider
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/billing/subscribers/{subscriber_id}/allocate")
def my_reseller_allocate_subscriber(
    subscriber_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Allocate unallocated reseller funds to one managed subscriber."""
    from app.services import reseller_portal_billing

    reseller_id = _reseller_id(db, principal)
    try:
        return reseller_portal_billing.allocate_unallocated_to_subscriber(
            db, reseller_id, subscriber_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/fiber-map")
def my_reseller_fiber_map(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Fiber plant map data (GeoJSON + stats) — same payload the web
    /reseller/fiber-map page renders, for the app's coverage map."""
    _reseller_id(db, principal)  # 403 unless the caller is a reseller
    from app.services import web_network_fiber

    return web_network_fiber.get_fiber_plant_map_data(db)


@router.post("/service-requests")
def my_reseller_service_request_create(
    payload: ServiceRequestCreate,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Submit a new-service / installation request (existing customer or
    lead). Serviceability is pre-flagged from fiber-plant proximity."""
    from app.services import reseller_service_requests

    reseller_id = _reseller_id(db, principal)
    return reseller_service_requests.create_request(
        db,
        reseller_id,
        subscriber_id=payload.subscriber_id,
        contact_name=payload.contact_name,
        contact_phone=payload.contact_phone,
        contact_email=payload.contact_email,
        address=payload.address,
        latitude=payload.latitude,
        longitude=payload.longitude,
        notes=payload.notes,
    )


@router.get("/service-requests")
def my_reseller_service_requests(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """The caller's submitted service requests, newest first."""
    from app.services import reseller_service_requests

    reseller_id = _reseller_id(db, principal)
    return {
        "items": reseller_service_requests.list_for_reseller(
            db, reseller_id, limit, offset
        )
    }


@router.get("/accounts/{account_id}/tickets")
def my_reseller_account_tickets(
    account_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """CRM support tickets for one managed account.

    CRM unavailability is a soft failure (empty list + flag), mirroring the
    web portal: the reseller can still see the rest of the account."""
    reseller_id = _reseller_id(db, principal)
    detail = reseller_portal.get_account_detail(
        db, reseller_id=reseller_id, account_id=account_id
    )
    if not detail:
        raise HTTPException(status_code=404, detail="Account not found")

    from app.services import crm_portal
    from app.services.crm_client import CRMClientError

    try:
        crm_sub_id = crm_portal.resolve_crm_subscriber_id(db, account_id)
        tickets = (
            crm_portal.get_crm_client().list_tickets(subscriber_id=crm_sub_id)
            if crm_sub_id
            else []
        )
    except CRMClientError:
        return {"items": [], "crm_available": False}

    items = [
        {
            "id": str(t.get("id") or t.get("name") or ""),
            "subject": t.get("subject") or t.get("title") or "Ticket",
            "status": t.get("status"),
            "priority": t.get("priority"),
            "created_at": t.get("created_at") or t.get("creation"),
            "updated_at": t.get("updated_at") or t.get("modified"),
        }
        for t in tickets
    ]
    return {"items": items, "crm_available": True}


@router.post("/accounts/{account_id}/impersonate")
def my_reseller_impersonate(
    account_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Mint a short-lived, READ-ONLY customer token for "view as customer".

    Enforcement lives in the auth layer (any non-GET under an impersonation
    token is 403), the grant is audited, and the session lapses in 15 minutes.
    """
    reseller_id = _reseller_id(db, principal)
    return reseller_portal.create_customer_impersonation_token(
        db,
        reseller_id,
        account_id,
        acting_subscriber_id=str(principal["subscriber_id"]),
    )


@router.get("/accounts/{account_id}/invoices")
def my_reseller_account_invoices(
    account_id: str,
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Invoices for one managed account; 404 if it is not the caller's."""
    reseller_id = _reseller_id(db, principal)
    invoices = reseller_portal.list_account_invoices(
        db, reseller_id, account_id, limit, offset
    )
    if invoices is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"items": invoices, "limit": limit, "offset": offset}


@router.get("/accounts/{account_id}/invoices/{invoice_id}")
def my_reseller_invoice(
    account_id: str,
    invoice_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """One invoice on a managed account; 404 if account or invoice isn't theirs."""
    reseller_id = _reseller_id(db, principal)
    invoice = reseller_portal.get_invoice_detail(
        db, reseller_id, account_id, invoice_id
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


@router.get("/revenue")
def my_reseller_revenue(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Revenue summary — invoice amounts by month and status, last 12 months."""
    reseller_id = _reseller_id(db, principal)
    return reseller_portal.get_revenue_summary(db, reseller_id)


# ── Sales/Quotes + installation tracking across the reseller's customers ──────


@router.get("/quotes")
def my_reseller_quotes(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Self-serve installation quotes across all the reseller's customers,
    each row tagged with its account. Served from the local CRM mirror, or
    natively behind ``quotes_native_read_enabled`` (Phase 3 §4.2)."""
    reseller_id = _reseller_id(db, principal)
    return reseller_crm_views.quotes_for_reseller(db, reseller_id)


@router.get("/projects")
def my_reseller_projects(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Installation/projects across all the reseller's customers (stage +
    progress). Served from the local mirror, or natively behind
    ``projects_native_read_enabled`` (Phase 3 §4.2)."""
    reseller_id = _reseller_id(db, principal)
    return reseller_crm_views.projects_for_reseller(db, reseller_id)


@router.get("/work-orders")
def my_reseller_work_orders(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Field-service work orders across all the reseller's customers (technician,
    schedule, ETA, status), from the local mirror."""
    reseller_id = _reseller_id(db, principal)
    return reseller_crm_views.work_orders_for_reseller(db, reseller_id)


@router.post("/accounts/{account_id}/quote-request", status_code=201)
def my_reseller_quote_request(
    account_id: str,
    payload: ResellerQuoteRequest,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Request a map-pinned installation quote on a managed customer's behalf.
    404 if the account isn't one of the reseller's (no IDOR)."""
    reseller_id = _reseller_id(db, principal)
    account = reseller_portal._get_customer_account(db, reseller_id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return quotes_mirror.request_quote(
        db,
        str(account.id),
        latitude=payload.latitude,
        longitude=payload.longitude,
        address=payload.address,
        region=payload.region,
        note=payload.note,
    )
