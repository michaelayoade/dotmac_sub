from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.billing import AccountAdjustment, Invoice, ServiceEntitlement
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    OfferPrice,
    PriceType,
    SubscriptionStatus,
)
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.prepaid_funding import PrepaidFundingReconstructionBatch
from app.models.subscriber import SubscriberStatus
from app.models.subscription_billing_treatment import (
    BillingTreatmentReason,
    BillingTreatmentStatus,
    SubscriptionBillingArrangement,
    SubscriptionBillingGrant,
    SubscriptionBillingTreatment,
)
from app.models.subscription_engine import SettingValueType
from app.services import billing_automation
from app.services.catalog.subscriptions import _validate_plan_change
from app.services.owner_commands import CommandContext
from app.services.prepaid_service_renewals import run_due_prepaid_service_renewals
from app.services.prepaid_threshold import resolve_prepaid_threshold_decision
from app.services.settings_cache import SettingsCache
from app.services.settings_spec import get_spec
from app.services.subscription_billing_grants import (
    SubscriptionBillingGrantError,
    stage_subscription_billing_grant,
)
from app.services.subscription_billing_treatments import (
    TREATMENT_WRITE_SCOPE,
    BillingTreatmentDecisionStatus,
    CreateBillingTreatmentCommand,
    SubscriptionBillingTreatmentError,
    create_subscription_billing_treatment,
    preview_subscription_billing_treatment,
    resolve_subscription_billing_treatment,
)


def _prepare_subscription(db, subscriber, subscription, *, mode, starts_at) -> None:
    subscriber.status = SubscriberStatus.active
    subscriber.is_active = True
    subscriber.billing_enabled = True
    subscriber.billing_mode = mode
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = mode
    subscription.billing_cycle = BillingCycle.monthly
    subscription.unit_price = Decimal("100.00")
    subscription.start_at = starts_at
    subscription.next_billing_at = starts_at
    subscription.offer.billing_mode = mode
    subscription.offer.billing_cycle = BillingCycle.monthly
    subscription.offer.is_active = True
    db.add(
        OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("100.00"),
            currency="NGN",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
    )
    db.commit()


def _approve(db, subscription, *, starts_at, ends_at=None, key="pytest-treatment"):
    subscription_id = subscription.id
    approved_end = ends_at or billing_automation._period_end(
        starts_at, BillingCycle.monthly
    )
    preview = preview_subscription_billing_treatment(
        db,
        subscription_id=subscription_id,
        treatment=SubscriptionBillingTreatment.complimentary,
        reason_code=BillingTreatmentReason.commercial_concession,
        reason="Management-approved complimentary service",
        starts_at=starts_at,
        ends_at=approved_end,
        sponsor_reference=None,
        cost_center=None,
        evaluated_at=starts_at - timedelta(minutes=1),
    )
    db.commit()
    command_id = uuid4()
    return create_subscription_billing_treatment(
        db,
        CreateBillingTreatmentCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="user:pytest",
                scope=TREATMENT_WRITE_SCOPE,
                reason="pytest approval",
                idempotency_key=key,
            ),
            subscription_id=subscription_id,
            treatment=SubscriptionBillingTreatment.complimentary,
            reason_code=BillingTreatmentReason.commercial_concession,
            reason="Management-approved complimentary service",
            starts_at=starts_at,
            ends_at=approved_end,
            sponsor_reference=None,
            cost_center=None,
            preview_effective_at=preview.evaluated_at,
            preview_fingerprint=preview.fingerprint,
        ),
    )


def test_approved_treatment_preserves_real_price_and_is_idempotent(
    db_session, subscriber, subscription
):
    starts_at = datetime.now(UTC) + timedelta(minutes=1)
    _prepare_subscription(
        db_session,
        subscriber,
        subscription,
        mode=BillingMode.prepaid,
        starts_at=starts_at,
    )
    subscription_id = subscription.id
    first = _approve(db_session, subscription, starts_at=starts_at)
    fingerprint = db_session.scalar(
        select(SubscriptionBillingArrangement.command_fingerprint).where(
            SubscriptionBillingArrangement.id == first.arrangement_id
        )
    )
    db_session.commit()
    command_id = uuid4()
    replay = create_subscription_billing_treatment(
        db_session,
        CreateBillingTreatmentCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="user:pytest",
                scope=TREATMENT_WRITE_SCOPE,
                reason="replay",
                idempotency_key="pytest-treatment",
            ),
            subscription_id=subscription_id,
            treatment=SubscriptionBillingTreatment.complimentary,
            reason_code=BillingTreatmentReason.commercial_concession,
            reason="Management-approved complimentary service",
            starts_at=starts_at,
            ends_at=first.ends_at,
            sponsor_reference=None,
            cost_center=None,
            preview_effective_at=starts_at - timedelta(minutes=1),
            preview_fingerprint=fingerprint,
        ),
    )
    arrangement = db_session.get(SubscriptionBillingArrangement, first.arrangement_id)
    assert replay.replayed is True
    assert arrangement.maximum_recurring_amount == Decimal("100.00")
    assert arrangement.approval_policy_max_days == 366
    assert arrangement.billing_cycle is BillingCycle.monthly
    assert subscription.unit_price == Decimal("100.00")


def test_grant_is_exact_idempotent_and_has_no_customer_money(
    db_session, subscriber, subscription
):
    starts_at = datetime.now(UTC) + timedelta(minutes=1)
    ends_at = billing_automation._period_end(starts_at, BillingCycle.monthly)
    _prepare_subscription(
        db_session,
        subscriber,
        subscription,
        mode=BillingMode.prepaid,
        starts_at=starts_at,
    )
    _approve(db_session, subscription, starts_at=starts_at, ends_at=ends_at)
    decision = resolve_subscription_billing_treatment(
        db_session, subscription, as_of=starts_at
    )
    initial_adjustments = db_session.query(AccountAdjustment).count()
    initial_invoices = db_session.query(Invoice).count()
    first = stage_subscription_billing_grant(
        db_session,
        subscription=subscription,
        decision=decision,
        starts_at=starts_at,
        ends_at=ends_at,
        actor="system:pytest",
    )
    db_session.commit()
    replay = stage_subscription_billing_grant(
        db_session,
        subscription=subscription,
        decision=decision,
        starts_at=starts_at,
        ends_at=ends_at,
        actor="system:pytest",
    )
    db_session.commit()
    entitlement = db_session.get(ServiceEntitlement, first.entitlement_id)
    assert replay.replayed is True
    assert db_session.query(SubscriptionBillingGrant).count() == 1
    assert entitlement.source_billing_grant_id == first.grant_id
    assert entitlement.amount_funded == Decimal("0.00")
    assert db_session.query(AccountAdjustment).count() == initial_adjustments
    assert db_session.query(Invoice).count() == initial_invoices


def test_zero_reference_value_does_not_fall_back_to_full_contract_price(
    db_session, subscriber, subscription
):
    starts_at = datetime.now(UTC) + timedelta(minutes=1)
    ends_at = billing_automation._period_end(starts_at, BillingCycle.monthly)
    _prepare_subscription(
        db_session,
        subscriber,
        subscription,
        mode=BillingMode.postpaid,
        starts_at=starts_at,
    )
    _approve(db_session, subscription, starts_at=starts_at, ends_at=ends_at)
    decision = resolve_subscription_billing_treatment(
        db_session, subscription, as_of=starts_at
    )

    with pytest.raises(SubscriptionBillingGrantError) as exc:
        stage_subscription_billing_grant(
            db_session,
            subscription=subscription,
            decision=decision,
            starts_at=starts_at,
            ends_at=ends_at,
            actor="system:pytest",
            reference_amount=Decimal("0.00"),
        )

    assert exc.value.code.endswith("invalid_reference_amount")
    assert db_session.query(SubscriptionBillingGrant).count() == 0


def test_stale_decision_cannot_grant_after_arrangement_revocation(
    db_session, subscriber, subscription
):
    starts_at = datetime.now(UTC) + timedelta(minutes=1)
    ends_at = billing_automation._period_end(starts_at, BillingCycle.monthly)
    _prepare_subscription(
        db_session,
        subscriber,
        subscription,
        mode=BillingMode.prepaid,
        starts_at=starts_at,
    )
    outcome = _approve(db_session, subscription, starts_at=starts_at, ends_at=ends_at)
    decision = resolve_subscription_billing_treatment(
        db_session, subscription, as_of=starts_at
    )
    arrangement = db_session.get(SubscriptionBillingArrangement, outcome.arrangement_id)
    arrangement.status = BillingTreatmentStatus.revoked
    db_session.flush()

    with pytest.raises(SubscriptionBillingGrantError) as exc:
        stage_subscription_billing_grant(
            db_session,
            subscription=subscription,
            decision=decision,
            starts_at=starts_at,
            ends_at=ends_at,
            actor="system:pytest",
        )

    assert exc.value.code.endswith("arrangement_not_effective")
    assert db_session.query(SubscriptionBillingGrant).count() == 0


def test_treatment_must_start_on_full_billing_boundary(
    db_session, subscriber, subscription
):
    billing_anchor = datetime.now(UTC) + timedelta(minutes=1)
    _prepare_subscription(
        db_session,
        subscriber,
        subscription,
        mode=BillingMode.postpaid,
        starts_at=billing_anchor,
    )

    with pytest.raises(SubscriptionBillingTreatmentError) as exc:
        preview_subscription_billing_treatment(
            db_session,
            subscription_id=subscription.id,
            treatment=SubscriptionBillingTreatment.complimentary,
            reason_code=BillingTreatmentReason.commercial_concession,
            reason="Management-approved complimentary service",
            starts_at=billing_anchor + timedelta(days=1),
            ends_at=billing_automation._period_end(
                billing_anchor + timedelta(days=1), BillingCycle.monthly
            ),
            sponsor_reference=None,
            cost_center=None,
            evaluated_at=billing_anchor - timedelta(minutes=1),
        )

    assert exc.value.code.endswith("unaligned_start")


def test_treatment_requires_finite_period(db_session, subscriber, subscription):
    starts_at = datetime.now(UTC) + timedelta(minutes=1)
    _prepare_subscription(
        db_session,
        subscriber,
        subscription,
        mode=BillingMode.postpaid,
        starts_at=starts_at,
    )

    with pytest.raises(SubscriptionBillingTreatmentError) as exc:
        preview_subscription_billing_treatment(
            db_session,
            subscription_id=subscription.id,
            treatment=SubscriptionBillingTreatment.complimentary,
            reason_code=BillingTreatmentReason.commercial_concession,
            reason="Management-approved complimentary service",
            starts_at=starts_at,
            ends_at=None,
            sponsor_reference=None,
            cost_center=None,
            evaluated_at=starts_at - timedelta(minutes=1),
        )

    assert exc.value.code.endswith("finite_period_required")


def test_registered_policy_caps_treatment_approval_horizon(
    db_session, subscriber, subscription
):
    spec = get_spec(SettingDomain.billing, "subscription_billing_treatment_max_days")
    assert spec is not None
    assert spec.default == 366
    assert spec.min_value == 1
    assert spec.max_value == 366
    db_session.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key="subscription_billing_treatment_max_days",
            value_type=SettingValueType.integer,
            value_text="31",
            is_active=True,
        )
    )
    db_session.commit()
    SettingsCache.invalidate(
        SettingDomain.billing.value, "subscription_billing_treatment_max_days"
    )
    starts_at = datetime.now(UTC) + timedelta(minutes=1)
    _prepare_subscription(
        db_session,
        subscriber,
        subscription,
        mode=BillingMode.postpaid,
        starts_at=starts_at,
    )
    ends_at = billing_automation._period_end(
        billing_automation._period_end(starts_at, BillingCycle.monthly),
        BillingCycle.monthly,
    )

    try:
        with pytest.raises(SubscriptionBillingTreatmentError) as exc:
            preview_subscription_billing_treatment(
                db_session,
                subscription_id=subscription.id,
                treatment=SubscriptionBillingTreatment.complimentary,
                reason_code=BillingTreatmentReason.commercial_concession,
                reason="Management-approved complimentary service",
                starts_at=starts_at,
                ends_at=ends_at,
                sponsor_reference=None,
                cost_center=None,
                evaluated_at=starts_at - timedelta(minutes=1),
            )
        assert exc.value.code.endswith("approval_horizon_exceeded")
        assert exc.value.details["maximum_days"] == 31
    finally:
        SettingsCache.invalidate(
            SettingDomain.billing.value,
            "subscription_billing_treatment_max_days",
        )


def test_prepaid_threshold_excludes_nonstandard_treatment(
    db_session, subscriber, subscription
):
    starts_at = datetime.now(UTC) + timedelta(minutes=1)
    _prepare_subscription(
        db_session,
        subscriber,
        subscription,
        mode=BillingMode.prepaid,
        starts_at=starts_at,
    )
    _approve(db_session, subscription, starts_at=starts_at)
    decision = resolve_prepaid_threshold_decision(
        db_session, subscriber, now=starts_at + timedelta(minutes=1), currency="NGN"
    )
    assert decision.threshold == Decimal("0.00")
    assert decision.non_billable_subscription_ids == (subscription.id,)
    assert decision.actionable_uncovered_subscription_ids == ()


def test_open_treatment_blocks_plan_change(db_session, subscriber, subscription):
    starts_at = datetime.now(UTC) + timedelta(minutes=1)
    _prepare_subscription(
        db_session,
        subscriber,
        subscription,
        mode=BillingMode.postpaid,
        starts_at=starts_at,
    )
    _approve(db_session, subscription, starts_at=starts_at)
    with pytest.raises(HTTPException) as exc:
        _validate_plan_change(db_session, subscription, str(uuid4()))
    assert exc.value.status_code == 409


def test_postpaid_and_prepaid_cycles_grant_without_customer_money(
    db_session, subscriber, subscription
):
    starts_at = datetime.now(UTC) + timedelta(minutes=1)
    _prepare_subscription(
        db_session,
        subscriber,
        subscription,
        mode=BillingMode.postpaid,
        starts_at=starts_at,
    )
    _approve(db_session, subscription, starts_at=starts_at)
    initial_invoices = db_session.query(Invoice).count()
    summary = billing_automation.run_invoice_cycle(db_session, run_at=starts_at)
    assert summary["non_cash_service_grants"] == 1
    assert db_session.query(Invoice).count() == initial_invoices


def test_price_above_approval_is_protected_drift(db_session, subscriber, subscription):
    starts_at = datetime.now(UTC) + timedelta(minutes=1)
    _prepare_subscription(
        db_session,
        subscriber,
        subscription,
        mode=BillingMode.prepaid,
        starts_at=starts_at,
    )
    _approve(db_session, subscription, starts_at=starts_at)
    subscription.unit_price = Decimal("200.00")
    db_session.commit()
    decision = resolve_subscription_billing_treatment(
        db_session, subscription, as_of=starts_at
    )
    assert decision.status is BillingTreatmentDecisionStatus.protected_drift
    assert decision.drift_reason == "approved_value_exceeded"
    assert decision.grantable is False


def test_sponsored_requires_funding_party_evidence(
    db_session, subscriber, subscription
):
    starts_at = datetime.now(UTC) + timedelta(minutes=1)
    _prepare_subscription(
        db_session,
        subscriber,
        subscription,
        mode=BillingMode.postpaid,
        starts_at=starts_at,
    )
    with pytest.raises(SubscriptionBillingTreatmentError) as exc:
        preview_subscription_billing_treatment(
            db_session,
            subscription_id=subscription.id,
            treatment=SubscriptionBillingTreatment.sponsored,
            reason_code=BillingTreatmentReason.sponsored_service,
            reason="Externally sponsored service",
            starts_at=starts_at,
            ends_at=billing_automation._period_end(starts_at, BillingCycle.monthly),
            sponsor_reference=None,
            cost_center=None,
            evaluated_at=starts_at - timedelta(minutes=1),
        )
    assert exc.value.code.endswith("missing_sponsor_evidence")


def test_non_cash_prepaid_grant_does_not_depend_on_funding_cutover(
    db_session, subscriber, subscription
):
    starts_at = datetime.now(UTC) + timedelta(minutes=1)
    _prepare_subscription(
        db_session,
        subscriber,
        subscription,
        mode=BillingMode.prepaid,
        starts_at=starts_at,
    )
    _approve(db_session, subscription, starts_at=starts_at)
    db_session.query(PrepaidFundingReconstructionBatch).delete(
        synchronize_session=False
    )
    db_session.commit()
    summary = run_due_prepaid_service_renewals(db_session, run_at=starts_at)
    assert summary["prepaid_renewals_non_cash_granted"] == 1
    assert summary["prepaid_renewals_skipped"] == "authority_not_materialized"
    assert db_session.query(SubscriptionBillingGrant).count() == 1
    assert db_session.query(AccountAdjustment).count() == 0
