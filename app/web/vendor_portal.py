"""Browser workspace for native vendor operations in Sub."""

import json
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.vendor_portal import (
    VendorAsBuiltCreate,
    VendorQuoteCreate,
    VendorQuoteLineCreate,
)
from app.schemas.vendor_purchase_invoice import (
    VendorPurchaseInvoiceCreate,
    VendorPurchaseInvoiceLineCreate,
)
from app.services.common import coerce_uuid
from app.services.field.vendor_auth import vendor_context
from app.services.vendor_portal_operations import vendor_portal_operations
from app.services.vendor_purchase_invoices import vendor_purchase_invoices
from app.web.auth.dependencies import require_web_auth

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/vendor", tags=["web-vendor-portal"])


def _context(auth: dict, db: Session) -> dict:
    context = vendor_context(db, auth)
    if not context.get("native_vendor_id"):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=409,
            detail="Vendor account is not linked to the native vendor domain",
        )
    return context


def _redirect(project_id: str, message: str | None = None) -> RedirectResponse:
    suffix = f"?message={message}" if message else ""
    return RedirectResponse(f"/vendor/projects/{project_id}{suffix}", status_code=303)


@router.get("", response_class=HTMLResponse)
def vendor_dashboard(
    request: Request,
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    vendor_id = str(context["native_vendor_id"])
    return templates.TemplateResponse(
        "vendor/dashboard.html",
        {
            "request": request,
            "vendor": context["native_vendor"],
            "available_projects": vendor_portal_operations.list_projects(
                db, vendor_id, available=True, limit=50, offset=0
            ),
            "my_projects": vendor_portal_operations.list_projects(
                db, vendor_id, available=False, limit=100, offset=0
            ),
        },
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def vendor_project_detail(
    request: Request,
    project_id: str,
    message: str | None = None,
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    vendor_id = str(context["native_vendor_id"])
    project = next(
        (
            item
            for item in vendor_portal_operations.list_projects(
                db, vendor_id, available=False, limit=500, offset=0
            )
            if str(item["id"]) == project_id
        ),
        None,
    )
    if project is None:
        available = vendor_portal_operations.list_projects(
            db, vendor_id, available=True, limit=500, offset=0
        )
        project = next((item for item in available if str(item["id"]) == project_id), None)
    if project is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Installation project not found")
    quote = vendor_portal_operations.latest_quote_for_project(
        db, str(project["id"]), vendor_id
    )
    invoice = vendor_purchase_invoices.for_project(
        db, str(project["id"]), vendor_id=vendor_id
    )
    return templates.TemplateResponse(
        "vendor/project_detail.html",
        {
            "request": request,
            "vendor": context["native_vendor"],
            "project": project,
            "quote": quote,
            "invoice": invoice,
            "message": message,
        },
    )


@router.post("/projects/{project_id}/quotes")
def vendor_create_quote(
    project_id: str,
    vat_rate_percent: Decimal = Form(default=Decimal("0")),
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    vendor_portal_operations.create_quote(
        db,
        VendorQuoteCreate(
            project_id=coerce_uuid(project_id), vat_rate_percent=vat_rate_percent
        ),
        vendor_id=str(context["native_vendor_id"]),
        user_id=str(auth["principal_id"]),
    )
    return _redirect(project_id, "Quote created")


@router.post("/projects/{project_id}/quotes/{quote_id}/lines")
def vendor_add_quote_line(
    project_id: str,
    quote_id: str,
    description: str = Form(...),
    quantity: Decimal = Form(...),
    unit_price: Decimal = Form(...),
    item_type: str | None = Form(default=None),
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    vendor_portal_operations.add_quote_line(
        db,
        quote_id,
        VendorQuoteLineCreate(
            item_type=item_type,
            description=description,
            quantity=quantity,
            unit_price=unit_price,
        ),
        str(context["native_vendor_id"]),
    )
    return _redirect(project_id, "Quote line added")


@router.post("/projects/{project_id}/quotes/{quote_id}/submit")
def vendor_submit_quote(
    project_id: str,
    quote_id: str,
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    vendor_portal_operations.submit_quote(
        db, quote_id, str(context["native_vendor_id"])
    )
    return _redirect(project_id, "Quote submitted")


@router.post("/projects/{project_id}/as-built")
def vendor_submit_as_built(
    project_id: str,
    geojson: str = Form(...),
    actual_length_meters: float | None = Form(default=None),
    variation_reason: str | None = Form(default=None),
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    vendor_portal_operations.submit_as_built(
        db,
        VendorAsBuiltCreate(
            project_id=coerce_uuid(project_id),
            geojson=json.loads(geojson),
            actual_length_meters=actual_length_meters,
            variation_reason=variation_reason,
        ),
        str(context["native_vendor_id"]),
        str(auth["principal_id"]),
    )
    return _redirect(project_id, "As-built submitted")


@router.post("/projects/{project_id}/purchase-invoices")
def vendor_create_invoice(
    project_id: str,
    invoice_number: str = Form(...),
    tax_rate_percent: Decimal = Form(default=Decimal("0")),
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    vendor_purchase_invoices.create(
        db,
        VendorPurchaseInvoiceCreate(
            project_id=coerce_uuid(project_id),
            invoice_number=invoice_number,
            tax_rate_percent=tax_rate_percent,
        ),
        vendor_id=str(context["native_vendor_id"]),
        created_by_system_user_id=str(auth["principal_id"]),
    )
    return _redirect(project_id, "Invoice created")


@router.post("/projects/{project_id}/purchase-invoices/{invoice_id}/lines")
def vendor_add_invoice_line(
    project_id: str,
    invoice_id: str,
    description: str = Form(...),
    quantity: Decimal = Form(...),
    unit_price: Decimal = Form(...),
    item_type: str | None = Form(default=None),
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    vendor_purchase_invoices.add_line(
        db,
        invoice_id,
        VendorPurchaseInvoiceLineCreate(
            item_type=item_type,
            description=description,
            quantity=quantity,
            unit_price=unit_price,
        ),
        vendor_id=str(context["native_vendor_id"]),
    )
    return _redirect(project_id, "Invoice line added")


@router.post("/projects/{project_id}/purchase-invoices/{invoice_id}/attachment")
async def vendor_upload_invoice_attachment(
    project_id: str,
    invoice_id: str,
    attachment: UploadFile = File(...),
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    vendor_purchase_invoices.upload_attachment(
        db,
        invoice_id,
        vendor_id=str(context["native_vendor_id"]),
        file_name=attachment.filename or "invoice.pdf",
        content_type=attachment.content_type,
        content=await attachment.read(),
    )
    return _redirect(project_id, "Attachment uploaded")


@router.post("/projects/{project_id}/purchase-invoices/{invoice_id}/submit")
def vendor_submit_invoice(
    project_id: str,
    invoice_id: str,
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    vendor_purchase_invoices.submit(
        db, invoice_id, vendor_id=str(context["native_vendor_id"])
    )
    return _redirect(project_id, "Invoice submitted")
