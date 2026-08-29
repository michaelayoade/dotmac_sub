"""Fail-closed controls for finance-reviewed Paystack recovery.

The supported path is deliberately singular: prove one local terminal intent,
observe the same exact Paystack reference, preview without writes, and only then
confirm the fingerprint through the canonical payment owners.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.billing import (
    Payment,
    PaymentAllocation,
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderEventFinancialEffect,
    PaymentProviderEventSource,
    PaymentProviderEventStatus,
    PaymentProviderType,
    PaymentStatus,
    TopupIntent,
)
from app.models.event_store import EventStore
from app.services import billing as billing_service
from app.services.owner_commands import CommandContext
from app.services.payment_gateway_adapter import (
    PaymentGatewayProviderStatus,
    PaymentGatewayTransaction,
    PaymentGatewayVerificationObservation,
    PaymentGatewayVerificationOutcome,
    PaymentGatewayVerificationReason,
)
from app.services.payment_reconciliation import (
    PAYSTACK_OUTSIDE_WINDOW_RECOVERY_SCOPE,
    ConfirmPaystackOutsideWindowRecoveryCommand,
    PaymentReconciliationError,
    PaystackOutsideWindowRecoveryPreview,
    PreviewPaystackOutsideWindowRecoveryQuery,
    confirm_paystack_outside_window_recovery,
    preview_paystack_outside_window_recovery,
)
from tests.integration_platform_helpers import enable_payment_provider

OBSERVED_AT = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
REFERENCE = "DMAC-PAYSTACK-OUTSIDE-WINDOW-1"
EXTERNAL_ID = "paystack-outside-window-transaction-1"
AUTHORIZED_NET = Decimal("5000.00")
PROVIDER_FEE = Decimal("90.00")
GROSS_AMOUNT = AUTHORIZED_NET + PROVIDER_FEE


@pytest.fixture
def paystack_configuration(db_session, monkeypatch):
    monkeypatch.setenv("PAYSTACK_TEST_SECRET", "sk_test_outside_window")
    monkeypatch.setenv("PAYSTACK_TEST_PUBLIC", "pk_test_outside_window")
    bindings = enable_payment_provider(db_session, "paystack")
    provider = PaymentProvider(
        name=f"Paystack Outside Window {uuid4().hex}",
        provider_type=PaymentProviderType.paystack,
        is_active=True,
    )
    db_session.add(provider)
    db_session.commit()
    return provider, bindings["payments.intent.v1"]


def _intent(
    db,
    subscriber,
    provider: PaymentProvider,
    checkout_binding,
    *,
    reference: str = REFERENCE,
    provider_type: str = "paystack",
    purpose: str | None = "account_credit_deposit",
    status: str = "failed",
    created_at: datetime | None = None,
) -> TopupIntent:
    intent = TopupIntent(
        account_id=subscriber.id,
        provider_id=provider.id,
        capability_binding_id=checkout_binding.id,
        purpose=purpose,
        allocation_policy="credit_only",
        credit_application_policy="pay_eligible_invoices",
        policy_version=1,
        preview_fingerprint="a" * 64,
        idempotency_key=f"original-topup:{reference}",
        channel="customer_selfcare",
        created_by="pytest:outside-window-recovery",
        reference=reference,
        provider_type=provider_type,
        currency="NGN",
        requested_amount=AUTHORIZED_NET,
        status=status,
        expires_at=OBSERVED_AT - timedelta(days=7),
        created_at=created_at or OBSERVED_AT - timedelta(days=8),
        metadata_={
            "payment_flow": "account_credit_deposit",
            "provider_id": str(provider.id),
            "capability_binding_id": str(checkout_binding.id),
        },
    )
    db.add(intent)
    db.commit()
    db.refresh(intent)
    return intent


def _success(
    intent: TopupIntent,
    *,
    reference: object = REFERENCE,
    amount: Decimal = GROSS_AMOUNT,
    provider_fee: Decimal = PROVIDER_FEE,
    currency: str = "NGN",
    external_id: str = EXTERNAL_ID,
) -> PaymentGatewayVerificationObservation:
    raw: dict[str, object] = {}
    if reference is not _OMITTED:
        raw["reference"] = reference
    return PaymentGatewayVerificationObservation(
        outcome=PaymentGatewayVerificationOutcome.succeeded,
        transaction=PaymentGatewayTransaction(
            provider_type="paystack",
            external_id=external_id,
            amount=amount,
            provider_fee=provider_fee,
            currency=currency,
            memo_prefix="Paystack",
            raw=raw,
        ),
        provider_status=PaymentGatewayProviderStatus.success,
        reason_code=PaymentGatewayVerificationReason.provider_reported_success,
    )


_OMITTED = object()


def _query(intent: TopupIntent, *, reference: str | None = None):
    return PreviewPaystackOutsideWindowRecoveryQuery(
        intent_id=intent.id,
        reference=reference if reference is not None else intent.reference,
        observed_at=OBSERVED_AT,
    )


def _context(*, key: str = "outside-window-recovery-1") -> CommandContext:
    return CommandContext.system(
        actor="pytest:finance-reviewer",
        scope=PAYSTACK_OUTSIDE_WINDOW_RECOVERY_SCOPE,
        reason="Reviewed exact outside-window Paystack transaction evidence",
        idempotency_key=key,
    )


def _confirmation(
    intent: TopupIntent,
    preview: PaystackOutsideWindowRecoveryPreview,
    *,
    key: str = "outside-window-recovery-1",
    fingerprint: str | None = None,
) -> ConfirmPaystackOutsideWindowRecoveryCommand:
    return ConfirmPaystackOutsideWindowRecoveryCommand(
        intent_id=intent.id,
        reference=intent.reference,
        preview_fingerprint=fingerprint or preview.fingerprint,
        review_reference="FIN-CHANGE-2026-08-29-001",
        confirmed=True,
        context=_context(key=key),
    )


def _financial_counts(db) -> tuple[int, int, int, int, int]:
    return (
        db.query(Payment).count(),
        db.query(PaymentAllocation).count(),
        db.query(PaymentProviderEvent).count(),
        db.query(AuditEvent).count(),
        db.query(EventStore).count(),
    )


def _intent_state(intent: TopupIntent) -> tuple[object, ...]:
    return (
        intent.status,
        intent.completed_payment_id,
        intent.completed_at,
        intent.external_id,
        intent.actual_amount,
        intent.gateway_last_observed_at,
        intent.gateway_last_outcome,
        intent.gateway_last_reason_code,
        intent.gateway_next_reconcile_at,
        intent.gateway_last_reconcile_attempt_at,
        intent.gateway_reconcile_attempt_count,
        intent.gateway_observation_count,
    )


def test_preview_is_read_only_and_returns_only_sanitized_typed_evidence(
    db_session, subscriber, paystack_configuration, monkeypatch
):
    provider, binding = paystack_configuration
    intent = _intent(db_session, subscriber, provider, binding)
    observation = _success(intent)
    verify = Mock(return_value=observation)
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        verify,
    )
    counts_before = _financial_counts(db_session)
    state_before = _intent_state(intent)

    preview = preview_paystack_outside_window_recovery(db_session, _query(intent))

    assert isinstance(preview, PaystackOutsideWindowRecoveryPreview)
    assert preview.intent_id == intent.id
    assert preview.reference == intent.reference
    assert preview.disposition.value == "recoverable"
    assert preview.actionable is True
    assert preview.provider_id == provider.id
    assert preview.checkout_binding_id == binding.id
    assert preview.provider_external_id == EXTERNAL_ID
    assert preview.gross_amount == GROSS_AMOUNT
    assert preview.provider_fee == PROVIDER_FEE
    assert preview.authorized_net_amount == AUTHORIZED_NET
    assert preview.currency == "NGN"
    assert len(preview.fingerprint) == 64
    assert preview.fingerprint == preview.fingerprint.lower()
    assert {field.name for field in fields(preview)} == {
        "intent_id",
        "reference",
        "disposition",
        "actionable",
        "fingerprint",
        "intent_status",
        "intent_created_at",
        "provider_id",
        "checkout_binding_id",
        "provider_external_id",
        "gross_amount",
        "provider_fee",
        "authorized_net_amount",
        "currency",
        "provider_status",
        "reason_code",
        "existing_payment_id",
    }
    for unsafe_name in ("raw", "metadata", "authorization", "customer", "email"):
        assert not hasattr(preview, unsafe_name)
    verify.assert_called_once_with(
        db_session,
        provider_type="paystack",
        reference=intent.reference,
        capability_binding_id=binding.id,
    )
    db_session.refresh(intent)
    assert _intent_state(intent) == state_before
    assert _financial_counts(db_session) == counts_before


@pytest.mark.parametrize("status", ("failed", "abandoned", "canceled", "expired"))
def test_preview_accepts_each_outside_window_terminal_status(
    db_session, subscriber, paystack_configuration, monkeypatch, status
):
    provider, binding = paystack_configuration
    intent = _intent(db_session, subscriber, provider, binding, status=status)
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        Mock(return_value=_success(intent)),
    )

    preview = preview_paystack_outside_window_recovery(db_session, _query(intent))

    assert preview.intent_status.value == status
    assert preview.actionable is True


@pytest.mark.parametrize(
    ("status", "created_at"),
    (
        ("pending", OBSERVED_AT - timedelta(days=8)),
        ("failed", OBSERVED_AT - timedelta(days=6)),
    ),
)
def test_preview_rejects_non_terminal_or_inside_window_before_provider_io(
    db_session,
    subscriber,
    paystack_configuration,
    monkeypatch,
    status,
    created_at,
):
    provider, binding = paystack_configuration
    intent = _intent(
        db_session,
        subscriber,
        provider,
        binding,
        status=status,
        created_at=created_at,
    )
    verify = Mock()
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        verify,
    )

    with pytest.raises(PaymentReconciliationError):
        preview_paystack_outside_window_recovery(db_session, _query(intent))

    verify.assert_not_called()
    assert db_session.query(Payment).count() == 0


@pytest.mark.parametrize(
    ("intent_id", "reference"),
    (
        (uuid4(), REFERENCE),
        (None, "DMAC-DIFFERENT-REFERENCE"),
    ),
)
def test_preview_requires_the_exact_local_intent_and_reference_before_provider_io(
    db_session,
    subscriber,
    paystack_configuration,
    monkeypatch,
    intent_id,
    reference,
):
    provider, binding = paystack_configuration
    intent = _intent(db_session, subscriber, provider, binding)
    verify = Mock()
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        verify,
    )
    query = PreviewPaystackOutsideWindowRecoveryQuery(
        intent_id=intent_id or intent.id,
        reference=reference,
        observed_at=OBSERVED_AT,
    )

    with pytest.raises(PaymentReconciliationError):
        preview_paystack_outside_window_recovery(db_session, query)

    verify.assert_not_called()


def test_preview_is_paystack_only_before_provider_io(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setenv("FLUTTERWAVE_TEST_SECRET", "flw-test-secret")
    monkeypatch.setenv("FLUTTERWAVE_TEST_PUBLIC", "flw-test-public")
    monkeypatch.setenv("FLUTTERWAVE_TEST_WEBHOOK", "flw-test-webhook")
    bindings = enable_payment_provider(db_session, "flutterwave")
    provider = PaymentProvider(
        name=f"Flutterwave Outside Window {uuid4().hex}",
        provider_type=PaymentProviderType.flutterwave,
        is_active=True,
    )
    db_session.add(provider)
    db_session.commit()
    intent = _intent(
        db_session,
        subscriber,
        provider,
        bindings["payments.intent.v1"],
        provider_type="flutterwave",
    )
    verify = Mock()
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        verify,
    )

    with pytest.raises(PaymentReconciliationError):
        preview_paystack_outside_window_recovery(db_session, _query(intent))

    verify.assert_not_called()


def test_preview_requires_one_active_canonical_finance_identity_before_provider_io(
    db_session, subscriber, paystack_configuration, monkeypatch
):
    provider, binding = paystack_configuration
    intent = _intent(db_session, subscriber, provider, binding)
    provider.is_active = False
    db_session.commit()
    verify = Mock()
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        verify,
    )

    with pytest.raises(PaymentReconciliationError) as captured:
        preview_paystack_outside_window_recovery(db_session, _query(intent))

    assert captured.value.code.endswith("provider_configuration_mismatch")
    verify.assert_not_called()


def test_preview_rejects_duplicate_finance_identities_before_provider_io(
    db_session, subscriber, paystack_configuration, monkeypatch
):
    provider, binding = paystack_configuration
    intent = _intent(db_session, subscriber, provider, binding)
    db_session.add(
        PaymentProvider(
            name=f"Duplicate Paystack {uuid4().hex}",
            provider_type=PaymentProviderType.paystack,
            is_active=True,
        )
    )
    db_session.commit()
    verify = Mock()
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        verify,
    )

    with pytest.raises(PaymentReconciliationError) as captured:
        preview_paystack_outside_window_recovery(db_session, _query(intent))

    assert captured.value.code.endswith("provider_configuration_ambiguous")
    verify.assert_not_called()


@pytest.mark.parametrize(
    ("observation", "expected_disposition"),
    (
        (
            PaymentGatewayVerificationObservation(
                outcome=PaymentGatewayVerificationOutcome.awaiting_confirmation,
                provider_status=PaymentGatewayProviderStatus.pending,
                reason_code=(
                    PaymentGatewayVerificationReason.provider_awaiting_confirmation
                ),
            ),
            "awaiting_confirmation",
        ),
        (
            PaymentGatewayVerificationObservation(
                outcome=PaymentGatewayVerificationOutcome.processing,
                provider_status=PaymentGatewayProviderStatus.processing,
                reason_code=PaymentGatewayVerificationReason.provider_reported_processing,
            ),
            "processing",
        ),
        (
            PaymentGatewayVerificationObservation(
                outcome=PaymentGatewayVerificationOutcome.failed,
                provider_status=PaymentGatewayProviderStatus.failed,
                reason_code=PaymentGatewayVerificationReason.provider_reported_failed,
            ),
            "provider_failed",
        ),
        (
            PaymentGatewayVerificationObservation(
                outcome=PaymentGatewayVerificationOutcome.abandoned,
                provider_status=PaymentGatewayProviderStatus.abandoned,
                reason_code=PaymentGatewayVerificationReason.provider_reported_abandoned,
            ),
            "provider_abandoned",
        ),
        (
            PaymentGatewayVerificationObservation(
                outcome=PaymentGatewayVerificationOutcome.unavailable,
                reason_code=PaymentGatewayVerificationReason.provider_unavailable,
            ),
            "provider_unavailable",
        ),
        (
            PaymentGatewayVerificationObservation(
                outcome=PaymentGatewayVerificationOutcome.unknown,
                reason_code=(
                    PaymentGatewayVerificationReason.provider_reference_not_found
                ),
            ),
            "reference_not_found",
        ),
        (
            PaymentGatewayVerificationObservation(
                outcome=PaymentGatewayVerificationOutcome.unknown,
                reason_code=(
                    PaymentGatewayVerificationReason.provider_evidence_incomplete
                ),
            ),
            "evidence_incomplete",
        ),
    ),
)
def test_non_success_preview_is_typed_non_actionable_and_writes_nothing(
    db_session,
    subscriber,
    paystack_configuration,
    monkeypatch,
    observation,
    expected_disposition,
):
    provider, binding = paystack_configuration
    intent = _intent(db_session, subscriber, provider, binding)
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        Mock(return_value=observation),
    )
    counts_before = _financial_counts(db_session)
    state_before = _intent_state(intent)

    preview = preview_paystack_outside_window_recovery(db_session, _query(intent))

    assert preview.disposition.value == expected_disposition
    assert preview.actionable is False
    db_session.refresh(intent)
    assert _intent_state(intent) == state_before
    assert _financial_counts(db_session) == counts_before


@pytest.mark.parametrize(
    ("reference", "expected_disposition"),
    (
        (_OMITTED, "conflict"),
        ("DMAC-WRONG-ECHOED-REFERENCE", "conflict"),
    ),
)
def test_success_requires_exact_echoed_reference(
    db_session,
    subscriber,
    paystack_configuration,
    monkeypatch,
    reference,
    expected_disposition,
):
    provider, binding = paystack_configuration
    intent = _intent(db_session, subscriber, provider, binding)
    observation = _success(
        intent,
        reference=reference,
    )
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        Mock(return_value=observation),
    )

    preview = preview_paystack_outside_window_recovery(db_session, _query(intent))

    assert preview.disposition.value == expected_disposition
    assert preview.actionable is False
    assert db_session.query(Payment).count() == 0


@pytest.mark.parametrize(
    ("amount", "provider_fee", "currency", "expected_disposition"),
    (
        (Decimal("5089.00"), PROVIDER_FEE, "NGN", "conflict"),
        (GROSS_AMOUNT, PROVIDER_FEE, "USD", "conflict"),
        (Decimal("50.00"), Decimal("90.00"), "NGN", "conflict"),
    ),
)
def test_success_requires_exact_gross_fee_net_and_currency_correlation(
    db_session,
    subscriber,
    paystack_configuration,
    monkeypatch,
    amount,
    provider_fee,
    currency,
    expected_disposition,
):
    provider, binding = paystack_configuration
    intent = _intent(db_session, subscriber, provider, binding)
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        Mock(
            return_value=_success(
                intent,
                amount=amount,
                provider_fee=provider_fee,
                currency=currency,
            )
        ),
    )

    preview = preview_paystack_outside_window_recovery(db_session, _query(intent))

    assert preview.disposition.value == expected_disposition
    assert preview.actionable is False
    assert db_session.query(Payment).count() == 0


def test_legacy_preview_preserves_owner_authorized_amount_when_fee_is_merchant_absorbed(
    db_session, subscriber, paystack_configuration, monkeypatch
):
    provider, binding = paystack_configuration
    intent = _intent(
        db_session,
        subscriber,
        provider,
        binding,
        purpose=None,
    )
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        Mock(
            return_value=_success(
                intent,
                amount=AUTHORIZED_NET,
                provider_fee=PROVIDER_FEE,
            )
        ),
    )

    preview = preview_paystack_outside_window_recovery(db_session, _query(intent))

    assert preview.actionable is True
    assert preview.gross_amount == AUTHORIZED_NET
    assert preview.provider_fee == PROVIDER_FEE
    assert preview.authorized_net_amount == AUTHORIZED_NET


def test_preview_rejects_metadata_only_invoice_instruction_before_provider_io(
    db_session, subscriber, paystack_configuration, monkeypatch
):
    provider, binding = paystack_configuration
    intent = _intent(
        db_session,
        subscriber,
        provider,
        binding,
        purpose=None,
    )
    intent.metadata_ = {
        "payment_flow": "invoice_payment",
        "invoice_id": str(uuid4()),
    }
    db_session.commit()
    observe = Mock()
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        observe,
    )

    with pytest.raises(
        PaymentReconciliationError,
        match="metadata-only or mismatched invoice instruction",
    ) as exc_info:
        preview_paystack_outside_window_recovery(db_session, _query(intent))

    assert exc_info.value.code.endswith("recovery_structural_invoice_required")
    observe.assert_not_called()


def test_preview_rejects_untrusted_or_failed_existing_provider_event_evidence(
    db_session, subscriber, paystack_configuration, monkeypatch
):
    provider, binding = paystack_configuration
    intent = _intent(db_session, subscriber, provider, binding)
    payment = billing_service.payments.record_verified_provider_settlement(
        db_session,
        account_id=subscriber.id,
        provider_id=provider.id,
        external_id=EXTERNAL_ID,
        gross_amount=GROSS_AMOUNT,
        provider_fee=PROVIDER_FEE,
        net_amount=AUTHORIZED_NET,
        currency="NGN",
        memo="Existing exact payment with unusable provider event evidence",
    ).payment
    db_session.add(
        PaymentProviderEvent(
            provider_id=provider.id,
            payment_id=payment.id,
            event_type="gateway.reconciliation.succeeded",
            external_id=EXTERNAL_ID,
            idempotency_key=f"paystack-{intent.reference}",
            source=PaymentProviderEventSource.gateway_reconciliation,
            observation_digest="e" * 64,
            observed_payment_status=PaymentStatus.succeeded,
            amount=GROSS_AMOUNT,
            provider_fee=PROVIDER_FEE,
            net_amount=AUTHORIZED_NET,
            provider_reference=intent.reference,
            currency="NGN",
            financial_effect=PaymentProviderEventFinancialEffect.none,
            status=PaymentProviderEventStatus.failed,
            error_code="historical_processing_failure",
            error="Historical processing failed",
            processed_at=OBSERVED_AT - timedelta(days=1),
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        Mock(return_value=_success(intent)),
    )

    preview = preview_paystack_outside_window_recovery(db_session, _query(intent))

    assert preview.disposition.value == "conflict"
    assert preview.reason_code == "existing_provider_event_conflict"
    assert preview.actionable is False


def test_confirm_performs_a_second_fresh_verification_before_moving_money(
    db_session, subscriber, paystack_configuration, monkeypatch
):
    provider, binding = paystack_configuration
    intent = _intent(db_session, subscriber, provider, binding)
    verify = Mock(side_effect=[_success(intent), _success(intent)])
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        verify,
    )
    preview = preview_paystack_outside_window_recovery(db_session, _query(intent))

    result = confirm_paystack_outside_window_recovery(
        db_session,
        _confirmation(intent, preview),
    )

    assert verify.call_count == 2
    assert result.intent_id == intent.id
    assert result.disposition.value == "recovered"
    assert result.payment_id is not None
    assert result.preview_fingerprint == preview.fingerprint
    assert result.replayed is False
    assert db_session.query(Payment).count() == 1
    assert db_session.query(PaymentProviderEvent).count() == 1
    db_session.refresh(intent)
    assert intent.completed_payment_id == result.payment_id
    assert intent.status == "completed"


def test_confirm_rejects_a_stale_fingerprint_after_fresh_verification(
    db_session, subscriber, paystack_configuration, monkeypatch
):
    provider, binding = paystack_configuration
    intent = _intent(db_session, subscriber, provider, binding)
    verify = Mock(side_effect=[_success(intent), _success(intent)])
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        verify,
    )
    preview = preview_paystack_outside_window_recovery(db_session, _query(intent))

    with pytest.raises(PaymentReconciliationError) as captured:
        confirm_paystack_outside_window_recovery(
            db_session,
            _confirmation(intent, preview, fingerprint="0" * 64),
        )

    assert captured.value.code.endswith("stale_preview")
    assert verify.call_count == 2
    assert db_session.query(Payment).count() == 0
    assert db_session.query(PaymentProviderEvent).count() == 0
    db_session.refresh(intent)
    assert intent.completed_payment_id is None


def test_confirm_fails_closed_when_the_fresh_provider_result_is_not_successful(
    db_session, subscriber, paystack_configuration, monkeypatch
):
    provider, binding = paystack_configuration
    intent = _intent(db_session, subscriber, provider, binding)
    unavailable = PaymentGatewayVerificationObservation(
        outcome=PaymentGatewayVerificationOutcome.unavailable,
        reason_code=PaymentGatewayVerificationReason.provider_unavailable,
    )
    verify = Mock(side_effect=[_success(intent), unavailable])
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        verify,
    )
    preview = preview_paystack_outside_window_recovery(db_session, _query(intent))
    state_before = _intent_state(intent)

    with pytest.raises(PaymentReconciliationError):
        confirm_paystack_outside_window_recovery(
            db_session,
            _confirmation(intent, preview),
        )

    assert verify.call_count == 2
    assert db_session.query(Payment).count() == 0
    assert db_session.query(PaymentProviderEvent).count() == 0
    db_session.refresh(intent)
    assert _intent_state(intent) == state_before


def test_exact_confirmation_replays_one_result_and_one_audit_effect(
    db_session, subscriber, paystack_configuration, monkeypatch
):
    provider, binding = paystack_configuration
    intent = _intent(db_session, subscriber, provider, binding)
    verify = Mock(side_effect=[_success(intent), _success(intent)])
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        verify,
    )
    preview = preview_paystack_outside_window_recovery(db_session, _query(intent))
    command = _confirmation(intent, preview)

    first = confirm_paystack_outside_window_recovery(db_session, command)
    audit_count = db_session.query(AuditEvent).count()
    event_count = db_session.query(EventStore).count()
    replay = confirm_paystack_outside_window_recovery(db_session, command)

    assert replay.replayed is True
    assert replay.recovery_run_id == first.recovery_run_id
    assert replay.payment_id == first.payment_id
    assert verify.call_count == 2
    assert db_session.query(Payment).count() == 1
    assert db_session.query(PaymentProviderEvent).count() == 1
    assert db_session.query(AuditEvent).count() == audit_count
    assert db_session.query(EventStore).count() == event_count
    recovery_audits = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.actor_id == "pytest:finance-reviewer")
        .all()
    )
    assert any(
        (audit.metadata_ or {}).get("review_reference") == "FIN-CHANGE-2026-08-29-001"
        and (audit.metadata_ or {}).get("preview_fingerprint") == preview.fingerprint
        for audit in recovery_audits
    )


def test_reused_idempotency_key_with_changed_review_evidence_is_a_typed_conflict(
    db_session, subscriber, paystack_configuration, monkeypatch
):
    provider, binding = paystack_configuration
    intent = _intent(db_session, subscriber, provider, binding)
    verify = Mock(side_effect=[_success(intent), _success(intent)])
    monkeypatch.setattr(
        "app.services.payment_reconciliation.payment_gateway_adapter.observe_verification",
        verify,
    )
    preview = preview_paystack_outside_window_recovery(db_session, _query(intent))
    command = _confirmation(intent, preview)
    confirm_paystack_outside_window_recovery(db_session, command)
    conflicting_command = ConfirmPaystackOutsideWindowRecoveryCommand(
        intent_id=command.intent_id,
        reference=command.reference,
        preview_fingerprint=command.preview_fingerprint,
        review_reference="FIN-CHANGE-2026-08-29-DIFFERENT",
        confirmed=True,
        context=command.context,
    )

    with pytest.raises(PaymentReconciliationError) as captured:
        confirm_paystack_outside_window_recovery(db_session, conflicting_command)

    assert captured.value.code.endswith("recovery_idempotency_conflict")
    assert verify.call_count == 2
    assert db_session.query(Payment).count() == 1
    assert db_session.query(PaymentProviderEvent).count() == 1
