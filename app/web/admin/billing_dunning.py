"""Admin billing dunning web routes."""

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_billing_dunning as web_billing_dunning_service
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


def _base_context(request: Request, db: Session, active_page: str) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "billing",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _actor_id(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    if not current_user:
        return None
    value = current_user.get("actor_id") or current_user.get("subscriber_id")
    return str(value) if value else None


@router.get(
    "/dunning",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:dunning:read"))],
)
def billing_dunning(
    request: Request,
    page: int = 1,
    per_page: int = Query(50, ge=10, le=100),
    status: str | None = None,
    customer_ref: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_dunning_service.build_listing_data(
        db,
        page=page,
        per_page=per_page,
        status=status,
        customer_ref=customer_ref,
    )
    return templates.TemplateResponse(
        "admin/billing/dunning.html",
        {
            **_base_context(request, db, "dunning"),
            **state,
        },
    )


@router.post(
    "/dunning/{case_id}/pause",
    dependencies=[Depends(require_permission("billing:dunning:write"))],
)
def dunning_pause(request: Request, case_id: str, db: Session = Depends(get_db)):
    web_billing_dunning_service.execute_action_with_audit(
        db,
        request=request,
        action="pause",
        actor_id=_actor_id(request),
        case_id=case_id,
    )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


@router.post(
    "/dunning/{case_id}/resume",
    dependencies=[Depends(require_permission("billing:dunning:write"))],
)
def dunning_resume(request: Request, case_id: str, db: Session = Depends(get_db)):
    web_billing_dunning_service.execute_action_with_audit(
        db,
        request=request,
        action="resume",
        actor_id=_actor_id(request),
        case_id=case_id,
    )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


@router.post(
    "/dunning/{case_id}/close",
    dependencies=[Depends(require_permission("billing:dunning:write"))],
)
def dunning_close(request: Request, case_id: str, db: Session = Depends(get_db)):
    web_billing_dunning_service.execute_action_with_audit(
        db,
        request=request,
        action="close",
        actor_id=_actor_id(request),
        case_id=case_id,
    )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


@router.post(
    "/dunning/bulk/pause",
    dependencies=[Depends(require_permission("billing:dunning:write"))],
)
def dunning_bulk_pause(
    request: Request, case_ids: str = Form(...), db: Session = Depends(get_db)
):
    web_billing_dunning_service.execute_action_with_audit(
        db,
        request=request,
        action="pause",
        actor_id=_actor_id(request),
        case_ids_csv=case_ids,
    )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


@router.post(
    "/dunning/bulk/resume",
    dependencies=[Depends(require_permission("billing:dunning:write"))],
)
def dunning_bulk_resume(
    request: Request, case_ids: str = Form(...), db: Session = Depends(get_db)
):
    web_billing_dunning_service.execute_action_with_audit(
        db,
        request=request,
        action="resume",
        actor_id=_actor_id(request),
        case_ids_csv=case_ids,
    )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)
