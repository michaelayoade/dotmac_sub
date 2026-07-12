"""Admin sales routes — leads (list/board/detail), pipeline settings,
quotes (list/detail), sales orders (list/detail).

Phase 3 §2.6 admin-web port (PR 11): the CRM's ``web/admin/crm_leads.py`` /
``crm_sales.py`` / ``crm_quotes.py`` + the sales-order pages of
``operations.py``, restyled onto sub's thin-route + context-builder idiom
(see ``support_tickets.py``). All business logic lives in
``app.services.web_sales`` and the native sales managers.

The kanban board persists stage drags through the already-ported API
endpoints ``GET /api/v1/leads/kanban`` / ``POST /api/v1/leads/kanban/move``
(``app/api/sales.py``) via ``static/js/kanban.js``.

RBAC: ``crm:lead:*`` guards leads *and* pipeline settings (pipelines ride
lead permissions, matching the API port); quotes and sales orders use
``crm:quote:read`` / ``crm:sales_order:read``. Key seeding lands with PR 12
— the guards are in place regardless.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_sales as web_sales_service
from app.services.auth_dependencies import require_permission

router = APIRouter(prefix="/sales", tags=["web-admin-sales"])
templates = Jinja2Templates(directory="templates")


def _ctx(request: Request, db: Session, active_page: str) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "sales",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _error_detail(exc: Exception) -> str:
    return str(getattr(exc, "detail", None) or exc)


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


@router.get(
    "/leads",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def leads_list(
    request: Request,
    status: str | None = Query(default=None),
    pipeline_id: str | None = Query(default=None),
    stage_id: str | None = Query(default=None),
    lead_source: str | None = Query(default=None),
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    context = _ctx(request, db, "sales-leads")
    context.update(
        web_sales_service.build_leads_list_context(
            db,
            status=status,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            lead_source=lead_source,
            search=search,
            page=page,
            per_page=per_page,
        )
    )
    return templates.TemplateResponse("admin/sales/leads/index.html", context)


@router.get(
    "/leads/board",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def leads_board(
    request: Request,
    pipeline_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    context = _ctx(request, db, "sales-leads")
    context.update(
        web_sales_service.build_leads_board_context(db, pipeline_id=pipeline_id)
    )
    return templates.TemplateResponse("admin/sales/leads/board.html", context)


@router.get(
    "/leads/{lead_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def lead_detail(request: Request, lead_id: str, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-leads")
    context.update(web_sales_service.build_lead_detail_context(db, lead_id=lead_id))
    return templates.TemplateResponse("admin/sales/leads/detail.html", context)


# ---------------------------------------------------------------------------
# Pipeline settings
# ---------------------------------------------------------------------------


@router.get(
    "/pipelines",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_settings(
    request: Request,
    db: Session = Depends(get_db),
):
    context = _ctx(request, db, "sales-pipelines")
    context.update(
        web_sales_service.build_pipeline_settings_context(
            db,
            bulk_result=request.query_params.get("bulk_result", "").strip(),
            bulk_count=request.query_params.get("bulk_count", "").strip(),
        )
    )
    return templates.TemplateResponse("admin/sales/pipelines/index.html", context)


@router.get(
    "/pipelines/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_new(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-pipelines")
    context.update(web_sales_service.build_pipeline_new_context())
    return templates.TemplateResponse("admin/sales/pipelines/form.html", context)


@router.post(
    "/pipelines",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_create(
    request: Request,
    name: str | None = Form(default=None),
    is_active: str | None = Form(default=None),
    create_default_stages: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        pipeline_id = web_sales_service.create_pipeline_from_form(
            db,
            name=name,
            is_active=is_active,
            create_default_stages=create_default_stages,
        )
        return RedirectResponse(
            url=f"/admin/sales/leads/board?pipeline_id={pipeline_id}",
            status_code=303,
        )
    except (ValidationError, ValueError) as exc:
        db.rollback()
        error = _error_detail(exc)

    context = _ctx(request, db, "sales-pipelines")
    context.update(
        web_sales_service.build_pipeline_form_error_context(
            mode="create",
            pipeline_id=None,
            name=name,
            is_active=is_active,
            create_default_stages=create_default_stages,
        )
    )
    context["error"] = error
    return templates.TemplateResponse(
        "admin/sales/pipelines/form.html", context, status_code=400
    )


@router.get(
    "/pipelines/{pipeline_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_edit(request: Request, pipeline_id: str, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-pipelines")
    context.update(
        web_sales_service.build_pipeline_edit_context(db, pipeline_id=pipeline_id)
    )
    return templates.TemplateResponse("admin/sales/pipelines/form.html", context)


@router.post(
    "/pipelines/{pipeline_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_update(
    request: Request,
    pipeline_id: str,
    name: str | None = Form(default=None),
    is_active: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        web_sales_service.update_pipeline_from_form(
            db, pipeline_id=pipeline_id, name=name, is_active=is_active
        )
        return RedirectResponse(url="/admin/sales/pipelines", status_code=303)
    except (ValidationError, ValueError) as exc:
        db.rollback()
        error = _error_detail(exc)

    context = _ctx(request, db, "sales-pipelines")
    context.update(
        web_sales_service.build_pipeline_form_error_context(
            mode="update",
            pipeline_id=pipeline_id,
            name=name,
            is_active=is_active,
            create_default_stages=None,
        )
    )
    context["error"] = error
    return templates.TemplateResponse(
        "admin/sales/pipelines/form.html", context, status_code=400
    )


@router.post(
    "/pipelines/{pipeline_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_delete(request: Request, pipeline_id: str, db: Session = Depends(get_db)):
    _ = request
    web_sales_service.deactivate_pipeline(db, pipeline_id)
    return RedirectResponse(url="/admin/sales/pipelines", status_code=303)


@router.post(
    "/pipelines/{pipeline_id}/stages",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_stage_create(
    request: Request,
    pipeline_id: str,
    name: str = Form(...),
    order_index: int = Form(0),
    default_probability: int = Form(50),
    db: Session = Depends(get_db),
):
    _ = request
    web_sales_service.create_stage_from_form(
        db,
        pipeline_id=pipeline_id,
        name=name,
        order_index=order_index,
        default_probability=default_probability,
    )
    return RedirectResponse(url="/admin/sales/pipelines", status_code=303)


@router.post(
    "/pipelines/stages/{stage_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_stage_update(
    request: Request,
    stage_id: str,
    name: str = Form(...),
    order_index: int = Form(0),
    default_probability: int = Form(50),
    is_active: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    _ = request
    web_sales_service.update_stage_from_form(
        db,
        stage_id=stage_id,
        name=name,
        order_index=order_index,
        default_probability=default_probability,
        is_active=is_active,
    )
    return RedirectResponse(url="/admin/sales/pipelines", status_code=303)


@router.post(
    "/pipelines/stages/{stage_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_stage_delete(
    request: Request, stage_id: str, db: Session = Depends(get_db)
):
    _ = request
    web_sales_service.deactivate_stage(db, stage_id=stage_id)
    return RedirectResponse(url="/admin/sales/pipelines", status_code=303)


@router.post(
    "/pipelines/{pipeline_id}/bulk-assign-leads",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_bulk_assign_leads(
    request: Request,
    pipeline_id: str,
    stage_id: str | None = Form(default=None),
    scope: str = Form("unassigned"),
    db: Session = Depends(get_db),
):
    _ = request
    count = web_sales_service.bulk_assign_leads(
        db, pipeline_id=pipeline_id, stage_id=stage_id, scope=scope
    )
    return RedirectResponse(
        url=f"/admin/sales/pipelines?bulk_result=ok&bulk_count={count}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------


@router.get(
    "/quotes",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:quote:read"))],
)
def quotes_list(
    request: Request,
    status: str | None = Query(default=None),
    lead_id: str | None = Query(default=None),
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    context = _ctx(request, db, "sales-quotes")
    context.update(
        web_sales_service.build_quotes_list_context(
            db,
            status=status,
            lead_id=lead_id,
            search=search,
            page=page,
            per_page=per_page,
        )
    )
    return templates.TemplateResponse("admin/sales/quotes/index.html", context)


# NOTE: `/quotes/new` must stay above `/quotes/{quote_id}` or the detail route
# captures "new" as an id.
@router.get(
    "/quotes/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def quote_new(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-quotes")
    context.update(web_sales_service.build_quote_new_context())
    return templates.TemplateResponse("admin/sales/quotes/form.html", context)


@router.post(
    "/quotes",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def quote_create(
    request: Request,
    subscriber_id: str | None = Form(default=None),
    lead_id: str | None = Form(default=None),
    status: str | None = Form(default=None),
    currency: str | None = Form(default=None),
    tax_rate: str | None = Form(default=None),
    expires_at: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    latitude: str | None = Form(default=None),
    longitude: str | None = Form(default=None),
    address: str | None = Form(default=None),
    region: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    fields = {
        "subscriber_id": subscriber_id,
        "lead_id": lead_id,
        "status": status,
        "currency": currency,
        "tax_rate": tax_rate,
        "expires_at": expires_at,
        "notes": notes,
        "latitude": latitude,
        "longitude": longitude,
        "address": address,
        "region": region,
    }
    try:
        quote_id = web_sales_service.create_quote_from_form(db, **fields)
        return RedirectResponse(
            url=f"/admin/sales/quotes/{quote_id}", status_code=303
        )
    except (ValidationError, ValueError) as exc:
        db.rollback()
        error = _error_detail(exc)

    context = _ctx(request, db, "sales-quotes")
    context.update(
        web_sales_service.build_quote_form_error_context(
            mode="create", quote_id=None, **fields
        )
    )
    context["error"] = error
    return templates.TemplateResponse(
        "admin/sales/quotes/form.html", context, status_code=400
    )


@router.get(
    "/quotes/{quote_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:quote:read"))],
)
def quote_detail(request: Request, quote_id: str, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-quotes")
    context.update(web_sales_service.build_quote_detail_context(db, quote_id=quote_id))
    return templates.TemplateResponse("admin/sales/quotes/detail.html", context)


@router.get(
    "/quotes/{quote_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def quote_edit(request: Request, quote_id: str, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-quotes")
    context.update(web_sales_service.build_quote_edit_context(db, quote_id=quote_id))
    return templates.TemplateResponse("admin/sales/quotes/form.html", context)


@router.post(
    "/quotes/{quote_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def quote_update(
    request: Request,
    quote_id: str,
    subscriber_id: str | None = Form(default=None),
    lead_id: str | None = Form(default=None),
    status: str | None = Form(default=None),
    currency: str | None = Form(default=None),
    tax_rate: str | None = Form(default=None),
    expires_at: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    latitude: str | None = Form(default=None),
    longitude: str | None = Form(default=None),
    address: str | None = Form(default=None),
    region: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    fields = {
        "subscriber_id": subscriber_id,
        "lead_id": lead_id,
        "status": status,
        "currency": currency,
        "tax_rate": tax_rate,
        "expires_at": expires_at,
        "notes": notes,
        "latitude": latitude,
        "longitude": longitude,
        "address": address,
        "region": region,
    }
    try:
        web_sales_service.update_quote_from_form(db, quote_id=quote_id, **fields)
        return RedirectResponse(
            url=f"/admin/sales/quotes/{quote_id}", status_code=303
        )
    except (ValidationError, ValueError) as exc:
        db.rollback()
        error = _error_detail(exc)

    context = _ctx(request, db, "sales-quotes")
    context.update(
        web_sales_service.build_quote_form_error_context(
            mode="update", quote_id=quote_id, **fields
        )
    )
    context["error"] = error
    return templates.TemplateResponse(
        "admin/sales/quotes/form.html", context, status_code=400
    )


@router.post(
    "/quotes/{quote_id}/status",
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def quote_set_status(
    quote_id: str,
    status: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    web_sales_service.set_quote_status(db, quote_id, status)
    return RedirectResponse(url=f"/admin/sales/quotes/{quote_id}", status_code=303)


@router.post(
    "/quotes/{quote_id}/delete",
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def quote_delete(quote_id: str, db: Session = Depends(get_db)):
    web_sales_service.deactivate_quote(db, quote_id)
    return RedirectResponse(url="/admin/sales/quotes", status_code=303)


# ---------------------------------------------------------------------------
# Sales orders
# ---------------------------------------------------------------------------


@router.get(
    "/sales-orders",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:sales_order:read"))],
)
def sales_orders_list(
    request: Request,
    status: str | None = Query(default=None),
    payment_status: str | None = Query(default=None),
    source_type: str | None = Query(default=None),
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    context = _ctx(request, db, "sales-orders")
    context.update(
        web_sales_service.build_sales_orders_list_context(
            db,
            status=status,
            payment_status=payment_status,
            source_type=source_type,
            search=search,
            page=page,
            per_page=per_page,
        )
    )
    return templates.TemplateResponse("admin/sales/sales_orders/index.html", context)


@router.get(
    "/sales-orders/{order_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:sales_order:read"))],
)
def sales_order_detail(request: Request, order_id: str, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-orders")
    context.update(
        web_sales_service.build_sales_order_detail_context(db, sales_order_id=order_id)
    )
    return templates.TemplateResponse("admin/sales/sales_orders/detail.html", context)
