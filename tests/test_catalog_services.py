from app.models.catalog import AccessType, PriceBasis, ServiceType
from app.schemas.catalog import CatalogOfferCreate, SubscriptionCreate
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
