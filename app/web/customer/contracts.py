"""Customer portal contract signing routes."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.contracts import contract_signatures
from app.web.customer.auth import get_current_customer_from_request

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/portal/service-orders", tags=["web-customer-contracts"])


@router.get("/{order_id}/contract", response_class=HTMLResponse)
def view_contract(
    request: Request,
    order_id: str,
    db: Session = Depends(get_db),
):
    """Display contract for signing."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url=f"/portal/auth/login?next=/portal/service-orders/{order_id}/contract",
            status_code=303,
        )

    result = contract_signatures.get_contract_context(
        db, order_id, customer.get("account_id")
    )

    if "redirect" in result:
        return RedirectResponse(url=result["redirect"], status_code=303)

    return templates.TemplateResponse(
        "customer/contracts/sign.html",
        {
            "request": request,
            "customer": customer,
            "service_order": result["service_order"],
            "contract_html": result["contract_html"],
            "document_id": result["document_id"],
            "prefill_name": customer.get("current_user", {}).get("name", ""),
            "prefill_email": customer.get("current_user", {}).get("email", ""),
            "active_page": "service-orders",
        },
    )


@router.post("/{order_id}/contract/sign")
def sign_contract(
    request: Request,
    order_id: str,
    signer_name: str = Form(...),
    signer_email: str = Form(...),
    agree: bool = Form(False),
    db: Session = Depends(get_db),
):
    """Process contract signature submission."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url=f"/portal/auth/login?next=/portal/service-orders/{order_id}/contract",
            status_code=303,
        )

    if not agree:
        raise HTTPException(
            status_code=400,
            detail="You must agree to the terms to sign the contract",
        )

    ip_address = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "")

    result = contract_signatures.sign_contract_for_customer(
        db=db,
        order_id=order_id,
        account_id=customer.get("account_id"),
        signer_name=signer_name,
        signer_email=signer_email,
        ip_address=ip_address,
        user_agent=user_agent,
    )

    if isinstance(result, str):
        return RedirectResponse(url=result, status_code=303)

    return RedirectResponse(
        url=f"/portal/service-orders/{order_id}?signed=true",
        status_code=303,
    )
