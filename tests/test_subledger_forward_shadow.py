"""ADR 0007 Phase 3 forward-shadow: one posting group per money transition.

Slice-1 exit gates: every owner-clean prepaid money transition stages exactly
one idempotent shadow posting group inside the owner's transaction; replays
duplicate nothing; reversals link the original group. No read authority moves.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.customer_subledger import (
    CustomerPositionEffect,
    CustomerPostingGroup,
    PositionEffectKind,
    PostingCommandKind,
)
from tests.test_account_credit_deposits import (
    _intent,
    _provider,
    _settle,
    _transaction,
)


def _groups(db, producer: str) -> list[CustomerPostingGroup]:
    return (
        db.query(CustomerPostingGroup)
        .filter(CustomerPostingGroup.producer_owner == producer)
        .order_by(CustomerPostingGroup.recorded_at)
        .all()
    )


def test_deposit_settlement_stages_one_shadow_group(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="10000.00")
    result = _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="fwd-shadow-dep-1"),
    )

    groups = _groups(db_session, "financial.account_credit_deposits")
    assert len(groups) == 1
    group = groups[0]
    assert group.command_kind is PostingCommandKind.customer_credit_deposit
    assert group.authority.value == "shadow"
    assert group.source_kind == "payment"
    assert group.source_id == result.payment.id
    effects = (
        db_session.query(CustomerPositionEffect)
        .filter(CustomerPositionEffect.group_id == group.id)
        .all()
    )
    assert len(effects) == 1
    assert effects[0].effect is PositionEffectKind.customer_credit_created
    assert Decimal(str(effects[0].amount)) == Decimal("10000.00")
    assert effects[0].payment_id == result.payment.id


def test_deposit_replay_duplicates_no_group(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="10000.00")
    tx = _transaction(intent, external_id="fwd-shadow-dep-2")
    first = _settle(db_session, intent_id=intent.id, transaction=tx)
    db_session.commit()
    replay = _settle(db_session, intent_id=intent.id, transaction=tx)

    assert replay.payment.id == first.payment.id
    groups = _groups(db_session, "financial.account_credit_deposits")
    assert len(groups) == 1


def test_posting_failure_rolls_back_the_whole_deposit(
    db_session, subscriber, monkeypatch
):
    # Atomicity gate: if staging the posting group fails, the deposit's
    # owner command aborts and NO payment or group survives.
    import app.services.account_credit_deposits as deposits_module
    from app.models.billing import Payment

    def _boom(*args, **kwargs):
        raise RuntimeError("posting unavailable")

    monkeypatch.setattr(
        "app.services.billing.customer_subledger.stage_posting_group", _boom
    )
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="10000.00")
    before_payments = db_session.query(Payment).count()

    import pytest as _pytest

    with _pytest.raises(Exception):
        _settle(
            db_session,
            intent_id=intent.id,
            transaction=_transaction(intent, external_id="fwd-shadow-atomic-1"),
        )
    db_session.rollback()
    assert db_session.query(Payment).count() == before_payments
    assert _groups(db_session, "financial.account_credit_deposits") == []
    assert deposits_module is not None


def test_downstream_failure_leaves_no_orphan_posting(
    db_session, subscriber, monkeypatch
):
    # Atomicity gate: a failure AFTER staging (credit application) aborts
    # the same transaction; the staged group must not survive alone.
    def _boom(db, account_id, *args, **kwargs):
        raise RuntimeError("application unavailable")

    monkeypatch.setattr(
        "app.services.account_credit_deposits.AccountCreditApplications.apply",
        _boom,
    )
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="10000.00")

    import pytest as _pytest

    with _pytest.raises(Exception):
        _settle(
            db_session,
            intent_id=intent.id,
            transaction=_transaction(intent, external_id="fwd-shadow-atomic-2"),
        )
    db_session.rollback()
    assert _groups(db_session, "financial.account_credit_deposits") == []
