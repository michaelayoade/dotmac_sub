"""Admin billing payment providers routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.billing import PaymentProviderCreate, PaymentProviderUpdate
from app.services import billing as billing_service
from app.services import web_billing_providers as web_billing_providers_service
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


@router.get(
    "/payment-providers",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:read"))],
)
def payment_providers_list(
    request: Request,
    show_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    state = web_billing_providers_service.list_data(db, show_inactive=show_inactive)
    return templates.TemplateResponse(
        "admin/billing/payment_providers.html",
        {
            **_base_context(request, db, "payment_providers"),
            **state,
        },
    )


@router.post(
    "/payment-providers",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_providers_create(
    request: Request,
    name: str = Form(...),
    provider_type: str = Form("paystack"),
    webhook_secret_ref: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        payload = PaymentProviderCreate(
            name=name.strip(),
            provider_type=web_billing_providers_service.parse_supported_provider_type(
                provider_type
            ),
            webhook_secret_ref=webhook_secret_ref.strip() if webhook_secret_ref else None,
            notes=notes.strip() if notes else None,
            is_active=is_active is not None,
        )
        billing_service.payment_providers.create(db, payload)
        return RedirectResponse(url="/admin/billing/payment-providers", status_code=303)
    except Exception as exc:
        state = web_billing_providers_service.list_data(db, show_inactive=False)
        return templates.TemplateResponse(
            "admin/billing/payment_providers.html",
            {
                **_base_context(request, db, "payment_providers"),
                **state,
                "error": str(exc),
            },
            status_code=400,
        )


@router.get(
    "/payment-providers/{provider_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_providers_edit(request: Request, provider_id: UUID, db: Session = Depends(get_db)):
    state = web_billing_providers_service.edit_data(db, provider_id=str(provider_id))
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment provider not found"},
            status_code=404,
        )
    return templates.TemplateResponse(
        "admin/billing/payment_provider_form.html",
        {
            **_base_context(request, db, "payment_providers"),
            **state,
            "action_url": f"/admin/billing/payment-providers/{provider_id}/edit",
            "form_title": "Edit Payment Provider",
            "submit_label": "Update Provider",
        },
    )


@router.post(
    "/payment-providers/{provider_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_providers_update(
    request: Request,
    provider_id: UUID,
    name: str = Form(...),
    provider_type: str = Form("paystack"),
    webhook_secret_ref: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        payload = PaymentProviderUpdate(
            name=name.strip(),
            provider_type=web_billing_providers_service.parse_supported_provider_type(
                provider_type
            ),
            webhook_secret_ref=webhook_secret_ref.strip() if webhook_secret_ref else None,
            notes=notes.strip() if notes else None,
            is_active=is_active is not None,
        )
        billing_service.payment_providers.update(db, str(provider_id), payload)
        return RedirectResponse(url="/admin/billing/payment-providers", status_code=303)
    except Exception as exc:
        state = web_billing_providers_service.edit_data(db, provider_id=str(provider_id))
        return templates.TemplateResponse(
            "admin/billing/payment_provider_form.html",
            {
                **_base_context(request, db, "payment_providers"),
                **(state or {}),
                "action_url": f"/admin/billing/payment-providers/{provider_id}/edit",
                "form_title": "Edit Payment Provider",
                "submit_label": "Update Provider",
                "error": str(exc),
            },
            status_code=400,
        )


@router.post(
    "/payment-providers/{provider_id}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_providers_deactivate(provider_id: UUID, db: Session = Depends(get_db)):
    billing_service.payment_providers.delete(db, str(provider_id))
    return RedirectResponse(url="/admin/billing/payment-providers", status_code=303)


@router.post(
    "/payment-providers/test",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_providers_test(
    request: Request,
    provider_type: str = Form(...),
    mode: str = Form("test"),
    show_inactive: bool = Form(False),
    db: Session = Depends(get_db),
):
    test_result = web_billing_providers_service.run_provider_test(
        db,
        provider_type_value=provider_type,
        mode=mode,
    )
    state = web_billing_providers_service.list_data(db, show_inactive=show_inactive)
    return templates.TemplateResponse(
        "admin/billing/payment_providers.html",
        {
            **_base_context(request, db, "payment_providers"),
            **state,
            "test_result": test_result,
        },
        status_code=200 if test_result["ok"] else 400,
    )


@router.post(
    "/payment-providers/failover-config",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_providers_failover_config(
    request: Request,
    primary_provider: str = Form(...),
    secondary_provider: str = Form(...),
    failover_enabled: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        web_billing_providers_service.update_failover_config(
            db,
            failover_enabled=failover_enabled is not None,
            primary_provider=primary_provider,
            secondary_provider=secondary_provider,
        )
        return RedirectResponse(url="/admin/billing/payment-providers", status_code=303)
    except Exception as exc:
        state = web_billing_providers_service.list_data(db, show_inactive=False)
        return templates.TemplateResponse(
            "admin/billing/payment_providers.html",
            {
                **_base_context(request, db, "payment_providers"),
                **state,
                "error": str(exc),
            },
            status_code=400,
        )


@router.post(
    "/payment-providers/failover-trigger",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_providers_failover_trigger(
    request: Request,
    db: Session = Depends(get_db),
):
    switched, message = web_billing_providers_service.trigger_failover_if_needed(db)
    state = web_billing_providers_service.list_data(db, show_inactive=False)
    payload_key = "notice" if switched else "error"
    return templates.TemplateResponse(
        "admin/billing/payment_providers.html",
        {
            **_base_context(request, db, "payment_providers"),
            **state,
            payload_key: message,
        },
        status_code=200 if switched else 409,
    )
