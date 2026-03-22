from app.models.catalog import AccessType, BillingCycle, NasVendor, PriceBasis, ServiceType
from app.schemas.catalog import (
    CatalogOfferCreate,
    CatalogOfferUpdate,
    SubscriptionCreate,
)
from app.schemas.settings import DomainSettingUpdate
from app.services import catalog as catalog_service
from app.services import settings_api
from app.services import web_catalog_offers as web_catalog_offers_service


def test_catalog_offer_create_list(db_session):
    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Fiber 100 Home",
            code="FIBER100",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        ),
    )
    items = catalog_service.offers.list(
        db_session,
        service_type=None,
        access_type=None,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert len(items) == 1
    assert items[0].id == offer.id


def test_catalog_offer_defaults_use_settings(db_session):
    settings_api.upsert_catalog_setting(
        db_session, "default_billing_cycle", DomainSettingUpdate(value_text="annual")
    )
    settings_api.upsert_catalog_setting(
        db_session, "default_contract_term", DomainSettingUpdate(value_text="twelve_month")
    )
    settings_api.upsert_catalog_setting(
        db_session, "default_offer_status", DomainSettingUpdate(value_text="inactive")
    )
    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Fiber 500 Home",
            code="FIBER500",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        ),
    )
    assert offer.billing_cycle.value == "annual"
    assert offer.contract_term.value == "twelve_month"
    assert offer.status.value == "inactive"


def test_offer_description_metadata_roundtrip():
    description = web_catalog_offers_service.normalize_offer_description(
        description="Public IP block add-on",
        plan_kind="ip_address",
        ip_block_size="/29",
    )
    meta, cleaned = web_catalog_offers_service.parse_offer_description_metadata(description)
    assert meta["plan_kind"] == "ip_address"
    assert meta["ip_block_size"] == "/29"
    assert cleaned == "Public IP block add-on"


def test_overview_page_data_filters_plan_kind_and_counts(db_session, subscriber):
    standard = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Standard Fiber",
            code="STD-1",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            description="Standard internet service",
        ),
    )
    ip_plan = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Public IP /29",
            code="IP-29",
            service_type=ServiceType.business,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            description=web_catalog_offers_service.normalize_offer_description(
                description="Extra public addresses",
                plan_kind="ip_address",
                ip_block_size="/29",
            ),
        ),
    )

    catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=ip_plan.id,
            status="active",
        ),
    )

    ip_only = web_catalog_offers_service.overview_page_data(
        db_session,
        plan_kind="ip_address",
        page=1,
        per_page=50,
    )
    assert len(ip_only["offers"]) == 1
    assert str(ip_only["offers"][0].id) == str(ip_plan.id)
    meta = ip_only["offer_plan_metadata"][str(ip_plan.id)]
    assert meta["plan_kind"] == "ip_address"
    assert meta["ip_block_size"] == "/29"
    assert ip_only["offer_active_subscription_counts"][str(ip_plan.id)] == 1

    standard_only = web_catalog_offers_service.overview_page_data(
        db_session,
        plan_kind="standard",
        page=1,
        per_page=50,
    )
    ids = {str(offer.id) for offer in standard_only["offers"]}
    assert str(standard.id) in ids
    assert str(ip_plan.id) not in ids


def test_ensure_offer_radius_profile_creates_generated_profile_and_link(db_session):
    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Unlimited Basic",
            code="UNL-BASIC",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            speed_download_mbps=6000,
            speed_upload_mbps=6000,
        ),
    )

    profile_id = web_catalog_offers_service.ensure_offer_radius_profile(db_session, offer)

    links = catalog_service.offer_radius_profiles.list(
        db_session,
        offer_id=str(offer.id),
        profile_id=None,
        order_by="offer_id",
        order_dir="asc",
        limit=10,
        offset=0,
    )

    assert profile_id
    assert len(links) == 1
    assert str(links[0].profile_id) == profile_id

    profile = catalog_service.radius_profiles.get(db_session, profile_id)
    assert profile.name == "Unlimited Basic"
    assert profile.code == web_catalog_offers_service.generated_radius_profile_code_for_offer(str(offer.id))
    assert profile.vendor == NasVendor.mikrotik
    assert profile.download_speed == 6000
    assert profile.upload_speed == 6000
    assert profile.mikrotik_rate_limit == "6000k/6000k"


def test_ensure_offer_radius_profile_updates_existing_generated_profile(db_session):
    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Starter Plan",
            code="STARTER",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            speed_download_mbps=3000,
            speed_upload_mbps=2000,
        ),
    )

    first_profile_id = web_catalog_offers_service.ensure_offer_radius_profile(db_session, offer)

    updated_offer = catalog_service.offers.update(
        db_session,
        str(offer.id),
        CatalogOfferUpdate(
            name="Starter Plan Plus",
            speed_download_mbps=7000,
            speed_upload_mbps=5000,
        ),
    )

    second_profile_id = web_catalog_offers_service.ensure_offer_radius_profile(
        db_session,
        updated_offer,
        previous_profile_id=first_profile_id,
    )

    assert second_profile_id == first_profile_id
    profile = catalog_service.radius_profiles.get(db_session, second_profile_id)
    assert profile.name == "Starter Plan Plus"
    assert profile.download_speed == 7000
    assert profile.upload_speed == 5000
    assert profile.mikrotik_rate_limit == "7000k/5000k"


def test_offer_form_context_exposes_full_billing_cycle_set(db_session):
    context = web_catalog_offers_service.offer_form_context(
        db_session,
        web_catalog_offers_service.default_offer_form(),
    )

    assert context["billing_cycles"] == [item.value for item in BillingCycle]
