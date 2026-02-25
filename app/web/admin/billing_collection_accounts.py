"""Admin billing collection accounts routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.billing import CollectionAccountUpdate
from app.services import billing as billing_service
from app.services import web_billing_collection_accounts as web_billing_collection_accounts_service
from app.services.auth_dependencies import require_permission
from app.services.billing import configuration as billing_config_service

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


@router.get(
    "/collection-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:read"))],
)
def collection_accounts_list(
    request: Request,
    show_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    state = web_billing_collection_accounts_service.list_data(db, show_inactive=show_inactive)
    return templates.TemplateResponse(
        "admin/billing/collection_accounts.html",
        {
            **_base_context(request, db, "collection_accounts"),
            **state,
        },
    )


@router.post(
    "/collection-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def collection_accounts_create(
    request: Request,
    name: str = Form(...),
    account_type: str = Form("bank"),
    currency: str = Form("NGN"),
    bank_name: str | None = Form(None),
    account_last4: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.create_collection_account(
            db=db,
            name=name,
            account_type=account_type,
            currency=currency,
            bank_name=bank_name,
            account_last4=account_last4,
            notes=notes,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/billing/collection-accounts", status_code=303)
    except Exception as exc:
        state = web_billing_collection_accounts_service.list_data(db, show_inactive=False)
        return templates.TemplateResponse(
            "admin/billing/collection_accounts.html",
            {
                **_base_context(request, db, "collection_accounts"),
                **state,
                "error": str(exc),
            },
            status_code=400,
        )


@router.get(
    "/collection-accounts/{account_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def collection_accounts_edit(request: Request, account_id: UUID, db: Session = Depends(get_db)):
    state = web_billing_collection_accounts_service.edit_data(db, account_id=str(account_id))
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Collection account not found"},
            status_code=404,
        )
    return templates.TemplateResponse(
        "admin/billing/collection_account_form.html",
        {
            **_base_context(request, db, "collection_accounts"),
            **state,
            "action_url": f"/admin/billing/collection-accounts/{account_id}/edit",
            "form_title": "Edit Collection Account",
            "submit_label": "Update Account",
        },
    )


@router.post(
    "/collection-accounts/{account_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def collection_accounts_update(
    request: Request,
    account_id: UUID,
    name: str = Form(...),
    account_type: str = Form("bank"),
    currency: str = Form("NGN"),
    bank_name: str | None = Form(None),
    account_last4: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.update_collection_account(
            db=db,
            account_id=account_id,
            name=name,
            account_type=account_type,
            currency=currency,
            bank_name=bank_name,
            account_last4=account_last4,
            notes=notes,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/billing/collection-accounts", status_code=303)
    except Exception as exc:
        state = web_billing_collection_accounts_service.edit_data(db, account_id=str(account_id))
        return templates.TemplateResponse(
            "admin/billing/collection_account_form.html",
            {
                **_base_context(request, db, "collection_accounts"),
                **(state or {}),
                "action_url": f"/admin/billing/collection-accounts/{account_id}/edit",
                "form_title": "Edit Collection Account",
                "submit_label": "Update Account",
                "error": str(exc),
            },
            status_code=400,
        )


@router.post(
    "/collection-accounts/{account_id}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def collection_accounts_deactivate(account_id: UUID, db: Session = Depends(get_db)):
    billing_service.collection_accounts.delete(db, str(account_id))
    return RedirectResponse(url="/admin/billing/collection-accounts", status_code=303)


@router.post(
    "/collection-accounts/{account_id}/activate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def collection_accounts_activate(account_id: UUID, db: Session = Depends(get_db)):
    billing_service.collection_accounts.update(
        db,
        str(account_id),
        CollectionAccountUpdate(is_active=True),
    )
    return RedirectResponse(url="/admin/billing/collection-accounts", status_code=303)
