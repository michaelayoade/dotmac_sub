"""Unauthenticated hand-off from an invoice PDF to Paystack."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import public_invoice_paystack_checkout
from app.services.branding_config import get_brand

router = APIRouter(tags=["web-public"])


@router.get("/pay/invoices/{invoice_id}")
def pay_invoice_with_paystack(
    invoice_id: UUID, db: Session = Depends(get_db)
) -> Response:
    """Create a current invoice checkout then redirect directly to Paystack."""
    app_url = str(get_brand().get("app_url") or "").rstrip("/")
    try:
        checkout = public_invoice_paystack_checkout.start_public_invoice_paystack_checkout(
            db,
            public_invoice_paystack_checkout.StartPublicInvoicePaystackCheckoutCommand(
                invoice_id=invoice_id,
                return_url=f"{app_url}/pay/invoices/complete",
            ),
        )
    except public_invoice_paystack_checkout.PublicInvoiceCheckoutError as exc:
        return HTMLResponse(
            f"<h1>Paystack checkout unavailable</h1><p>{exc}</p>", status_code=409
        )
    return RedirectResponse(url=checkout.authorization_url, status_code=303)


@router.get("/pay/invoices/complete", response_class=HTMLResponse)
def pay_invoice_complete() -> HTMLResponse:
    """Provider return page; webhook/reconciliation remains settlement authority."""
    return HTMLResponse(
        "<h1>Payment received</h1><p>We are confirming your payment now. "
        "Your account will update once Paystack confirms it.</p>"
    )
