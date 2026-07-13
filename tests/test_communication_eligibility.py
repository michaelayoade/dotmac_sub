"""The consent ledger, and the one thing it must never do.

An unsubscribe is a refusal of *marketing*. It is not permission to stop sending
someone their invoice. The test that matters most here is not "does unsubscribe
work" -- it is "does unsubscribe leave the invoice alone".

Get that backwards and a customer who clicks unsubscribe on a promo silently
stops receiving bills, notices no problem, and finds out when they are
disconnected for non-payment. That is a billing incident wearing a consent
ledger's clothes.
"""

from __future__ import annotations

import pytest

from app.models.notification import (
    CommunicationSuppression,
    NotificationChannel,
    SuppressionReason,
    SuppressionScope,
)
from app.services import communication_eligibility as eligibility

EMAIL = NotificationChannel.email
SMS = NotificationChannel.sms


# ---------------------------------------------------------------------------
# The rule that carries the risk
# ---------------------------------------------------------------------------


def test_unsubscribe_blocks_marketing_but_NOT_the_invoice(db_session, subscriber):
    """The whole point. A marketing suppression must not touch billing."""
    eligibility.suppress_committed(
        db_session,
        channel=EMAIL,
        address="cust@example.com",
        scope=SuppressionScope.marketing,
        reason=SuppressionReason.unsubscribe,
    )

    # Marketing: blocked.
    assert not eligibility.may_send(
        db_session, channel=EMAIL, address="cust@example.com", category="marketing"
    )
    # Their invoice: still sent. This is the line that stops a consent feature
    # from becoming a billing outage.
    assert eligibility.may_send(
        db_session, channel=EMAIL, address="cust@example.com", category="billing"
    )
    # As are every other transactional category.
    for category in ("account", "service", "connectivity", "credentials", "usage"):
        assert eligibility.may_send(
            db_session, channel=EMAIL, address="cust@example.com", category=category
        ), f"{category} must survive a marketing unsubscribe"


def test_scope_all_blocks_everything_including_billing(db_session):
    """A hard bounce or spam complaint means the address is unusable. Sending
    an invoice to it is not a duty, it is a bounce loop."""
    eligibility.suppress_committed(
        db_session,
        channel=EMAIL,
        address="dead@example.com",
        scope=SuppressionScope.all,
        reason=SuppressionReason.bounce,
    )

    assert not eligibility.may_send(
        db_session, channel=EMAIL, address="dead@example.com", category="marketing"
    )
    assert not eligibility.may_send(
        db_session, channel=EMAIL, address="dead@example.com", category="billing"
    )


def test_an_unknown_category_is_treated_as_transactional(db_session):
    """Fail towards delivering the invoice, not towards silence.

    If a typo'd or newly-added category defaulted to *marketing*, a marketing
    suppression would silently start eating it.
    """
    eligibility.suppress_committed(
        db_session,
        channel=EMAIL,
        address="c@example.com",
        scope=SuppressionScope.marketing,
    )

    assert eligibility.may_send(
        db_session, channel=EMAIL, address="c@example.com", category="brand_new_thing"
    )
    assert eligibility.may_send(
        db_session, channel=EMAIL, address="c@example.com", category=None
    )


# ---------------------------------------------------------------------------
# Address handling -- a suppression must not be dodgeable
# ---------------------------------------------------------------------------


def test_suppression_cannot_be_dodged_by_case_or_punctuation(db_session):
    eligibility.suppress_committed(
        db_session, channel=EMAIL, address="Person@Example.COM"
    )
    eligibility.suppress_committed(db_session, channel=SMS, address="+234 801 234 5678")

    assert not eligibility.may_send(
        db_session, channel=EMAIL, address="person@example.com", category="marketing"
    )
    assert not eligibility.may_send(
        db_session, channel=SMS, address="2348012345678", category="marketing"
    )


def test_suppression_is_per_channel(db_session):
    """Unsubscribing from email does not consent-block their SMS, and vice
    versa. They are different contact methods."""
    eligibility.suppress_committed(db_session, channel=EMAIL, address="x@example.com")

    assert not eligibility.may_send(
        db_session, channel=EMAIL, address="x@example.com", category="marketing"
    )
    assert eligibility.may_send(
        db_session, channel=SMS, address="x@example.com", category="marketing"
    )


def test_no_address_is_not_a_consent_decision(db_session):
    """A missing recipient is a delivery bug. Do not launder it into
    'suppressed' -- let the sender fail loudly on its own terms."""
    assert eligibility.may_send(
        db_session, channel=EMAIL, address=None, category="marketing"
    )
    assert eligibility.may_send(
        db_session, channel=EMAIL, address="  ", category="marketing"
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def test_suppressing_twice_is_idempotent(db_session):
    first = eligibility.suppress_committed(
        db_session, channel=EMAIL, address="dup@example.com"
    )
    second = eligibility.suppress_committed(
        db_session, channel=EMAIL, address="DUP@example.com"
    )

    assert first.id == second.id
    assert db_session.query(CommunicationSuppression).count() == 1


def test_a_bounce_escalates_a_marketing_block_but_unsubscribe_never_downgrades(
    db_session,
):
    """Scope only ever escalates. A hard bounce must not be quietly downgraded
    to marketing-only because the customer later clicked an unsubscribe link --
    that would resume sending invoices to a dead mailbox."""
    eligibility.suppress_committed(
        db_session,
        channel=EMAIL,
        address="e@example.com",
        scope=SuppressionScope.marketing,
    )
    eligibility.suppress_committed(
        db_session,
        channel=EMAIL,
        address="e@example.com",
        scope=SuppressionScope.all,
        reason=SuppressionReason.bounce,
    )
    row = db_session.query(CommunicationSuppression).one()
    assert row.scope is SuppressionScope.all

    # Now an unsubscribe arrives. It must NOT pull the scope back to marketing.
    eligibility.suppress_committed(
        db_session,
        channel=EMAIL,
        address="e@example.com",
        scope=SuppressionScope.marketing,
        reason=SuppressionReason.unsubscribe,
    )
    db_session.refresh(row)
    assert row.scope is SuppressionScope.all


def test_unsuppress_restores_delivery(db_session):
    eligibility.suppress_committed(
        db_session, channel=EMAIL, address="back@example.com"
    )
    assert not eligibility.may_send(
        db_session, channel=EMAIL, address="back@example.com", category="marketing"
    )

    assert eligibility.unsuppress_committed(
        db_session, channel=EMAIL, address="BACK@example.com"
    )
    assert eligibility.may_send(
        db_session, channel=EMAIL, address="back@example.com", category="marketing"
    )
    assert not eligibility.unsuppress_committed(
        db_session, channel=EMAIL, address="back@example.com"
    )


def test_cannot_suppress_an_empty_address(db_session):
    with pytest.raises(ValueError, match="empty address"):
        eligibility.suppress_committed(db_session, channel=EMAIL, address="   ")


# ---------------------------------------------------------------------------
# Bulk path must agree with the single path
# ---------------------------------------------------------------------------


def test_filter_eligible_agrees_with_may_send(db_session):
    """Audience building must not be a second, drifting implementation of the
    same rule."""
    eligibility.suppress_committed(
        db_session,
        channel=EMAIL,
        address="no@example.com",
        scope=SuppressionScope.marketing,
    )
    eligibility.suppress_committed(
        db_session,
        channel=EMAIL,
        address="dead@example.com",
        scope=SuppressionScope.all,
    )
    addresses = ["yes@example.com", "no@example.com", "dead@example.com"]

    marketing = eligibility.filter_eligible(
        db_session, channel=EMAIL, addresses=addresses, category="marketing"
    )
    assert marketing == ["yes@example.com"]

    billing = eligibility.filter_eligible(
        db_session, channel=EMAIL, addresses=addresses, category="billing"
    )
    # The marketing unsubscribe does not block billing; the dead address does.
    assert sorted(billing) == ["no@example.com", "yes@example.com"]

    for category in ("marketing", "billing"):
        expected = [
            a
            for a in addresses
            if eligibility.may_send(
                db_session, channel=EMAIL, address=a, category=category
            )
        ]
        got = eligibility.filter_eligible(
            db_session, channel=EMAIL, addresses=addresses, category=category
        )
        assert sorted(got) == sorted(expected), (
            f"bulk and single disagree on {category}"
        )


def test_an_escalation_survives_without_a_commit(db_session):
    """The bug this exists to stop.

    `suppress()` escalates by mutating the existing row. If it does not flush,
    the change lives only in the Session -- and with autoflush off it is thrown
    away, leaving the row at `marketing`. The address hard-bounced, but we would
    go on sending it invoices.

    The committed variant could never catch this: commit flushes.
    """
    eligibility.suppress(
        db_session,
        channel=EMAIL,
        address="bounced@example.com",
        scope=SuppressionScope.marketing,
        reason=SuppressionReason.unsubscribe,
    )
    eligibility.suppress(
        db_session,
        channel=EMAIL,
        address="bounced@example.com",
        scope=SuppressionScope.all,
        reason=SuppressionReason.bounce,
    )

    row = db_session.query(CommunicationSuppression).one()
    db_session.refresh(row)  # re-read from the DB, discarding un-flushed state
    assert row.scope is SuppressionScope.all, "the escalation never reached the DB"
    assert not eligibility.may_send(
        db_session, channel=EMAIL, address="bounced@example.com", category="billing"
    )
