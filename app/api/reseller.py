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


def _profile_payload(db: Session, reseller_id: str, subscriber_id: str) -> dict:
    from app.models.auth import MFAMethod
    from app.models.subscriber import Reseller
    from app.services.common import coerce_uuid

    reseller = db.get(Reseller, coerce_uuid(reseller_id))
    if reseller is None:
        raise HTTPException(status_code=404, detail="Reseller not found")
    methods = (
        db.query(MFAMethod)
        .filter(MFAMethod.subscriber_id == coerce_uuid(subscriber_id))
        .filter(MFAMethod.is_active.is_(True))
        .order_by(MFAMethod.created_at.desc())
        .all()
    )
    return {
        "name": reseller.name,
        "code": reseller.code,
        "contact_email": reseller.contact_email,
        "contact_phone": reseller.contact_phone,
        "notes": reseller.notes,
        "mfa_enabled": any(m.enabled and m.verified_at is not None for m in methods),
        "mfa_methods": [
            {
                "id": str(m.id),
                "label": m.label,
                "method_type": m.method_type.value,
                "verified_at": m.verified_at,
                "enabled": m.enabled,
            }
            for m in methods
        ],
    }


@router.get("/profile")
def my_reseller_profile(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """The caller's reseller organization profile + MFA state."""
    reseller_id = _reseller_id(db, principal)
    return _profile_payload(db, reseller_id, str(principal["subscriber_id"]))


@router.patch("/profile")
def my_reseller_profile_update(
    payload: ResellerProfileUpdate,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Update contact details (same fields the web profile form edits)."""
    from app.models.subscriber import Reseller
    from app.services.common import coerce_uuid

    reseller_id = _reseller_id(db, principal)
    reseller = db.get(Reseller, coerce_uuid(reseller_id))
    if reseller is None:
        raise HTTPException(status_code=404, detail="Reseller not found")
    fields = payload.model_fields_set
    if "contact_email" in fields:
        reseller.contact_email = (payload.contact_email or "").strip() or None
    if "contact_phone" in fields:
        reseller.contact_phone = (payload.contact_phone or "").strip() or None
    if "notes" in fields:
        reseller.notes = (payload.notes or "").strip() or None
    db.commit()
    return _profile_payload(db, reseller_id, str(principal["subscriber_id"]))


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
    return _profile_payload(db, reseller_id, str(principal["subscriber_id"]))


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
