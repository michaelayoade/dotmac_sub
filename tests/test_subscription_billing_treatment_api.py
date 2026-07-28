from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.billing_treatments import (
    create_treatment,
    list_treatments,
    preview_treatment,
    revoke_treatment,
)
from app.models.catalog import BillingCycle, BillingMode, OfferPrice, PriceType
from app.models.subscription_billing_treatment import (
    BillingTreatmentReason,
    BillingTreatmentStatus,
    SubscriptionBillingTreatment,
)
from app.schemas.subscription_billing_treatment import (
    BillingTreatmentConfirmRequest,
    BillingTreatmentPreviewRequest,
    BillingTreatmentRevokeRequest,
)
from app.services.billing_automation import _period_end


def _prepare(db, subscription, starts_at) -> None:
    subscription.billing_mode = BillingMode.postpaid
    subscription.billing_cycle = BillingCycle.monthly
    subscription.unit_price = Decimal("25000.00")
    subscription.start_at = starts_at
    subscription.next_billing_at = starts_at
    subscription.offer.billing_mode = BillingMode.postpaid
    subscription.offer.billing_cycle = BillingCycle.monthly
    db.add(
        OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("25000.00"),
            currency="NGN",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
    )
    db.commit()


def _principal() -> dict[str, str]:
    return {"principal_id": str(uuid4()), "principal_type": "user"}


def test_admin_api_previews_creates_lists_and_revokes(db_session, subscription):
    starts_at = datetime.now(UTC) + timedelta(minutes=1)
    _prepare(db_session, subscription, starts_at)
    request = BillingTreatmentPreviewRequest(
        treatment=SubscriptionBillingTreatment.complimentary,
        reason_code=BillingTreatmentReason.internal_service,
        reason="Approved infrastructure service",
        starts_at=starts_at,
        ends_at=_period_end(starts_at, BillingCycle.monthly),
    )
    preview = preview_treatment(subscription.id, request, _principal(), db_session)
    created = create_treatment(
        subscription.id,
        BillingTreatmentConfirmRequest(
            **request.model_dump(),
            preview_effective_at=preview.evaluated_at,
            preview_fingerprint=preview.fingerprint,
            idempotency_key="api-treatment-create",
        ),
        _principal(),
        db_session,
    )
    rows = list_treatments(subscription.id, _principal(), db_session)
    revoked = revoke_treatment(
        created.arrangement_id,
        BillingTreatmentRevokeRequest(
            reason="Service retired", idempotency_key="api-treatment-revoke"
        ),
        _principal(),
        db_session,
    )
    assert preview.maximum_recurring_amount == Decimal("25000.00")
    assert preview.approval_policy_max_days == 366
    assert created.approval_policy_max_days == 366
    assert created.status is BillingTreatmentStatus.active
    assert len(rows) == 1
    assert revoked.status is BillingTreatmentStatus.revoked


def test_admin_api_maps_missing_sponsor_evidence_to_422(db_session, subscription):
    starts_at = datetime.now(UTC) + timedelta(minutes=1)
    _prepare(db_session, subscription, starts_at)
    with pytest.raises(HTTPException) as captured:
        preview_treatment(
            subscription.id,
            BillingTreatmentPreviewRequest(
                treatment=SubscriptionBillingTreatment.sponsored,
                reason_code=BillingTreatmentReason.sponsored_service,
                reason="Sponsored service",
                starts_at=starts_at,
                ends_at=_period_end(starts_at, BillingCycle.monthly),
            ),
            _principal(),
            db_session,
        )
    assert captured.value.status_code == 422
    assert captured.value.detail["code"].endswith("missing_sponsor_evidence")


def test_admin_api_requires_finite_treatment_end():
    with pytest.raises(ValidationError):
        BillingTreatmentPreviewRequest(
            treatment=SubscriptionBillingTreatment.complimentary,
            reason_code=BillingTreatmentReason.internal_service,
            reason="Approved infrastructure service",
            starts_at=datetime.now(UTC),
        )
