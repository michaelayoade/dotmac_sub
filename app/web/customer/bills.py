"""Customer portal bill-payments hub (VAS Phase 2, feature-flagged)."""

import logging
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import vas_purchases as vas_purchases_service
from app.services import vas_wallet as vas_wallet_service
from app.web.customer.auth import get_current_customer_from_request
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
router = APIRouter(prefix="/portal", tags=["web-customer"])

logger = logging.getLogger(__name__)


def _jsonable_catalog(catalog: list[dict]) -> list[dict]:
    """Decimal -> float so service dicts can be embedded via |tojson."""
    out = []
    for bucket in catalog:
        services = []
        for service in bucket["services"]:
            services.append(
                {
                    **service,
                    "min_amount": float(service["min_amount"])
                    if service["min_amount"] is not None
                    else None,
                    "max_amount": float(service["max_amount"])
                    if service["max_amount"] is not None
                    else None,
                    "variations": [
                        {
                            **variation,
                            "amount": float(variation["amount"])
                            if variation["amount"] is not None
                            else None,
                        }
                        for variation in service["variations"]
                    ],
                }
            )
        out.append({"category": bucket["category"], "services": services})
    return out


@router.get("/bills", response_class=HTMLResponse)
def customer_bills_hub(request: Request, db: Session = Depends(get_db)) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/bills", status_code=303
        )
    subscriber_id = str(customer.get("subscriber_id") or "")
    catalog = _jsonable_catalog(vas_purchases_service.customer_catalog(db))
    overview = vas_wallet_service.wallet_overview(db, subscriber_id, limit=0)
    transactions = vas_purchases_service.list_transactions(db, subscriber_id, limit=10)
    return templates.TemplateResponse(
        "customer/bills/index.html",
        {
            "request": request,
            "customer": customer,
            "active_page": "wallet",
            "catalog": catalog,
            "balance": overview["balance"],
            "transactions": transactions,
            "token_for": vas_purchases_service.transaction_token,
            "form_error": request.query_params.get("error"),
        },
    )


@router.post("/bills/verify")
def customer_bills_verify(
    request: Request,
    payload: dict,
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    try:
        result = vas_purchases_service.verify_identifier(
            db,
            service_id=str(payload.get("service_id") or ""),
            identifier=str(payload.get("identifier") or ""),
            variation_type=payload.get("variation_type"),
        )
    except HTTPException as exc:
        return JSONResponse({"detail": str(exc.detail)}, status_code=exc.status_code)
    return JSONResponse(
        {
            "customer_name": result.get("customer_name"),
            "address": result.get("address"),
        }
    )


@router.post("/bills/purchase")
def customer_bills_purchase(
    request: Request,
    service_id: str = Form(...),
    identifier: str = Form(...),
    variation_code: str = Form(""),
    amount: str = Form(""),
    phone: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    value: Decimal | None = None
    if amount.strip():
        try:
            value = Decimal(amount.strip())
        except (InvalidOperation, ValueError):
            return RedirectResponse(
                url="/portal/bills?error=Invalid amount", status_code=303
            )
    try:
        txn = vas_purchases_service.purchase(
            db,
            subscriber_id=str(customer.get("subscriber_id") or ""),
            service_id=service_id,
            identifier=identifier,
            variation_code=variation_code.strip() or None,
            amount=value,
            phone=phone.strip() or None,
        )
    except HTTPException as exc:
        return RedirectResponse(
            url=f"/portal/bills?error={exc.detail}", status_code=303
        )
    return RedirectResponse(url=f"/portal/bills/receipt/{txn.id}", status_code=303)


@router.get("/bills/receipt/{txn_id}", response_class=HTMLResponse)
def customer_bills_receipt(
    request: Request,
    txn_id: str,
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    txn = vas_purchases_service.get_transaction(
        db, str(customer.get("subscriber_id") or ""), txn_id
    )
    return templates.TemplateResponse(
        "customer/bills/receipt.html",
        {
            "request": request,
            "customer": customer,
            "active_page": "wallet",
            "txn": txn,
            "token": vas_purchases_service.transaction_token(txn),
        },
    )
