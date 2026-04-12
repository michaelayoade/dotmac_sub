"""Admin catalog management web routes."""

import logging
from typing import Any, cast

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db
from app.services import web_admin as web_admin_service
from app.services import web_bulk_tariff_change as web_bulk_tariff_change_service
from app.services import web_catalog_calculator as web_catalog_calculator_service
from app.services import web_catalog_offers as web_catalog_offers_service
from app.services import (
    web_catalog_subscription_workflows as web_catalog_subscription_workflows_service,
)
from app.services import web_catalog_subscriptions as web_catalog_subscriptions_service
from app.services import web_fup as web_fup_service
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data, parse_form_data_sync

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/catalog", tags=["web-admin-catalog"])


def _base_context(
    request: Request, db: Session, active_page: str, active_menu: str = "catalog"
) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _get_actor_id(request: Request) -> str | None:
    return web_admin_service.get_actor_id(request)


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:read"))],
)
def catalog_overview(
    request: Request,
    status: str | None = None,
    plan_kind: str | None = None,
    plan_category: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    page_data = web_catalog_offers_service.overview_page_data(
        db,
        status=status,
        plan_kind=plan_kind,
        plan_category=plan_category,
        search=search,
        page=page,
        per_page=per_page,
    )
    catalog_stats = web_catalog_offers_service.dashboard_stats(db)
    context = _base_context(request, db, active_page="catalog")
    context.update(page_data)
    context["catalog_stats"] = catalog_stats
    return templates.TemplateResponse("admin/catalog/index.html", context)


@router.get("/products", response_class=HTMLResponse)
def catalog_products(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    return RedirectResponse("/admin/catalog", status_code=302)


@router.get("/products/{path:path}", response_class=HTMLResponse)
def catalog_products_redirect(request: Request, path: str) -> RedirectResponse:
    return RedirectResponse("/admin/catalog", status_code=302)


@router.get("/offers", response_class=HTMLResponse)
def catalog_offers(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    return RedirectResponse("/admin/catalog", status_code=302)


@router.get("/offers/create", response_class=HTMLResponse)
def catalog_offers_create(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    offer = web_catalog_offers_service.default_offer_form()
    context = _base_context(request, db, active_page="catalog")
    context.update(web_catalog_offers_service.offer_form_context(db, offer))
    return templates.TemplateResponse("admin/catalog/offer_form.html", context)


@router.post(
    "/offers",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def catalog_offers_create_post(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    result = web_catalog_offers_service.handle_offer_create_form(
        db,
        form=form,
        request=request,
        actor_id=_get_actor_id(request),
    )
    if result.get("redirect_url"):
        return RedirectResponse(str(result["redirect_url"]), status_code=303)
    context = _base_context(request, db, active_page="catalog")
    context.update(cast(dict[str, Any], result["form_context"]))
    return templates.TemplateResponse("admin/catalog/offer_form.html", context)


@router.get("/offers/{offer_id}", response_class=HTMLResponse)
def catalog_offer_detail(
    request: Request, offer_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    detail_context = web_catalog_offers_service.offer_detail_context(db, offer_id)
    if detail_context is None:
        context = _base_context(request, db, active_page="catalog")
        context.update({"message": "Offer not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    context = _base_context(request, db, active_page="catalog")
    context.update(detail_context)
    context.update(web_fup_service.fup_context(request, db, offer_id))
    return templates.TemplateResponse("admin/catalog/offer_detail.html", context)


@router.get("/offers/{offer_id}/edit", response_class=HTMLResponse)
def catalog_offer_edit(
    request: Request, offer_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    form_context = web_catalog_offers_service.offer_edit_form_context(db, offer_id)
    if form_context is None:
        context = _base_context(request, db, active_page="catalog")
        context.update({"message": "Offer not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    context = _base_context(request, db, active_page="catalog")
    context.update(form_context)
    return templates.TemplateResponse("admin/catalog/offer_form.html", context)


@router.post("/offers/{offer_id}/edit", response_class=HTMLResponse)
def catalog_offer_edit_post(
    request: Request,
    offer_id: str,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    result = web_catalog_offers_service.handle_offer_update_form(
        db,
        offer_id=offer_id,
        form=form,
        request=request,
        actor_id=_get_actor_id(request),
    )
    if result.get("not_found"):
        context = _base_context(request, db, active_page="catalog")
        context.update({"message": "Offer not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )
    if result.get("redirect_url"):
        return RedirectResponse(str(result["redirect_url"]), status_code=303)
    context = _base_context(request, db, active_page="catalog")
    context.update(cast(dict[str, Any], result["form_context"]))
    return templates.TemplateResponse("admin/catalog/offer_form.html", context)


# ---------------------------------------------------------------------------
# FUP (Fair Usage Policy) routes
# ---------------------------------------------------------------------------


@router.get(
    "/offers/{offer_id}/fup",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:read"))],
)
def offer_fup(
    offer_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """FUP configuration page for a catalog offer."""
    context = _base_context(request, db, active_page="catalog")
    context.update(web_fup_service.fup_context(request, db, offer_id))
    context["fup_return_to"] = f"/admin/catalog/offers/{offer_id}/fup"
    return templates.TemplateResponse("admin/catalog/fup.html", context)


@router.post(
    "/offers/{offer_id}/fup/settings",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def offer_fup_settings_update(
    offer_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Update FUP policy accounting settings."""
    form = parse_form_data_sync(request)
    web_fup_service.handle_policy_update(db, offer_id, form)
    return RedirectResponse(
        url=web_fup_service.redirect_to_fup_context(form, offer_id),
        status_code=303,
    )


@router.post(
    "/offers/{offer_id}/fup/rules",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def offer_fup_add_rule(
    offer_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Add a new FUP rule."""
    form = parse_form_data_sync(request)
    web_fup_service.handle_add_rule(db, offer_id, form)
    return RedirectResponse(
        url=web_fup_service.redirect_to_fup_context(form, offer_id),
        status_code=303,
    )


@router.post(
    "/offers/{offer_id}/fup/rules/{rule_id}/update",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def offer_fup_update_rule(
    offer_id: str, rule_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Update an existing FUP rule."""
    form = parse_form_data_sync(request)
    web_fup_service.handle_update_rule(db, rule_id, form)
    return RedirectResponse(
        url=web_fup_service.redirect_to_fup_context(form, offer_id),
        status_code=303,
    )


@router.post(
    "/offers/{offer_id}/fup/rules/{rule_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def offer_fup_delete_rule(
    offer_id: str, rule_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Delete an FUP rule."""
    form = parse_form_data_sync(request)
    web_fup_service.handle_delete_rule(db, rule_id)
    return RedirectResponse(
        url=web_fup_service.redirect_to_fup_context(form, offer_id),
        status_code=303,
    )


@router.post(
    "/offers/{offer_id}/fup/clone",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def offer_fup_clone_rules(
    offer_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Clone FUP rules from another offer."""
    form = parse_form_data_sync(request)
    source_offer_id = str(form.get("source_offer_id", ""))
    web_fup_service.handle_clone_rules(db, source_offer_id, offer_id)
    return RedirectResponse(
        url=web_fup_service.redirect_to_fup_context(form, offer_id),
        status_code=303,
    )


@router.post(
    "/offers/{offer_id}/fup/simulate",
    dependencies=[Depends(require_permission("catalog:read"))],
)
def offer_fup_simulate(offer_id: str, request: Request, db: Session = Depends(get_db)):
    """Simulate FUP rules for a given usage scenario. Returns JSON for HTMX."""
    form = parse_form_data_sync(request)
    result = web_fup_service.simulate_offer_fup(db, offer_id, form)
    return JSONResponse(result, status_code=400 if result.get("error") else 200)


# ---------------------------------------------------------------------------
# Tariff Plan Usage Graph
# ---------------------------------------------------------------------------


@router.get(
    "/offers/{offer_id}/usage-graph",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:read"))],
)
def offer_usage_graph_modal(
    offer_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Render the usage graph modal partial for HTMX."""
    context: dict[str, Any] = {"request": request}
    context.update(
        web_catalog_offers_service.offer_usage_graph_modal_context(
            db,
            offer_id,
            period=request.query_params.get("period", "monthly"),
        )
    )
    return templates.TemplateResponse("admin/catalog/_plan_graph_modal.html", context)


@router.get(
    "/offers/{offer_id}/usage-graph/data",
    dependencies=[Depends(require_permission("catalog:read"))],
)
def offer_usage_graph_data(
    offer_id: str,
    period: str = "monthly",
    months: int = 12,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Return JSON data for the plan usage graph."""
    if period not in {"daily", "weekly", "monthly", "quarterly", "annual"}:
        period = "monthly"
    data = web_catalog_offers_service.plan_usage_graph_data(
        db, offer_id, period=period, months=months
    )
    return JSONResponse(data)


@router.get("/subscriptions", response_class=HTMLResponse)
def catalog_subscriptions(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    page_data = web_catalog_subscriptions_service.subscriptions_list_page_data(
        db, status=status, page=page, per_page=per_page
    )
    context = _base_context(request, db, active_page="catalog-subscriptions")
    context.update(page_data)
    return templates.TemplateResponse("admin/catalog/subscriptions.html", context)


@router.get("/subscriptions/new", response_class=HTMLResponse)
def catalog_subscription_new(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    account_id = request.query_params.get("account_id", "").strip()
    subscriber_id = request.query_params.get("subscriber_id", "").strip()
    subscription = web_catalog_subscriptions_service.default_subscription_form(
        account_id, subscriber_id
    )
    context = _base_context(request, db, active_page="catalog-subscriptions")
    context.update(
        web_catalog_subscriptions_service.subscription_form_context(db, subscription)
    )
    return templates.TemplateResponse("admin/catalog/subscription_form.html", context)


@router.post("/subscriptions", response_class=HTMLResponse)
def catalog_subscription_create(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    result = web_catalog_subscription_workflows_service.handle_subscription_create_form(
        db,
        form=form,
        request=request,
        actor_id=_get_actor_id(request),
    )
    if result.get("redirect_url"):
        return RedirectResponse(str(result["redirect_url"]), status_code=303)
    context = _base_context(request, db, active_page="catalog-subscriptions")
    context.update(cast(dict[str, Any], result["form_context"]))
    return templates.TemplateResponse("admin/catalog/subscription_form.html", context)


@router.get("/subscriptions/{subscription_id}/edit", response_class=HTMLResponse)
def catalog_subscription_edit(
    request: Request, subscription_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    form_context = (
        web_catalog_subscription_workflows_service.subscription_edit_form_context(
            db, subscription_id
        )
    )
    if form_context is None:
        context = _base_context(request, db, active_page="catalog-subscriptions")
        context.update({"message": "Subscription not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    context = _base_context(request, db, active_page="catalog-subscriptions")
    context.update(form_context)
    context["notice"] = request.query_params.get("notice")
    context["error"] = request.query_params.get("error") or context.get("error")
    return templates.TemplateResponse("admin/catalog/subscription_form.html", context)


@router.get("/subscriptions/{subscription_id}", response_class=HTMLResponse)
def catalog_subscription_detail(
    request: Request, subscription_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    detail_context = (
        web_catalog_subscription_workflows_service.subscription_detail_page_context(
            db, subscription_id
        )
    )
    if detail_context is None:
        context = _base_context(request, db, active_page="catalog-subscriptions")
        context.update({"message": "Subscription not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )
    context = _base_context(request, db, active_page="catalog-subscriptions")
    context.update(detail_context)
    return templates.TemplateResponse("admin/catalog/subscription_detail.html", context)


@router.post("/subscriptions/{subscription_id}/edit", response_class=HTMLResponse)
def catalog_subscription_update(
    request: Request,
    subscription_id: str,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    result = web_catalog_subscription_workflows_service.handle_subscription_update_form(
        db,
        subscription_id=subscription_id,
        form=form,
        request=request,
        actor_id=_get_actor_id(request),
    )
    if result.get("redirect_url"):
        return RedirectResponse(str(result["redirect_url"]), status_code=303)
    context = _base_context(request, db, active_page="catalog-subscriptions")
    context.update(cast(dict[str, Any], result["form_context"]))
    return templates.TemplateResponse("admin/catalog/subscription_form.html", context)


@router.post(
    "/subscriptions/{subscription_id}/send-credentials",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def catalog_subscription_send_credentials(
    subscription_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    return RedirectResponse(
        web_catalog_subscription_workflows_service.send_subscription_credentials_redirect(
            db,
            subscription_id=subscription_id,
        ),
        status_code=303,
    )


@router.post(
    "/subscriptions/bulk/activate",
    dependencies=[Depends(require_permission("catalog:write"))],
)
def subscription_bulk_activate(
    request: Request,
    subscription_ids: str = Form(...),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Bulk activate subscriptions."""
    return JSONResponse(
        web_catalog_subscription_workflows_service.bulk_activate_response(
            db,
            subscription_ids=subscription_ids,
            request=request,
            actor_id=_get_actor_id(request),
        )
    )


@router.post(
    "/subscriptions/bulk/suspend",
    dependencies=[Depends(require_permission("catalog:write"))],
)
def subscription_bulk_suspend(
    request: Request,
    subscription_ids: str = Form(...),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Bulk suspend subscriptions."""
    return JSONResponse(
        web_catalog_subscription_workflows_service.bulk_suspend_response(
            db,
            subscription_ids=subscription_ids,
            request=request,
            actor_id=_get_actor_id(request),
        )
    )


@router.post(
    "/subscriptions/bulk/cancel",
    dependencies=[Depends(require_permission("catalog:write"))],
)
def subscription_bulk_cancel(
    request: Request,
    subscription_ids: str = Form(...),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Bulk cancel subscriptions."""
    return JSONResponse(
        web_catalog_subscription_workflows_service.bulk_cancel_response(
            db,
            subscription_ids=subscription_ids,
            request=request,
            actor_id=_get_actor_id(request),
        )
    )


@router.post(
    "/subscriptions/bulk/change-plan",
    dependencies=[Depends(require_permission("catalog:write"))],
)
def subscription_bulk_change_plan(
    request: Request,
    subscription_ids: str = Form(...),
    target_offer_id: str = Form(...),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Bulk change plan/offer for subscriptions."""
    return JSONResponse(
        web_catalog_subscription_workflows_service.bulk_change_plan_response(
            db,
            subscription_ids=subscription_ids,
            target_offer_id=target_offer_id,
            request=request,
            actor_id=_get_actor_id(request),
        )
    )


@router.get("/calculator", response_class=HTMLResponse)
def pricing_calculator(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Pricing calculator tool to test and validate offers."""
    page_data = web_catalog_calculator_service.calculator_page_data(db)
    context = _base_context(request, db, active_page="catalog-calculator")
    context.update(page_data)
    return templates.TemplateResponse("admin/catalog/calculator.html", context)


# ---------------------------------------------------------------------------
# Bulk Tariff Change routes
# ---------------------------------------------------------------------------


@router.get(
    "/bulk-tariff-change",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def bulk_tariff_change_page(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Bulk tariff change wizard."""
    context = _base_context(request, db, active_page="catalog")
    context.update(web_bulk_tariff_change_service.page_context(request, db))
    return templates.TemplateResponse("admin/catalog/bulk_tariff_change.html", context)


@router.post(
    "/bulk-tariff-change/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def bulk_tariff_change_preview(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Preview bulk tariff change results."""
    context = _base_context(request, db, active_page="catalog")
    context.update(web_bulk_tariff_change_service.preview_context(request, db, form))
    return templates.TemplateResponse("admin/catalog/bulk_tariff_change.html", context)


@router.post(
    "/bulk-tariff-change/execute",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def bulk_tariff_change_execute(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Execute the bulk tariff change."""
    context = _base_context(request, db, active_page="catalog")
    context.update(web_bulk_tariff_change_service.execute_context(request, db, form))
    return templates.TemplateResponse("admin/catalog/bulk_tariff_change.html", context)
