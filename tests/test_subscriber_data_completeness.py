"""Subscriber data completeness: derived, never stored; suggestions never bind.

The invariant under test is that "complete" here means exactly what the
consuming report means — a subscriber cannot be complete for ``ncc_filing``
while the NCC return files them as Unknown.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    Offer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import (
    NINVerificationStatus,
    Subscriber,
    SubscriberCategory,
    SubscriberNINVerification,
)
from app.services import subscriber_data_completeness as completeness
from app.services.subscriber_data_completeness import FieldKey, Purpose


def _offer(db) -> Offer:
    offer = Offer(
        name=f"Plan {uuid.uuid4().hex[:5]}",
        code=f"P-{uuid.uuid4().hex[:6]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        speed_download_mbps=100,
        speed_upload_mbps=100,
        is_active=True,
    )
    db.add(offer)
    db.commit()
    return offer


def _subscriber(
    db, *, region=None, city=None, address_line1=None, **kwargs
) -> Subscriber:
    subscriber = Subscriber(
        first_name="A",
        last_name="B",
        email=f"s-{uuid.uuid4().hex[:8]}@x.io",
        subscriber_number=f"S-{uuid.uuid4().hex[:6]}",
        account_number=f"ACC-{uuid.uuid4().hex[:6]}",
        region=region,
        city=city,
        address_line1=address_line1,
        **kwargs,
    )
    subscriber.category = SubscriberCategory.residential
    db.add(subscriber)
    db.commit()
    return subscriber


def _subscribe(db, subscriber, offer, *, status=SubscriptionStatus.active):
    row = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        billing_mode=BillingMode.prepaid,
    )
    db.add(row)
    db.commit()
    return row


# ── ncc_filing completeness ─────────────────────────────────────────────────


def test_resolvable_state_is_complete_for_ncc_filing(db_session):
    subscriber = _subscriber(db_session, region="Lagos")
    assert completeness.missing_for(subscriber, Purpose.ncc_filing) == ()
    assert completeness.is_complete(subscriber, Purpose.ncc_filing) is True


def test_unresolvable_state_is_missing_for_ncc_filing(db_session):
    subscriber = _subscriber(db_session, address_line1="No landmark here")
    missing = completeness.missing_for(subscriber, Purpose.ncc_filing)
    assert [m.key for m in missing] == [FieldKey.state]
    assert missing[0].why  # the policy explains itself to a UI


def test_district_address_already_resolves_so_is_not_missing(db_session):
    """A district-only address is NOT a completeness gap: ``infer_state``
    already maps Wuse → FCT through the place gazetteer. Guards the boundary —
    if this ever fails, the queue has started demanding capture for data we
    already have."""
    subscriber = _subscriber(db_session, address_line1="Plot 5, Wuse 2")
    assert completeness.missing_for(subscriber, Purpose.ncc_filing) == ()


def test_completeness_agrees_with_the_report(db_session):
    """The invariant: complete here ⇔ not Unknown in the NCC return."""
    from app.services.ncc_subscriber_report import _UNKNOWN, infer_state

    resolvable = _subscriber(db_session, region="Kano")
    unresolvable = _subscriber(db_session, address_line1="Nothing to match")

    assert (infer_state(resolvable) != _UNKNOWN) is completeness.is_complete(
        resolvable, Purpose.ncc_filing
    )
    assert (infer_state(unresolvable) != _UNKNOWN) is completeness.is_complete(
        unresolvable, Purpose.ncc_filing
    )


# ── suggestions ─────────────────────────────────────────────────────────────


def test_state_has_no_suggester_because_the_signals_are_exhausted(db_session):
    """`infer_state` already spends every text signal a naive suggester would
    use, so a suggester built on those helpers could only ever return None for
    a subscriber `missing_for` flags — dead code. Rather than ship that, there
    is no state suggester until an independent signal (ONT GPS via Nominatim)
    is wired. This test pins that decision so it is revisited deliberately."""
    subscriber = _subscriber(db_session, address_line1="Nothing to match")
    assert completeness.missing_for(subscriber, Purpose.ncc_filing)
    assert completeness.suggest(subscriber, FieldKey.state) is None


def test_suggestions_never_complete_a_field(db_session):
    """Even with a suggester registered, a suggestion is unconfirmed evidence —
    it must not make a subscriber complete."""
    subscriber = _subscriber(db_session, address_line1="Nothing to match")
    fake = completeness.Suggestion(
        key=FieldKey.state,
        value="Lagos",
        source="test",
        confidence="low",
        note="unconfirmed",
    )
    completeness._SUGGESTERS[FieldKey.state] = lambda _s: fake
    try:
        assert completeness.suggest(subscriber, FieldKey.state) is fake
        # Still incomplete: the suggestion is not a captured fact.
        assert completeness.missing_for(subscriber, Purpose.ncc_filing)
        assert completeness.is_complete(subscriber, Purpose.ncc_filing) is False
        rows, _ = completeness.queue(db_session, Purpose.ncc_filing)
        row = next(r for r in rows if r.subscriber_id == str(subscriber.id))
        assert row.suggestions == (fake,)  # surfaced to the human, not applied
    finally:
        completeness._SUGGESTERS.pop(FieldKey.state, None)


def test_suggest_is_none_for_missing_subscriber():
    assert completeness.suggest(None, FieldKey.state) is None


# ── kyc ─────────────────────────────────────────────────────────────────────


def test_kyc_requires_verified_identity_not_merely_an_attempt(db_session):
    """Production carries 34 NIN rows, all failed. A verification row is not
    verification."""
    subscriber = _subscriber(db_session, region="Lagos", phone="08030000000")
    db_session.add(
        SubscriberNINVerification(
            subscriber_id=subscriber.id,
            nin="12345678901",
            status=NINVerificationStatus.failed,
        )
    )
    db_session.commit()
    db_session.refresh(subscriber)

    missing = completeness.missing_for(subscriber, Purpose.kyc)
    assert FieldKey.identity in [m.key for m in missing]

    db_session.add(
        SubscriberNINVerification(
            subscriber_id=subscriber.id,
            nin="12345678901",
            status=NINVerificationStatus.success,
        )
    )
    db_session.commit()
    db_session.refresh(subscriber)
    assert completeness.missing_for(subscriber, Purpose.kyc) == ()


# ── queue + readiness ───────────────────────────────────────────────────────


def test_queue_covers_only_active_subscription_subscribers(db_session):
    offer = _offer(db_session)
    incomplete_active = _subscriber(db_session, address_line1="Nothing to match")
    _subscribe(db_session, incomplete_active, offer)

    incomplete_canceled = _subscriber(db_session, address_line1="Nothing to match")
    _subscribe(
        db_session, incomplete_canceled, offer, status=SubscriptionStatus.canceled
    )

    complete_active = _subscriber(db_session, region="Lagos")
    _subscribe(db_session, complete_active, offer)

    no_subscription = _subscriber(db_session, address_line1="Nothing to match")

    rows, total = completeness.queue(db_session, Purpose.ncc_filing)
    ids = {r.subscriber_id for r in rows}

    assert str(incomplete_active.id) in ids
    assert str(incomplete_canceled.id) not in ids  # not in scope
    assert str(complete_active.id) not in ids  # nothing missing
    assert str(no_subscription.id) not in ids  # not in scope
    assert total == len(rows) == 1


def test_queue_pages_stably(db_session):
    offer = _offer(db_session)
    for _ in range(3):
        subscriber = _subscriber(db_session, address_line1="Nothing to match")
        _subscribe(db_session, subscriber, offer)

    first, total = completeness.queue(db_session, Purpose.ncc_filing, limit=2, offset=0)
    second, _ = completeness.queue(db_session, Purpose.ncc_filing, limit=2, offset=2)

    assert total == 3
    assert len(first) == 2 and len(second) == 1
    assert not {r.subscriber_id for r in first} & {r.subscriber_id for r in second}


def test_readiness_counts_add_up(db_session):
    offer = _offer(db_session)
    for region in ("Lagos", "Kano"):
        subscriber = _subscriber(db_session, region=region)
        _subscribe(db_session, subscriber, offer)
    for _ in range(3):
        subscriber = _subscriber(db_session, address_line1="Nothing to match")
        _subscribe(db_session, subscriber, offer)

    report = completeness.readiness(db_session, Purpose.ncc_filing)

    assert report["purpose"] == Purpose.ncc_filing.value
    assert report["total_in_scope"] == 5
    assert report["complete"] == 2
    assert report["incomplete"] == 3
    assert report["complete"] + report["incomplete"] == report["total_in_scope"]
    assert report["missing_by_field"] == {FieldKey.state.value: 3}


@pytest.mark.parametrize("purpose", list(Purpose))
def test_every_purpose_declares_why_each_field_is_required(purpose):
    """The policy must explain itself — a queue row shows `why` to an operator."""
    requirements = completeness.requirements_for(purpose)
    assert requirements
    for requirement in requirements:
        assert requirement.label.strip()
        assert requirement.why.strip()
