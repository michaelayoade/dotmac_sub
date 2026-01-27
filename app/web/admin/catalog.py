"""Admin catalog management web routes."""

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session
from typing import Optional
from uuid import UUID

from app.db import SessionLocal
from app.schemas.catalog import (
    CatalogOfferCreate,
    CatalogOfferUpdate,
    OfferPriceCreate,
    OfferPriceUpdate,
    OfferRadiusProfileCreate,
    OfferRadiusProfileUpdate,
    SubscriptionCreate,
    SubscriptionUpdate,
)
from app.services import catalog as catalog_service
from app.services import audit as audit_service
from app.services import settings_spec
from app.services.audit_helpers import (
    build_changes_metadata,
    diff_dicts,
    extract_changes,
    format_changes,
    log_audit_event,
    model_to_dict,
)
from app.services.auth_dependencies import require_permission
from app.models.subscriber import Subscriber
from app.services import subscriber as subscriber_service
from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    ContractTerm,
    NasDevice,
    OfferStatus,
    PriceBasis,
    PriceUnit,
    GuaranteedSpeedType,
    RadiusProfile,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.domain_settings import SettingDomain

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/catalog", tags=["web-admin-catalog"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _base_context(request: Request, db: Session, active_page: str, active_menu: str = "catalog"):
    from app.web.admin import get_sidebar_stats, get_current_user
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _offer_form_context(
    request: Request,
    db: Session,
    offer: dict,
    error: str | None = None,
    offer_addon_links: list | None = None,
):
    default_billing_mode = settings_spec.resolve_value(
        db, SettingDomain.catalog, "default_billing_mode"
    ) or BillingMode.prepaid.value
    if not offer.get("billing_mode"):
        offer["billing_mode"] = default_billing_mode
    region_zones = catalog_service.region_zones.list(
        db=db, is_active=True, order_by="name", order_dir="asc", limit=200, offset=0
    )
    usage_allowances = catalog_service.usage_allowances.list(
        db=db, is_active=True, order_by="name", order_dir="asc", limit=200, offset=0
    )
    sla_profiles = catalog_service.sla_profiles.list(
        db=db, is_active=True, order_by="name", order_dir="asc", limit=200, offset=0
    )
    radius_profiles = catalog_service.radius_profiles.list(
        db=db, vendor=None, is_active=True, order_by="name", order_dir="asc", limit=200, offset=0
    )
    policy_sets = catalog_service.policy_sets.list(
        db=db, is_active=True, order_by="name", order_dir="asc", limit=200, offset=0
    )
    # Get all active add-ons for linking
    add_ons = catalog_service.add_ons.list(
        db=db, is_active=True, addon_type=None, order_by="name", order_dir="asc", limit=200, offset=0
    )

    # Build offer addon links map for the form
    addon_links_map = {}
    if offer_addon_links:
        for link in offer_addon_links:
            addon_links_map[str(link.add_on_id)] = {
                "is_required": link.is_required,
                "min_quantity": link.min_quantity,
                "max_quantity": link.max_quantity,
            }

    context = _base_context(request, db, active_page="catalog")
    context.update(
        {
            "offer": offer,
            "region_zones": region_zones,
            "usage_allowances": usage_allowances,
            "sla_profiles": sla_profiles,
            "radius_profiles": radius_profiles,
            "policy_sets": policy_sets,
            "add_ons": add_ons,
            "addon_links_map": addon_links_map,
            "service_types": [item.value for item in ServiceType],
            "access_types": [item.value for item in AccessType],
            "price_bases": [item.value for item in PriceBasis],
            "billing_cycles": [BillingCycle.monthly.value, BillingCycle.annual.value],
            "billing_modes": [item.value for item in BillingMode],
            "contract_terms": [item.value for item in ContractTerm],
            "offer_statuses": [item.value for item in OfferStatus],
            "price_units": [item.value for item in PriceUnit],
            "guaranteed_speed_types": [item.value for item in GuaranteedSpeedType],
            "action_url": "/admin/catalog/offers",
        }
    )
    if error:
        context["error"] = error
    return context


def _build_audit_activities(
    db: Session,
    entity_type: str,
    entity_id: str,
    limit: int = 10,
) -> list[dict]:
    events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type=entity_type,
        entity_id=entity_id,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=limit,
        offset=0,
    )
    actor_ids = {str(event.actor_id) for event in events if getattr(event, "actor_id", None)}
    people = {}
    if actor_ids:
        people = {
            str(person.id): person
            for person in db.query(Subscriber).filter(Subscriber.id.in_(actor_ids)).all()
        }
    activities = []
    for event in events:
        actor = people.get(str(event.actor_id)) if getattr(event, "actor_id", None) else None
        actor_name = f"{actor.first_name} {actor.last_name}".strip() if actor else "System"
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        activities.append(
            {
                "title": (event.action or "Activity").replace("_", " ").title(),
                "description": f"{actor_name}" + (f" · {change_summary}" if change_summary else ""),
                "occurred_at": event.occurred_at,
            }
        )
    return activities


def _parse_addon_links_from_form(form) -> list[dict]:
    """Parse addon link configurations from form data.

    Form fields are expected in the format:
    - addon_link_{addon_id}: 'true' if addon is selected
    - addon_required_{addon_id}: 'true' if addon is required
    - addon_min_qty_{addon_id}: minimum quantity (optional)
    - addon_max_qty_{addon_id}: maximum quantity (optional)
    """
    addon_configs = []
    # Find all selected addon checkboxes
    for key in form.keys():
        if key.startswith("addon_link_"):
            addon_id = key.replace("addon_link_", "")
            if form.get(key) == "true":
                is_required = form.get(f"addon_required_{addon_id}") == "true"
                min_qty_str = form.get(f"addon_min_qty_{addon_id}", "").strip()
                max_qty_str = form.get(f"addon_max_qty_{addon_id}", "").strip()

                config = {
                    "add_on_id": addon_id,
                    "is_required": is_required,
                    "min_quantity": int(min_qty_str) if min_qty_str else None,
                    "max_quantity": int(max_qty_str) if max_qty_str else None,
                }
                addon_configs.append(config)
    return addon_configs


def _subscription_form_context(request: Request, db: Session, subscription: dict, error: str | None = None):
    default_billing_mode = settings_spec.resolve_value(
        db, SettingDomain.catalog, "default_billing_mode"
    ) or BillingMode.prepaid.value
    if not subscription.get("billing_mode"):
        subscription["billing_mode"] = default_billing_mode
    accounts = subscriber_service.accounts.list(
        db=db,
        subscriber_id=None,
        reseller_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    offers = catalog_service.offers.list(
        db=db,
        service_type=None,
        access_type=None,
        status=OfferStatus.active.value,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    # Get NAS devices and RADIUS profiles for provisioning
    nas_devices = (
        db.query(NasDevice)
        .filter(NasDevice.is_active.is_(True))
        .order_by(NasDevice.name)
        .all()
    )
    radius_profiles = (
        db.query(RadiusProfile)
        .filter(RadiusProfile.is_active.is_(True))
        .order_by(RadiusProfile.name)
        .all()
    )
    context = _base_context(request, db, active_page="subscriptions")
    subscriber_label = ""
    subscriber_id = subscription.get("subscriber_id") if isinstance(subscription, dict) else None
    if subscriber_id:
        try:
            subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
            if subscriber.person:
                subscriber_label = f"{subscriber.person.first_name} {subscriber.person.last_name}"
            elif subscriber.person and subscriber.person.organization:
                subscriber_label = subscriber.person.organization.name
            else:
                subscriber_label = "Subscriber"
            if subscriber.subscriber_number:
                subscriber_label = f"{subscriber_label} ({subscriber.subscriber_number})"
        except Exception:
            subscriber_label = ""
    context.update(
        {
            "subscription": subscription,
            "accounts": accounts,
            "offers": offers,
            "nas_devices": nas_devices,
            "radius_profiles": radius_profiles,
            "subscription_statuses": [item.value for item in SubscriptionStatus],
            "billing_modes": [item.value for item in BillingMode],
            "contract_terms": [item.value for item in ContractTerm],
            "action_url": "/admin/catalog/subscriptions",
            "subscriber_label": subscriber_label,
            "billing_mode_help_text": settings_spec.resolve_value(
                db, SettingDomain.catalog, "billing_mode_help_text"
            ) or "Overrides tariff default.",
            "billing_mode_prepaid_notice": settings_spec.resolve_value(
                db, SettingDomain.catalog, "billing_mode_prepaid_notice"
            ) or "Balance enforcement applies.",
            "billing_mode_postpaid_notice": settings_spec.resolve_value(
                db, SettingDomain.catalog, "billing_mode_postpaid_notice"
            ) or "This subscription follows dunning steps.",
        }
    )
    if error:
        context["error"] = error
    return context


def _subscription_activities(db: Session, subscription_id: str) -> list[dict]:
    audit_events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type="subscription",
        entity_id=str(subscription_id),
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    actor_ids = {str(event.actor_id) for event in audit_events if getattr(event, "actor_id", None)}
    people = {}
    if actor_ids:
        people = {
            str(person.id): person
            for person in db.query(Subscriber).filter(Subscriber.id.in_(actor_ids)).all()
        }
    activities = []
    for event in audit_events:
        actor = people.get(str(event.actor_id)) if getattr(event, "actor_id", None) else None
        actor_name = f"{actor.first_name} {actor.last_name}".strip() if actor else "System"
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        activities.append(
            {
                "title": (event.action or "Activity").replace("_", " ").title(),
                "description": f"{actor_name}" + (f" · {change_summary}" if change_summary else ""),
                "occurred_at": event.occurred_at,
            }
        )
    return activities


@router.get("", response_class=HTMLResponse)
def catalog_overview(
    request: Request,
    status: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    query = db.query(CatalogOffer)
    if search:
        query = query.filter(
            CatalogOffer.name.ilike(f"%{search}%") | CatalogOffer.code.ilike(f"%{search}%")
        )
    if status:
        query = query.filter(CatalogOffer.status == OfferStatus(status))
    total = query.count()
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    offers = (
        query.order_by(CatalogOffer.created_at.desc())
        .limit(per_page)
        .offset(offset)
        .all()
    )
    offer_ids = [offer.id for offer in offers]
    offer_subscription_counts = {}
    offer_active_subscription_counts = {}
    if offer_ids:
        rows = (
            db.query(Subscription.offer_id, func.count(Subscription.id))
            .filter(Subscription.offer_id.in_(offer_ids))
            .group_by(Subscription.offer_id)
            .all()
        )
        offer_subscription_counts = {str(row[0]): row[1] for row in rows}
        active_rows = (
            db.query(Subscription.offer_id, func.count(Subscription.id))
            .filter(Subscription.offer_id.in_(offer_ids))
            .filter(Subscription.status == SubscriptionStatus.active)
            .group_by(Subscription.offer_id)
            .all()
        )
        offer_active_subscription_counts = {str(row[0]): row[1] for row in active_rows}

    radius_profiles = catalog_service.radius_profiles.list(
        db=db, vendor=None, is_active=True, order_by="name", order_dir="asc", limit=200, offset=0
    )
    context = _base_context(request, db, active_page="catalog")
    context.update(
        {
            "offers": offers,
            "offer_subscription_counts": offer_subscription_counts,
            "offer_active_subscription_counts": offer_active_subscription_counts,
            "status": status,
            "search": search,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "radius_profiles": radius_profiles,
            "service_types": [item.value for item in ServiceType],
            "access_types": [item.value for item in AccessType],
            "price_bases": [item.value for item in PriceBasis],
            "billing_cycles": [BillingCycle.monthly.value, BillingCycle.annual.value],
            "contract_terms": [item.value for item in ContractTerm],
            "offer_statuses": [item.value for item in OfferStatus],
        }
    )
    return templates.TemplateResponse("admin/catalog/index.html", context)


@router.get("/products", response_class=HTMLResponse)
def catalog_products(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    return RedirectResponse("/admin/catalog", status_code=302)


@router.get("/products/{path:path}", response_class=HTMLResponse)
def catalog_products_redirect(request: Request, path: str):
    return RedirectResponse("/admin/catalog", status_code=302)


@router.get("/offers", response_class=HTMLResponse)
def catalog_offers(
    request: Request,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    return RedirectResponse("/admin/catalog", status_code=302)


@router.get("/offers/create", response_class=HTMLResponse)
def catalog_offers_create(request: Request, db: Session = Depends(get_db)):
    offer = {
        "name": "",
        "code": "",
        "service_type": ServiceType.residential.value,
        "access_type": AccessType.fiber.value,
        "price_basis": PriceBasis.flat.value,
        "billing_cycle": BillingCycle.monthly.value,
        "billing_mode": "",
        "contract_term": ContractTerm.month_to_month.value,
        "region_zone_id": "",
        "usage_allowance_id": "",
        "sla_profile_id": "",
        "radius_profile_id": "",
        "policy_set_id": "",
        "splynx_tariff_id": "",
        "splynx_service_name": "",
        "splynx_tax_id": "",
        "with_vat": False,
        "vat_percent": "",
        "speed_download_mbps": "",
        "speed_upload_mbps": "",
        "guaranteed_speed_limit_at": "",
        "guaranteed_speed": GuaranteedSpeedType.none.value,
        "aggregation": "",
        "priority": "",
        "available_for_services": True,
        "show_on_customer_portal": True,
        "status": OfferStatus.active.value,
        "description": "",
        "is_active": True,
        "price_id": "",
        "price_amount": "",
        "price_currency": "NGN",
        "price_billing_cycle": BillingCycle.monthly.value,
        "price_unit": PriceUnit.month.value,
        "price_description": "",
    }
    context = _offer_form_context(request, db, offer)
    return templates.TemplateResponse("admin/catalog/offer_form.html", context)


@router.post("/offers", response_class=HTMLResponse)
async def catalog_offers_create_post(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    return_to = form.get("return_to", "").strip()
    offer = {
        "name": form.get("name", "").strip(),
        "code": form.get("code", "").strip(),
        "service_type": form.get("service_type", "").strip(),
        "access_type": form.get("access_type", "").strip(),
        "price_basis": form.get("price_basis", "").strip(),
        "billing_cycle": form.get("billing_cycle", "").strip(),
        "billing_mode": form.get("billing_mode", "").strip(),
        "contract_term": form.get("contract_term", "").strip(),
        "region_zone_id": form.get("region_zone_id", "").strip(),
        "usage_allowance_id": form.get("usage_allowance_id", "").strip(),
        "sla_profile_id": form.get("sla_profile_id", "").strip(),
        "radius_profile_id": form.get("radius_profile_id", "").strip(),
        "policy_set_id": form.get("policy_set_id", "").strip(),
        "splynx_tariff_id": form.get("splynx_tariff_id", "").strip(),
        "splynx_service_name": form.get("splynx_service_name", "").strip(),
        "splynx_tax_id": form.get("splynx_tax_id", "").strip(),
        "with_vat": form.get("with_vat") == "true",
        "vat_percent": form.get("vat_percent", "").strip(),
        "speed_download_mbps": form.get("speed_download_mbps", "").strip(),
        "speed_upload_mbps": form.get("speed_upload_mbps", "").strip(),
        "guaranteed_speed_limit_at": form.get("guaranteed_speed_limit_at", "").strip(),
        "guaranteed_speed": form.get("guaranteed_speed", "").strip(),
        "aggregation": form.get("aggregation", "").strip(),
        "priority": form.get("priority", "").strip(),
        "available_for_services": form.get("available_for_services") == "true",
        "show_on_customer_portal": form.get("show_on_customer_portal") == "true",
        "status": form.get("status", "").strip(),
        "description": form.get("description", "").strip(),
        "is_active": form.get("is_active") == "true",
        "price_id": form.get("price_id", "").strip(),
        "price_amount": form.get("price_amount", "").strip(),
        "price_currency": form.get("price_currency", "NGN").strip(),
        "price_billing_cycle": form.get("price_billing_cycle", "").strip(),
        "price_unit": form.get("price_unit", "").strip(),
        "price_description": form.get("price_description", "").strip(),
    }

    error = None
    if not offer["radius_profile_id"]:
        error = "RADIUS profile is required."
    if not offer["price_amount"]:
        error = "Recurring price is required."
    if error:
        context = _offer_form_context(
            request, db, offer, error or "Please correct the highlighted fields."
        )
        return templates.TemplateResponse("admin/catalog/offer_form.html", context)

    try:
        payload_data = {
            "name": offer["name"],
            "service_type": offer["service_type"],
            "access_type": offer["access_type"],
            "price_basis": offer["price_basis"],
            "is_active": offer["is_active"],
        }
        if offer["code"]:
            payload_data["code"] = offer["code"]
        if offer["billing_cycle"]:
            payload_data["billing_cycle"] = offer["billing_cycle"]
        if offer["billing_mode"]:
            payload_data["billing_mode"] = offer["billing_mode"]
        if offer["contract_term"]:
            payload_data["contract_term"] = offer["contract_term"]
        if offer["region_zone_id"]:
            payload_data["region_zone_id"] = offer["region_zone_id"]
        if offer["usage_allowance_id"]:
            payload_data["usage_allowance_id"] = offer["usage_allowance_id"]
        if offer["sla_profile_id"]:
            payload_data["sla_profile_id"] = offer["sla_profile_id"]
        if offer["policy_set_id"]:
            payload_data["policy_set_id"] = offer["policy_set_id"]
        if offer["splynx_tariff_id"]:
            payload_data["splynx_tariff_id"] = offer["splynx_tariff_id"]
        if offer["splynx_service_name"]:
            payload_data["splynx_service_name"] = offer["splynx_service_name"]
        if offer["splynx_tax_id"]:
            payload_data["splynx_tax_id"] = offer["splynx_tax_id"]
        payload_data["with_vat"] = offer["with_vat"]
        if offer["vat_percent"]:
            payload_data["vat_percent"] = offer["vat_percent"]
        if offer["speed_download_mbps"]:
            payload_data["speed_download_mbps"] = offer["speed_download_mbps"]
        if offer["speed_upload_mbps"]:
            payload_data["speed_upload_mbps"] = offer["speed_upload_mbps"]
        if offer["guaranteed_speed_limit_at"]:
            payload_data["guaranteed_speed_limit_at"] = offer["guaranteed_speed_limit_at"]
        if offer["guaranteed_speed"]:
            payload_data["guaranteed_speed"] = offer["guaranteed_speed"]
        if offer["aggregation"]:
            payload_data["aggregation"] = offer["aggregation"]
        if offer["priority"]:
            payload_data["priority"] = offer["priority"]
        payload_data["available_for_services"] = offer["available_for_services"]
        payload_data["show_on_customer_portal"] = offer["show_on_customer_portal"]
        if offer["status"]:
            payload_data["status"] = offer["status"]
        if offer["description"]:
            payload_data["description"] = offer["description"]

        payload = CatalogOfferCreate(**payload_data)
        created_offer = catalog_service.offers.create(db=db, payload=payload)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="catalog_offer",
            entity_id=str(created_offer.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "name": created_offer.name,
                "service_type": created_offer.service_type.value if created_offer.service_type else None,
            },
        )
        if offer["radius_profile_id"]:
            catalog_service.offer_radius_profiles.create(
                db=db,
                payload=OfferRadiusProfileCreate(
                    offer_id=created_offer.id,
                    profile_id=offer["radius_profile_id"],
                ),
            )

        # Sync addon links
        addon_configs = _parse_addon_links_from_form(form)
        if addon_configs:
            catalog_service.offer_addons.sync(
                db=db,
                offer_id=str(created_offer.id),
                addon_configs=addon_configs,
            )

        if offer["price_amount"]:
            price_payload = {
                "offer_id": created_offer.id,
                "amount": offer["price_amount"],
                "currency": offer["price_currency"],
            }
            if offer["price_billing_cycle"]:
                price_payload["billing_cycle"] = offer["price_billing_cycle"]
            if offer["price_unit"]:
                price_payload["unit"] = offer["price_unit"]
            if offer["price_description"]:
                price_payload["description"] = offer["price_description"]
            price = OfferPriceCreate(**price_payload)
            created_price = catalog_service.offer_prices.create(db=db, payload=price)
            log_audit_event(
                db=db,
                request=request,
                action="price_created",
                entity_type="catalog_offer",
                entity_id=str(created_offer.id),
                actor_id=str(current_user.get("subscriber_id")) if current_user else None,
                metadata={
                    "price_amount": str(created_price.amount),
                    "currency": created_price.currency,
                },
            )

        return RedirectResponse(return_to or "/admin/catalog/offers", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]

    context = _offer_form_context(
        request, db, offer, error or "Please correct the highlighted fields."
    )
    return templates.TemplateResponse("admin/catalog/offer_form.html", context)


@router.get("/offers/{offer_id}", response_class=HTMLResponse)
def catalog_offer_detail(request: Request, offer_id: str, db: Session = Depends(get_db)):
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

    # Get prices and subscriptions
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
        account_id=None,
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
        "activities": _build_audit_activities(db, "catalog_offer", str(offer_id), limit=10),
    })
    return templates.TemplateResponse("admin/catalog/offer_detail.html", context)


@router.get("/offers/{offer_id}/edit", response_class=HTMLResponse)
def catalog_offer_edit(request: Request, offer_id: str, db: Session = Depends(get_db)):
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

    links = catalog_service.offer_radius_profiles.list(
        db=db,
        offer_id=offer_id,
        profile_id=None,
        order_by="offer_id",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    radius_profile_id = links[0].profile_id if links else ""

    # Load existing offer-addon links
    offer_addon_links = catalog_service.offer_addons.list(
        db=db, offer_id=offer_id, limit=200
    )
    prices = catalog_service.offer_prices.list(
        db=db,
        offer_id=offer_id,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )
    price = next((item for item in prices if item.price_type.value == "recurring"), None)
    if not price and prices:
        price = prices[0]

    offer_data = {
        "name": offer.name,
        "code": offer.code or "",
        "service_type": offer.service_type,
        "access_type": offer.access_type,
        "price_basis": offer.price_basis,
        "billing_cycle": offer.billing_cycle,
        "billing_mode": offer.billing_mode.value if offer.billing_mode else BillingMode.prepaid.value,
        "contract_term": offer.contract_term,
        "region_zone_id": offer.region_zone_id or "",
        "usage_allowance_id": offer.usage_allowance_id or "",
        "sla_profile_id": offer.sla_profile_id or "",
        "radius_profile_id": radius_profile_id or "",
        "policy_set_id": offer.policy_set_id or "",
        "splynx_tariff_id": offer.splynx_tariff_id or "",
        "splynx_service_name": offer.splynx_service_name or "",
        "splynx_tax_id": offer.splynx_tax_id or "",
        "with_vat": offer.with_vat,
        "vat_percent": offer.vat_percent or "",
        "speed_download_mbps": offer.speed_download_mbps or "",
        "speed_upload_mbps": offer.speed_upload_mbps or "",
        "guaranteed_speed_limit_at": offer.guaranteed_speed_limit_at or "",
        "guaranteed_speed": offer.guaranteed_speed.value if offer.guaranteed_speed else GuaranteedSpeedType.none.value,
        "aggregation": offer.aggregation or "",
        "priority": offer.priority or "",
        "available_for_services": offer.available_for_services,
        "show_on_customer_portal": offer.show_on_customer_portal,
        "status": offer.status,
        "description": offer.description or "",
        "is_active": offer.is_active,
        "price_id": str(price.id) if price else "",
        "price_amount": price.amount if price else "",
        "price_currency": price.currency if price else "NGN",
        "price_billing_cycle": price.billing_cycle.value if price and price.billing_cycle else "",
        "price_unit": price.unit.value if price and price.unit else "",
        "price_description": price.description if price and price.description else "",
    }
    context = _offer_form_context(request, db, offer_data, offer_addon_links=offer_addon_links)
    context["action_url"] = f"/admin/catalog/offers/{offer_id}/edit"
    return templates.TemplateResponse("admin/catalog/offer_form.html", context)


@router.post("/offers/{offer_id}/edit", response_class=HTMLResponse)
async def catalog_offer_edit_post(request: Request, offer_id: str, db: Session = Depends(get_db)):
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

    form = await request.form()
    offer_data = {
        "name": form.get("name", "").strip(),
        "code": form.get("code", "").strip(),
        "service_type": form.get("service_type", "").strip(),
        "access_type": form.get("access_type", "").strip(),
        "price_basis": form.get("price_basis", "").strip(),
        "billing_cycle": form.get("billing_cycle", "").strip(),
        "billing_mode": form.get("billing_mode", "").strip(),
        "contract_term": form.get("contract_term", "").strip(),
        "region_zone_id": form.get("region_zone_id", "").strip(),
        "usage_allowance_id": form.get("usage_allowance_id", "").strip(),
        "sla_profile_id": form.get("sla_profile_id", "").strip(),
        "radius_profile_id": form.get("radius_profile_id", "").strip(),
        "policy_set_id": form.get("policy_set_id", "").strip(),
        "splynx_tariff_id": form.get("splynx_tariff_id", "").strip(),
        "splynx_service_name": form.get("splynx_service_name", "").strip(),
        "splynx_tax_id": form.get("splynx_tax_id", "").strip(),
        "with_vat": form.get("with_vat") == "true",
        "vat_percent": form.get("vat_percent", "").strip(),
        "speed_download_mbps": form.get("speed_download_mbps", "").strip(),
        "speed_upload_mbps": form.get("speed_upload_mbps", "").strip(),
        "guaranteed_speed_limit_at": form.get("guaranteed_speed_limit_at", "").strip(),
        "guaranteed_speed": form.get("guaranteed_speed", "").strip(),
        "aggregation": form.get("aggregation", "").strip(),
        "priority": form.get("priority", "").strip(),
        "available_for_services": form.get("available_for_services") == "true",
        "show_on_customer_portal": form.get("show_on_customer_portal") == "true",
        "status": form.get("status", "").strip(),
        "description": form.get("description", "").strip(),
        "is_active": form.get("is_active") == "true",
        "price_id": form.get("price_id", "").strip(),
        "price_amount": form.get("price_amount", "").strip(),
        "price_currency": form.get("price_currency", "NGN").strip(),
        "price_billing_cycle": form.get("price_billing_cycle", "").strip(),
        "price_unit": form.get("price_unit", "").strip(),
        "price_description": form.get("price_description", "").strip(),
    }

    error = None
    if not offer_data["radius_profile_id"]:
        error = "RADIUS profile is required."
    if not offer_data["price_amount"]:
        error = "Recurring price is required."
    if error:
        context = _offer_form_context(request, db, offer_data, error)
        context["action_url"] = f"/admin/catalog/offers/{offer_id}/edit"
        return templates.TemplateResponse("admin/catalog/offer_form.html", context)

    payload_data = {
        "name": offer_data["name"],
        "service_type": offer_data["service_type"],
        "access_type": offer_data["access_type"],
        "price_basis": offer_data["price_basis"],
        "is_active": offer_data["is_active"],
    }
    if offer_data["code"]:
        payload_data["code"] = offer_data["code"]
    if offer_data["billing_cycle"]:
        payload_data["billing_cycle"] = offer_data["billing_cycle"]
    if offer_data["billing_mode"]:
        payload_data["billing_mode"] = offer_data["billing_mode"]
    if offer_data["contract_term"]:
        payload_data["contract_term"] = offer_data["contract_term"]
    if offer_data["region_zone_id"]:
        payload_data["region_zone_id"] = offer_data["region_zone_id"]
    if offer_data["usage_allowance_id"]:
        payload_data["usage_allowance_id"] = offer_data["usage_allowance_id"]
    if offer_data["sla_profile_id"]:
        payload_data["sla_profile_id"] = offer_data["sla_profile_id"]
    if offer_data["policy_set_id"]:
        payload_data["policy_set_id"] = offer_data["policy_set_id"]
    if offer_data["splynx_tariff_id"]:
        payload_data["splynx_tariff_id"] = offer_data["splynx_tariff_id"]
    if offer_data["splynx_service_name"]:
        payload_data["splynx_service_name"] = offer_data["splynx_service_name"]
    if offer_data["splynx_tax_id"]:
        payload_data["splynx_tax_id"] = offer_data["splynx_tax_id"]
    payload_data["with_vat"] = offer_data["with_vat"]
    if offer_data["vat_percent"]:
        payload_data["vat_percent"] = offer_data["vat_percent"]
    if offer_data["speed_download_mbps"]:
        payload_data["speed_download_mbps"] = offer_data["speed_download_mbps"]
    if offer_data["speed_upload_mbps"]:
        payload_data["speed_upload_mbps"] = offer_data["speed_upload_mbps"]
    if offer_data["guaranteed_speed_limit_at"]:
        payload_data["guaranteed_speed_limit_at"] = offer_data["guaranteed_speed_limit_at"]
    if offer_data["guaranteed_speed"]:
        payload_data["guaranteed_speed"] = offer_data["guaranteed_speed"]
    if offer_data["aggregation"]:
        payload_data["aggregation"] = offer_data["aggregation"]
    if offer_data["priority"]:
        payload_data["priority"] = offer_data["priority"]
    payload_data["available_for_services"] = offer_data["available_for_services"]
    payload_data["show_on_customer_portal"] = offer_data["show_on_customer_portal"]
    if offer_data["status"]:
        payload_data["status"] = offer_data["status"]
    if offer_data["description"]:
        payload_data["description"] = offer_data["description"]

    try:
        before_snapshot = model_to_dict(existing_offer)
        payload = CatalogOfferUpdate(**payload_data)
        updated_offer = catalog_service.offers.update(db=db, offer_id=offer_id, payload=payload)
        after_snapshot = model_to_dict(updated_offer)
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata = {"changes": changes} if changes else None
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="catalog_offer",
            entity_id=str(updated_offer.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata,
        )

        links = catalog_service.offer_radius_profiles.list(
            db=db,
            offer_id=offer_id,
            profile_id=None,
            order_by="offer_id",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        if links:
            catalog_service.offer_radius_profiles.update(
                db=db,
                link_id=links[0].id,
                payload=OfferRadiusProfileUpdate(profile_id=offer_data["radius_profile_id"]),
            )
        else:
            catalog_service.offer_radius_profiles.create(
                db=db,
                payload=OfferRadiusProfileCreate(
                    offer_id=offer_id,
                    profile_id=offer_data["radius_profile_id"],
                ),
            )

        # Sync addon links
        addon_configs = _parse_addon_links_from_form(form)
        catalog_service.offer_addons.sync(
            db=db,
            offer_id=offer_id,
            addon_configs=addon_configs,
        )

        if offer_data["price_amount"]:
            price_payload = {
                "amount": offer_data["price_amount"],
                "currency": offer_data["price_currency"],
            }
            if offer_data["price_billing_cycle"]:
                price_payload["billing_cycle"] = offer_data["price_billing_cycle"]
            if offer_data["price_unit"]:
                price_payload["unit"] = offer_data["price_unit"]
            if offer_data["price_description"]:
                price_payload["description"] = offer_data["price_description"]
            if offer_data["price_id"]:
                payload = OfferPriceUpdate(**price_payload)
                updated_price = catalog_service.offer_prices.update(
                    db=db, price_id=offer_data["price_id"], payload=payload
                )
                log_audit_event(
                    db=db,
                    request=request,
                    action="price_updated",
                    entity_type="catalog_offer",
                    entity_id=str(offer_id),
                    actor_id=str(current_user.get("subscriber_id")) if current_user else None,
                    metadata={
                        "price_amount": str(updated_price.amount),
                        "currency": updated_price.currency,
                    },
                )
            else:
                price_payload["offer_id"] = offer_id
                payload = OfferPriceCreate(**price_payload)
                created_price = catalog_service.offer_prices.create(db=db, payload=payload)
                log_audit_event(
                    db=db,
                    request=request,
                    action="price_created",
                    entity_type="catalog_offer",
                    entity_id=str(offer_id),
                    actor_id=str(current_user.get("subscriber_id")) if current_user else None,
                    metadata={
                        "price_amount": str(created_price.amount),
                        "currency": created_price.currency,
                    },
                )

        return RedirectResponse(f"/admin/catalog/offers/{offer_id}", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _offer_form_context(request, db, offer_data, error or "Please correct the highlighted fields.")
    context["action_url"] = f"/admin/catalog/offers/{offer_id}/edit"
    return templates.TemplateResponse("admin/catalog/offer_form.html", context)


@router.get("/subscriptions", response_class=HTMLResponse)
def catalog_subscriptions(
    request: Request,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    offset = (page - 1) * per_page
    subscriptions = catalog_service.subscriptions.list(
        db=db,
        account_id=None,
        offer_id=None,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )
    all_subscriptions = catalog_service.subscriptions.list(
        db=db,
        account_id=None,
        offer_id=None,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_subscriptions)
    total_pages = (total + per_page - 1) // per_page if total else 1

    context = _base_context(request, db, active_page="subscriptions")
    context.update(
        {
            "subscriptions": subscriptions,
            "status": status,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        }
    )
    return templates.TemplateResponse("admin/catalog/subscriptions.html", context)


@router.get("/subscriptions/new", response_class=HTMLResponse)
def catalog_subscription_new(request: Request, db: Session = Depends(get_db)):
    account_id = request.query_params.get("account_id", "").strip()
    subscriber_id = request.query_params.get("subscriber_id", "").strip()
    subscription = {
        "account_id": account_id,
        "subscriber_id": subscriber_id,
        "offer_id": "",
        "status": SubscriptionStatus.pending.value,
        "billing_mode": "",
        "contract_term": ContractTerm.month_to_month.value,
        "start_at": "",
        "end_at": "",
        "next_billing_at": "",
        "canceled_at": "",
        "cancel_reason": "",
        "splynx_service_id": "",
        "router_id": "",
        "service_description": "",
        "quantity": "",
        "unit": "",
        "unit_price": "",
        "discount": False,
        "discount_value": "",
        "discount_type": "",
        "service_status_raw": "",
        "login": "",
        "ipv4_address": "",
        "ipv6_address": "",
        "mac_address": "",
        "provisioning_nas_device_id": "",
        "radius_profile_id": "",
    }
    context = _subscription_form_context(request, db, subscription)
    return templates.TemplateResponse("admin/catalog/subscription_form.html", context)


@router.post("/subscriptions", response_class=HTMLResponse)
async def catalog_subscription_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    subscriber_id = (form.get("subscriber_id") or "").strip()
    subscription = {
        "account_id": (form.get("account_id") or "").strip(),
        "subscriber_id": subscriber_id,
        "offer_id": (form.get("offer_id") or "").strip(),
        "status": (form.get("status") or "").strip(),
        "billing_mode": (form.get("billing_mode") or "").strip(),
        "contract_term": (form.get("contract_term") or "").strip(),
        "start_at": (form.get("start_at") or "").strip(),
        "end_at": (form.get("end_at") or "").strip(),
        "next_billing_at": (form.get("next_billing_at") or "").strip(),
        "canceled_at": (form.get("canceled_at") or "").strip(),
        "cancel_reason": (form.get("cancel_reason") or "").strip(),
        "splynx_service_id": (form.get("splynx_service_id") or "").strip(),
        "router_id": (form.get("router_id") or "").strip(),
        "service_description": (form.get("service_description") or "").strip(),
        "quantity": (form.get("quantity") or "").strip(),
        "unit": (form.get("unit") or "").strip(),
        "unit_price": (form.get("unit_price") or "").strip(),
        "discount": form.get("discount") == "true",
        "discount_value": (form.get("discount_value") or "").strip(),
        "discount_type": (form.get("discount_type") or "").strip(),
        "service_status_raw": (form.get("service_status_raw") or "").strip(),
        "login": (form.get("login") or "").strip(),
        "ipv4_address": (form.get("ipv4_address") or "").strip(),
        "ipv6_address": (form.get("ipv6_address") or "").strip(),
        "mac_address": (form.get("mac_address") or "").strip(),
        "provisioning_nas_device_id": (form.get("provisioning_nas_device_id") or "").strip(),
        "radius_profile_id": (form.get("radius_profile_id") or "").strip(),
    }
    error = None
    if not subscription["account_id"]:
        if not subscriber_id:
            error = "Account or subscriber is required."
        else:
            try:
                subscriber_uuid = UUID(subscriber_id)
            except ValueError:
                error = "Subscriber is invalid."
            else:
                accounts = subscriber_service.accounts.list(
                    db=db,
                    subscriber_id=str(subscriber_uuid),
                    reseller_id=None,
                    order_by="created_at",
                    order_dir="desc",
                    limit=1,
                    offset=0,
                )
                if accounts:
                    subscription["account_id"] = str(accounts[0].id)
                else:
                    from app.schemas.subscriber import SubscriberAccountCreate

                    try:
                        account = subscriber_service.accounts.create(
                            db=db,
                            payload=SubscriberAccountCreate(subscriber_id=subscriber_uuid),
                        )
                    except Exception as exc:
                        error = exc.detail if hasattr(exc, "detail") else str(exc)
                    else:
                        subscription["account_id"] = str(account.id)
    if not error and not subscription["offer_id"]:
        error = "Offer is required."
    if error:
        context = _subscription_form_context(request, db, subscription, error)
        return templates.TemplateResponse("admin/catalog/subscription_form.html", context)

    payload_data = {
        "account_id": subscription["account_id"],
        "offer_id": subscription["offer_id"],
    }
    if subscription["status"]:
        payload_data["status"] = subscription["status"]
    if subscription["billing_mode"]:
        payload_data["billing_mode"] = subscription["billing_mode"]
    if subscription["contract_term"]:
        payload_data["contract_term"] = subscription["contract_term"]
    if subscription["start_at"]:
        payload_data["start_at"] = subscription["start_at"]
    if subscription["end_at"]:
        payload_data["end_at"] = subscription["end_at"]
    if subscription["next_billing_at"]:
        payload_data["next_billing_at"] = subscription["next_billing_at"]
    if subscription["canceled_at"]:
        payload_data["canceled_at"] = subscription["canceled_at"]
    if subscription["cancel_reason"]:
        payload_data["cancel_reason"] = subscription["cancel_reason"]
    if subscription["splynx_service_id"]:
        payload_data["splynx_service_id"] = subscription["splynx_service_id"]
    if subscription["router_id"]:
        payload_data["router_id"] = subscription["router_id"]
    if subscription["service_description"]:
        payload_data["service_description"] = subscription["service_description"]
    if subscription["quantity"]:
        payload_data["quantity"] = subscription["quantity"]
    if subscription["unit"]:
        payload_data["unit"] = subscription["unit"]
    if subscription["unit_price"]:
        payload_data["unit_price"] = subscription["unit_price"]
    payload_data["discount"] = subscription["discount"]
    if subscription["discount_value"]:
        payload_data["discount_value"] = subscription["discount_value"]
    if subscription["discount_type"]:
        payload_data["discount_type"] = subscription["discount_type"]
    if subscription["service_status_raw"]:
        payload_data["service_status_raw"] = subscription["service_status_raw"]
    if subscription["login"]:
        payload_data["login"] = subscription["login"]
    if subscription["ipv4_address"]:
        payload_data["ipv4_address"] = subscription["ipv4_address"]
    if subscription["ipv6_address"]:
        payload_data["ipv6_address"] = subscription["ipv6_address"]
    if subscription["mac_address"]:
        payload_data["mac_address"] = subscription["mac_address"]
    if subscription["provisioning_nas_device_id"]:
        payload_data["provisioning_nas_device_id"] = subscription["provisioning_nas_device_id"]
    if subscription["radius_profile_id"]:
        payload_data["radius_profile_id"] = subscription["radius_profile_id"]

    try:
        from app.web.admin import get_current_user
        from datetime import datetime, timezone

        # Get quick options from form
        activate_immediately = form.get("activate_immediately") == "1"
        generate_invoice = form.get("generate_invoice") == "1"
        send_welcome_email = form.get("send_welcome_email") == "1"

        # Override status if activate_immediately is checked
        if activate_immediately:
            payload_data["status"] = "active"
            if not payload_data.get("start_at"):
                payload_data["start_at"] = datetime.now(timezone.utc).isoformat()

        payload = SubscriptionCreate(**payload_data)
        created = catalog_service.subscriptions.create(db=db, payload=payload)
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="subscription",
            entity_id=str(created.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"offer_id": str(created.offer_id), "account_id": str(created.account_id)},
        )

        # Handle generate_invoice option
        if generate_invoice and created.account_id:
            from app.services import billing as billing_service
            from app.schemas.billing import InvoiceCreate, InvoiceLineCreate
            from decimal import Decimal

            # Get offer details for invoice line
            offer = catalog_service.offers.get(db=db, offer_id=str(created.offer_id))
            line_amount = Decimal("0.00")
            line_description = "Subscription"
            if offer:
                line_description = offer.name
                if offer.prices:
                    line_amount = offer.prices[0].amount or Decimal("0.00")

            # Create invoice
            invoice_payload = InvoiceCreate(
                account_id=created.account_id,
                status="issued",
                issued_at=datetime.now(timezone.utc),
            )
            invoice = billing_service.invoices.create(db=db, payload=invoice_payload)

            # Add line item
            billing_service.invoice_lines.create(
                db,
                InvoiceLineCreate(
                    invoice_id=invoice.id,
                    description=line_description,
                    quantity=Decimal("1"),
                    unit_price=line_amount,
                ),
            )

        # Handle send_welcome_email option
        if send_welcome_email and created.account:
            from app.services import email as email_service
            account = created.account
            if account.subscriber:
                subscriber = account.subscriber
                email_addr = None
                if subscriber.person and subscriber.person.email:
                    email_addr = subscriber.person.email
                elif subscriber.organization and subscriber.organization.email:
                    email_addr = subscriber.organization.email
                if email_addr:
                    email_service.send_email(
                        db=db,
                        to_email=email_addr,
                        subject="Welcome to your new subscription",
                        template_name="subscription_welcome",
                        context={"subscription": created, "account": account},
                    )

        # Redirect back to subscriber page if coming from subscriber context
        if subscriber_id:
            return RedirectResponse(f"/admin/subscribers/{subscriber_id}", status_code=303)
        return RedirectResponse("/admin/catalog/subscriptions", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = exc.detail if hasattr(exc, "detail") else str(exc)

    context = _subscription_form_context(
        request,
        db,
        subscription,
        error or "Please correct the highlighted fields.",
    )
    return templates.TemplateResponse("admin/catalog/subscription_form.html", context)


@router.get("/subscriptions/{subscription_id}/edit", response_class=HTMLResponse)
def catalog_subscription_edit(request: Request, subscription_id: str, db: Session = Depends(get_db)):
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

    subscription = {
        "id": str(subscription_obj.id),
        "account_id": str(subscription_obj.account_id),
        "offer_id": str(subscription_obj.offer_id),
        "status": subscription_obj.status.value if subscription_obj.status else "",
        "billing_mode": subscription_obj.billing_mode.value if subscription_obj.billing_mode else "",
        "contract_term": subscription_obj.contract_term.value if subscription_obj.contract_term else "",
        "start_at": subscription_obj.start_at.strftime("%Y-%m-%dT%H:%M") if subscription_obj.start_at else "",
        "end_at": subscription_obj.end_at.strftime("%Y-%m-%dT%H:%M") if subscription_obj.end_at else "",
        "next_billing_at": subscription_obj.next_billing_at.strftime("%Y-%m-%dT%H:%M") if subscription_obj.next_billing_at else "",
        "canceled_at": subscription_obj.canceled_at.strftime("%Y-%m-%dT%H:%M") if subscription_obj.canceled_at else "",
        "cancel_reason": subscription_obj.cancel_reason or "",
        "splynx_service_id": subscription_obj.splynx_service_id or "",
        "router_id": subscription_obj.router_id or "",
        "service_description": subscription_obj.service_description or "",
        "quantity": subscription_obj.quantity or "",
        "unit": subscription_obj.unit or "",
        "unit_price": subscription_obj.unit_price or "",
        "discount": subscription_obj.discount,
        "discount_value": subscription_obj.discount_value or "",
        "discount_type": subscription_obj.discount_type or "",
        "service_status_raw": subscription_obj.service_status_raw or "",
        "login": subscription_obj.login or "",
        "ipv4_address": subscription_obj.ipv4_address or "",
        "ipv6_address": subscription_obj.ipv6_address or "",
        "mac_address": subscription_obj.mac_address or "",
        "provisioning_nas_device_id": str(subscription_obj.provisioning_nas_device_id) if subscription_obj.provisioning_nas_device_id else "",
        "radius_profile_id": str(subscription_obj.radius_profile_id) if subscription_obj.radius_profile_id else "",
    }
    context = _subscription_form_context(request, db, subscription)
    context["activities"] = _subscription_activities(db, str(subscription_id))
    context["action_url"] = f"/admin/catalog/subscriptions/{subscription_id}/edit"
    return templates.TemplateResponse("admin/catalog/subscription_form.html", context)


@router.get("/subscriptions/{subscription_id}", response_class=HTMLResponse)
def catalog_subscription_detail(request: Request, subscription_id: str, db: Session = Depends(get_db)):
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
            "activities": _subscription_activities(db, str(subscription_id)),
        }
    )
    return templates.TemplateResponse("admin/catalog/subscription_detail.html", context)


@router.post("/subscriptions/{subscription_id}/edit", response_class=HTMLResponse)
async def catalog_subscription_update(
    request: Request,
    subscription_id: str,
    db: Session = Depends(get_db),
):
    form = await request.form()
    subscription = {
        "id": subscription_id,
        "account_id": (form.get("account_id") or "").strip(),
        "offer_id": (form.get("offer_id") or "").strip(),
        "status": (form.get("status") or "").strip(),
        "billing_mode": (form.get("billing_mode") or "").strip(),
        "contract_term": (form.get("contract_term") or "").strip(),
        "start_at": (form.get("start_at") or "").strip(),
        "end_at": (form.get("end_at") or "").strip(),
        "next_billing_at": (form.get("next_billing_at") or "").strip(),
        "canceled_at": (form.get("canceled_at") or "").strip(),
        "cancel_reason": (form.get("cancel_reason") or "").strip(),
        "splynx_service_id": (form.get("splynx_service_id") or "").strip(),
        "router_id": (form.get("router_id") or "").strip(),
        "service_description": (form.get("service_description") or "").strip(),
        "quantity": (form.get("quantity") or "").strip(),
        "unit": (form.get("unit") or "").strip(),
        "unit_price": (form.get("unit_price") or "").strip(),
        "discount": form.get("discount") == "true",
        "discount_value": (form.get("discount_value") or "").strip(),
        "discount_type": (form.get("discount_type") or "").strip(),
        "service_status_raw": (form.get("service_status_raw") or "").strip(),
        "login": (form.get("login") or "").strip(),
        "ipv4_address": (form.get("ipv4_address") or "").strip(),
        "ipv6_address": (form.get("ipv6_address") or "").strip(),
        "mac_address": (form.get("mac_address") or "").strip(),
        "provisioning_nas_device_id": (form.get("provisioning_nas_device_id") or "").strip(),
        "radius_profile_id": (form.get("radius_profile_id") or "").strip(),
    }
    error = None
    if not subscription["account_id"]:
        error = "Account is required."
    elif not subscription["offer_id"]:
        error = "Offer is required."
    if error:
        context = _subscription_form_context(request, db, subscription, error)
        context["action_url"] = f"/admin/catalog/subscriptions/{subscription_id}/edit"
        return templates.TemplateResponse("admin/catalog/subscription_form.html", context)

    payload_data = {
        "account_id": subscription["account_id"],
        "offer_id": subscription["offer_id"],
    }
    if subscription["status"]:
        payload_data["status"] = subscription["status"]
    if subscription["billing_mode"]:
        payload_data["billing_mode"] = subscription["billing_mode"]
    if subscription["contract_term"]:
        payload_data["contract_term"] = subscription["contract_term"]
    if subscription["start_at"]:
        payload_data["start_at"] = subscription["start_at"]
    if subscription["end_at"]:
        payload_data["end_at"] = subscription["end_at"]
    if subscription["next_billing_at"]:
        payload_data["next_billing_at"] = subscription["next_billing_at"]
    if subscription["canceled_at"]:
        payload_data["canceled_at"] = subscription["canceled_at"]
    if subscription["cancel_reason"]:
        payload_data["cancel_reason"] = subscription["cancel_reason"]
    if subscription["splynx_service_id"]:
        payload_data["splynx_service_id"] = subscription["splynx_service_id"]
    if subscription["router_id"]:
        payload_data["router_id"] = subscription["router_id"]
    if subscription["service_description"]:
        payload_data["service_description"] = subscription["service_description"]
    if subscription["quantity"]:
        payload_data["quantity"] = subscription["quantity"]
    if subscription["unit"]:
        payload_data["unit"] = subscription["unit"]
    if subscription["unit_price"]:
        payload_data["unit_price"] = subscription["unit_price"]
    payload_data["discount"] = subscription["discount"]
    if subscription["discount_value"]:
        payload_data["discount_value"] = subscription["discount_value"]
    if subscription["discount_type"]:
        payload_data["discount_type"] = subscription["discount_type"]
    if subscription["service_status_raw"]:
        payload_data["service_status_raw"] = subscription["service_status_raw"]
    if subscription["login"]:
        payload_data["login"] = subscription["login"]
    if subscription["ipv4_address"]:
        payload_data["ipv4_address"] = subscription["ipv4_address"]
    if subscription["ipv6_address"]:
        payload_data["ipv6_address"] = subscription["ipv6_address"]
    if subscription["mac_address"]:
        payload_data["mac_address"] = subscription["mac_address"]
    if subscription["provisioning_nas_device_id"]:
        payload_data["provisioning_nas_device_id"] = subscription["provisioning_nas_device_id"]
    if subscription["radius_profile_id"]:
        payload_data["radius_profile_id"] = subscription["radius_profile_id"]

    try:
        from app.web.admin import get_current_user
        before = catalog_service.subscriptions.get(db=db, subscription_id=subscription_id)
        payload = SubscriptionUpdate(**payload_data)
        catalog_service.subscriptions.update(
            db=db,
            subscription_id=subscription_id,
            payload=payload,
        )
        after = catalog_service.subscriptions.get(db=db, subscription_id=subscription_id)
        metadata_payload = build_changes_metadata(before, after)
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="subscription",
            entity_id=str(subscription_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
        return RedirectResponse("/admin/catalog/subscriptions", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = exc.detail if hasattr(exc, "detail") else str(exc)

    context = _subscription_form_context(
        request,
        db,
        subscription,
        error or "Please correct the highlighted fields.",
    )
    context["action_url"] = f"/admin/catalog/subscriptions/{subscription_id}/edit"
    return templates.TemplateResponse("admin/catalog/subscription_form.html", context)


@router.post("/subscriptions/bulk/activate", dependencies=[Depends(require_permission("catalog:write"))])
def subscription_bulk_activate(
    request: Request,
    subscription_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    """Bulk activate subscriptions."""
    from fastapi.responses import JSONResponse
    from app.models.catalog import SubscriptionStatus
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    count = 0

    for sub_id in subscription_ids.split(","):
        sub_id = sub_id.strip()
        if not sub_id:
            continue
        try:
            subscription = catalog_service.subscriptions.get(db, sub_id)
            if subscription and subscription.status in [SubscriptionStatus.pending, SubscriptionStatus.suspended]:
                subscription.status = SubscriptionStatus.active
                db.commit()
                log_audit_event(
                    db=db,
                    request=request,
                    action="activate",
                    entity_type="subscription",
                    entity_id=sub_id,
                    actor_id=str(current_user.get("subscriber_id")) if current_user else None,
                )
                count += 1
        except Exception:
            continue

    return JSONResponse({"message": f"Activated {count} subscriptions", "count": count})


@router.post("/subscriptions/bulk/suspend", dependencies=[Depends(require_permission("catalog:write"))])
def subscription_bulk_suspend(
    request: Request,
    subscription_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    """Bulk suspend subscriptions."""
    from fastapi.responses import JSONResponse
    from app.models.catalog import SubscriptionStatus
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    count = 0

    for sub_id in subscription_ids.split(","):
        sub_id = sub_id.strip()
        if not sub_id:
            continue
        try:
            subscription = catalog_service.subscriptions.get(db, sub_id)
            if subscription and subscription.status == SubscriptionStatus.active:
                subscription.status = SubscriptionStatus.suspended
                db.commit()
                log_audit_event(
                    db=db,
                    request=request,
                    action="suspend",
                    entity_type="subscription",
                    entity_id=sub_id,
                    actor_id=str(current_user.get("subscriber_id")) if current_user else None,
                )
                count += 1
        except Exception:
            continue

    return JSONResponse({"message": f"Suspended {count} subscriptions", "count": count})


@router.post("/subscriptions/bulk/cancel", dependencies=[Depends(require_permission("catalog:write"))])
def subscription_bulk_cancel(
    request: Request,
    subscription_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    """Bulk cancel subscriptions."""
    from fastapi.responses import JSONResponse
    from app.models.catalog import SubscriptionStatus
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    count = 0

    for sub_id in subscription_ids.split(","):
        sub_id = sub_id.strip()
        if not sub_id:
            continue
        try:
            subscription = catalog_service.subscriptions.get(db, sub_id)
            if subscription and subscription.status not in [SubscriptionStatus.canceled, SubscriptionStatus.expired]:
                subscription.status = SubscriptionStatus.canceled
                db.commit()
                log_audit_event(
                    db=db,
                    request=request,
                    action="cancel",
                    entity_type="subscription",
                    entity_id=sub_id,
                    actor_id=str(current_user.get("subscriber_id")) if current_user else None,
                )
                count += 1
        except Exception:
            continue

    return JSONResponse({"message": f"Canceled {count} subscriptions", "count": count})


@router.get("/calculator", response_class=HTMLResponse)
def pricing_calculator(request: Request, db: Session = Depends(get_db)):
    """Pricing calculator tool to test and validate offers."""
    offers = catalog_service.offers.list(
        db=db,
        service_type=None,
        access_type=None,
        status="active",
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    add_ons = catalog_service.add_ons.list(
        db=db,
        is_active=True,
        addon_type=None,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    usage_allowances = catalog_service.usage_allowances.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )

    # Pre-load prices for offers and add-ons
    offers_with_prices = []
    for offer in offers:
        prices = catalog_service.offer_prices.list(
            db=db,
            offer_id=str(offer.id),
            is_active=True,
            order_by="created_at",
            order_dir="asc",
            limit=50,
            offset=0,
        )
        offers_with_prices.append({
            "id": str(offer.id),
            "name": offer.name,
            "code": offer.code or "",
            "service_type": offer.service_type.value if offer.service_type else "",
            "billing_cycle": offer.billing_cycle.value if offer.billing_cycle else "",
            "usage_allowance_id": str(offer.usage_allowance_id) if offer.usage_allowance_id else "",
            "with_vat": offer.with_vat,
            "vat_percent": float(offer.vat_percent) if offer.vat_percent else 0,
            "prices": [
                {
                    "price_type": p.price_type.value if p.price_type else "",
                    "amount": float(p.amount) if p.amount else 0,
                    "currency": p.currency or "NGN",
                    "billing_cycle": p.billing_cycle.value if p.billing_cycle else "",
                }
                for p in prices
            ],
        })

    add_ons_with_prices = []
    for addon in add_ons:
        prices = catalog_service.add_on_prices.list(
            db=db,
            add_on_id=str(addon.id),
            is_active=True,
            order_by="created_at",
            order_dir="asc",
            limit=50,
            offset=0,
        )
        add_ons_with_prices.append({
            "id": str(addon.id),
            "name": addon.name,
            "addon_type": addon.addon_type.value if addon.addon_type else "",
            "prices": [
                {
                    "price_type": p.price_type.value if p.price_type else "",
                    "amount": float(p.amount) if p.amount else 0,
                    "currency": p.currency or "NGN",
                    "billing_cycle": p.billing_cycle.value if p.billing_cycle else "",
                }
                for p in prices
            ],
        })

    usage_allowances_data = [
        {
            "id": str(ua.id),
            "name": ua.name,
            "included_gb": ua.included_gb or 0,
            "overage_rate": float(ua.overage_rate) if ua.overage_rate else 0,
            "overage_cap_gb": ua.overage_cap_gb or 0,
        }
        for ua in usage_allowances
    ]

    # Build offer-addon map for filtering add-ons by selected offer
    offer_addon_map = {}
    for offer in offers:
        offer_addons = catalog_service.offer_addons.list(
            db=db, offer_id=str(offer.id), limit=200
        )
        offer_addon_map[str(offer.id)] = [str(link.add_on_id) for link in offer_addons]

    context = _base_context(request, db, active_page="calculator")
    context.update({
        "offers": offers_with_prices,
        "add_ons": add_ons_with_prices,
        "usage_allowances": usage_allowances_data,
        "offer_addon_map": offer_addon_map,
    })
    return templates.TemplateResponse("admin/catalog/calculator.html", context)
