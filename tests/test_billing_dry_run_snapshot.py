"""Regression: the dry-run billing snapshot leaves no committed changes.

``run_invoice_cycle(dry_run=True)`` does NOT commit, but it DOES dirty ORM
objects in the session before the dry-run branch — notably fast-forwarding
``subscription.next_billing_at`` (billing_automation.py:855). The snapshot CLI's
contract is to ``db.rollback()`` after the run so nothing persists. This test
exercises that: a past-due subscription's ``next_billing_at`` is dirtied
in-session by the dry run, and the rollback restores it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

from app.models.catalog import BillingCycle, Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.billing_automation import run_invoice_cycle


def test_dry_run_then_rollback_restores_next_billing_at(db_session, catalog_offer):
    past = datetime(2020, 1, 1, tzinfo=UTC)
    subscriber = Subscriber(
        first_name="D",
        last_name="R",
        email="dryrun@e.com",
        status=SubscriberStatus.active,
    )
    db_session.add(subscriber)
    db_session.flush()
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        next_billing_at=past,
        start_at=past,
    )
    db_session.add(sub)
    db_session.commit()
    original = sub.next_billing_at

    # Force a resolvable price so the past-due sub reaches the fast-forward path
    # (billing_automation.py:855) — we are testing the rollback contract, not
    # price resolution.
    with patch(
        "app.services.billing_automation._resolve_price",
        return_value=(Decimal("1000.00"), "NGN", BillingCycle.monthly),
    ):
        run_invoice_cycle(db_session, dry_run=True)

    # The dry run fast-forwarded next_billing_at in-session (the risk the CLI's
    # db.rollback() exists to discard).
    assert sub.next_billing_at != original, (
        "expected the dry run to dirty next_billing_at in the session"
    )

    # Contract: the dry run leaves NO committed change. Dropping the in-session
    # dirt and reloading from the DB must yield the original value — i.e. the
    # fast-forward was never persisted.
    db_session.expire(sub)
    assert sub.next_billing_at == original
