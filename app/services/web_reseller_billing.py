"""Reseller portal billing page builders.

Pairs with ``app/web/reseller/billing_routes.py`` and the Jinja templates
under ``templates/reseller/billing/``.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from urllib.parse import quote_plus

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
            "saved_cards": reseller_portal_billing.list_payment_methods(
                db, str(context["subscriber"].id)
            ),
            "billing_activity": reseller_portal_billing.account_activity(summary),
            **summary,
        },
    )


def payment_methods(request: Request, db: Session, saved=None, error=None):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    reseller_id = str(context["reseller"].id)
    page_data = reseller_portal_billing.get_payment_methods_page(
        db, reseller_id, str(context["subscriber"].id)
    )
    return templates.TemplateResponse(
        "reseller/billing/payment_methods.html",
        {
            "request": request,
            "active_page": "billing",
            "reseller": context["reseller"],
            "current_user": context["current_user"],
            "success": saved,
            "form_error": error,
            **page_data,
        },
    )


def payment_method_set_default(request: Request, db: Session, method_id: str):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    ok = reseller_portal_billing.set_default_payment_method(
        db, str(context["subscriber"].id), method_id
    )
    if not ok:
        return RedirectResponse(
            url="/reseller/billing/payment-methods?error="
            + quote_plus("Card not found."),
            status_code=303,
        )
    return RedirectResponse(
        url="/reseller/billing/payment-methods?saved="
        + quote_plus("Default card updated."),
        status_code=303,
    )


def payment_method_remove(request: Request, db: Session, method_id: str):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    removed = reseller_portal_billing.remove_payment_method(
        db, str(context["subscriber"].id), method_id
    )
    if not removed:
        return RedirectResponse(
            url="/reseller/billing/payment-methods?error="
            + quote_plus("Card not found."),
            status_code=303,
        )
    return RedirectResponse(
        url="/reseller/billing/payment-methods?saved=" + quote_plus("Card removed."),
        status_code=303,
    )


def billing_pay_intent(
    request: Request,
    db: Session,
    amount: str,
    *,
    payment_method_id: str | None = None,
    save_card: bool = False,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    reseller_id = str(context["reseller"].id)
    try:
        intent = reseller_portal_billing.start_consolidated_payment(
            db,
            reseller_id,
            Decimal(amount),
            payment_method_id=payment_method_id or None,
            save_card=save_card,
            login_subscriber_id=str(context["subscriber"].id),
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
    # A saved card was charged server-to-server: skip the gateway popup and go
    # straight to verification, which records the payment.
    if intent.get("charged"):
        return RedirectResponse(
            url="/reseller/billing/pay/verify?reference="
            + quote_plus(intent["reference"])
            + "&provider="
            + quote_plus(intent["provider_type"]),
            status_code=303,
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


def billing_pay_verify(
    request: Request, db: Session, reference: str, provider: str | None = None
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    reseller_id = str(context["reseller"].id)
    try:
        result = reseller_portal_billing.verify_and_record_consolidated_payment(
            db, reseller_id, reference, provider=provider
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
