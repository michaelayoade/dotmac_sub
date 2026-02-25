"""Service helpers for admin catalog offer web routes."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    ContractTerm,
    GuaranteedSpeedType,
    OfferStatus,
    PriceBasis,
    PriceUnit,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.domain_settings import SettingDomain
from app.schemas.catalog import (
    CatalogOfferCreate,
    CatalogOfferUpdate,
    OfferPriceCreate,
    OfferPriceUpdate,
    OfferRadiusProfileCreate,
    OfferRadiusProfileUpdate,
)
from app.services import catalog as catalog_service
from app.services import settings_spec
from app.services.audit_helpers import (
    diff_dicts,
    log_audit_event,
    model_to_dict,
)
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)

PLAN_KIND_STANDARD = "standard"
PLAN_KIND_IP_ADDRESS = "ip_address"
PLAN_KIND_DEVICE_REPLACEMENT = "device_replacement"
PLAN_KINDS = [PLAN_KIND_STANDARD, PLAN_KIND_IP_ADDRESS, PLAN_KIND_DEVICE_REPLACEMENT]
IP_BLOCK_SIZES = ["/32", "/30", "/29", "/28", "/27", "/26", "/25", "/24"]


def parse_offer_description_metadata(description: str | None) -> tuple[dict[str, str | None], str | None]:
    text = str(description or "").strip()
    metadata = {"plan_kind": None, "ip_block_size": None}
    if not text:
        return metadata, None

    cleaned_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("[plan_kind:") and lowered.endswith("]"):
            metadata["plan_kind"] = stripped[11:-1].strip() or None
            continue
        if lowered.startswith("[ip_block_size:") and lowered.endswith("]"):
            metadata["ip_block_size"] = stripped[15:-1].strip() or None
            continue
        cleaned_lines.append(stripped)
    cleaned_text = "\n".join(line for line in cleaned_lines if line).strip() or None
    return metadata, cleaned_text


def normalize_offer_description(
    *,
    description: str | None,
    plan_kind: str | None,
    ip_block_size: str | None,
) -> str | None:
    metadata, cleaned = parse_offer_description_metadata(description)
    resolved_plan_kind = str(plan_kind or metadata.get("plan_kind") or PLAN_KIND_STANDARD).strip().lower()
    if resolved_plan_kind not in PLAN_KINDS:
        resolved_plan_kind = PLAN_KIND_STANDARD

    resolved_ip_block = str(ip_block_size or metadata.get("ip_block_size") or "").strip() or None
    lines: list[str] = []
    if resolved_plan_kind != PLAN_KIND_STANDARD:
        lines.append(f"[plan_kind:{resolved_plan_kind}]")
    if resolved_plan_kind == PLAN_KIND_IP_ADDRESS and resolved_ip_block:
        lines.append(f"[ip_block_size:{resolved_ip_block}]")
    if cleaned:
        lines.append(cleaned)
    return "\n".join(lines).strip() or None


def default_offer_form() -> dict[str, object]:
    """Return default values for offer create form."""
    return {
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
        "status": "active",
        "description": "",
        "is_active": True,
        "plan_kind": PLAN_KIND_STANDARD,
        "ip_block_size": "",
        "price_id": "",
        "price_type": "recurring",
        "price_amount": "",
        "price_currency": "NGN",
        "price_billing_cycle": BillingCycle.monthly.value,
        "price_unit": PriceUnit.month.value,
        "price_description": "",
    }


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value if isinstance(value, str) else default


def parse_offer_form(form: FormData) -> dict[str, object]:
    """Parse offer form payload from request form."""
    return {
        "name": _form_str(form, "name").strip(),
        "code": _form_str(form, "code").strip(),
        "service_type": _form_str(form, "service_type").strip(),
        "access_type": _form_str(form, "access_type").strip(),
        "price_basis": _form_str(form, "price_basis").strip(),
        "billing_cycle": _form_str(form, "billing_cycle").strip(),
        "billing_mode": _form_str(form, "billing_mode").strip(),
        "contract_term": _form_str(form, "contract_term").strip(),
        "region_zone_id": _form_str(form, "region_zone_id").strip(),
        "usage_allowance_id": _form_str(form, "usage_allowance_id").strip(),
        "sla_profile_id": _form_str(form, "sla_profile_id").strip(),
        "radius_profile_id": _form_str(form, "radius_profile_id").strip(),
        "policy_set_id": _form_str(form, "policy_set_id").strip(),
        "splynx_tariff_id": _form_str(form, "splynx_tariff_id").strip(),
        "splynx_service_name": _form_str(form, "splynx_service_name").strip(),
        "splynx_tax_id": _form_str(form, "splynx_tax_id").strip(),
        "with_vat": form.get("with_vat") == "true",
        "vat_percent": _form_str(form, "vat_percent").strip(),
        "speed_download_mbps": _form_str(form, "speed_download_mbps").strip(),
        "speed_upload_mbps": _form_str(form, "speed_upload_mbps").strip(),
        "guaranteed_speed_limit_at": _form_str(form, "guaranteed_speed_limit_at").strip(),
        "guaranteed_speed": _form_str(form, "guaranteed_speed").strip(),
        "aggregation": _form_str(form, "aggregation").strip(),
        "priority": _form_str(form, "priority").strip(),
        "available_for_services": form.get("available_for_services") == "true",
        "show_on_customer_portal": form.get("show_on_customer_portal") == "true",
        "status": _form_str(form, "status").strip(),
        "description": _form_str(form, "description").strip(),
        "is_active": form.get("is_active") == "true",
        "plan_kind": _form_str(form, "plan_kind").strip() or PLAN_KIND_STANDARD,
        "ip_block_size": _form_str(form, "ip_block_size").strip(),
        "price_id": _form_str(form, "price_id").strip(),
        "price_type": _form_str(form, "price_type").strip() or "recurring",
        "price_amount": _form_str(form, "price_amount").strip(),
        "price_currency": _form_str(form, "price_currency", "NGN").strip(),
        "price_billing_cycle": _form_str(form, "price_billing_cycle").strip(),
        "price_unit": _form_str(form, "price_unit").strip(),
        "price_description": _form_str(form, "price_description").strip(),
    }


def validate_offer_form(offer: dict[str, object]) -> str | None:
    """Validate required offer form fields."""
    plan_kind = str(offer.get("plan_kind") or PLAN_KIND_STANDARD).strip().lower()
    if plan_kind not in PLAN_KINDS:
        return "Plan kind is invalid."
    if plan_kind == PLAN_KIND_IP_ADDRESS and not str(offer.get("ip_block_size") or "").strip():
        return "IP block size is required for IP address plans."

    if plan_kind == PLAN_KIND_STANDARD and not offer.get("radius_profile_id"):
        return "RADIUS profile is required."
    price_type = str(offer.get("price_type") or "recurring").strip().lower()
    if price_type not in {"recurring", "one_time", "usage"}:
        return "Price type is invalid."
    if not offer.get("price_amount"):
        return "Price amount is required."
    return None


def build_offer_payload_data(offer: dict[str, object]) -> dict[str, object]:
    """Build payload dict for CatalogOffer create/update schemas."""
    payload_data = {
        "name": offer["name"],
        "service_type": offer["service_type"],
        "access_type": offer["access_type"],
        "price_basis": offer["price_basis"],
        "is_active": offer["is_active"],
        "with_vat": offer["with_vat"],
        "available_for_services": offer["available_for_services"],
        "show_on_customer_portal": offer["show_on_customer_portal"],
        "description": normalize_offer_description(
            description=str(offer.get("description") or "").strip() or None,
            plan_kind=str(offer.get("plan_kind") or PLAN_KIND_STANDARD),
            ip_block_size=str(offer.get("ip_block_size") or "").strip() or None,
        ),
    }
    optional_fields = [
        "code",
        "billing_cycle",
        "billing_mode",
        "contract_term",
        "region_zone_id",
        "usage_allowance_id",
        "sla_profile_id",
        "policy_set_id",
        "splynx_tariff_id",
        "splynx_service_name",
        "splynx_tax_id",
        "vat_percent",
        "speed_download_mbps",
        "speed_upload_mbps",
        "guaranteed_speed_limit_at",
        "guaranteed_speed",
        "aggregation",
        "priority",
        "status",
    ]
    for key in optional_fields:
        value = offer.get(key)
        if value:
            payload_data[key] = value
    return payload_data


def upsert_radius_profile_link(db: Session, offer_id: str, profile_id: str) -> None:
    """Create or update radius profile link for offer."""
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
            payload=OfferRadiusProfileUpdate(profile_id=coerce_uuid(profile_id)),
        )
    else:
        catalog_service.offer_radius_profiles.create(
            db=db,
            payload=OfferRadiusProfileCreate(
                offer_id=coerce_uuid(offer_id),
                profile_id=coerce_uuid(profile_id),
            ),
        )


def create_recurring_price(db: Session, offer_id: str, offer: dict[str, object]):
    """Create recurring offer price if amount is provided."""
    if not offer.get("price_amount"):
        return None
    price_payload = {
        "offer_id": offer_id,
        "price_type": str(offer.get("price_type") or "recurring"),
        "amount": offer["price_amount"],
        "currency": offer["price_currency"],
    }
    if offer.get("price_billing_cycle"):
        price_payload["billing_cycle"] = offer["price_billing_cycle"]
    if offer.get("price_unit"):
        price_payload["unit"] = offer["price_unit"]
    if offer.get("price_description"):
        price_payload["description"] = offer["price_description"]
    return catalog_service.offer_prices.create(
        db=db, payload=OfferPriceCreate.model_validate(price_payload)
    )


def upsert_recurring_price(db: Session, offer_id: str, offer: dict[str, object]):
    """Create/update recurring offer price if amount is provided."""
    if not offer.get("price_amount"):
        return None, None
    price_payload = {
        "price_type": str(offer.get("price_type") or "recurring"),
        "amount": offer["price_amount"],
        "currency": offer["price_currency"],
    }
    if offer.get("price_billing_cycle"):
        price_payload["billing_cycle"] = offer["price_billing_cycle"]
    if offer.get("price_unit"):
        price_payload["unit"] = offer["price_unit"]
    if offer.get("price_description"):
        price_payload["description"] = offer["price_description"]
    if offer.get("price_id"):
        updated = catalog_service.offer_prices.update(
            db=db,
            price_id=str(offer["price_id"]),
            payload=OfferPriceUpdate.model_validate(price_payload),
        )
        return updated, "updated"
    price_payload["offer_id"] = offer_id
    created = catalog_service.offer_prices.create(
        db=db, payload=OfferPriceCreate.model_validate(price_payload)
    )
    return created, "created"


def offer_edit_form_data(db: Session, offer_id: str, offer: CatalogOffer) -> tuple[dict[str, object], list]:
    """Build offer edit form values from persisted offer + related records."""
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
    offer_addon_links = catalog_service.offer_addons.list(db=db, offer_id=offer_id, limit=200)
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
        "plan_kind": PLAN_KIND_STANDARD,
        "ip_block_size": "",
        "price_id": str(price.id) if price else "",
        "price_type": price.price_type.value if price and price.price_type else "recurring",
        "price_amount": price.amount if price else "",
        "price_currency": price.currency if price else "NGN",
        "price_billing_cycle": price.billing_cycle.value if price and price.billing_cycle else "",
        "price_unit": price.unit.value if price and price.unit else "",
        "price_description": price.description if price and price.description else "",
    }
    metadata, cleaned_description = parse_offer_description_metadata(offer.description)
    plan_kind = str(metadata.get("plan_kind") or PLAN_KIND_STANDARD).strip().lower()
    if plan_kind not in PLAN_KINDS:
        plan_kind = PLAN_KIND_STANDARD
    offer_data["plan_kind"] = plan_kind
    offer_data["ip_block_size"] = metadata.get("ip_block_size") or ""
    offer_data["description"] = cleaned_description or ""
    return offer_data, offer_addon_links


def create_offer_payload(offer: dict[str, object]) -> CatalogOfferCreate:
    return CatalogOfferCreate.model_validate(build_offer_payload_data(offer))


def update_offer_payload(offer: dict[str, object]) -> CatalogOfferUpdate:
    return CatalogOfferUpdate.model_validate(build_offer_payload_data(offer))


def parse_addon_links_from_form(form: FormData) -> list[dict[str, object]]:
    """Parse addon link configurations from form data.

    Form fields are expected in the format:
    - addon_link_{addon_id}: 'true' if addon is selected
    - addon_required_{addon_id}: 'true' if addon is required
    - addon_min_qty_{addon_id}: minimum quantity (optional)
    - addon_max_qty_{addon_id}: maximum quantity (optional)
    """
    addon_configs: list[dict[str, object]] = []
    for key in form.keys():
        if key.startswith("addon_link_"):
            addon_id = key.replace("addon_link_", "")
            if form.get(key) == "true":
                is_required = form.get(f"addon_required_{addon_id}") == "true"
                min_qty_str = _form_str(form, f"addon_min_qty_{addon_id}").strip()
                max_qty_str = _form_str(form, f"addon_max_qty_{addon_id}").strip()

                config: dict[str, object] = {
                    "add_on_id": addon_id,
                    "is_required": is_required,
                    "min_quantity": int(min_qty_str) if min_qty_str else None,
                    "max_quantity": int(max_qty_str) if max_qty_str else None,
                }
                addon_configs.append(config)
    return addon_configs


def build_addon_links_map(offer_addon_links: list | None) -> dict[str, dict[str, object]]:
    """Build addon links map for the offer form template."""
    addon_links_map: dict[str, dict[str, object]] = {}
    if offer_addon_links:
        for link in offer_addon_links:
            addon_links_map[str(link.add_on_id)] = {
                "is_required": link.is_required,
                "min_quantity": link.min_quantity,
                "max_quantity": link.max_quantity,
            }
    return addon_links_map


def offer_form_context(
    db: Session,
    offer: dict[str, object],
    error: str | None = None,
    offer_addon_links: list | None = None,
) -> dict[str, object]:
    """Build context dict for the offer create/edit form template.

    Returns all reference data (enums, related entities) needed by the form.
    """
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
    add_ons = catalog_service.add_ons.list(
        db=db, is_active=True, addon_type=None, order_by="name", order_dir="asc", limit=200, offset=0
    )

    addon_links_map = build_addon_links_map(offer_addon_links)

    context: dict[str, object] = {
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
        "price_types": ["recurring", "one_time"],
        "guaranteed_speed_types": [item.value for item in GuaranteedSpeedType],
        "plan_kinds": PLAN_KINDS,
        "ip_block_sizes": IP_BLOCK_SIZES,
        "action_url": "/admin/catalog/offers",
    }
    if error:
        context["error"] = error
    return context


def dashboard_stats(db: Session) -> dict[str, object]:
    """Return catalog dashboard KPIs and chart data from core service."""
    return catalog_service.offers.get_dashboard_stats(db)


def overview_page_data(
    db: Session,
    *,
    status: str | None = None,
    plan_kind: str | None = None,
    search: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict[str, object]:
    """Build page data for the catalog overview route.

    Returns offers, subscription counts, pagination, and enum value lists.
    """
    stmt = select(CatalogOffer)
    if search:
        stmt = stmt.where(
            CatalogOffer.name.ilike(f"%{search}%") | CatalogOffer.code.ilike(f"%{search}%")
        )
    if status:
        stmt = stmt.where(CatalogOffer.status == OfferStatus(status))
    normalized_plan_kind = str(plan_kind or "").strip().lower()
    if normalized_plan_kind in {PLAN_KIND_IP_ADDRESS, PLAN_KIND_DEVICE_REPLACEMENT}:
        stmt = stmt.where(CatalogOffer.description.ilike(f"%[plan_kind:{normalized_plan_kind}]%"))
    elif normalized_plan_kind == PLAN_KIND_STANDARD:
        stmt = stmt.where(
            (CatalogOffer.description.is_(None))
            | (
                (~CatalogOffer.description.ilike("%[plan_kind:ip_address]%"))
                & (~CatalogOffer.description.ilike("%[plan_kind:device_replacement]%"))
            )
        )

    total: int = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page

    offers = db.scalars(
        stmt.order_by(CatalogOffer.created_at.desc()).limit(per_page).offset(offset)
    ).all()
    offer_plan_metadata = {}
    for offer in offers:
        metadata, _ = parse_offer_description_metadata(offer.description)
        plan = str(metadata.get("plan_kind") or PLAN_KIND_STANDARD).strip().lower()
        if plan not in PLAN_KINDS:
            plan = PLAN_KIND_STANDARD
        offer_plan_metadata[str(offer.id)] = {
            "plan_kind": plan,
            "ip_block_size": metadata.get("ip_block_size"),
        }

    offer_ids = [offer.id for offer in offers]
    offer_subscription_counts: dict[str, int] = {}
    offer_active_subscription_counts: dict[str, int] = {}
    if offer_ids:
        rows = db.execute(
            select(Subscription.offer_id, func.count(Subscription.id))
            .where(Subscription.offer_id.in_(offer_ids))
            .group_by(Subscription.offer_id)
        ).all()
        offer_subscription_counts = {str(row[0]): row[1] for row in rows}

        active_rows = db.execute(
            select(Subscription.offer_id, func.count(Subscription.id))
            .where(Subscription.offer_id.in_(offer_ids))
            .where(Subscription.status == SubscriptionStatus.active)
            .group_by(Subscription.offer_id)
        ).all()
        offer_active_subscription_counts = {str(row[0]): row[1] for row in active_rows}

    radius_profiles = catalog_service.radius_profiles.list(
        db=db, vendor=None, is_active=True, order_by="name", order_dir="asc", limit=200, offset=0
    )

    return {
        "offers": offers,
        "offer_subscription_counts": offer_subscription_counts,
        "offer_active_subscription_counts": offer_active_subscription_counts,
        "status": status,
        "plan_kind": normalized_plan_kind or "",
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
        "offer_plan_metadata": offer_plan_metadata,
    }


def create_offer_with_audit(
    db: Session,
    offer: dict[str, object],
    form: FormData,
    request: object,
    actor_id: str | None,
) -> object:
    """Create offer, link radius profile, sync addons, create price, and log audit.

    Returns the created offer ORM object.
    """
    payload = create_offer_payload(offer)
    created_offer = catalog_service.offers.create(db=db, payload=payload)

    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="catalog_offer",
        entity_id=str(created_offer.id),
        actor_id=actor_id,
        metadata={
            "name": created_offer.name,
            "service_type": created_offer.service_type.value if created_offer.service_type else None,
        },
    )

    if offer["radius_profile_id"]:
        upsert_radius_profile_link(db, str(created_offer.id), str(offer["radius_profile_id"]))

    addon_configs = parse_addon_links_from_form(form)
    if addon_configs:
        catalog_service.offer_addons.sync(
            db=db,
            offer_id=str(created_offer.id),
            addon_configs=addon_configs,
        )

    if offer["price_amount"]:
        created_price = create_recurring_price(db, str(created_offer.id), offer)
        if created_price:
            log_audit_event(
                db=db,
                request=request,
                action="price_created",
                entity_type="catalog_offer",
                entity_id=str(created_offer.id),
                actor_id=actor_id,
                metadata={
                    "price_amount": str(created_price.amount),
                    "currency": created_price.currency,
                },
            )

    return created_offer


def update_offer_with_audit(
    db: Session,
    offer_id: str,
    existing_offer: object,
    offer_data: dict[str, object],
    form: FormData,
    request: object,
    actor_id: str | None,
) -> object:
    """Update offer, sync radius/addons/price, and log audit.

    Returns the updated offer ORM object.
    """
    before_snapshot = model_to_dict(existing_offer)
    payload = update_offer_payload(offer_data)
    updated_offer = catalog_service.offers.update(db=db, offer_id=offer_id, payload=payload)
    after_snapshot = model_to_dict(updated_offer)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata = {"changes": changes} if changes else None

    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="catalog_offer",
        entity_id=str(updated_offer.id),
        actor_id=actor_id,
        metadata=metadata,
    )

    upsert_radius_profile_link(db, offer_id, str(offer_data["radius_profile_id"]))

    addon_configs = parse_addon_links_from_form(form)
    catalog_service.offer_addons.sync(
        db=db,
        offer_id=offer_id,
        addon_configs=addon_configs,
    )

    price, price_action = upsert_recurring_price(db, offer_id, offer_data)
    if price and price_action:
        log_audit_event(
            db=db,
            request=request,
            action=f"price_{price_action}",
            entity_type="catalog_offer",
            entity_id=str(offer_id),
            actor_id=actor_id,
            metadata={
                "price_amount": str(price.amount),
                "currency": price.currency,
            },
        )

    return updated_offer
