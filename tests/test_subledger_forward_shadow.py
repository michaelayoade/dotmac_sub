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

    with _pytest.raises(RuntimeError, match="posting unavailable"):
        _settle(
            db_session,
            intent_id=intent.id,
            transaction=_transaction(intent, external_id="fwd-shadow-atomic-1"),
        )
    db_session.rollback()
    assert db_session.query(Payment).count() == before_payments
    assert _groups(db_session, "financial.account_credit_deposits") == []
    db_session.refresh(intent)
    # The intent itself rolled back: no settlement linkage survived.
    assert "settlement_payment_id" not in (intent.metadata_ or {})
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

    with _pytest.raises(RuntimeError, match="application unavailable"):
        _settle(
            db_session,
            intent_id=intent.id,
            transaction=_transaction(intent, external_id="fwd-shadow-atomic-2"),
        )
    db_session.rollback()
    assert _groups(db_session, "financial.account_credit_deposits") == []
    db_session.refresh(intent)
    assert "settlement_payment_id" not in (intent.metadata_ or {})


def _host_command(db, operation, key):
    from uuid import uuid4

    from app.services.owner_commands import (
        CommandContext,
        OwnerCommandDefinition,
        execute_owner_command,
    )

    command_id = uuid4()
    return execute_owner_command(
        db,
        definition=OwnerCommandDefinition(
            owner="billing.contracts",
            concern="versioned billing contract terms",
            name="record_billing_contract_version",
        ),
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="user:pytest",
            scope="forward-shadow:test",
            reason="pytest forward shadow webhook seam",
            idempotency_key=key,
        ),
        operation=operation,
    )


def _provider_payment(db, subscriber, amount="100.00"):
    from uuid import uuid4

    from app.models.billing import PaymentProvider, PaymentProviderType
    from tests.test_payment_refund_evidence import _payment

    provider = PaymentProvider(
        name=f"Provider {uuid4().hex}", provider_type=PaymentProviderType.custom
    )
    db.add(provider)
    db.commit()
    return provider, _payment(db, subscriber, provider=provider)


def test_provider_refund_stages_one_group_at_the_deciding_owner_root(
    db_session, subscriber
):
    from decimal import Decimal as D
    from uuid import uuid4

    from app.models.billing import PaymentRefund
    from app.schemas.billing import PaymentProviderEventIngest
    from app.services import billing as billing_service
    from app.services.owner_commands import CommandContext as _Ctx
    from app.services.payment_provider_events import WEBHOOK_PARTICIPANT_SCOPE
    from tests.payment_provider_event_helpers import provider_event_command

    def stage_verified_provider_event(db, payload):
        return billing_service.payment_provider_events.stage_verified_webhook_event(
            db,
            provider_event_command(payload),
            context=_Ctx.system(
                actor="pytest:payment-provider-event",
                scope=WEBHOOK_PARTICIPANT_SCOPE,
                reason="forward-shadow webhook seam test",
                idempotency_key=payload.idempotency_key or payload.external_id,
            ),
        )

    provider, payment = _provider_payment(db_session, subscriber)

    db_session.commit()
    _host_command(
        db_session,
        lambda: stage_verified_provider_event(
            db_session,
            PaymentProviderEventIngest(
                provider_id=provider.id,
                payment_id=payment.id,
                event_type="charge.refunded",
                amount=D("40.00"),
                currency="NGN",
                idempotency_key=f"provider-refund-{uuid4().hex}",
            ),
        ),
        key=f"fwd-refund-{uuid4().hex}",
    )

    refund = db_session.query(PaymentRefund).one()
    groups = _groups(db_session, "financial.payment_provider_events")
    assert len(groups) == 1
    group = groups[0]
    assert group.command_kind is PostingCommandKind.refund
    assert group.source_kind == "payment_refund"
    assert group.source_id == refund.id
    assert group.reverses_group_id is None
    effects = (
        db_session.query(CustomerPositionEffect)
        .filter(CustomerPositionEffect.group_id == group.id)
        .all()
    )
    assert {e.effect for e in effects} >= {PositionEffectKind.credit_refunded}


def test_provider_reversal_links_the_original_settlement_group(db_session, subscriber):
    from decimal import Decimal as D
    from uuid import uuid4

    from app.schemas.billing import PaymentProviderEventIngest
    from app.services import billing as billing_service
    from app.services.owner_commands import CommandContext as _Ctx
    from app.services.payment_provider_events import WEBHOOK_PARTICIPANT_SCOPE
    from tests.payment_provider_event_helpers import provider_event_command

    def stage_verified_provider_event(db, payload):
        return billing_service.payment_provider_events.stage_verified_webhook_event(
            db,
            provider_event_command(payload),
            context=_Ctx.system(
                actor="pytest:payment-provider-event",
                scope=WEBHOOK_PARTICIPANT_SCOPE,
                reason="forward-shadow webhook seam test",
                idempotency_key=payload.idempotency_key or payload.external_id,
            ),
        )

    # Original money transition staged by F1 (deposit settlement group).
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="10000.00")
    _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="fwd-shadow-rev-orig"),
    )
    db_session.commit()
    original = _groups(db_session, "financial.account_credit_deposits")[0]
    payment_id = original.source_id

    db_session.commit()
    _host_command(
        db_session,
        lambda: stage_verified_provider_event(
            db_session,
            PaymentProviderEventIngest(
                provider_id=provider.id,
                payment_id=payment_id,
                event_type="charge.reversed",
                amount=D("10000.00"),
                currency="NGN",
                idempotency_key=f"provider-reversal-{uuid4().hex}",
            ),
        ),
        key=f"fwd-reversal-{uuid4().hex}",
    )

    reversal_groups = [
        g
        for g in _groups(db_session, "financial.payment_provider_events")
        if g.command_kind is PostingCommandKind.reversal
    ]
    assert len(reversal_groups) == 1
    assert reversal_groups[0].reverses_group_id == original.id
    assert reversal_groups[0].source_kind == "payment_reversal"


def test_unwrapped_refund_path_stages_nothing(db_session, subscriber):
    from decimal import Decimal as D
    from uuid import uuid4

    from app.schemas.billing import PaymentProviderEventIngest
    from tests.payment_provider_event_helpers import stage_verified_provider_event

    provider, payment = _provider_payment(db_session, subscriber)

    # Legacy root: no owner command. The money transition proceeds; the
    # posting gap belongs to the verifier, never to invented history.
    stage_verified_provider_event(
        db_session,
        PaymentProviderEventIngest(
            provider_id=provider.id,
            payment_id=payment.id,
            event_type="charge.refunded",
            amount=D("40.00"),
            currency="NGN",
            idempotency_key=f"provider-refund-{uuid4().hex}",
        ),
    )

    assert _groups(db_session, "financial.payment_provider_events") == []
