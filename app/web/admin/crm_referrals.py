"""Admin web pages for the referral program (Phase 3 §2.6).

Ported from CRM ``app/web/admin/crm_referrals.py`` + templates and restyled
to sub conventions (thin routes over ``web_referrals`` context builders, the
service-requests queue idiom). Rides the ``crm:lead:*`` permissions like the
staff API (``app/api/crm_referrals.py``) — referrals are part of the
sales/lead funnel. Actions call the native ``Referrals`` service directly:
qualify override, issue reward (idempotent wallet credit), reject.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_referrals as web_referrals_service
from app.services.auth_dependencies import require_permission
from app.services.referrals import referrals as referrals_service

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/referrals", tags=["web-admin-referrals"])


def _ctx(request: Request, db: Session) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "referrals",
        "active_menu": "referrals",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _redirect(
    referral_id: str, *, error: str | None = None, message: str | None = None
) -> RedirectResponse:
    url = f"/admin/referrals/{referral_id}"
    if error:
        url += f"?error={quote(error)}"
    elif message:
        url += f"?message={quote(message)}"
    return RedirectResponse(url=url, status_code=303)


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def referrals_list(
    request: Request,
    status: str | None = None,
    reward_status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    context = _ctx(request, db)
    context.update(
        web_referrals_service.list_data(
            db,
            status=status,
            reward_status=reward_status,
            page=page,
            per_page=per_page,
        )
    )
    return templates.TemplateResponse("admin/referrals/index.html", context)


@router.get(
    "/{referral_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def referral_detail(
    request: Request,
    referral_id: str,
    error: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
):
    state = web_referrals_service.detail_data(db, referral_id=referral_id)
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Referral not found"},
            status_code=404,
        )
    context = _ctx(request, db)
    context.update(state)
    context.update({"error": error, "message": message})
    return templates.TemplateResponse("admin/referrals/detail.html", context)


@router.post(
    "/{referral_id}/qualify",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def referral_qualify(request: Request, referral_id: str, db: Session = Depends(get_db)):
    try:
        referrals_service.qualify_override(db, referral_id)
    except HTTPException as exc:
        return _redirect(referral_id, error=str(exc.detail))
    return _redirect(
        referral_id, message="Referral qualified — reward is ready to issue."
    )


@router.post(
    "/{referral_id}/issue-reward",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def referral_issue_reward(
    request: Request, referral_id: str, db: Session = Depends(get_db)
):
    try:
        referrals_service.issue_reward(db, referral_id)
    except HTTPException as exc:
        return _redirect(referral_id, error=str(exc.detail))
    return _redirect(referral_id, message="Reward credited to the referrer's wallet.")


@router.post(
    "/{referral_id}/reject",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def referral_reject(
    request: Request,
    referral_id: str,
    reason: str = Form(default="Rejected by admin"),
    db: Session = Depends(get_db),
):
    try:
        referrals_service.reject(db, referral_id, reason.strip() or "Rejected by admin")
    except HTTPException as exc:
        return _redirect(referral_id, error=str(exc.detail))
    return _redirect(referral_id, message="Referral rejected.")
