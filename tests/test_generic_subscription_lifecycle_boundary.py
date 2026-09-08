"""Generic subscription updates cannot change lifecycle or enforcement state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.billing import ServiceEntitlement, ServiceEntitlementStatus
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.enforcement_lock import EnforcementReason
from app.schemas.catalog import SubscriptionTechnicalUpdate, SubscriptionUpdate
from app.services import catalog as catalog_service
from app.services.account_lifecycle import get_active_locks, suspend_subscription


def test_generic_api_contract_excludes_lifecycle_fields() -> None:
    with pytest.raises(ValidationError):
        SubscriptionTechnicalUpdate.model_validate({"status": "suspended"})
    with pytest.raises(ValidationError):
        SubscriptionTechnicalUpdate.model_validate(
            {"next_billing_at": "2026-07-25T00:00:00Z"}
        )


def test_technical_subscription_edit_preserves_lifecycle_lock(
    db_session, subscriber, subscription
):
    subscription_id = subscription.id
    subscription.status = SubscriptionStatus.active
    db_session.commit()
    suspend_subscription(
        db_session,
        str(subscription_id),
        reason=EnforcementReason.admin,
        source="admin:pytest",
        emit=False,
    )
    db_session.commit()

    updated = catalog_service.subscriptions.update(
        db_session,
        str(subscription_id),
        SubscriptionUpdate(service_description="router metadata corrected"),
    )

    locks = get_active_locks(db_session, subscription_id=str(subscription_id))
    assert updated.status == SubscriptionStatus.suspended
    assert [(lock.reason, lock.source) for lock in locks] == [
        (EnforcementReason.admin, "admin:pytest")
    ]


def test_active_historical_technical_edit_preserves_funded_future_anchor(
    db_session, subscriber, subscription
):
    now = datetime.now(UTC)
    historical_start = now - timedelta(days=365 * 5)
    funded_through = now + timedelta(days=29)
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = BillingMode.prepaid
    subscription.start_at = historical_start
    subscription.next_billing_at = funded_through
    subscriber.billing_mode = BillingMode.prepaid
    entitlement = ServiceEntitlement(
        account_id=subscriber.id,
        subscription_id=subscription.id,
        starts_at=now - timedelta(days=1),
        ends_at=funded_through,
        amount_funded=Decimal("35000.00"),
        status=ServiceEntitlementStatus.active,
    )
    db_session.add(entitlement)
    db_session.commit()

    updated = catalog_service.subscriptions.update(
        db_session,
        str(subscription.id),
        SubscriptionUpdate(service_description="router metadata corrected"),
    )

    assert updated.service_description == "router metadata corrected"
    assert updated.next_billing_at.replace(tzinfo=UTC) == funded_through
    db_session.refresh(entitlement)
    assert entitlement.status is ServiceEntitlementStatus.active
    assert entitlement.ends_at.replace(tzinfo=UTC) == funded_through
