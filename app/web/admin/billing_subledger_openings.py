"""Admin routes for reviewed customer-subledger opening corrections.

Adapter only: the route checks the dedicated permission, resolves the staff
principal, and renders projections. ``financial.customer_subledger_opening_
positions`` owns the preview, its enforcement consequence, and the
append-only correction, and rechecks permission and freshness under lock.
"""

from urllib.parse import quote_plus
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import (
    web_subledger_opening_corrections as web_opening_corrections,
)
from app.services.auth_dependencies import has_permission, require_permission
from app.services.domain_errors import DomainError

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])

_PERMISSION = web_opening_corrections.ACTION_PERMISSION
_BASE = "/accounts/{account_id}/subledger-opening/{currency}/correction"


def _staff(auth: dict, db: Session) -> web_opening_corrections.OpeningCorrectionStaff:
    if str(auth.get("principal_type") or "") != "system_user":
        raise HTTPException(
            status_code=403,
            detail="A signed-in staff user is required to correct an opening",
        )
    try:
        system_user_id = UUID(str(auth.get("principal_id") or ""))
    except ValueError as exc:
        raise HTTPException(
            status_code=403, detail="Authorized actor is missing"
        ) from exc
    return web_opening_corrections.OpeningCorrectionStaff(
        actor=f"system_user:{system_user_id}",
        system_user_id=system_user_id,
        permission_granted=has_permission(auth, db, _PERMISSION),
    )


def _account_redirect(account_id: UUID, *, key: str, message: str) -> Response:
    return RedirectResponse(
        url=f"/admin/billing/accounts/{account_id}?{key}={quote_plus(message)}#subledger-opening",
        status_code=303,
    )


def _render(
    request: Request,
    *,
    db: Session,
    page: web_opening_corrections.OpeningCorrectionPage,
    status_code: int = 200,
) -> Response:
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        request,
        "admin/billing/subledger_opening_correction.html",
        {
            "page": page,
            "active_page": "accounts",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
        status_code=status_code,
    )


@router.get(_BASE, response_class=HTMLResponse)
def subledger_opening_correction_form(
    request: Request,
    account_id: UUID,
    currency: str,
    auth: dict = Depends(require_permission(_PERMISSION)),
    db: Session = Depends(get_db),
) -> Response:
    _staff(auth, db)
    try:
        page = web_opening_corrections.build_entry_page(
            db,
            account_id=account_id,
            values=web_opening_corrections.OpeningCorrectionFormValues(
                currency=currency
            ),
        )
    except DomainError as exc:
        return _account_redirect(
            account_id, key="opening_correction_error", message=exc.message
        )
    return _render(request, db=db, page=page)


@router.post(f"{_BASE}/preview", response_class=HTMLResponse)
def subledger_opening_correction_preview(
    request: Request,
    account_id: UUID,
    currency: str,
    corrected_opening_amount: str = Form(""),
    reason: str = Form(""),
    review_reference: str = Form(""),
    auth: dict = Depends(require_permission(_PERMISSION)),
    db: Session = Depends(get_db),
) -> Response:
    staff = _staff(auth, db)
    values = web_opening_corrections.OpeningCorrectionFormValues(
        currency=currency,
        corrected_opening_amount=corrected_opening_amount,
        reason=reason,
        review_reference=review_reference,
    )
    try:
        page = web_opening_corrections.build_review_page(
            db, account_id=account_id, actor=staff.actor, values=values
        )
    except DomainError as exc:
        try:
            page = web_opening_corrections.build_entry_page(
                db, account_id=account_id, values=values, error=exc
            )
        except DomainError as load_error:
            return _account_redirect(
                account_id,
                key="opening_correction_error",
                message=load_error.message,
            )
        return _render(
            request,
            db=db,
            page=page,
            status_code=web_opening_corrections.error_status(exc),
        )
    return _render(request, db=db, page=page)


@router.post(f"{_BASE}/confirm")
def subledger_opening_correction_confirm(
    request: Request,
    account_id: UUID,
    currency: str,
    corrected_opening_amount: str = Form(""),
    reason: str = Form(""),
    review_reference: str = Form(""),
    preview_fingerprint: str = Form(""),
    confirmation_token: str = Form(""),
    confirmed: str | None = Form(None),
    auth: dict = Depends(require_permission(_PERMISSION)),
    db: Session = Depends(get_db),
) -> Response:
    staff = _staff(auth, db)
    values = web_opening_corrections.OpeningCorrectionFormValues(
        currency=currency,
        corrected_opening_amount=corrected_opening_amount,
        reason=reason,
        review_reference=review_reference,
    )
    try:
        result = web_opening_corrections.confirm_correction(
            db,
            account_id=account_id,
            staff=staff,
            values=values,
            preview_fingerprint=preview_fingerprint,
            confirmation_token=confirmation_token,
            confirmed=confirmed,
        )
    except DomainError as exc:
        try:
            page = web_opening_corrections.rebuild_after_confirm_error(
                db,
                account_id=account_id,
                actor=staff.actor,
                values=values,
                error=exc,
            )
        except DomainError:
            return _account_redirect(
                account_id, key="opening_correction_error", message=exc.message
            )
        return _render(
            request,
            db=db,
            page=page,
            status_code=web_opening_corrections.error_status(exc),
        )
    unit = currency.strip().upper()
    message = (
        f"Opening corrected from {unit} {result.previous_opening_amount:,.2f} "
        f"to {unit} {result.corrected_opening_amount:,.2f} "
        f"(delta {unit} {result.delta:+,.2f}); correction {result.correction_id}."
    )
    if result.replayed:
        message = f"This correction was already recorded. {message}"
    return _account_redirect(
        account_id, key="opening_correction_message", message=message
    )
