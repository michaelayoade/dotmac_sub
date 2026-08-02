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
