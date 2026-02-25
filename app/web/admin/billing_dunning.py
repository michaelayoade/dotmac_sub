"""Admin billing dunning web routes."""

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_billing_dunning as web_billing_dunning_service
from app.services.audit_helpers import log_audit_event
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


def _log_bulk_audit_events(
    *,
    db: Session,
    request: Request,
    action: str,
    entity_type: str,
    entity_ids: list[str],
    actor_id: str | None,
) -> None:
    for entity_id in entity_ids:
        log_audit_event(
            db=db,
            request=request,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_id=actor_id,
        )


@router.get("/dunning", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def billing_dunning(
    request: Request,
    page: int = 1,
    status: str | None = None,
    customer_ref: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_dunning_service.build_listing_data(
        db,
        page=page,
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


@router.post("/dunning/{case_id}/pause", dependencies=[Depends(require_permission("billing:write"))])
def dunning_pause(request: Request, case_id: str, db: Session = Depends(get_db)):
    processed_ids = web_billing_dunning_service.execute_action(
        db,
        action="pause",
        case_id=case_id,
    )
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    _log_bulk_audit_events(
        db=db,
        request=request,
        action="pause",
        entity_type="dunning_case",
        entity_ids=processed_ids,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


@router.post("/dunning/{case_id}/resume", dependencies=[Depends(require_permission("billing:write"))])
def dunning_resume(request: Request, case_id: str, db: Session = Depends(get_db)):
    processed_ids = web_billing_dunning_service.execute_action(
        db,
        action="resume",
        case_id=case_id,
    )
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    _log_bulk_audit_events(
        db=db,
        request=request,
        action="resume",
        entity_type="dunning_case",
        entity_ids=processed_ids,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


@router.post("/dunning/{case_id}/close", dependencies=[Depends(require_permission("billing:write"))])
def dunning_close(request: Request, case_id: str, db: Session = Depends(get_db)):
    processed_ids = web_billing_dunning_service.execute_action(
        db,
        action="close",
        case_id=case_id,
    )
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    _log_bulk_audit_events(
        db=db,
        request=request,
        action="close",
        entity_type="dunning_case",
        entity_ids=processed_ids,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


@router.post("/dunning/bulk/pause", dependencies=[Depends(require_permission("billing:write"))])
def dunning_bulk_pause(request: Request, case_ids: str = Form(...), db: Session = Depends(get_db)):
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    processed_ids = web_billing_dunning_service.execute_action(
        db,
        action="pause",
        case_ids_csv=case_ids,
    )
    _log_bulk_audit_events(
        db=db,
        request=request,
        action="pause",
        entity_type="dunning_case",
        entity_ids=processed_ids,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


@router.post("/dunning/bulk/resume", dependencies=[Depends(require_permission("billing:write"))])
def dunning_bulk_resume(request: Request, case_ids: str = Form(...), db: Session = Depends(get_db)):
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    processed_ids = web_billing_dunning_service.execute_action(
        db,
        action="resume",
        case_ids_csv=case_ids,
    )
    _log_bulk_audit_events(
        db=db,
        request=request,
        action="resume",
        entity_type="dunning_case",
        entity_ids=processed_ids,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)
