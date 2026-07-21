"""Payment gateway boundary for customer-facing payment flows."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from sqlalchemy.orm import Session

from app.services.adapters import adapter_registry
from app.services.integrations import payment_capability


@dataclass(frozen=True)
class PaymentGatewayContext:
    provider_type: str
    public_key: str | None
    reference: str


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
    """Closed transport observation returned to reconciliation policy."""

    succeeded = "succeeded"
    not_found = "not_found"
    not_successful = "not_successful"
    unavailable = "unavailable"


@dataclass(frozen=True)
class PaymentGatewayVerificationObservation:
    """Provider verification fact without billing consequences."""

    outcome: PaymentGatewayVerificationOutcome
    transaction: PaymentGatewayTransaction | None = None
    error_code: str | None = None


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
        invoice_number: str | None = None,
    ) -> PaymentGatewayContext:
        if provider_type not in {"paystack", "flutterwave"}:
            raise ValueError(f"Unsupported payment provider {provider_type!r}")
        return PaymentGatewayContext(
            provider_type=provider_type,
            public_key=payment_capability.get_public_key(db, provider_type),
            reference=payment_capability.generate_reference(invoice_number),
        )

    def verify(
        self,
        db: Session,
        *,
        provider_type: str,
        reference: str,
    ) -> PaymentGatewayTransaction:
        if provider_type == "flutterwave":
            tx = payment_capability.verify_transaction(
                db, provider_type="flutterwave", reference=reference
            )
            if tx.get("status") != "successful":
                raise ValueError(
                    f"Payment was not successful (status: {tx.get('status')})"
                )
            return PaymentGatewayTransaction(
                provider_type="flutterwave",
                external_id=str(tx.get("id", "")),
                amount=Decimal(str(tx.get("amount", 0))),
                currency=str(tx.get("currency") or "NGN"),
                provider_fee=Decimal(str(tx.get("app_fee") or 0)),
                metadata=dict(tx.get("meta") or {}),
                memo_prefix="Flutterwave",
                raw=dict(tx),
            )

        if provider_type != "paystack":
            raise ValueError(f"Unsupported payment provider {provider_type!r}")

        tx = payment_capability.verify_transaction(
            db, provider_type="paystack", reference=reference
        )
        if tx.get("status") != "success":
            raise ValueError(f"Payment was not successful (status: {tx.get('status')})")
        return PaymentGatewayTransaction(
            provider_type="paystack",
            external_id=str(tx.get("id", "")),
            amount=payment_capability.kobo_to_naira(tx.get("amount", 0)),
            currency=str(tx.get("currency") or "NGN"),
            provider_fee=payment_capability.kobo_to_naira(tx.get("fees", 0)),
            metadata=dict(tx.get("metadata") or {}),
            memo_prefix="Paystack",
            raw=dict(tx),
        )

    def observe_verification(
        self,
        db: Session,
        *,
        provider_type: str,
        reference: str,
    ) -> PaymentGatewayVerificationObservation:
        """Observe one gateway reference and normalize transport failures."""

        try:
            transaction = self.verify(
                db,
                provider_type=provider_type,
                reference=reference,
            )
        except payment_capability.PaymentCapabilityError as exc:
            outcome = (
                PaymentGatewayVerificationOutcome.not_found
                if payment_capability.is_verification_not_found(exc)
                else PaymentGatewayVerificationOutcome.unavailable
            )
            return PaymentGatewayVerificationObservation(
                outcome=outcome,
                error_code=exc.error_code,
            )
        except ValueError as exc:
            return PaymentGatewayVerificationObservation(
                outcome=PaymentGatewayVerificationOutcome.not_successful,
                error_code=type(exc).__name__,
            )
        except RuntimeError as exc:
            return PaymentGatewayVerificationObservation(
                outcome=PaymentGatewayVerificationOutcome.unavailable,
                error_code=type(exc).__name__,
            )
        return PaymentGatewayVerificationObservation(
            outcome=PaymentGatewayVerificationOutcome.succeeded,
            transaction=transaction,
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
