"""Customer portal wallet pages (VAS Phase 1, feature-flagged)."""

import logging
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import vas_wallet as vas_wallet_service
from app.services.customer_context import optional_customer_subscriber_id
from app.web.customer.auth import get_current_customer_from_request
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
router = APIRouter(prefix="/portal", tags=["web-customer"])

logger = logging.getLogger(__name__)


def _page_context(request: Request, db: Session, customer: dict) -> dict:
    subscriber_id = str(optional_customer_subscriber_id(db, customer) or "")
    overview = vas_wallet_service.wallet_overview(db, subscriber_id)
    from app.services.collections._core import _resolve_prepaid_available_balance

    open_due = Decimal("0.00")
    try:
        available = _resolve_prepaid_available_balance(db, subscriber_id)
        if available < 0:
            open_due = -available
    except Exception:  # noqa: BLE001
        logger.warning("Could not resolve open balance for wallet page")
    return {
        "request": request,
        "customer": customer,
        "active_page": "wallet",
        **overview,
        "open_bill_due": open_due,
        "customer_email": customer.get("username") or "",
        "submitted": request.query_params.get("paid") == "1",
        "funded": request.query_params.get("funded"),
        "form_error": request.query_params.get("error"),
    }


@router.get("/wallet", response_class=HTMLResponse)
def customer_wallet_page(request: Request, db: Session = Depends(get_db)) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/wallet", status_code=303
        )
    context = _page_context(request, db, customer)
    return templates.TemplateResponse("customer/wallet/index.html", context)


@router.post("/wallet/topup/intent")
def customer_wallet_topup_intent(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    try:
        amount = Decimal(str(payload.get("amount") or "0"))
    except (InvalidOperation, ValueError):
        return JSONResponse({"detail": "Invalid amount"}, status_code=400)
    try:
        result = vas_wallet_service.initiate_topup(
            db,
            str(optional_customer_subscriber_id(db, customer) or ""),
            amount,
            provider=(str(payload.get("provider") or "").strip() or None),
        )
    except HTTPException as exc:
        return JSONResponse({"detail": str(exc.detail)}, status_code=exc.status_code)
    return JSONResponse(
        {
            "provider_type": result["provider_type"],
            "provider_public_key": result["provider_public_key"],
            "reference": result["reference"],
            "amount": str(result["amount"]),
            "currency": result["currency"],
        }
    )


@router.get("/wallet/topup/verify", response_class=HTMLResponse)
def customer_wallet_topup_verify(
    request: Request,
    reference: str,
    provider: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    try:
        result = vas_wallet_service.verify_topup(
            db,
            str(optional_customer_subscriber_id(db, customer) or ""),
            reference,
            provider=provider,
        )
    except (HTTPException, ValueError) as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return RedirectResponse(url=f"/portal/wallet?error={detail}", status_code=303)
    return RedirectResponse(
        url=f"/portal/wallet?funded={result['amount']}", status_code=303
    )


@router.post("/wallet/pay-bill")
def customer_wallet_pay_bill(
    request: Request,
    amount: str = Form(...),
    idempotency_key: str = Form(default=""),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return RedirectResponse(
            url="/portal/wallet?error=Invalid amount", status_code=303
        )
    try:
        vas_wallet_service.pay_bill(
            db,
            str(optional_customer_subscriber_id(db, customer) or ""),
            value,
            idempotency_key=(idempotency_key or "").strip() or None,
        )
    except HTTPException as exc:
        # ``detail`` may be a structured ``{code, message}`` dict now.
        detail = exc.detail
        message = detail.get("message") if isinstance(detail, dict) else str(detail)
        return RedirectResponse(url=f"/portal/wallet?error={message}", status_code=303)
    return RedirectResponse(url="/portal/wallet?paid=1", status_code=303)


@router.post("/wallet/auto-deduct")
def customer_wallet_auto_deduct(
    request: Request,
    enabled: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    vas_wallet_service.set_auto_deduct(
        db,
        str(optional_customer_subscriber_id(db, customer) or ""),
        enabled.strip().lower() in {"1", "true", "on", "yes"},
    )
    return RedirectResponse(url="/portal/wallet", status_code=303)
