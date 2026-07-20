"""Admin web pages for the native referral program.

Ported from CRM ``app/web/admin/crm_referrals.py`` + templates and restyled
to sub conventions (thin routes over ``web_referrals`` context builders, the
service-requests queue idiom). Rides the ``crm:lead:*`` permissions like the
staff API (``app/api/crm_referrals.py``) — referrals are part of the
sales/lead funnel. Actions call the native ``Referrals`` service directly:
qualify override, issue reward (idempotent account credit), reject.
"""

from __future__ import annotations

from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import referral_account_conversion
from app.services import referrals as referral_program
from app.services import web_referrals as web_referrals_service
from app.services.auth_dependencies import require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

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


def _conversion_source(request: Request) -> str:
    from app.web.admin import get_current_user

    auth = get_current_user(request)
    actor = str(auth.get("principal_id") or auth.get("id") or "unknown").strip()
    principal_type = str(auth.get("principal_type") or "system_user").strip()
    return f"admin_referral_attach:{principal_type}:{actor}"[:80]


def _program_context(
    request: Request,
    *,
    action: str,
    reason: str,
    idempotency_key: str,
) -> CommandContext:
    return CommandContext.system(
        actor=f"{action}:{_conversion_source(request)}"[:80],
        scope=referral_program.REFERRAL_PROGRAM_SCOPE,
        reason=reason,
        idempotency_key=idempotency_key,
    )


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
        resolved_referral_id = UUID(referral_id)
        db_session_adapter.release_read_transaction(db)
        referral_program.qualify_referral_override(
            db,
            referral_program.QualifyReferralOverrideCommand(
                context=_program_context(
                    request,
                    action="admin_referral_qualify",
                    reason="Operator reviewed and overrode referral qualification",
                    idempotency_key=f"referral-qualify-override:{resolved_referral_id}",
                ),
                referral_id=resolved_referral_id,
            ),
        )
    except (ValueError, DomainError) as exc:
        return _redirect(referral_id, error=str(exc))
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
        resolved_referral_id = UUID(referral_id)
        db_session_adapter.release_read_transaction(db)
        referral_program.issue_referral_reward(
            db,
            referral_program.IssueReferralRewardCommand(
                context=_program_context(
                    request,
                    action="admin_referral_reward",
                    reason="Operator requested referral reward issuance",
                    idempotency_key=f"referral-reward:{resolved_referral_id}",
                ),
                referral_id=resolved_referral_id,
            ),
        )
    except (ValueError, DomainError) as exc:
        return _redirect(referral_id, error=str(exc))
    return _redirect(
        referral_id, message="Reward issued as an auditable account credit."
    )


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
        resolved_referral_id = UUID(referral_id)
        resolved_reason = reason.strip() or "Rejected by admin"
        db_session_adapter.release_read_transaction(db)
        referral_program.reject_referral(
            db,
            referral_program.RejectReferralCommand(
                context=_program_context(
                    request,
                    action="admin_referral_reject",
                    reason="Operator rejected the referral",
                    idempotency_key=f"referral-reject:{resolved_referral_id}",
                ),
                referral_id=resolved_referral_id,
                reason=resolved_reason,
            ),
        )
    except (ValueError, DomainError) as exc:
        return _redirect(referral_id, error=str(exc))
    return _redirect(referral_id, message="Referral rejected.")


@router.post(
    "/{referral_id}/attach-subscriber",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("crm:lead:write")),
        Depends(require_permission("customer:update")),
    ],
)
def referral_attach_subscriber(
    request: Request,
    referral_id: str,
    subscriber_id: str = Form(...),
    referred_party_id: str = Form(...),
    referred_lead_id: str = Form(...),
    reason: str = Form(...),
    db: Session = Depends(get_db),
):
    """Attach an existing account only after exact Party-context review."""

    try:
        resolved_referral_id = UUID(referral_id)
        resolved_subscriber_id = UUID(subscriber_id)
        db_session_adapter.release_read_transaction(db)
        result = referral_account_conversion.attach_existing_account(
            db,
            referral_account_conversion.AttachExistingReferralAccountCommand(
                context=CommandContext.system(
                    actor=_conversion_source(request),
                    scope=(
                        referral_account_conversion.REFERRAL_ACCOUNT_CONVERSION_SCOPE
                    ),
                    reason=reason,
                    idempotency_key=(
                        f"referral-account-attach:{resolved_referral_id}:"
                        f"{resolved_subscriber_id}"
                    ),
                ),
                referral_id=resolved_referral_id,
                referred_party_id=UUID(referred_party_id),
                referred_lead_id=UUID(referred_lead_id),
                subscriber_id=resolved_subscriber_id,
            ),
        )
    except (
        ValueError,
        referral_account_conversion.ReferralAccountConversionError,
    ) as exc:
        return _redirect(referral_id, error=str(exc))
    message = (
        "Subscriber was already attached to this exact referral Party."
        if result.outcome == "already_attached"
        else "Subscriber attached to the reviewed referral Party."
    )
    return _redirect(referral_id, message=message)
