"""Admin review workspace for vendor quotes and purchase invoices."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.auth_dependencies import require_any_permission
from app.services.vendor_portal_operations import vendor_portal_operations
from app.services.vendor_purchase_invoices import vendor_purchase_invoices

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/vendors/operations", tags=["web-admin-vendor-operations"])
_read = require_any_permission("inventory:read", "finance:ap:read")
_write = require_any_permission("inventory:write", "finance:ap:write")


def _ctx(request: Request, db: Session) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "vendor-operations",
        "active_menu": "operations",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _actor(request: Request) -> str:
    auth = getattr(request.state, "auth", {}) or {}
    return str(auth.get("principal_id") or auth.get("person_id") or "")


@router.get("", response_class=HTMLResponse)
def vendor_operations_queue(
    request: Request,
    _auth: dict = Depends(_read),
    db: Session = Depends(get_db),
):
    context = _ctx(request, db)
    context.update(
        {
            "quotes": vendor_portal_operations.list_reviewable_quotes(db),
            "invoices": vendor_purchase_invoices.list(
                db, status="submitted", limit=200, offset=0
            ),
        }
    )
    return templates.TemplateResponse("admin/vendors/operations.html", context)


@router.post("/quotes/{quote_id}/approve")
def approve_vendor_quote(
    request: Request,
    quote_id: str,
    review_notes: str | None = Form(default=None),
    _auth: dict = Depends(_write),
    db: Session = Depends(get_db),
):
    vendor_portal_operations.review_quote(
        db,
        quote_id,
        reviewer_id=_actor(request),
        approve=True,
        notes=review_notes,
    )
    return RedirectResponse("/admin/vendors/operations", status_code=303)


@router.post("/quotes/{quote_id}/request-revision")
def request_vendor_quote_revision(
    request: Request,
    quote_id: str,
    review_notes: str | None = Form(default=None),
    _auth: dict = Depends(_write),
    db: Session = Depends(get_db),
):
    vendor_portal_operations.review_quote(
        db,
        quote_id,
        reviewer_id=_actor(request),
        approve=False,
        notes=review_notes,
    )
    return RedirectResponse("/admin/vendors/operations", status_code=303)


@router.post("/invoices/{invoice_id}/approve")
def approve_vendor_invoice(
    request: Request,
    invoice_id: str,
    review_notes: str | None = Form(default=None),
    _auth: dict = Depends(_write),
    db: Session = Depends(get_db),
):
    vendor_purchase_invoices.approve(
        db,
        invoice_id,
        reviewer_system_user_id=_actor(request),
        review_notes=review_notes,
    )
    return RedirectResponse("/admin/vendors/operations", status_code=303)


@router.post("/invoices/{invoice_id}/request-revision")
def request_vendor_invoice_revision(
    request: Request,
    invoice_id: str,
    review_notes: str | None = Form(default=None),
    _auth: dict = Depends(_write),
    db: Session = Depends(get_db),
):
    vendor_purchase_invoices.reject(
        db,
        invoice_id,
        reviewer_system_user_id=_actor(request),
        review_notes=review_notes,
    )
    return RedirectResponse("/admin/vendors/operations", status_code=303)
