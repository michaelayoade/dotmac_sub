"""Service helpers for reseller portal routes."""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.services import crm_portal, customer_portal, reseller_portal
from app.web.reseller.branding import get_reseller_templates

logger = logging.getLogger(__name__)

templates = get_reseller_templates()


def _require_reseller_context(request: Request, db: Session):
    context = reseller_portal.get_context(
        db, request.cookies.get(reseller_portal.SESSION_COOKIE_NAME)
    )
    if not context:
        return None
    return context


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

    # Add open tickets count from CRM (fails silently)
    open_tickets = 0
    try:
        account_ids = [a["id"] for a in summary.get("accounts", [])]
        if account_ids:
            open_tickets = crm_portal.reseller_open_tickets_count(
                db, str(context["reseller"].id), account_ids
            )
    except Exception:
        logger.warning("Could not fetch CRM open tickets for reseller dashboard", exc_info=True)

    return templates.TemplateResponse(
        "reseller/dashboard/index.html",
        {
            "request": request,
            "active_page": "dashboard",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "summary": summary,
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
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    offset = (page - 1) * per_page
    accounts = reseller_portal.list_accounts(
        db,
        reseller_id=str(context["reseller"].id),
        limit=per_page,
        offset=offset,
        search=search,
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

    # Note: underlying service function name is legacy misspelling.
    session_token = reseller_portal.create_customer_imsubscriberation_session(
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
            {"request": request, "current_user": context["current_user"], "reseller": context["reseller"]},
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
        },
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
            {"request": request, "current_user": context["current_user"], "reseller": context["reseller"]},
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
            {"request": request, "current_user": context["current_user"], "reseller": context["reseller"]},
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
            "active_page": "reports",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "summary": summary,
        },
    )


def reseller_profile(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    return templates.TemplateResponse(
        "reseller/profile/index.html",
        {
            "request": request,
            "active_page": "profile",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
        },
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

    return templates.TemplateResponse(
        "reseller/profile/index.html",
        {
            "request": request,
            "active_page": "profile",
            "current_user": context["current_user"],
            "reseller": reseller,
            "success": "Profile updated successfully.",
        },
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
            {"request": request, "current_user": context["current_user"], "reseller": context["reseller"]},
            status_code=404,
        )

    ticket_context = crm_portal.reseller_account_tickets_context(
        request, db, account_id,
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
