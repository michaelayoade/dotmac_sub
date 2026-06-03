"""Reseller portal billing page builders.

Pairs with ``app/web/reseller/billing_routes.py`` and the Jinja templates
under ``templates/reseller/billing/``.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.services import reseller_portal, reseller_portal_billing
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


def billing_overview(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    reseller_id = str(context["reseller"].id)
    summary = reseller_portal_billing.get_billing_account_summary(db, reseller_id)
    return templates.TemplateResponse(
        "reseller/billing/index.html",
        {
            "request": request,
            "active_page": "billing",
            "reseller": context["reseller"],
            "current_user": context["current_user"],
            **summary,
        },
    )


def billing_pay_intent(
    request: Request,
    db: Session,
    amount: str,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    reseller_id = str(context["reseller"].id)
    try:
        intent = reseller_portal_billing.start_consolidated_payment(
            db, reseller_id, Decimal(amount)
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "reseller/billing/pay_error.html",
            {
                "request": request,
                "active_page": "billing",
                "reseller": context["reseller"],
                "current_user": context["current_user"],
                "error": str(exc),
            },
            status_code=400,
        )
    return templates.TemplateResponse(
        "reseller/billing/pay_checkout.html",
        {
            "request": request,
            "active_page": "billing",
            "reseller": context["reseller"],
            "current_user": context["current_user"],
            "intent": intent,
        },
    )


def billing_pay_verify(request: Request, db: Session, reference: str):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    reseller_id = str(context["reseller"].id)
    try:
        result = reseller_portal_billing.verify_and_record_consolidated_payment(
            db, reseller_id, reference
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "reseller/billing/pay_error.html",
            {
                "request": request,
                "active_page": "billing",
                "reseller": context["reseller"],
                "current_user": context["current_user"],
                "error": str(exc),
            },
            status_code=400,
        )
    return templates.TemplateResponse(
        "reseller/billing/pay_success.html",
        {
            "request": request,
            "active_page": "billing",
            "reseller": context["reseller"],
            "current_user": context["current_user"],
            "result": result,
        },
    )
