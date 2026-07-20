"""C1-sub backstop: CRM sales payments can't be double-recorded.

CRM payments are stored with ``external_id = 'crm:<ref>'`` and no provider, so
they fall outside ``uq_payments_active_external_id`` (provider-gated). The new
partial index ``uq_payments_active_crm_external_id`` closes the double-record
window while leaving non-CRM payments (splynx / provider / manual) unconstrained.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.billing import Payment, PaymentStatus


def _payment(db_session, *, external_id, active=True):
    pmt = Payment(
        id=uuid.uuid4(),
        amount=Decimal("100.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        external_id=external_id,
        is_active=active,
    )
    db_session.add(pmt)
    return pmt


def test_duplicate_crm_external_id_is_rejected(db_session):
    _payment(db_session, external_id="crm:install-1")
    db_session.flush()

    _payment(db_session, external_id="crm:install-1")
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_non_crm_external_ids_are_not_constrained_by_this_index(db_session):
    # Two non-CRM payments can share an external_id (e.g. provider webhooks);
    # this index only covers the crm: namespace.
    _payment(db_session, external_id="provider-ref-9")
    _payment(db_session, external_id="provider-ref-9")
    db_session.flush()

    assert (
        db_session.query(Payment)
        .filter(Payment.external_id == "provider-ref-9")
        .count()
        == 2
    )


def test_inactive_crm_payment_does_not_block_a_new_one(db_session):
    # A soft-deleted (is_active=False) CRM payment shouldn't block re-recording:
    # the index is partial on is_active.
    _payment(db_session, external_id="crm:install-2", active=False)
    _payment(db_session, external_id="crm:install-2", active=True)
    db_session.flush()

    active = (
        db_session.query(Payment)
        .filter(Payment.external_id == "crm:install-2", Payment.is_active.is_(True))
        .count()
    )
    assert active == 1
