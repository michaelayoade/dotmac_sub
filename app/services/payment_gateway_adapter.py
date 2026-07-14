"""Payment gateway boundary for customer-facing payment flows."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from sqlalchemy.orm import Session

from app.services.adapters import adapter_registry


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
    metadata: dict[str, object] = field(default_factory=dict)
    memo_prefix: str = ""
    raw: dict[str, object] = field(default_factory=dict)


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
        if provider_type == "flutterwave":
            from app.services.flutterwave import generate_reference, get_public_key

            return PaymentGatewayContext(
                provider_type="flutterwave",
                public_key=get_public_key(db),
                reference=generate_reference(invoice_number),
            )

        if provider_type != "paystack":
            raise ValueError(f"Unsupported payment provider {provider_type!r}")

        from app.services.paystack import generate_reference, get_public_key

        return PaymentGatewayContext(
            provider_type="paystack",
            public_key=get_public_key(db),
            reference=generate_reference(invoice_number),
        )

    def verify(
        self,
        db: Session,
        *,
        provider_type: str,
        reference: str,
    ) -> PaymentGatewayTransaction:
        if provider_type == "flutterwave":
            from app.services import flutterwave as flutterwave_svc

            tx = flutterwave_svc.verify_transaction(db, reference)
            if tx.get("status") != "successful":
                raise ValueError(
                    f"Payment was not successful (status: {tx.get('status')})"
                )
            return PaymentGatewayTransaction(
                provider_type="flutterwave",
                external_id=str(tx.get("id", "")),
                amount=Decimal(str(tx.get("amount", 0))),
                currency=str(tx.get("currency") or "NGN"),
                metadata=dict(tx.get("meta") or {}),
                memo_prefix="Flutterwave",
                raw=dict(tx),
            )

        if provider_type != "paystack":
            raise ValueError(f"Unsupported payment provider {provider_type!r}")

        from app.services.paystack import kobo_to_naira, verify_transaction

        tx = verify_transaction(db, reference)
        if tx.get("status") != "success":
            raise ValueError(f"Payment was not successful (status: {tx.get('status')})")
        return PaymentGatewayTransaction(
            provider_type="paystack",
            external_id=str(tx.get("id", "")),
            amount=kobo_to_naira(tx.get("amount", 0)),
            currency=str(tx.get("currency") or "NGN"),
            metadata=dict(tx.get("metadata") or {}),
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
            from app.services import flutterwave as flutterwave_svc

            tx_id = str(transaction_id or "").strip()
            if not tx_id:
                tx = flutterwave_svc.verify_transaction(db, reference)
                tx_id = str(tx.get("id") or "").strip()
            if not tx_id:
                raise ValueError("Flutterwave transaction id not found for reference")
            raw = dict(
                flutterwave_svc.refund_transaction(
                    db,
                    tx_id,
                    amount,
                    request_key=request_key,
                )
            )
            return self._normalize_refund("flutterwave", raw, tx_id)

        if provider_type == "paystack":
            from app.services import paystack as paystack_svc

            raw = dict(
                paystack_svc.refund_transaction(
                    db,
                    reference,
                    amount,
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
            from app.services import paystack as paystack_service

            if refund_id:
                raw = dict(paystack_service.fetch_refund(db, refund_id))
                return self._normalize_refund(provider_type, raw, transaction_id)
            rows = paystack_service.list_refunds(db, transaction_id=transaction_id)
            for row in rows:
                if str(row.get("merchant_note") or "").strip() == request_key:
                    return self._normalize_refund(provider_type, row, transaction_id)
            return None

        if provider_type == "flutterwave":
            from app.services import flutterwave as flutterwave_service

            if refund_id:
                raw = dict(flutterwave_service.fetch_refund(db, refund_id))
                return self._normalize_refund(provider_type, raw, transaction_id)
            rows = flutterwave_service.list_refunds(db, transaction_id=transaction_id)
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
