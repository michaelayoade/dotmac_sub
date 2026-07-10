"""Service helpers for reseller portal routes."""

from __future__ import annotations

import logging
import math

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.auth import MFAMethod
from app.services import auth_flow as auth_flow_service
from app.services import (
    crm_portal,
    customer_portal,
    reseller_crm_views,
    reseller_portal,
    work_orders_mirror,
)
from app.web.reseller.branding import get_reseller_templates

logger = logging.getLogger(__name__)

templates = get_reseller_templates()


RESELLER_LOGIN_URL = "/reseller/auth/login"


def _require_reseller_context(request: Request, db: Session):
    context = reseller_portal.get_context(
        db, request.cookies.get(reseller_portal.SESSION_COOKIE_NAME)
    )
    if not context:
        return None
    # Surface admin "view as reseller" state to the layout banner (read in
    # reseller.html via request.state, like the customer portal banner). Set on
    # every guarded request so any reseller page shows the exit control.
    request.state.reseller_impersonation = {
        "active": bool(context.get("is_impersonation")),
        "return_to": context.get("return_to") or "/admin/resellers",
    }
    return context


def require_reseller_context(request: Request, db: Session = Depends(get_db)):
    """Router-level auth guard for the reseller portal.

    Attached to the portal router via ``dependencies=`` so every route is
    protected structurally rather than by per-handler convention — a new
    route cannot accidentally ship unauthenticated. Returns the reseller
    context (handlers may also depend on it directly); raises a 303 redirect
    to the login page when there is no valid reseller session.
    """
    context = _require_reseller_context(request, db)
    if not context:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": RESELLER_LOGIN_URL},
        )
    return context


def _profile_context(request: Request, context, **extra):
    subscriber = context["subscriber"]
    mfa_methods = (
        db_methods if (db_methods := extra.pop("mfa_methods", None)) is not None else []
    )
    return {
        "request": request,
        "active_page": "profile",
        "current_user": context["current_user"],
        "reseller": context["reseller"],
        "subscriber": subscriber,
        "mfa_methods": mfa_methods,
        "mfa_enabled": any(
            bool(method.enabled and method.is_active) for method in mfa_methods
        ),
        **extra,
    }


def _reseller_mfa_methods(db: Session, context) -> list[MFAMethod]:
    """MFA methods for the acting principal — subscriber-backed reseller login
    or a first-class reseller_user (Layer 3)."""
    query = db.query(MFAMethod).filter(MFAMethod.is_active.is_(True))
    if context.get("principal_type") == "reseller_user":
        query = query.filter(MFAMethod.reseller_user_id == context["principal_id"])
    else:
        query = query.filter(MFAMethod.subscriber_id == context["principal_id"])
    return query.order_by(MFAMethod.created_at.desc()).all()


def reseller_home(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    return RedirectResponse(url="/reseller/dashboard", status_code=303)


def reseller_dashboard(
    request: Request,
    db: Session,
    page: int,
    per_page: int,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    offset = (page - 1) * per_page
    summary = reseller_portal.get_dashboard_summary(
        db,
        reseller_id=str(context["reseller"].id),
        limit=per_page,
        offset=offset,
    )
    customer_statuses = reseller_portal.list_customer_connection_statuses(
        db,
        reseller_id=str(context["reseller"].id),
        limit=per_page,
        offset=offset,
    )

    # Add open tickets count from CRM. None means the CRM was unavailable.
    open_tickets: int | None = None
    try:
        account_ids = [a["id"] for a in summary.get("accounts", [])]
        if account_ids:
            open_tickets = crm_portal.reseller_open_tickets_count(
                db, str(context["reseller"].id), account_ids
            )
    except Exception:
        logger.warning(
            "Could not fetch CRM open tickets for reseller dashboard", exc_info=True
        )

    return templates.TemplateResponse(
        "reseller/dashboard/index.html",
        {
            "request": request,
            "active_page": "dashboard",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "summary": summary,
            "customer_statuses": customer_statuses,
            "open_tickets": open_tickets,
            "page": page,
            "per_page": per_page,
        },
    )


def reseller_accounts(
    request: Request,
    db: Session,
    page: int,
    per_page: int,
    search: str | None = None,
    status_filter: str | None = None,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    total = reseller_portal.count_accounts(
        db,
        reseller_id=str(context["reseller"].id),
        search=search,
        status_filter=status_filter,
    )
    total_pages = max(1, math.ceil(total / per_page)) if per_page else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    accounts = reseller_portal.list_accounts(
        db,
        reseller_id=str(context["reseller"].id),
        limit=per_page,
        offset=offset,
        search=search,
        status_filter=status_filter,
    )
    return templates.TemplateResponse(
        "reseller/accounts/index.html",
        {
            "request": request,
            "active_page": "accounts",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "accounts": accounts,
            "page": page,
            "per_page": per_page,
            "search": search or "",
            "status_filter": status_filter or "",
            "status_options": reseller_portal.ACCOUNT_LIST_STATUS_OPTIONS,
            "total": total,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        },
    )


def reseller_account_view(
    request: Request,
    db: Session,
    account_id: str,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    session_token = reseller_portal.create_customer_impersonation_session(
        db=db,
        reseller_id=str(context["reseller"].id),
        account_id=account_id,
        return_to="/reseller/accounts",
    )
    response = RedirectResponse(url="/portal/dashboard", status_code=303)
    response.set_cookie(
        key=customer_portal.SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=customer_portal.get_session_max_age(db),
    )
    return response


def reseller_account_detail(
    request: Request,
    db: Session,
    account_id: str,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    detail = reseller_portal.get_account_detail(
        db,
        reseller_id=str(context["reseller"].id),
        account_id=account_id,
    )
    if not detail:
        return templates.TemplateResponse(
            "reseller/errors/404.html",
            {
                "request": request,
                "current_user": context["current_user"],
                "reseller": context["reseller"],
            },
            status_code=404,
        )

    return templates.TemplateResponse(
        "reseller/accounts/detail.html",
        {
            "request": request,
            "active_page": "accounts",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "account": detail,
            "status_success": request.query_params.get("status_success"),
            "status_error": request.query_params.get("status_error"),
        },
    )


def reseller_account_status_update(
    request: Request,
    db: Session,
    account_id: str,
    action: str,
):
    from urllib.parse import quote_plus

    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    try:
        result = reseller_portal.update_customer_account_status(
            db,
            reseller_id=str(context["reseller"].id),
            account_id=account_id,
            action=action,
            actor_id=context["principal_id"],
        )
    except ValueError as exc:
        message = str(exc) or "Unsupported status action"
        return RedirectResponse(
            url=f"/reseller/accounts/{account_id}?status_error={quote_plus(message)}",
            status_code=303,
        )
    except Exception:
        logger.warning("reseller_account_status_update_failed", exc_info=True)
        return RedirectResponse(
            url=f"/reseller/accounts/{account_id}?status_error={quote_plus('Unable to update account status')}",
            status_code=303,
        )

    if not result:
        return templates.TemplateResponse(
            "reseller/errors/404.html",
            {
                "request": request,
                "current_user": context["current_user"],
                "reseller": context["reseller"],
            },
            status_code=404,
        )

    status_label = str(result.get("status") or "updated").replace("_", " ").title()
    if action.strip().lower() == "deactivate":
        status_label = "Deactivated"
    return RedirectResponse(
        url=f"/reseller/accounts/{account_id}?status_success={quote_plus(f'Account status changed to {status_label}')}",
        status_code=303,
    )


def reseller_account_invoices(
    request: Request,
    db: Session,
    account_id: str,
    page: int,
    per_page: int,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    offset = (page - 1) * per_page
    invoices = reseller_portal.list_account_invoices(
        db,
        reseller_id=str(context["reseller"].id),
        account_id=account_id,
        limit=per_page,
        offset=offset,
    )
    if invoices is None:
        return templates.TemplateResponse(
            "reseller/errors/404.html",
            {
                "request": request,
                "current_user": context["current_user"],
                "reseller": context["reseller"],
            },
            status_code=404,
        )

    return templates.TemplateResponse(
        "reseller/accounts/invoices.html",
        {
            "request": request,
            "active_page": "accounts",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "invoices": invoices,
            "account_id": account_id,
            "page": page,
            "per_page": per_page,
        },
    )


def reseller_invoice_detail(
    request: Request,
    db: Session,
    account_id: str,
    invoice_id: str,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    invoice = reseller_portal.get_invoice_detail(
        db,
        reseller_id=str(context["reseller"].id),
        account_id=account_id,
        invoice_id=invoice_id,
    )
    if not invoice:
        return templates.TemplateResponse(
            "reseller/errors/404.html",
            {
                "request": request,
                "current_user": context["current_user"],
                "reseller": context["reseller"],
            },
            status_code=404,
        )

    return templates.TemplateResponse(
        "reseller/accounts/invoice_detail.html",
        {
            "request": request,
            "active_page": "accounts",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "invoice": invoice,
            "account_id": account_id,
        },
    )


def reseller_revenue_report(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    summary = reseller_portal.get_revenue_summary(
        db, reseller_id=str(context["reseller"].id)
    )

    return templates.TemplateResponse(
        "reseller/reports/revenue.html",
        {
            "request": request,
            "active_page": "billing",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "summary": summary,
        },
    )


def reseller_profile(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    mfa_methods = _reseller_mfa_methods(db, context)
    current_session_token = request.cookies.get(reseller_portal.SESSION_COOKIE_NAME)
    active_sessions = reseller_portal.list_reseller_sessions_for_principal(
        context["principal_id"],
        current_session_token=current_session_token,
    )
    return templates.TemplateResponse(
        "reseller/profile/index.html",
        _profile_context(
            request,
            context,
            mfa_methods=mfa_methods,
            active_sessions=active_sessions,
            other_session_count=sum(
                1 for session in active_sessions if not session["is_current"]
            ),
            success="Other portal sessions signed out."
            if request.query_params.get("sessions") == "signed-out"
            else None,
            verify_sent=request.query_params.get("verify_sent"),
        ),
    )


def reseller_profile_sign_out_other_sessions(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url=RESELLER_LOGIN_URL, status_code=303)
    current_session_token = request.cookies.get(reseller_portal.SESSION_COOKIE_NAME)
    reseller_portal.revoke_other_reseller_sessions_for_principal(
        context["principal_id"],
        current_session_token,
        db=db,
    )
    return RedirectResponse(
        url="/reseller/profile?sessions=signed-out", status_code=303
    )


def reseller_resend_email_verification(request: Request, db: Session):
    """Resend the email-verification link to the reseller's login subscriber."""
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url=RESELLER_LOGIN_URL, status_code=303)
    sent = False
    # Email verification is a subscriber-account concept; a first-class
    # reseller_user principal has no backing subscriber, so this is a no-op
    # (the profile template only shows the verify card when a subscriber exists).
    subscriber = context["subscriber"]
    if subscriber is not None:
        try:
            sent = auth_flow_service.send_email_verification(db, str(subscriber.id))
        except Exception:
            logger.warning("reseller_resend_email_verification_failed", exc_info=True)
    return RedirectResponse(
        url=f"/reseller/profile?verify_sent={'1' if sent else '0'}",
        status_code=303,
    )


def reseller_profile_update(
    request: Request,
    db: Session,
    contact_email: str | None,
    contact_phone: str | None,
    notes: str | None,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    reseller = context["reseller"]
    if contact_email is not None:
        reseller.contact_email = contact_email.strip() or None
    if contact_phone is not None:
        reseller.contact_phone = contact_phone.strip() or None
    if notes is not None:
        reseller.notes = notes.strip() or None
    db.commit()
    db.refresh(reseller)
    mfa_methods = _reseller_mfa_methods(db, context)

    return templates.TemplateResponse(
        "reseller/profile/index.html",
        _profile_context(
            request,
            {**context, "reseller": reseller},
            mfa_methods=mfa_methods,
            success="Profile updated successfully.",
        ),
    )


def reseller_mfa_setup(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    if context["principal_type"] == "reseller_user":
        setup = auth_flow_service.auth_flow.reseller_mfa_setup(
            db, context["principal_id"], "Authenticator app"
        )
    else:
        setup = auth_flow_service.auth_flow.mfa_setup(
            db, context["principal_id"], "Authenticator app"
        )
    return templates.TemplateResponse(
        "reseller/profile/mfa_setup.html",
        {
            "request": request,
            "active_page": "profile",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "method_id": setup["method_id"],
            "secret_key": setup["secret"],
            "otpauth_uri": setup["otpauth_uri"],
        },
    )


def reseller_mfa_confirm(request: Request, db: Session, method_id: str, code: str):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    try:
        if context["principal_type"] == "reseller_user":
            method = auth_flow_service.auth_flow.reseller_mfa_confirm(
                db, method_id, code.strip(), context["principal_id"]
            )
        else:
            method = auth_flow_service.auth_flow.mfa_confirm(
                db, method_id, code.strip(), context["principal_id"]
            )
    except Exception:
        return templates.TemplateResponse(
            "reseller/profile/mfa_setup.html",
            {
                "request": request,
                "active_page": "profile",
                "current_user": context["current_user"],
                "reseller": context["reseller"],
                "method_id": method_id,
                "secret_key": "",
                "otpauth_uri": "",
                "error": "Invalid verification code. Please try again.",
            },
            status_code=401,
        )

    recovery_codes = (
        auth_flow_service.generate_mfa_recovery_codes(db, method)
        if getattr(method, "id", None)
        else []
    )
    if recovery_codes:
        return templates.TemplateResponse(
            "reseller/profile/mfa_setup.html",
            {
                "request": request,
                "active_page": "profile",
                "current_user": context["current_user"],
                "reseller": context["reseller"],
                "method_id": method_id,
                "secret_key": "",
                "otpauth_uri": "",
                "recovery_codes": recovery_codes,
                "continue_url": "/reseller/profile",
            },
        )

    mfa_methods = _reseller_mfa_methods(db, context)
    return templates.TemplateResponse(
        "reseller/profile/index.html",
        _profile_context(
            request,
            context,
            mfa_methods=mfa_methods,
            success="Two-factor authentication enabled.",
        ),
    )


def reseller_account_tickets(
    request: Request,
    db: Session,
    account_id: str,
):
    """Show CRM tickets for a reseller's customer account."""
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    # Verify reseller owns this account
    detail = reseller_portal.get_account_detail(
        db,
        reseller_id=str(context["reseller"].id),
        account_id=account_id,
    )
    if not detail:
        return templates.TemplateResponse(
            "reseller/errors/404.html",
            {
                "request": request,
                "current_user": context["current_user"],
                "reseller": context["reseller"],
            },
            status_code=404,
        )

    ticket_context = crm_portal.reseller_account_tickets_context(
        request,
        db,
        account_id,
        current_user=context["current_user"],
        reseller=context["reseller"],
    )
    ticket_context["account"] = detail
    return templates.TemplateResponse(
        "reseller/accounts/tickets.html",
        ticket_context,
    )


def reseller_fiber_map(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    from app.services import web_network_fiber as fiber_service

    map_data = fiber_service.get_fiber_plant_map_data(db)

    return templates.TemplateResponse(
        "reseller/network/fiber-map.html",
        {
            "request": request,
            "active_page": "fiber-map",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            **map_data,
            "read_only": True,
        },
    )


def reseller_quotes_page(request: Request, db: Session):
    """Sales/Quotes across the reseller's customers: quotes, installs, visits."""
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url=RESELLER_LOGIN_URL, status_code=303)
    from app.services import reseller_crm_views

    reseller_id = str(context["reseller"].id)
    return templates.TemplateResponse(
        "reseller/quotes/index.html",
        {
            "request": request,
            "active_page": "quotes",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "quotes": reseller_crm_views.quotes_for_reseller(db, reseller_id),
            "projects": reseller_crm_views.projects_for_reseller(db, reseller_id),
            "work_orders": reseller_crm_views.work_orders_for_reseller(db, reseller_id),
        },
    )


def reseller_service_requests_page(request: Request, db: Session):
    """New-service / installation requests: list + submission form."""
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url=RESELLER_LOGIN_URL, status_code=303)
    from app.services import reseller_service_requests

    items = reseller_service_requests.list_for_reseller(
        db, str(context["reseller"].id), limit=100, offset=0
    )
    return templates.TemplateResponse(
        "reseller/service-requests/index.html",
        {
            "request": request,
            "active_page": "service-requests",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "service_requests": items,
            "form_error": request.query_params.get("error"),
            "submitted": request.query_params.get("submitted") == "1",
        },
    )


def reseller_service_request_create(
    request: Request,
    db: Session,
    *,
    contact_name: str,
    contact_phone: str,
    contact_email: str,
    address: str,
    latitude: str,
    longitude: str,
    notes: str,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url=RESELLER_LOGIN_URL, status_code=303)
    from urllib.parse import quote_plus

    from app.services import reseller_service_requests

    def _coord(raw: str, low: float, high: float) -> float | None:
        try:
            value = float(raw.strip())
        except (TypeError, ValueError):
            return None
        return value if low <= value <= high else None

    lat = _coord(latitude, -90.0, 90.0)
    lon = _coord(longitude, -180.0, 180.0)
    if (lat is None) != (lon is None):
        lat = lon = None

    try:
        reseller_service_requests.create_request(
            db,
            str(context["reseller"].id),
            subscriber_id=None,
            contact_name=contact_name,
            contact_phone=contact_phone,
            contact_email=contact_email,
            address=address,
            latitude=lat,
            longitude=lon,
            notes=notes,
        )
    except HTTPException as exc:
        return RedirectResponse(
            url=f"/reseller/service-requests?error={quote_plus(str(exc.detail))}",
            status_code=303,
        )
    return RedirectResponse(
        url="/reseller/service-requests?submitted=1", status_code=303
    )


def reseller_vas_page(request: Request, db: Session):
    """Reseller VAS: float wallet, sell-for-customer, commissions."""
    from app.services import vas_purchases, vas_wallet

    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url=RESELLER_LOGIN_URL, status_code=303)
    if not vas_wallet.is_enabled(db):
        raise HTTPException(status_code=404, detail="Not found")
    reseller = context["reseller"]
    wallet = vas_wallet.get_or_create_reseller_wallet(db, str(reseller.id))

    from decimal import Decimal

    from sqlalchemy import func as _func

    from app.models.vas import VasEntryCategory, VasEntryType, VasWalletEntry

    commission_total = db.query(
        _func.coalesce(_func.sum(VasWalletEntry.amount), Decimal("0.00"))
    ).filter(
        VasWalletEntry.wallet_id == wallet.id,
        VasWalletEntry.entry_type == VasEntryType.credit,
        VasWalletEntry.category == VasEntryCategory.commission,
    ).scalar() or Decimal("0.00")

    from app.web.customer.bills import _jsonable_catalog

    return templates.TemplateResponse(
        "reseller/vas/index.html",
        {
            "request": request,
            "active_page": "vas",
            "reseller": reseller,
            "balance": vas_wallet.wallet_balance(db, wallet.id),
            "commission_total": Decimal(str(commission_total)),
            "catalog": _jsonable_catalog(vas_purchases.customer_catalog(db)),
            "transactions": vas_purchases.list_reseller_transactions(
                db, str(reseller.id), limit=15
            ),
            "min_topup": 100,
            "payment_options": vas_wallet.topup_payment_options(db),
            "form_error": request.query_params.get("error"),
            "funded": request.query_params.get("funded"),
        },
    )


def reseller_vas_topup_intent(request: Request, db: Session, amount, provider=None):
    from decimal import Decimal, InvalidOperation

    from fastapi.responses import JSONResponse

    from app.services import vas_wallet

    context = _require_reseller_context(request, db)
    if not context:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return JSONResponse({"detail": "Invalid amount"}, status_code=400)
    try:
        result = vas_wallet.initiate_reseller_topup(
            db,
            str(context["reseller"].id),
            value,
            provider=(str(provider or "").strip() or None),
        )
    except HTTPException as exc:
        return JSONResponse({"detail": str(exc.detail)}, status_code=exc.status_code)
    return JSONResponse(
        {
            "provider_type": result["provider_type"],
            "provider_public_key": result["provider_public_key"],
            "reference": result["reference"],
            "currency": result["currency"],
        }
    )


def reseller_vas_topup_verify(
    request: Request, db: Session, reference: str, provider: str | None = None
):
    from app.services import vas_wallet

    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url=RESELLER_LOGIN_URL, status_code=303)
    try:
        result = vas_wallet.verify_reseller_topup(
            db,
            str(context["reseller"].id),
            reference,
            provider=(str(provider or "").strip() or None),
        )
    except (HTTPException, ValueError) as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return RedirectResponse(url=f"/reseller/vas?error={detail}", status_code=303)
    return RedirectResponse(
        url=f"/reseller/vas?funded={result['amount']}", status_code=303
    )


def reseller_vas_sell(
    request: Request,
    db: Session,
    *,
    service_id: str,
    identifier: str,
    variation_code: str,
    amount: str,
):
    from decimal import Decimal, InvalidOperation

    from app.services import vas_purchases

    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url=RESELLER_LOGIN_URL, status_code=303)
    value = None
    if amount.strip():
        try:
            value = Decimal(amount.strip())
        except (InvalidOperation, ValueError):
            return RedirectResponse(
                url="/reseller/vas?error=Invalid amount", status_code=303
            )
    try:
        vas_purchases.reseller_purchase(
            db,
            reseller_id=str(context["reseller"].id),
            service_id=service_id,
            identifier=identifier,
            variation_code=variation_code.strip() or None,
            amount=value,
        )
    except HTTPException as exc:
        return RedirectResponse(
            url=f"/reseller/vas?error={exc.detail}", status_code=303
        )
    return RedirectResponse(url="/reseller/vas?funded=sold", status_code=303)


# ─── Field-service work orders (technician map + rating, per managed account) ──


def reseller_work_orders_page(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    tracker = reseller_crm_views.work_orders_for_reseller(
        db, str(context["reseller"].id)
    )
    return templates.TemplateResponse(
        "reseller/work_orders/index.html",
        {
            "request": request,
            "active_page": "work-orders",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "tracker": tracker,
        },
    )


def reseller_technician_location(
    request: Request, db: Session, account_id: str, work_order_id: str
):
    """Live tech position for a managed account's work order (polled). Same-origin,
    reseller-session-authed; gated to accounts the reseller owns."""
    context = _require_reseller_context(request, db)
    if not context:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    account = reseller_portal.owned_account(db, str(context["reseller"].id), account_id)
    if account is None:
        return JSONResponse({"available": False, "reason": "not_found"})
    data = work_orders_mirror.technician_location(db, str(account.id), work_order_id)
    return JSONResponse(data)


def reseller_rate_technician(
    request: Request,
    db: Session,
    account_id: str,
    work_order_id: str,
    rating: int,
    comment: str,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    account = reseller_portal.owned_account(db, str(context["reseller"].id), account_id)
    status_flag = "ok"
    if account is None:
        status_flag = "error"
    else:
        try:
            work_orders_mirror.rate_technician(
                db,
                str(account.id),
                work_order_id,
                rating=max(1, min(5, rating)),
                comment=(comment or "")[:2000] or None,
            )
        except (LookupError, ValueError):
            status_flag = "error"
    return RedirectResponse(
        url=f"/reseller/work-orders?rated={status_flag}", status_code=303
    )
