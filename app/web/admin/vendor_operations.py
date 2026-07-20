"""Admin review workspace for vendor quotes and purchase invoices."""

from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.auth_dependencies import require_any_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.vendor_portal_operations import (
    ReviewVendorQuoteCommand,
    vendor_portal_operations,
)
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


def _review_context(request: Request, *, quote_id: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=_actor(request),
        scope=quote_id,
        reason="vendor_quote_review",
    )


def _quote_error(exc: DomainError) -> HTTPException:
    suffix = exc.code.rsplit(".", 1)[-1]
    status_code = 404 if suffix.endswith("not_found") else 409
    return HTTPException(status_code=status_code, detail=exc.message)


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
    context = _review_context(request, quote_id=quote_id)
    db_session_adapter.release_read_transaction(db)
    try:
        vendor_portal_operations.review_quote(
            db,
            ReviewVendorQuoteCommand(
                context=context,
                quote_id=quote_id,
                reviewer_id=_actor(request),
                approve=True,
                notes=review_notes,
            ),
        )
    except DomainError as exc:
        raise _quote_error(exc) from exc
    return RedirectResponse("/admin/vendors/operations", status_code=303)


@router.post("/quotes/{quote_id}/request-revision")
def request_vendor_quote_revision(
    request: Request,
    quote_id: str,
    review_notes: str | None = Form(default=None),
    _auth: dict = Depends(_write),
    db: Session = Depends(get_db),
):
    context = _review_context(request, quote_id=quote_id)
    db_session_adapter.release_read_transaction(db)
    try:
        vendor_portal_operations.review_quote(
            db,
            ReviewVendorQuoteCommand(
                context=context,
                quote_id=quote_id,
                reviewer_id=_actor(request),
                approve=False,
                notes=review_notes,
            ),
        )
    except DomainError as exc:
        raise _quote_error(exc) from exc
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
