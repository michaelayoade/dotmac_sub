from app.models.catalog import AccessType, PriceBasis, ServiceType
from app.schemas.catalog import CatalogOfferCreate
from app.schemas.settings import DomainSettingUpdate
from app.services import catalog as catalog_service
from app.services import settings_api


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
