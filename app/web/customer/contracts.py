"""Customer portal contract signing routes."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.legal import LegalDocumentType
from app.models.provisioning import ServiceOrder
from app.schemas.contracts import ContractSignatureCreate
from app.services.contracts import contract_signatures
from app.services.common import coerce_uuid
from app.web.customer.auth import get_current_customer_from_request

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/portal/service-orders", tags=["web-customer-contracts"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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

    # Get the service order
    service_order = db.get(ServiceOrder, coerce_uuid(order_id))
    if not service_order:
        raise HTTPException(status_code=404, detail="Service order not found")

    # Verify the customer has access to this service order
    account_id = customer.get("account_id")
    if account_id and str(service_order.account_id) != str(account_id):
        raise HTTPException(status_code=403, detail="Access denied")

    # Check if already signed
    existing_signature = contract_signatures.get_for_service_order(db, order_id)
    if existing_signature:
        return RedirectResponse(
            url=f"/portal/service-orders/{order_id}?signed=true",
            status_code=303,
        )

    # Get the contract template
    contract_template = contract_signatures.get_contract_template(
        db, LegalDocumentType.terms_of_service
    )

    # Default contract text if no template exists
    contract_html = ""
    if contract_template and contract_template.content:
        contract_html = contract_template.content
    else:
        contract_html = """
        <h2>Service Agreement</h2>
        <p>By signing this agreement, you acknowledge and accept the following terms:</p>
        <ol>
            <li>You agree to pay all service fees as invoiced.</li>
            <li>Service is provided on a month-to-month basis unless otherwise specified.</li>
            <li>Either party may terminate with 30 days written notice.</li>
            <li>You agree to use the service in accordance with applicable laws.</li>
        </ol>
        <p>This agreement is effective upon signing and remains in effect until terminated.</p>
        """

    # Pre-fill customer info
    customer_name = customer.get("name", "")
    customer_email = customer.get("email", "")

    return templates.TemplateResponse(
        "customer/contracts/sign.html",
        {
            "request": request,
            "customer": customer,
            "service_order": service_order,
            "contract_html": contract_html,
            "document_id": str(contract_template.id) if contract_template else None,
            "prefill_name": customer_name,
            "prefill_email": customer_email,
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

    # Validate agreement checkbox
    if not agree:
        raise HTTPException(
            status_code=400,
            detail="You must agree to the terms to sign the contract",
        )

    # Get the service order
    service_order = db.get(ServiceOrder, coerce_uuid(order_id))
    if not service_order:
        raise HTTPException(status_code=404, detail="Service order not found")

    # Verify access
    account_id = customer.get("account_id")
    if account_id and str(service_order.account_id) != str(account_id):
        raise HTTPException(status_code=403, detail="Access denied")

    # Check if already signed
    existing_signature = contract_signatures.get_for_service_order(db, order_id)
    if existing_signature:
        return RedirectResponse(
            url=f"/portal/service-orders/{order_id}?already_signed=true",
            status_code=303,
        )

    # Get contract template for agreement text
    contract_template = contract_signatures.get_contract_template(
        db, LegalDocumentType.terms_of_service
    )
    agreement_text = ""
    document_id = None
    if contract_template:
        agreement_text = contract_template.content or ""
        document_id = contract_template.id
    else:
        agreement_text = "Default service agreement terms accepted."

    # Capture IP address and user agent
    ip_address = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "")

    # Create the signature record
    signature = contract_signatures.create(
        db,
        ContractSignatureCreate(
            account_id=service_order.account_id,
            service_order_id=service_order.id,
            document_id=document_id,
            signer_name=signer_name.strip(),
            signer_email=signer_email.strip(),
            ip_address=ip_address,
            user_agent=user_agent[:500] if user_agent else None,
            agreement_text=agreement_text,
            signed_at=datetime.now(timezone.utc),
        ),
    )

    return RedirectResponse(
        url=f"/portal/service-orders/{order_id}?signed=true",
        status_code=303,
    )
