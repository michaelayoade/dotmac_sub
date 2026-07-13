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

from app.services import customer_portal_flow_payments as customer_payments
from app.services import payment_proofs, reseller_portal, reseller_portal_billing
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


def _login_subscriber_id(context) -> str | None:
    """The login subscriber's id for subscriber-keyed features (saved cards).

    None for a first-class reseller_user principal (Layer 3) — those have no
    backing subscriber, so saved-card flows degrade to empty rather than error.
    """
    subscriber = context.get("subscriber")
    return str(subscriber.id) if subscriber is not None else None


def billing_overview(
    request: Request,
    db: Session,
    allocated: str | None = None,
    error: str | None = None,
    subscriber_search: str | None = None,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    reseller_id = str(context["reseller"].id)
    summary = reseller_portal_billing.get_billing_account_summary(
        db, reseller_id, subscriber_search=subscriber_search
    )
    return templates.TemplateResponse(
        "reseller/billing/index.html",
        {
            "request": request,
            "active_page": "billing",
            "reseller": context["reseller"],
            "current_user": context["current_user"],
            "saved_cards": reseller_portal_billing.list_payment_methods(
                db, _login_subscriber_id(context), reseller_id
            ),
            "billing_activity": reseller_portal_billing.account_activity(
                db, reseller_id, summary
            ),
            "allocation_success": allocated,
            "allocation_error": error,
            "payment_options": [
                opt
                for opt in customer_payments._topup_payment_options(db)
                if opt["provider_type"] != "direct_bank_transfer"
            ],
            "bank_transfer_accounts": (
                customer_payments.enabled_direct_bank_transfer_accounts(db)
            ),
            "direct_bank_transfer_enabled": (
                customer_payments.direct_bank_transfer_enabled(db)
            ),
            **summary,
        },
    )


def payment_methods(request: Request, db: Session, saved=None, error=None):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    reseller_id = str(context["reseller"].id)
    page_data = reseller_portal_billing.get_payment_methods_page(
        db, reseller_id, _login_subscriber_id(context)
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
        db, _login_subscriber_id(context), method_id, str(context["reseller"].id)
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
        db, _login_subscriber_id(context), method_id, str(context["reseller"].id)
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


def allocate_subscriber_funds(
    request: Request, db: Session, subscriber_id: str, amount: str | None = None
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    reseller_id = str(context["reseller"].id)
    try:
        result = reseller_portal_billing.allocate_unallocated_to_subscriber(
            db, reseller_id, subscriber_id, amount=amount
        )
    except ValueError as exc:
        return RedirectResponse(
            url="/reseller/billing?error=" + quote_plus(str(exc)),
            status_code=303,
        )
    message = (
        f"Allocated {result['currency']} {result['allocated_total']:,.2f} "
        f"to {result['invoice_count']} invoice(s)."
    )
    return RedirectResponse(
        url="/reseller/billing?allocated=" + quote_plus(message),
        status_code=303,
    )


def billing_pay_intent(
    request: Request,
    db: Session,
    amount: str,
    *,
    provider: str | None = None,
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
            provider=provider or None,
            payment_method_id=payment_method_id or None,
            save_card=save_card,
            login_subscriber_id=_login_subscriber_id(context),
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


async def billing_submit_transfer_proof(
    request: Request,
    db: Session,
    *,
    file,
    amount: str,
    gross_amount: str | None = None,
    wht_rate: str | None = None,
    bank_name: str | None = None,
    reference: str | None = None,
):
    """Record a reseller bulk bank-transfer receipt (optionally net of WHT).

    ``amount`` is the net cash transferred; ``gross_amount``/``wht_rate`` capture
    any tax withheld at source. The billing account is credited the gross on
    staff verification and the WHT becomes a tracked receivable."""
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    reseller_id = str(context["reseller"].id)
    from app.services import billing as billing_service

    try:
        ba = billing_service.billing_accounts.get_for_reseller(db, reseller_id)
        path = await payment_proofs.save_proof_file(file)
        payment_proofs.submit_proof(
            db,
            None,
            submitted_by=_login_subscriber_id(context),
            amount=amount,
            currency=ba.currency,
            bank_name=bank_name,
            reference=reference,
            file_path=path,
            billing_account_id=str(ba.id),
            gross_amount=gross_amount,
            wht_rate=wht_rate,
        )
    except Exception as exc:  # noqa: BLE001 - surface as a friendly redirect
        detail = getattr(exc, "detail", None) or str(exc)
        return RedirectResponse(
            url="/reseller/billing?error="
            + quote_plus(f"Could not submit receipt: {detail}"),
            status_code=303,
        )
    return RedirectResponse(
        url="/reseller/billing?allocated="
        + quote_plus(
            "Transfer receipt submitted — we will verify it and credit your "
            "account, then you can allocate it to invoices."
        ),
        status_code=303,
    )
