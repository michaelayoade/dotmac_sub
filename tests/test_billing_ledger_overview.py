from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.billing import LedgerEntry, LedgerEntryType, LedgerSource
from app.models.subscriber import Reseller, Subscriber
from app.services.web_billing_ledger import build_ledger_entries_data, render_ledger_csv


def _create_ledger_entry(
    db_session,
    *,
    account_id,
    entry_type: LedgerEntryType,
    amount: str,
    source: LedgerSource = LedgerSource.other,
):
    entry = LedgerEntry(
        account_id=account_id,
        entry_type=entry_type,
        source=source,
        amount=Decimal(amount),
        currency="NGN",
        memo="test",
    )
    db_session.add(entry)
    db_session.commit()
    db_session.refresh(entry)
    return entry


def test_build_ledger_entries_data_includes_totals(db_session, subscriber):
    _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="120.00",
    )
    _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.debit,
        amount="30.00",
    )

    state = build_ledger_entries_data(
        db_session,
        customer_ref=str(subscriber.id),
        entry_type=None,
    )

    assert state["ledger_totals"]["credit_count"] == 1
    assert state["ledger_totals"]["debit_count"] == 1
    assert state["ledger_totals"]["credit_total"] == pytest.approx(120.0)
    assert state["ledger_totals"]["debit_total"] == pytest.approx(30.0)
    assert state["ledger_totals"]["net_total"] == pytest.approx(90.0)


def test_build_ledger_entries_data_applies_date_range(db_session, subscriber):
    old_entry = _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="10.00",
    )
    old_entry.created_at = datetime.now(UTC) - timedelta(days=40)
    db_session.add(old_entry)
    db_session.commit()

    _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.debit,
        amount="5.00",
    )

    state = build_ledger_entries_data(
        db_session,
        customer_ref=str(subscriber.id),
        entry_type=None,
        date_range="month",
    )

    assert len(state["entries"]) == 1
    assert state["ledger_totals"]["credit_count"] == 0
    assert state["ledger_totals"]["debit_count"] == 1


def test_build_ledger_entries_data_filters_by_category(db_session, subscriber):
    _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="90.00",
        source=LedgerSource.payment,
    )
    _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.debit,
        amount="25.00",
        source=LedgerSource.invoice,
    )

    state = build_ledger_entries_data(
        db_session,
        customer_ref=str(subscriber.id),
        entry_type=None,
        category="payment",
    )

    assert len(state["entries"]) == 1
    assert state["entries"][0].source == LedgerSource.payment


def test_build_ledger_entries_data_filters_by_partner(db_session):
    reseller_a = Reseller(name="Partner A")
    reseller_b = Reseller(name="Partner B")
    db_session.add_all([reseller_a, reseller_b])
    db_session.commit()

    account_a = Subscriber(
        first_name="Partner",
        last_name="A",
        email="partner-a-ledger@example.com",
        reseller_id=reseller_a.id,
    )
    account_b = Subscriber(
        first_name="Partner",
        last_name="B",
        email="partner-b-ledger@example.com",
        reseller_id=reseller_b.id,
    )
    db_session.add_all([account_a, account_b])
    db_session.commit()

    _create_ledger_entry(
        db_session,
        account_id=account_a.id,
        entry_type=LedgerEntryType.credit,
        amount="70.00",
        source=LedgerSource.payment,
    )
    _create_ledger_entry(
        db_session,
        account_id=account_b.id,
        entry_type=LedgerEntryType.credit,
        amount="40.00",
        source=LedgerSource.payment,
    )

    state = build_ledger_entries_data(
        db_session,
        customer_ref=None,
        entry_type=None,
        partner_id=str(reseller_a.id),
    )

    assert len(state["entries"]) == 1
    assert state["entries"][0].account_id == account_a.id
    assert state["selected_partner_id"] == str(reseller_a.id)


def test_render_ledger_csv_contains_split_debit_and_credit(db_session, subscriber):
    debit_entry = _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.debit,
        amount="15.00",
        source=LedgerSource.invoice,
    )
    credit_entry = _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="22.00",
        source=LedgerSource.payment,
    )

    csv_text = render_ledger_csv([debit_entry, credit_entry])

    assert "entry_id,account_id,entry_type,source,debit_amount,credit_amount,currency,description,created_at" in csv_text
    assert ",debit,invoice,15.00,,NGN," in csv_text
    assert ",credit,payment,,22.00,NGN," in csv_text
