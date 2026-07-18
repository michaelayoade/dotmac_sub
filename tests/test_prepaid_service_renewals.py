from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.billing import (
    AccountAdjustment,
    LedgerEntryType,
    LedgerSource,
    ServiceEntitlement,
)
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    OfferPrice,
    PriceType,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import SubscriberStatus
from app.services.customer_financial_ledger import calculate_customer_balance
from app.services.prepaid_service_renewals import (
    confirm_prepaid_service_renewal,
    preview_prepaid_service_renewal,
    run_due_prepaid_service_renewals,
)
from scripts.one_off.reconcile_prepaid_service_cycle_gaps import (
    apply_reconciliation,
    parse_reconciliation_plan,
    preview_reconciliation,
)
from tests.prepaid_funding_helpers import materialize_test_prepaid_opening_balance


def _prepare(db_session, subscriber, subscription, amount="100.00"):
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = datetime(2026, 7, 1, tzinfo=UTC)
    db_session.commit()
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal(amount),
        position_at=datetime(2026, 6, 30, tzinfo=UTC),
    )


def _preview(db_session, subscription, amount="50.00"):
    return preview_prepaid_service_renewal(
        db_session,
        subscription_id=subscription.id,
        starts_at=datetime(2026, 7, 1, tzinfo=UTC),
        ends_at=datetime(2026, 7, 31, tzinfo=UTC),
        amount=Decimal(amount),
        currency="NGN",
    )


def test_prepaid_service_renewal_posts_exact_debit_and_entitlement(
    db_session, subscriber, subscription
):
    _prepare(db_session, subscriber, subscription)
    preview = _preview(db_session, subscription)

    assert preview.funding_before == Decimal("100.00")
    assert preview.funding_after == Decimal("50.00")
    assert preview.allowed is True

    result = confirm_prepaid_service_renewal(
        db_session,
        preview,
        evidence_ref="pytest:reviewed-service-cycle",
        commit=True,
    )

    assert result.ledger_entry.entry_type == LedgerEntryType.debit
    assert result.ledger_entry.source == LedgerSource.adjustment
    assert result.ledger_entry.effective_date.replace(tzinfo=UTC) == datetime(
        2026, 7, 1, tzinfo=UTC
    )
    assert result.entitlement.source_ledger_entry_id == result.ledger_entry.id
    assert result.entitlement.subscription_id == subscription.id
    assert result.adjustment.origin == "prepaid_service_renewal"
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("50.00")
    db_session.refresh(subscription)
    assert subscription.next_billing_at.replace(tzinfo=UTC) == datetime(
        2026, 7, 31, tzinfo=UTC
    )


def test_prepaid_service_renewal_is_idempotent(db_session, subscriber, subscription):
    _prepare(db_session, subscriber, subscription)
    preview = _preview(db_session, subscription)
    first = confirm_prepaid_service_renewal(
        db_session,
        preview,
        evidence_ref="pytest:reviewed-service-cycle",
        commit=True,
    )
    replay = confirm_prepaid_service_renewal(
        db_session,
        preview,
        evidence_ref="pytest:reviewed-service-cycle",
        commit=True,
    )

    assert replay.replayed is True
    assert replay.ledger_entry.id == first.ledger_entry.id
    assert db_session.query(AccountAdjustment).count() == 1
    assert db_session.query(ServiceEntitlement).count() == 1
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("50.00")


def test_prepaid_service_renewal_replay_checks_under_account_lock(
    db_session, subscriber, subscription, monkeypatch
):
    _prepare(db_session, subscriber, subscription)
    preview = _preview(db_session, subscription)
    confirm_prepaid_service_renewal(
        db_session,
        preview,
        evidence_ref="pytest:reviewed-service-cycle",
        commit=True,
    )

    locks: list[str] = []
    monkeypatch.setattr(
        "app.services.prepaid_service_renewals.lock_account",
        lambda _db, account_id: locks.append(account_id),
    )

    replay = confirm_prepaid_service_renewal(
        db_session,
        preview,
        evidence_ref="pytest:reviewed-service-cycle",
        commit=True,
    )

    assert replay.replayed is True
    assert locks == [str(subscriber.id)]


def test_prepaid_service_renewal_rejects_insufficient_canonical_funding(
    db_session, subscriber, subscription
):
    _prepare(db_session, subscriber, subscription, amount="40.00")
    preview = _preview(db_session, subscription)

    assert preview.allowed is False
    assert preview.shortfall == Decimal("10.00")
    with pytest.raises(HTTPException) as exc:
        confirm_prepaid_service_renewal(
            db_session,
            preview,
            evidence_ref="pytest:reviewed-service-cycle",
        )
    assert exc.value.status_code == 402
    assert db_session.query(AccountAdjustment).count() == 0


def test_prepaid_service_renewal_rejects_overlapping_entitlement(
    db_session, subscriber, subscription
):
    _prepare(db_session, subscriber, subscription)
    preview = _preview(db_session, subscription)
    confirm_prepaid_service_renewal(
        db_session,
        preview,
        evidence_ref="pytest:reviewed-service-cycle",
        commit=True,
    )

    with pytest.raises(HTTPException, match="already has active funding"):
        preview_prepaid_service_renewal(
            db_session,
            subscription_id=subscription.id,
            starts_at=datetime(2026, 7, 15, tzinfo=UTC),
            ends_at=datetime(2026, 8, 15, tzinfo=UTC),
            amount=Decimal("50.00"),
            currency="NGN",
        )


def _reconciliation_payload(subscriber, subscription):
    return {
        "schema": "dotmac.prepaid_service_cycle_reconciliation.v1",
        "captured_at": "2026-07-18T09:43:32Z",
        "source": "pytest:isolated-audit-replay",
        "currency": "NGN",
        "candidate_cohort_sha256": "a" * 64,
        "blocker_manifest_sha256": "b" * 64,
        "entry_count": 1,
        "total_amount": "50.00",
        "entries": [
            {
                "account_id": str(subscriber.id),
                "subscription_id": str(subscription.id),
                "period_start": "2026-07-01T00:00:00Z",
                "period_end": "2026-07-31T00:00:00Z",
                "amount": "50.00",
                "funding_before": "100.00",
                "currency": "NGN",
                "reason": "due_service_charge_without_native_entitlement",
            }
        ],
    }


def test_reconciliation_plan_is_hash_bound_and_idempotent(
    db_session, subscriber, subscription
):
    _prepare(db_session, subscriber, subscription)
    plan = parse_reconciliation_plan(_reconciliation_payload(subscriber, subscription))

    dry_run = preview_reconciliation(db_session, plan)
    assert dry_run["ready"] is True
    assert dry_run["already_reconciled"] == 0

    first = apply_reconciliation(
        db_session,
        plan,
        evidence_ref="pytest:reviewed-gap-plan",
        approved_by="pytest",
    )
    assert first["applied"] == 1
    assert first["replayed"] == 0

    replay = apply_reconciliation(
        db_session,
        plan,
        evidence_ref="pytest:reviewed-gap-plan",
        approved_by="pytest",
    )
    assert replay["applied"] == 0
    assert replay["replayed"] == 1
    assert replay["already_reconciled"] == 1


def test_reconciliation_plan_rejects_tampered_total(subscriber, subscription):
    payload = _reconciliation_payload(subscriber, subscription)
    payload["total_amount"] = "49.99"

    with pytest.raises(ValueError, match="total_amount"):
        parse_reconciliation_plan(payload)


def test_reconciliation_plan_preserves_explicit_zero_result(
    db_session, subscriber, subscription
):
    payload = _reconciliation_payload(subscriber, subscription)
    payload["entry_count"] = 0
    payload["total_amount"] = "0.00"
    payload["entries"] = []

    plan = parse_reconciliation_plan(payload)
    preview = preview_reconciliation(db_session, plan)

    assert plan.entries == ()
    assert preview == {
        "plan_sha256": plan.sha256,
        "entries": 0,
        "accounts": 0,
        "total_amount": "0.00",
        "blocked_accounts": 0,
        "already_reconciled": 0,
        "ready": True,
    }


def _prepare_scheduled_cycle(db_session, subscriber, subscription):
    _prepare(db_session, subscriber, subscription)
    subscription.offer.billing_cycle = BillingCycle.monthly
    subscription.offer.is_active = True
    subscription.unit_price = Decimal("50.00")
    db_session.add(
        OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("50.00"),
            currency="NGN",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
    )
    db_session.commit()


def test_scheduled_owner_funds_current_due_cycle(db_session, subscriber, subscription):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)

    summary = run_due_prepaid_service_renewals(
        db_session,
        run_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
    )
    db_session.commit()

    assert summary["prepaid_renewals_scanned"] == 1
    assert summary["prepaid_renewals_funded"] == 1
    assert db_session.query(AccountAdjustment).count() == 1
    assert db_session.query(ServiceEntitlement).count() == 1
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("50.00")


def test_scheduled_owner_dry_run_writes_nothing(db_session, subscriber, subscription):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)

    summary = run_due_prepaid_service_renewals(
        db_session,
        run_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
        dry_run=True,
    )

    assert summary["prepaid_renewals_funded"] == 1
    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(ServiceEntitlement).count() == 0
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("100.00")


def test_scheduled_owner_refuses_catalog_fallback_without_contract_price(
    db_session, subscriber, subscription
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    subscription.unit_price = None
    db_session.commit()

    summary = run_due_prepaid_service_renewals(
        db_session,
        run_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
    )

    assert summary["prepaid_renewals_missing_price"] == 1
    assert summary["prepaid_renewals_funded"] == 0
    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(ServiceEntitlement).count() == 0
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("100.00")


def test_scheduled_owner_refuses_historical_catch_up(
    db_session, subscriber, subscription
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    subscription.next_billing_at = datetime(2026, 7, 1, tzinfo=UTC)
    db_session.commit()

    summary = run_due_prepaid_service_renewals(
        db_session,
        run_at=datetime(2026, 7, 5, tzinfo=UTC),
    )

    assert summary["prepaid_renewals_stale_anchor"] == 1
    assert summary["prepaid_renewals_funded"] == 0
    assert db_session.query(AccountAdjustment).count() == 0


def test_scheduled_owner_restores_canonically_funded_prepaid_lock(
    db_session, subscriber, subscription
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    subscription.status = SubscriptionStatus.suspended
    subscriber.status = SubscriberStatus.suspended
    lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=subscriber.id,
        reason=EnforcementReason.prepaid,
        source="pytest:prepaid-balance",
        is_active=True,
    )
    db_session.add(lock)
    db_session.commit()

    summary = run_due_prepaid_service_renewals(
        db_session,
        run_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
    )
    db_session.commit()

    db_session.refresh(lock)
    db_session.refresh(subscription)
    assert summary["prepaid_renewals_funded"] == 1
    assert summary["prepaid_renewals_restored"] == 1
    assert lock.is_active is False
    assert subscription.status == SubscriptionStatus.active
