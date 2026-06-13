"""Splynx transaction mirror: parsing helpers + deposit-reconciliation math."""

from datetime import date
from decimal import Decimal

from sqlalchemy import case, func

from app.models.splynx_transaction import SplynxBillingTransaction as T
from scripts.billing.import_splynx_transactions import _d, _int


def test_date_helper_handles_splynx_quirks():
    assert _d(None) is None
    assert _d("0000-00-00") is None
    assert _d("") is None
    assert _d(date(2026, 6, 3)) == date(2026, 6, 3)
    assert _d("2026-06-03") == date(2026, 6, 3)


def test_int_helper_treats_zero_as_none():
    assert _int(0) is None  # Splynx uses 0 for an absent FK
    assert _int(None) is None
    assert _int("") is None
    assert _int(97970) == 97970
    assert _int("123") == 123


def test_mirror_net_equals_credit_minus_debit(db_session):
    """deposit = Σcredit − Σdebit — the parity invariant the import preserves."""
    rows = [
        ("credit", "34000.00", False),
        ("credit", "3762.00", False),
        ("debit", "6270.83", False),
        ("debit", "999999.99", True),  # deleted -> excluded
    ]
    for i, (etype, amt, deleted) in enumerate(rows):
        db_session.add(
            T(
                splynx_transaction_id=1000 + i,
                splynx_customer_id=25313,
                entry_type=etype,
                amount=Decimal(amt),
                deleted=deleted,
            )
        )
    db_session.commit()

    signed = func.sum(case((T.entry_type == "credit", T.amount), else_=-T.amount))
    net = (
        db_session.query(signed)
        .filter(T.splynx_customer_id == 25313)
        .filter(T.deleted.is_(False))
        .scalar()
    )
    # 34000 + 3762 - 6270.83 = 31491.17 (deleted debit excluded)
    assert Decimal(str(net)) == Decimal("31491.17")
