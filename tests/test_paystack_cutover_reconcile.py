"""Tests for the Paystack cutover reconciliation tools.

Covers the pure/decidable logic in the read-only exporter and the dry-run-first
credit poster. All Paystack HTTP is mocked; no network access.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.billing import (
    Payment,
    PaymentProvider,
    PaymentProviderType,
    PaymentStatus,
)
from app.models.subscriber import Subscriber
from scripts.one_off import paystack_cutover_post_credits as poster
from scripts.one_off import paystack_cutover_reconcile_export as exporter

# --- exporter: classification / recovery bucket -------------------------------


def _tx(**over):
    base = {
        "reference": "ref-1",
        "external_id": "1001",
        "status": "success",
        "amount": Decimal("5000.00"),
        "currency": "NGN",
        "paid_at": "2026-06-15T10:00:00Z",
        "created_at": "2026-06-15T10:00:00Z",
        "customer_email": "a@example.com",
        "metadata": {},
        "raw": {},
    }
    base.update(over)
    return exporter.GatewayTransaction(**base)


class _StubPayment:
    """Lightweight stand-in for a Payment row for classification tests."""

    def __init__(self, *, payment_id=None):
        self.id = payment_id or uuid.uuid4()


class _StubEvent:
    def __init__(self, *, payment_id=None):
        self.payment_id = payment_id


class _StubIntent:
    def __init__(self, *, completed_payment_id=None):
        self.completed_payment_id = completed_payment_id


def test_classification_recorded_payment_wins():
    result = exporter._classification(_tx(), _StubPayment(), None, None, None)
    assert result == "recorded_payment"


def test_classification_recorded_legacy_same_day():
    result = exporter._classification(_tx(), None, None, None, _StubPayment())
    assert result == "recorded_legacy_payment_same_day"


def test_classification_provider_event_links_payment():
    event = _StubEvent(payment_id=uuid.uuid4())
    result = exporter._classification(_tx(), None, event, None, None)
    assert result == "provider_event_links_payment_missing_locally"


def test_classification_intent_completed_payment():
    intent = _StubIntent(completed_payment_id=uuid.uuid4())
    result = exporter._classification(_tx(), None, None, intent, None)
    assert result == "intent_completed_payment_missing_locally"


def test_classification_provider_event_only():
    result = exporter._classification(_tx(), None, _StubEvent(), None, None)
    assert result == "provider_event_only_no_payment"


def test_classification_intent_only():
    result = exporter._classification(_tx(), None, None, _StubIntent(), None)
    assert result == "intent_only_no_payment"


def test_classification_missing_success_payment():
    result = exporter._classification(_tx(status="success"), None, None, None, None)
    assert result == "missing_success_payment"


def test_classification_not_success_no_local_payment():
    result = exporter._classification(_tx(status="failed"), None, None, None, None)
    assert result == "not_success_no_local_payment"


def test_recovery_bucket_already_recorded():
    assert (
        exporter._recovery_bucket("recorded_payment", 1)
        == "already_recorded_or_not_success"
    )


def test_recovery_bucket_single_email_match():
    assert (
        exporter._recovery_bucket("missing_success_payment", 1) == "single_email_match"
    )


def test_recovery_bucket_no_email_match():
    assert exporter._recovery_bucket("missing_success_payment", 0) == "no_email_match"


def test_recovery_bucket_ambiguous_email_match():
    assert (
        exporter._recovery_bucket("missing_success_payment", 2)
        == "ambiguous_email_match"
    )


# --- exporter: legacy same-day matcher ----------------------------------------


def _make_subscriber(db_session) -> Subscriber:
    sub = Subscriber(
        first_name="Test",
        last_name="User",
        email=f"legacy-{uuid.uuid4().hex}@example.com",
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def _add_payment(
    db_session,
    *,
    account_id,
    amount,
    paid_at,
    memo="legacy import",
    status=PaymentStatus.succeeded,
    is_active=True,
) -> Payment:
    payment = Payment(
        account_id=account_id,
        amount=Decimal(str(amount)),
        currency="NGN",
        status=status,
        paid_at=paid_at,
        memo=memo,
        is_active=is_active,
    )
    db_session.add(payment)
    db_session.commit()
    db_session.refresh(payment)
    return payment


def test_legacy_same_day_matches_within_fee_delta(db_session):
    sub = _make_subscriber(db_session)
    paid_at = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
    # Gateway gross 5000, local net 4000 -> delta 1000 within tolerance (2500).
    payment = _add_payment(
        db_session, account_id=sub.id, amount="4000.00", paid_at=paid_at
    )
    tx = _tx(amount=Decimal("5000.00"), paid_at="2026-06-15T12:00:00Z")
    match = exporter._find_legacy_same_day_payment(db_session, tx, [sub])
    assert match is not None
    assert match.id == payment.id


def test_legacy_same_day_none_when_delta_too_large(db_session):
    sub = _make_subscriber(db_session)
    paid_at = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
    # delta 3000 > tolerance 2500 -> no match.
    _add_payment(db_session, account_id=sub.id, amount="2000.00", paid_at=paid_at)
    tx = _tx(amount=Decimal("5000.00"), paid_at="2026-06-15T12:00:00Z")
    match = exporter._find_legacy_same_day_payment(db_session, tx, [sub])
    assert match is None


def test_legacy_same_day_none_when_different_day(db_session):
    sub = _make_subscriber(db_session)
    paid_at = datetime(2026, 6, 14, 10, 0, tzinfo=UTC)
    _add_payment(db_session, account_id=sub.id, amount="4000.00", paid_at=paid_at)
    tx = _tx(amount=Decimal("5000.00"), paid_at="2026-06-15T12:00:00Z")
    match = exporter._find_legacy_same_day_payment(db_session, tx, [sub])
    assert match is None


def test_legacy_same_day_skips_recovery_credits(db_session):
    sub = _make_subscriber(db_session)
    paid_at = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
    _add_payment(
        db_session,
        account_id=sub.id,
        amount="4000.00",
        paid_at=paid_at,
        memo="Paystack cutover recovery ref: ref-x id: 9",
    )
    tx = _tx(
        amount=Decimal("5000.00"),
        paid_at="2026-06-15T12:00:00Z",
        reference="",
    )
    match = exporter._find_legacy_same_day_payment(db_session, tx, [sub])
    assert match is None


def test_legacy_same_day_none_when_no_subscribers(db_session):
    tx = _tx(amount=Decimal("5000.00"), paid_at="2026-06-15T12:00:00Z", reference="")
    match = exporter._find_legacy_same_day_payment(db_session, tx, [])
    assert match is None


# --- poster: verify guards ----------------------------------------------------


def _candidate(**over) -> poster.Candidate:
    base = {
        "reference": "ref-1",
        "paystack_id": "1001",
        "amount": Decimal("5000.00"),
        "currency": "NGN",
        "paid_at": datetime(2026, 6, 15, 10, 0, tzinfo=UTC),
        "account_id": uuid.uuid4(),
    }
    base.update(over)
    return poster.Candidate(**base)


def _verify_payload(**over) -> dict:
    base = {
        "status": "success",
        "id": 1001,
        "amount": 500000,  # kobo -> 5000.00 NGN
        "currency": "NGN",
    }
    base.update(over)
    return base


def test_verify_candidate_accepts_matching(db_session, monkeypatch):
    monkeypatch.setattr(poster, "verify_transaction", lambda db, ref: _verify_payload())
    # Should not raise.
    poster._verify_candidate(db_session, _candidate())


def test_verify_candidate_rejects_status_mismatch(db_session, monkeypatch):
    monkeypatch.setattr(
        poster,
        "verify_transaction",
        lambda db, ref: _verify_payload(status="abandoned"),
    )
    with pytest.raises(RuntimeError, match="status"):
        poster._verify_candidate(db_session, _candidate())


def test_verify_candidate_rejects_amount_mismatch(db_session, monkeypatch):
    monkeypatch.setattr(
        poster,
        "verify_transaction",
        lambda db, ref: _verify_payload(amount=400000),  # 4000.00
    )
    with pytest.raises(RuntimeError, match="amount changed"):
        poster._verify_candidate(db_session, _candidate())


def test_verify_candidate_rejects_currency_mismatch(db_session, monkeypatch):
    monkeypatch.setattr(
        poster,
        "verify_transaction",
        lambda db, ref: _verify_payload(currency="USD"),
    )
    with pytest.raises(RuntimeError, match="currency mismatch"):
        poster._verify_candidate(db_session, _candidate())


def test_post_candidate_dry_run_currency_mismatch_not_posted(db_session, monkeypatch):
    """A currency mismatch surfaces as an error and writes nothing."""
    provider = PaymentProvider(
        name=f"paystack-{uuid.uuid4().hex}",
        provider_type=PaymentProviderType.paystack,
    )
    db_session.add(provider)
    db_session.commit()
    db_session.refresh(provider)

    monkeypatch.setattr(
        poster,
        "verify_transaction",
        lambda db, ref: _verify_payload(currency="USD"),
    )
    with pytest.raises(RuntimeError, match="currency mismatch"):
        poster._post_candidate(db_session, provider, _candidate(), dry_run=True)

    assert db_session.query(Payment).count() == 0


def test_post_candidate_dry_run_would_post_on_match(db_session, monkeypatch):
    provider = PaymentProvider(
        name=f"paystack-{uuid.uuid4().hex}",
        provider_type=PaymentProviderType.paystack,
    )
    db_session.add(provider)
    db_session.commit()
    db_session.refresh(provider)

    monkeypatch.setattr(poster, "verify_transaction", lambda db, ref: _verify_payload())
    result = poster._post_candidate(db_session, provider, _candidate(), dry_run=True)
    assert result == "would_post"
    assert db_session.query(Payment).count() == 0


# --- poster: idempotency ------------------------------------------------------


def test_post_candidate_skips_existing_external_id(db_session, monkeypatch):
    provider = PaymentProvider(
        name=f"paystack-{uuid.uuid4().hex}",
        provider_type=PaymentProviderType.paystack,
    )
    db_session.add(provider)
    db_session.commit()
    db_session.refresh(provider)

    sub = _make_subscriber(db_session)
    existing = Payment(
        account_id=sub.id,
        provider_id=provider.id,
        amount=Decimal("5000.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        external_id="1001",
        paid_at=datetime(2026, 6, 15, 10, 0, tzinfo=UTC),
        is_active=True,
    )
    db_session.add(existing)
    db_session.commit()
    db_session.refresh(existing)

    called = {"verify": False}

    def _should_not_verify(db, ref):
        called["verify"] = True
        return _verify_payload()

    monkeypatch.setattr(poster, "verify_transaction", _should_not_verify)

    candidate = _candidate(paystack_id="1001", account_id=sub.id)
    result = poster._post_candidate(db_session, provider, candidate, dry_run=True)
    assert result == f"skip_existing:{existing.id}"
    # Idempotency short-circuits before any Paystack verify call.
    assert called["verify"] is False
    assert db_session.query(Payment).count() == 1


def test_existing_payment_matches_on_reference(db_session):
    provider = PaymentProvider(
        name=f"paystack-{uuid.uuid4().hex}",
        provider_type=PaymentProviderType.paystack,
    )
    db_session.add(provider)
    db_session.commit()
    db_session.refresh(provider)

    sub = _make_subscriber(db_session)
    existing = Payment(
        account_id=sub.id,
        provider_id=provider.id,
        amount=Decimal("5000.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        external_id="ref-1",
        is_active=True,
    )
    db_session.add(existing)
    db_session.commit()
    db_session.refresh(existing)

    candidate = _candidate(reference="ref-1", paystack_id="nope", account_id=sub.id)
    found = poster._existing_payment(db_session, provider.id, candidate)
    assert found is not None
    assert found.id == existing.id


def test_no_existing_payment_when_ids_absent(db_session):
    provider = PaymentProvider(
        name=f"paystack-{uuid.uuid4().hex}",
        provider_type=PaymentProviderType.paystack,
    )
    db_session.add(provider)
    db_session.commit()
    db_session.refresh(provider)

    candidate = _candidate(reference="", paystack_id="")
    assert poster._existing_payment(db_session, provider.id, candidate) is None
