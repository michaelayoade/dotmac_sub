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

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import reseller_portal
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


def _reseller_id(db: Session, principal: dict) -> str:
    """Return the caller's reseller_id, or 403 for non-reseller principals."""
    if principal.get("principal_type") != "subscriber":
        raise HTTPException(status_code=403, detail="A reseller account is required")
    reseller_id = reseller_portal.reseller_id_for_subscriber(
        db, str(principal["subscriber_id"])
    )
    if not reseller_id:
        raise HTTPException(status_code=403, detail="A reseller account is required")
    return reseller_id


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
    # external CRM (0 when unreachable), bounded to the page's accounts.
    from app.services import crm_portal

    try:
        account_ids = [
            str(a.get("id")) for a in summary.get("accounts", []) if a.get("id")
        ]
        summary["open_tickets"] = crm_portal.reseller_open_tickets_count(
            db, reseller_id, account_ids
        )
    except Exception:
        summary["open_tickets"] = 0
    return summary


@router.get("/accounts")
def my_reseller_accounts(
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """The reseller's managed customer accounts (paginated)."""
    reseller_id = _reseller_id(db, principal)
    return {
        "items": reseller_portal.list_accounts(db, reseller_id, limit, offset, search),
        "count": reseller_portal.count_accounts(db, reseller_id, search),
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
    """Consolidated billing statement for the caller's reseller account."""
    from app.services import reseller_portal_billing

    reseller_id = _reseller_id(db, principal)
    return reseller_portal_billing.get_billing_account_summary(db, reseller_id)


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
            db, reseller_id, payload.amount
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
