"""CRM installation-invoice creation is idempotent on external_ref (no double-create).

Backed by the crm_external_ref column + uq_invoices_active_crm_external_ref partial
unique index (migration 212), mirroring the CRM-payment idempotency (C1).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.models.billing import Invoice
from app.models.subscriber import Subscriber
from app.services import crm_api


@pytest.fixture(autouse=True)
def _ensure_sequence_table(db_session):
    # Invoice-number generation needs document_sequences, whose model isn't
    # imported before the SQLite test schema is built.
    from app.models.sequence import DocumentSequence

    DocumentSequence.__table__.create(db_session.get_bind(), checkfirst=True)


def _subscriber(db_session) -> Subscriber:
    sub = Subscriber(
        first_name="In",
        last_name="Stall",
        email=f"i-{uuid.uuid4().hex[:8]}@x.io",
        subscriber_number=f"S-{uuid.uuid4().hex[:6]}",
    )
    db_session.add(sub)
    db_session.commit()
    return sub


def _invoice(db_session, sub, ref):
    return crm_api.create_installation_invoice(
        db_session,
        subscriber_id=str(sub.id),
        amount=Decimal("15000"),
        description="Installation",
        external_ref=ref,
    )


def test_installation_invoice_is_idempotent(db_session):
    sub = _subscriber(db_session)
    i1 = _invoice(db_session, sub, "crm-inv-1")
    i2 = _invoice(db_session, sub, "crm-inv-1")

    assert i1.id == i2.id  # same ref → one invoice
    assert i1.crm_external_ref == "crm-inv-1"  # column populated
    assert (
        db_session.query(Invoice)
        .filter(Invoice.crm_external_ref == "crm-inv-1")
        .count()
        == 1
    )


def test_distinct_refs_create_distinct_invoices(db_session):
    sub = _subscriber(db_session)
    _invoice(db_session, sub, "crm-inv-1")
    _invoice(db_session, sub, "crm-inv-2")
    assert db_session.query(Invoice).count() == 2


def test_integrity_error_reraised_when_no_existing(db_session, monkeypatch):
    """If the commit hits a constraint but no matching invoice can be found on
    re-query, the error is not swallowed (only a genuine duplicate returns)."""
    from sqlalchemy.exc import IntegrityError

    sub = _subscriber(db_session)

    def boom():
        raise IntegrityError("some other constraint", {}, Exception())

    monkeypatch.setattr(db_session, "commit", boom)
    monkeypatch.setattr(crm_api, "_find_invoice_by_crm_ref", lambda db, ref: None)

    with pytest.raises(IntegrityError):
        _invoice(db_session, sub, "crm-inv-x")
