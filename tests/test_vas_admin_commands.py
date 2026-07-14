from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.models.billing import TopupIntent
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscriber import Subscriber
from app.models.vas import (
    VasEntryCategory,
    VasRateCard,
    VasRefundRequest,
    VasRefundStatus,
    VasService,
    VasTransaction,
    VasTransactionStatus,
)
from app.services import vas_admin_commands, vas_purchases, vas_refunds, vas_wallet
from app.services.payment_gateway_adapter import (
    PaymentGatewayRefund,
    PaymentGatewayRefundState,
)


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="VAS",
        last_name="Admin",
        email=f"vas-admin-{uuid.uuid4().hex}@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)
    return subscriber


def _service(db_session, *, enabled: bool = False) -> VasService:
    service = VasService(
        category="airtime",
        service_id=f"admin-{uuid.uuid4().hex[:10]}",
        name="Admin Airtime",
        is_enabled=enabled,
        min_amount=Decimal("50.00"),
        max_amount=Decimal("50000.00"),
    )
    db_session.add(service)
    db_session.commit()
    db_session.refresh(service)
    return service


def _review_transaction(db_session, *, amount: str = "100.00"):
    subscriber = _subscriber(db_session)
    service = _service(db_session)
    wallet = vas_wallet.get_or_create_wallet(db_session, str(subscriber.id))
    vas_wallet.credit_wallet(
        db_session,
        wallet,
        amount=Decimal("500.00"),
        category=VasEntryCategory.topup,
        reference=f"review-fund-{uuid.uuid4().hex}",
    )
    vas_wallet.debit_wallet(
        db_session,
        wallet,
        amount=Decimal(amount),
        category=VasEntryCategory.purchase,
        reference=f"review-purchase-{uuid.uuid4().hex}",
    )
    txn = VasTransaction(
        wallet_id=wallet.id,
        subscriber_id=subscriber.id,
        service_pk=service.id,
        identifier="08031234567",
        amount=Decimal(amount),
        request_id=f"admin-review-{uuid.uuid4().hex}",
        status=VasTransactionStatus.review,
    )
    db_session.add(txn)
    db_session.commit()
    db_session.refresh(txn)
    return txn, wallet


def _refundable_topup(db_session, *, amount: str = "100.00"):
    subscriber = _subscriber(db_session)
    wallet = vas_wallet.get_or_create_wallet(db_session, str(subscriber.id))
    reference = f"paystack-{uuid.uuid4().hex}"
    topup = vas_wallet.credit_wallet(
        db_session,
        wallet,
        amount=Decimal(amount),
        category=VasEntryCategory.topup,
        reference=reference,
        memo="Wallet top-up via paystack",
    )
    db_session.add(
        TopupIntent(
            account_id=subscriber.id,
            reference=reference,
            provider_type="paystack",
            currency="NGN",
            requested_amount=Decimal(amount),
            actual_amount=Decimal(amount),
            external_id=f"tx-{uuid.uuid4().hex}",
            status="completed",
            metadata_={"payment_flow": "vas_wallet_topup"},
        )
    )
    db_session.commit()
    return topup, wallet


def _gateway_refund(
    *,
    state: PaymentGatewayRefundState,
    status: str,
    amount: str = "100.00",
    external_id: str = "refund-1",
) -> PaymentGatewayRefund:
    return PaymentGatewayRefund(
        provider_type="paystack",
        external_id=external_id,
        transaction_id="provider-transaction-1",
        amount=Decimal(amount),
        status=status,
        state=state,
        raw={"id": external_id, "status": status, "amount": int(Decimal(amount) * 100)},
    )


def test_toggle_service_commits_through_command_owner(db_session):
    service = _service(db_session)

    enabled = vas_admin_commands.toggle_service(
        db_session,
        service_pk=str(service.id),
    )

    db_session.refresh(service)
    assert enabled is True
    assert service.is_enabled is True


def test_toggle_service_rejects_unknown_target(db_session):
    with pytest.raises(
        vas_admin_commands.VasAdminResourceNotFound,
        match="Service not found",
    ):
        vas_admin_commands.toggle_service(
            db_session,
            service_pk=str(uuid.uuid4()),
        )


def test_category_and_rate_card_inputs_are_owned_by_command_boundary(db_session):
    vas_admin_commands.set_categories(
        db_session,
        enabled_categories=" Airtime, DATA, ,electricity-bill ",
    )
    vas_admin_commands.add_rate_card(
        db_session,
        category=" AIRTIME ",
        party_type="owner",
        rate_pct="2.7500",
        effective_from="2026-07-13T09:30:00",
        memo=" July rate ",
    )

    setting = (
        db_session.query(DomainSetting)
        .filter_by(domain=SettingDomain.vas, key="enabled_categories")
        .one()
    )
    card = db_session.query(VasRateCard).one()
    assert setting.value_text == "airtime,data,electricity-bill"
    assert card.category == "airtime"
    assert card.rate_pct == Decimal("2.7500")
    assert card.memo == "July rate"

    with pytest.raises(
        vas_admin_commands.VasAdminCommandError,
        match="Invalid rate card values",
    ):
        vas_admin_commands.add_rate_card(
            db_session,
            category="airtime",
            party_type="unknown",
            rate_pct="not-a-rate",
        )


def test_review_refund_restores_wallet_and_closes_transaction(db_session):
    txn, wallet = _review_transaction(db_session)
    assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("400.00")

    vas_admin_commands.resolve_review_refund(db_session, txn_id=str(txn.id))

    db_session.refresh(txn)
    assert txn.status == VasTransactionStatus.refunded
    assert txn.refunded_at is not None
    assert txn.error == "Manually resolved: refunded"
    assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("500.00")


def test_review_delivery_records_optional_token_and_manual_outcome(db_session):
    txn, _wallet = _review_transaction(db_session)

    vas_admin_commands.resolve_review_delivered(
        db_session,
        txn_id=str(txn.id),
        token=" 1234-5678 ",
    )

    db_session.refresh(txn)
    assert txn.status == VasTransactionStatus.delivered
    assert txn.provider_status == "Manually resolved: delivered"
    assert vas_purchases.transaction_token(txn) == "1234-5678"


def test_refund_to_source_commits_request_and_debit_before_gateway_and_blocks_replay(
    db_session,
    monkeypatch,
):
    topup, wallet = _refundable_topup(db_session)
    calls: list[str] = []

    def refund(
        _db,
        *,
        provider_type,
        reference,
        transaction_id,
        amount,
        request_key,
    ):
        request = db_session.get(VasRefundRequest, request_key)
        assert request is not None
        assert request.status == VasRefundStatus.submitting
        assert request.wallet_debit_entry_id is not None
        assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("0.00")
        assert provider_type == "paystack"
        assert reference == topup.reference
        assert transaction_id
        assert amount == Decimal("100.00")
        calls.append(request_key)
        return _gateway_refund(
            state=PaymentGatewayRefundState.succeeded,
            status="processed",
        )

    monkeypatch.setattr(
        vas_refunds.payment_gateway_adapter,
        "refund",
        refund,
    )

    outcome = vas_admin_commands.refund_to_source(
        db_session,
        entry_id=str(topup.id),
    )

    assert outcome.provider == "paystack"
    assert outcome.reference == topup.reference
    assert outcome.amount == Decimal("100.00")
    assert outcome.status == VasRefundStatus.succeeded
    assert len(calls) == 1
    assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("0.00")

    replay = vas_admin_commands.refund_to_source(db_session, entry_id=str(topup.id))

    assert replay.request_id == outcome.request_id
    assert replay.already_requested is True
    assert len(calls) == 1


def test_refund_to_source_response_loss_is_repaired_without_second_submission(
    db_session,
    monkeypatch,
):
    topup, wallet = _refundable_topup(db_session, amount="125.00")
    submissions = 0

    def fail_refund(*args, **kwargs):
        nonlocal submissions
        submissions += 1
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        vas_refunds.payment_gateway_adapter,
        "refund",
        fail_refund,
    )

    outcome = vas_admin_commands.refund_to_source(
        db_session,
        entry_id=str(topup.id),
    )

    assert outcome.status == VasRefundStatus.submitting
    assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("0.00")
    assert submissions == 1

    monkeypatch.setattr(
        vas_refunds.payment_gateway_adapter,
        "find_refund",
        lambda *_args, **_kwargs: _gateway_refund(
            state=PaymentGatewayRefundState.succeeded,
            status="processed",
            amount="125.00",
        ),
    )
    stats = vas_refunds.reconcile_refund_requests(db_session)

    request = db_session.get(VasRefundRequest, outcome.request_id)
    assert request.status == VasRefundStatus.succeeded
    assert stats["succeeded"] == 1
    assert submissions == 1
    assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("0.00")


def test_terminal_gateway_failure_restores_reserved_wallet_once(
    db_session,
    monkeypatch,
):
    topup, wallet = _refundable_topup(db_session, amount="125.00")
    monkeypatch.setattr(
        vas_refunds.payment_gateway_adapter,
        "refund",
        lambda *_args, **_kwargs: _gateway_refund(
            state=PaymentGatewayRefundState.failed,
            status="failed",
            amount="125.00",
        ),
    )

    with pytest.raises(
        vas_admin_commands.VasAdminCommandError,
        match="wallet reservation was restored",
    ):
        vas_admin_commands.refund_to_source(db_session, entry_id=str(topup.id))

    request = db_session.query(VasRefundRequest).one()
    reversal_id = request.wallet_reversal_entry_id
    assert request.status == VasRefundStatus.failed
    assert reversal_id is not None
    assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("125.00")

    with pytest.raises(vas_admin_commands.VasAdminCommandError):
        vas_admin_commands.refund_to_source(db_session, entry_id=str(topup.id))

    db_session.refresh(request)
    assert request.wallet_reversal_entry_id == reversal_id
    assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("125.00")


def test_ambiguous_submission_is_observed_only_and_escalates_without_reposting(
    db_session,
    monkeypatch,
):
    topup, wallet = _refundable_topup(db_session)
    submissions = 0
    observations = 0

    def lose_response(*_args, **_kwargs):
        nonlocal submissions
        submissions += 1
        raise TimeoutError("response lost")

    def not_visible(*_args, **_kwargs):
        nonlocal observations
        observations += 1
        return None

    monkeypatch.setattr(
        vas_refunds.payment_gateway_adapter,
        "refund",
        lose_response,
    )
    monkeypatch.setattr(
        vas_refunds.payment_gateway_adapter,
        "find_refund",
        not_visible,
    )

    outcome = vas_admin_commands.refund_to_source(
        db_session,
        entry_id=str(topup.id),
    )
    for _ in range(3):
        vas_refunds.reconcile_refund_requests(db_session)

    request = db_session.get(VasRefundRequest, outcome.request_id)
    assert submissions == 1
    assert observations == 3
    assert request.status == VasRefundStatus.needs_attention
    assert request.reconcile_attempts == 3
    assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("0.00")


def test_reconciler_resumes_prepared_request_after_process_exit(
    db_session,
    monkeypatch,
):
    topup, wallet = _refundable_topup(db_session)
    request, existed = vas_refunds._prepare_request(
        db_session,
        entry_id=str(topup.id),
    )
    assert existed is False
    assert request.status == VasRefundStatus.prepared
    assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("0.00")

    monkeypatch.setattr(
        vas_refunds.payment_gateway_adapter,
        "refund",
        lambda *_args, **_kwargs: _gateway_refund(
            state=PaymentGatewayRefundState.pending,
            status="pending",
        ),
    )

    stats = vas_refunds.reconcile_refund_requests(db_session)

    db_session.refresh(request)
    assert stats["accepted"] == 1
    assert request.status == VasRefundStatus.accepted
    assert request.provider_refund_id == "refund-1"


def test_vas_refund_reconciler_is_registered_on_billing_queue():
    from app.celery_app import celery_app

    task_name = "app.tasks.vas.reconcile_refund_requests"
    assert task_name in celery_app.tasks
    assert celery_app.conf.task_routes[task_name] == {"queue": "billing"}
