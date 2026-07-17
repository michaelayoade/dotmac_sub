"""Subscriber data completeness: derived, never stored; suggestions never bind.

Two invariants under test:

1. "complete" here means exactly what the consuming report means — a
   subscriber cannot be complete for ``ncc_filing`` while the NCC return files
   them as Unknown.
2. **complete is not verified.** A value we inferred and nobody confirmed is
   complete and unverified, and it belongs in the revalidation queue.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
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
from app.models.subscriber_field_verification import SubscriberFieldVerification
from app.services import subscriber_data_completeness as completeness
from app.services.subscriber_data_completeness import FieldKey, Provenance, Purpose


def _offer(db) -> CatalogOffer:
    offer = CatalogOffer(
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


def _capture(
    db,
    subscriber,
    *,
    key=FieldKey.state,
    value="Lagos",
    source="customer_portal",
    verified_at=None,
    evidence=None,
):
    """Append a confirmation to the ledger — what the capture slice will do."""
    row = SubscriberFieldVerification(
        subscriber_id=subscriber.id,
        field_key=key.value,
        value=value,
        source=source,
        verified_at=verified_at or datetime.now(UTC),
        verified_by_actor_id="actor-1",
        verified_by_actor_name="Ada Operator",
        evidence=evidence,
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
    # queue() is scoped to subscribers with an active subscription, so the
    # row only appears once this one is actually a customer.
    _subscribe(db_session, subscriber, _offer(db_session))
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

    # Confirmed and fresh — the only reason to leave the queue.
    verified_active = _subscriber(db_session, region="Lagos")
    _subscribe(db_session, verified_active, offer)
    _capture(db_session, verified_active, value="Lagos")

    no_subscription = _subscriber(db_session, address_line1="Nothing to match")

    rows, total = completeness.queue(db_session, Purpose.ncc_filing)
    ids = {r.subscriber_id for r in rows}

    assert str(incomplete_active.id) in ids
    assert str(incomplete_canceled.id) not in ids  # not in scope
    assert str(verified_active.id) not in ids  # confirmed, fresh
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


# ── provenance ──────────────────────────────────────────────────────────────


def test_a_ledger_row_makes_a_field_captured(db_session):
    subscriber = _subscriber(db_session, region="Lagos")
    _capture(db_session, subscriber, value="Lagos", source="customer_portal")

    (state,) = completeness.state_of(db_session, subscriber, Purpose.ncc_filing)
    assert state.provenance is Provenance.captured
    assert state.value == "Lagos"
    assert state.source == "customer_portal"
    assert state.verified_at is not None
    assert state.is_stale is False
    assert state.needs_revalidation is False


def test_inferred_state_is_complete_but_not_verified(db_session):
    """The heart of it: 3,558 production locations look like this — a value
    matched out of an address string that nobody ever confirmed. Complete,
    and not a fact."""
    subscriber = _subscriber(db_session, region="Lagos")

    (state,) = completeness.state_of(db_session, subscriber, Purpose.ncc_filing)
    assert state.provenance is Provenance.inferred
    assert state.needs_revalidation is True

    assert completeness.is_complete(subscriber, Purpose.ncc_filing) is True
    assert completeness.is_verified(db_session, subscriber, Purpose.ncc_filing) is False


def test_unresolvable_state_is_absent(db_session):
    subscriber = _subscriber(db_session, address_line1="Nothing to match")
    (state,) = completeness.state_of(db_session, subscriber, Purpose.ncc_filing)
    assert state.provenance is Provenance.absent
    assert state.needs_revalidation is True


def test_latest_capture_wins_over_earlier_ones(db_session):
    """The ledger is append-only: a customer who moves is re-confirmed, not
    overwritten, and the newest confirmation is the current one."""
    subscriber = _subscriber(db_session, region="Lagos")
    now = datetime.now(UTC)
    _capture(
        db_session,
        subscriber,
        value="Kano",
        source="agent",
        verified_at=now - timedelta(days=30),
    )
    _capture(
        db_session,
        subscriber,
        value="Lagos",
        source="customer_portal",
        verified_at=now,
    )

    (state,) = completeness.state_of(db_session, subscriber, Purpose.ncc_filing)
    assert state.value == "Lagos"
    assert state.source == "customer_portal"


def test_capture_carries_its_evidence(db_session):
    """A GPS fix travels with its accuracy so a consumer can refuse to derive
    an LGA from a fix too coarse to distinguish one."""
    subscriber = _subscriber(db_session, region="Lagos")
    row = _capture(
        db_session,
        subscriber,
        source="field_gps",
        evidence={"lat": 6.45, "lng": 3.39, "accuracy_m": 8},
    )
    db_session.refresh(row)
    assert row.evidence["accuracy_m"] == 8


# ── freshness ───────────────────────────────────────────────────────────────


def test_a_stale_capture_needs_revalidation(db_session):
    subscriber = _subscriber(db_session, region="Lagos")
    window = completeness.requirements_for(Purpose.ncc_filing)[0].revalidate_after
    assert window is not None
    _capture(
        db_session,
        subscriber,
        verified_at=datetime.now(UTC) - window - timedelta(days=1),
    )

    (state,) = completeness.state_of(db_session, subscriber, Purpose.ncc_filing)
    assert state.provenance is Provenance.captured
    assert state.is_stale is True
    assert state.needs_revalidation is True
    assert completeness.is_verified(db_session, subscriber, Purpose.ncc_filing) is False


def test_a_fresh_capture_is_verified(db_session):
    subscriber = _subscriber(db_session, region="Lagos")
    _capture(db_session, subscriber, verified_at=datetime.now(UTC))
    assert completeness.is_verified(db_session, subscriber, Purpose.ncc_filing) is True


def test_a_stale_capture_re_enters_the_queue(db_session):
    offer = _offer(db_session)
    subscriber = _subscriber(db_session, region="Lagos")
    _subscribe(db_session, subscriber, offer)
    window = completeness.requirements_for(Purpose.ncc_filing)[0].revalidate_after
    assert window is not None
    _capture(
        db_session,
        subscriber,
        verified_at=datetime.now(UTC) - window - timedelta(days=1),
    )

    rows, total = completeness.queue(db_session, Purpose.ncc_filing)
    assert total == 1
    assert rows[0].subscriber_id == str(subscriber.id)
    assert rows[0].missing == ()  # nothing absent — it is stale, not missing


# ── revalidate everyone ─────────────────────────────────────────────────────


def test_queue_includes_inferred_subscribers_not_just_absent(db_session):
    """ "Revalidate all customers, not just the ones we don't have." An inferred
    subscriber is a guess we are filing to a regulator, so it queues too."""
    offer = _offer(db_session)

    inferred = _subscriber(db_session, region="Lagos")
    _subscribe(db_session, inferred, offer)

    absent = _subscriber(db_session, address_line1="Nothing to match")
    _subscribe(db_session, absent, offer)

    captured = _subscriber(db_session, region="Kano")
    _subscribe(db_session, captured, offer)
    _capture(db_session, captured, value="Kano")

    rows, total = completeness.queue(db_session, Purpose.ncc_filing)
    ids = {r.subscriber_id for r in rows}

    assert str(inferred.id) in ids  # complete, unconfirmed — still queued
    assert str(absent.id) in ids
    assert str(captured.id) not in ids  # confirmed and fresh
    assert total == 2


def test_readiness_separates_verified_from_merely_complete(db_session):
    offer = _offer(db_session)

    captured = _subscriber(db_session, region="Kano")
    _subscribe(db_session, captured, offer)
    _capture(db_session, captured, value="Kano")

    for _ in range(2):
        inferred = _subscriber(db_session, region="Lagos")
        _subscribe(db_session, inferred, offer)

    absent = _subscriber(db_session, address_line1="Nothing to match")
    _subscribe(db_session, absent, offer)

    report = completeness.readiness(db_session, Purpose.ncc_filing)

    assert report["total_in_scope"] == 4
    # Three have a state; only one is a fact.
    assert report["complete"] == 3
    assert report["verified"] == 1
    assert report["needs_revalidation"] == 3
    assert report["incomplete"] == 1
    assert report["fields_by_provenance"] == {
        Provenance.captured.value: 1,
        Provenance.inferred.value: 2,
        Provenance.absent.value: 1,
    }
    assert sum(report["fields_by_provenance"].values()) == 4  # one field each
    assert report["verified"] + report["needs_revalidation"] == report["total_in_scope"]
    assert report["stale_fields"] == 0


def test_readiness_counts_stale_fields(db_session):
    offer = _offer(db_session)
    subscriber = _subscriber(db_session, region="Lagos")
    _subscribe(db_session, subscriber, offer)
    window = completeness.requirements_for(Purpose.ncc_filing)[0].revalidate_after
    assert window is not None
    _capture(
        db_session,
        subscriber,
        verified_at=datetime.now(UTC) - window - timedelta(days=1),
    )

    report = completeness.readiness(db_session, Purpose.ncc_filing)
    assert report["stale_fields"] == 1
    assert report["verified"] == 0
    assert report["fields_by_provenance"][Provenance.captured.value] == 1
