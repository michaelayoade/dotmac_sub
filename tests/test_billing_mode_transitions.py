from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.catalog import (
    BillingCycle,
    BillingMode,
    OfferPrice,
    PriceType,
    SubscriptionStatus,
)
from app.models.offer_availability import OfferBillingModeAvailability
from app.models.subscriber import SubscriberStatus
from app.services.billing_mode_transitions import (
    BILLING_MODE_WRITE_SCOPE,
    BillingModeTransitionError,
    ConfirmBillingModeTransitionCommand,
    PreviewBillingModeTransitionRequest,
    confirm_billing_mode_transition,
    preview_billing_mode_transition,
)
from app.services.owner_commands import CommandContext


def _prepare(db, subscriber, subscription, *, mode: BillingMode) -> None:
    now = datetime.now(UTC)
    subscriber.status = SubscriberStatus.active
    subscriber.is_active = True
    subscriber.billing_enabled = True
    subscriber.billing_mode = mode
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = mode
    subscription.billing_cycle = BillingCycle.monthly
    subscription.start_at = now - timedelta(days=10)
    subscription.next_billing_at = now + timedelta(days=20)
    subscription.unit_price = Decimal("5000.00")
    subscription.offer.billing_mode = mode
    subscription.offer.billing_cycle = BillingCycle.monthly
    db.add(
        OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("5000.00"),
            currency="NGN",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
    )
    db.add_all(
        [
            OfferBillingModeAvailability(
                offer_id=subscription.offer_id,
                billing_mode=BillingMode.prepaid,
                is_active=True,
            ),
            OfferBillingModeAvailability(
                offer_id=subscription.offer_id,
                billing_mode=BillingMode.postpaid,
                is_active=True,
            ),
        ]
    )
    db.commit()


def _command(account_id, target_mode, fingerprint, *, key=None):
    command_id = uuid4()
    return ConfirmBillingModeTransitionCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="user:pytest",
            scope=BILLING_MODE_WRITE_SCOPE,
            reason="Approved customer billing-mode change",
            idempotency_key=key or f"pytest-billing-mode:{command_id}",
        ),
        account_id=account_id,
        target_mode=target_mode,
        expected_preview_fingerprint=fingerprint,
    )


@pytest.mark.parametrize(
    ("current_mode", "target_mode"),
    (
        (BillingMode.prepaid, BillingMode.postpaid),
        (BillingMode.postpaid, BillingMode.prepaid),
    ),
)
def test_account_and_current_subscriptions_change_mode_atomically(
    db_session,
    subscriber,
    subscription,
    current_mode,
    target_mode,
):
    _prepare(db_session, subscriber, subscription, mode=current_mode)
    original_anchor = subscription.next_billing_at
    subscriber.prepaid_low_balance_at = datetime.now(UTC) - timedelta(days=1)
    subscriber.prepaid_deactivation_at = datetime.now(UTC) + timedelta(days=1)
    original_low_balance_at = subscriber.prepaid_low_balance_at
    original_deactivation_at = subscriber.prepaid_deactivation_at
    db_session.commit()
    preview = preview_billing_mode_transition(
        db_session,
        PreviewBillingModeTransitionRequest(
            account_id=subscriber.id,
            target_mode=target_mode,
        ),
    )
    assert preview.allowed is True
    assert preview.currency == "NGN"
    db_session.commit()

    outcome = confirm_billing_mode_transition(
        db_session,
        _command(subscriber.id, target_mode, preview.fingerprint),
    )

    assert outcome.prior_mode is current_mode
    assert outcome.billing_mode is target_mode
    assert outcome.changed_subscription_ids == (subscription.id,)
    assert subscriber.billing_mode is target_mode
    assert subscription.billing_mode is target_mode
    assert subscription.next_billing_at == original_anchor
    if current_mode is BillingMode.prepaid:
        assert subscriber.prepaid_low_balance_at is None
        assert subscriber.prepaid_deactivation_at is None
    else:
        assert subscriber.prepaid_low_balance_at == original_low_balance_at
        assert subscriber.prepaid_deactivation_at == original_deactivation_at


def test_confirmation_is_idempotent(db_session, subscriber, subscription):
    _prepare(db_session, subscriber, subscription, mode=BillingMode.prepaid)
    preview = preview_billing_mode_transition(
        db_session,
        PreviewBillingModeTransitionRequest(
            account_id=subscriber.id,
            target_mode=BillingMode.postpaid,
        ),
    )
    db_session.commit()
    key = f"pytest-billing-mode:{uuid4()}"
    first = confirm_billing_mode_transition(
        db_session,
        _command(
            subscriber.id,
            BillingMode.postpaid,
            preview.fingerprint,
            key=key,
        ),
    )
    db_session.commit()
    replay = confirm_billing_mode_transition(
        db_session,
        _command(
            subscriber.id,
            BillingMode.postpaid,
            preview.fingerprint,
            key=key,
        ),
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.changed_subscription_ids == (subscription.id,)


def test_pricing_review_blocks_conversion(db_session, subscriber, subscription):
    _prepare(db_session, subscriber, subscription, mode=BillingMode.prepaid)
    db_session.query(OfferPrice).filter(
        OfferPrice.offer_id == subscription.offer_id
    ).delete(synchronize_session=False)
    db_session.commit()

    preview = preview_billing_mode_transition(
        db_session,
        PreviewBillingModeTransitionRequest(
            account_id=subscriber.id,
            target_mode=BillingMode.postpaid,
        ),
    )

    assert preview.allowed is False
    assert "pricing_review_required" in {
        blocker.code for blocker in preview.readiness.blocking_blockers
    }


def test_non_billable_service_blocks_conversion(db_session, subscriber, subscription):
    _prepare(db_session, subscriber, subscription, mode=BillingMode.prepaid)
    price = (
        db_session.query(OfferPrice)
        .filter(OfferPrice.offer_id == subscription.offer_id)
        .one()
    )
    price.amount = Decimal("0.00")
    subscription.unit_price = Decimal("0.00")
    db_session.commit()

    preview = preview_billing_mode_transition(
        db_session,
        PreviewBillingModeTransitionRequest(
            account_id=subscriber.id,
            target_mode=BillingMode.postpaid,
        ),
    )

    codes = {blocker.code for blocker in preview.readiness.blocking_blockers}
    assert "non_billable_service" in codes
    assert "price_evidence_invalid" in codes


def test_offer_must_support_target_mode(db_session, subscriber, subscription):
    _prepare(db_session, subscriber, subscription, mode=BillingMode.prepaid)
    db_session.query(OfferBillingModeAvailability).filter(
        OfferBillingModeAvailability.offer_id == subscription.offer_id,
        OfferBillingModeAvailability.billing_mode == BillingMode.postpaid,
    ).delete(synchronize_session=False)
    db_session.commit()

    preview = preview_billing_mode_transition(
        db_session,
        PreviewBillingModeTransitionRequest(
            account_id=subscriber.id,
            target_mode=BillingMode.postpaid,
        ),
    )

    assert preview.allowed is False
    assert "target_mode_unavailable" in {
        blocker.code for blocker in preview.readiness.blocking_blockers
    }


def test_account_without_current_service_cannot_convert(
    db_session, subscriber, subscription
):
    _prepare(db_session, subscriber, subscription, mode=BillingMode.prepaid)
    subscription.status = SubscriptionStatus.canceled
    db_session.commit()

    preview = preview_billing_mode_transition(
        db_session,
        PreviewBillingModeTransitionRequest(
            account_id=subscriber.id,
            target_mode=BillingMode.postpaid,
        ),
    )

    assert preview.allowed is False
    assert "no_current_service" in {
        blocker.code for blocker in preview.readiness.blocking_blockers
    }


def test_disabled_account_cannot_convert(db_session, subscriber, subscription):
    _prepare(db_session, subscriber, subscription, mode=BillingMode.prepaid)
    subscriber.status = SubscriberStatus.disabled
    db_session.commit()

    preview = preview_billing_mode_transition(
        db_session,
        PreviewBillingModeTransitionRequest(
            account_id=subscriber.id,
            target_mode=BillingMode.postpaid,
        ),
    )

    assert preview.allowed is False
    assert "account_status_ineligible" in {
        blocker.code for blocker in preview.readiness.blocking_blockers
    }


def test_stale_preview_cannot_change_mode(db_session, subscriber, subscription):
    _prepare(db_session, subscriber, subscription, mode=BillingMode.prepaid)
    preview = preview_billing_mode_transition(
        db_session,
        PreviewBillingModeTransitionRequest(
            account_id=subscriber.id,
            target_mode=BillingMode.postpaid,
        ),
    )
    subscription.next_billing_at = subscription.next_billing_at + timedelta(days=1)
    db_session.commit()

    with pytest.raises(BillingModeTransitionError) as exc_info:
        confirm_billing_mode_transition(
            db_session,
            _command(subscriber.id, BillingMode.postpaid, preview.fingerprint),
        )

    assert exc_info.value.code.endswith("stale_preview")
    assert subscriber.billing_mode is BillingMode.prepaid
    assert subscription.billing_mode is BillingMode.prepaid
