"""The payment-health enforcement gate is a SYSTEMIC circuit-breaker, not a
hair-trigger.

A handful of unprocessed payment-webhook dead-letters must NOT halt all
collections — per-account "did this customer just pay / have a plan" protection
is handled under a row lock by ``_dunning_shield_reason``. Regression guard for
the 2026-06-26 collections leak where ``max_dead_letters`` defaulted to 0, so a
single dead-letter blocked every suspension/throttle.
"""

from app.models.billing import (
    PaymentWebhookDeadLetter,
    PaymentWebhookDeadLetterStatus,
)
from app.models.domain_settings import DomainSetting, SettingDomain
from app.services.billing_enforcement_guards import payment_channel_health


def _dead_letter(db, key):
    db.add(
        PaymentWebhookDeadLetter(
            provider_type="paystack",
            idempotency_key=key,
            status=PaymentWebhookDeadLetterStatus.failed,
        )
    )


def test_few_dead_letters_do_not_block_enforcement(db_session):
    for i in range(5):
        _dead_letter(db_session, f"dl-{i}")
    db_session.commit()

    health = payment_channel_health(db_session)

    assert "payment_webhook_dead_letters" not in health.reasons
    assert health.ok  # nothing else unhealthy in a clean test DB


def test_dead_letter_breaker_still_trips_above_threshold(db_session):
    # Lower the systemic threshold so the breaker is exercisable in a test.
    db_session.add(
        DomainSetting(
            domain=SettingDomain.collections,
            key="billing_enforcement_payment_max_dead_letters",
            value_text="2",
            is_active=True,
        )
    )
    for i in range(3):
        _dead_letter(db_session, f"dl-{i}")
    db_session.commit()

    health = payment_channel_health(db_session)

    assert "payment_webhook_dead_letters" in health.reasons
    assert not health.ok
