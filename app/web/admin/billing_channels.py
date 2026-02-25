"""Admin billing payment channels routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import billing as billing_service
from app.services import web_billing_channels as web_billing_channels_service
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
    "/payment-channels",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:read"))],
)
def payment_channels_list(request: Request, db: Session = Depends(get_db)):
    state = web_billing_channels_service.list_payment_channels_data(db)
    return templates.TemplateResponse(
        "admin/billing/payment_channels.html",
        {
            **_base_context(request, db, "payment_channels"),
            **state,
        },
    )


@router.post(
    "/payment-channels",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channels_create(
    request: Request,
    name: str = Form(...),
    channel_type: str = Form("other"),
    provider_id: str | None = Form(None),
    default_collection_account_id: str | None = Form(None),
    is_default: str | None = Form(None),
    is_active: str | None = Form(None),
    fee_rules: str | None = Form(None),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.create_payment_channel(
            db=db,
            name=name,
            channel_type=channel_type,
            provider_id=provider_id,
            default_collection_account_id=default_collection_account_id,
            is_default=is_default,
            is_active=is_active,
            fee_rules=fee_rules,
            notes=notes,
        )
        return RedirectResponse(url="/admin/billing/payment-channels", status_code=303)
    except Exception as exc:
        state = web_billing_channels_service.list_payment_channels_data(db)
        return templates.TemplateResponse(
            "admin/billing/payment_channels.html",
            {
                **_base_context(request, db, "payment_channels"),
                **state,
                "error": str(exc),
            },
            status_code=400,
        )


@router.get(
    "/payment-channels/{channel_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channels_edit(request: Request, channel_id: UUID, db: Session = Depends(get_db)):
    state = web_billing_channels_service.load_payment_channel_edit_data(db, str(channel_id))
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment channel not found"},
            status_code=404,
        )
    return templates.TemplateResponse(
        "admin/billing/payment_channel_form.html",
        {
            **_base_context(request, db, "payment_channels"),
            **state,
            "action_url": f"/admin/billing/payment-channels/{channel_id}/edit",
            "form_title": "Edit Payment Channel",
            "submit_label": "Update Channel",
        },
    )


@router.post(
    "/payment-channels/{channel_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channels_update(
    request: Request,
    channel_id: UUID,
    name: str = Form(...),
    channel_type: str = Form("other"),
    provider_id: str | None = Form(None),
    default_collection_account_id: str | None = Form(None),
    is_default: str | None = Form(None),
    is_active: str | None = Form(None),
    fee_rules: str | None = Form(None),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.update_payment_channel(
            db=db,
            channel_id=channel_id,
            name=name,
            channel_type=channel_type,
            provider_id=provider_id,
            default_collection_account_id=default_collection_account_id,
            is_default=is_default,
            is_active=is_active,
            fee_rules=fee_rules,
            notes=notes,
        )
        return RedirectResponse(url="/admin/billing/payment-channels", status_code=303)
    except Exception as exc:
        state = web_billing_channels_service.load_payment_channel_edit_data(db, str(channel_id))
        return templates.TemplateResponse(
            "admin/billing/payment_channel_form.html",
            {
                **_base_context(request, db, "payment_channels"),
                **(state or {}),
                "action_url": f"/admin/billing/payment-channels/{channel_id}/edit",
                "form_title": "Edit Payment Channel",
                "submit_label": "Update Channel",
                "error": str(exc),
            },
            status_code=400,
        )


@router.post(
    "/payment-channels/{channel_id}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channels_deactivate(channel_id: UUID, db: Session = Depends(get_db)):
    billing_service.payment_channels.delete(db, str(channel_id))
    return RedirectResponse(url="/admin/billing/payment-channels", status_code=303)


@router.get(
    "/payment-channel-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:read"))],
)
def payment_channel_accounts_list(request: Request, db: Session = Depends(get_db)):
    state = web_billing_channels_service.list_payment_channel_accounts_data(db)
    return templates.TemplateResponse(
        "admin/billing/payment_channel_accounts.html",
        {
            **_base_context(request, db, "payment_channel_accounts"),
            **state,
        },
    )


@router.post(
    "/payment-channel-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channel_accounts_create(
    request: Request,
    channel_id: str = Form(...),
    collection_account_id: str = Form(...),
    currency: str | None = Form(None),
    priority: int = Form(0),
    is_default: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.create_payment_channel_account(
            db=db,
            channel_id=channel_id,
            collection_account_id=collection_account_id,
            currency=currency,
            priority=priority,
            is_default=is_default,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/billing/payment-channel-accounts", status_code=303)
    except Exception as exc:
        state = web_billing_channels_service.list_payment_channel_accounts_data(db)
        return templates.TemplateResponse(
            "admin/billing/payment_channel_accounts.html",
            {
                **_base_context(request, db, "payment_channel_accounts"),
                **state,
                "error": str(exc),
            },
            status_code=400,
        )


@router.get(
    "/payment-channel-accounts/{mapping_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channel_accounts_edit(request: Request, mapping_id: UUID, db: Session = Depends(get_db)):
    state = web_billing_channels_service.load_payment_channel_account_edit_data(db, str(mapping_id))
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment channel mapping not found"},
            status_code=404,
        )
    return templates.TemplateResponse(
        "admin/billing/payment_channel_account_form.html",
        {
            **_base_context(request, db, "payment_channel_accounts"),
            **state,
            "action_url": f"/admin/billing/payment-channel-accounts/{mapping_id}/edit",
            "form_title": "Edit Channel Mapping",
            "submit_label": "Update Mapping",
        },
    )


@router.post(
    "/payment-channel-accounts/{mapping_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channel_accounts_update(
    request: Request,
    mapping_id: UUID,
    channel_id: str = Form(...),
    collection_account_id: str = Form(...),
    currency: str | None = Form(None),
    priority: int = Form(0),
    is_default: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.update_payment_channel_account(
            db=db,
            mapping_id=mapping_id,
            channel_id=channel_id,
            collection_account_id=collection_account_id,
            currency=currency,
            priority=priority,
            is_default=is_default,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/billing/payment-channel-accounts", status_code=303)
    except Exception as exc:
        state = web_billing_channels_service.load_payment_channel_account_edit_data(db, str(mapping_id))
        return templates.TemplateResponse(
            "admin/billing/payment_channel_account_form.html",
            {
                **_base_context(request, db, "payment_channel_accounts"),
                **(state or {}),
                "action_url": f"/admin/billing/payment-channel-accounts/{mapping_id}/edit",
                "form_title": "Edit Channel Mapping",
                "submit_label": "Update Mapping",
                "error": str(exc),
            },
            status_code=400,
        )


@router.post(
    "/payment-channel-accounts/{mapping_id}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channel_accounts_deactivate(mapping_id: UUID, db: Session = Depends(get_db)):
    billing_service.payment_channel_accounts.delete(db, str(mapping_id))
    return RedirectResponse(url="/admin/billing/payment-channel-accounts", status_code=303)
