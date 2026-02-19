"""Admin catalog management web routes."""


from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db
from app.models.catalog import SubscriptionStatus
from app.services import catalog as catalog_service
from app.services import web_catalog_calculator as web_catalog_calculator_service
from app.services import web_catalog_offers as web_catalog_offers_service
from app.services import web_catalog_subscriptions as web_catalog_subscriptions_service
from app.services.audit_helpers import build_audit_activities
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/catalog", tags=["web-admin-catalog"])


def _base_context(request: Request, db: Session, active_page: str, active_menu: str = "catalog") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _get_actor_id(request: Request) -> str | None:
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    return str(current_user.get("subscriber_id")) if current_user else None


@router.get("", response_class=HTMLResponse)
def catalog_overview(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    page_data = web_catalog_offers_service.overview_page_data(
        db, status=status, search=search, page=page, per_page=per_page
    )
    context = _base_context(request, db, active_page="catalog")
    context.update(page_data)
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
def catalog_offers_create(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    offer = web_catalog_offers_service.default_offer_form()
    context = _base_context(request, db, active_page="catalog")
    context.update(web_catalog_offers_service.offer_form_context(db, offer))
    return templates.TemplateResponse("admin/catalog/offer_form.html", context)


@router.post("/offers", response_class=HTMLResponse)
def catalog_offers_create_post(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    return_to_raw = form.get("return_to")
    return_to = return_to_raw.strip() if isinstance(return_to_raw, str) else ""
    offer = web_catalog_offers_service.parse_offer_form(form)
    error = web_catalog_offers_service.validate_offer_form(offer)
    if error:
        context = _base_context(request, db, active_page="catalog")
        context.update(
            web_catalog_offers_service.offer_form_context(
                db, offer, error or "Please correct the highlighted fields."
            )
        )
        return templates.TemplateResponse("admin/catalog/offer_form.html", context)

    try:
        actor_id = _get_actor_id(request)
        web_catalog_offers_service.create_offer_with_audit(
            db, offer, form, request, actor_id
        )
        return RedirectResponse(return_to or "/admin/catalog/offers", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]

    context = _base_context(request, db, active_page="catalog")
    context.update(
        web_catalog_offers_service.offer_form_context(
            db, offer, error or "Please correct the highlighted fields."
        )
    )
    return templates.TemplateResponse("admin/catalog/offer_form.html", context)


@router.get("/offers/{offer_id}", response_class=HTMLResponse)
def catalog_offer_detail(request: Request, offer_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        offer = catalog_service.offers.get(db=db, offer_id=offer_id)
    except Exception:
        context = _base_context(request, db, active_page="catalog")
        context.update({"message": "Offer not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    prices = catalog_service.offer_prices.list(
        db=db,
        offer_id=offer_id,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    subscriptions = catalog_service.subscriptions.list(
        db=db,
        subscriber_id=None,
        offer_id=offer_id,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )

    context = _base_context(request, db, active_page="catalog")
    context.update({
        "offer": offer,
        "prices": prices,
        "subscriptions": subscriptions,
        "activities": build_audit_activities(db, "catalog_offer", str(offer_id), limit=10),
    })
    return templates.TemplateResponse("admin/catalog/offer_detail.html", context)


@router.get("/offers/{offer_id}/edit", response_class=HTMLResponse)
def catalog_offer_edit(request: Request, offer_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        offer = catalog_service.offers.get(db=db, offer_id=offer_id)
    except Exception:
        context = _base_context(request, db, active_page="catalog")
        context.update({"message": "Offer not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    offer_data, offer_addon_links = web_catalog_offers_service.offer_edit_form_data(
        db, offer_id, offer
    )
    context = _base_context(request, db, active_page="catalog")
    context.update(
        web_catalog_offers_service.offer_form_context(
            db, offer_data, offer_addon_links=offer_addon_links
        )
    )
    context["action_url"] = f"/admin/catalog/offers/{offer_id}/edit"
    return templates.TemplateResponse("admin/catalog/offer_form.html", context)


@router.post("/offers/{offer_id}/edit", response_class=HTMLResponse)
def catalog_offer_edit_post(
    request: Request,
    offer_id: str,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    try:
        existing_offer = catalog_service.offers.get(db=db, offer_id=offer_id)
    except Exception:
        context = _base_context(request, db, active_page="catalog")
        context.update({"message": "Offer not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    offer_data = web_catalog_offers_service.parse_offer_form(form)
    error = web_catalog_offers_service.validate_offer_form(offer_data)
    if error:
        context = _base_context(request, db, active_page="catalog")
        context.update(web_catalog_offers_service.offer_form_context(db, offer_data, error))
        context["action_url"] = f"/admin/catalog/offers/{offer_id}/edit"
        return templates.TemplateResponse("admin/catalog/offer_form.html", context)

    try:
        actor_id = _get_actor_id(request)
        web_catalog_offers_service.update_offer_with_audit(
            db, offer_id, existing_offer, offer_data, form, request, actor_id
        )
        return RedirectResponse(f"/admin/catalog/offers/{offer_id}", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="catalog")
    context.update(
        web_catalog_offers_service.offer_form_context(
            db, offer_data, error or "Please correct the highlighted fields."
        )
    )
    context["action_url"] = f"/admin/catalog/offers/{offer_id}/edit"
    return templates.TemplateResponse("admin/catalog/offer_form.html", context)


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
    context = _base_context(request, db, active_page="subscriptions")
    context.update(page_data)
    return templates.TemplateResponse("admin/catalog/subscriptions.html", context)


@router.get("/subscriptions/new", response_class=HTMLResponse)
def catalog_subscription_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    account_id = request.query_params.get("account_id", "").strip()
    subscriber_id = request.query_params.get("subscriber_id", "").strip()
    subscription = web_catalog_subscriptions_service.default_subscription_form(
        account_id, subscriber_id
    )
    context = _base_context(request, db, active_page="subscriptions")
    context.update(web_catalog_subscriptions_service.subscription_form_context(db, subscription))
    return templates.TemplateResponse("admin/catalog/subscription_form.html", context)


@router.post("/subscriptions", response_class=HTMLResponse)
def catalog_subscription_create(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    subscription = web_catalog_subscriptions_service.parse_subscription_form(form)
    subscriber_id = str(subscription.get("subscriber_id") or "")
    error = web_catalog_subscriptions_service.resolve_account_id(db, subscription)
    if not error:
        error = web_catalog_subscriptions_service.validate_subscription_form(
            subscription, for_create=True
        )
    if error:
        context = _base_context(request, db, active_page="subscriptions")
        context.update(
            web_catalog_subscriptions_service.subscription_form_context(db, subscription, error)
        )
        return templates.TemplateResponse("admin/catalog/subscription_form.html", context)

    payload_data = web_catalog_subscriptions_service.build_payload_data(subscription)

    try:
        actor_id = _get_actor_id(request)
        web_catalog_subscriptions_service.create_subscription_with_audit(
            db, payload_data, form, request, actor_id
        )
        if subscriber_id:
            return RedirectResponse(f"/admin/subscribers/{subscriber_id}", status_code=303)
        return RedirectResponse("/admin/catalog/subscriptions", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = web_catalog_subscriptions_service.error_message(exc)

    context = _base_context(request, db, active_page="subscriptions")
    context.update(
        web_catalog_subscriptions_service.subscription_form_context(
            db, subscription, error or "Please correct the highlighted fields."
        )
    )
    return templates.TemplateResponse("admin/catalog/subscription_form.html", context)


@router.get("/subscriptions/{subscription_id}/edit", response_class=HTMLResponse)
def catalog_subscription_edit(
    request: Request, subscription_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    try:
        subscription_obj = catalog_service.subscriptions.get(
            db=db,
            subscription_id=subscription_id,
        )
    except Exception:
        context = _base_context(request, db, active_page="subscriptions")
        context.update({"message": "Subscription not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    subscription = web_catalog_subscriptions_service.edit_form_data(subscription_obj)
    context = _base_context(request, db, active_page="subscriptions")
    context.update(web_catalog_subscriptions_service.subscription_form_context(db, subscription))
    context["activities"] = build_audit_activities(db, "subscription", str(subscription_id))
    context["action_url"] = f"/admin/catalog/subscriptions/{subscription_id}/edit"
    return templates.TemplateResponse("admin/catalog/subscription_form.html", context)


@router.get("/subscriptions/{subscription_id}", response_class=HTMLResponse)
def catalog_subscription_detail(
    request: Request, subscription_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    try:
        subscription = catalog_service.subscriptions.get(
            db=db,
            subscription_id=subscription_id,
        )
    except Exception:
        context = _base_context(request, db, active_page="subscriptions")
        context.update({"message": "Subscription not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )
    context = _base_context(request, db, active_page="subscriptions")
    context.update(
        {
            "subscription": subscription,
            "activities": build_audit_activities(db, "subscription", str(subscription_id)),
        }
    )
    return templates.TemplateResponse("admin/catalog/subscription_detail.html", context)


@router.post("/subscriptions/{subscription_id}/edit", response_class=HTMLResponse)
def catalog_subscription_update(
    request: Request,
    subscription_id: str,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    subscription = web_catalog_subscriptions_service.parse_subscription_form(
        form, subscription_id=subscription_id
    )
    error = web_catalog_subscriptions_service.validate_subscription_form(
        subscription, for_create=False
    )
    if error:
        context = _base_context(request, db, active_page="subscriptions")
        context.update(
            web_catalog_subscriptions_service.subscription_form_context(db, subscription, error)
        )
        context["action_url"] = f"/admin/catalog/subscriptions/{subscription_id}/edit"
        return templates.TemplateResponse("admin/catalog/subscription_form.html", context)

    payload_data = web_catalog_subscriptions_service.build_payload_data(subscription)

    try:
        actor_id = _get_actor_id(request)
        web_catalog_subscriptions_service.update_subscription_with_audit(
            db, subscription_id, payload_data, request, actor_id
        )
        return RedirectResponse("/admin/catalog/subscriptions", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = web_catalog_subscriptions_service.error_message(exc)

    context = _base_context(request, db, active_page="subscriptions")
    context.update(
        web_catalog_subscriptions_service.subscription_form_context(
            db, subscription, error or "Please correct the highlighted fields."
        )
    )
    context["action_url"] = f"/admin/catalog/subscriptions/{subscription_id}/edit"
    return templates.TemplateResponse("admin/catalog/subscription_form.html", context)


@router.post("/subscriptions/bulk/activate", dependencies=[Depends(require_permission("catalog:write"))])
def subscription_bulk_activate(
    request: Request,
    subscription_ids: str = Form(...),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Bulk activate subscriptions."""
    actor_id = _get_actor_id(request)
    count = web_catalog_subscriptions_service.bulk_update_status(
        db,
        subscription_ids,
        target_status=SubscriptionStatus.active,
        allowed_from=[SubscriptionStatus.pending, SubscriptionStatus.suspended],
        request=request,
        actor_id=actor_id,
    )
    return JSONResponse({"message": f"Activated {count} subscriptions", "count": count})


@router.post("/subscriptions/bulk/suspend", dependencies=[Depends(require_permission("catalog:write"))])
def subscription_bulk_suspend(
    request: Request,
    subscription_ids: str = Form(...),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Bulk suspend subscriptions."""
    actor_id = _get_actor_id(request)
    count = web_catalog_subscriptions_service.bulk_update_status(
        db,
        subscription_ids,
        target_status=SubscriptionStatus.suspended,
        allowed_from=[SubscriptionStatus.active],
        request=request,
        actor_id=actor_id,
    )
    return JSONResponse({"message": f"Suspended {count} subscriptions", "count": count})


@router.post("/subscriptions/bulk/cancel", dependencies=[Depends(require_permission("catalog:write"))])
def subscription_bulk_cancel(
    request: Request,
    subscription_ids: str = Form(...),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Bulk cancel subscriptions."""
    actor_id = _get_actor_id(request)
    count = web_catalog_subscriptions_service.bulk_update_status(
        db,
        subscription_ids,
        target_status=SubscriptionStatus.canceled,
        allowed_from=[
            SubscriptionStatus.active,
            SubscriptionStatus.pending,
            SubscriptionStatus.suspended,
        ],
        request=request,
        actor_id=actor_id,
    )
    return JSONResponse({"message": f"Canceled {count} subscriptions", "count": count})


@router.get("/calculator", response_class=HTMLResponse)
def pricing_calculator(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Pricing calculator tool to test and validate offers."""
    page_data = web_catalog_calculator_service.calculator_page_data(db)
    context = _base_context(request, db, active_page="calculator")
    context.update(page_data)
    return templates.TemplateResponse("admin/catalog/calculator.html", context)
