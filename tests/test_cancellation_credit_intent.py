"""The unified cancellation-credit-intent policy is the ONLY suppression path.

Before this change, `generate_credit: bool` was a scattered ad hoc flag with
independent `False` defaults at four call sites. These tests target the
single typed policy function and `cancel_subscription`'s wiring to it.
"""

from __future__ import annotations

from app.services.billing_automation import (
    CancellationCreditIntent,
    cancellation_credit_intent_should_evaluate,
)


def test_customer_requested_termination_is_evaluated() -> None:
    assert cancellation_credit_intent_should_evaluate(
        CancellationCreditIntent.CUSTOMER_REQUESTED_TERMINATION
    )


def test_administrative_termination_is_evaluated() -> None:
    assert cancellation_credit_intent_should_evaluate(
        CancellationCreditIntent.ADMINISTRATIVE_TERMINATION
    )


def test_administrative_correction_is_evaluated_for_no_credit() -> None:
    assert not cancellation_credit_intent_should_evaluate(
        CancellationCreditIntent.ADMINISTRATIVE_CORRECTION
    )


def test_recoverable_deletion_and_correction_are_the_only_suppressing_intents() -> None:
    suppressing = {
        CancellationCreditIntent.ADMINISTRATIVE_RECOVERABLE_DELETION,
        CancellationCreditIntent.ADMINISTRATIVE_CORRECTION,
    }
    for intent in CancellationCreditIntent:
        if intent in suppressing:
            assert not cancellation_credit_intent_should_evaluate(intent)
        else:
            assert cancellation_credit_intent_should_evaluate(intent)


def test_cancel_subscription_suppresses_credit_only_for_recoverable_deletion(
    db_session, monkeypatch
):
    """`cancel_subscription` must call `generate_cancellation_credit` for
    every intent except `administrative_recoverable_deletion`.

    This fails before the fix because `cancel_subscription` required a
    `generate_credit: bool` parameter with no typed-intent gate at all;
    after the fix, the gate is driven exclusively by
    `cancellation_credit_intent_should_evaluate`.
    """
    from app.models.catalog import SubscriptionStatus
    from app.services import account_lifecycle, billing_automation
    from tests.test_account_lifecycle import (
        _make_offer,
        _make_subscriber,
        _make_subscription,
    )

    calls: list[str] = []
    monkeypatch.setattr(
        billing_automation,
        "generate_cancellation_credit",
        lambda db, sub: calls.append(str(sub.id)),
    )

    subscriber = _make_subscriber(db_session)
    offer = _make_offer(db_session)

    for intent, should_call in (
        (CancellationCreditIntent.CUSTOMER_REQUESTED_TERMINATION, True),
        (CancellationCreditIntent.ADMINISTRATIVE_TERMINATION, True),
        (CancellationCreditIntent.ADMINISTRATIVE_RECOVERABLE_DELETION, False),
        (CancellationCreditIntent.ADMINISTRATIVE_CORRECTION, False),
    ):
        subscription = _make_subscription(
            db_session, subscriber, offer, status=SubscriptionStatus.active
        )
        db_session.commit()
        calls.clear()
        account_lifecycle.cancel_subscription(
            db_session,
            str(subscription.id),
            "test",
            "test",
            credit_intent=intent,
            emit=False,
        )
        assert bool(calls) == should_call, intent
