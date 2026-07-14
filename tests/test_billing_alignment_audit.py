"""Regression coverage for the read-only billing alignment harness."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Uuid,
    event,
)

from app.models.billing import (
    CreditNote,
    CreditNoteStatus,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentStatus,
)
from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionEntry,
    ServiceExtensionScope,
    ServiceExtensionStatus,
)
from app.models.splynx_transaction import SplynxBillingTransaction
from app.models.subscriber import Subscriber
from app.services.customer_financial_ledger import (
    LEGACY_LEDGER_CUTOVER,
    PAYMENT_ACTIVITY_AT,
    SERVICE_ACTIVITY_AT,
    calculate_customer_balance,
)
from scripts.one_off.billing_alignment_audit import (
    _batch_customer_positions,
    _batch_reconstructed_positions,
    _configure_read_only_session,
    d1_double_swings,
    d2_unbacked_deactivated_credits,
    d12_enforcement_mismatch,
)
from scripts.one_off.export_prepaid_funding_snapshot import (
    build_prepaid_funding_snapshot,
)
from scripts.one_off.reconstruct_splynx_mirror import (
    _date as normalize_splynx_date,
)
from scripts.one_off.reconstruct_splynx_mirror import (
    _entry_type as normalize_splynx_entry_type,
)


def test_d1_detector_finds_legacy_corrupted_pair(db_session, subscriber):
    original = LedgerEntry(
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=Decimal("2500.00"),
        currency="NGN",
        memo="Top-up",
        is_active=False,
    )
    db_session.add(original)
    db_session.flush()
    reversal = LedgerEntry(
        account_id=subscriber.id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.payment,
        amount=Decimal("2500.00"),
        currency="NGN",
        memo=f"Reversal of ledger entry {original.id}",
    )
    db_session.add(reversal)
    db_session.commit()

    finding = d1_double_swings(db_session)

    assert finding.count == 1
    assert finding.amount == Decimal("2500.00")
    assert finding.rows[0]["original_id"] == str(original.id)
    assert finding.rows[0]["balance_affecting"] is True


def test_batch_position_matches_canonical_native_balance(db_session, subscriber):
    db_session.add_all(
        [
            Payment(
                account_id=subscriber.id,
                amount=Decimal("100.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
            ),
            Invoice(
                account_id=subscriber.id,
                invoice_number="ALIGN-BATCH-1",
                status=InvoiceStatus.issued,
                subtotal=Decimal("30.00"),
                total=Decimal("30.00"),
                balance_due=Decimal("30.00"),
                currency="NGN",
                is_proforma=False,
            ),
            CreditNote(
                account_id=subscriber.id,
                credit_number="ALIGN-CN-1",
                status=CreditNoteStatus.issued,
                subtotal=Decimal("5.00"),
                total=Decimal("5.00"),
                currency="NGN",
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal("10.00"),
                currency="NGN",
                memo="Approved manual adjustment",
            ),
        ]
    )
    db_session.commit()

    expected = calculate_customer_balance(db_session, str(subscriber.id))
    actual = _batch_customer_positions(db_session, [subscriber.id], currency="NGN")

    assert expected == Decimal("65.00")
    assert actual[(str(subscriber.id), "NGN")] == expected


def test_batch_position_uses_constant_query_count(db_session, subscriber):
    statements = 0

    def count_statement(*_args):
        nonlocal statements
        statements += 1

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", count_statement)
    try:
        _batch_customer_positions(db_session, [subscriber.id], currency="NGN")
    finally:
        event.remove(bind, "before_cursor_execute", count_statement)

    # Mirror discovery plus payments, allocations, invoices, credit notes and
    # operational ledger. The count is per batch, not per account.
    assert statements <= 6


def test_batch_position_matches_canonical_migrated_balance(db_session, subscriber):
    db_session.add(
        SplynxBillingTransaction(
            splynx_transaction_id=900001,
            splynx_customer_id=900001,
            subscriber_id=subscriber.id,
            entry_type="credit",
            amount=Decimal("100.00"),
            transaction_date=date(2026, 3, 1),
            deleted=False,
        )
    )
    db_session.add_all(
        [
            # Pre-window native documents are already represented by the
            # mirror and must not be counted a second time.
            Payment(
                account_id=subscriber.id,
                amount=Decimal("50.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
                created_at=PAYMENT_ACTIVITY_AT - timedelta(days=1),
            ),
            Payment(
                account_id=subscriber.id,
                amount=Decimal("20.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
                created_at=PAYMENT_ACTIVITY_AT + timedelta(days=1),
            ),
            Invoice(
                account_id=subscriber.id,
                invoice_number="ALIGN-MIRROR-OLD",
                status=InvoiceStatus.issued,
                subtotal=Decimal("30.00"),
                total=Decimal("30.00"),
                balance_due=Decimal("30.00"),
                currency="NGN",
                is_proforma=False,
                created_at=SERVICE_ACTIVITY_AT - timedelta(days=1),
            ),
            Invoice(
                account_id=subscriber.id,
                invoice_number="ALIGN-MIRROR-NEW",
                status=InvoiceStatus.issued,
                subtotal=Decimal("10.00"),
                total=Decimal("10.00"),
                balance_due=Decimal("10.00"),
                currency="NGN",
                is_proforma=False,
                created_at=SERVICE_ACTIVITY_AT + timedelta(days=1),
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal("5.00"),
                currency="NGN",
                memo="Old imported adjustment",
                effective_date=LEGACY_LEDGER_CUTOVER - timedelta(days=1),
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal("2.00"),
                currency="NGN",
                memo="New approved adjustment",
                effective_date=LEGACY_LEDGER_CUTOVER + timedelta(days=1),
            ),
        ]
    )
    db_session.commit()

    expected = calculate_customer_balance(db_session, str(subscriber.id))
    actual = _batch_customer_positions(db_session, [subscriber.id], currency="NGN")

    assert expected == Decimal("108.00")
    assert actual[(str(subscriber.id), "NGN")] == expected


def test_batch_position_preserves_per_currency_balances(db_session, subscriber):
    db_session.add_all(
        [
            Payment(
                account_id=subscriber.id,
                amount=Decimal("100.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
            ),
            Payment(
                account_id=subscriber.id,
                amount=Decimal("20.00"),
                currency="USD",
                status=PaymentStatus.succeeded,
            ),
        ]
    )
    db_session.commit()

    positions = _batch_customer_positions(db_session, [subscriber.id], currency=None)

    assert positions[(str(subscriber.id), "NGN")] == Decimal("100.00")
    assert positions[(str(subscriber.id), "USD")] == Decimal("20.00")


def test_batch_position_does_not_double_count_payment_linked_refund(
    db_session, subscriber
):
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("100.00"),
        refunded_amount=Decimal("20.00"),
        currency="NGN",
        status=PaymentStatus.partially_refunded,
    )
    db_session.add(payment)
    db_session.flush()
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            payment_id=payment.id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.refund,
            amount=Decimal("20.00"),
            currency="NGN",
        )
    )
    db_session.commit()

    expected = calculate_customer_balance(db_session, str(subscriber.id))
    actual = _batch_customer_positions(db_session, [subscriber.id], currency="NGN")

    assert expected == Decimal("80.00")
    assert actual[(str(subscriber.id), "NGN")] == expected


def _replay_tables(db_session, subscriber_id, subscription_id):
    metadata = MetaData()
    final_balances = Table(
        "audit_splynx_final_balances",
        metadata,
        Column("subscriber_id", Uuid(as_uuid=True), primary_key=True),
        Column("final_deposit", Numeric(19, 4), nullable=False),
    )
    final_services = Table(
        "audit_splynx_final_services",
        metadata,
        Column("splynx_service_id", Integer, primary_key=True),
        Column("subscriber_id", Uuid(as_uuid=True), nullable=False),
        Column("subscription_id", Uuid(as_uuid=True)),
        Column("source_status", String, nullable=False),
        Column("source_deleted", Boolean, nullable=False),
        Column("last_charge_total", Numeric(19, 4)),
        Column("last_period_from", Date),
        Column("last_period_to", Date),
    )
    metadata.create_all(db_session.get_bind())
    db_session.execute(
        final_balances.insert().values(
            subscriber_id=subscriber_id,
            final_deposit=Decimal("100.00"),
        )
    )
    db_session.execute(
        final_services.insert().values(
            splynx_service_id=990001,
            subscriber_id=subscriber_id,
            subscription_id=subscription_id,
            source_status="active",
            source_deleted=False,
            last_charge_total=Decimal("50.00"),
            last_period_from=date(2026, 6, 1),
            last_period_to=date(2026, 6, 30),
        )
    )
    return metadata


def _source_mapped_subscription(db_session, subscriber):
    offer = CatalogOffer(
        name="Replay source plan",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.prepaid,
    )
    db_session.add(offer)
    db_session.flush()
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
        unit_price=Decimal("999.00"),
        splynx_service_id=990001,
    )
    db_session.add(subscription)
    db_session.flush()
    return subscription


def test_replay_uses_source_balance_and_schedule_not_current_billing_outputs(
    db_session, subscriber
):
    subscription = _source_mapped_subscription(db_session, subscriber)
    metadata = _replay_tables(db_session, subscriber.id, subscription.id)
    subscriber.deposit = Decimal("999.00")
    db_session.add(
        Invoice(
            account_id=subscriber.id,
            invoice_number="PHANTOM-OUTPUT",
            status=InvoiceStatus.issued,
            total=Decimal("90.00"),
            balance_due=Decimal("90.00"),
            currency="NGN",
            is_proforma=False,
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
    )
    db_session.commit()
    try:
        replay = _batch_reconstructed_positions(
            db_session,
            [subscriber.id],
            snapshot_at=datetime(2026, 7, 12, tzinfo=UTC),
        )
    finally:
        metadata.drop_all(db_session.get_bind())

    assert replay.positions[str(subscriber.id)] == Decimal("50.00")
    assert replay.service_charges[str(subscriber.id)] == Decimal("50.00")
    assert str(subscriber.id) not in replay.incomplete


def test_replay_service_extension_shifts_schedule_without_crediting_cash(
    db_session, subscriber
):
    subscription = _source_mapped_subscription(db_session, subscriber)
    metadata = _replay_tables(db_session, subscriber.id, subscription.id)
    extension = ServiceExtension(
        reason="Approved outage compensation",
        window_start=datetime(2026, 6, 25, tzinfo=UTC),
        window_end=datetime(2026, 6, 30, tzinfo=UTC),
        days=14,
        scope_type=ServiceExtensionScope.subscribers,
        scope_subscriber_ids=[str(subscriber.id)],
        status=ServiceExtensionStatus.applied,
        affected_count=1,
        skipped_count=0,
        applied_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    db_session.add(extension)
    db_session.flush()
    db_session.add(
        ServiceExtensionEntry(
            extension_id=extension.id,
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            previous_next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
            new_next_billing_at=datetime(2026, 7, 15, tzinfo=UTC),
        )
    )
    db_session.commit()
    try:
        replay = _batch_reconstructed_positions(
            db_session,
            [subscriber.id],
            snapshot_at=datetime(2026, 7, 12, tzinfo=UTC),
        )
    finally:
        metadata.drop_all(db_session.get_bind())

    assert replay.positions[str(subscriber.id)] == Decimal("100.00")
    assert replay.service_charges.get(str(subscriber.id), Decimal("0.00")) == Decimal(
        "0.00"
    )


def test_funding_export_uses_owner_cohort_replay_and_threshold(db_session, subscriber):
    subscription = _source_mapped_subscription(db_session, subscriber)
    metadata = _replay_tables(db_session, subscriber.id, subscription.id)
    db_session.commit()
    try:
        export = build_prepaid_funding_snapshot(
            db_session,
            snapshot_at=datetime(2026, 7, 12, tzinfo=UTC),
            source="splynx-final-plus-native-events:test",
        )
    finally:
        metadata.drop_all(db_session.get_bind())

    assert export.ready is True
    assert export.candidate_ids == (str(subscriber.id),)
    assert export.positions[str(subscriber.id)] == Decimal("50.00")
    payload = export.funding_payload()
    assert payload["source"] == "splynx-final-plus-native-events:test"
    assert payload["captured_at"] == "2026-07-12T00:00:00Z"
    assert payload["accounts"] == [
        {
            "account_id": str(subscriber.id),
            "available_balance": "50.00",
            "required_balance": f"{export.thresholds[str(subscriber.id)]:.2f}",
        }
    ]


def test_funding_export_blocks_candidate_without_source_baseline(
    db_session, subscriber
):
    subscription = _source_mapped_subscription(db_session, subscriber)
    metadata = _replay_tables(db_session, subscriber.id, subscription.id)
    final_balances = metadata.tables["audit_splynx_final_balances"]
    db_session.execute(final_balances.delete())
    db_session.commit()
    try:
        export = build_prepaid_funding_snapshot(
            db_session,
            snapshot_at=datetime(2026, 7, 12, tzinfo=UTC),
            source="splynx-final-plus-native-events:test",
        )
    finally:
        metadata.drop_all(db_session.get_bind())

    assert export.ready is False
    assert export.missing_baseline == (str(subscriber.id),)
    with pytest.raises(ValueError, match="incomplete provenance"):
        export.funding_payload()


def test_funding_export_reports_incomplete_replay_reason(db_session, subscriber):
    subscription = _source_mapped_subscription(db_session, subscriber)
    metadata = _replay_tables(db_session, subscriber.id, subscription.id)
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.adjustment,
            amount=Decimal("500.00"),
            currency="NGN",
            effective_date=datetime(2026, 7, 2, tzinfo=UTC),
        )
    )
    db_session.commit()
    try:
        export = build_prepaid_funding_snapshot(
            db_session,
            snapshot_at=datetime(2026, 7, 12, tzinfo=UTC),
            source="splynx-final-plus-native-events:test",
        )
    finally:
        metadata.drop_all(db_session.get_bind())

    account_id = str(subscriber.id)
    assert export.ready is False
    assert export.incomplete[account_id] == (
        "post_legacy_adjustment_requires_provenance",
    )
    diagnostics = export.diagnostics_payload()
    assert diagnostics["incomplete_reason_counts"] == {
        "post_legacy_adjustment_requires_provenance": 1
    }


def test_replay_quarantines_unproven_post_legacy_adjustment(db_session, subscriber):
    subscription = _source_mapped_subscription(db_session, subscriber)
    metadata = _replay_tables(db_session, subscriber.id, subscription.id)
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.adjustment,
            amount=Decimal("500.00"),
            currency="NGN",
            effective_date=datetime(2026, 7, 2, tzinfo=UTC),
        )
    )
    db_session.commit()
    try:
        replay = _batch_reconstructed_positions(
            db_session,
            [subscriber.id],
            snapshot_at=datetime(2026, 7, 12, tzinfo=UTC),
        )
    finally:
        metadata.drop_all(db_session.get_bind())

    assert (
        "post_legacy_adjustment_requires_provenance"
        in replay.incomplete[str(subscriber.id)]
    )


def test_d2_excludes_pre_cutover_projection_when_account_has_cutoff_mirror(
    db_session, subscriber
):
    # Current deposit is intentionally different: it is an output under audit,
    # not a gate for accepting the source-faithful cutoff ledger.
    subscriber.deposit = Decimal("999.00")
    db_session.add_all(
        [
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.payment,
                amount=Decimal("100.00"),
                currency="NGN",
                is_active=False,
                effective_date=PAYMENT_ACTIVITY_AT - timedelta(days=1),
            ),
            SplynxBillingTransaction(
                splynx_transaction_id=900002,
                splynx_customer_id=900002,
                subscriber_id=subscriber.id,
                entry_type="credit",
                amount=Decimal("50.00"),
                transaction_date=date(2026, 3, 1),
                deleted=False,
            ),
        ]
    )
    db_session.commit()

    finding = d2_unbacked_deactivated_credits(db_session)

    assert finding.count == 0
    assert finding.amount == Decimal("0.00")
    assert "excluded cutoff-covered=1 rows / 1 accounts" in finding.note
    assert "subscriber.deposit is deliberately not consulted" in finding.note


def test_d2_keeps_credit_when_account_has_no_legacy_mirror(db_session, subscriber):
    subscriber.deposit = Decimal("100.00")
    entry = LedgerEntry(
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=Decimal("100.00"),
        currency="NGN",
        is_active=False,
        effective_date=PAYMENT_ACTIVITY_AT - timedelta(days=1),
    )
    db_session.add(entry)
    db_session.commit()

    finding = d2_unbacked_deactivated_credits(db_session)

    assert finding.count == 1
    assert finding.amount == Decimal("100.00")
    assert finding.rows[0]["verdict"] == "no_cutoff_mirror"


def test_d2_keeps_post_cutover_credit_even_when_account_has_cutoff_mirror(
    db_session, subscriber
):
    db_session.add_all(
        [
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.adjustment,
                amount=Decimal("75.00"),
                currency="NGN",
                is_active=False,
                effective_date=PAYMENT_ACTIVITY_AT + timedelta(days=1),
            ),
            SplynxBillingTransaction(
                splynx_transaction_id=900003,
                splynx_customer_id=900003,
                subscriber_id=subscriber.id,
                entry_type="credit",
                amount=Decimal("25.00"),
                transaction_date=date(2026, 3, 1),
                deleted=False,
            ),
        ]
    )
    db_session.commit()

    finding = d2_unbacked_deactivated_credits(db_session)

    assert finding.count == 1
    assert finding.amount == Decimal("75.00")
    assert finding.rows[0]["verdict"] == "post_cutover_credit_without_payment"


def test_d2_accepts_source_cutoff_deposit_without_transaction_history(
    db_session, subscriber
):
    metadata = MetaData()
    cutoff_balances = Table(
        "audit_splynx_cutoff_balances",
        metadata,
        Column("subscriber_id", Uuid(as_uuid=True), primary_key=True),
        Column("cutoff_deposit", Numeric(19, 4), nullable=False),
    )
    metadata.create_all(db_session.get_bind())
    db_session.execute(
        cutoff_balances.insert().values(
            subscriber_id=subscriber.id,
            cutoff_deposit=Decimal("125.00"),
        )
    )
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("125.00"),
            currency="NGN",
            is_active=False,
            effective_date=PAYMENT_ACTIVITY_AT - timedelta(days=1),
        )
    )
    db_session.commit()

    finding = d2_unbacked_deactivated_credits(db_session)

    assert finding.count == 0
    assert "coverage_source=source_cutoff_deposit" in finding.note


def test_d12_uses_canonical_batch_threshold_owner():
    source = d12_enforcement_mismatch.__code__.co_names

    assert "resolve_prepaid_thresholds" in source
    assert "_prepaid_threshold" not in source


def test_d12_query_count_does_not_scale_with_prepaid_accounts(db_session):
    offer = CatalogOffer(
        name="Audit query budget",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.prepaid,
    )
    db_session.add(offer)
    db_session.flush()
    accounts = []
    for index in range(20):
        account = Subscriber(
            first_name="Audit",
            last_name=str(index),
            email=f"audit-query-{index}@example.invalid",
            billing_mode=BillingMode.prepaid,
            min_balance=Decimal("1000.00"),
            is_active=True,
        )
        db_session.add(account)
        db_session.flush()
        db_session.add(
            Subscription(
                subscriber_id=account.id,
                offer_id=offer.id,
                status=SubscriptionStatus.active,
                billing_mode=BillingMode.prepaid,
                unit_price=Decimal("17500.00"),
            )
        )
        accounts.append(account)
    db_session.commit()

    statements = 0

    def count_statement(*_args):
        nonlocal statements
        statements += 1

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", count_statement)
    try:
        d12_enforcement_mismatch(db_session, limit=0, batch_size=5)
    finally:
        event.remove(bind, "before_cursor_execute", count_statement)

    assert statements < 25, (
        f"D12 issued {statements} statements for {len(accounts)} accounts; "
        "the threshold or balance derivation is no longer batched"
    )


def test_reconstruction_normalizes_splynx_zero_dates():
    assert normalize_splynx_date("0000-00-00") is None
    assert normalize_splynx_date("2026-06-15") == date(2026, 6, 15)


def test_reconstruction_preserves_only_balance_affecting_entry_types():
    assert normalize_splynx_entry_type("CREDIT") == "credit"
    assert normalize_splynx_entry_type("debit") == "debit"
    assert normalize_splynx_entry_type("") == "other"


def test_postgresql_primary_is_refused_by_default():
    db = MagicMock()
    db.get_bind.return_value = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql")
    )
    db.scalar.return_value = False

    with pytest.raises(RuntimeError, match="Refusing to run"):
        _configure_read_only_session(
            db, statement_timeout_ms=10000, allow_primary=False
        )

    assert db.execute.call_count == 2


def test_postgresql_replica_is_allowed():
    db = MagicMock()
    db.get_bind.return_value = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql")
    )
    db.scalar.return_value = True

    _configure_read_only_session(db, statement_timeout_ms=10000, allow_primary=False)

    assert db.execute.call_count == 2
