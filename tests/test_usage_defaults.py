from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.catalog import AccessType, PriceBasis, ServiceType
from app.models.usage import UsageCharge, UsageChargeStatus, UsageSource
from app.schemas.catalog import (
    CatalogOfferCreate,
    SubscriptionCreate,
    UsageAllowanceCreate,
)
from app.schemas.settings import DomainSettingUpdate
from app.schemas.usage import UsageRatingRunRequest, UsageRecordCreate
from app.services import catalog as catalog_service
from app.services import settings_api
from app.services import usage as usage_service


def test_usage_charge_defaults_use_settings(db_session, subscriber_account):
    settings_api.upsert_billing_setting(
        db_session, "default_currency", DomainSettingUpdate(value_text="EUR")
    )
    settings_api.upsert_usage_setting(
        db_session, "default_charge_status", DomainSettingUpdate(value_text="posted")
    )
    allowance = catalog_service.usage_allowances.create(
        db_session,
        UsageAllowanceCreate(
            name="Metered",
            included_gb=0,
            overage_rate=Decimal("1.00"),
        ),
    )
    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Fiber 1G Home",
            code="FIBER1G",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            usage_allowance_id=allowance.id,
        ),
    )
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber_account.id,
            offer_id=offer.id,
        ),
    )
    period_start = datetime(2024, 1, 1, tzinfo=UTC)
    period_end = datetime(2024, 2, 1, tzinfo=UTC)
    usage_service.usage_records.create(
        db_session,
        UsageRecordCreate(
            subscription_id=subscription.id,
            source=UsageSource.api,
            recorded_at=period_start + timedelta(days=1),
            total_gb=Decimal("1.00"),
        ),
    )
    usage_service.usage_rating_runs.run(
        db_session,
        UsageRatingRunRequest(period_start=period_start, period_end=period_end),
    )
    charge = (
        db_session.query(UsageCharge)
        .filter(UsageCharge.subscription_id == subscription.id)
        .first()
    )
    assert charge is not None
    assert charge.currency == "EUR"
    assert charge.status == UsageChargeStatus.posted
