from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from fastapi import HTTPException

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
    currency: str = "NGN",
):
    entry = LedgerEntry(
        account_id=account_id,
        entry_type=entry_type,
        source=source,
        amount=Decimal(amount),
        currency=currency,
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


def test_build_ledger_entries_data_groups_totals_by_currency(db_session, subscriber):
    _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="120.00",
        currency="NGN",
    )
    _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="20.00",
        currency="USD",
    )
    _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.debit,
        amount="30.00",
        currency="NGN",
    )

    state = build_ledger_entries_data(
        db_session,
        customer_ref=str(subscriber.id),
        entry_type=None,
    )

    assert state["ledger_totals"]["credit_amounts"] == {
        "NGN": Decimal("120.00"),
        "USD": Decimal("20.00"),
    }
    assert state["ledger_totals"]["debit_amounts"] == {"NGN": Decimal("30.00")}
    assert state["ledger_totals"]["credit_display"] == "NGN 120.00, USD 20.00"
    assert state["ledger_totals"]["debit_display"] == "NGN 30.00"
    assert state["ledger_totals"]["net_display"] == "NGN 90.00, USD 20.00"


def test_build_ledger_entries_data_applies_custom_date_range(db_session, subscriber):
    old_entry = _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="10.00",
    )
    old_entry.created_at = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    db_session.add(old_entry)

    in_range_entry = _create_ledger_entry(
        db_session,
        account_id=subscriber.id,
        entry_type=LedgerEntryType.debit,
        amount="5.00",
    )
    in_range_entry.created_at = datetime(2026, 2, 28, 18, 30, tzinfo=UTC)
    db_session.add(in_range_entry)
    db_session.commit()

    state = build_ledger_entries_data(
        db_session,
        customer_ref=str(subscriber.id),
        entry_type=None,
        start_date="2026-02-01",
        end_date="2026-02-28",
    )

    assert len(state["entries"]) == 1
    assert state["entries"][0].id == in_range_entry.id
    assert state["ledger_totals"]["credit_count"] == 0
    assert state["ledger_totals"]["debit_count"] == 1
    assert state["start_date"] == "2026-02-01"
    assert state["end_date"] == "2026-02-28"


def test_build_ledger_entries_data_rejects_reversed_date_range(
    db_session, subscriber
):
    with pytest.raises(HTTPException) as exc_info:
        build_ledger_entries_data(
            db_session,
            customer_ref=str(subscriber.id),
            entry_type=None,
            start_date="2026-03-01",
            end_date="2026-02-28",
        )

    assert exc_info.value.status_code == 400


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

    assert (
        "entry_id,customer_name,entry_type,source,debit_amount,credit_amount,currency,description,date"
        in csv_text
    )
    assert subscriber.name in csv_text
    assert str(subscriber.id) not in csv_text
    assert ",debit,invoice,15.00,,NGN," in csv_text
    assert ",credit,payment,,22.00,NGN," in csv_text
