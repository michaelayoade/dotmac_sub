"""Capability facade for payment-provider transports and inbound authentication."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.services.integrations import installations
from app.services.integrations.connectors.payment_gateway import (
    PAYMENT_INTENT_CAPABILITY,
    PAYMENT_RECONCILE_CAPABILITY,
    PAYMENT_REFUND_CAPABILITY,
    PAYMENT_WEBHOOK_CAPABILITY,
)
from app.services.integrations.runtime import OperationStatus, OperationTrigger
from app.services.integrations.runtime_execution import (
    build_execution_context,
    make_operation_executor,
)
from app.services.secrets import resolve_secret


class PaymentCapabilityError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error_code = message
        self.status_code: int | None = None
        if message.startswith("provider_http_"):
            try:
                self.status_code = int(message.rsplit("_", 1)[1])
            except ValueError:
                pass


def _connector(provider_type: str) -> str:
    provider = provider_type.strip().lower()
    if provider not in {"paystack", "flutterwave"}:
        raise PaymentCapabilityError(f"unsupported payment provider {provider_type!r}")
    return provider


def _binding(db: Session, provider_type: str, capability_id: str):
    return installations.require_enabled_capability_binding(
        db, connector_key=_connector(provider_type), capability_id=capability_id
    )


def _execute(
    db: Session,
    *,
    provider_type: str,
    capability_id: str,
    action: str,
    params: dict[str, Any],
    trigger: OperationTrigger,
    correlation_id: str,
) -> dict[str, Any]:
    binding = _binding(db, provider_type, capability_id)
    context = build_execution_context(db, capability_binding_id=binding.id)
    result = make_operation_executor(
        context,
        correlation_id=correlation_id[:160],
        trigger=trigger,
        actor="integration.payments",
    )(action, params)
    if result.status != OperationStatus.succeeded:
        raise PaymentCapabilityError(result.error_code or "payment capability failed")
    return dict(result.output)


def generate_reference(invoice_number: str | None = None) -> str:
    suffix = uuid.uuid4().hex[:8]
    return f"DMAC-{invoice_number}-{suffix}" if invoice_number else f"DMAC-{suffix}"


def amount_to_kobo(amount: Decimal | float | int) -> int:
    return int(Decimal(str(amount)) * 100)


def kobo_to_naira(kobo: int | str | Decimal) -> Decimal:
    return Decimal(str(kobo)) / 100


def get_public_key(db: Session, provider_type: str) -> str:
    return str(
        _execute(
            db,
            provider_type=provider_type,
            capability_id=PAYMENT_INTENT_CAPABILITY,
            action="get_public_key",
            params={},
            trigger=OperationTrigger.interactive,
            correlation_id=f"payment-public-key:{provider_type}",
        ).get("value")
        or ""
    )


def initialize_transaction(
    db: Session,
    *,
    provider_type: str,
    email: str,
    reference: str,
    redirect_url: str,
    amount: Decimal | float | int | None = None,
    amount_kobo: int | None = None,
    metadata: dict[str, Any] | None = None,
    currency: str | None = None,
) -> dict[str, Any]:
    return dict(
        _execute(
            db,
            provider_type=provider_type,
            capability_id=PAYMENT_INTENT_CAPABILITY,
            action="initialize",
            params={
                "email": email,
                "reference": reference,
                "redirect_url": redirect_url,
                "amount": amount,
                "amount_kobo": amount_kobo,
                "metadata": metadata or {},
                "currency": currency,
            },
            trigger=OperationTrigger.interactive,
            correlation_id=f"payment-initialize:{provider_type}:{reference}",
        ).get("item")
        or {}
    )


def charge_authorization(
    db: Session,
    *,
    authorization_code: str,
    email: str,
    amount_kobo: int,
    reference: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return dict(
        _execute(
            db,
            provider_type="paystack",
            capability_id=PAYMENT_INTENT_CAPABILITY,
            action="charge_authorization",
            params={
                "authorization_code": authorization_code,
                "email": email,
                "amount_kobo": amount_kobo,
                "reference": reference,
                "metadata": metadata or {},
            },
            trigger=OperationTrigger.event,
            correlation_id=f"payment-charge-authorization:{reference}",
        ).get("item")
        or {}
    )


def verify_transaction(
    db: Session, *, provider_type: str, reference: str
) -> dict[str, Any]:
    return dict(
        _execute(
            db,
            provider_type=provider_type,
            capability_id=PAYMENT_RECONCILE_CAPABILITY,
            action="verify",
            params={"reference": reference},
            trigger=OperationTrigger.reconcile,
            correlation_id=f"payment-verify:{provider_type}:{reference}",
        ).get("item")
        or {}
    )


def refund_transaction(
    db: Session,
    *,
    provider_type: str,
    transaction_id: str,
    amount: Decimal | None = None,
    request_key: str | None = None,
) -> dict[str, Any]:
    return dict(
        _execute(
            db,
            provider_type=provider_type,
            capability_id=PAYMENT_REFUND_CAPABILITY,
            action="refund",
            params={
                "transaction_id": transaction_id,
                "amount": str(amount) if amount is not None else None,
                "request_key": request_key,
            },
            trigger=OperationTrigger.event,
            correlation_id=f"payment-refund:{provider_type}:{request_key or transaction_id}",
        ).get("item")
        or {}
    )


def fetch_refund(db: Session, *, provider_type: str, refund_id: str) -> dict[str, Any]:
    return dict(
        _execute(
            db,
            provider_type=provider_type,
            capability_id=PAYMENT_RECONCILE_CAPABILITY,
            action="fetch_refund",
            params={"refund_id": refund_id},
            trigger=OperationTrigger.reconcile,
            correlation_id=f"payment-refund-read:{provider_type}:{refund_id}",
        ).get("item")
        or {}
    )


def list_refunds(
    db: Session, *, provider_type: str, transaction_id: str
) -> list[dict[str, Any]]:
    return list(
        _execute(
            db,
            provider_type=provider_type,
            capability_id=PAYMENT_RECONCILE_CAPABILITY,
            action="list_refunds",
            params={"transaction_id": transaction_id},
            trigger=OperationTrigger.reconcile,
            correlation_id=f"payment-refunds:{provider_type}:{transaction_id}",
        ).get("items")
        or []
    )


def list_transactions_page(
    db: Session,
    *,
    provider_type: str,
    from_date: str,
    to_date: str,
    status: str | None,
    page: int,
    per_page: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    output = _execute(
        db,
        provider_type=provider_type,
        capability_id=PAYMENT_RECONCILE_CAPABILITY,
        action="list_transactions",
        params={
            "from_date": from_date,
            "to_date": to_date,
            "status": status,
            "page": page,
            "per_page": per_page,
        },
        trigger=OperationTrigger.reconcile,
        correlation_id=(
            f"payment-transactions:{provider_type}:{from_date}:{to_date}:{page}"
        ),
    )
    return list(output.get("items") or []), dict(output.get("meta") or {})


def inbound_context(
    db: Session,
    *,
    provider_type: str,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
):
    binding = _binding(db, provider_type, PAYMENT_WEBHOOK_CAPABILITY)
    context = build_execution_context(
        db, capability_binding_id=binding.id, secret_resolver=secret_resolver
    )
    return binding, dict(context.secret_material)


def verify_webhook_signature(
    db: Session, *, provider_type: str, body: bytes, signature: str
) -> tuple[Any, bool]:
    binding, material = inbound_context(db, provider_type=provider_type)
    if provider_type == "paystack":
        secret = str(material.get("gateway_credentials") or "")
        expected = (
            hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
            if secret
            else ""
        )
    else:
        expected = str(material.get("webhook_signing_secret") or "")
    return binding, bool(
        expected and signature and hmac.compare_digest(expected, signature)
    )
