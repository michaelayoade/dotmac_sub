"""Reviewed prepaid reconstruction is the final runtime funding authority."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.billing import (
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentStatus,
)
from app.models.catalog import BillingMode
from app.models.prepaid_funding import (
    PrepaidFundingBaseline,
    PrepaidFundingReconstructionBatch,
)
from app.models.splynx_transaction import SplynxBillingTransaction
from app.models.subscriber import Subscriber
from app.services.customer_financial_position import prepaid_available_balance
from app.services.prepaid_funding_reconstruction import (
    PrepaidFundingBaselineMissingError,
    PrepaidFundingReconstructionError,
    apply_prepaid_funding_reconstruction,
    authority_cutover_batch,
    parse_reconstruction_manifest,
    preview_prepaid_funding_reconstruction,
)


def _payload(position_at: datetime, balances: dict[object, str]) -> dict:
    return {
        "source": "splynx-final-plus-native-events:reviewed-test",
        "captured_at": position_at.isoformat().replace("+00:00", "Z"),
        "currency": "NGN",
        "accounts": [
            {
                "account_id": str(account_id),
                "available_balance": amount,
                "required_balance": "0.00",
            }
            for account_id, amount in balances.items()
        ],
    }


def _apply(db, position_at: datetime, balances: dict[object, str]):
    db.query(PrepaidFundingReconstructionBatch).filter(
        PrepaidFundingReconstructionBatch.source
        == "pytest-empty-native-install-cutover"
    ).delete()
    db.flush()
    payload = _payload(position_at, balances)
    digest = parse_reconstruction_manifest(payload).manifest_sha256
    return apply_prepaid_funding_reconstruction(
        db,
        payload,
        expected_manifest_sha256=digest,
        evidence_ref="finance-review:prepaid-reconstruction-test",
        approved_by="billing-operations-test",
        expected_account_ids=set(balances),
        now=position_at + timedelta(minutes=1),
    )


def test_preview_requires_the_exact_nonempty_cohort(db_session, subscriber):
    position_at = datetime.now(UTC) - timedelta(minutes=5)
    payload = _payload(position_at, {subscriber.id: "100.00"})

    empty = preview_prepaid_funding_reconstruction(
        db_session,
        payload,
        expected_account_ids=set(),
        now=position_at + timedelta(minutes=1),
    )
    assert "reconstruction_expected_cohort_empty" in empty.blockers
    assert any(
        blocker.startswith("unexpected_reconstruction_account:")
        for blocker in empty.blockers
    )

    missing = preview_prepaid_funding_reconstruction(
        db_session,
        payload,
        expected_account_ids={subscriber.id, uuid.uuid4()},
        now=position_at + timedelta(minutes=1),
    )
    assert any(
        blocker.startswith("missing_reconstruction_account:")
        for blocker in missing.blockers
    )


def test_apply_is_hash_bound_idempotent_and_marks_one_final_cutover(
    db_session, subscriber
):
    position_at = datetime.now(UTC) - timedelta(minutes=5)
    payload = _payload(position_at, {subscriber.id: "100.00"})
    digest = parse_reconstruction_manifest(payload).manifest_sha256

    with pytest.raises(PrepaidFundingReconstructionError, match="reviewed hash"):
        apply_prepaid_funding_reconstruction(
            db_session,
            payload,
            expected_manifest_sha256="0" * 64,
            evidence_ref="finance-review:wrong-hash",
            approved_by="billing-operations-test",
            expected_account_ids={subscriber.id},
            now=position_at + timedelta(minutes=1),
        )

    first = _apply(db_session, position_at, {subscriber.id: "100.00"})
    db_session.commit()
    replay = apply_prepaid_funding_reconstruction(
        db_session,
        payload,
        expected_manifest_sha256=digest,
        evidence_ref="finance-review:prepaid-reconstruction-test",
        approved_by="billing-operations-test",
        expected_account_ids={subscriber.id},
        now=position_at + timedelta(minutes=2),
    )

    assert first.idempotent_replay is False
    assert replay.idempotent_replay is True
    assert replay.batch.id == first.batch.id
    assert authority_cutover_batch(db_session).id == first.batch.id
    assert first.batch.is_authority_cutover is True


def test_runtime_uses_baseline_plus_native_events_and_never_splynx(
    db_session, subscriber
):
    subscriber.billing_mode = BillingMode.prepaid
    position_at = datetime.now(UTC) - timedelta(days=1)
    _apply(db_session, position_at, {subscriber.id: "100.00"})
    db_session.add_all(
        [
            SplynxBillingTransaction(
                splynx_transaction_id=991,
                splynx_customer_id=1001,
                subscriber_id=subscriber.id,
                entry_type="credit",
                amount=Decimal("9999.00"),
                description="Legacy mirror must not return",
                transaction_date=date.today(),
            ),
            Payment(
                account_id=subscriber.id,
                amount=Decimal("25.00"),
                refunded_amount=Decimal("0.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
                paid_at=position_at + timedelta(hours=1),
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal("10.00"),
                currency="NGN",
                memo="Approved post-reconstruction debit",
                effective_date=position_at + timedelta(hours=2),
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.adjustment,
                amount=Decimal("777.00"),
                currency="NGN",
                memo="Approved pre-reconstruction credit",
                effective_date=position_at - timedelta(hours=1),
            ),
        ]
    )
    db_session.commit()

    assert prepaid_available_balance(db_session, subscriber.id) == Decimal("115.00")


def test_archived_mirror_presence_does_not_apply_old_runtime_cutoff_heuristics(
    db_session, subscriber
):
    subscriber.billing_mode = BillingMode.prepaid
    position_at = datetime(2026, 3, 16, tzinfo=UTC)
    native_at = datetime(2026, 4, 1, tzinfo=UTC)
    _apply(db_session, position_at, {subscriber.id: "100.00"})
    db_session.add_all(
        [
            SplynxBillingTransaction(
                splynx_transaction_id=992,
                splynx_customer_id=1002,
                subscriber_id=subscriber.id,
                entry_type="credit",
                amount=Decimal("9999.00"),
                description="Archived migration evidence",
                transaction_date=position_at.date(),
            ),
            Payment(
                account_id=subscriber.id,
                amount=Decimal("25.00"),
                refunded_amount=Decimal("0.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
                paid_at=native_at,
                created_at=native_at,
            ),
        ]
    )
    db_session.commit()

    assert prepaid_available_balance(db_session, subscriber.id) == Decimal("125.00")


def test_reviewed_supersession_is_append_only_not_legacy_rollback(
    db_session, subscriber
):
    first_at = datetime.now(UTC) - timedelta(days=2)
    first = _apply(db_session, first_at, {subscriber.id: "100.00"})
    db_session.commit()
    second = _apply(
        db_session,
        first_at + timedelta(days=1),
        {subscriber.id: "125.00"},
    )
    db_session.commit()

    baselines = db_session.query(PrepaidFundingBaseline).all()
    inactive = [row for row in baselines if not row.is_active]
    active = [row for row in baselines if row.is_active]
    assert len(inactive) == 1
    assert len(active) == 1
    assert inactive[0].superseded_at is not None
    assert first.batch.is_authority_cutover is True
    assert second.batch.is_authority_cutover is False
    assert authority_cutover_batch(db_session).id == first.batch.id
    assert prepaid_available_balance(db_session, subscriber.id) == Decimal("125.00")


def test_pre_cutover_account_without_baseline_fails_closed(db_session, subscriber):
    subscriber.billing_mode = BillingMode.prepaid
    position_at = datetime.now(UTC) - timedelta(minutes=1)
    subscriber.created_at = position_at - timedelta(days=1)
    missing = Subscriber(
        first_name="Missing",
        last_name="Baseline",
        email="missing-prepaid-baseline@example.com",
        billing_mode=BillingMode.prepaid,
        created_at=position_at - timedelta(days=1),
    )
    db_session.add(missing)
    db_session.commit()
    _apply(
        db_session,
        position_at,
        {subscriber.id: "100.00"},
    )
    db_session.commit()

    with pytest.raises(PrepaidFundingBaselineMissingError, match="baseline missing"):
        prepaid_available_balance(db_session, missing.id)


def test_post_cutover_native_account_starts_from_zero(db_session, subscriber):
    subscriber.billing_mode = BillingMode.prepaid
    position_at = datetime.now(UTC) - timedelta(days=1)
    _apply(db_session, position_at, {subscriber.id: "100.00"})
    db_session.commit()

    native = Subscriber(
        first_name="Post",
        last_name="Cutover",
        email="post-cutover-prepaid@example.com",
        billing_mode=BillingMode.prepaid,
        created_at=position_at + timedelta(hours=1),
    )
    db_session.add(native)
    db_session.flush()
    db_session.add(
        Payment(
            account_id=native.id,
            amount=Decimal("50.00"),
            refunded_amount=Decimal("0.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            paid_at=position_at + timedelta(hours=2),
        )
    )
    db_session.commit()

    assert prepaid_available_balance(db_session, native.id) == Decimal("50.00")
