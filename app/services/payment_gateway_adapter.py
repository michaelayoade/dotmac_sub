"""Payment gateway boundary for customer-facing payment flows."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session


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


class PaymentGatewayAdapter:
    """Normalize Paystack and Flutterwave operations for UI flows."""

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


payment_gateway_adapter = PaymentGatewayAdapter()

