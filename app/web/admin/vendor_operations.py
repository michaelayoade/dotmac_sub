"""Admin review workspace for vendor projects, quotes, and purchase invoices."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import (
    vendor_as_built_review_proposals,
    vendor_project_review_proposals,
)
from app.services.auth_dependencies import (
    can,
    require_any_permission,
    require_permission,
)
from app.services.vendor_portal_operations import vendor_portal_operations
from app.services.vendor_purchase_invoices import vendor_purchase_invoices

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/vendors/operations", tags=["web-admin-vendor-operations"])
_read = require_any_permission("inventory:read", "finance:ap:read")
_write = require_any_permission("inventory:write", "finance:ap:write")
_project_write = require_permission("inventory:write")


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
    message: str | None = None,
    _auth: dict = Depends(_read),
    db: Session = Depends(get_db),
):
    context = _ctx(request, db)
    show_field_reviews = can(request, "inventory:read")
    context.update(
        {
            "message": message,
            "show_field_reviews": show_field_reviews,
            "projects": (
                vendor_portal_operations.list_reviewable_projects(db)
                if show_field_reviews
                else []
            ),
            "as_builts": (
                vendor_portal_operations.list_reviewable_as_builts(db)
                if show_field_reviews
                else []
            ),
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


@router.post("/projects/{project_id}/{action}/preview", response_class=HTMLResponse)
def preview_vendor_project_review(
    request: Request,
    project_id: str,
    action: str,
    reason: str | None = Form(default=None),
    _auth: dict = Depends(_project_write),
    db: Session = Depends(get_db),
):
    proposal = vendor_project_review_proposals.issue_review(
        db,
        project_id=project_id,
        action=action,
        actor_id=_actor(request),
        reason=reason,
    )
    context = _ctx(request, db)
    context["proposal"] = proposal
    return templates.TemplateResponse(
        "admin/vendors/project_review_confirm.html", context
    )


@router.post("/projects/{project_id}/{action}/confirm")
def confirm_vendor_project_review(
    request: Request,
    project_id: str,
    action: str,
    confirmation_token: str = Form(...),
    _auth: dict = Depends(_project_write),
    db: Session = Depends(get_db),
):
    result = vendor_project_review_proposals.confirm_review(
        db,
        confirmation_token=confirmation_token,
        project_id=project_id,
        action=action,
        actor_id=_actor(request),
    )
    label = "verified" if result.action == "verify" else "returned for rework"
    return RedirectResponse(
        f"/admin/vendors/operations?message=Project+{label}", status_code=303
    )


@router.post("/as-built/{as_built_id}/{action}/preview", response_class=HTMLResponse)
def preview_vendor_as_built_review(
    request: Request,
    as_built_id: str,
    action: str,
    reason: str | None = Form(default=None),
    _auth: dict = Depends(_project_write),
    db: Session = Depends(get_db),
):
    proposal = vendor_as_built_review_proposals.issue_review(
        db,
        as_built_id=as_built_id,
        action=action,
        actor_id=_actor(request),
        reason=reason,
    )
    context = _ctx(request, db)
    context["proposal"] = proposal
    return templates.TemplateResponse(
        "admin/vendors/as_built_review_confirm.html", context
    )


@router.post("/as-built/{as_built_id}/{action}/confirm")
def confirm_vendor_as_built_review(
    request: Request,
    as_built_id: str,
    action: str,
    confirmation_token: str = Form(...),
    _auth: dict = Depends(_project_write),
    db: Session = Depends(get_db),
):
    result = vendor_as_built_review_proposals.confirm_review(
        db,
        confirmation_token=confirmation_token,
        as_built_id=as_built_id,
        action=action,
        actor_id=_actor(request),
    )
    label = "accepted" if result.action == "accept" else "rejected"
    return RedirectResponse(
        f"/admin/vendors/operations?message=As-built+evidence+{label}",
        status_code=303,
    )


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
