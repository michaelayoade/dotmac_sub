"""Native account deposits require the incumbent command and actual evidence."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.billing import LedgerEntry, Payment
from app.services import owner_commands
from app.services.billing import starter_manual_settlement as owner


def _command(reference="BANK-RECEIPT-1"):
    return owner.ManualStarterAccountDeposit(
        uuid4(), Decimal("102500"), "NGN", reference, "native-deposit-test"
    )


def test_native_deposit_refuses_outside_existing_manual_payment_command(db_session):
    with pytest.raises(owner.StarterManualSettlementError) as caught:
        owner.record_manual_starter_account_deposit(db_session, command=_command())
    assert caught.value.code.endswith("owner_required")
    assert db_session.scalar(select(func.count()).select_from(Payment)) == 0
    assert db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 0


def test_native_deposit_never_invents_a_bank_receipt_reference(db_session, monkeypatch):
    monkeypatch.setattr(
        owner_commands, "owner_command_active", lambda db, *, owner: True
    )
    with pytest.raises(owner.StarterManualSettlementError) as caught:
        owner.record_manual_starter_account_deposit(db_session, command=_command(" "))
    assert caught.value.code.endswith("evidence_required")
    assert db_session.scalar(select(func.count()).select_from(Payment)) == 0
    assert db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 0


def test_unconfigured_withholding_feature_keeps_existing_cash_lane(
    db_session, subscriber, monkeypatch
):
    from app.models.customer_tax_policy import CustomerTaxPolicy
    from app.services import settings_spec
    from app.services.billing import starter_policies

    db_session.add(
        CustomerTaxPolicy(
            account_id=subscriber.id, withholding_tax_enabled=True, version=1
        )
    )
    db_session.flush()
    monkeypatch.setattr(
        starter_policies, "starter_ledger_live_enabled", lambda db: True
    )
    resolve = settings_spec.resolve_value

    def unconfigured(db, domain, key):
        if key in {
            "starter_billing_withholding_tax_jurisdiction_id",
            "starter_billing_withholding_tax_code",
        }:
            return None
        return resolve(db, domain, key)

    monkeypatch.setattr(settings_spec, "resolve_value", unconfigured)
    assert not owner.native_account_deposit_enabled(
        db_session, account_id=subscriber.id
    )
