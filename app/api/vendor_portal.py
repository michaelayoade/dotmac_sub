"""Native vendor portal API backed by Sub domain tables."""

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.common import ListResponse
from app.schemas.vendor_portal import (
    VendorAsBuiltCreate,
    VendorQuoteCreate,
    VendorQuoteLineCreate,
    VendorQuoteLineUpdate,
    VendorRouteRevisionCreate,
)
from app.schemas.vendor_purchase_invoice import (
    VendorPurchaseInvoiceCreate,
    VendorPurchaseInvoiceLineCreate,
    VendorPurchaseInvoiceLineUpdate,
    VendorPurchaseInvoiceRead,
    VendorPurchaseInvoiceUpdate,
)
from app.services.field.vendor_auth import (
    require_native_vendor_context,
    require_scoped_permission,
)
from app.services.vendor_portal_operations import vendor_portal_operations
from app.services.vendor_purchase_invoices import vendor_purchase_invoices

router = APIRouter(
    prefix="/vendor",
    tags=["vendor-portal"],
    dependencies=[Depends(require_scoped_permission)],
)


def _vendor_id(context: dict) -> str:
    return str(context["native_vendor_id"])


@router.get("/projects/available")
def list_available_projects(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    items = vendor_portal_operations.list_projects(
        db, _vendor_id(context), available=True, limit=limit, offset=offset
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.get("/projects/mine")
def list_my_projects(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    items = vendor_portal_operations.list_projects(
        db, _vendor_id(context), available=False, limit=limit, offset=offset
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.post("/quotes", status_code=status.HTTP_201_CREATED)
def create_quote(
    payload: VendorQuoteCreate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return vendor_portal_operations.create_quote(
        db,
        payload,
        vendor_id=_vendor_id(context),
        user_id=str(context["principal_id"]),
    )


@router.get("/quotes/{quote_id}")
def get_quote(
    quote_id: str,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return vendor_portal_operations.get_quote(db, quote_id, _vendor_id(context))


@router.post("/quotes/{quote_id}/line-items", status_code=status.HTTP_201_CREATED)
def add_quote_line(
    quote_id: str,
    payload: VendorQuoteLineCreate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return vendor_portal_operations.add_quote_line(
        db, quote_id, payload, _vendor_id(context)
    )


@router.patch("/quotes/{quote_id}/line-items/{line_id}")
def update_quote_line(
    quote_id: str,
    line_id: str,
    payload: VendorQuoteLineUpdate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return vendor_portal_operations.update_quote_line(
        db, quote_id, line_id, payload, _vendor_id(context)
    )


@router.delete("/quotes/{quote_id}/line-items/{line_id}")
def delete_quote_line(
    quote_id: str,
    line_id: str,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return vendor_portal_operations.delete_quote_line(
        db, quote_id, line_id, _vendor_id(context)
    )


@router.post("/quotes/{quote_id}/submit")
def submit_quote(
    quote_id: str,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return vendor_portal_operations.submit_quote(db, quote_id, _vendor_id(context))


@router.post(
    "/quotes/{quote_id}/route-revisions", status_code=status.HTTP_201_CREATED
)
def create_route_revision(
    quote_id: str,
    payload: VendorRouteRevisionCreate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return vendor_portal_operations.create_route_revision(
        db, quote_id, payload, _vendor_id(context)
    )


@router.post("/route-revisions/{revision_id}/submit")
def submit_route_revision(
    revision_id: str,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return vendor_portal_operations.submit_route_revision(
        db, revision_id, _vendor_id(context), str(context["principal_id"])
    )


@router.post("/as-built", status_code=status.HTTP_201_CREATED)
def submit_as_built(
    payload: VendorAsBuiltCreate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return vendor_portal_operations.submit_as_built(
        db, payload, _vendor_id(context), str(context["principal_id"])
    )


@router.get(
    "/purchase-invoices", response_model=ListResponse[VendorPurchaseInvoiceRead]
)
def list_purchase_invoices(
    project_id: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    items = vendor_purchase_invoices.list(
        db,
        vendor_id=_vendor_id(context),
        project_id=project_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.post(
    "/purchase-invoices",
    response_model=VendorPurchaseInvoiceRead,
    status_code=status.HTTP_201_CREATED,
)
def create_purchase_invoice(
    payload: VendorPurchaseInvoiceCreate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return vendor_purchase_invoices.create(
        db,
        payload,
        vendor_id=_vendor_id(context),
        created_by_system_user_id=str(context["principal_id"]),
    )


@router.get(
    "/purchase-invoices/{invoice_id}", response_model=VendorPurchaseInvoiceRead
)
def get_purchase_invoice(
    invoice_id: str,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return vendor_purchase_invoices.get(
        db, invoice_id, vendor_id=_vendor_id(context)
    )


@router.patch(
    "/purchase-invoices/{invoice_id}", response_model=VendorPurchaseInvoiceRead
)
def update_purchase_invoice(
    invoice_id: str,
    payload: VendorPurchaseInvoiceUpdate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return vendor_purchase_invoices.update(
        db, invoice_id, payload, vendor_id=_vendor_id(context)
    )


@router.post(
    "/purchase-invoices/{invoice_id}/line-items",
    response_model=VendorPurchaseInvoiceRead,
    status_code=status.HTTP_201_CREATED,
)
def add_purchase_invoice_line(
    invoice_id: str,
    payload: VendorPurchaseInvoiceLineCreate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return vendor_purchase_invoices.add_line(
        db, invoice_id, payload, vendor_id=_vendor_id(context)
    )


@router.patch(
    "/purchase-invoices/{invoice_id}/line-items/{line_id}",
    response_model=VendorPurchaseInvoiceRead,
)
def update_purchase_invoice_line(
    invoice_id: str,
    line_id: str,
    payload: VendorPurchaseInvoiceLineUpdate,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return vendor_purchase_invoices.update_line(
        db, invoice_id, line_id, payload, vendor_id=_vendor_id(context)
    )


@router.delete(
    "/purchase-invoices/{invoice_id}/line-items/{line_id}",
    response_model=VendorPurchaseInvoiceRead,
)
def delete_purchase_invoice_line(
    invoice_id: str,
    line_id: str,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return vendor_purchase_invoices.delete_line(
        db, invoice_id, line_id, vendor_id=_vendor_id(context)
    )


@router.post(
    "/purchase-invoices/{invoice_id}/attachment",
    response_model=VendorPurchaseInvoiceRead,
)
async def upload_purchase_invoice_attachment(
    invoice_id: str,
    attachment: UploadFile = File(...),
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    content = await attachment.read()
    return vendor_purchase_invoices.upload_attachment(
        db,
        invoice_id,
        vendor_id=_vendor_id(context),
        file_name=attachment.filename or "invoice.pdf",
        content_type=attachment.content_type,
        content=content,
    )


@router.get("/purchase-invoices/{invoice_id}/attachment")
def download_purchase_invoice_attachment(
    invoice_id: str,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    file, stream = vendor_purchase_invoices.attachment_file(
        db, invoice_id, vendor_id=_vendor_id(context)
    )
    return StreamingResponse(
        stream.chunks,
        media_type=stream.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{file.original_filename}"'
        },
    )


@router.post(
    "/purchase-invoices/{invoice_id}/submit",
    response_model=VendorPurchaseInvoiceRead,
)
def submit_purchase_invoice(
    invoice_id: str,
    context: dict = Depends(require_native_vendor_context),
    db: Session = Depends(get_db),
):
    return vendor_purchase_invoices.submit(
        db, invoice_id, vendor_id=_vendor_id(context)
    )
