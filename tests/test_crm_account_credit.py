"""CRM/referral rewards use the evidence-backed account-credit owner."""

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.api.crm import create_crm_credit
from app.models.audit import AuditEvent
from app.models.billing import LedgerEntry
from app.models.subscriber import Subscriber
from app.services import crm_api


def _subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="Referral",
        last_name="Credit",
        email=f"referral-credit-{uuid.uuid4().hex}@example.com",
        is_active=True,
    )
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


def test_account_credit_is_idempotent_and_has_exact_evidence(db_session):
    subscriber = _subscriber(db_session)
    first = crm_api.create_account_credit(
        db_session,
        subscriber_id=str(subscriber.id),
        amount=Decimal("2500.00"),
        reason="Referral reward",
        external_ref="referral:durable-1",
    )
    replay = crm_api.create_account_credit(
        db_session,
        subscriber_id=str(subscriber.id),
        amount=Decimal("2500.00"),
        reason="Referral reward",
        external_ref="referral:durable-1",
    )

    assert replay.id == first.id
    assert first.issue_preview_fingerprint
    assert first.funding_ledger_entry_id
    funding = db_session.get(LedgerEntry, first.funding_ledger_entry_id)
    assert funding is not None
    assert funding.amount == Decimal("2500.00")
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_type == "credit_note")
        .filter(AuditEvent.entity_id == str(first.id))
        .filter(AuditEvent.action == "issue")
        .one()
    )
    assert audit.metadata_["funding_ledger_entry_id"] == str(funding.id)


def test_account_credit_rejects_reuse_with_different_money(db_session):
    subscriber = _subscriber(db_session)
    crm_api.create_account_credit(
        db_session,
        subscriber_id=str(subscriber.id),
        amount=Decimal("2500.00"),
        external_ref="referral:durable-2",
    )

    with pytest.raises(HTTPException) as exc:
        crm_api.create_account_credit(
            db_session,
            subscriber_id=str(subscriber.id),
            amount=Decimal("2600.00"),
            external_ref="referral:durable-2",
        )
    assert exc.value.status_code == 409


def test_credit_endpoint_requires_idempotency_key(db_session):
    subscriber = _subscriber(db_session)
    with pytest.raises(HTTPException) as exc:
        create_crm_credit(
            payload={"subscriber_id": str(subscriber.id), "amount": "2500"},
            db=db_session,
        )
    assert exc.value.status_code == 400
    assert "external_ref" in str(exc.value.detail)
