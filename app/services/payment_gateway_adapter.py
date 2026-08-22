"""Payment gateway boundary for customer-facing payment flows."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.adapters import adapter_registry
from app.services.integrations import payment_capability


@dataclass(frozen=True)
class PaymentGatewayContext:
    provider_type: str
    public_key: str | None
    reference: str
    capability_binding_id: UUID


@dataclass(frozen=True)
class PaymentGatewayTransaction:
    provider_type: str
    external_id: str
    amount: Decimal
    currency: str
    provider_fee: Decimal = Decimal("0.00")
    metadata: dict[str, object] = field(default_factory=dict)
    memo_prefix: str = ""
    raw: dict[str, object] = field(default_factory=dict)


class PaymentGatewayVerificationOutcome(str, Enum):
    """Closed provider observation vocabulary; it carries no billing decision."""

    succeeded = "succeeded"
    awaiting_confirmation = "awaiting_confirmation"
    processing = "processing"
    failed = "failed"
    abandoned = "abandoned"
    unavailable = "unavailable"
    unknown = "unknown"


class PaymentGatewayProviderStatus(str, Enum):
    """Allowlisted transaction statuses emitted by supported provider adapters."""

    success = "success"
    successful = "successful"
    succeeded = "succeeded"
    failed = "failed"
    abandoned = "abandoned"
    ongoing = "ongoing"
    pending = "pending"
    processing = "processing"
    queued = "queued"
    reversed = "reversed"


class PaymentGatewayVerificationReason(str, Enum):
    """Safe evidence codes suitable for persistence and presentation."""

    provider_reported_success = "provider_reported_success"
    provider_reported_failed = "provider_reported_failed"
    provider_reported_abandoned = "provider_reported_abandoned"
    provider_reported_reversed = "provider_reported_reversed"
    provider_reported_processing = "provider_reported_processing"
    provider_awaiting_confirmation = "provider_awaiting_confirmation"
    provider_reference_not_found = "provider_reference_not_found"
    provider_unavailable = "provider_unavailable"
    provider_evidence_incomplete = "provider_evidence_incomplete"
    provider_status_unknown = "provider_status_unknown"


@dataclass(frozen=True)
class PaymentGatewayVerificationObservation:
    """Provider verification fact without billing consequences."""

    outcome: PaymentGatewayVerificationOutcome
    transaction: PaymentGatewayTransaction | None = None
    provider_status: PaymentGatewayProviderStatus | None = None
    reason_code: PaymentGatewayVerificationReason = (
        PaymentGatewayVerificationReason.provider_status_unknown
    )


class PaymentGatewayVerificationError(ValueError):
    """Typed non-success observation returned by the legacy verify boundary."""

    def __init__(self, observation: PaymentGatewayVerificationObservation) -> None:
        super().__init__(observation.reason_code.value)
        self.observation = observation


def _string_keyed_mapping(value: object) -> dict[str, object]:
    """Narrow an untrusted provider value to safe string-keyed metadata."""

    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _kobo_value(value: object) -> int | str | Decimal:
    """Narrow an untrusted provider amount before currency conversion."""

    if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
        return 0
    return value


class PaymentGatewayRefundState(str, Enum):
    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"
    needs_attention = "needs_attention"


@dataclass(frozen=True)
class PaymentGatewayRefund:
    provider_type: str
    external_id: str
    transaction_id: str
    amount: Decimal
    status: str
    state: PaymentGatewayRefundState
    raw: dict[str, object] = field(default_factory=dict)


class PaymentGatewayAdapter:
    """Normalize Paystack and Flutterwave operations for UI flows."""

    name = "payment_gateway"

    def build_context(
        self,
        db: Session,
        *,
        provider_type: str,
        capability_binding_id: UUID,
        invoice_number: str | None = None,
    ) -> PaymentGatewayContext:
        if provider_type not in {"paystack", "flutterwave"}:
            raise ValueError(f"Unsupported payment provider {provider_type!r}")
        return PaymentGatewayContext(
            provider_type=provider_type,
            public_key=payment_capability.get_public_key(
                db,
                provider_type,
                checkout_binding_id=capability_binding_id,
            ),
            reference=payment_capability.generate_reference(invoice_number),
            capability_binding_id=capability_binding_id,
        )

    def verify(
        self,
        db: Session,
        *,
        provider_type: str,
        reference: str,
        capability_binding_id: UUID | str | None = None,
    ) -> PaymentGatewayTransaction:
        observation = self.observe_verification(
            db,
            provider_type=provider_type,
            reference=reference,
            capability_binding_id=capability_binding_id,
        )
        if (
            observation.outcome is not PaymentGatewayVerificationOutcome.succeeded
            or observation.transaction is None
        ):
            raise PaymentGatewayVerificationError(observation)
        return observation.transaction

    def observe_verification(
        self,
        db: Session,
        *,
        provider_type: str,
        reference: str,
        capability_binding_id: UUID | str | None = None,
    ) -> PaymentGatewayVerificationObservation:
        """Observe one gateway reference without deciding an intent transition."""

        if provider_type not in {"paystack", "flutterwave"}:
            raise ValueError(f"Unsupported payment provider {provider_type!r}")

        try:
            tx = payment_capability.verify_transaction(
                db,
                provider_type=provider_type,
                reference=reference,
                checkout_binding_id=capability_binding_id,
            )
        except payment_capability.PaymentCapabilityError as exc:
            return PaymentGatewayVerificationObservation(
                outcome=(
                    PaymentGatewayVerificationOutcome.unknown
                    if payment_capability.is_verification_not_found(exc)
                    else PaymentGatewayVerificationOutcome.unavailable
                ),
                reason_code=(
                    PaymentGatewayVerificationReason.provider_reference_not_found
                    if payment_capability.is_verification_not_found(exc)
                    else PaymentGatewayVerificationReason.provider_unavailable
                ),
            )
        except RuntimeError:
            return PaymentGatewayVerificationObservation(
                outcome=PaymentGatewayVerificationOutcome.unavailable,
                reason_code=PaymentGatewayVerificationReason.provider_unavailable,
            )

        raw_status = str(tx.get("status") or "").strip().lower()
        try:
            provider_status = PaymentGatewayProviderStatus(raw_status)
        except ValueError:
            return PaymentGatewayVerificationObservation(
                outcome=PaymentGatewayVerificationOutcome.unknown,
                reason_code=PaymentGatewayVerificationReason.provider_status_unknown,
            )

        successful = (
            provider_status is PaymentGatewayProviderStatus.success
            if provider_type == "paystack"
            else provider_status
            in {
                PaymentGatewayProviderStatus.successful,
                PaymentGatewayProviderStatus.succeeded,
            }
        )
        if successful:
            try:
                transaction = self._normalize_verified_transaction(provider_type, tx)
            except (ArithmeticError, TypeError, ValueError):
                return PaymentGatewayVerificationObservation(
                    outcome=PaymentGatewayVerificationOutcome.unknown,
                    provider_status=provider_status,
                    reason_code=(
                        PaymentGatewayVerificationReason.provider_evidence_incomplete
                    ),
                )
            if (
                not transaction.external_id.strip()
                or len(transaction.external_id.strip()) > 120
                or transaction.amount <= Decimal("0.00")
                or transaction.provider_fee < Decimal("0.00")
                or transaction.provider_fee > transaction.amount
                or len(transaction.currency.strip()) != 3
            ):
                return PaymentGatewayVerificationObservation(
                    outcome=PaymentGatewayVerificationOutcome.unknown,
                    provider_status=provider_status,
                    reason_code=(
                        PaymentGatewayVerificationReason.provider_evidence_incomplete
                    ),
                )
            return PaymentGatewayVerificationObservation(
                outcome=PaymentGatewayVerificationOutcome.succeeded,
                transaction=transaction,
                provider_status=provider_status,
                reason_code=PaymentGatewayVerificationReason.provider_reported_success,
            )
        if provider_type == "paystack":
            if provider_status is PaymentGatewayProviderStatus.abandoned:
                outcome = PaymentGatewayVerificationOutcome.abandoned
                reason = PaymentGatewayVerificationReason.provider_reported_abandoned
            elif provider_status in {
                PaymentGatewayProviderStatus.failed,
                PaymentGatewayProviderStatus.reversed,
            }:
                outcome = PaymentGatewayVerificationOutcome.failed
                reason = (
                    PaymentGatewayVerificationReason.provider_reported_reversed
                    if provider_status is PaymentGatewayProviderStatus.reversed
                    else PaymentGatewayVerificationReason.provider_reported_failed
                )
            elif provider_status is PaymentGatewayProviderStatus.pending:
                outcome = PaymentGatewayVerificationOutcome.awaiting_confirmation
                reason = PaymentGatewayVerificationReason.provider_awaiting_confirmation
            elif provider_status in {
                PaymentGatewayProviderStatus.ongoing,
                PaymentGatewayProviderStatus.processing,
                PaymentGatewayProviderStatus.queued,
            }:
                outcome = PaymentGatewayVerificationOutcome.processing
                reason = PaymentGatewayVerificationReason.provider_reported_processing
            else:
                outcome = PaymentGatewayVerificationOutcome.unknown
                reason = PaymentGatewayVerificationReason.provider_status_unknown
        elif provider_status is PaymentGatewayProviderStatus.failed:
            outcome = PaymentGatewayVerificationOutcome.failed
            reason = PaymentGatewayVerificationReason.provider_reported_failed
        elif provider_status is PaymentGatewayProviderStatus.pending:
            outcome = PaymentGatewayVerificationOutcome.awaiting_confirmation
            reason = PaymentGatewayVerificationReason.provider_awaiting_confirmation
        else:
            outcome = PaymentGatewayVerificationOutcome.unknown
            reason = PaymentGatewayVerificationReason.provider_status_unknown
        return PaymentGatewayVerificationObservation(
            outcome=outcome,
            provider_status=provider_status,
            reason_code=reason,
        )

    @staticmethod
    def _normalize_verified_transaction(
        provider_type: str, tx: dict[str, object]
    ) -> PaymentGatewayTransaction:
        if provider_type == "flutterwave":
            return PaymentGatewayTransaction(
                provider_type=provider_type,
                external_id=str(tx.get("id", "")),
                amount=Decimal(str(tx.get("amount", 0))),
                currency=str(tx.get("currency") or "NGN"),
                provider_fee=Decimal(str(tx.get("app_fee") or 0)),
                metadata=_string_keyed_mapping(tx.get("meta")),
                memo_prefix="Flutterwave",
                raw=dict(tx),
            )
        return PaymentGatewayTransaction(
            provider_type=provider_type,
            external_id=str(tx.get("id", "")),
            amount=payment_capability.kobo_to_naira(_kobo_value(tx.get("amount"))),
            currency=str(tx.get("currency") or "NGN"),
            provider_fee=payment_capability.kobo_to_naira(_kobo_value(tx.get("fees"))),
            metadata=_string_keyed_mapping(tx.get("metadata")),
            memo_prefix="Paystack",
            raw=dict(tx),
        )

    def refund(
        self,
        db: Session,
        *,
        provider_type: str,
        reference: str,
        amount: Decimal | None = None,
        transaction_id: str | None = None,
        request_key: str | None = None,
    ) -> PaymentGatewayRefund:
        if provider_type == "flutterwave":
            tx_id = str(transaction_id or "").strip()
            if not tx_id:
                tx = payment_capability.verify_transaction(
                    db, provider_type="flutterwave", reference=reference
                )
                tx_id = str(tx.get("id") or "").strip()
            if not tx_id:
                raise ValueError("Flutterwave transaction id not found for reference")
            raw = dict(
                payment_capability.refund_transaction(
                    db,
                    provider_type="flutterwave",
                    transaction_id=tx_id,
                    amount=amount,
                    request_key=request_key,
                )
            )
            return self._normalize_refund("flutterwave", raw, tx_id)

        if provider_type == "paystack":
            raw = dict(
                payment_capability.refund_transaction(
                    db,
                    provider_type="paystack",
                    transaction_id=reference,
                    amount=amount,
                    request_key=request_key,
                )
            )
            return self._normalize_refund(
                "paystack", raw, str(transaction_id or reference)
            )

        raise ValueError(f"Refunds are not supported for provider {provider_type!r}")

    def find_refund(
        self,
        db: Session,
        *,
        provider_type: str,
        transaction_id: str,
        request_key: str,
        refund_id: str | None = None,
    ) -> PaymentGatewayRefund | None:
        """Observe a prior refund without initiating another money movement."""
        if provider_type == "paystack":
            if refund_id:
                raw = dict(
                    payment_capability.fetch_refund(
                        db, provider_type="paystack", refund_id=refund_id
                    )
                )
                return self._normalize_refund(provider_type, raw, transaction_id)
            rows = payment_capability.list_refunds(
                db, provider_type="paystack", transaction_id=transaction_id
            )
            for row in rows:
                if str(row.get("merchant_note") or "").strip() == request_key:
                    return self._normalize_refund(provider_type, row, transaction_id)
            return None

        if provider_type == "flutterwave":
            if refund_id:
                raw = dict(
                    payment_capability.fetch_refund(
                        db, provider_type="flutterwave", refund_id=refund_id
                    )
                )
                return self._normalize_refund(provider_type, raw, transaction_id)
            rows = payment_capability.list_refunds(
                db, provider_type="flutterwave", transaction_id=transaction_id
            )
            for row in rows:
                if request_key in str(row.get("comments") or ""):
                    return self._normalize_refund(provider_type, row, transaction_id)
            return None

        raise ValueError(f"Refunds are not supported for provider {provider_type!r}")

    @staticmethod
    def _normalize_refund(
        provider_type: str,
        raw: dict[str, object],
        transaction_id: str,
    ) -> PaymentGatewayRefund:
        status = str(raw.get("status") or "unknown").strip().lower()
        if provider_type == "paystack":
            amount = Decimal(str(raw.get("amount") or 0)) / 100
            if status == "processed":
                state = PaymentGatewayRefundState.succeeded
            elif status == "failed":
                state = PaymentGatewayRefundState.failed
            elif status in {"needs-attention", "needs_attention"}:
                state = PaymentGatewayRefundState.needs_attention
            else:
                state = PaymentGatewayRefundState.pending
        else:
            amount = Decimal(
                str(
                    raw.get("amount_refunded")
                    or raw.get("AmountRefunded")
                    or raw.get("amount")
                    or 0
                )
            )
            successful = {
                "successful",
                "succeeded",
                "completed-bank-transfer",
                "completed-momo",
                "completed-mpgs",
                "completed-offline",
                "completed-preauth",
            }
            meta = raw.get("meta")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (TypeError, ValueError):
                    meta = {}
            disburse_status = (
                str(meta.get("disburse_status") or "").strip().lower()
                if isinstance(meta, dict)
                else ""
            )
            if disburse_status == "failed":
                state = PaymentGatewayRefundState.failed
            elif status in successful or disburse_status in {
                "successful",
                "succeeded",
            }:
                state = PaymentGatewayRefundState.succeeded
            elif status == "failed":
                state = PaymentGatewayRefundState.failed
            else:
                state = PaymentGatewayRefundState.pending

        refund_id = str(raw.get("id") or raw.get("flw_ref") or "").strip()
        return PaymentGatewayRefund(
            provider_type=provider_type,
            external_id=refund_id,
            transaction_id=transaction_id,
            amount=amount,
            status=status,
            state=state,
            raw=raw,
        )


payment_gateway_adapter = PaymentGatewayAdapter()
adapter_registry.register(payment_gateway_adapter)
